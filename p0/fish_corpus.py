#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语料层门面：`ingest / stats / retag`（T02），`search / read / forget / rebuild / doctor`（T03/T05）。

为什么要有这一层
--------------
CLI、MCP、日报三条调用面**只能认识这一个模块**：它们不该知道 DSH 是 zstd 多帧、
WB 是行流，也不该知道打标要注入两个解析器。门面负责把这些拼起来：

    fish.py ──(注入 project_of / citizen_of)──> fish_corpus ──> 适配器 ──> store

**本模块不 import fish**（判据 J2）：项目/公民解析器由调用方以回调注入。
直接 `python fish_corpus.py ingest` 也能跑，但那样没有解析器，`project_source`
会如实写 `unmapped` + `project_note` 说明原因——不猜项目，也不假装查过。

并发与锁（设计 §4.3 四条）
------------------------
- 正确性靠 `BEGIN IMMEDIATE` + `busy_timeout=15000`（在 store 里）；
- 礼仪锁 `data/_audit/corpus.ingest.lock` 只做"别白跑"，**不是正确性手段**；
  锁内容 `{pid, at, token}`，活锁 → `{busy: True}` 且 **exit 0**（日报不该因并发假红）。
- 锁过期按**时间**判（15 分钟）而不是查 pid 存活：Windows 上 `os.kill(pid, 0)`
  的语义不可靠（可能真去终止进程），拿它做"锁属主还活着吗"是拿数据开玩笑。

为什么检索/治理的错误要分档（T03/T05）
----------------------------------
`search` 查不到东西 → **正常返回空**（`hits=[]` + `total_matched=0`）；
门牌解析失败 → `PointerError`（`bad_pointer`/`not_found`/`ambiguous_pointer` 三档）；
库不存在 → 读动作返回空并声明 `degraded=[{"reason":"db_missing"}]`，写动作报错。
把"没数据""没这个门牌""库还没建"三件事混成一个空结果，正是 PRD 反复警告的假绿。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

import fish_common
import fish_corpus_dsh as dsh_adapter
import fish_corpus_gov as gov_mod
import fish_corpus_rank as rank_mod
import fish_corpus_redact as redact_mod
import fish_corpus_store as store
import fish_corpus_tag as tag_mod
import fish_corpus_wb as wb_adapter

# 适配器注册表（C-P2-1：第三/四宿主只在这里加一行，门面不动）
ADAPTERS = {
    dsh_adapter.HOST: dsh_adapter.DshAdapter,
    wb_adapter.HOST: wb_adapter.WbAdapter,
}
HOSTS = tuple(ADAPTERS)
DEFAULT_HOSTS = (dsh_adapter.HOST, wb_adapter.HOST)

# 门牌/库层错误在门面处再导出：调用方（CLI / MCP）只该认识一个模块，
# 不该为了 catch 一个门牌错误而 import 实现层。
PointerError = store.PointerError
CorpusStoreError = store.CorpusStoreError

LOCK_NAME = "corpus.ingest.lock"
LOCK_TTL_SEC = 900
AUDIT_DIR = "_audit"

ENV_DSH_SESSIONS = "FISH_CORPUS_DSH_SESSIONS"
ENV_WB_PROJECTS = "FISH_CORPUS_WB_PROJECTS"

# `--limit` 的缺省：不限制（K4 冷建要能一次跑完）
DEFAULT_LIMIT = None
# 段文本上限（≈0.5 MB/part）：定义搬去了 `fish_corpus_store`（两个适配器都要用，
# 而适配器**不许** import 门面——那是最短的一个环）。这里保留同名再导出，
# 既有的调用面与文档不用改，也不再是"死常量"（2026-10-27 复审 M6）。
MAX_PART_CHARS = store.MAX_PART_CHARS

# 停滞哨兵（C-P0-9 红②）：连续 N 轮"0 新增"但源在变 → `stalled`。
# 三个读数键的定义在 `store`（`gov.doctor` 也要读同一份，复审 🟡 报告↔代码对齐）。
STALL_STREAK_ALARM = 3
META_STALL_STREAK = store.META_STALL_STREAK
META_STALL_SIG = store.META_STALL_SIG
META_STALL_ALARM = store.META_STALL_ALARM

# 采集时间窗口（2026-10-27 用户决策：只投影最近 3 天，更早暂不投影、留待后续回填）。
# `DEFAULT_WINDOW_DAYS` 只给 **CLI 缺省**用；`ingest()` 函数默认 `days=None`（不限），
# 既有调用方（日报 / 各测试）行为一字不变——窗口是"调用方选择的口径"，不是函数暗改。
DEFAULT_WINDOW_DAYS = 3
SECONDS_PER_DAY = 86400
# 读数键定义在 `store`：`ingest` 写、`stats` 读，键名两处各写一套就是两套口径。
META_INGEST_WINDOW_DAYS = store.META_INGEST_WINDOW_DAYS
META_SKIPPED_BY_WINDOW = store.META_INGEST_SKIPPED_BY_WINDOW

# ---------------------------------------------------------------- 检索（T03 · 设计 §6）
SEARCH_DEFAULT_LIMIT = 8
SEARCH_MAX_LIMIT = 32                  # 硬上限：超出夹紧（并在 meta 里如实说）
SEARCH_CANDIDATE_FACTOR = 8            # 送 ranker 的候选 = limit×8 ...
SEARCH_CANDIDATE_MIN = 64              # ... 且不少于 64（太少会让排序退化成"谁先入库")
SNIPPET_CHARS = 200
READ_DEFAULT_MAX_CHARS = 8000

# ---------------------------------------------------------------- 推送（T04 · 设计 §7）
# 这批数字是**门槛**不是调参：`tool_after` 956 推 / 1 采纳（实测）就是"门槛太松"的代价。
PUSH_SEARCH_LIMIT = 3                  # 候选池 3，最终只推 1 条
PUSH_MAX_ITEMS = 1
PUSH_MIN_RELEVANCE = 0.6               # 与 `push.prompt_min_relevance` 同口径
PUSH_MIN_CHARS = 80                    # 短句没有结论价值（"好嘞""继续"）
PUSH_INCLUDE_SUBAGENT = False          # D11：子代理默认不推（可开）
PUSH_TITLE_MAX_CHARS = 34              # 34 + 40 + 门牌 29 + 装饰 ≈ 108 ≤ 120 token 预算
PUSH_REASON_MAX_CHARS = 40
# 语义层类别（T03a）：`kind` 保持 `conv_related`（`test_corpus_push` 锁死），合流侧
# 认的是 `kind_class`——两份字段各有各的读者，不用一个字段同时承担两种含义。
# 常量本体只有一份（`fish_corpus_rank.KIND_CLASS_CORPUS`），这里只是别名：两处各写
# 一个 "corpus" 字面量，改词表时必漏一处。
PUSH_KIND_CLASS = rank_mod.KIND_CLASS_CORPUS
# 重复度的窗口（24h）。与去重**共用同一个数**（有意为之，不再声称"可独立调"——
# 首轮 🟡 Y3 的修法）：去重问"本会话还挡不挡"，重复度问"全局还有多新"，两件不同
# 的事，但用的是同一段历史；分成两个数只会让"被挡住"和"被降权"说的不是一段时间。
PUSH_DUP_WINDOW_HOURS = rank_mod.DUP_WINDOW_HOURS

# CLI 里由 `run_cli` 承担的动作（ingest/stats/retag 是 T02 的既有分支，留在薄壳里）
CLI_ACTIONS = ("search", "read", "forget", "rebuild", "doctor")


class CorpusError(RuntimeError):
    """语料层的通用失败（参数错、依赖缺）。"""


class CorpusNotReady(CorpusError):
    """后续轮次的能力位（P1/P2：档案归档、跨宿主合并等）。

    T03/T05 交付后 `search/read/rebuild/forget/doctor` **都已在位**，本异常不再由它们抛出；
    保留这个类型是为了让"还没做的能力"仍有一个**显式**的失败形态——比返回空结果好，
    因为空结果会让"库里没数据"和"功能没做"混在一起。
    """


# ------------------------------------------------------------------ 宿主发现与根目录


def adapter_for(host):
    """按宿主取适配器实例；未知宿主**响亮失败**（不静默回落）。"""
    key = (host or "").strip().lower()
    if key not in ADAPTERS:
        raise CorpusError("未知宿主 %r（已登记：%s）" % (host, ", ".join(HOSTS)))
    return ADAPTERS[key]()


def default_roots(host) -> list:
    """缺省扫描根：环境变量优先，其次宿主自己的家目录（**不写死绝对路径**）。

    为什么允许环境变量覆盖：测试与"别的机器"都用得上；把
    `I:\\app\\DSH-Home\\home\\sessions` 写死在代码里，等于让这份代码只能在
    这台机器上跑（而语料层是要随 fish 一起搬的）。
    """
    key = (host or "").strip().lower()
    if key == dsh_adapter.HOST:
        explicit = os.environ.get(ENV_DSH_SESSIONS)
        if explicit:
            return [Path(explicit)]
        home = os.environ.get("DSH_HOME")
        base = Path(home) if home else (Path.home() / ".dsh")
        return [base / "sessions"]
    if key == wb_adapter.HOST:
        explicit = os.environ.get(ENV_WB_PROJECTS)
        if explicit:
            return [Path(explicit)]
        return [Path.home() / ".workbuddy" / "projects"]
    raise CorpusError("未知宿主 %r" % (host,))


def _resolve_roots(host, roots):
    """把 `roots` 参数归一成列表：`None`→缺省；单值→单元素；dict→按宿主取。"""
    if isinstance(roots, dict):
        value = roots.get(host)
        return list(value) if isinstance(value, (list, tuple)) else ([value] if value else [])
    if roots is None:
        return default_roots(host)
    if isinstance(roots, (str, os.PathLike)):
        return [roots]
    return list(roots)


# ------------------------------------------------------------------ 礼仪锁


def _lock_path(root) -> Path:
    return Path(root) / AUDIT_DIR / LOCK_NAME


def acquire_lock(root) -> "dict | None":
    """拿礼仪锁；拿到返回锁内容，拿不到返回 None（调用方据此报 `busy`）。"""
    path = _lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"pid": os.getpid(), "at": time.time(), "token": secrets.token_hex(8)}
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = LOCK_TTL_SEC + 1
        if age < LOCK_TTL_SEC:
            return None
        try:
            path.unlink()                     # 过期锁：清掉再来一次
        except OSError:
            return None
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return None
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(payload))
    return payload


def release_lock(root, payload) -> None:
    """放锁。只有 token 对得上才删（不删别人的锁）。"""
    path = _lock_path(root)
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if payload and current.get("token") != payload.get("token"):
        return
    try:
        path.unlink()
    except OSError:
        pass


# ------------------------------------------------------------------ 采集


def _build_tagger(project_of, citizen_of, candidates_of):
    return tag_mod.Tagger(project_of=project_of, citizen_of=citizen_of,
                          candidates_of=candidates_of)


def ingest(hosts=DEFAULT_HOSTS, *, root=None, roots=None, limit=DEFAULT_LIMIT,
           dry_run: bool = False, project_of=None, citizen_of=None, candidates_of=None,
           conn=None, use_lock: bool = True, host=None, batch_frames=None,
           max_frame_bytes=None, config_of=None,
           skip_sessions=None, days=None) -> dict:
    """采集入口。返回计数器（`sessions_seen/segments_new/degraded/orphan/lag_sec/...`）。

    `dry_run=True` 时**不写库、不建库**：库不存在就在内存里跑一遍（只读真目录），
    所以"看看会采到什么"不会在生产目录里留下一个空库。

    `max_frame_bytes` / `config_of`（A①：帧上限可配）：`None` 时按
    `dsh_adapter.resolve_max_frame_bytes` 的三级取值（环境变量 > 配置键
    `corpus.max_frame_bytes` > 缺省 64 MiB）。`config_of(key)` 由调用方注入读主库配置
    （J2：语料层不许 import 主库）。

    `skip_sessions`（T05 · rebuild 用）：`{(host, session_id), ...}` 命中的文件**整份跳过**。
    为什么在采集侧跳过而不是"采完再删"：先复活再删意味着删除发生过两次，
    期间任何一次读取都能看到本该消失的原话——`forget` 的语义是"它不再存在"。

    `days`（2026-10-27 用户决策：只投影最近 3 天，更早留待后续回填）：只采**会话文件
    mtime ≥ 现在 − days×86400 秒**的文件，`None` 与 `0` 都是"不限"（0 = 显式全量）。
    过滤发生在 `discover()` 之后、任何解压/读字节之前——被窗口跳过的文件**不建
    `corpus_source` 行、不记 orphan**，只计入 `skipped_by_window`（"没采"与"库里没有"
    必须分得开）。函数缺省 `None` 是有意的：CLI 传 3，函数层保持全量，既有调用不变。
    """
    if host:                                  # CLI 的 `--host` 便捷写法
        hosts = (host,)
    if isinstance(hosts, str):
        hosts = (hosts,)
    hosts = tuple((h or "").strip().lower() for h in hosts)
    for h in hosts:
        adapter_for(h)                        # 提前响亮失败（未知宿主）
    skip = {(str(h), str(s)) for h, s in (skip_sessions or ())}
    # 窗口口径在**门面解析一次**：同一轮里两个宿主用同一个 cutoff（"现在"是这一轮
    # 开始的那一刻），报障时"这一轮到底采了几天"只有一个答案。
    window_days = _window_days(days)
    cutoff_ns = (None if not window_days
                 else time.time_ns() - window_days * SECONDS_PER_DAY * 10 ** 9)
    started = time.time()
    created_conn = False
    lock = None
    result = {
        "hosts": list(hosts), "dry_run": bool(dry_run), "busy": False,
        "sessions_seen": 0, "sessions_ok": 0, "sessions_skipped": 0, "sessions_new": 0,
        "segments_new": 0, "segments_updated": 0, "segments_skipped": 0,
        "unknown_events": 0, "unchanged": 0, "degraded": [], "orphan": [],
        "skipped_by_tombstone": 0, "prefix_bytes_read": 0,
        "by_host": {}, "limit": limit, "elapsed_sec": 0.0, "lag_sec": None,
        # 停滞判据要的第二个读数（复审 🟠-2："新段 + 新事件双 0"才算空转）。
        "events_seen": 0,
        # 非降级告警（目录名≠头部 id 等）：并入返回，不再写进没人读的死键（复审 🟡）。
        "warnings": [],
        # 段级台账（pointer/range）本轮的回收量（复审 🟠-1：这两种作用域必须真的生效）。
        "purged_by_segment_tombstone": 0,
        # 时间窗口（2026-10-27）：`window_days=0` = 不限；被窗口跳过的文件**不入库**，
        # 只在 `skipped_by_window` 计数（"没采"与"库里没有"必须分得开）。
        "window_days": window_days, "skipped_by_window": 0,
    }
    if root is not None:
        root = Path(root)
    if use_lock and root is not None and not dry_run:
        lock = acquire_lock(root)
        if lock is None:
            result["busy"] = True
            result["skipped"] = "locked"
            return result
    try:
        if conn is None:
            if root is None:
                raise CorpusError("ingest 需要 root 或 conn（语料库位置）")
            if dry_run:
                conn = _dry_run_conn(root)
            else:
                conn = store.open_db(root)
            created_conn = True
        tagger = _build_tagger(project_of, citizen_of, candidates_of)
        # 帧上限在**门面解析一次**（不是每个文件解析一次）：同一轮采集里所有会话用同一个数，
        # 报障时"这一轮的上限是多少"只有一个答案。
        frame_cap = dsh_adapter.resolve_max_frame_bytes(config_of, max_frame_bytes)
        # M1：**日常采集也按删除台账过滤**（不只是 rebuild）。不这么做的话 `forget` 只在
        # 那一天成立：第二天日采把原话采回来，而"删过"这件事只留在台账里没人看。
        tomb_sessions, tomb_hosts = store.tombstone_skip_sets(conn)
        skip |= tomb_sessions
        for h in hosts:
            adapter = adapter_for(h)
            if h in tomb_hosts:
                result["by_host"][h] = {
                    "host": h, "sessions_seen": 0, "sessions_ok": 0, "sessions_skipped": 0,
                    "sessions_new": 0, "segments_new": 0, "segments_updated": 0,
                    "segments_skipped": 0, "unknown_events": 0, "unchanged": 0,
                    "skipped_by_tombstone": 0, "prefix_bytes_read": 0,
                    "skipped_by_window": 0,
                    "events_seen": 0, "warnings": [],
                    "degraded": [], "orphan": [],
                    "roots": [str(r) for r in _resolve_roots(h, roots)],
                    "skipped_host_tombstone": True}
                continue
            host_result = _ingest_host(adapter, conn, _resolve_roots(h, roots), tagger,
                                       limit=limit, dry_run=dry_run,
                                       batch_frames=batch_frames, skip_sessions=skip,
                                       skip_hosts=tomb_hosts, max_frame_bytes=frame_cap,
                                       window_cutoff_ns=cutoff_ns)
            result["by_host"][h] = host_result
            for key in ("sessions_seen", "sessions_ok", "sessions_skipped",
                        "segments_new", "segments_updated", "segments_skipped",
                        "unknown_events", "unchanged", "skipped_by_tombstone",
                        "prefix_bytes_read", "events_seen", "skipped_by_window"):
                result[key] += host_result[key]
            result["degraded"].extend(host_result["degraded"])
            result["orphan"].extend(host_result["orphan"])
            result["warnings"].extend(host_result["warnings"])
            result["sessions_new"] += host_result["sessions_new"]
        result["lag_sec"] = _lag_sec(conn)
        if not dry_run:
            # 段级台账收口（M1 的同一个道理，作用域更细）：会话级跳过挡不住
            # `pointer`/`range`——它们只能落在段表上，所以采完再按台账回收一次。
            purged = store.purge_segment_tombstones(conn)
            result["purged_by_segment_tombstone"] = int(purged["deleted"])
            result["segment_tombstones"] = {"pointers": purged["pointers"],
                                            "ranges": purged["ranges"]}
            # 窗口读数落 meta（`stats` 要跨进程看得见"上一轮采了几天、更早的跳过几个"）。
            # 只存口径与计数，**不存文件清单**：被窗口跳过的文件不入库，这是它唯一痕迹。
            store.set_meta(conn, META_INGEST_WINDOW_DAYS, window_days)
            store.set_meta(conn, META_SKIPPED_BY_WINDOW, result["skipped_by_window"])
            _update_stall(conn, result)
    finally:
        if created_conn and conn is not None:
            conn.close()
        if lock is not None:
            release_lock(root, lock)
    result["elapsed_sec"] = round(time.time() - started, 3)
    return result


def _window_days(days) -> int:
    """把 `days` 归一成**读数口径**：`None` / `0` / 负数 = 0（不限），其余取整。

    为什么要归一：`window_days` 会出现在返回值和 `stats` 里，"到底限没限"必须是一个
    能直接比对的整数；`None` 与 `0` 在读数上看着不一样、语义上却一样，留着早晚出事。
    非整数直接响亮失败（而不是"当 0 处理"）——把 `--days abc` 静默当成全量采集，
    是这个设计里最贵的一种宽容。
    """
    if days is None:
        return 0
    try:
        value = int(days)
    except (TypeError, ValueError):
        raise CorpusError("days 必须是整数（0 或省略 = 不限）：%r" % (days,)) from None
    return value if value > 0 else 0


def _in_window(item, cutoff_ns) -> bool:
    """发现阶段的窗口判定：`mtime ≥ cutoff` 才采（`cutoff_ns is None` = 不限）。

    这里是时间窗口**唯一的红线**，所以它单独成函数、判据也钉在它上面（变异自证
    `mutation_corpus_3gate.W1/W2`：恒 True 会把 4 天前的会话也采回来，恒 False 会把
    2 天前的也丢掉）。读不到 mtime（`None`/0）按"很久以前"处理：宁可不采，
    也不要在不知道文件多新的情况下把它投影进来。
    """
    if cutoff_ns is None:
        return True
    return int(item.get("mtime_ns") or 0) >= int(cutoff_ns)


def _dry_run_conn(root):
    """演练用的连接：库在就用**只读**连接（能读到真游标），库不在就用内存库。"""
    import sqlite3
    path = store.db_path(root)
    if path.exists():
        return store.connect(path, readonly=True)
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    store.migrate(conn)
    return conn


def _ingest_host(adapter, conn, roots, tagger, *, limit, dry_run, batch_frames,
                 skip_sessions=frozenset(), skip_hosts=frozenset(),
                 max_frame_bytes=None, window_cutoff_ns=None) -> dict:
    """采一个宿主：发现 → 按 mtime 倒序 → 逐文件；再收口孤儿与降级。

    `window_cutoff_ns`（2026-10-27）：**发现阶段**的窗口过滤——`discover()` 之后、
    任何解压/读字节之前按 mtime 丢掉窗口外的文件。这一步之后它们就不存在了：
    不建 `corpus_source` 行、不参与 orphan 对账（从没进过管线），只加 `skipped_by_window`。
    """
    host = adapter.host
    counters = {
        "host": host, "sessions_seen": 0, "sessions_ok": 0, "sessions_skipped": 0,
        "sessions_new": 0, "segments_new": 0, "segments_updated": 0,
        "segments_skipped": 0, "unknown_events": 0, "unchanged": 0,
        "skipped_by_tombstone": 0, "prefix_bytes_read": 0, "events_seen": 0,
        "skipped_by_window": 0,
        "warnings": [],
        "degraded": [], "orphan": [], "roots": [str(r) for r in roots],
    }
    discovered = adapter.discover(roots)
    # 时间窗口：**发现阶段**就丢（还没解压一个字节）。`sessions_seen` 只说"进了管线几个"，
    # 窗口外的单独计数——否则"只采 3 天"和"源里就这么多"在回执上长得一模一样。
    if window_cutoff_ns is not None:
        kept = [item for item in discovered if _in_window(item, window_cutoff_ns)]
        counters["skipped_by_window"] = len(discovered) - len(kept)
        discovered = kept
    counters["sessions_seen"] = len(discovered)
    selected = discovered if limit in (None, 0) else discovered[:int(limit)]
    counters["sessions_skipped"] = len(discovered) - len(selected)
    extra = {}
    if batch_frames:
        extra["batch_frames"] = int(batch_frames)
    if max_frame_bytes and host == dsh_adapter.HOST:
        # 只有 DSH 是"一文件多 zstd 帧"（A①）；WB 是逐行文本，没有帧上限这回事，
        # 传过去会变成 TypeError，所以按宿主名分派而不是盲传。
        extra["max_frame_bytes"] = int(max_frame_bytes)
    for item in selected:
        path_id = str(item["session_id"])
        # R1：目录名与头部 id 不一致的映射已落盘时，游标/台账/跳过都按**真 id** 走
        # （否则每轮都会"发现不一致 → 重取 sid"一次）。
        sid = store.resolve_path_session(conn, host, path_id)
        if sid != path_id:
            extra["session_id_override"] = sid
        elif "session_id_override" in extra:
            extra.pop("session_id_override")
        # 台账判据走 store 那一份（评审 🟠-1）：`*:<sid>`（CLI 不带 host 的形态）也要命中，
        # 否则源一重写就把被删的原话采回来。目录名与真 id 两个键都查。
        if (store.is_session_forgotten(conn, host, path_id, skip_sessions, skip_hosts)
                or store.is_session_forgotten(conn, host, sid, skip_sessions, skip_hosts)):
            counters["skipped_by_tombstone"] += 1
            continue                          # 删除台账命中：整份不采（T05 / M1）
        cursor = store.read_cursor(conn, host, sid)
        if cursor is None:
            counters["sessions_new"] += 1
        file_result = adapter.ingest_file(conn, item["path"], cursor, tagger=tagger,
                                          dry_run=dry_run, **extra)
        for key in ("segments_new", "segments_updated", "segments_skipped",
                    "unknown_events", "prefix_bytes_read", "events_seen"):
            counters[key] += file_result.get(key, 0)
        # 非降级告警并入返回（复审 🟡：旧实现写进 `counters["warning"]`，没人读）。
        for name in file_result.get("warnings") or ():
            counters["warnings"].append({
                "host": host, "path": item["path"],
                "session_id": file_result.get("session_id") or sid, "warning": name})
        if file_result.get("unchanged"):
            counters["unchanged"] += 1
        if file_result.get("status") == store.SOURCE_DEGRADED:
            counters["degraded"].append({
                "host": host, "path": item["path"],
                "session_id": file_result.get("session_id") or sid,
                "reasons": [d["reason_code"] for d in file_result.get("degraded", [])],
            })
        else:
            counters["sessions_ok"] += 1
    if not dry_run:
        counters["orphan"] = _mark_orphans(conn, host)
        conn.commit()
    return counters


def _mark_orphans(conn, host) -> list:
    """游标指向的文件已经不存在 → 标 `orphan`（C-P0-4 红②：别装作还采得到）。"""
    orphans = []
    for row in store.list_sources(conn, host):
        path = row.get("path")
        if path and Path(path).exists():
            continue
        if row.get("status") != store.SOURCE_ORPHAN:
            store.set_source_status(conn, host, row["session_id"], store.SOURCE_ORPHAN,
                                    "源文件已不在")
        orphans.append({"host": host, "session_id": row["session_id"], "path": path})
    return orphans


def _lag_sec(conn):
    """`now - max(last_ingested_at)`：日报用它抓"假增量"（>26h 就是源不长了）。"""
    row = conn.execute("SELECT MAX(last_ingested_at) FROM corpus_source").fetchone()
    if not row or not row[0]:
        return None
    try:
        last = time.mktime(time.strptime(row[0], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except (TypeError, ValueError):
        return None
    return int(max(0, time.time() - last))


def _source_signature(conn) -> str:
    """源**事实**指纹：游标位置 / 事件数 / 段数（**不含 mtime**），用于停滞哨兵。

    为什么不含 mtime（复审 🟠-2）：纯 `touch` 一个字节都不多读，把它算进指纹就等于
    把"我碰了它一下"当成"源在变"，连续三次触碰就误报 `stalled`。指纹只由"实际消费/
    实际产出"决定，源真长出新字节时 `bytes_total` 会变，那才是"源在动"。
    """
    rows = conn.execute(
        "SELECT host, session_id, bytes_total, events_seen, segments_kept"
        " FROM corpus_source ORDER BY host, session_id").fetchall()
    blob = "|".join("%s:%s:%s:%s:%s" % tuple(row) for row in rows)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _update_stall(conn, result) -> dict:
    """C-P0-9 红②：连续 `STALL_STREAK_ALARM` 轮"新段与新事件**双 0**但源在变" → `stalled`。

    为什么这条哨兵必要：采集变成"空转"时，`segments_new=0` 与"源没长"长得一模一样；
    只有把"源确实变了"和"我一个段都没采到"放在一起看，才分得出假增量。

    2026-10-27 复审 🟠-2 两条修正：
      ① 判据改成"新增段数 + 新增事件数**双 0**"——只看段数会把"事件采到了但都不成段"
         （例如整条消息只有 reasoning/tool-call）也报成停滞，那是另一种故障；
      ② 源指纹去掉了 mtime，且**源没动就归零**——纯 `touch` 不是"源在变"，而旧逻辑
         会把上一次累积的 streak 一直留着，等下一次触碰凑够 3 次就报警。
    读数落在 `corpus_meta`（跨进程/跨轮次都要看得见，所以不能只放在返回值里）。
    """
    sig = _source_signature(conn)
    prev = store.get_meta(conn, META_STALL_SIG)
    try:
        streak = int(store.get_meta(conn, META_STALL_STREAK, "0") or 0)
    except (TypeError, ValueError):
        streak = 0
    new_segments = int(result.get("segments_new") or 0)
    new_events = int(result.get("events_seen") or 0)
    source_moved = bool(prev) and prev != sig
    if new_segments == 0 and new_events == 0 and source_moved:
        streak += 1
    else:                                  # 有产出，或源根本没动 → 归零（不留旧账）
        streak = 0
    alarm = "stalled" if streak >= STALL_STREAK_ALARM else None
    store.set_meta(conn, META_STALL_SIG, sig)
    store.set_meta(conn, META_STALL_STREAK, streak)
    store.set_meta(conn, META_STALL_ALARM, alarm or "")
    conn.commit()
    result["stall"] = {"zero_streak": streak, "alarm": alarm, "source_moved": source_moved,
                       "events_seen": new_events}
    result["alarm"] = alarm
    return result["stall"]


# ------------------------------------------------------------------ 统计


def _meta_int(conn, key: str) -> int:
    """读一个整数型 meta 读数（缺失/脏值都算 0：统计是只读动作，不许因旧库报错）。"""
    try:
        return int(store.get_meta(conn, key, "0") or 0)
    except (TypeError, ValueError):
        return 0


def stats(*, root=None, host=None, conn=None) -> dict:
    """采集读数（C-P0-9）：待采/已采/跳过/degraded/orphan 与文件数对账。

    **库不存在时返回空读数、不建库**：体检/统计是只读动作，为了看一眼就落一个空库
    文件，会让"到底采过没有"变得看不出来（而 `db_bytes.total == 0` 也不会说话）。

    2026-10-27（M1）：**已 forget 的会话不计入** `sessions`/`by_project`，也不计入
    `by_host[host]["sessions"]`——"删了还在计数"就是删了个寂寞。

    2026-10-27（复审 🟠-1）：`pointer`/`range` 两种作用域也要同口径——段数按**段表现存
    实况**算（并且扣掉落单的段级台账命中），不再信 `corpus_session.segments` 这个可能
    过期的派生计数；`unmapped_cwds` 出口再脱敏一次（`cwd` 里带 token 时不回显真值）。

    2026-10-27（终批）：① 已 forget 的判定收敛到 `store.is_session_forgotten`（跨宿主
    `*:<sid>` 也算）；② `degraded`/`orphan` 的 path 出口过 `_safe_field`——旧库里
    带 token 的路径不该从统计口回显成真值。

    2026-10-27（时间窗口）：`window_days` / `skipped_by_window` 是**上一轮采集**的口径与
    计数（存在 `corpus_meta`，跨进程可见）。为什么放在 stats 而不是只放在 ingest 回执：
    回执是一次性的，而"这一轮为什么少采了"往往在第二天才被问到。
    """
    created = False
    if conn is None:
        if root is None:
            raise CorpusError("stats 需要 root 或 conn")
        if not store.db_path(root).exists():
            return {"as_of": store.now_utc(), "scope": {"host": host} if host else {},
                    "segments": 0, "sessions": 0, "by_host": {}, "by_project": {},
                    "by_status": {}, "degraded": [], "orphan": [], "unmapped_cwds": [],
                    "lag_sec": None, "db_bytes": {}, "db_missing": True,
                    "forgotten_sessions": 0, "alarm": None,
                    "window_days": 0, "skipped_by_window": 0,
                    "tombstones": {"session": 0, "host": 0, "pointer": 0, "range": 0},
                    "stall": {"zero_streak": 0, "alarm": None}}
        conn = store.open_db(root, readonly=True)
        created = True
    try:
        forgotten, forgotten_hosts = store.forgotten_sessions(conn)
        scopes = store.tombstone_scopes(conn)
        where, args = store.segment_tombstone_where(scopes)
        live_sql = "SELECT host, session_id, COUNT(*) AS n FROM corpus_segment"
        if where:
            live_sql += " WHERE NOT " + where
        live_sql += " GROUP BY host, session_id"
        live_counts = {(row["host"], row["session_id"]): int(row["n"])
                       for row in conn.execute(live_sql, args).fetchall()}
        by_host = {}
        for row in conn.execute(
                "SELECT host, COUNT(*) AS files, COALESCE(SUM(segments_kept),0) AS segs"
                " FROM corpus_source GROUP BY host").fetchall():
            by_host[row["host"]] = {"sources": int(row["files"]), "segments_kept": int(row["segs"]),
                                    "sessions": 0, "segments": 0}
        kept_sessions = 0
        forgotten_seen = 0
        by_project = {}
        for row in conn.execute(
                "SELECT host, session_id, project, segments FROM corpus_session"
                " ORDER BY host, session_id").fetchall():
            key = (row["host"], row["session_id"])
            if store.is_session_forgotten(conn, key[0], key[1], forgotten, forgotten_hosts):
                forgotten_seen += 1
                continue
            kept_sessions += 1
            segs = live_counts.get(key, 0)      # 实况段数（不是派生计数）
            slot = by_host.setdefault(row["host"], {"sources": 0, "segments_kept": 0,
                                                    "sessions": 0, "segments": 0})
            slot["sessions"] += 1
            slot["segments"] += segs
            by_project.setdefault(row["host"], []).append(
                {"project": row["project"], "sessions": 1, "segments": segs})
        merged = {}
        for h, items in by_project.items():
            buckets = {}
            for item in items:
                slot = buckets.setdefault(item["project"], {"project": item["project"],
                                                           "sessions": 0, "segments": 0})
                slot["sessions"] += item["sessions"]
                slot["segments"] += item["segments"]
            merged[h] = [buckets[key] for key in sorted(buckets)]
        total_segments = sum(live_counts.values())
        by_status = {}
        for row in conn.execute(
                "SELECT host, status, COUNT(*) AS n FROM corpus_source"
                " GROUP BY host, status").fetchall():
            by_status.setdefault(row["host"], {})[row["status"]] = int(row["n"])
        degraded = [dict(row, path=_safe_field(row.get("path")))
                    for row in store.list_degraded(conn, host)]
        orphan = [{"host": r["host"], "session_id": r["session_id"],
                   "path": _safe_field(r["path"])}
                  for r in store.list_sources(conn, host)
                  if r.get("status") == store.SOURCE_ORPHAN]
        unmapped = [{"workspace": _safe_field(row["workspace"]), "sessions": int(row["n"])}
                    for row in conn.execute(
                        "SELECT workspace, COUNT(*) AS n FROM corpus_session"
                        " WHERE project='' AND workspace<>'' GROUP BY workspace"
                        " ORDER BY n DESC LIMIT 10").fetchall()]
        try:
            streak = int(store.get_meta(conn, META_STALL_STREAK, "0") or 0)
        except (TypeError, ValueError):
            streak = 0
        alarm = store.get_meta(conn, META_STALL_ALARM) or None
        return {
            "as_of": store.now_utc(),
            "scope": {"host": host} if host else {},
            "segments": total_segments,
            "sessions": kept_sessions,
            "by_host": by_host,
            "by_project": merged,
            "by_status": by_status,
            "degraded": degraded,
            "orphan": orphan,
            "unmapped_cwds": unmapped,
            # 时间窗口（2026-10-27）：上一轮采集的时间口径，以及"更早的跳过了几个"。
            # `window_days=0` = 不限；两个读数都来自 `corpus_meta`（跨进程可见）。
            "window_days": _meta_int(conn, META_INGEST_WINDOW_DAYS),
            "skipped_by_window": _meta_int(conn, META_SKIPPED_BY_WINDOW),
            "lag_sec": _lag_sec(conn),
            "db_bytes": _db_bytes(conn),
            "forgotten_sessions": forgotten_seen,
            # 四档作用域各有多少条（复审 🟠-1）：读数上看得到"到底删过哪些范围"。
            "tombstones": {"session": len(scopes["sessions"]), "host": len(scopes["hosts"]),
                           "pointer": len(scopes["pointers"]),
                           "range": len(scopes["ranges"])},
            "alarm": alarm,
            "stall": {"zero_streak": streak, "alarm": alarm},
        }
    finally:
        if created:
            conn.close()


def _db_bytes(conn) -> dict:
    """库体积分项（K10 ≤1 GB 的观察口）。失败返回空 dict（不因统计影响主流程）。"""
    try:
        page = conn.execute("PRAGMA page_size").fetchone()[0]
        count = conn.execute("PRAGMA page_count").fetchone()[0]
        return {"total": int(page) * int(count), "segments": _table_bytes(conn, "corpus_segment"),
                "fts": _table_bytes(conn, "corpus_fts")}
    except Exception:                          # noqa: BLE001 —— 读体积失败不是业务失败
        return {}


def _table_bytes(conn, table: str) -> int:
    try:
        row = conn.execute("SELECT SUM(pgsize) FROM dbstat WHERE name LIKE ?",
                           (table + "%",)).fetchone()
        return int(row[0] or 0)
    except Exception:                          # noqa: BLE001 —— dbstat 需要编译选项
        return 0


# ------------------------------------------------------------------ 重算


def retag(*, root=None, host=None, session_id=None, all_: bool = True,
          dry_run: bool = False, project_of=None, citizen_of=None, candidates_of=None,
          conn=None, allow_downgrade: bool = False) -> dict:
    """按库里已有 cwd 重算会话标签（R5）。**不重采、不改段数**，幂等。

    `allow_downgrade=False`（默认）：重算不出映射时**保留写时的归属**并把这件事记进
    `warnings`——库里的 cwd 已脱敏，拿它重算会把 mapped 悄悄降成 unmapped（安全 🟡）。

    `citizen_of`（终验黄项）：身份列与项目列同一个道理——不给解析器时 `Tagger` 只能
    得出 `(None, None)`，旧实现把它写进 UPDATE，等于**把公民身份抹成 NULL**。这里把
    解析器透传下去，CLI 侧由 `fish.py` 注入 `fish_activity.resolve_citizen`；真的没注入
    时由 `Tagger.retag` 保留原列（不是猜，也不是清空）。
    """
    if project_of is None:
        raise tag_mod.TagError(
            "retag 需要 project_of（否则会把已映射的会话改回 unmapped）")
    created = False
    if conn is None:
        if root is None:
            raise CorpusError("retag 需要 root 或 conn")
        conn = store.open_db(root)
        created = True
    try:
        tagger = tag_mod.Tagger(project_of=project_of, citizen_of=citizen_of,
                                candidates_of=candidates_of)
        return tagger.retag(conn, host=host, session_id=session_id, all_=all_,
                            dry_run=dry_run, allow_downgrade=allow_downgrade)
    finally:
        if created:
            conn.close()


# ------------------------------------------------------------------ 检索（T03）


def _connect_read(root, conn):
    """读动作的连接：库不存在返回 `(None, False)`——**读不建库**，也不留空库。

    2026-10-27（M3）：这里是**只读连接**（`mode=ro`）。旧实现用写连接，于是每次
    `search`/`read` 都会跑 `migrate()` 的 DDL 并抢写锁——采集中（`busy_timeout` 15s）
    能把用户消息边界上的 prompt-push 直接卡死。只读连接还顺带保证了"读动作不改库"。
    """
    if conn is not None:
        return conn, False
    if root is None:
        raise CorpusError("需要 root 或 conn（语料库位置）")
    if not store.db_path(root).exists():
        return None, False
    return store.open_db(root, readonly=True), True


def _connect_write(root, conn):
    """写动作的连接（会建库/建表）：`forget`/`rebuild`/`doctor` 的审计要写日志。"""
    if conn is not None:
        return conn, False
    if root is None:
        raise CorpusError("需要 root 或 conn（语料库位置）")
    return store.open_db(root), True


def _dedupe(items) -> list:
    seen, out = set(), []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _resolve_terms(q, terms) -> list:
    """查询 token：`terms is None` → 层内切（`index_tokens`）；给了就**只用给的**。

    "给了仍回退层内切"会让调用方对自己的召回失去控制（明明只想查 A，结果把 B
    也带进来），所以这里没有"保险起见再切一次"。
    """
    if terms is not None:
        return _dedupe(str(t).strip().lower() for t in terms)
    return _dedupe(fish_common.index_tokens(q or ""))


def _clamp_limit(limit) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        raise CorpusError("limit 必须是整数，收到 %r" % (limit,))
    return max(1, min(value, SEARCH_MAX_LIMIT))


def _snippet(cand, chars=SNIPPET_CHARS) -> tuple:
    """摘录（**出口二次脱敏**：入库漏了、出口补上，不许把 `redacted_count` 算成 0）。"""
    text, count, _hits = redact_mod.redact(cand.get("text") or "")
    clip = text[:chars] + ("…" if len(text) > chars else "")
    return clip, max(int(cand.get("redacted_count") or 0), count)


def _safe_field(value) -> str:
    """**非正文**可回显列的出口二次脱敏（`workspace`/`cwd` 族）。

    为什么出口还要再来一次（安全新🟠）：写侧修好只覆盖"以后采的"——旧库里已经存在
    的脏 cwd 仍会被 `search`/`read`/`stats` 原样回显。正文那道（`_snippet`）早就有，
    这里把同一层补到路径列上，规则仍是唯一那份 `RULES`。
    """
    if not value:
        return ""
    text, _count, _hits = redact_mod.redact(str(value))
    return text


def _hit(cand, pointer, snippet, score, why, redacted_count, ranker_name) -> dict:
    """`hits[]` 的一行——**字段名与个数按 PRD §8.1 冻结**，不许增删。"""
    return {
        "pointer": pointer, "host": cand["host"], "session_id": cand["session_id"],
        # 标题与 `workspace` 一样是**可回显文本**（安全 🟠）：写侧那道只覆盖"以后采的"，
        # 旧库里已有的脏标题在这里必须再脱一次——出口是 AI 真正看到的地方。
        "session_title": _safe_field(cand.get("session_title")), "role": cand["role"],
        "occurred_at": cand["occurred_at"], "project": cand.get("project") or "",
        "workspace": _safe_field(cand.get("workspace")), "citizen_id": cand.get("citizen_id"),
        "origin": cand.get("origin"), "shadowed": int(cand.get("shadowed") or 0),
        "snippet": snippet, "chars": int(cand.get("chars") or 0),
        "score": round(float(score), 6), "ranker": ranker_name, "why": list(why or ()),
        "_redacted_count": int(redacted_count),
    }


def _strip_private(hit: dict) -> dict:
    out = dict(hit)
    out.pop("_redacted_count", None)
    return out


def _log_access(conn, *, root=None, action, via, query=None, result_count=None,
                pointers=None, citizen_id=None, session_key=None) -> None:
    """写审计（失败不影响主流程：审计是度量，不是功能）。

    读路径是**只读连接**（M3），只读连接上写必然失败——所以在失败之后另开一个
    **短超时**写连接补记一条：`busy_timeout=250ms`，被采集占着写锁就放弃这一次审计，
    绝不让"记一笔审计"把用户消息边界上的 prompt-push 拖住 15 秒。
    """
    payload = {"action": action, "via": via, "citizen_id": citizen_id,
               "session_key": session_key, "query": query,
               "result_count": result_count, "pointers": pointers}
    try:
        store.write_access_log(conn, **payload)
        return
    except Exception:                          # noqa: BLE001 —— 只读连接上写必然失败
        pass
    if root is None:
        return
    wconn = None
    try:
        wconn = store.connect(store.db_path(root), busy_timeout_ms=250)
        store.write_access_log(wconn, **payload)
        wconn.commit()
    except Exception:                          # noqa: BLE001 —— 见 docstring
        pass
    finally:
        if wconn is not None:
            try:
                wconn.close()
            except sqlite3.Error:
                pass


def search(q, terms=None, filters=None, limit=SEARCH_DEFAULT_LIMIT, budget_tokens=None,
           ranker="default", *, root=None, conn=None, citizen_id=None, session_key=None,
           via="api", record=True, time_from=None, time_to=None,
           include_shadowed=False, count=True) -> dict:
    """检索原话（设计 §6）：一次 SQL 把 `MATCH` 与全部过滤下推，排序交给 Ranker。

    返回 `{"hits": [...], "meta": {total_matched, returned, ranker, filters_echo,
    degraded, as_of, truncated, limit, budget_tokens}}`。

    `count=False`（T03a）：**不统计全库命中总数**，`meta.total_matched` 回 `None`。
    为什么要有这个口子：推送候选只要 top-K，"总共命中多少"是给人看的读数、不是
    候选的输入；而在 5 万段库上这一次 `COUNT` 约 26ms——候选生成发生在用户消息
    边界上，26ms 是白付的。为什么回 `None` 而不是 0：0 会被读成"一条都没命中"。
    """
    rank_impl = rank_mod.get_ranker(ranker)    # 未知 ranker → ValueError（不静默回落）
    filters = dict(filters or {})
    # 三个"形态特殊"的键允许同时从 filters 里给（MCP/脚本两种写法都通）
    include_shadowed = bool(filters.pop("include_shadowed", include_shadowed))
    time_from = filters.pop("time_from", time_from)
    time_to = filters.pop("time_to", time_to)
    limit_val = _clamp_limit(limit)
    now = rank_mod.utc_now()
    degraded = []
    hits = []
    total = 0 if count else None
    truncated = False
    conn_obj, created = _connect_read(root, conn)
    try:
        if conn_obj is None:
            degraded.append({"reason": "db_missing",
                             "note": "语料库还没建：先跑 `fish corpus ingest`"})
        else:
            tokens = _resolve_terms(q, terms)
            expr = rank_mod.match_expr(tokens)
            if expr is not None:
                pool = max(limit_val * SEARCH_CANDIDATE_FACTOR, SEARCH_CANDIDATE_MIN)
                cands = store.search_segments(
                    conn_obj, expr, filters=filters,
                    include_shadowed=include_shadowed, time_from=time_from,
                    time_to=time_to, limit=pool)
                total = store.count_matching(
                    conn_obj, expr, filters=filters, include_shadowed=include_shadowed,
                    time_from=time_from, time_to=time_to) if count else None
                for cand in cands:
                    cand["pointer"] = store.canonical_pointer(
                        cand["host"], cand["session_id"], cand["loc"], cand["part"])
                project = (filters.get("project") or [""])[0] \
                    if isinstance(filters.get("project"), (list, tuple)) else ""
                ranked = rank_impl.rank(
                    cands, q, {"terms": tokens, "project": project, "now": now})
                by_pointer = {cand["pointer"]: cand for cand in cands}
                used = 0
                for pointer, score, why in ranked:
                    cand = by_pointer.get(pointer)
                    if cand is None:
                        continue
                    snippet, redacted = _snippet(cand)
                    cost = len(pointer) + len(snippet)
                    if budget_tokens and hits and used + cost > int(budget_tokens):
                        truncated = True
                        break
                    hits.append(_hit(cand, pointer, snippet, score, why, redacted,
                                     rank_impl.name))
                    used += cost
                    if len(hits) >= limit_val:
                        break
                # `total is None` = 本次没统计（`count=False`）：不能用它判"被截断"
                truncated = (truncated or (total is not None and total > len(cands))
                             or len(hits) < len(ranked))
    finally:
        if conn_obj is not None and record:
            _log_access(conn_obj, root=root, action="search", via=via, query=q,
                        result_count=len(hits),
                        pointers=",".join(hit["pointer"] for hit in hits),
                        citizen_id=citizen_id, session_key=session_key)
        if created:
            conn_obj.close()
    return {"hits": [_strip_private(hit) for hit in hits],
            "meta": {"total_matched": total, "returned": len(hits),
                     "ranker": rank_impl.name, "filters_echo": filters,
                     "degraded": degraded, "as_of": store.now_utc(),
                     "truncated": bool(truncated), "limit": limit_val,
                     "budget_tokens": budget_tokens}}


def _source_state(conn, host, session_id) -> dict:
    """源文件现在还在不在（`source.missing`）——**只影响声明，不影响正文**。

    两件事必须分开（安全 🟠）：
      · `missing` 用**库里的原始路径**判（`Path(path).exists()`）——出参里打码的话，
        一个带 token 的真实路径就永远判成"缺失"，声明开始撒谎；
      · 回给调用方的 `path` 过 `_safe_field`——它是出口，旧库/新数据都可能带 token。
    所以：**判据吃原值，出口只给脱敏值**。写侧一律不打码路径（`_mark_orphans` 也靠它）。
    """
    row = store.read_cursor(conn, host, session_id)
    if row is None:
        return {"path": None, "missing": True, "status": None,
                "note": "没有采集游标：源文件路径未知（正文来自投影）"}
    path = row.get("path") or ""
    exists = bool(path) and Path(path).exists()
    return {"path": _safe_field(path) or None, "missing": not exists,
            "status": row.get("status"),
            "note": "" if exists else "源文件已不在：正文来自投影（脱敏后的切片），不猜"}


def read(pointer, max_chars=READ_DEFAULT_MAX_CHARS, *, root=None, conn=None,
         citizen_id=None, session_key=None, via="api", record=True) -> dict:
    """门牌 → 原文（`conv:` 缩写先按 `sid8` 补全成 `log:`，多命中就报歧义）。

    源文件缺失时**仍返回已存正文**并声明 `source.missing=true`（设计 §8.3 约束 2）：
    投影存在的意义就是"源没了，那段话还在"；用摘要顶替或直接报错都会让人以为没存过。
    """
    conn_obj, created = _connect_read(root, conn)
    if conn_obj is None:
        raise CorpusError("语料库不存在：先跑 `fish corpus ingest`（%s）"
                          % (store.db_path(root) if root is not None else "?"))
    try:
        resolved = store.resolve_pointer(conn_obj, pointer)
        seg = store.get_segment(conn_obj, resolved["host"], resolved["session_id"],
                                resolved["loc"], resolved["part"])
        if seg is None:
            raise store.PointerError(
                "not_found", "门牌 %s 在库里没有对应段（可能已 forget）"
                % resolved["pointer"])
        text, count, _hits = redact_mod.redact(seg.get("text") or "")
        limit = max(1, int(max_chars))
        truncated = len(text) > limit
        if truncated:
            text = text[:limit]
        source = _source_state(conn_obj, resolved["host"], resolved["session_id"])
        result = {
            "pointer": resolved["pointer"], "code": resolved["code"],
            "host": resolved["host"], "session_id": resolved["session_id"],
            "sid8": store.sid8(resolved["session_id"]),
            "loc": resolved["loc"], "part": resolved["part"], "role": seg.get("role"),
            "occurred_at": seg.get("occurred_at"),
            # 出口再脱敏一次（安全 🟠）：标题本身就是内容（"XX凭证那次对话"），
            # 写侧脱敏只对**以后采的**生效，旧库里的脏标题必须在这里拦住。
            "session_title": _safe_field(seg.get("session_title")),
            "project": seg.get("project") or "",
            "workspace": _safe_field(seg.get("workspace")),
            "citizen_id": seg.get("citizen_id"),
            "origin": seg.get("origin"), "shadowed": int(seg.get("shadowed") or 0),
            "chars": int(seg.get("chars") or 0),
            "redacted_count": max(int(seg.get("redacted_count") or 0), count),
            "text": text, "truncated": truncated,
            "truncated_in_store": int(seg.get("chars") or 0) > len(seg.get("text") or ""),
            "offset_space": seg.get("offset_space"),
            "source": source, "text_from": "projection",
            "as_of": store.now_utc(),
        }
        if record:
            _log_access(conn_obj, root=root, action="read", via=via, result_count=1,
                        pointers=resolved["code"], citizen_id=citizen_id,
                        session_key=session_key)
        return result
    finally:
        if created:
            conn_obj.close()


# ------------------------------------------------------------------ 推送候选（T04）


def _clip(text, chars) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= chars else value[:chars] + "…"


def _push_title(hit) -> str:
    title = (hit.get("session_title") or "").strip()
    if not title:
        title = (hit.get("snippet") or "").strip()
    return _clip(title, PUSH_TITLE_MAX_CHARS)


def _push_reason(hit) -> str:
    day = str(hit.get("occurred_at") or "")[:10]
    who = "助手" if hit.get("role") == store.ROLE_ASSISTANT else "你"
    return _clip("原话里%s说过（%s）" % (who, day), PUSH_REASON_MAX_CHARS)


def _dup_count_map(dup_counts, now, skip):
    """`dup_counts(hours, now)` → `{code: 全局次数}`；**取不到就记 `dup_unknown`**。

    ## 语义：**全局重复度**（首轮 🟠 S3 的收口）

    回调回答的是"这条内容还新吗"——同一段原话在 24h 内被**任意会话/公民**推送过
    几次。它**不是**本会话的 exclude 集合：本会话要不要再看同一条，由 `recent_of`
    负责（挡，不产）；这里问的是"它在池子里已经出现过很多次没有"（罚）。两件事分开，
    否则被 `exclude` 掉的 code 恰好就是会被数到的 code → 活下来的候选 count 恒 0
    （首轮的结构性恒 0 就是这样来的）。

    与 `fish.py._silence_weight` 的分工（两者都会进 `_cand` 的分数，所以必须说清）：

      · 本回调 → **重复度 / 新颖性**：24h 内被推过几次（跨会话）；
      · `_silence_weight` → **历史忽略率**：推过而没人理的比例（最多降到 0.15）。

    这是两个不同现象，不是同一现象罚两次：重复且每次都被采纳（用户在用）→ dup 高、
    silence = 1.0；推过一次就没人理 → dup = 0.2、silence 低。同时进两个读数的只有
    "又重复又被忽略"的那一小撮，方向本就不同（前者压"再推一次的新增价值"，后者压
    "这条内容的可信度"），且各有上限/下限，谁都不能单独把一条压死。
    若调用方只想量"**别的会话**推过没有"（把本会话的去重完全留给 `recent_of`），
    在回调里排除本会话即可（`exclude_session=session_key`）——这条口径由
    `test_c1_dup_penalty_counts_other_sessions_only` 判据覆盖。

    ## 缺省与失败

    缺省（`None`）**也记账**：按 0.0 继续出候选，但计一次 `dup_unknown`。为什么不静默
    按 0：`dup_penalty` 是六特征里唯一的负向项，缺数会让重复推送变多，而这种"变多"
    在推送日志里看不出来（每条都长得一样）。也不让整批失败：候选生成发生在用户消息
    边界上，一个**可选特征**读不到不该拖垮这条链路（与 `_kb_push_candidates` 的兜底
    同一条纪律）。`dup_unknown` 的值是"**几次取数没拿到**"，不是条数。

    两种回包都接受：`{code: count}`（新写的调用方）与 `{code, ...}` 集合（既有
    `recent_of` 的形态就是集合，按"推过 1 次"算）——取数层不该为了形状再包一层。
    """
    if dup_counts is None:
        skip("dup_unknown")                    # 缺省 = 没有读数（可选特征，不编造 0）
        return None
    try:
        raw = dup_counts(PUSH_DUP_WINDOW_HOURS, now)
    except Exception:                          # noqa: BLE001 —— 见 docstring
        skip("dup_unknown")
        return None
    if raw is None:
        skip("dup_unknown")
        return None
    if isinstance(raw, (str, bytes)):
        # 字符串会被 `for code in raw` 拆成单字，最后得到一份"每个字推过一次"的假读数
        # ——比没有读数更坏（它看起来有值）。
        skip("dup_unknown")
        return None
    try:
        if hasattr(raw, "items"):
            return {str(code): int(count) for code, count in raw.items()}
        return {str(code): 1 for code in raw}
    except (TypeError, ValueError):
        # 回包形态不对（比如给了字符串）——当作没有读数，但要让调用方看得见。
        skip("dup_unknown")
        return None


def conv_push_candidates(*, query, project=None, root=None, conn=None, exclude=(), limit=PUSH_MAX_ITEMS, now=None,
                         min_relevance=PUSH_MIN_RELEVANCE, min_chars=PUSH_MIN_CHARS,
                         include_subagent=PUSH_INCLUDE_SUBAGENT, via="push",
                         scorer=None, dup_counts=None, citizen_id=None) -> dict:
    """语料命中 → 推送候选（C-P0-6 · 设计 §7）。

    **只产条目，不在这里发明打分**：给 `scorer`（`fish.py` 传它的 `_cand`）时，
    打分公式仍是唯一那一份；本模块只负责门槛与"为什么没推"的账。
    `relevance` 就是检索分数（公式权重和为 1，天然落在 [0,1]），`_cand` 拿它当
    `relevance` 且 `freshness=None`——避免同一件事被折扣两次（设计 §6.5）。

    返回 `{"items": [...], "skipped": {原因: 条数}, "ranker": 名字}`。
    `skipped` 不是装饰：**不推也要能回答"为什么不推"**，否则门槛松动时无人察觉。
    其中 `dup_unknown` 是"**取数没拿到**"的次数（缺省/回调失败各计一次），不是条数。

    ## 候选结构（T03a R2：与合流侧对齐，口径见 52 号 §4）

    每个 item 除既有字段外带五件东西：

      · `kind_class="corpus"` —— **语义层**类别。`kind` 仍是 `conv_related`
        （`test_corpus_push` 锁死，改它会动既有判据），合流侧认 `kind_class`；
        归一入口是 `fish_corpus_rank.semantic_kind`（三类映射只写一份）。
      · `score` + `score_stage="first_phase"` —— **首阶段分**：有 `scorer` 时它就是
        `fish.py._cand` 的 `(benefit − cost) × weight`；没有 `scorer` 时是检索分
        （占位但不编造）。它**不是** `score_breakdown` 四项的加权和——两者是两个
        阶段的数（34 §3.5 的 R3 只在重排之后成立），**禁止互相验算**。
      · `rerank=None` —— 重排器名字的占位。T03a 没有第二阶段，`None` 就是"还没重排"
        （不写 `""`：空串会被读成"重排器叫空名"）。T03b 重排后必须**原子替换**
        `score`/`score_breakdown` 并把 `score_stage` 置 `SCORE_STAGE_RERANK`。
      · `freshness` —— **与 `_cand` 完全同键**的 dict `{factor, band, age_days}`。
        为什么不是 float（首轮 🟠 S4）：合流池里 mem/kb 候选本来就是 dict，同名字段
        两种类型会让下游一半 `TypeError`、一半静默算错，而且只在两类同池时才现形。
      · `score_breakdown` —— 四项（`relevance` / `freshness` / `project_match` /
        `dup_penalty`）；`freshness` 在这一格里仍是**数值 factor**（明细要能复算），
        与上面那个 dict 出自**同一次**计算。`access_ratio` / `link_strength` 要摸库，
        留给 T03b 补。

    给了 `scorer` 时这些字段用 `setdefault` 补齐：**`score` 一个字都不改**（打分公式
    只有 `fish.py._cand` 那一份，这里补的是"解释"，不是"分数"）。
    `dup_counts` 是可选回调（`hours, now -> {code: 全局次数}`，见 `_dup_count_map`）：
    本模块**不查主库**（J1：语料层不认识 `push_log`），缺省即 `dup_penalty=0.0` 并
    计 `dup_unknown`。
    """
    skipped = {}

    def _skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1

    if not str(query or "").strip():
        _skip("no_query")
        return {"items": [], "skipped": skipped, "ranker": None}
    if not str(project or "").strip():
        _skip("no_project")
        return {"items": [], "skipped": skipped, "ranker": None}
    banned = {str(code) for code in (exclude or ())}
    dup_count_map = _dup_count_map(dup_counts, now, _skip)
    res = search(query, filters={"project": [project], "role": [store.ROLE_ASSISTANT]},
                 limit=PUSH_SEARCH_LIMIT, root=root, conn=conn, via=via, record=False,
                 # 只要 top-K：不统计全库总数（5 万段库上那一次 COUNT ≈ 26ms，白付）
                 count=False)
    items, seen = [], set()
    for hit in res["hits"]:
        parsed = store.parse_pointer(hit["pointer"])
        code = store.push_pointer(hit["host"], hit["session_id"], parsed["loc"],
                                  parsed["part"])
        if code in banned or code in seen:
            _skip("excluded")
            continue
        if int(hit.get("shadowed") or 0):
            _skip("shadowed")
            continue
        if not hit.get("citizen_id"):
            _skip("no_citizen")               # 身份查不到就不推（不静默：计入 skipped）
            continue
        # 外部审查 🔴8（2026-10-29）：**跨公民推送**——旧实现只要求"有 citizen_id"，
        # 不比较是谁的，于是 A 会话会被推 B 会话生成的内容。给了 `citizen_id` 就
        # 只推**本公民**的段（没给 = 旧行为，兼容既有调用点）。
        if citizen_id and str(hit.get("citizen_id")) != str(citizen_id):
            _skip("other_citizen")
            continue
        if (hit.get("origin") or "main") == "subagent" and not include_subagent:
            _skip("subagent")
            continue
        if int(hit.get("chars") or 0) < int(min_chars):
            _skip("too_short")
            continue
        relevance = float(hit.get("score") or 0.0)
        if relevance < float(min_relevance):
            _skip("low_relevance")
            continue
        # 分项与解释用**同一次**计算：`freshness` 的 dict（给候选字段）与数值 factor
        # （给明细）从这里一起出，各算一遍就会在四舍五入后对不上。
        freshness = rank_mod.freshness_reading(hit.get("occurred_at"), now=now)
        dup_count = int(dup_count_map.get(code, 0)) if dup_count_map else 0
        breakdown = {
            "relevance": round(relevance, 6),
            "freshness": freshness["factor"],
            # 检索侧已按项目下推：这里用**回包里的项目**算，不拿入参冒充命中。
            "project_match": rank_mod.project_match(hit.get("project"), project),
            "dup_penalty": round(rank_mod.dup_penalty(dup_count), 6),
        }
        seen.add(code)
        items.append({"code": code, "pointer": hit["pointer"], "title": _push_title(hit),
                      "reason": _push_reason(hit), "relevance": breakdown["relevance"],
                      "chars": int(hit.get("chars") or 0),
                      "occurred_at": hit.get("occurred_at"), "host": hit.get("host"),
                      "session_id": hit.get("session_id"), "ranker": hit.get("ranker"),
                      # ---- T03a R2：类别 + 阶段 + 分数 + 明细 + 同型新鲜度 ----
                      "kind_class": PUSH_KIND_CLASS, "score": breakdown["relevance"],
                      "score_stage": rank_mod.SCORE_STAGE_FIRST, "rerank": None,
                      "freshness": dict(freshness),
                      "score_breakdown": dict(breakdown)})
        if len(items) >= max(int(limit), 1):
            break
    items.sort(key=lambda item: (-item["relevance"], item["code"]))
    items = items[:max(int(limit), 1)]
    if scorer is None:
        return {"items": items, "skipped": skipped, "ranker": res["meta"]["ranker"]}
    out = []
    for item in items:
        cand = scorer(item)
        if cand is None:
            _skip("scorer_none")
            continue
        if float(cand.get("score") or 0.0) <= 0.0:
            _skip("score_le_zero")            # M11：候选不许绕过 `score > 0`
            continue
        cand.setdefault("relevance", item["relevance"])
        # 项目归属来自**检索侧**的过滤（段表没有 project 列，标签挂在会话上）：
        # `_cand` 回查 `code_index` 对 `conv:` 一定查不到，不补这一格就会被判成"跨项目"。
        if not cand.get("project"):
            cand["project"] = project
        # 解释性字段只补不改：`score` 由 `_cand` 定，这里**不覆盖**（见 docstring）。
        cand.setdefault("kind_class", PUSH_KIND_CLASS)
        cand.setdefault("freshness", dict(item["freshness"]))
        cand.setdefault("score_stage", rank_mod.SCORE_STAGE_FIRST)
        cand.setdefault("rerank", None)
        cand.setdefault("score_breakdown", dict(item["score_breakdown"]))
        out.append(cand)
    out.sort(key=lambda cand: (-cand["score"], cand["code"]))
    return {"items": out, "skipped": skipped, "ranker": res["meta"]["ranker"]}


def conv_push_for_session(*, root, query, project=None, exclude=(), limit=PUSH_MAX_ITEMS,
                          now=None, recent_of=None, dup_counts_of=None,
                          scorer=None, citizen_id=None) -> list:
    """`fish.py` 的**薄壳口**（M8）：把两处主库依赖做成回调，这里只做门槛与去重。

    - `recent_of(hours, now)`     → **本会话**最近推过的 code 集合（挡：24h 内推过就不产）；
    - `dup_counts_of(hours, now)` → **全局**（任意会话/公民）这些 code 的推送次数
      （罚：`dup_penalty`；想只量"别的会话推过没有"，在回调里排除本会话即可）；
    - `scorer(item)`              → 候选 dict（打分公式只有 `fish.py._cand` 那一份）。

    两个回调分开是有意的（首轮 🟠 S3）：`recent_of` 决定"**挡不挡**"（会话维度），
    `dup_counts_of` 决定"**降几分**"（全局维度）。首轮把两者做成同一回调、同一窗口、
    同一会话键，结果是"被挡掉的 code 恰好就是会被数到的 code"，活下来的候选恒为 0
    ——负向特征永不生效，R4 也就无从判别。口径写进两个名字（`recent_*` / `dup_*`），
    接线的人一眼能看出哪个是本会话、哪个是全局。

    为什么搬到这里：`fish.py` 的行数窗口只剩十几行（`test_split_contract` J4 锁 15,300–
    15,500），而这段逻辑与"语料检索 + 门槛"是一体的——留在门面里既挤预算，也让
    推送侧与语料侧的口径各写一遍。**J1 不变**：本模块不认识主库，三个依赖都是注入的。
    缺 `scorer` 就返回空列表：没有打分公式时不发明分数（不猜）。
    """
    if not str(query or "").strip() or not str(project or "").strip() or scorer is None:
        return []
    pushed = set()
    if recent_of is not None:
        try:
            pushed = {str(code) for code in (recent_of(PUSH_DUP_WINDOW_HOURS, now) or ())}
        except Exception:                      # noqa: BLE001 —— 主库读失败不拖垮推送
            pushed = set()
    res = conv_push_candidates(root=root, query=query, project=project,
                               exclude=set(exclude or ()) | pushed, limit=limit,
                               now=now, dup_counts=dup_counts_of, scorer=scorer,
                               citizen_id=citizen_id)
    return res["items"]


# ------------------------------------------------------------------ 治理（T05）


def forget(scope, *, reason=None, dry_run=False, root=None, conn=None, via="api",
           citizen_id=None, session_key=None) -> dict:
    """删除（段 + FTS）并写台账。**台账先写后删**——崩了也不复活（D14）。

    四种作用域（设计 §8 / PRD C-P0-8）**都真的生效**（2026-10-27 复审 🟠-1）：
      · `session` / `host` —— 写台账后整份跳过（采集侧 `tombstone_skip_sets`）；
      · `pointer` / `range` —— 段级台账，采集收口时由 `store.purge_segment_tombstones`
        再删一次（否则源被重写 → 游标归零 → 重采，被 forget 的原文会复活）。
    返回里的 `erase` 是物理擦除回执（`ok` 为 False 时明文可能仍在空闲页/WAL 里）。
    """
    scope = _coerce_scope(scope)
    conn_obj, created = _connect_write(root, conn)
    try:
        return gov_mod.forget(conn_obj, scope, reason=reason, dry_run=dry_run, via=via,
                              citizen_id=citizen_id, session_key=session_key)
    finally:
        if created:
            conn_obj.close()


def rebuild(scope=None, *, dry_run=True, root=None, conn=None, hosts=DEFAULT_HOSTS,
            roots=None, project_of=None, citizen_of=None, candidates_of=None, host=None,
            limit=None, via="api", citizen_id=None, session_key=None,
            apply_=None) -> dict:
    """全量重放（跳过 tombstone）。`apply_=True` 等价于 `dry_run=False`（CLI 的 `--apply`）。"""
    if apply_ is not None:
        dry_run = not bool(apply_)
    conn_obj, created = _connect_write(root, conn)
    try:
        return gov_mod.rebuild(conn_obj, root, ingest_fn=ingest, scope=scope,
                               dry_run=dry_run, hosts=hosts, roots=roots,
                               project_of=project_of, citizen_of=citizen_of,
                               candidates_of=candidates_of, host=host, limit=limit,
                               via=via, citizen_id=citizen_id,
                               session_key=session_key)
    finally:
        if created:
            conn_obj.close()


def doctor(*, root=None, conn=None, via="api", citizen_id=None, session_key=None) -> dict:
    """体检：全库凭证形态命中必须 0；`FISH_CORPUS_NO_REDACT=1` 时必须红。"""
    if conn is None:
        if root is None:
            raise CorpusError("doctor 需要 root 或 conn")
        if not store.db_path(root).exists():
            return {"ok": True, "reasons": [], "db_missing": True, "segments": 0,
                    "sessions": 0, "scanned": 0, "secret_hits": [], "hits": [],
                    "secret_hit_count": 0, "fts_gap": 0, "tombstones": 0,
                    "degraded": [], "orphans": 0, "access_log_rows": 0,
                    "unmapped_sessions": 0, "rules": list(redact_mod.RULE_NAMES),
                    "redact_disabled": redact_mod.disabled(), "via": via,
                    "alarm": None, "stall": {"zero_streak": 0, "alarm": None},
                    "as_of": store.now_utc(), "db_exists": False}
        conn = store.open_db(root)
    return gov_mod.doctor(conn, root=root, via=via, citizen_id=citizen_id,
                          session_key=session_key)


def prune_access_log(*, root=None, conn=None, keep_days=None, dry_run=False) -> dict:
    """访问审计清理（日报一步；默认保留 30 天，设计 §8）。"""
    days = gov_mod.ACCESS_LOG_KEEP_DAYS if keep_days is None else int(keep_days)
    created = False
    if conn is None:
        if root is None:
            raise CorpusError("prune_access_log 需要 root 或 conn")
        if not store.db_path(root).exists():
            return {"cutoff": None, "planned": 0, "deleted": 0, "skipped": "db_missing"}
        conn = store.open_db(root)
        created = True
    try:
        return gov_mod.prune_access_log(conn, keep_days=days, dry_run=dry_run)
    finally:
        if created:
            conn.close()


# ------------------------------------------------------------------ CLI（薄壳的另一半）


def run_command(args, resolvers=None) -> int:
    """`fish corpus <action>` 的**全部动作**（`fish.py` 只留薄壳与解析器注入）。

    为什么搬进门面（M8）：CLI 的动作/渲染分支有 60 多行，而 `fish.py` 的行数窗口
    （`test_split_contract` J4：15,300–15,500）只剩十几行余量——为了塞新代码去删注释是
    拿可读性换行数。搬过来之后 `fish.py` 只剩"开主库连接 + 注入解析器 + 关连接"。

    `resolvers` 由调用方注入（J2：本模块不认识主库）：
      `project_of` / `candidates_of` / `citizen_of` / `mark_accepted`。
    缺解析器时行为与 `fish_corpus.py ingest` 直跑一致（`project_source='unmapped'`）。
    """
    resolvers = dict(resolvers or {})
    project_of = resolvers.get("project_of")
    candidates_of = resolvers.get("candidates_of")
    citizen_of = resolvers.get("citizen_of")
    mark_accepted = resolvers.get("mark_accepted")
    config_of = resolvers.get("config_of")
    root = Path(args.root)
    action = str(getattr(args, "action", "") or "")
    host_arg = str(getattr(args, "host", "all") or "all")
    if action == "ingest":
        hosts = ("dsh", "workbuddy") if host_arg == "all" else (host_arg,)
        result = ingest(hosts, root=root, limit=getattr(args, "limit", None),
                        dry_run=bool(getattr(args, "dry_run", False)),
                        days=getattr(args, "days", None),
                        project_of=project_of, candidates_of=candidates_of,
                        citizen_of=citizen_of, config_of=config_of)
        if result.get("busy"):
            # 并发时**exit 0**：日报不该因为别人正在采而报假红（设计 §4.3 ①）。
            print(f"语料采集：忙（{result.get('skipped')}），本轮跳过")
            return 0
        dry_run = bool(getattr(args, "dry_run", False))
        tag = "（演练）" if dry_run else ""
        print(f"语料采集{tag}：会话 {result['sessions_seen']}"
              f" / 正常 {result['sessions_ok']} / 跳过 {result['sessions_skipped']}"
              f" / 新增段 {result['segments_new']} / 更新段 {result['segments_updated']}"
              f" / 未变 {result['unchanged']}"
              f" / 降级 {len(result['degraded'])} / 孤儿 {len(result['orphan'])}"
              f" / 用时 {result['elapsed_sec']}s")
        # 采集窗口（2026-10-27）：口径与"更早的跳过了几个"必须打在回执上——
        # 否则"只采了 3 天"和"源里就这么多"在命令行上长得一模一样。
        print("  🗓 时间窗口：%s（更早跳过 %d 个会话文件，不入库）"
              % ("最近 %d 天" % result["window_days"] if result.get("window_days")
                 else "不限", result.get("skipped_by_window") or 0))
        for item in result["degraded"]:
            # 出口脱敏（安全终审）：path 是**源日志绝对路径**，可能把 cwd 里的
            # `token=<值>` 原样带回终端；与 read/stats 同一份 `_safe_field`。
            print(f"  ⚠ 降级 {item['host']} {_safe_field(item['path'])} {item['reasons']}")
        if result.get("purged_by_segment_tombstone"):
            print(f"  ⏭ 台账回收 {result['purged_by_segment_tombstone']} 段"
                  "（pointer/range 作用域，见 corpus_tombstone）")
        for warn in result.get("warnings") or []:
            print(f"  ⚠ 告警 {warn['host']} {warn['session_id']}：{warn['warning']}")
        if (result.get("stall") or {}).get("alarm"):
            print(f"  ⚠ 语料停滞：连续 {result['stall']['zero_streak']} 轮 0 新增但源在变"
                  "（C-P0-9 红②）")
        return 0 if not result["degraded"] else 1
    if action == "retag":
        counters = retag(
            root=root, host=None if host_arg == "all" else host_arg,
            session_id=getattr(args, "session", None),
            all_=bool(getattr(args, "all", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
            project_of=project_of, citizen_of=citizen_of, candidates_of=candidates_of)
        print(f"语料重算{'（演练）' if counters['dry_run'] else ''}：会话 {counters['sessions']}"
              f" / 变更 {counters['changed']} / 已映射 {counters['mapped']}"
              f" / 未映射 {counters['unmapped']} / 无 cwd {counters['no_cwd']}"
              f" / 冲突 {counters['conflict']}"
              f" / 保留原映射 {counters.get('kept_mapped', 0)}"
              f" / 保留原身份 {counters.get('kept_identity', 0)}")
        for warn in counters.get("warnings") or ():
            # 安全 🟡："已映射 → 未映射"是**越跑越差**的静默故障，必须在回执上叫出来。
            print(f"  ⚠ {warn}")
        return 0
    if action in CLI_ACTIONS:                  # search/read/forget/rebuild/doctor
        key = os.environ.get("FISH_SESSION_KEY")
        rc, out_lines, accept = run_cli(
            action, root=root, session_key=key, target=getattr(args, "target", None),
            host=host_arg, limit=getattr(args, "limit", None),
            max_chars=getattr(args, "max_chars", READ_DEFAULT_MAX_CHARS),
            json_out=bool(getattr(args, "json", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
            apply_=bool(getattr(args, "apply", False)),
            project_of=project_of, candidates_of=candidates_of, citizen_of=citizen_of)
        print("\n".join(out_lines))
        if accept and mark_accepted is not None:   # 精确回填（D12）：前缀回填会让 K7 虚高
            mark_accepted(accept, key)
        return rc
    payload = stats(root=root, host=None if host_arg == "all" else host_arg)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _coerce_scope(scope):
    """scope 归一：字符串形态 `session:<sid>` / `host:<h>` / `log:…` / `conv:…` / `range:a..b`。

    为什么要支持字符串：CLI 只有一个位置参数，把 scope 拆成五个 flag 会让"要不要删、
    删什么范围"这两件事在命令行上纠缠；写成一个可读的 scope 串，误删的代价更高、更好审。
    """
    if isinstance(scope, dict):
        return scope
    text = str(scope or "").strip()
    for prefix, kind in (("session:", "session"), ("host:", "host")):
        if text.startswith(prefix):
            value = text[len(prefix):].strip()
            return ({"type": "host", "host": value} if kind == "host"
                    else {"type": "session", "session_id": value})
    if text.startswith("log:") or text.startswith("conv:"):
        return {"type": "pointer", "pointer": text}
    if text.startswith("range:"):
        start, _, end = text[len("range:"):].partition("..")
        return {"type": "range", "from": start, "to": end}
    raise CorpusError("不认识的 scope：%r（session:<sid>/host:<h>/log:…/conv:…/range:a..b）"
                      % text)


def run_cli(action, *, root=None, target=None, host=None, limit=None, dry_run=False,
            max_chars=READ_DEFAULT_MAX_CHARS, reason="", json_out=False, apply_=False,
            project_of=None, candidates_of=None, citizen_of=None,
            session_key=None) -> tuple:
    """`fish corpus search|read|forget|rebuild|doctor` 的**全部逻辑**。

    返回 `(退出码, 输出行, 需要回填 accepted 的 code 列表)`。放在模块里而不是
    `fish.py`：设计 §12 给 `fish.py` 的行数窗口只剩几十行，把渲染/报错分支塞回去
    只能用删注释来腾地方——那是拿可读性换行数（见交付报告"偏离"）。
    """
    act = str(action or "").strip().lower()
    if act not in CLI_ACTIONS:
        raise CorpusError("run_cli 不认识的动作：%r（只认 %s）"
                          % (act, "/".join(CLI_ACTIONS)))
    host_filter = None if (host or "all").lower() == "all" else host.lower()
    if act == "search":
        res = search(target or "", filters={"host": [host_filter]} if host_filter else None,
                     limit=limit or SEARCH_DEFAULT_LIMIT, root=root, via="cli:corpus",
                     session_key=session_key)
        if json_out:
            return 0, [json.dumps(res, ensure_ascii=False, indent=2)], []
        lines = ["命中 %d 条 / 返回 %d 条（ranker=%s%s）"
                 % (res["meta"]["total_matched"], res["meta"]["returned"],
                    res["meta"]["ranker"],
                    "，已截断" if res["meta"]["truncated"] else "")]
        for hit in res["hits"]:
            lines.append("- %s  score=%.3f  %s  %s"
                         % (hit["pointer"], hit["score"],
                            hit["session_title"] or hit["session_id"][:8],
                            hit["snippet"][:60].replace("\n", " ")))
            lines.append("    理由：%s" % "；".join(hit["why"]))
        if not res["hits"]:
            lines.append("  （没有命中：换个词，或先跑 `fish corpus ingest`）")
        for item in res["meta"]["degraded"]:
            lines.append("  ⚠ %s：%s" % (item.get("reason"), item.get("note") or ""))
        return 0, lines, []
    if act == "read":
        try:
            res = read(target or "", max_chars=max_chars, root=root, via="cli:corpus",
                       session_key=session_key)
        except store.PointerError as exc:
            lines = ["门牌错误（%s）：%s" % (exc.code, exc)]
            for cand in exc.candidates:
                lines.append("  候选：%s" % cand)
            return 2, lines, []
        lines = [res["pointer"],
                 "as_of=%s｜%s｜%s｜%s｜%s"
                 % (res["as_of"], res["occurred_at"], res["role"],
                    res["project"] or "未归属",
                    res["session_title"] or res["sid8"]),
                 "", res["text"], ""]
        if res["truncated"]:
            lines.append("（已截断到 %d 字；库内 %d 字）" % (max_chars, res["chars"]))
        if res["source"]["missing"]:
            lines.append("（%s）" % res["source"]["note"])
        return 0, lines, [res["code"]]
    if act == "forget":
        scope = _coerce_scope(target)
        if isinstance(scope, dict) and scope.get("type") in ("session", "range") \
                and host_filter:
            scope = dict(scope, host=host_filter)
        res = forget(scope, reason=reason or None, dry_run=dry_run, root=root,
                     via="cli:corpus", session_key=session_key)
        head = ("语料删除%s：%s（%s）"
                % ("（演练）" if dry_run else "", res["scope"]["label"],
                   res["scope"]["type"]))
        tail = ("计划删除 %d 段，实际删除 %d 段；台账 %s"
                % (res["planned"], res["segments_deleted"], res["tombstone_key"]))
        lines = [head, tail]
        erase = res.get("erase") or {}
        if erase:
            # DEL-01：擦除结果必须**打在回执上**。旧版只在 JSON 里有，CLI 一个字不印，
            # 于是"busy=1（WAL 没截断）"与"真擦干净了"在命令行上长得一模一样。
            lines.append(
                "擦除：%s｜VACUUM=%s｜checkpoint=%s（busy=%s）｜wal=%s B｜db=%s B"
                % ("OK" if erase.get("ok") else "失败（明文可能仍在空闲页/WAL 里）",
                   "已跑" if erase.get("vacuumed") else "跳过",
                   json.dumps(erase.get("checkpoint")), erase.get("checkpoint_busy"),
                   erase.get("wal_bytes"), erase.get("db_bytes")))
            if erase.get("error"):
                lines.append("  ⚠ %s（已尝试 %d 次；物理擦除需要无并发读者——"
                             "停掉其它语料连接后重跑 forget）"
                             % (erase["error"], erase.get("attempts") or 1))
        return (0 if (not erase or erase.get("ok")) else 1), lines, []
    if act == "rebuild":
        scope = {"type": "host", "host": host_filter} if host_filter else None
        res = rebuild(scope, root=root, host=host_filter,
                      dry_run=not bool(apply_), project_of=project_of,
                      candidates_of=candidates_of, citizen_of=citizen_of,
                      via="cli:corpus", session_key=session_key)
        lines = ["语料重建%s：会话 %d / 重放 %d / 计划 %d 段 / 写入 %d 段 / 现有 %d 段"
                 % ("（演练）" if res["dry_run"] else "", res["sessions"],
                    res["sessions_replayed"], res["planned"], res["written"],
                    res["segments_now"]),
                 "台账跳过：%d 段（%d 条台账）"
                 % (res["skipped_by_tombstone"], res["tombstones"])]
        for item in res.get("skipped_sessions") or []:
            lines.append("  ⏭ %s %s（%d 段，已 forget）"
                         % (item["host"], item["session_id"], item["segments"]))
        return 0, lines, []
    if act == "doctor":
        res = doctor(root=root, via="cli:corpus", session_key=session_key)
        if json_out:
            return (0 if res["ok"] else 1), \
                [json.dumps(res, ensure_ascii=False, indent=2)], []
        lines = ["语料体检：%s（段 %d / 会话 %d / 台账 %d / 降级 %d / 孤儿 %d）"
                 % ("OK" if res["ok"] else "红", res["segments"], res["sessions"],
                    res["tombstones"], len(res["degraded"]), res["orphans"])]
        for item in res["reasons"]:
            lines.append("  ⚠ %s" % item)
        if res.get("alarm"):
            # 复审 🟡："报告说 doctor 打印停滞、实际没打印"——停滞必须在这里可见，
            # 否则一个只在 stats JSON 深处出现的告警等于不存在。
            lines.append("  ⚠ 停滞：连续 %d 轮 0 新增但源在变（C-P0-9 红②）"
                         % int((res.get("stall") or {}).get("zero_streak") or 0))
        for hit in res["secret_hits"]:
            lines.append("  ⚠ 明文凭证：%s（%s）" % (hit["pointer"], ",".join(hit["types"])))
        lines.append("  脱敏规则 %d 条｜全文索引差 %d 条｜未归属会话 %d 个"
                     % (len(res["rules"]), res["fts_gap"], res["unmapped_sessions"]))
        return (0 if res["ok"] else 1), lines, []
    raise CorpusError("run_cli 漏了分支：%r" % act)


# ------------------------------------------------------------------ 门面对象


class CorpusFacade:
    """`CorpusFacade`（设计 §3 类图）：门面对象形态，方法即上面这些函数。"""

    def __init__(self, root=None):
        self.root = Path(root) if root is not None else None

    def ingest(self, hosts=DEFAULT_HOSTS, **kwargs):
        kwargs.setdefault("root", self.root)
        return ingest(hosts, **kwargs)

    def search(self, *args, **kwargs):
        kwargs.setdefault("root", self.root)
        return search(*args, **kwargs)

    def read(self, *args, **kwargs):
        kwargs.setdefault("root", self.root)
        return read(*args, **kwargs)

    def stats(self, **kwargs):
        kwargs.setdefault("root", self.root)
        return stats(**kwargs)

    def forget(self, *args, **kwargs):
        kwargs.setdefault("root", self.root)
        return forget(*args, **kwargs)

    def rebuild(self, *args, **kwargs):
        kwargs.setdefault("root", self.root)
        return rebuild(*args, **kwargs)

    def doctor(self, **kwargs):
        kwargs.setdefault("root", self.root)
        return doctor(**kwargs)

    def retag(self, **kwargs):
        kwargs.setdefault("root", self.root)
        return retag(**kwargs)


# ---------------------------------------------------------------- CLI 注册（T01 行数预算）


def register_cli(subparsers, cmd_corpus):
    """语料子命令的 argparse 注册（2026-10-27 · T01 行数预算：从 fish.py 移来）。

    为什么在门面而不是 fish.py：
      · corpus 的子命令面属于语料领域，`fish.py` 只留一行调用；
      · `fish.py` 的 J4 行数守卫（15,300–15,500）把 CLI 注册赶回领域模块；
      · `cmd_corpus` 由调用方传入——门面**不 import fish**（判据 J2）。
    """
    corp = subparsers.add_parser(
        "corpus", help="语料：ingest/stats/retag/search/read/forget/rebuild/doctor")
    corpsub = corp.add_subparsers(dest="action", required=True)
    c_ing = corpsub.add_parser("ingest", help="增量采集两个宿主到独立库 corpus.sqlite")
    c_ing.add_argument("--host", default="all", choices=["all", "dsh", "workbuddy"])
    c_ing.add_argument("--limit", type=int, default=None, help="每宿主最多几个文件")
    c_ing.add_argument("--days", type=int, default=DEFAULT_WINDOW_DAYS,
                       help="只采最近 N 天（0=不限；缺省 3）")
    c_ing.add_argument("--dry-run", dest="dry_run", action="store_true",
                       help="只算不写（不建库；库在则只读）")
    c_ing.set_defaults(func=cmd_corpus)
    c_stat = corpsub.add_parser("stats", help="采集读数：段/会话/degraded/孤儿/滞后")
    c_stat.add_argument("--host", default="all", choices=["all", "dsh", "workbuddy"])
    c_stat.set_defaults(func=cmd_corpus)
    c_re = corpsub.add_parser("retag", help="按库里已有 cwd 重算项目归属（补映射后用）")
    c_re.add_argument("--host", default="all", choices=["all", "dsh", "workbuddy"])
    c_re.add_argument("--session", default=None, help="只重算一个会话")
    c_re.add_argument("--all", dest="all", action="store_true",
                      help="全量重算（缺省即全量；保留此开关以便脚本显式表达）")
    c_re.add_argument("--dry-run", dest="dry_run", action="store_true", help="只算不写")
    c_re.set_defaults(func=cmd_corpus)
    c_sea = corpsub.add_parser("search", help="检索原话（门牌/摘录/分数/为什么）")
    c_sea.add_argument("target")
    c_sea.add_argument("--limit", type=int, default=8)
    c_sea.add_argument("--json", action="store_true")
    c_sea.set_defaults(func=cmd_corpus)
    c_rd = corpsub.add_parser("read", help="按门牌读回原话（conv:/log: 皆可）")
    c_rd.add_argument("target")
    c_rd.add_argument("--max-chars", dest="max_chars", type=int, default=8000)
    c_rd.set_defaults(func=cmd_corpus)
    c_fg = corpsub.add_parser("forget", help="删除：session:<sid> / host:<h> / log:…")
    c_fg.add_argument("target")
    c_fg.add_argument("--dry-run", dest="dry_run", action="store_true")
    c_fg.set_defaults(func=cmd_corpus)
    c_rb = corpsub.add_parser("rebuild", help="全量重放（跳过 tombstone）；默认只报计划")
    c_rb.add_argument("target", nargs="?", default=None)
    c_rb.add_argument("--apply", action="store_true")
    c_rb.add_argument("--host", default="all", choices=["all", "dsh", "workbuddy"])
    c_rb.set_defaults(func=cmd_corpus)
    corpsub.add_parser("doctor", help="语料体检：全库凭证形态必须 0").set_defaults(
        func=cmd_corpus)
