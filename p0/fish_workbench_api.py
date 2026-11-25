#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fish 工作台读口（默认只读；唯一写口是 /api/push/*）— p0/fish_workbench_api.py

## 这个文件解决的问题

fish 工作台首屏要一条**真实**的五源合流时间线：工具动作、看板登记、推送、记忆事件、
系统事件混排成一条改不了的时间线。现有 `/api/activity` 只回 `tool_activity` 一个源；
前端自己合并五个端点就是第二份领域实现（93 §2.1 记的 K8 死法），而且五个 `asOf` 会混进一屏。

所以这里新增四个**只读**端点（契约见 `docs/97-工作台-技术设计-20261123.md` §C）：

    GET /api/facets   左栏切片器：项目（含「未归属」）+ 今日板，各带 lastTouchedAt 与计数
    GET /api/stream   中间流水：五源合流（单键 sk 排序 + 折叠），带游标
    GET /api/stats    右栏页签一：在跑/最近来过、公民、工具、热推、热搜、未闭合档期
    GET /api/entry    右栏页签二：单条详情 + 公民档案 + 该公民最近动作/事件
    GET /api/hosts    设置页：宿主接入层投影（装没装 / 接没接 / 接的哪条通道）
    GET /api/hosts/<id>           单个宿主详情（每次现探）
    POST /api/hosts/<id>/refresh  重探一次（**不写盘**：只重读文件与库，没有副作用）

设置页的「推送」二级页另加四个端点（数据面在 `fish_push_audit.py`）：

    GET  /api/push/overview       诊断概览（总量/采纳/违规/通道/口径 + 配置现场值）
    GET  /api/push/events         最近推送事件（按 id 倒序）
    POST /api/push/config         写**一个**白名单配置键（白名单 + 类型/范围 + 审计）
    POST /api/push/preset         止血 / 恢复 两个预设（同上，另一个粒度的动作）

## 三条硬纪律（与 fish_ui.py 同构，不另立一套）

1. **默认只读**：读连接的每一处都走 `fish_ui.open_readonly()`（`mode=ro` +
   `PRAGMA query_only=1`），每次开连接都由本文件的 `prove_read_only()` **真试一次写**：
   被拒时把 `sqlite3.OperationalError: attempt to write a readonly database` 原文打成一行
   `readonly_proven` JSON（证据，不是声明）；万一真能写就删表并**拒绝启动**。
   **唯一的写口**是 `POST /api/push/config` 与 `POST /api/push/preset`（以及
   `POST /api/hosts/<id>/refresh` —— 那条不写盘）。写口**不在**本文件开写连接：它开在
   `fish_push_audit.write_config / apply_preset` 内部的一条独立连接上，且只允许白名单键，
   每次成功写入都追加一行审计到 `<data>/_audit/push-config.jsonl`。GET 永远走只读连接。
2. **只绑回环**：默认 `127.0.0.1:8790`。这是本机仪表盘，不是服务。
   **写口默认只在回环上开**：`--host` 不是 127.0.0.1 / localhost / ::1 时，两个推送写端点
   一律 **403 `write_requires_loopback`**（读端点照常），启动时另打一行大声警告；
   真要在一个非回环地址上写，必须显式加 `--allow-remote-write`（缺省没有这个能力）。
   判据见 `verify_push_panel.py` V-29（`--host 0.0.0.0` 无 flag → 403；加 flag → 200）。
3. **不猜**：查不到给 null / `availability.state="missing"` + 原因；**绝不拿假数据填**。

## 复用（禁止第二份实现）

- 只读连接与写拒绝证明：`fish_ui.open_readonly` / `fish_ui.assert_write_rejected`
- 人名：`fish_naming.disambiguate`（经 `base_label`，绝不吐公民 ID）
- 会话短标识：`fish_naming.session_short`（**唯一实现**，4→6→8 避撞阶梯；
  空/非法键给空串，标签怎么展示由调用点决定——112 W-B · N-5）
- 宿主：**一律取 `session_key` 前缀**（实测 `tool_activity.host` 列与之一致率仅 2.5%，
  详见 §1.2六）；`citizens_all.host` 是自报值域，另作「公民档案的宿主」
- 推送采纳率：`fish_push_vocab.accept_rate` / `fish.push_stats(...)` 的 `accept_rate`
  （分母 `decided = accepted + ignored`；无已判时回 `None` 不回 0）——这里**不重算**

## 用

    python p0/fish_workbench_api.py --db I:\\app\\fish-data\\data\\index.sqlite --port 8790
    python p0/fish_workbench_api.py --dist ui/dist          # 同时发前端静态文件
    python p0/fish_workbench_api.py --selftest              # 不起服务，跑四个端点打印读数

## 边界（不装样子）

- 一期不读对话语料（另一库）；`events` 记的是系统事件不是对话轮次。
- `events` 只有一部分能按会话归属（本文件用 `payload.session_key` **或**
  `actor.session_key` 兜底，所以比设计文档记的 28.4% 略高）；板视角下仍会有系统事件缺席，
  响应里用 `unattributedEvents` 如实报出条数。
- 折叠在**服务端**算（前端只渲染）：折叠要跨源比较「同会话是否连续」，前端算就是第二份实现。
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import re
import sqlite3
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

# ── 复用现有实现（绝不写第二份）────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import fish_common  # noqa: E402  可见性谓词 `_visibility_sql` / 主人解析 `resolve_owner` / `warn_ignored`
import fish_lineage  # noqa: E402  会话树注册表（树视图缺投影时现读宿主头的唯一入口）
import fish_naming  # noqa: E402  人类面取名唯一实现
import fish_push_vocab  # noqa: E402  推送采纳率的唯一实现（112 W-B · N-3）
import fish_hosts   # noqa: E402  宿主接入层（只读探测：装没装/接没接/接的哪条通道）
import fish_push_audit  # noqa: E402  推送面板数据面（只读诊断 + 窄写配置）
import fish_ui      # noqa: E402  只读连接纪律 + 会话短标识兜底

SCHEMA = "fish-workbench/1"
#: 此读口实现的端点清单（`/api/meta` 的 `capabilities`）——**已实现**，不是"计划"。
#: 口径：页签名叫「现场」，数据契约名沿用 `workbench`（改名要连带改测试，契约锚点不动）。
#: `hosts` = 设置页的宿主投影（`GET /api/hosts`、`GET /api/hosts/<id>`、
#: `POST /api/hosts/<id>/refresh`）；它**只读**，唯一的 POST 也不写盘（重探一次）。
#: `push` = 设置页的推送面板（`/api/push/overview|events` 只读，`/api/push/config|preset`
#: 是**唯一两个真写口**：白名单 + 类型/范围 + 审计，见 `fish_push_audit.py`）。
API_CAPABILITIES = ("entry", "facets", "health", "hosts", "meta", "push", "stats", "stream")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8790
#: 回环地址白名单。**写口只在这些地址上默认开放**（本机仪表盘不是服务）：
#: 绑到别的地址（`0.0.0.0` / 局域网 IP）时，`POST /api/push/config|preset` 回
#: 403 `write_requires_loopback`，除非显式 `--allow-remote-write`。
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")


def is_loopback(host: str | None) -> bool:
    """这个监听地址是不是回环（写口默认开放的那一类）。"""
    return (host or "").strip().lower() in LOOPBACK_HOSTS

# 库位置：实测 fish 数据在 <fish-data>/data/index.sqlite；`p0/data` 是指过去的 junction，
# 而 `fish_ui.DEFAULT_DB` 指向的 `<fish root>/data` 在本机不存在——不能沿用它。
_DB_CANDIDATES = [
    os.path.join(os.path.dirname(_HERE), "data", "index.sqlite"),
    os.path.join(_HERE, "data", "index.sqlite"),
    os.path.join(os.path.dirname(os.path.dirname(_HERE)), "fish-data", "data", "index.sqlite"),
]

# ── 刻度（口径写死，不写成散落常量）──────────────────────────────────────
WINDOW_DELTA = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "all": None,
}
WINDOW_ALL_SINCE = "1970-01-01T00:00:00Z"

# 折叠：同会话 60 秒内连续 ≥3 条工具动作 → 折一条（96 §六规则 3）。
# 用环境变量而不是改代码，是为了让变异自证能**不改源码**地跑一次（见 README）。
FOLD_SEC = int(os.environ.get("FISH_WB_FOLD_SEC", "60") or 60)
FOLD_MIN = int(os.environ.get("FISH_WB_FOLD_MIN", "3") or 3)

# 推送折叠（97 §A3 第 4 行「同 code 连续推送折叠 ×N」）：**同一 code 连续相邻**的推送折叠。
# 门槛与工具折叠共用 FOLD_MIN——这样 QA 的 D2 变异（FISH_WB_FOLD_MIN=9999）能一次关掉两种折叠，
# 不会因为"push 还在折"而让 D2 的 groups=0 判据假红。间隔上限单独一个键：
# 实测同 code 相邻推送间隔 p50=51s / p90=152s，取 300s 能把"一阵连推"折成一条，
# 又不会把早/晚两次互不相干的推送缝在一起。
FOLD_PUSH = os.environ.get("FISH_WB_FOLD_PUSH", "1").strip().lower() not in (
    "0", "false", "no", "off")
FOLD_PUSH_SEC = int(os.environ.get("FISH_WB_FOLD_PUSH_SEC", "300") or 300)

# 游标下推（F-1）：默认开。=0 时退回旧的「分支各取最新 N 行、游标在并集外过滤」路径——
# 这条路径**只为变异自证**保留（把游标放回并集外 → 深翻页断言必须红），生产不要关。
CURSOR_PUSHDOWN = os.environ.get("FISH_WB_CURSOR_PUSHDOWN", "1").strip() != "0"

LIMIT_DEFAULT = 40
LIMIT_MAX = 500
MOTION_WINDOW_MIN = 30          # 「在跑」窗口（96 §七①：近 30 分钟）
FOLD_ITEMS_MAX = 100            # 单组折叠最多带多少条明细（24h 实测最大组 72）

CACHE_TTL_S = {
    "facets": 10.0,
    "stats": 60.0,
    "entry": 30.0,
    "streamcount": 10.0,
    # P2-4（2026-11-26）：宿主探测一次 5 个宿主 ~0.7s；设置页打开会连读，
    # 2–5 秒足够挡住连点，又不会把「刚卸载」藏到分钟级。显式 refresh 会清这一档。
    "hosts": 3.0,
    # P2-4：推送概览 ~0.8s；10 秒内重复打开设置页复用同一份读数，
    # POST /api/push/config|preset 写完后立刻失效（见 cache_invalidate）。
    "push_overview": 10.0,
}
#: 进程内缓存条数上限（P2-5）：长跑进程里每换一个 win/days/entry key 就多一条，
#: 没有上限就是慢性内存泄漏。LRU 淘汰最久未用的条目。
CACHE_MAX_ENTRIES = 256

#: 当前进程读的库（缓存键的一部分：同一进程换库不会串读数）。
_CURRENT_DB = ""

# 五源优先级（97 §C2 的 rk，越小越先）：把它拍成 sk 的单键，避免三元组方向反掉。
RK = {"tool": 0, "post": 1, "push": 2, "event": 3}

BOARD_KIND_WORD = {
    "checkin": "签到",
    "progress": "报进度",
    "done": "完成",
    "note": "备忘",
    "left": "下板",
}

MEMORY_EVENT_TYPES = ("memory.recorded", "memory.accessed", "memory.retired")


# ── 结构化日志（一行 JSON 到 stderr）────────────────────────────────────
def now_dt() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """库里所有时间戳都是 `YYYY-MM-DDTHH:MM:SSZ`（**只到秒**），这里保持一致。"""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def log(event: str, **fields) -> None:
    record = {"ts": iso(now_dt()), "event": event}
    record.update(fields)
    print(json.dumps(record, ensure_ascii=False), file=sys.stderr, flush=True)


# ── 信封（93 §6 / 97 §C1）──────────────────────────────────────────────
def envelope(data, *, as_of: str, availability: dict, sources: list) -> dict:
    return {
        "schema": SCHEMA,
        "asOf": as_of,
        "availability": availability,
        "data": data,
        "sources": sources,
    }


def error_payload(code: str, hint: str, action: str) -> dict:
    """错误信封：`{ok:false, code, hint, action}`——三段（出了什么/为什么/你能做什么）。"""
    return {
        "ok": False,
        "code": code,
        "hint": hint,
        "action": action,
        "schema": SCHEMA,
        "asOf": iso(now_dt()),
    }


class Sources:
    """`sources[]` 的累加器：每个数据源一条 `{source, ok, rows, error, ms}`。"""

    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, source: str, *, rows: int | None = None, ok: bool = True,
            error: str | None = None, ms: float | None = None) -> None:
        entry = {"source": source, "ok": ok, "rows": rows,
                 "error": error, "ms": None if ms is None else round(ms, 2)}
        self.items.append(entry)

    def as_list(self) -> list:
        return self.items

    # 注：这里曾有一个 `state()` 方法会返回 `"partial"`（部分数据源不可用）。
    # QA F-8 指出它**没有任何可构造的输入**——全文件无人调用（任何一个源失败都走
    # `_missing_envelope` 的 `missing`，而不是这里），UI 侧也没有「部分可用」的判定口径。
    # 口径没定就不发明状态（与 96 §七「四者文案必须不同」同纪律）：方法已删，
    # `partial` 也从前端类型里删掉，见 `ui/README.md` §六「本期不支持 partial」。


# ── 小缓存（facets 10s / stats 60s / entry 30s / hosts 3s / push_overview 10s）
#: 用 OrderedDict 维护 LRU：`cache_get` 命中时 move_to_end，`cache_put` 超上限时
#: 从队头淘汰最久未用的条目（P2-5：_CACHE 无上限 = 长跑进程的慢性内存泄漏）。
_CACHE: "OrderedDict[tuple, tuple[float, dict]]" = OrderedDict()
_CACHE_LOCK = threading.Lock()


def cache_get(scope: str, key: tuple):
    cache_key = (scope, key)
    with _CACHE_LOCK:
        hit = _CACHE.get(cache_key)
        if not hit:
            return None
        ts, value = hit
        if time.time() - ts > CACHE_TTL_S.get(scope, 0):
            # 过期即删：不删的话过期条目会一直占着 LRU 名额。
            del _CACHE[cache_key]
            return None
        _CACHE.move_to_end(cache_key)
    return value


def cache_put(scope: str, key: tuple, value: dict) -> None:
    cache_key = (scope, key)
    with _CACHE_LOCK:
        _CACHE[cache_key] = (time.time(), value)
        _CACHE.move_to_end(cache_key)
        while len(_CACHE) > CACHE_MAX_ENTRIES:
            _CACHE.popitem(last=False)


def cache_invalidate(scope: str) -> int:
    """清掉一个 scope 的全部条目，返回清掉的条数。

    P2-4：`POST /api/push/config|preset` 写完配置后必须让 `push_overview` 立即失效，
    否则 10 秒窗口里读到的还是写前读数——「改了但没变」会被误判成写失败。
    `POST /api/hosts/<id>/refresh` 用同一入口清 `hosts` 档。
    """
    with _CACHE_LOCK:
        doomed = [key for key in _CACHE if key[0] == scope]
        for key in doomed:
            del _CACHE[key]
        return len(doomed)


# ── 连接 ────────────────────────────────────────────────────────────────
#: 只读证明的探针 DDL：在只读连接上一定被 SQLite 拒绝（不会留下任何东西）。
_READONLY_PROBE_DDL = "CREATE TABLE __fish_wb_readonly_probe (x INTEGER)"


def prove_read_only(conn: sqlite3.Connection, db_path: str) -> None:
    """真试一次写，把**真实的异常类型与消息**打进 stderr；能写则拒绝启动（F-6）。

    为什么不能只靠 `fish_ui.assert_write_rejected`：它在被拒绝时**静默 return**，
    启动日志里只剩一句"写操作已被拒绝并验证"的声明——那是声明，不是证据（QA F-6 指的就是这个）。
    这里做同一件事，但把 `sqlite3.OperationalError: attempt to write a readonly database`
    的原文作为一行 `readonly_proven` JSON 落到 stderr；万一真能写，立刻删表并**拒绝启动**
    （退出码非 0，不是只打印一行日志）。
    """
    try:
        conn.execute(_READONLY_PROBE_DDL)
    except sqlite3.Error as exc:
        message = str(exc).lower()
        if not any(word in message for word in ("readonly", "read-only", "query_only")):
            # 写探针失败但不是"只读拒绝"——判不到，不冒充通过（缺证据不是通过）。
            log("readonly_unproven", db=db_path, probe="create_table",
                errorType=type(exc).__name__, error=str(exc),
                action="拒绝启动：写探针的失败原因不是只读拒绝，判不到")
            raise SystemExit(
                f"只读校验判不到：写探针报 {type(exc).__name__}: {exc}，"
                "这不是'只读拒绝'的证据。拒绝启动。"
            ) from exc
        log("readonly_proven", db=db_path, probe="create_table",
            errorType=type(exc).__name__, error=str(exc),
            basis="mode=ro + PRAGMA query_only=1（fish_ui.open_readonly）")
        return
    # 走到这里 = 写成功了。先收拾干净再大声失败（与 fish_ui 的纪律一致）。
    try:
        conn.execute("DROP TABLE __fish_wb_readonly_probe")
    except sqlite3.Error as exc:
        log("readonly_probe_cleanup_failed", db=db_path,
            errorType=type(exc).__name__, error=str(exc))
    log("readonly_failed", db=db_path, probe="create_table",
        errorType=None, error="探针建表居然成功了：这个连接能写",
        action="拒绝启动")
    raise SystemExit(
        "只读校验失败：这个连接居然能建表。拒绝启动——"
        "宁可没有读口，也不能让一个自称只读的面有写能力。"
    )


def open_conn(db_path: str) -> sqlite3.Connection:
    """只读连接：复用 fish_ui 的 `mode=ro` + `query_only`，每次开连接都留下**被拒证据**。

    另外加三条**连接级只读 PRAGMA**（F-3，不改库、不写任何东西）：这条读口最贵的两件事是
    「时间列上没有索引 → 全表扫描」与「ORDER BY 的临时 B 树落盘」。实测（同一查询、同一时刻
    交替跑 7 次）基线中位 45.2 ms → cache_size 64 MB 后 20.1 ms / mmap 256 MB 后 22.5 ms。
    老版本或不支持的环境直接忽略（退回默认，不影响正确性）。
    """
    conn = fish_ui.open_readonly(db_path)
    for pragma in ("PRAGMA cache_size=-65536", "PRAGMA mmap_size=268435456",
                   "PRAGMA temp_store=MEMORY"):
        try:
            conn.execute(pragma)
        except sqlite3.Error:
            pass  # A档：连接级调优是性能旁路（失败退回默认，不影响正确性）；且 open_conn 每次请求都跑，出声会变成每请求一条噪声
    prove_read_only(conn, db_path)
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (name,)
    ).fetchone()
    return row is not None


# ── 时间窗 / 作用域 ─────────────────────────────────────────────────────
def parse_window(win: str | None, now: datetime) -> tuple[str, str]:
    name = (win or "24h").strip().lower()
    if name not in WINDOW_DELTA:
        raise ValueError(f"win 只能是 {sorted(WINDOW_DELTA)}")
    delta = WINDOW_DELTA[name]
    return name, (WINDOW_ALL_SINCE if delta is None else iso(now - delta))


def parse_scope(query: dict) -> tuple[str, str | None, int | None]:
    """`scope=all|project:<名>|project:__none__|board:<id>`，也接受 `project=` / `board=`。

    返回 `(kind, project, board_id)`；单选语义写死（97 §B1：不出现「项目+看板」同时生效）。
    """
    raw = (query.get("scope") or [""])[0].strip()
    if raw and raw != "all":
        if raw.startswith("project:"):
            name = raw[len("project:"):]
            if not name:
                raise ValueError("scope=project: 缺少项目名")
            return ("project-none" if name == "__none__" else "project"), name, None
        if raw.startswith("board:"):
            try:
                return "board", None, int(raw[len("board:"):])
            except ValueError as exc:
                raise ValueError("scope=board: 需要一个整数板 id") from exc
        raise ValueError("scope 只能是 all / project:<名> / project:__none__ / board:<id>")
    project = (query.get("project") or [""])[0].strip()
    if project:
        return ("project-none" if project == "__none__" else "project"), project, None
    board = (query.get("board") or [""])[0].strip()
    if board:
        try:
            return "board", None, int(board)
        except ValueError as exc:
            raise ValueError("board 需要一个整数板 id") from exc
    return "all", None, None


def parse_int(query: dict, key: str, default: int, lo: int, hi: int) -> int:
    raw = (query.get(key) or [""])[0].strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} 必须是整数") from exc
    return max(lo, min(hi, value))


# ── 五源合流的 SQL 构造（一处构造，facets 计数与 stream 共用）──────────────
_UNION_COLS = (
    "src, rk, pk, ts, session_key, citizen_id, project, "
    "tool_name, action, path, path_dir, path_abs, ok, "
    "etype, payload, actor, subject, "
    "channel, reaction, latency_sec, code, board_id, post_kind, post_text"
)

# ── 事件的项目归属（F-5）───────────────────────────────────────────────
# 实测：24h 内 `memory.recorded` 的 payload **0 条带 project**（只有 `session.opened` 带），
# 所以按 `json_extract(payload,'$.project')` 过滤项目时记忆事件整类消失。
# 记忆的真值在 `code_index`（`events.subject.id` = code），这里统一改成 join code_index。
# 第二取法「从 actor 的 session 反查项目」实测覆盖 **0/43**（记忆事件不带 session_key，
# presence 里也查不到对应会话），所以只作对照读数、不进 SQL——见回执里的两种取法一致率。
_EVENT_CODE_SQL = (
    # 注意 `subject` 不一定是 JSON：`maintenance.pruned` 的 subject 是裸字符串 'push_log'
    # （实测 20 行），直接 json_extract 会抛 "malformed JSON"。所以先 json_valid 再取。
    "COALESCE("
    "CASE WHEN json_valid(subject) THEN json_extract(subject,'$.id') END,"
    "CASE WHEN json_valid(payload) THEN json_extract(payload,'$.code') END,"
    "CASE WHEN json_valid(payload) THEN json_extract(payload,'$.codes[0]') END)"
)
_EVENT_PROJECT_PAYLOAD_SQL = (
    "COALESCE(json_extract(COALESCE(payload,'{}'),'$.project'),"
    " json_extract(COALESCE(payload,'{}'),'$.projects[0]'))"
)
_EVENT_PROJECT_CODE_INDEX_SQL = (
    "COALESCE((SELECT ci.project FROM code_index ci WHERE ci.code = " + _EVENT_CODE_SQL + "),"
    " json_extract(COALESCE(payload,'{}'),'$.project'),"
    " json_extract(COALESCE(payload,'{}'),'$.projects[0]'))"
)
# `FISH_WB_EVENT_PROJECT=payload` = 变异开关：退回按 payload.project 取项目（F-5 的变异自证）。
EVENT_PROJECT_MODE = os.environ.get("FISH_WB_EVENT_PROJECT", "codeindex").strip().lower()
EVENT_PROJECT_SQL = (_EVENT_PROJECT_PAYLOAD_SQL if EVENT_PROJECT_MODE == "payload"
                     else _EVENT_PROJECT_CODE_INDEX_SQL)

# ── 看板登记的项目归属（R-04）─────────────────────────────────────────────
# `board_posts` 只有 `board_id`，**没有项目列**；项目名在 `boards.scope_key`
# （实测 `boards.scope` 只有 `project` / `root` 两种，root 的 `scope_key` 是空串）。
# 评审 R-04 原文：`scope=project:CZHY` 的 30 行里 25 行 `project=None`——因为这一列被写死成 NULL，
# 而作用域过滤走的是 `board_id IN (… boards WHERE scope_key=?)`，于是"筛得到、显示不出来"。
#
# 所以这里只留**一处表达式**，stream / 计数 / facets / entry 四处都用它：
# 四个地方各推导一遍，就会出现「筛出来的行项目是 null、facets 里却计在 CZHY」这种自相矛盾。
# 根看板（`scope='root'` 或 `scope_key` 为空）项目置 NULL，并由 `boardScope` 字段显式标出「根看板」。
_POST_PROJECT_SQL = (
    "(SELECT CASE WHEN b.scope = 'project' AND COALESCE(b.scope_key,'') <> '' "
    "THEN b.scope_key ELSE NULL END FROM boards b WHERE b.id = {alias}.board_id)"
)


def post_project_sql(alias: str = "board_posts") -> str:
    """看板登记的「项目归属」唯一表达式（R-04 的落点，四处共用）。"""
    return _POST_PROJECT_SQL.format(alias=alias)


# ── 游标（sk 三元组）：解析/格式化/单分支下推谓词 ───────────────────────
_CURSOR_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\|(\d)\|(\d{12})$")


def parse_cursor(raw: str | None) -> tuple[str, int, int] | None:
    """`2026-11-23T12:52:44Z|9|000000040471` → `(ts, 9-rk, pk)`；空 = 无游标。"""
    text = (raw or "").strip()
    if not text:
        return None
    match = _CURSOR_RE.fullmatch(text)
    if not match:
        raise ValueError("游标形如 2026-11-23T12:52:44Z|9|000000040471")
    return (match.group(1), int(match.group(2)), int(match.group(3)))


def format_cursor(cursor: tuple[str, int, int] | None) -> str:
    if not cursor:
        return ""
    ts, w, pk = cursor
    return f"{ts}|{w}|{int(pk):012d}"


def _cursor_where(ts_col: str, pk_col: str, rk: int, cursor: tuple[str, int, int],
                  direction: str) -> tuple[str, list]:
    """把 sk 游标下推进**一个分支**的 WHERE（分支的 rk 是常量，所以三元组比较可降维）。

    sk = `ts|(9-rk)|pad12(pk)`，降序比较就是 (ts, 9-rk, pk) 三元组降序：
      · 游标源与分支同源  → ts 相同还要比 pk；
      · 分支优先级更低（9-rk 更小）→ ts 相同也全是"更旧"，取 ts <= 游标 ts；
      · 分支优先级更高（9-rk 更大）→ ts 相同全是"更新"，取 ts < 游标 ts。
    """
    c_ts, c_w, c_pk = cursor
    w = 9 - rk
    if direction == "newer":
        if w > c_w:
            return f"{ts_col} >= ?", [c_ts]
        if w == c_w:
            return (f"({ts_col} > ? OR ({ts_col} = ? AND {pk_col} > ?))",
                    [c_ts, c_ts, c_pk])
        return f"{ts_col} > ?", [c_ts]
    if w < c_w:
        return f"{ts_col} <= ?", [c_ts]
    if w == c_w:
        return (f"({ts_col} < ? OR ({ts_col} = ? AND {pk_col} < ?))",
                [c_ts, c_ts, c_pk])
    return f"{ts_col} < ?", [c_ts]


#: 四源的物理表名（拓扑固定，与 `STREAM_TABLES` 同源）。
_SCOPE_TABLE = {"tool": "tool_activity", "post": "board_posts",
                "push": "push_log", "event": "events"}

#: 板作用域 = **板上会话**（97 §A2 改法二）：板上会话做的事，不是板上登记的条数。
_BOARD_SESSIONS_SQL = "(SELECT DISTINCT session_key FROM presence WHERE board_id = ?)"

#: 事件的会话归属（只有一部分事件能按会话归属，见文件头「边界」）。
_EVENT_SESSION_SQL = (
    "COALESCE(json_extract(COALESCE(payload,'{}'),'$.session_key'),"
    " json_extract(COALESCE(actor,'{}'),'$.session_key'))"
)


def scope_where(scope_kind: str, project: str | None, board_id: int | None,
                since: str) -> dict[str, tuple[list[str], list]]:
    """四源各自的 `(WHERE 片段列表, 参数)` —— **全文件唯一一处作用域语义**（R-16）。

    取行（`_branch_selects`）与计数（`_count_sql`）都只从这里拿谓词。为什么必须是**一份**：
    这两处各推导一遍，就会出现「筛出来的行项目是 null、facets 里却计在 CZHY」这类自相矛盾
    （评审 R-04 的原症状），而且是**两份口径同时改才不漂**的那种隐性耦合。
    现在改这里一行，行与计数**同时**变——判据见 `p0/tools/verify_v4.py` 的 V3：
    ① 作用域谓词的三个字面量（`project = ?` / `project IS NULL OR project = ''` /
    `session_key IN`）在全文各有且只有 `scope_where` 里的那几处；② `_branch_selects` 与
    `_count_sql` 都调用本函数；③ 变异（把项目谓词整段删掉）后**计数与行数一起变**。

    返回的列表是**每次新建的**（调用方可以往下 append 游标谓词，不会串到下一次调用）。
    """
    where: dict[str, tuple[list[str], list]] = {
        "tool": (["seen_at >= ?"], [since]),
        "post": (["created_at >= ?"], [since]),
        "push": (["pushed_at >= ?"], [since]),
        "event": (["ts >= ?"], [since]),
    }

    def add(name: str, predicate: str, *args) -> None:
        where[name][0].append(predicate)
        where[name][1].extend(args)

    if scope_kind == "project":
        add("tool", "project = ?", project)
        add("push", "project = ?", project)
        # 看板登记的项目不在自己表上（R-04）：四处共用同一表达式，口径才不漂。
        add("post", post_project_sql() + " = ?", project)
        # 记忆事件的项目来自 code_index（F-5）：同样只在这里定义一次。
        add("event", EVENT_PROJECT_SQL + " = ?", project)
    elif scope_kind == "project-none":
        add("tool", "(project IS NULL OR project = '')")
        add("push", "(project IS NULL OR project = '')")
        add("post", post_project_sql() + " IS NULL")
        add("event", f"({EVENT_PROJECT_SQL} IS NULL OR {EVENT_PROJECT_SQL} = '')")
    elif scope_kind == "board":
        add("tool", f"session_key IN {_BOARD_SESSIONS_SQL}", board_id)
        add("push", f"session_key IN {_BOARD_SESSIONS_SQL}", board_id)
        add("post", "board_id = ?", board_id)
        add("event", f"{_EVENT_SESSION_SQL} IN {_BOARD_SESSIONS_SQL}", board_id)
    return where


def _branch_selects(scope_kind: str, project: str | None, board_id: int | None,
                    since: str, cursors: dict | None = None,
                    direction: str = "older") -> list[tuple[str, list]]:
    """四个源各自的 `(SQL 片段, 参数)`。

    ⚠️ 三条纪律都落在这里：
    1. **作用域语义唯一落点**：本函数不再自己写作用域谓词，整段来自 `scope_where`——
       与 `_count_sql` 是**同一份**，「左栏计数」与「点下去看到的条数」不可能漂。
    2. **游标必须下推到分支内**（F-1）：先按作用域 + 游标收窄，再 `ORDER BY ... LIMIT`。
       旧实现把游标放在并集外过滤，而分支各自只交出「全窗口最新 N 行」——游标越深，
       分支窗口越够不到更旧的行，深翻页就会系统性地丢行（24h 实测只到 17.8%）。
    3. **游标按分支独立**（`cursors={src: (ts,w,pk)}`）：四个源的密度不同（24h 里工具 2.5k、
       推送 0.7k、事件 0.3k），共用一个游标会让某分支先被 LIMIT 截断，合并出来的"前缀"
       在它的边界以下并不完整——取页时必须按每个分支自己的前沿算完整性边界（见 collect_stream）。
    """
    cursors = cursors or {}
    where = scope_where(scope_kind, project, board_id, since)
    tool_w, tool_p = where["tool"]
    post_w, post_p = where["post"]
    push_w, push_p = where["push"]
    ev_w, ev_p = where["event"]

    # 游标下推：每个分支各自把 sk 比较降维成 ts/pk 比较（见 `_cursor_where`）。
    if cursors.get("tool"):
        pred, args = _cursor_where("seen_at", "id", 0, cursors["tool"], direction)
        tool_w.append(pred); tool_p.extend(args)
    if cursors.get("post"):
        pred, args = _cursor_where("created_at", "id", 1, cursors["post"], direction)
        post_w.append(pred); post_p.extend(args)
    if cursors.get("push"):
        pred, args = _cursor_where("pushed_at", "id", 2, cursors["push"], direction)
        push_w.append(pred); push_p.extend(args)
    if cursors.get("event"):
        pred, args = _cursor_where("ts", "seq", 3, cursors["event"], direction)
        ev_w.append(pred); ev_p.extend(args)

    # sk 在**分支内**就算好（省掉一次并集后的全行字符串拼接；排序仍按同一个 sk）。
    tool_sql = (
        f"SELECT {_UNION_COLS}, sk FROM (SELECT 'tool' src, 0 rk, id pk, seen_at ts, "
        "session_key, citizen_id, project, tool_name, action, path, path_dir, path_abs, ok, "
        "NULL etype, NULL payload, NULL actor, NULL subject, "
        "NULL channel, NULL reaction, NULL latency_sec, NULL code, "
        "NULL board_id, NULL post_kind, NULL post_text, "
        "seen_at || '|9|' || printf('%012d', id) sk "
        f"FROM tool_activity WHERE {' AND '.join(tool_w)} ORDER BY seen_at DESC LIMIT ?) a"
    )
    post_sql = (
        f"SELECT {_UNION_COLS}, sk FROM (SELECT 'post' src, 1 rk, id pk, created_at ts, "
        f"session_key, citizen_id, {post_project_sql()} project, NULL tool_name, NULL action, "
        "NULL path, NULL path_dir, NULL path_abs, NULL ok, "
        "NULL etype, NULL payload, NULL actor, NULL subject, "
        "NULL channel, NULL reaction, NULL latency_sec, NULL code, "
        "board_id, kind post_kind, text post_text, "
        "created_at || '|8|' || printf('%012d', id) sk "
        f"FROM board_posts WHERE {' AND '.join(post_w)} ORDER BY created_at DESC LIMIT ?) p"
    )
    push_sql = (
        f"SELECT {_UNION_COLS}, sk FROM (SELECT 'push' src, 2 rk, id pk, pushed_at ts, "
        "session_key, citizen_id, project, NULL tool_name, NULL action, "
        "NULL path, NULL path_dir, NULL path_abs, NULL ok, "
        "NULL etype, NULL payload, NULL actor, NULL subject, "
        "channel, reaction, latency_sec, code, "
        "NULL board_id, NULL post_kind, NULL post_text, "
        "pushed_at || '|7|' || printf('%012d', id) sk "
        f"FROM push_log WHERE {' AND '.join(push_w)} ORDER BY pushed_at DESC LIMIT ?) l"
    )
    ev_sql = (
        f"SELECT {_UNION_COLS}, sk FROM (SELECT 'event' src, 3 rk, seq pk, ts, "
        "COALESCE(json_extract(COALESCE(payload,'{}'),'$.session_key'),"
        " json_extract(COALESCE(actor,'{}'),'$.session_key')) session_key, "
        "json_extract(COALESCE(actor,'{}'),'$.citizen_id') citizen_id, "
        + EVENT_PROJECT_SQL + " project, "
        "NULL tool_name, NULL action, NULL path, NULL path_dir, NULL path_abs, NULL ok, "
        "type etype, payload, actor, subject, "
        "NULL channel, NULL reaction, NULL latency_sec, NULL code, "
        "NULL board_id, NULL post_kind, NULL post_text, "
        "ts || '|6|' || printf('%012d', seq) sk "
        f"FROM events WHERE {' AND '.join(ev_w)} ORDER BY ts DESC LIMIT ?) e"
    )
    return [
        (tool_sql, tool_p + [1]),
        (post_sql, post_p + [1]),
        (push_sql, push_p + [1]),
        (ev_sql, ev_p + [1]),
    ]


def _union_sql(scope_kind: str, project: str | None, board_id: int | None,
               since: str, branch_limit: int,
               cursors: dict | None = None,
               direction: str = "older") -> tuple[str, list]:
    branches = _branch_selects(scope_kind, project, board_id, since,
                               cursors=cursors, direction=direction)
    sql = " UNION ALL ".join(part for part, _ in branches)
    params: list = []
    for part, args in branches:
        # 最后那个 LIMIT ? 占位就是 branch_limit（前面保持作用域/游标参数原样）。
        params.extend(args[:-1])
        params.append(branch_limit)
    return sql, params


def _count_sql(scope_kind: str, project: str | None, board_id: int | None,
               since: str) -> tuple[str, list]:
    """与取行**同一份**作用域谓词的计数版（facets / 「本刀 N 条」用）。

    这里不再出现任何作用域判断——`scope_kind` 的分支只在 `scope_where` 里（R-16）。
    """
    where = scope_where(scope_kind, project, board_id, since)
    parts: list[str] = []
    params: list = []
    for name in ("tool", "post", "push", "event"):
        predicates, args = where[name]
        parts.append(
            f"SELECT COUNT(*) FROM {_SCOPE_TABLE[name]} WHERE {' AND '.join(predicates)}")
        params.extend(args)
    return " UNION ALL ".join(parts), params


def scope_label(scope_kind: str, project: str | None, board_id: int | None) -> str:
    if scope_kind == "project":
        return project or ""
    if scope_kind == "project-none":
        return "未归属"
    if scope_kind == "board":
        return f"看板 {board_id}"
    return "全部切片"


# ── 取名（人名 / 会话短标识 / 宿主）─────────────────────────────────────
def load_citizen_labels(conn: sqlite3.Connection, citizen_ids) -> dict:
    """一批公民 → `{citizen_id: label}`；底层是 `fish_naming.disambiguate`（唯一实现）。"""
    ids = sorted({str(c) for c in citizen_ids if c})
    if not ids:
        return {}
    rows = []
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        rows.extend(conn.execute(
            "SELECT citizen_id, display_name, host, created_at FROM citizens_all "
            "WHERE citizen_id IN (" + ",".join("?" * len(chunk)) + ")", chunk).fetchall())
    labels = fish_naming.disambiguate([
        {"citizen_id": r["citizen_id"], "display_name": r["display_name"],
         "host": r["host"], "created_at": r["created_at"]} for r in rows
    ])
    for cid in ids:
        labels.setdefault(cid, fish_naming.base_label("", "", cid))
    return labels


def session_tag(session_key: str | None) -> str:
    """会话短标识：**只有一份实现**（`fish_naming.session_short`；112 W-B · N-5）。

    旧实现是"正常路径 `session_short` → 兜底 `fish_ui.session_tag`（取末 4 位）"，
    同一个会话两条路径给出两个都"看着对"的标签。现在两处同取**前缀**，兜底分支
    不再有意义；空标签的展示策略由调用点决定（面板这里是 `""`）。

    结果缓存（F-3）：`session_short` 每次要跑一遍避撞阶梯，单次 ~30 µs；500 条的页要调它
    1.8k 次（实测占 `limit=500` 总耗时的 55 ms）。会话键与短标识在一次进程生命周期内不变，
    同一个会话在全页/翻页里显示同一个短标识反而是我们要的稳定性。
    """
    return _session_tag_cached((session_key or "").strip())


@functools.lru_cache(maxsize=4096)
def _session_tag_cached(key: str) -> str:
    if not key:
        return ""
    return fish_naming.session_short(key)


def host_from_session(session_key: str | None) -> str:
    """**宿主一律取会话键前缀**（机器证据）。

    为什么不取 `tool_activity.host` 列：实测 `host='workbuddy'` 而 `session_key` 前缀是
    `codebuddy` 的有 26,994 行，一致率只有 2.5%——那一列是账号归属，不是宿主。
    """
    key = (session_key or "").strip()
    if not key or ":" not in key:
        return ""
    return key.split(":", 1)[0].strip().lower()


def host_label_for_protocol(protocol: str | None) -> str:
    """会话键前缀 → **人类面宿主标签**（并列，不猜；设计 106 §6）。

    ⚠️ 不许改成 `host_label(protocol)`——这是设计里点名的坑：`host_label("codebuddy")`
    返回 `"CodeBuddy"`（`_ALIAS_OWNER` 命中的是 id 本身），而 `codebuddy:` 前缀下的
    26,200 行其实属于 **WorkBuddy**。`hosts_for_protocol` 的口径是「反查不唯一时不猜」，
    所以这里**并列**显示 `"WorkBuddy/CodeBuddy"`；用 host_label 会把 WorkBuddy 的会话
    显示成 CodeBuddy —— 一个看起来很像真的假归属。
    """
    key = (protocol or "").strip().lower()
    if not key:
        return ""
    return "/".join(fish_hosts.REGISTRY[host].display
                    for host in fish_hosts.hosts_for_protocol(key))


# ── 会话树三键（113 §9.1 · W5）：父 / 代理 / 关系 ─────────────────────────
#
# 三个键**只在读时叠加**在 actor 上，`events.actor`（库内 JSON 列）**一个键都不加**。
# 理由不是洁癖：那张表的三种老形状（`{citizen_id,evidence_level}` 2,793 /
# `{kind,id,session_key}` 349 / `{system}` 21，113 §1-3）各有消费方，而父链是
# **宿主日志派生**的事实——把它写进不可变表等于把"派生"变成"原始"，将来宿主头变了
# 也回不去了。所以它是 `actor_of` 的读时装饰，不是事件结构的一部分。

#: 从 `session_lineage` 取的四列（父 / 代理 id / 代理角色 / 关系）。
LINEAGE_COLS = ("parent_session", "agent_id", "agent_role", "relation")

#: 会话键前缀 → 会话树宿主（`session_lineage.host` 只认 dsh / workbuddy）。
#: `codebuddy:` 前缀下的 26,200 行其实属于 WorkBuddy（`host_label_for_protocol` 记的那条坑），
#: 而树表按**宿主**分，所以这里归一到 workbuddy。
_LINEAGE_HOSTS = {"dsh": "dsh", "workbuddy": "workbuddy", "codebuddy": "workbuddy"}


def protocol_of(session_key: str) -> tuple:
    """会话键 → `(protocol, session_id)`；空/非法 → `("", "")`（**不猜**）。"""
    return fish_naming.split_session_key(session_key or "")


def _rows_as_dicts(cursor) -> list:
    """把结果集转成 dict 列表——**不依赖 `conn.row_factory`**。

    为什么：面板的连接是 `sqlite3.Row`（`fish_ui.open_readonly`），而 CLI / 测试用
    `fish.connect`（返回 tuple）。`lineage_of` 两边都要能调，靠下标会静默错位。
    """
    cols = [d[0] for d in (cursor.description or ())]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def lineage_of(conn, session_keys, *, host: str | None = None) -> dict:
    """批量取 `{会话键: {parent_session, agent_id, agent_role, relation}}`。

    四类"判不到"都**不写键**（调用方渲染成 null）：没有会话键、前缀不是已知协议、
    投影表里没有这一行（W6 回填之前这是常态）、老库还没建投影表（`sqlite3.Error`）。
    **绝不拿 main / 空串冒充**——"判不到"与"它是根"在树上长得一样，但语义相反。

    一次查询取整页（`WHERE (host,session_id) IN (...)`），不做 per-row 查询：
    `collect_stream` 一页最多 500 条，逐条查是 500 次索引命中。
    """
    pairs, out = [], {}
    for key in dict.fromkeys(k for k in (session_keys or ()) if k):
        proto, sid = protocol_of(key)
        table_host = _LINEAGE_HOSTS.get(proto)
        if not table_host or (host and table_host != host) or not sid:
            continue
        pairs.append((key, table_host, sid))
    if not pairs:
        return out
    where = " OR ".join("(host = ? AND session_id = ?)" for _ in pairs)
    params = [value for _key, table_host, sid in pairs for value in (table_host, sid)]
    try:
        rows = _rows_as_dicts(conn.execute(
            f"SELECT host, session_id, {', '.join(LINEAGE_COLS)} "
            f"  FROM session_lineage WHERE {where}", params))
    except sqlite3.Error:                  # 老库没有投影表：判不到，不炸
        return out
    index = {(r["host"], r["session_id"]): r for r in rows}
    for key, table_host, sid in pairs:
        row = index.get((table_host, sid))
        if row is not None:
            out[key] = {col: row[col] for col in LINEAGE_COLS}
    return out


def _lineage_keys(row, lineage: dict | None) -> dict:
    """父 / 代理 / 关系三键（113 §9.1）。判不到就是 `None`，**绝不用 main/空串冒充**。

    `agent` 的形状是 `{id, role}`，两列**都为空时给 `None`**——挂一个
    `{"id": None, "role": None}` 上去，前端会渲染成"有代理但没名字"，
    而真相是"这一行没有代理信息"（DSH 侧 `agent_id` 恒 NULL，见 113 §2.3）。
    """
    node = (lineage or {}).get(row["session_key"] or "")
    if not node:
        return {"parentSession": None, "agent": None, "relation": None}
    agent = None
    if node.get("agent_id") or node.get("agent_role"):
        agent = {"id": node.get("agent_id"), "role": node.get("agent_role")}
    return {"parentSession": node.get("parent_session"), "agent": agent,
            "relation": node.get("relation")}


def actor_of(row, labels: dict, *, lineage: dict | None = None) -> dict:
    """一行 → 界面上的"谁"。四档判定（system / 公民 / 会话 / 未命名）**一个不改**，
    末端统一叠三键（113 §9.1：唯一一处 `out.update`，四档分支不各写一遍）。

    `lineage` 是 `lineage_of()` 的返回值（缺省 `None` = 判不到）：折叠阶段只用
    `actor["sessionTag"]`，不需要为它查库，所以 stub 侧刻意不传。
    """
    cid = (row["citizen_id"] or "") if row["citizen_id"] else ""
    session_key = row["session_key"] or ""
    tag = session_tag(session_key)
    host = host_from_session(session_key)
    label = labels.get(cid) if cid else None
    if cid == "system":
        out = {"name": "系统", "labelKind": "system", "citizenId": "system",
               "host": host, "sessionTag": tag}
    elif label:
        out = {"name": label, "labelKind": "citizen", "citizenId": cid,
               "host": host, "sessionTag": tag}
    elif tag:
        out = {"name": f"会话 {tag}", "labelKind": "session", "citizenId": cid or None,
               "host": host, "sessionTag": tag}
    else:
        out = {"name": "未命名", "labelKind": "none", "citizenId": cid or None,
               "host": host, "sessionTag": tag}
    out.update(_lineage_keys(row, lineage))      # 唯一一处叠加（113 §9.1）
    return out


# ── 会话树视图（113 §9.3 · W5）：递归 CTE + LIMIT 懒加载 ──────────────────
#
# 为什么树视图在**这个**文件而不在 `fish_board.py`（113 §9.3 原写 board）：
# `fish_board.py` 的行数窗口 `FISH_BOARD_LINES_MAX=2440`、实测 2,437——只剩 3 行。
# 树视图（查询 + 渲染）放进去必须抬 `test_split_contract.py` 的窗口常量，那不在 W5 的
# 允许面；而本文件是 W5 的**读取面本体**（"读取路由与 UI"），且没有行数窗口。
# 偏差已在交付里列名，**落点变了、语义没变**。

#: 一屏最多渲染多少行。懒加载的口径：**先给近的**，超出的用 `truncated` 如实报出，
#: 不静默截断（静默截断会让"树只有 3 个孩子"看起来像事实）。
TREE_ROW_LIMIT = 200

#: 向下展开的递归 CTE（每个节点 1 行，`d` = 相对根的层数）。
#:
#: 两个守卫都必须在 SQL 里，不能靠 Python 事后剪枝：
#:   · `instr` 路径守卫：**未标 cycle 的环**（W6 之前是常态）会无限递归；
#:   · `d.rel <> 'cycle'`：113 §9.3 明写"`relation='cycle'` 的行不展开"——它自己
#:     照常出现（读者要知道这里成环了），只是不再往下走。
_TREE_DOWN_SQL = """
WITH RECURSIVE down(sid, rel, d, path) AS (
  SELECT session_id, COALESCE(relation, ''), 0, '/' || session_id || '/'
    FROM session_lineage WHERE host = :host AND session_id = :sid
  UNION ALL
  SELECT c.session_id, COALESCE(c.relation, ''), d.d + 1,
         d.path || c.session_id || '/'
    FROM down d
    JOIN session_lineage c ON c.host = :host AND c.parent_session = d.sid
   WHERE d.d < :depth
     AND d.rel <> 'cycle'
     AND instr(d.path, '/' || c.session_id || '/') = 0
)
SELECT sid, d FROM down ORDER BY d, sid LIMIT :limit
"""

#: 树视图每行要带的列（`depth` / `relation` / `basis` 是 113 §9.3 点名的三列）。
TREE_COLS = ("session_id", "parent_session", "depth", "relation", "basis", "origin",
             "agent_id", "agent_role", "agent_label", "cwd", "lineage_path",
             "depth_host", "source", "observed_at")


def _tree_host_of(conn, session: str) -> tuple:
    """`session tree <x>` 的 x → `(host, 裸 id, 是否推断)`。

    三条判据，**顺序即优先级**：会话键前缀（机器证据）→ 投影表里这一行的 host →
    都判不到才按 `dsh` 现读，且**标成推断**。裸 id 在 DSH 是常态（113 §0.3：668 裸 /
    114 带 `session-`），所以这里不能因为"没有前缀"就当错误。
    """
    proto, sid = protocol_of(session)
    if proto:
        return _LINEAGE_HOSTS.get(proto, ""), sid, False
    bare = str(session or "").strip()
    if not bare:
        return "", "", False
    try:
        rows = _rows_as_dicts(conn.execute(
            "SELECT host FROM session_lineage WHERE session_id=? LIMIT 1", (bare,)))
    except sqlite3.Error:
        rows = []
    if rows:
        return rows[0]["host"], bare, False
    return "dsh", bare, True


def _tree_rows_projection(conn, host: str, sid: str, depth: int,
                          limit: int) -> tuple:
    """投影表优先：CTE 拿到 `(sid, 层数)` → 再取整行。返回 `(rows, truncated)`。"""
    walk = _rows_as_dicts(conn.execute(
        _TREE_DOWN_SQL, {"host": host, "sid": sid, "depth": max(int(depth), 0),
                         "limit": int(limit) + 1}))
    truncated = len(walk) > int(limit)
    walk = walk[:int(limit)]
    if not walk:
        return [], False
    ids = [r["sid"] for r in walk]
    marks = ", ".join("?" * len(ids))
    detail = {r["session_id"]: r for r in _rows_as_dicts(conn.execute(
        f"SELECT {', '.join(TREE_COLS)} FROM session_lineage "
        f" WHERE host=? AND session_id IN ({marks})", [host] + ids))}
    rows = []
    for item in walk:
        node = dict(detail.get(item["sid"]) or {"session_id": item["sid"]})
        node["depth"] = int(item["d"])
        node["live"] = False
        rows.append(node)
    return rows, truncated


def _tree_rows_live(host: str, sid: str, depth: int, limit: int) -> tuple:
    """投影表没这一行时**现读宿主会话头**（只读、不写投影）。返回 `(rows, truncated)`。

    为什么允许现读：W6 回填之前 `session_lineage` 可能是 0 行（本机实测就是 0 行），
    只读投影的话 `session tree` 在任何真实会话上都会返回空树——那不是"没有父子关系"，
    那是"没投影"。`fish_lineage.parent_of` 早就是这个口径（投影优先、缺行现读）。
    代价写在返回值的 `note` 里：**离线投影，非实时**。
    """
    try:
        adapter = fish_lineage.get_adapter(_LINEAGE_HOSTS.get(host, host))
    except KeyError:
        return [], False
    root = adapter.resolve(sid)
    if root is None:
        return [], False
    children: dict = {}
    truncated = False
    if int(depth) > 0:
        try:
            for node in adapter.scan():
                parent = node.get("parent_session")
                if parent:
                    children.setdefault(parent, []).append(node)
        except (RuntimeError, OSError):     # 宿主日志读不动：只剩根一行（不猜子）
            children = {}
    rows = [dict(root, depth=0, live=True)]
    seen = {root.get("session_id")}
    frontier = [root.get("session_id")]
    for level in range(1, max(int(depth), 0) + 1):
        nxt = []
        for parent in frontier:
            for node in children.get(parent) or ():
                node_sid = node.get("session_id")
                if not node_sid or node_sid in seen:
                    continue
                if len(rows) >= int(limit):
                    truncated = True
                    break
                seen.add(node_sid)
                rows.append(dict(node, depth=level, live=True))
                nxt.append(node_sid)
        frontier = nxt
        if truncated:
            break
    return rows, truncated


def session_tree(conn, session: str, *, depth: int = 1,
                 limit: int = TREE_ROW_LIMIT) -> dict:
    """会话子树（113 §9.3）：**投影优先 → 缺行现读宿主头**，默认只展 1 层。

    返回的结构就是 CLI 的输出契约（`session_tree_text` 只做渲染）：
    `{session, host, session_id, host_inferred, depth, limit, source, rows, truncated, note}`。
    `rows` 每行带 `depth` / `relation` / `basis`（113 §9.3 点名的三列）+ `agent_id` /
    `agent_role` / `origin` / `live`。**判不到根时 `rows=[]` 且 `note` 说明原因**，
    不编一个"根"出来。
    """
    try:
        depth = max(int(depth), 0)
    except (TypeError, ValueError):
        depth = 1
    try:
        limit = max(int(limit), 1)
    except (TypeError, ValueError):
        limit = TREE_ROW_LIMIT
    host, sid, inferred = _tree_host_of(conn, session)
    out = {"session": str(session or ""), "host": host, "session_id": sid,
           "host_inferred": inferred, "depth": depth, "limit": limit,
           "source": "none", "rows": [], "truncated": False, "note": ""}
    if not host or not sid:
        out["note"] = "判不到会话：需要 `host:会话id` 或一个能查到的裸会话 id"
        return out
    try:
        rows, truncated = _tree_rows_projection(conn, host, sid, depth, limit)
    except sqlite3.Error:                 # 老库没有投影表：退现读，不炸
        rows, truncated = [], False
    if rows:
        out.update(source="projection", rows=rows, truncated=truncated)
        if truncated:
            out["note"] = f"已按 limit={limit} 截断：还有更深/更多的行没显示"
        return out
    rows, truncated = _tree_rows_live(host, sid, depth, limit)
    if not rows:
        out["note"] = ("会话树投影里没有这一行，宿主日志也判不到它"
                       "（不是'它是根'——是判不到）")
        return out
    out.update(source="live", rows=rows, truncated=truncated,
               note="离线投影（现读宿主会话头）：**非实时**，父子关系以宿主日志为准")
    return out


def session_tree_text(tree: dict) -> str:
    """把 `session_tree()` 的结构渲染成 CLI 文本：**每行都带 depth/relation/basis**。"""
    head = (f"会话树 {tree.get('session') or '(空)'}  host={tree.get('host') or '?'}"
            f"  来源={tree.get('source')}  层数={tree.get('depth')}")
    if tree.get("host_inferred"):
        head += "（宿主按裸 id 推断为 dsh：判不到就是判不到）"
    lines = [head]
    for row in tree.get("rows") or ():
        pad = "  " * int(row.get("depth") or 0)
        relation = row.get("relation") or "-"
        basis = row.get("basis") or "-"
        origin = row.get("origin") or "-"
        role = row.get("agent_role") or "-"
        agent = row.get("agent_id") or "-"
        live = " ·live" if row.get("live") else ""
        lines.append(f"{pad}- {row.get('session_id')}  depth={row.get('depth')}"
                     f"  relation={relation}  basis={basis}{live}")
        lines.append(f"{pad}    origin={origin}  agent_role={role}  agent_id={agent}")
    if tree.get("truncated"):
        lines.append(f"（已截断：只显示前 {len(tree.get('rows') or ())} 行）")
    if tree.get("note"):
        lines.append(f"（{tree['note']}）")
    return "\n".join(lines) + "\n"


def json_obj(raw) -> dict:
    """库里 `payload` / `actor` / `subject` 都是 JSON 字符串；坏数据当空对象，不炸。"""
    if not raw:
        return {}
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def payload_keys(raw) -> list:
    """只回**键名清单**——`payload` 的值（含 `token_hash`）永不外传。"""
    return sorted(json_obj(raw).keys())


# ── 行 → 界面条目（服务端生成一句话模板，前端不另写一套）────────────────
_USELESS_PATHS = {"", ".", "..", "/", "\\", "./"}


def path_label(row) -> str:
    """渲染顺序 `path_dir → path_abs → path`（97 §A5 段一）。

    实测 `path_dir` 有大量 `.` / `/`（bash 命令串被塞进 path 列的副作用，97 §F-R4）——
    这些值当「目标」显示会读成「改了一处 .」，所以先剔掉无信息量再按顺序取第一个。
    """
    for key in ("path_dir", "path_abs", "path"):
        value = (row[key] or "").strip() if row[key] else ""
        if value and value not in _USELESS_PATHS:
            return value
    return ""


def make_item(row, labels: dict, board_names: dict, lineage: dict | None = None) -> dict:
    src = row["src"]
    ts = row["ts"] or ""
    sk = f"{ts}|{9 - int(row['rk'])}|{int(row['pk']):012d}" if ts else ""
    actor = actor_of(row, labels, lineage=lineage)
    project = (row["project"] or "") if row["project"] else ""
    item = {
        "key": f"{src}:{int(row['pk'])}",
        "src": src,
        "cls": src,
        "rk": int(row["rk"]),
        "ts": ts,
        "sk": sk,
        "actor": actor,
        "project": project or None,
        "text": "",
        "tool": None,
        "path": None,
        "meta": {},
    }

    if src == "tool":
        tool_name = (row["tool_name"] or "").strip()
        action = (row["action"] or "").strip()
        label = path_label(row)
        if tool_name == "edit":
            verb = "改了一处"
        elif tool_name == "write":
            verb = "写了文件"
        elif action == "exec":
            verb = "执行了命令"
        elif action == "write":
            verb = "写了文件"
        else:
            verb = ""
        if verb:
            # exec 行的 `path` 是**命令串**不是目标路径（96 §六：exec→「执行了命令」不带目标），
            # 拼上去会读成「执行了命令 git status --short」——那不是「做在哪」。
            show_target = label if action != "exec" else ""
            item["text"] = f"{actor['name']} {verb}" + (f" {show_target}" if show_target else "")
        else:
            # 未登记工具**原样显示 tool_name**，不猜语义（96 §六）。
            item["text"] = f"{actor['name']} 用了 {tool_name or '未知工具'}" + (
                f"（{label}）" if label else "")
        item["tool"] = {"name": tool_name, "action": action,
                        "ok": None if row["ok"] is None else bool(row["ok"])}
        item["path"] = {"dir": row["path_dir"] or "", "abs": row["path_abs"] or "",
                        "raw": row["path"] or "", "label": label}
        item["meta"] = {"argsDigestPresent": bool(row["args_digest"]) if "args_digest" in row.keys() else None}
    elif src == "post":
        board_id = row["board_id"]
        info = board_names.get(board_id) or {}
        board_name = info.get("label") or f"看板 {board_id}"
        board_scope = info.get("scope") or ""
        kind = (row["post_kind"] or "").strip()
        word = BOARD_KIND_WORD.get(kind, kind or "登记")
        full = (row["post_text"] or "").strip()
        first_line = full.splitlines()[0] if full else ""
        if len(first_line) > 60:
            first_line = first_line[:60] + "…"
        item["cls"] = "post"
        item["text"] = (f"{actor['name']} 在 {board_name} {word}" +
                        (f"：{first_line}" if first_line else ""))
        # 项目归属由 `post_project_sql()` 推导（R-04）：根看板推不出项目，这里显式标出来。
        item["meta"] = {"boardId": board_id, "boardName": board_name, "kind": kind,
                        "fullText": full,
                        "boardScope": board_scope or None,
                        "boardScopeKey": info.get("scopeKey")}
    elif src == "push":
        code = (row["code"] or "").strip()
        item["text"] = f"fish 推了 {code or '（无编码）'} 给 {actor['name']}"
        item["meta"] = {"code": code, "channel": row["channel"] or "",
                        "reaction": row["reaction"], "latencySec": row["latency_sec"]}
    else:
        payload = json_obj(row["payload"])
        etype = (row["etype"] or "").strip()
        item["cls"] = "memory" if etype in MEMORY_EVENT_TYPES else "system"
        if etype == "memory.recorded":
            code = str(payload.get("code") or "")
            item["text"] = f"{actor['name']} 记下了一条记忆 {code}".strip()
            item["meta"] = {"type": etype, "code": code,
                            "title": str(payload.get("title") or "")[:80],
                            "payloadKeys": payload_keys(row["payload"])}
        elif etype == "memory.accessed":
            codes = payload.get("codes") or []
            first = str(codes[0]) if codes else ""
            more = f"（共 {len(codes)} 条）" if len(codes) > 1 else ""
            item["text"] = f"{actor['name']} 取用了记忆 {first}{more}".strip()
            item["meta"] = {"type": etype, "code": first, "count": len(codes),
                            "source": str(payload.get("source") or ""),
                            "payloadKeys": payload_keys(row["payload"])}
        elif etype == "memory.retired":
            code = str(payload.get("code") or "")
            item["text"] = f"{actor['name']} 退役了记忆 {code}".strip()
            item["meta"] = {"type": etype, "code": code,
                            "payloadKeys": payload_keys(row["payload"])}
        elif etype == "git.commit":
            sha = str(payload.get("sha") or "")
            subject = str(payload.get("subject") or "")
            item["text"] = (f"{actor['name']} 提交了 {sha[:8]}"
                            + (f"「{subject[:40]}」" if subject else ""))
            item["meta"] = {"type": etype, "sha8": sha[:8], "subject": subject[:120],
                            "payloadKeys": payload_keys(row["payload"])}
        elif etype == "session.opened":
            item["text"] = "开了一个会话"
            item["meta"] = {"type": etype}
        elif etype == "citizen.registered":
            name = str(payload.get("display_name") or "")
            item["text"] = f"登记了一个公民{('：' + name) if name else ''}"
            item["meta"] = {"type": etype, "displayName": name}
        else:
            # 未登记类型**显示 type 原值**，不译成看不见原文的「系统动作」（96 §六）。
            item["text"] = f"发生了 {etype}" if etype else "发生了一条系统事件"
            item["meta"] = {"type": etype,
                            "payloadKeys": payload_keys(row["payload"])}
        item["related"] = {}
    return item


# ── 折叠（服务端算，前端只渲染）────────────────────────────────────────
def _ts_epoch(ts: str) -> int | None:
    """`YYYY-MM-DDTHH:MM:SSZ` → epoch 秒；格式不对回 None。

    手写切片而不是 `strptime`（F-3）：库里时间戳格式是写死的（见 `iso()`），而 500 条的页
    折叠时要比较约 1k 次相邻时间，`strptime` 单次 ~28 µs（实测 30 ms）。结果缓存，
    同一时间戳在折叠/翻页里会反复出现。
    """
    try:
        return int(datetime(int(ts[0:4]), int(ts[5:7]), int(ts[8:10]),
                            int(ts[11:13]), int(ts[14:16]), int(ts[17:19]),
                            tzinfo=timezone.utc).timestamp())
    except (ValueError, IndexError, TypeError):
        return None


@functools.lru_cache(maxsize=16384)
def _ts_epoch_cached(ts: str) -> int | None:
    return _ts_epoch(ts)


def _gap_seconds(newer: str, older: str) -> float:
    a = _ts_epoch_cached(newer)
    b = _ts_epoch_cached(older)
    if a is None or b is None:
        return float("inf")
    return float(abs(a - b))


def _tool_run_end(items: list[dict], i: int) -> int:
    """从 i 起的同会话连续工具动作（60 s 内）到哪里结束（不含）。"""
    head = items[i]
    j = i + 1
    while j < len(items):
        nxt = items[j]
        if nxt["src"] != "tool" or nxt["actor"]["sessionTag"] != head["actor"]["sessionTag"]:
            break
        if _gap_seconds(items[j - 1]["ts"], nxt["ts"]) > FOLD_SEC:
            break
        j += 1
    return j


def _push_run_end(items: list[dict], i: int) -> int:
    """从 i 起的**同一 code 连续推送**到哪里结束（不含）。

    「连续」按显示顺序严格相邻：中间夹了任何非推送行就断开——只有这样才能保证
    折叠条覆盖的是一段真正连续的时间，不会出现「折叠条时间跨度压过下一行」的倒序观感。
    间隔上限 FOLD_PUSH_SEC（默认 300 s）：把"一阵连推"折成一条，又不把早晚两次硬缝在一起。
    """
    head = items[i]
    code = head["meta"].get("code") or ""
    j = i + 1
    while j < len(items):
        nxt = items[j]
        if nxt["src"] != "push" or (nxt["meta"].get("code") or "") != code:
            break
        if _gap_seconds(items[j - 1]["ts"], nxt["ts"]) > FOLD_PUSH_SEC:
            break
        j += 1
    return j


def fold_items(items: list[dict]) -> tuple[list[dict], dict]:
    """两条折叠规则都在这里（服务端算，前端只渲染）：

    1. 同会话 60 s 内连续 ≥3 条工具动作 → 折一条（96 §六规则 3）；
    2. 同一 code 连续 ≥FOLD_MIN 条推送 → 折一条（97 §A3 第 4 行「同 code 连续推送折叠 ×N」）。
       97 §A3 明确要求这条，旧实现只折工具动作，于是项目视图首屏 90% 是同一个 code 的推送刷屏。

    返回 `(显示条目, {groups, covered, thresholdSec, minItems})`。
    折叠条**按组内最新时间参与排序**（本函数输入已按 sk 降序，组头即最新一条）。
    """
    out: list[dict] = []
    groups = 0
    covered = 0
    i = 0
    while i < len(items):
        head = items[i]
        run_end: int | None = None
        kind = ""
        if head["src"] == "tool" and head["actor"]["sessionTag"]:
            run_end, kind = _tool_run_end(items, i), "tool"
        elif FOLD_PUSH and head["src"] == "push" and (head["meta"].get("code") or ""):
            run_end, kind = _push_run_end(items, i), "push"

        if run_end is None:                      # 不参与任何折叠：原样放行
            out.append(head)
            i += 1
            continue
        run = items[i:run_end]
        if len(run) < FOLD_MIN:                  # 不够门槛：整段原样放行（组内行不丢）
            out.extend(run)
            i = run_end
            continue

        group_items = run[:FOLD_ITEMS_MAX]
        if kind == "tool":
            project = head["project"] or "未归属项目"
            key = f"fold:{head['actor']['sessionTag']}:{run[-1]['sk']}"
            text = (f"{head['actor']['name']} 在 {project} 连续做了 "
                    f"{len(run)} 个动作（展开）")
        else:
            code = head["meta"].get("code") or ""
            key = f"fold:push:{code}:{run[-1]['sk']}"
            # 一组里可能推给多个对象（实测 30 条组里有 2 个）；文字不能断言成"给某一个人"。
            names = {it["actor"]["name"] for it in run if it["actor"].get("name")}
            if len(names) == 1:
                text = f"fish 连续推了 {code} 给 {next(iter(names))}"
            else:
                text = f"fish 连续推了 {code}（给 {len(names)} 个对象）"
        out.append({
            "key": key,
            "src": kind,
            "cls": "fold",
            "rk": head["rk"],
            "ts": run[0]["ts"],
            "sk": run[0]["sk"],
            "actor": head["actor"],
            "project": head["project"],
            "text": text,
            "tool": None,
            "path": None,
            "meta": {"kind": kind, "truncated": len(run) > FOLD_ITEMS_MAX,
                     "firstTs": run[0]["ts"], "lastTs": run[-1]["ts"],
                     "code": (head["meta"].get("code") or "") if kind == "push" else None},
            "fold": {"count": len(run), "items": group_items},
        })
        groups += 1
        covered += len(run)
        i = run_end
    return out, {"groups": groups, "covered": covered,
                 "thresholdSec": FOLD_SEC, "minItems": FOLD_MIN}


def last_raw_sk(item: dict) -> str:
    """一条显示条目覆盖的**最旧原始 sk**——翻页游标必须落在原始行上，不能落在折叠条上。"""
    if item.get("fold"):
        inner = item["fold"]["items"]
        return inner[-1]["sk"] if inner else item["sk"]
    return item["sk"]


def last_raw_cursor(item: dict) -> tuple[str, int, int] | None:
    sk = last_raw_sk(item)
    return parse_cursor(sk) if sk else None


def _stub_item(row, labels: dict) -> dict:
    """折叠只需要的一小撮字段（不生成 text / 不解析 payload）。

    `fold_items` 判断"同会话连续"/"同 code 连续"只用到 src / ts / sk / sessionTag / code，
    渲染文字用的 actor.name 与 project 也是现成的。先拿 stub 折叠、只给最终返回的条目做
    `make_item`，可以把 `limit=500` 那 1.8k 行的文本/路径/payload 解析砍掉三分之二（F-3）。
    """
    src = row["src"]
    ts = row["ts"] or ""
    rk = int(row["rk"])
    pk = int(row["pk"])
    return {
        "key": f"{src}:{pk}",
        "src": src,
        "cls": src,
        "rk": rk,
        "ts": ts,
        "sk": f"{ts}|{9 - rk}|{pk:012d}" if ts else "",
        "actor": actor_of(row, labels),
        "project": (row["project"] or None) if row["project"] else None,
        "text": "",
        "tool": None,
        "path": None,
        "meta": {"code": (row["code"] or "").strip() if src == "push" else ""},
    }


def _materialize(item: dict, rows_by_key: dict, labels: dict, board_names: dict,
                 lineage: dict | None = None) -> dict:
    """把 stub（或折叠组）补成完整条目：单个条目、折叠组里的明细都在这里做 make_item。"""
    if not item.get("fold"):
        row = rows_by_key.get(item["key"])
        return make_item(row, labels, board_names, lineage) if row is not None else item
    inner = [make_item(rows_by_key[sub["key"]], labels, board_names, lineage)
             for sub in item["fold"]["items"] if sub["key"] in rows_by_key]
    # 折叠条的 `actor` 原本抄自 stub（折叠阶段不查库），这里用**已带三键**的明细头替换：
    # 否则同一条流水里"展开的明细有父、折叠头没有"，读起来像两种事实。
    head = inner[0]["actor"] if inner else item.get("actor")
    return {**item, "actor": head,
            "fold": {"count": item["fold"]["count"], "items": inner}}


# ── 端点实现 ────────────────────────────────────────────────────────────
def _board_names(conn: sqlite3.Connection) -> dict:
    """板 id → `{label, scope, scopeKey}`（label 是给人看的名字）。

    除了名字，还要带 `scope`：根看板的 `scope='root'`、`scope_key` 为空，它的登记
    **没有项目归属**，得在条目上显式标成「根看板」，不能只留一个空项目让用户猜（R-04）。
    """
    out = {}
    for r in conn.execute("SELECT id, scope, scope_key FROM boards"):
        key = (r["scope_key"] or "").strip()
        out[int(r["id"])] = {
            "label": key or ("（根看板）" if r["scope"] == "root" else (r["scope"] or "看板")),
            "scope": (r["scope"] or "").strip().lower() or None,
            "scopeKey": key or None,
        }
    return out


def _fetch_batch(conn: sqlite3.Connection, *, scope_kind: str, project: str | None,
                 board_id: int | None, since: str, branch_limit: int,
                 cursors: dict, direction: str) -> list:
    """取一批原始行（按 sk 降序，批内不含游标行本身）；游标按分支下推（F-1）。"""
    union_sql, params = _union_sql(scope_kind, project, board_id, since, branch_limit,
                                   cursors=cursors, direction=direction)
    sql = (f"WITH u AS ({union_sql}) "
           f"SELECT {_UNION_COLS}, sk FROM u ORDER BY sk DESC")
    return conn.execute(sql, params).fetchall()


def _fetch_batch_legacy(conn: sqlite3.Connection, *, scope_kind: str, project: str | None,
                        board_id: int | None, since: str, branch_limit: int,
                        cursor: tuple[str, int, int] | None, direction: str) -> list:
    """变异路径（`FISH_WB_CURSOR_PUSHDOWN=0`）：分支各取最新 N 行、游标在并集外过滤。

    这是修复前的旧实现（24h 深翻页只能覆盖 17.8%）。留着是为了让变异自证能一条环境变量
    跑红：把游标放回并集外 → 「翻到底 rawSum == counts.total」断言必须失败。
    """
    union_sql, params = _union_sql(scope_kind, project, board_id, since, branch_limit)
    where, extra = "", []
    if cursor:
        where = " WHERE sk > ?" if direction == "newer" else " WHERE sk < ?"
        extra = [format_cursor(cursor)]
    sql = (f"WITH u AS ({union_sql}) "
           f"SELECT {_UNION_COLS}, sk FROM u{where} ORDER BY sk DESC LIMIT ?")
    return conn.execute(sql, params + extra + [branch_limit]).fetchall()


def collect_stream(conn: sqlite3.Connection, *, scope_kind: str, project: str | None,
                   board_id: int | None, win: str, since: str, limit: int,
                   num_cursor: str | None, direction: str,
                   known_total: int | None = None) -> dict:
    src = Sources()
    board_names = _board_names(conn)
    cursor = parse_cursor(num_cursor)

    # 每批每分支取多少：折叠只让显示条目变少，按 1.0× + 余量取；万一取不满就再取一批
    # （项目视图整页被折成一两条时，短页会让无限滚动没有滚动条、loadMore 永不触发）。
    # 为什么不是 1.4×：24h 里工具源占约三分之二，limit=500 时取 1.4× 会连推送/事件也各取
    # 七百条（实测 1,810 行才产出 500 条显示）；1.0× 实测 1,438 行、单批填满；而 0.7×
    # 反而因为"完整性边界"不够深触发补页，两批共 1,902 行——更慢。
    batch_n = min(max(int(limit * 1.0) + 48, 96), 1000)
    legacy_n = min(max(limit * 5, 200), 2000)   # 变异路径的旧窗口（复现 17.8% 那版）

    rows_total = 0
    sql_ms = 0.0
    shown: list[dict] = []

    if not CURSOR_PUSHDOWN:
        t0 = time.perf_counter()
        rows = _fetch_batch_legacy(conn, scope_kind=scope_kind, project=project,
                                   board_id=board_id, since=since,
                                   branch_limit=legacy_n, cursor=cursor,
                                   direction=direction)
        sql_ms += (time.perf_counter() - t0) * 1000
        rows_total += len(rows)
        labels = load_citizen_labels(conn, [r["citizen_id"] for r in rows if r["citizen_id"]])
        # 三键一次查整批（113 §9.1）：per-row 查会让一页 500 条变成 500 次索引命中。
        lineage = lineage_of(conn, [r["session_key"] for r in rows])
        items = [make_item(r, labels, board_names, lineage) for r in rows]
        display, _ = fold_items(items)
        shown = display[:limit]
    elif direction != "older":
        # 轮询方向（2 s 一次）：只要"比 topCursor 新"的那一小段，单批足够。
        t0 = time.perf_counter()
        rows = _fetch_batch(conn, scope_kind=scope_kind, project=project,
                            board_id=board_id, since=since, branch_limit=legacy_n,
                            cursors={name: cursor for name in RK}, direction=direction)
        sql_ms += (time.perf_counter() - t0) * 1000
        rows_total += len(rows)
        labels = load_citizen_labels(conn, [r["citizen_id"] for r in rows if r["citizen_id"]])
        # 三键一次查整批（113 §9.1）：per-row 查会让一页 500 条变成 500 次索引命中。
        lineage = lineage_of(conn, [r["session_key"] for r in rows])
        items = [make_item(r, labels, board_names, lineage) for r in rows]
        display, _ = fold_items(items)
        shown = display[:limit]
    else:
        # 深翻页：按分支维护前沿，只有「完整性边界之上」的条目才算进本页（F-1 的核心）。
        # 为什么不能只看 merged[:limit]：四个源密度不同（24h 工具 2.5k / 推送 0.7k / 事件 0.3k），
        # 某个分支会被自己的 LIMIT 先截断，它边界以下的行**没取到**；若这时把游标压到更旧的
        # 位置，那一段就永久跳过（实测 24h 项目视图因此丢 160 行，全是 12:44–12:49 的推送）。
        per_branch: dict = {name: cursor for name in RK}
        exhausted: set[str] = set()
        accumulated: list = []
        safe: list[dict] = []
        labels: dict = {}
        batches = 0
        while True:
            t0 = time.perf_counter()
            rows = _fetch_batch(conn, scope_kind=scope_kind, project=project,
                                board_id=board_id, since=since, branch_limit=batch_n,
                                cursors=per_branch, direction=direction)
            sql_ms += (time.perf_counter() - t0) * 1000
            rows_total += len(rows)
            batches += 1
            if rows:
                got: dict[str, list] = {}
                for row in rows:
                    got.setdefault(row["src"], []).append(row)
                for name, rk in RK.items():
                    part = got.get(name) or []
                    if part:
                        last = part[-1]
                        per_branch[name] = (last["ts"], 9 - rk, int(last["pk"]))
                        if len(part) < batch_n:
                            exhausted.add(name)
                    else:
                        exhausted.add(name)
                accumulated.extend(rows)
            # 每轮在**累计行**上排序 + 折叠：折叠组会跨批延续，单批折会把一组切成两行。
            accumulated.sort(key=lambda row: row["sk"], reverse=True)
            labels = load_citizen_labels(
                conn, [r["citizen_id"] for r in accumulated if r["citizen_id"]])
            # 折叠只看 stub（连不连续、同不同 code），不做文本渲染——渲染留给最终返回的条目。
            stubs = [_stub_item(r, labels) for r in accumulated]
            display, _ = fold_items(stubs)
            caps = [format_cursor(per_branch[name])
                    for name in RK if name not in exhausted]
            boundary = max(caps) if caps else ""
            safe = ([it for it in display if last_raw_sk(it) > boundary]
                    if boundary else display)
            if len(safe) >= limit or not rows or not caps or batches >= 12:
                break
        rows_by_key = {f"{r['src']}:{int(r['pk'])}": r for r in accumulated}
        lineage = lineage_of(conn, [r["session_key"] for r in accumulated])
        shown = [_materialize(it, rows_by_key, labels, board_names, lineage)
                 for it in safe[:limit]]

    src.add("tool_activity,board_posts,push_log,events", rows=rows_total, ms=sql_ms)

    # 折叠后再按条数截断；游标落在**最后一条显示条目覆盖的最旧原始行**上。
    cursor_out = format_cursor(last_raw_cursor(shown[-1])) if shown else ""
    top_cursor = shown[0]["sk"] if shown else ""

    # 折叠读数只统计**这一页返回的**条目（拿整段 fetched 算会把没返回的组也算进去）。
    shown_groups = sum(1 for it in shown if it.get("fold"))
    shown_covered = sum(it["fold"]["count"] for it in shown if it.get("fold"))
    shown_raw = (len(shown) - shown_groups) + shown_covered

    total = known_total
    total_known = total is not None
    # 缓存键**含 since**（R-01 残留）：计数是「本刀 N 条」，而"这一刀"由 since 定义。
    # 键里不带它，10 秒 TTL 内两页只要落在同一秒就会复用**另一个 since 的**计数，
    # 翻页末端于是差 1–2 行（窗口滑出去的行）。行与计数必须同一个 since 才可比。
    counted_key = (_CURRENT_DB, scope_kind, project, board_id, win, since)
    if total is None:
        cached_total = cache_get("streamcount", counted_key)
        if cached_total is not None:
            total = cached_total["value"]
            total_known = True
        elif direction == "newer":
            # 轮询（2 s 一次）不重算全量计数：那是首屏才需要的一次性读数，
            # 每 2 秒对 27k 行做 4 次 COUNT 纯属浪费。
            total = None
        else:
            count_sql, count_params = _count_sql(scope_kind, project, board_id, since)
            total = sum(int(r[0]) for r in conn.execute(count_sql, count_params).fetchall())
            total_known = True
            cache_put("streamcount", counted_key, {"value": total})
    return {
        "data": {
            "items": shown,
            "cursor": cursor_out,
            "topCursor": top_cursor,
            "counts": {"total": total, "totalKnown": total_known,
                       "returned": len(shown), "rawFetched": rows_total},
            "fold": {"groups": shown_groups, "covered": shown_covered,
                     "thresholdSec": FOLD_SEC, "minItems": FOLD_MIN,
                     "push": {"enabled": FOLD_PUSH, "gapSec": FOLD_PUSH_SEC},
                     "displayed": len(shown), "displayedRaw": shown_raw},
            "scope": {"kind": scope_kind, "project": project, "board": board_id,
                      "label": scope_label(scope_kind, project, board_id), "win": win,
                      "since": since},
            "window": {"win": win, "since": since},
        },
        "sources": src.as_list(),
    }


def collect_facets(conn: sqlite3.Connection, *, win: str, since: str) -> dict:
    src = Sources()
    now = now_dt()

    # ── 项目：四个源各按自己那一列分组，再合并 ──────────────────────
    t0 = time.perf_counter()
    tool_rows = conn.execute(
        "SELECT project AS name, COUNT(*) AS n, MAX(seen_at) AS last "
        "FROM tool_activity WHERE seen_at >= ? GROUP BY project", (since,)).fetchall()
    src.add("tool_activity", rows=len(tool_rows), ms=(time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    push_rows = conn.execute(
        "SELECT project AS name, COUNT(*) AS n, MAX(pushed_at) AS last "
        "FROM push_log WHERE pushed_at >= ? GROUP BY project", (since,)).fetchall()
    src.add("push_log", rows=len(push_rows), ms=(time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    # 与 stream / 计数**同一个**项目表达式（R-04）：只这处 join `boards.scope_key`、
    # 别处写死 NULL 的话，左栏 CZHY 的计数就与点进去看到的行数对不上。
    post_rows = conn.execute(
        "SELECT " + post_project_sql("p") + " AS name, COUNT(*) AS n, MAX(p.created_at) AS last "
        "FROM board_posts p WHERE p.created_at >= ? GROUP BY 1", (since,)).fetchall()
    src.add("board_posts", rows=len(post_rows), ms=(time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    ev_rows = conn.execute(
        "SELECT " + EVENT_PROJECT_SQL + " AS name, "
        "COUNT(*) AS n, MAX(ts) AS last FROM events WHERE ts >= ? GROUP BY 1", (since,)).fetchall()
    src.add("events", rows=len(ev_rows), ms=(time.perf_counter() - t0) * 1000)

    buckets: dict[str, dict] = {}

    def take(row, source: str) -> None:
        name = (row["name"] or "").strip()
        slot = buckets.setdefault(name, {"count": 0, "last": "", "bySource": {}})
        slot["count"] += int(row["n"] or 0)
        slot["bySource"][source] = int(row["n"] or 0)
        last = row["last"] or ""
        if last > slot["last"]:
            slot["last"] = last

    for row in tool_rows:
        take(row, "tool")
    for row in push_rows:
        take(row, "push")
    for row in post_rows:
        take(row, "post")
    for row in ev_rows:
        take(row, "event")

    unattributed = buckets.pop("", None)
    projects = [
        {"name": name, "label": name, "count": slot["count"],
         "lastTouchedAt": slot["last"] or None, "isUnattributed": False,
         "bySource": slot["bySource"]}
        for name, slot in buckets.items()
    ]
    projects.sort(key=lambda p: (p["lastTouchedAt"] or "", p["name"]), reverse=True)

    # 97 §A2 改法一：未归属是左栏**最大的一格**，必须有；默认不隐藏。
    unattributed_item = None
    if unattributed and unattributed["count"] > 0:
        unattributed_item = {
            "name": "__none__", "label": "未归属",
            "count": unattributed["count"], "lastTouchedAt": unattributed["last"] or None,
            "isUnattributed": True, "bySource": unattributed["bySource"],
        }

    # ── 今日板：period_key = 当日 且 open ───────────────────────────
    period = now.strftime("%y%m%d")
    t0 = time.perf_counter()
    board_rows = conn.execute(
        "SELECT id, scope, scope_key, period_key, status FROM boards "
        "WHERE period_key = ? AND status = 'open' ORDER BY id DESC", (period,)).fetchall()
    src.add("boards", rows=len(board_rows), ms=(time.perf_counter() - t0) * 1000)
    board_ids = [int(r["id"]) for r in board_rows]

    members: dict[int, int] = {}
    running: dict[int, int] = {}
    posts: dict[int, tuple] = {}
    if board_ids:
        marks = ",".join("?" * len(board_ids))
        t0 = time.perf_counter()
        for r in conn.execute(
                f"SELECT board_id, COUNT(DISTINCT session_key) AS n FROM presence "
                f"WHERE board_id IN ({marks}) GROUP BY board_id", board_ids):
            members[int(r["board_id"])] = int(r["n"] or 0)
        cutoff = iso(now - timedelta(minutes=MOTION_WINDOW_MIN))
        for r in conn.execute(
                f"SELECT board_id, COUNT(DISTINCT session_key) AS n FROM presence "
                f"WHERE board_id IN ({marks}) AND last_seen_at >= ? GROUP BY board_id",
                board_ids + [cutoff]):
            running[int(r["board_id"])] = int(r["n"] or 0)
        for r in conn.execute(
                f"SELECT board_id, COUNT(*) AS n, MAX(created_at) AS last FROM board_posts "
                f"WHERE board_id IN ({marks}) GROUP BY board_id", board_ids):
            posts[int(r["board_id"])] = (int(r["n"] or 0), r["last"])
        src.add("presence", rows=sum(members.values()),
                ms=(time.perf_counter() - t0) * 1000)

    boards = []
    for r in board_rows:
        bid = int(r["id"])
        key = (r["scope_key"] or "").strip()
        name = key or ("（根看板）" if r["scope"] == "root" else (r["scope"] or "看板"))
        post_count, last_post = posts.get(bid, (0, None))
        boards.append({
            "id": bid,
            "label": name,
            "scopeKey": key or None,
            "scope": r["scope"],
            "periodKey": r["period_key"],
            "memberCount": members.get(bid, 0),
            "runningCount": running.get(bid, 0),
            "postCount": post_count,
            "lastPostAt": last_post,
            "scopeKind": "board",
        })
    # 「有登记或有人在跑」优先（97 §A2），其余按 id 降序
    boards.sort(key=lambda b: (b["postCount"] > 0 or b["runningCount"] > 0,
                               b["lastPostAt"] or "", b["id"]), reverse=True)

    total_all = sum(p["count"] for p in projects) + (unattributed_item["count"] if unattributed_item else 0)
    state = "ok" if total_all else "empty"
    return {
        "data": {
            "projects": projects,
            "unattributed": unattributed_item,
            "boards": boards,
            "boardsOtherOpen": 0,
            "window": {"win": win, "since": since, "periodKey": period,
                       "total": total_all},
        },
        "sources": src.as_list(),
        "state": state,
    }


def collect_stats(conn: sqlite3.Connection, *, days: int, top: int) -> dict:
    src = Sources()
    now = now_dt()
    since_window = iso(now - timedelta(days=days))
    since_24h = iso(now - timedelta(hours=24))
    cutoff_30m = iso(now - timedelta(minutes=MOTION_WINDOW_MIN))

    # ── ① 在跑 / 最近来过（量词固定「会话」）───────────────────────
    t0 = time.perf_counter()
    in_motion = conn.execute(
        "SELECT COUNT(DISTINCT session_key) FROM tool_activity WHERE seen_at >= ?",
        (cutoff_30m,)).fetchone()[0]
    seen_24h = conn.execute(
        "SELECT COUNT(*) FROM (SELECT session_key FROM presence WHERE last_seen_at >= ? "
        "AND session_key IS NOT NULL AND session_key <> '' "
        "UNION SELECT session_key FROM tool_activity WHERE seen_at >= ? "
        "AND session_key IS NOT NULL AND session_key <> '')", (since_24h, since_24h)).fetchone()[0]
    last_action = conn.execute("SELECT MAX(seen_at) FROM tool_activity").fetchone()[0]
    src.add("tool_activity,presence", rows=int(seen_24h or 0),
            ms=(time.perf_counter() - t0) * 1000)

    # ── ② 未闭合档期（表自带唯一索引 ux_stint_open 定义同一谓词）───
    t0 = time.perf_counter()
    stints = conn.execute(
        "SELECT project, COUNT(*) AS n, MIN(from_ts) AS oldest FROM project_stints "
        "WHERE to_ts IS NULL GROUP BY project ORDER BY n DESC").fetchall()
    src.add("project_stints", rows=sum(int(r["n"] or 0) for r in stints),
            ms=(time.perf_counter() - t0) * 1000)
    open_total = 0
    oldest_days = None
    stint_projects = []
    for r in stints:
        n = int(r["n"] or 0)
        open_total += n
        days_old = None
        if r["oldest"]:
            try:
                start = datetime.strptime(r["oldest"][:19], "%Y-%m-%dT%H:%M:%S").replace(
                    tzinfo=timezone.utc)
                days_old = round((now - start).total_seconds() / 86400, 1)
            except ValueError:
                days_old = None
        if days_old is not None and (oldest_days is None or days_old > oldest_days):
            oldest_days = days_old
        stint_projects.append({"name": (r["project"] or "").strip(), "count": n,
                               "oldestDays": days_old})

    # ── ③ 说了在做但没动手的会话 ──────────────────────────────────
    t0 = time.perf_counter()
    presence_rows = conn.execute(
        "SELECT session_key, citizen_id, status, doing, last_seen_at, auto_source, board_id "
        "FROM presence ORDER BY last_seen_at DESC").fetchall()
    src.add("presence", rows=len(presence_rows), ms=(time.perf_counter() - t0) * 1000)
    labels = load_citizen_labels(conn, [r["citizen_id"] for r in presence_rows if r["citizen_id"]])
    auto_n = declared_n = unclear_n = 0
    idle_items = []
    for r in presence_rows:
        doing = (r["doing"] or "").strip()
        auto = "自动登记" in doing
        auto_source = (r["auto_source"] or "").strip()
        if auto and auto_source:
            auto_n += 1
            bucket = "auto"
        elif auto:
            unclear_n += 1
            bucket = "unclear"
        else:
            declared_n += 1
            bucket = "declared"
            if len(idle_items) < 6:
                idle_items.append({
                    "label": labels.get(r["citizen_id"], "") or "未命名",
                    "sessionTag": session_tag(r["session_key"]),
                    "doing": doing, "autoSource": auto_source,
                    "lastSeenAt": r["last_seen_at"], "bucket": bucket,
                })

    # ── ④ 推送（采纳率**复用 fish.push_stats**，不重算）─────────────
    t0 = time.perf_counter()
    push_detail = None
    try:
        import fish  # 2s 级 import：只在 stats 首调时发生一次（进程内缓存）
        push_detail = fish.push_stats(conn, days=days, now=now)
        push_error = None
    except Exception as exc:  # noqa: BLE001  读口不能因为一个复用函数挂掉
        push_error = f"{type(exc).__name__}: {exc}"
        log("push_stats_unavailable", error=push_error)
    src.add("push_log", ok=push_detail is not None, error=push_error,
            rows=(push_detail or {}).get("total"), ms=(time.perf_counter() - t0) * 1000)

    push_top = []
    if push_detail is not None:
        top_rows = conn.execute(
            "SELECT code, COUNT(*) AS pushed, "
            "SUM(CASE WHEN reaction='accepted' THEN 1 ELSE 0 END) AS accepted, "
            "SUM(CASE WHEN reaction='ignored' THEN 1 ELSE 0 END) AS ignored, "
            "SUM(CASE WHEN reaction IS NULL THEN 1 ELSE 0 END) AS pending, "
            "MAX(pushed_at) AS last FROM push_log WHERE pushed_at >= ? "
            "GROUP BY code ORDER BY pushed DESC, last DESC LIMIT ?",
            (since_window, top)).fetchall()
        for r in top_rows:
            accepted = int(r["accepted"] or 0)
            ignored = int(r["ignored"] or 0)
            push_top.append({
                "code": r["code"] or "",
                "pushed": int(r["pushed"] or 0),
                "accepted": accepted, "ignored": ignored,
                "pending": int(r["pending"] or 0),
                # 与 push_stats 同一个函数（fish_push_vocab.accept_rate）；无已判回 None 不回 0
                "rate": fish_push_vocab.accept_rate(accepted, ignored),
                "lastPushedAt": r["last"],
            })
        channels = conn.execute(
            "SELECT channel, COUNT(*) AS n FROM push_log WHERE pushed_at >= ? "
            "GROUP BY channel ORDER BY n DESC", (since_window,)).fetchall()
        channel_top = [{"channel": r["channel"] or "", "count": int(r["n"])} for r in channels]
    else:
        channel_top = []

    # ── ⑤ 工具使用（今日与累计**分开**，绝不混一格）──────────────────
    t0 = time.perf_counter()
    in24 = conn.execute(
        "SELECT tool_name, COUNT(*) AS n FROM tool_activity WHERE seen_at >= ? "
        "GROUP BY tool_name ORDER BY n DESC", (since_24h,)).fetchall()
    total_tools = conn.execute("SELECT COUNT(*) FROM tool_activity").fetchone()[0]
    cumulative = conn.execute(
        "SELECT tool_name, COUNT(*) AS n FROM tool_activity GROUP BY tool_name "
        "ORDER BY n DESC").fetchall()
    src.add("tool_activity", rows=len(in24), ms=(time.perf_counter() - t0) * 1000)
    tools_in_24h = sum(int(r["n"]) for r in in24)

    # ── ⑥ 公民综合（宿主按 citizens_all.host 归一小写显示，不改库）──
    t0 = time.perf_counter()
    by_kind = conn.execute(
        "SELECT kind AS k, COUNT(*) AS n FROM citizens_all GROUP BY kind ORDER BY n DESC").fetchall()
    by_host = conn.execute(
        # 大小写不统一（库里 `zcode` 与 `ZCode` 并存、`NULL` 与 `unknown` 并存）：
        # **只在查询期归一成小写做合并**，不改库、也不改原始值域。
        "SELECT lower(trim(COALESCE(host,''))) AS h, COUNT(*) AS n "
        "FROM citizens_all GROUP BY 1 ORDER BY n DESC").fetchall()
    citizens_total = conn.execute("SELECT COUNT(*) FROM citizens_all").fetchone()[0]
    active_citizens = conn.execute(
        "SELECT COUNT(DISTINCT citizen_id) FROM tool_activity WHERE seen_at >= ? "
        "AND citizen_id IS NOT NULL", (since_24h,)).fetchone()[0]
    src.add("citizens_all", rows=citizens_total, ms=(time.perf_counter() - t0) * 1000)

    # ── ⑦ 热搜（近窗 + 全期两档，每行带最后取用时间）────────────────
    t0 = time.perf_counter()
    hot_window = conn.execute(
        "SELECT code, COUNT(*) AS n, MAX(accessed_at) AS last FROM memory_access_log "
        "WHERE accessed_at >= ? GROUP BY code ORDER BY n DESC, last DESC LIMIT ?",
        (since_window, top)).fetchall()
    hot_all = conn.execute(
        "SELECT code, COUNT(*) AS n, MAX(accessed_at) AS last FROM memory_access_log "
        "GROUP BY code ORDER BY n DESC, last DESC LIMIT ?", (top,)).fetchall()
    src.add("memory_access_log", rows=len(hot_all), ms=(time.perf_counter() - t0) * 1000)
    titles = {}
    codes = {r["code"] for r in list(hot_window) + list(hot_all) if r["code"]}
    if codes:
        # 热搜榜要**显示标题**，所以它是"可见读路径"，必须与其它读路径走同一条谓词：
        # 复用 `fish_common._visibility_sql`（第 39 轮起可见性执行的唯一实现），
        # 这里**不新写一套判定**——两份真相迟早漂移，而这份漂移是"安静泄露"。
        #
        # viewer = 主人 citizen：本读口是单用户本机仪表盘，读口面就是主人面；
        # owner 由 `fish_common.resolve_owner` 解析（绝不硬编码 id）。解析不到时
        # viewer 为空 → `_visibility_sql` 对 private 一律排除，方向是"少露"不是"多露"。
        #
        # 开关关着（`privacy.visibility_enforced` 默认 False）时它返回空片段，
        # 查询与旧行为逐字节等价。
        vis_sql, vis_params = fish_common._visibility_sql(
            conn, fish_common.resolve_owner(conn))
        code_list = sorted(codes)
        for start in range(0, len(code_list), 500):
            chunk = code_list[start:start + 500]
            for r in conn.execute(
                    "SELECT code, title FROM code_index WHERE code IN ("
                    + ",".join("?" * len(chunk)) + ")" + vis_sql,
                    [*chunk, *vis_params]):
                titles[r["code"]] = r["title"]

    def hot_item(r) -> dict:
        return {"code": r["code"] or "", "title": titles.get(r["code"]),
                "count": int(r["n"] or 0), "lastAccessedAt": r["last"]}

    state = "ok" if (tools_in_24h or total_tools or citizens_total) else "empty"
    return {
        "data": {
            "window": {"days": days, "since": since_window},
            "motion": {
                "inMotion": int(in_motion or 0),
                "seen24h": int(seen_24h or 0),
                "lastActionAt": last_action,
                "windowMinutes": MOTION_WINDOW_MIN,
                # 量词写死「会话」：presence 是会话级，写「人」即口径造假（96 §七①）
                "quantifier": "会话",
                "inMotionSince": cutoff_30m,
            },
            "openStints": {"total": open_total, "projects": stint_projects,
                           "oldestDays": oldest_days},
            "idleDeclared": {"auto": auto_n, "declared": declared_n,
                             "unclear": unclear_n, "items": idle_items,
                             "total": auto_n + declared_n + unclear_n},
            "push": {
                "total": (push_detail or {}).get("total"),
                "accepted": (push_detail or {}).get("accepted"),
                "ignored": (push_detail or {}).get("ignored"),
                "pending": (push_detail or {}).get("pending"),
                "decided": (push_detail or {}).get("decided"),
                # 复用 push_stats 的 accept_rate（decided 分母；无已判回 None）
                "acceptRate": (push_detail or {}).get("accept_rate"),
                "channels": channel_top,
                "top": push_top,
                "rateDenominator": "accepted + ignored（已判）",
            },
            "tools": {
                "in24h": tools_in_24h,
                "in24hByName": [{"name": r["tool_name"] or "", "count": int(r["n"])}
                                for r in in24],
                "cumulative": int(total_tools or 0),
                "cumulativeByName": [{"name": r["tool_name"] or "", "count": int(r["n"])}
                                     for r in cumulative],
            },
            "citizens": {
                "total": int(citizens_total or 0),
                "active24h": int(active_citizens or 0),
                "byKind": [{"kind": r["k"] or "", "count": int(r["n"])} for r in by_kind],
                # 大小写统一（zcode 与 ZCode 并存）——只在显示层归一，**不改库**
                "byHost": [{"host": (r["h"] or None), "count": int(r["n"])} for r in by_host],
            },
            "hotAccess": {
                "window": [hot_item(r) for r in hot_window],
                "allTime": [hot_item(r) for r in hot_all],
            },
        },
        "sources": src.as_list(),
        "state": state,
    }


def collect_entry(conn: sqlite3.Connection, key: str) -> dict | None:
    """单条详情：`tool:40424` / `post:443` / `push:5967` / `event:2978`。"""
    match = re.fullmatch(r"(tool|post|push|event):(\d+)", key or "")
    if not match:
        raise ValueError("id 形如 tool:40424 / post:443 / push:5967 / event:2978")
    src, num = match.group(1), int(match.group(2))
    srcs = Sources()
    board_names = _board_names(conn)
    if src == "tool":
        row = conn.execute(
            "SELECT 'tool' src,0 rk,id pk,seen_at ts,session_key,citizen_id,project,"
            "tool_name,action,path,path_dir,path_abs,ok,NULL etype,NULL payload,"
            "NULL actor,NULL subject,NULL channel,NULL reaction,NULL latency_sec,"
            "NULL code,NULL board_id,NULL post_kind,NULL post_text,args_digest "
            "FROM tool_activity WHERE id=?", (num,)).fetchone()
        table = "tool_activity"
    elif src == "post":
        row = conn.execute(
            "SELECT 'post' src,1 rk,id pk,created_at ts,session_key,citizen_id,"
            + post_project_sql() + " project,"
            "NULL tool_name,NULL action,NULL path,NULL path_dir,NULL path_abs,"
            "NULL ok,NULL etype,NULL payload,NULL actor,NULL subject,NULL channel,"
            "NULL reaction,NULL latency_sec,NULL code,board_id,kind post_kind,text post_text,"
            "NULL args_digest FROM board_posts WHERE id=?", (num,)).fetchone()
        table = "board_posts"
    elif src == "push":
        row = conn.execute(
            "SELECT 'push' src,2 rk,id pk,pushed_at ts,session_key,citizen_id,project,"
            "NULL tool_name,NULL action,NULL path,NULL path_dir,NULL path_abs,NULL ok,"
            "NULL etype,NULL payload,NULL actor,NULL subject,channel,reaction,latency_sec,"
            "code,NULL board_id,NULL post_kind,NULL post_text,NULL args_digest "
            "FROM push_log WHERE id=?", (num,)).fetchone()
        table = "push_log"
    else:
        row = conn.execute(
            "SELECT 'event' src,3 rk,seq pk,ts,"
            "COALESCE(json_extract(COALESCE(payload,'{}'),'$.session_key'),"
            " json_extract(COALESCE(actor,'{}'),'$.session_key')) session_key,"
            "json_extract(COALESCE(actor,'{}'),'$.citizen_id') citizen_id,"
            + EVENT_PROJECT_SQL + " project,"
            "NULL tool_name,NULL action,NULL path,NULL path_dir,NULL path_abs,NULL ok,"
            "type etype,payload,actor,subject,NULL channel,NULL reaction,NULL latency_sec,"
            "NULL code,NULL board_id,NULL post_kind,NULL post_text,NULL args_digest "
            "FROM events WHERE seq=?", (num,)).fetchone()
        table = "events"
    srcs.add(table, rows=1 if row else 0)
    if not row:
        return None

    labels = load_citizen_labels(conn, [row["citizen_id"]] if row["citizen_id"] else [])
    entry = make_item(row, labels, board_names, lineage_of(conn, [row["session_key"]]))
    citizen_id = row["citizen_id"] or ""

    # ── 这个人 ────────────────────────────────────────────────────
    citizen = conn.execute(
        "SELECT citizen_id, display_name, kind, role, host, status, created_at, last_seen_at "
        "FROM citizens_all WHERE citizen_id=?", (citizen_id,)).fetchone() if citizen_id else None
    session_key = row["session_key"] or ""
    presence = conn.execute(
        "SELECT board_id, status, doing, surface, auto_surface, last_seen_at, auto_source, "
        "session_surface FROM presence WHERE session_key=? ORDER BY last_seen_at DESC LIMIT 1",
        (session_key,)).fetchone() if session_key else None
    running = False
    if presence and presence["last_seen_at"]:
        try:
            seen = datetime.strptime(presence["last_seen_at"][:19], "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=timezone.utc)
            running = (now_dt() - seen).total_seconds() <= MOTION_WINDOW_MIN * 60
        except ValueError:
            running = False
    surface = (presence["surface"] or "").strip() if presence else ""
    auto_surface = (presence["auto_surface"] or "").strip() if presence else ""
    actor = {
        "label": entry["actor"]["name"],
        "labelKind": entry["actor"]["labelKind"],
        "citizenId": citizen_id or None,
        "sessionTag": entry["actor"]["sessionTag"],
        "host": entry["actor"]["host"],
        # 人类面标签（**只增键**，W6）：前缀 → 并列显示；`codebuddy` 前缀是
        # `"WorkBuddy/CodeBuddy"`（不猜——见 `host_label_for_protocol` 的坑）。
        "hostLabel": host_label_for_protocol(entry["actor"]["host"]),
        # 宿主取会话键前缀的可见说明（前端在详情里以 hover ⓘ 展示，正文不占位）
        "hostBasis": "按会话键前缀判定",
        "citizen": ({
            "displayName": citizen["display_name"],
            "kind": citizen["kind"], "role": citizen["role"],
            "host": (citizen["host"] or "").strip().lower() or None,
            # 自报面的**机器归一键**（W6，只增键）：alias → 规范 id；认不出的原样小写；
            # `unknown`/`n/a` 等哨兵 → ""。旧键 `host` 是原始自报值，一个字节不动。
            "hostCanonical": fish_hosts.canonical_host(citizen["host"]),
            # 自报面的**人类面标签**（W6，只增键）：保留原始写法；认不出 → "未登记宿主 <原值>"。
            "hostLabel": fish_hosts.host_label(citizen["host"]),
            "status": citizen["status"], "createdAt": citizen["created_at"],
            "lastSeenAt": citizen["last_seen_at"],
        } if citizen else None),
        "session": ({
            "present": True,
            "running": running,
            "boardId": presence["board_id"],
            "status": presence["status"],
            "doing": presence["doing"] or "",
            "autoSource": presence["auto_source"],
            "surface": surface,
            "autoSurface": auto_surface,
            "surfaceMismatch": bool(surface and auto_surface and surface != auto_surface),
            "lastSeenAt": presence["last_seen_at"],
        } if presence else {"present": False, "running": False}),
    }

    # ── 他的账本（两段分列，不合并计数）────────────────────────────
    if citizen_id:
        tool_rows = conn.execute(
            "SELECT id, tool_name, action, path_dir, path_abs, path, ok, seen_at, project, "
            "session_key FROM tool_activity WHERE citizen_id=? "
            "ORDER BY seen_at DESC, id DESC LIMIT 10", (citizen_id,)).fetchall()
        ev_rows = conn.execute(
            "SELECT seq, ts, type, subject FROM events "
            "WHERE json_extract(COALESCE(actor,'{}'),'$.citizen_id')=? "
            "ORDER BY seq DESC LIMIT 10", (citizen_id,)).fetchall()
        day_rows = conn.execute(
            "SELECT substr(seen_at,1,10) AS d, COUNT(*) AS n FROM tool_activity "
            "WHERE citizen_id=? AND seen_at>=? GROUP BY d ORDER BY d DESC LIMIT 5",
            (citizen_id, iso(now_dt() - timedelta(days=5)))).fetchall()
        post_count = conn.execute(
            "SELECT COUNT(*) FROM board_posts WHERE citizen_id=?", (citizen_id,)).fetchone()[0]
    else:
        tool_rows = conn.execute(
            "SELECT id, tool_name, action, path_dir, path_abs, path, ok, seen_at, project, "
            "session_key FROM tool_activity WHERE session_key=? "
            "ORDER BY seen_at DESC, id DESC LIMIT 10", (session_key,)).fetchall()
        ev_rows = []
        day_rows = []
        post_count = 0
    srcs.add("tool_activity,events,board_posts", rows=len(tool_rows) + len(ev_rows))

    ledger = {
        "tools": [{
            "key": f"tool:{int(r['id'])}", "toolName": r["tool_name"] or "",
            "action": r["action"] or "", "pathLabel": path_label(r),
            "ok": None if r["ok"] is None else bool(r["ok"]), "ts": r["seen_at"],
            "project": r["project"] or None, "sessionTag": session_tag(r["session_key"]),
        } for r in tool_rows],
        "events": [{
            "key": f"event:{int(r['seq'])}", "type": r["type"] or "", "ts": r["ts"],
            # **只在 subject 是记忆时才回 id**：`session.opened` 的 subject.id 是 26 位
            # 公民 ULID，人类面永不出现它（fish_naming 铁律）——宁可留空，也不印机器码。
            "code": (str(json_obj(r["subject"]).get("id") or "")
                     if json_obj(r["subject"]).get("kind") == "memory" else ""),
        } for r in ev_rows],
        "days": [{"date": r["d"], "count": int(r["n"])} for r in day_rows],
        "postCount": int(post_count or 0),
    }

    related = {"project": entry["project"],
               "board": None, "code": entry["meta"].get("code") or None}
    if entry["src"] == "post":
        related["board"] = {"id": entry["meta"].get("boardId"),
                            "label": entry["meta"].get("boardName")}

    state = "ok"
    return {
        "data": {"entry": entry, "actor": actor, "ledger": ledger, "related": related},
        "sources": srcs.as_list(),
        "state": state,
    }


# ── 端点的可复用入口（HTTP 与启动预热走**同一条**路径）─────────────────
def _missing_envelope(detail: str, as_of: str) -> dict:
    """「缺」是能力缺失，不是空：用 `availability.state="missing"` + 原文原因回答，
    **绝不用空数组/0 冒充**（96 §七共同纪律、97 §D5）。"""
    return {
        "schema": SCHEMA,
        "asOf": as_of,
        "availability": {"state": "missing", "detail": detail},
        "data": None,
        "sources": [{"source": "db", "ok": False, "rows": None, "error": detail, "ms": None}],
    }


def _guard_tables(conn: sqlite3.Connection, tables) -> str | None:
    missing = [name for name in tables if not table_exists(conn, name)]
    return ("缺表：" + "、".join(missing)) if missing else None


STREAM_TABLES = ("tool_activity", "board_posts", "push_log", "events", "presence", "boards",
                 # 记忆事件的项目归属要 join code_index（F-5）；缺它时如实报「缺表」，
                 # 而不是让 events 分支抛一个 "no such table" 的查询错。
                 "code_index")
STATS_TABLES = ("tool_activity", "board_posts", "push_log", "events", "presence",
                "project_stints", "citizens_all", "memory_access_log")


def api_facets(db_path: str, win: str) -> dict:
    now = now_dt()
    _, since = parse_window(win, now)
    key = ("facets", db_path, win)
    cached = cache_get("facets", key)
    if cached is None:
        conn = open_conn(db_path)
        try:
            problem = _guard_tables(conn, STREAM_TABLES)
            if problem:
                return _missing_envelope(problem, iso(now))
            result = collect_facets(conn, win=win, since=since)
        except sqlite3.Error as exc:
            return _missing_envelope(f"查询失败：{type(exc).__name__}: {exc}", iso(now))
        finally:
            conn.close()
        state = result.pop("state", "ok")
        cached = {
            "payload": envelope(result["data"], as_of=iso(now),
                                availability={"state": state, "detail": None},
                                sources=result["sources"]),
            "state": state,
        }
        cache_put("facets", key, cached)
    payload = cached["payload"]
    payload["asOf"] = iso(now)
    return payload


def api_stream(db_path: str, *, scope_kind: str, project: str | None,
               board_id: int | None, win: str, limit: int,
               num_cursor: str | None, direction: str,
               since_fixed: str | None = None) -> dict:
    """`/api/stream` 的实现。

    `since_fixed`（HTTP 参数名 `wsince`）= **页面固定窗口起点**（R-01 残留的修法）。
    不传就按 `win` 现算（`now - 24h` 这种滑窗）；传了就整段沿用调用方第一次拿到的
    `window.since`。为什么必须能固定：滑窗下第 1 页与第 24 页的 `since` 差几秒，
    末端的行会滑出窗口，「本刀 N 条」与可达行数于是差 1–2 行——**不是丢行，是窗口在动**。
    固定它之后计数缓存键（键里含 since）在整段翻页里稳定，缓存也能真正命中。
    """
    now = now_dt()
    _, since = parse_window(win, now)
    if since_fixed:
        since = since_fixed
    conn = open_conn(db_path)
    try:
        problem = _guard_tables(conn, STREAM_TABLES)
        if problem:
            return _missing_envelope(problem, iso(now))
        result = collect_stream(
            conn, scope_kind=scope_kind, project=project, board_id=board_id,
            win=win, since=since, limit=limit, num_cursor=num_cursor,
            direction=direction)
    except sqlite3.Error as exc:
        return _missing_envelope(f"查询失败：{type(exc).__name__}: {exc}", iso(now))
    finally:
        conn.close()
    total = result["data"]["counts"]["total"]
    state = "empty" if total == 0 else "ok"
    return envelope(result["data"], as_of=iso(now),
                    availability={"state": state, "detail": None},
                    sources=result["sources"])


def api_stats(db_path: str, *, days: int, top: int) -> dict:
    now = now_dt()
    key = ("stats", db_path, days, top)
    cached = cache_get("stats", key)
    if cached is None:
        conn = open_conn(db_path)
        try:
            problem = _guard_tables(conn, STATS_TABLES)
            if problem:
                return _missing_envelope(problem, iso(now))
            result = collect_stats(conn, days=days, top=top)
        except sqlite3.Error as exc:
            return _missing_envelope(f"查询失败：{type(exc).__name__}: {exc}", iso(now))
        finally:
            conn.close()
        state = result.pop("state", "ok")
        # `partial` 已删（QA F-8：没有可构造输入，口径未定）。这里只保留 ok/empty 两种，
        # 源级故障一律走 `_missing_envelope` 的 `missing`。
        cached = {
            "payload": envelope(result["data"], as_of=iso(now),
                                availability={"state": state, "detail": None},
                                sources=result["sources"]),
            "state": state,
        }
        cache_put("stats", key, cached)
    payload = cached["payload"]
    payload["asOf"] = iso(now)
    return payload


def api_entry(db_path: str, key: str) -> dict | None:
    now = now_dt()
    cache_key = ("entry", db_path, key)
    cached = cache_get("entry", cache_key)
    if cached is None:
        conn = open_conn(db_path)
        try:
            problem = _guard_tables(conn, STREAM_TABLES + ("citizens_all",))
            if problem:
                return _missing_envelope(problem, iso(now))
            result = collect_entry(conn, key)
        except sqlite3.Error as exc:
            return _missing_envelope(f"查询失败：{type(exc).__name__}: {exc}", iso(now))
        finally:
            conn.close()
        if result is None:
            return None
        cached = {"payload": envelope(result["data"], as_of=iso(now),
                                      availability={"state": result.get("state", "ok"),
                                                    "detail": None},
                                      sources=result["sources"])}
        cache_put("entry", cache_key, cached)
    payload = cached["payload"]
    payload["asOf"] = iso(now)
    return payload


def api_meta(db_path: str) -> dict:
    """`/api/meta`（R-09）：读口自述——**看这屏之前先能确认自己在看什么**。

    为什么单开一个端点而不是塞进 `/api/health`：health 是探活（轻、无参数、给脚本轮询用），
    meta 是契约自述（schema 名 + 能力清单 + 库位置 + 进程号），两者的读者与更新频率都不同。
    `capabilities` 列的是**这台读口实现了哪些端点**（不是"将来会有"），所以它非空本身就是判据。

    边界（如实写）：`db.corpus` 只回**路径或 null**，不开、不读、不校验内容——语料库是另一份
    只读文件，本期界面不读它（文件头「边界」第一条）。
    """
    now = now_dt()
    index_path = os.path.abspath(db_path)
    corpus_path = os.path.join(os.path.dirname(index_path), "corpus.sqlite")
    return {
        "schema": SCHEMA,
        "schemas": [SCHEMA],
        "capabilities": sorted(API_CAPABILITIES),
        "db": {"index": index_path,
               "corpus": corpus_path if os.path.exists(corpus_path) else None},
        "readOnly": True,
        "serverPid": os.getpid(),
        "asOf": iso(now),
    }


# ── 宿主接入层（设置页的读口）：**短 TTL 缓存，不落库、不写盘** ────────────
def api_hosts(db_path: str, host_id: str | None = None, *,
              host: str | None = None) -> dict:
    """`GET /api/hosts`：只列**探测到安装痕迹**的宿主，按注册表顺序。

    三条口径写死在这里：
      1. **短 TTL 缓存（3 秒）**：探测结果是「当下」，但一次列表要探 5 个宿主
         （实测 689–753 ms）；3 秒只挡住连点与并发读，又不会把「刚卸载」藏成分钟级。
         `POST /api/hosts/<id>/refresh` 会清空这一档，强制下一次列表重探。
         缓存键含 `db_path` 与 `host` 过滤参数——过滤后的子集不能复用全量读数。
      2. **只读**：`fish_hosts` 里没有一处写文件；这个端点也不碰库（只读连接查最后活跃）；
      3. **列表口径 = installed**：装了但没接 fish 的宿主也要出现（空心点那档），
         否则用户永远看不到「这台机器上还有谁没接」。
    """
    # `host` 是 HTTP 查询参数名；`host_id` 是内部调用名。`all` 等同不过滤。
    filter_id = (host_id or host or "").strip().lower() or None
    if filter_id == "all":
        filter_id = None
    cache_key = (os.path.abspath(db_path), filter_id or "")
    cached = cache_get("hosts", cache_key)
    if cached is not None:
        log("cache_hit", scope="hosts", host=filter_id or "all")
        return cached
    started = time.perf_counter()
    now = now_dt()
    items = fish_hosts.installed_statuses(db_path=db_path)
    if filter_id is not None:
        items = [item for item in items if str(item.get("id") or "") == filter_id]
    consistency = fish_hosts.consistency()
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    with_seen = sum(1 for item in items if item["lastSeenAt"])
    sources = [
        {"source": "hosts.files", "ok": True, "rows": len(items), "error": None,
         "ms": elapsed_ms},
        {
            "source": "hosts.db",
            "ok": os.path.isfile(db_path),
            "rows": with_seen,
            "error": None if os.path.isfile(db_path) else f"库不存在：{db_path}",
            "ms": elapsed_ms,
        },
    ]
    payload = envelope(
        {"items": items, "generatedAt": iso(now), "consistency": consistency},
        as_of=iso(now),
        availability={"state": "ok" if items else "empty",
                      "detail": None if items else "本机没有探测到任何已安装的宿主"},
        sources=sources,
    )
    cache_put("hosts", cache_key, payload)
    return payload


def api_host(db_path: str, host_id: str, *, refresh: bool = False) -> dict | None:
    """`GET /api/hosts/<id>`（也用于 `POST .../refresh`）：单个宿主的详情，**每次现探**。

    注意：**refresh 与 GET 详情是同一条实现**——本读口本来就不缓存单个宿主的探测结果，
    所以「刷新」不需要另一条代码路径（写两条只会让两条慢慢漂开）。动作面显式的意义在
    HTTP 层：刷新是 `POST`，它明确表示「允许重探」，而 GET 只是读。

    `refresh=True` 时额外清空 `hosts` 列表缓存：列表是 3 秒 TTL 的批量探测，
    刚点过「刷新」的用户不该在下一屏看到刷新前的缓存（P2-4）。
    """
    started = time.perf_counter()
    now = now_dt()
    if host_id not in fish_hosts.REGISTRY:
        return None
    item = fish_hosts.detect(host_id, db_path=db_path)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    if refresh:
        cleared = cache_invalidate("hosts")
        log("cache_invalidate", scope="hosts", host=host_id, entries=cleared)
    return envelope(
        item,
        as_of=iso(now),
        availability={"state": "ok", "detail": None},
        sources=[
            {"source": "hosts.files", "ok": True, "rows": len(item["configs"]),
             "error": None, "ms": elapsed_ms},
            {"source": "hosts.db", "ok": os.path.isfile(db_path),
             "rows": len(item["lastSeenSources"]),
             "error": None if os.path.isfile(db_path) else f"库不存在：{db_path}",
             "ms": elapsed_ms},
        ],
    )


# ── 推送面板（设置页第二项）：只读诊断 + 两个窄写动作 ────────────────────
#: 写口的两条路径（白名单/范围判定在 `fish_push_audit` 里，HTTP 只做映射）。
PUSH_CONFIG_PATH = "/api/push/config"
PUSH_PRESET_PATH = "/api/push/preset"
#: 请求体上限（与宿主 refresh 那条同一个数：配置写入不该超过 64 KB）。
BODY_MAX_BYTES = 65536


def push_envelope(data: dict, *, as_of: str, sources: list) -> dict:
    """推送端点的信封：在只读信封之上多一个 `pid`（写口要能自证「这个进程改的」）。"""
    return {
        "schema": SCHEMA,
        "asOf": as_of,
        "availability": {"state": "ok", "detail": None},
        "data": data,
        "sources": sources,
        "pid": os.getpid(),
    }


def api_push_overview(db_path: str, days: int) -> dict:
    """`GET /api/push/overview`：诊断概览（`totals` 来自 `fish.push_stats`，不重算）。

    P2-4：一次概览实测 ~797 ms，10 秒 TTL 挡住设置页的连续打开；
    `POST /api/push/config|preset` 写完配置后调用 `cache_invalidate("push_overview")`，
    保证「改了值 → 立刻看到新读数」。
    """
    cache_key = (os.path.abspath(db_path), int(days))
    cached = cache_get("push_overview", cache_key)
    if cached is not None:
        log("cache_hit", scope="push_overview", days=days)
        return cached
    data = fish_push_audit.overview(db_path, days=days)
    sources = data.pop("sources", [])
    payload = push_envelope(data, as_of=data.get("asOf") or iso(now_dt()), sources=sources)
    cache_put("push_overview", cache_key, payload)
    return payload


def api_push_events(db_path: str, *, limit: int, channel: str | None,
                    reaction: str | None) -> dict:
    """`GET /api/push/events`：最近推送事件（按 id 倒序，可按通道/结果过滤）。"""
    data = fish_push_audit.events(db_path, limit=limit, channel=channel, reaction=reaction)
    sources = data.pop("sources", [])
    return push_envelope(data, as_of=data.get("asOf") or iso(now_dt()), sources=sources)


def push_audit_source(audit_at, changed: bool, *, rows: int = 1,
                      unchanged_detail: str = "值未变化，未写库、未写审计") -> dict:
    """写口回执里「审计」那一项：**没改 ≠ 失败**，三种情形必须分开。

    判据只能是 `changed`，不能是 `auditAt is not None`——后者把两件事压成了一个 null：

      · `changed=True` 且审计有值   → `ok=true`（证据齐）；
      · `changed=True` 但审计没写成 → `ok=false`（配置已生效、证据缺一行，要出声）；
      · `changed=False`（同值短路） → **什么都没写，也就不需要审计** → `ok=true`。

    第三种给的是 `rows=0`（真没写进审计的 0 行，不是 1）+ `detail` 一句人话；
    前两种 `detail=None`（原因在 `error` 里）。前端据此区分「没变」与「失败」。
    """
    if not changed:
        return {"source": "push-config-audit", "ok": True, "rows": 0, "error": None,
                "ms": None, "detail": unchanged_detail}
    if audit_at:
        return {"source": "push-config-audit", "ok": True, "rows": rows, "error": None,
                "ms": None, "detail": None}
    return {"source": "push-config-audit", "ok": False, "rows": rows,
            "error": "审计写失败（配置已生效；看服务端 stderr）", "ms": None,
            "detail": None}


def api_push_write_config(db_path: str, key, value) -> dict:
    """`POST /api/push/config`：写**一个**白名单键（审计落在 `_audit/push-config.jsonl`）。"""
    data = fish_push_audit.write_config(db_path, key, value)
    log("push_config_written", key=data["key"], old=data["old"], new=data["new"],
        changed=data["changed"], audit=data["auditAt"])
    cleared = cache_invalidate("push_overview")
    log("cache_invalidate", scope="push_overview", reason="push_config", entries=cleared)
    return push_envelope(data, as_of=iso(now_dt()), sources=[
        {"source": "config", "ok": True, "rows": 1 if data["changed"] else 0,
         "error": None, "ms": None},
        push_audit_source(data["auditAt"], data["changed"]),
    ])


def api_push_apply_preset(db_path: str, preset) -> dict:
    """`POST /api/push/preset`：止血 / 恢复（一次开一个连接，逐键校验与审计）。"""
    data = fish_push_audit.apply_preset(db_path, preset)
    log("push_preset_applied", preset=data["preset"], changed=data["changedCount"],
        audit=data["auditAt"])
    cleared = cache_invalidate("push_overview")
    log("cache_invalidate", scope="push_overview", reason="push_preset", entries=cleared)
    return push_envelope(data, as_of=iso(now_dt()), sources=[
        {"source": "config", "ok": True, "rows": data["changedCount"], "error": None,
         "ms": None},
        # 重复应用同一个预设（如连点两次「恢复」）→ `changedCount=0` → 没写库也没写审计，
        # 那是「已经就是这样」，不是审计失败。
        push_audit_source(data["auditAt"], data["changedCount"] > 0,
                          rows=data["changedCount"],
                          unchanged_detail="值未变化，未写库、未写审计"
                                           "（本次预设的键现场值已经就是这样）"),
    ])


def _read_json_body(handler: "Handler") -> dict:
    """读请求体并解析成 JSON object；非 JSON / 非 object 一律 `ValueError`。

    为什么在这里做而不是在各自的动作里：两条写口的要求一字不差（Content-Length ≤ 64 KB、
    body 必须是 JSON object），拆成两份就迟早只改一处。
    """
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError as exc:
        raise ValueError("Content-Length 不是数字") from exc
    if length > BODY_MAX_BYTES:
        raise OverflowError(f"请求体 {length} 字节，超过 {BODY_MAX_BYTES} 字节上限")
    raw = handler.rfile.read(length) if length else b""
    if not raw:
        raise ValueError("请求体是空的：要一个 JSON object")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"请求体不是 UTF-8 JSON：{exc}") from exc
    if not isinstance(body, dict):
        raise ValueError("请求体必须是 JSON object，例如 {\"key\":…,\"value\":…}")
    return body


def warm_up(db_path: str) -> None:
    """启动后后台预热：把 stats（含 `import fish` 的固定成本）与 facets 先算一遍。

    为什么必须有：整页首屏要求 facets+stream+stats 三请求 < 1 s，而 `collect_stats`
    第一次调用要 import `fish`（模块 2 s 级）并跑 `push_stats`。这个成本属于**进程启动**，
    不该让用户读屏时等——挪到预热线程里，首屏就只剩查库的时间。
    """
    started = time.perf_counter()
    try:
        api_facets(db_path, "24h")
        api_stats(db_path, days=1, top=5)
        api_stream(db_path, scope_kind="all", project=None, board_id=None,
                   win="24h", limit=LIMIT_DEFAULT, num_cursor=None, direction="older")
    except Exception as exc:  # noqa: BLE001  预热失败不影响服务，只记录
        log("warmup_failed", error=f"{type(exc).__name__}: {exc}")
        return
    log("warmed", ms=round((time.perf_counter() - started) * 1000, 1))



# ── 静态文件（生产：读口同源发 ui/dist）─────────────────────────────────
def resolve_static(dist_dir: str, url_path: str) -> tuple[str | None, int]:
    """把 URL 路径解析成 dist 里的真实文件；`(path, status)`。

    安全：`realpath` 之后必须仍在 dist 前缀里——`/assets/../../fish_ui.py` 之类一律 404。
    """
    root = os.path.realpath(dist_dir)
    rel = unquote(url_path).lstrip("/") or "index.html"
    candidate = os.path.realpath(os.path.join(root, rel))
    if candidate != root and not candidate.startswith(root + os.sep):
        return None, 404
    if os.path.isfile(candidate):
        return candidate, 200
    if "." not in os.path.basename(rel):
        index = os.path.join(root, "index.html")
        if os.path.isfile(index):
            return os.path.realpath(index), 200
    return None, 404


CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}

_ORIGIN_RE = re.compile(r"^http://(127\.0\.0\.1|localhost)(:\d+)?$")
#: 唯一的写语义路径（**不写盘**：重探一次宿主，返回同一份结构）。GET 详情与它共用实现。
REFRESH_PATH_RE = re.compile(r"^/api/hosts/([a-z][a-z0-9-]{1,31})/refresh$")


# ── HTTP 层 ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "fish-workbench/1"
    db_path = ""
    dist_dir: str | None = None
    #: 写口开不开：`main()` 按「绑的地址是不是回环 / 有没有 --allow-remote-write」定一次。
    write_allowed = True
    #: 本进程绑的地址（409 文案要说清「绑的是什么」，不靠请求里的 Host 猜）。
    bind_host = DEFAULT_HOST
    semaphore = threading.BoundedSemaphore(8)

    # 安静一点：只走我们自己的结构化日志（一行 JSON 到 stderr）
    def log_message(self, fmt, *args):
        return

    # ── 响应工具 ────────────────────────────────────────────────
    def _origin_allowed(self) -> tuple[bool, str]:
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return True, ""                       # 同源导航 / curl：没有 Origin 头
        if origin == "null":
            return False, ""                      # file:// 之类，一律拒
        if _ORIGIN_RE.match(origin):
            return True, origin
        return False, ""

    def _send_json(self, code: int, payload: dict, *, allow_origin: str = "") -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if allow_origin:
            self.send_header("Access-Control-Allow-Origin", allow_origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, code: str, hint: str, action: str) -> None:
        self._send_json(status, error_payload(code, hint, action))

    def _dist_hint(self) -> None:
        body = (
            "<!doctype html><meta charset='utf-8'><title>fish 工作台</title>"
            "<body style='font:16px/1.6 sans-serif;padding:32px;max-width:640px'>"
            "<h1 style='font-size:20px'>还没有构建产物</h1>"
            "<p>这个读口没有在 <code>--dist</code> 指向的目录里找到前端文件。</p>"
            "<p>开发时请另开一个窗口跑 <code>npm run dev</code>（Vite 会把 /api 代理到这里）；"
            "要看同源产物就先在 <code>ui/</code> 里跑 <code>npm run build</code>，"
            "再用 <code>--dist ui/dist</code> 启动本服务。</p></body>"
        ).encode("utf-8")
        self._send_bytes(200, body, "text/html; charset=utf-8")

    # ── 路由 ────────────────────────────────────────────────────
    def do_OPTIONS(self):  # noqa: N802
        allowed, origin = self._origin_allowed()
        if not allowed:
            self._send_error(403, "origin_rejected",
                             "只接受来自 http://127.0.0.1 或 http://localhost 的请求",
                             "从本机页面或 curl 访问；Origin: null 会被拒绝")
            return
        self.send_response(204)
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def _reject_non_get(self):  # noqa: N802
        """除三条 POST 外**一律拒写**（PUT/PATCH/DELETE 没有出路，将来也不加）。

        三条 POST 的语义各不相同，都不是「通用写口」：
          · `POST /api/hosts/<id>/refresh` —— 重探一次，**不写盘**；
          · `POST /api/push/config`        —— 写一个白名单配置键（审计）；
          · `POST /api/push/preset`        —— 止血/恢复（同上，另一个粒度）。
        """
        self._send_error(405, "method_not_allowed",
                         "这个读口默认只读：GET 读，写只有 POST /api/push/config 与 "
                         "POST /api/push/preset（白名单 + 审计），以及不写盘的 "
                         "POST /api/hosts/<id>/refresh",
                         "改用 GET；写配置用 POST /api/push/config 或 /api/push/preset")

    do_PUT = _reject_non_get       # noqa: N815
    do_PATCH = _reject_non_get     # noqa: N815
    do_DELETE = _reject_non_get    # noqa: N815

    def do_POST(self):  # noqa: N802
        """三条 POST：宿主重探（不写盘）+ 推送配置的窄写口（白名单 + 审计）。

        为什么「刷新宿主」也走 POST：设置页的「刷新」是一个**允许发生的动作**，用 GET
        表达会让人以为刷几次都一样；用 POST 把「这次是新证据」写进 HTTP 语义里。

        为什么写配置也走 POST 而不是 PUT/PATCH：本读口**默认只读**，写是一个例外；
        例外越少越好。两条写口都在 `fish_push_audit` 里做白名单/范围/审计，HTTP 层只做
        错误映射（`BadKey`→400 bad_key、`BadValue`→400 bad_value、其余→500）。
        """
        started = time.perf_counter()
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        status = 200
        try:
            allowed, origin = self._origin_allowed()
            if not allowed:
                status = 403
                self._send_error(403, "origin_rejected",
                                 "只接受来自 http://127.0.0.1 或 http://localhost 的请求",
                                 "从本机页面或 curl 访问；Origin: null 会被拒绝")
                return
            if path in (PUSH_CONFIG_PATH, PUSH_PRESET_PATH):
                # 写口只认回环（或显式 --allow-remote-write）：绑在 0.0.0.0 上还让人远程写配置，
                # 等于把这台机器的推送行为交给整个局域网（评审 🟡-3）。
                if not self.write_allowed:
                    status = 403
                    self._send_error(
                        403, "write_requires_loopback",
                        f"写口只在回环上开放（127.0.0.1 / localhost / ::1）；"
                        f"本进程绑的是 {self.bind_host}，且没有 --allow-remote-write",
                        "读端点照常可用；真要从别的地址写，重启读口时加 --allow-remote-write")
                    return
                status = self._post_push(path, origin)
                return
            match = REFRESH_PATH_RE.fullmatch(path)
            if not match:
                status = 405
                self._reject_non_get()
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > 65536:
                status = 413
                self._send_error(413, "payload_too_large", "刷新不需要请求体",
                                 "别带 body：刷新只是重探一次")
                return
            if length:
                self.rfile.read(length)   # 读完，避免 keep-alive 下串包（语义上忽略）
            host_id = match.group(1)
            if host_id not in fish_hosts.REGISTRY:
                status = 404
                self._send_error(404, "unknown_host", f"未登记的宿主 id：{host_id}",
                                 f"可用：{', '.join(fish_hosts.REGISTRY)}")
                return
            if not self.semaphore.acquire(blocking=False):
                status = 503
                self._send_error(503, "busy", "并发连接已满（上限 8）", "等一秒再重试")
                return
            try:
                payload = api_host(self.db_path, host_id, refresh=True)
            finally:
                self.semaphore.release()
            self._send_json(200, payload, allow_origin=origin)
        except BrokenPipeError:
            status = 499
        except Exception as exc:  # noqa: BLE001  读口出错要说出来，不静默
            status = 500
            log("request_error", path=path, method="POST",
                error=f"{type(exc).__name__}: {exc}")
            try:
                self._send_error(500, "internal_error", f"{type(exc).__name__}: {exc}",
                                 "看服务端 stderr 的那行 JSON 日志；这是读口的缺陷")
            except OSError:
                pass  # A档：客户端已断开（回写注定再失败一次），真正的错误已由上面的 log(request_error) 落盘
        finally:
            log("request", path=path, method="POST", status=status,
                ms=round((time.perf_counter() - started) * 1000, 2))

    def _post_push(self, path: str, origin: str) -> int:
        """两条推送写口的公共前半段：读 body → 取参数 → 调数据面 → 映射错误。

        错误映射写死在这里（与任务书一致）：未知/不可写键 → 400 `bad_key`；值非法/越界 →
        400 `bad_value`；库写失败 → 500 `internal_error`；body 不是 JSON object →
        400 `bad_json`；body 超过 64 KB → 413。
        """
        try:
            body = _read_json_body(self)
        except OverflowError as exc:
            self._send_error(413, "payload_too_large", str(exc),
                             "配置写入的请求体只该有一个键值对")
            return 413
        except ValueError as exc:
            self._send_error(400, "bad_json", str(exc),
                             'body 形如 {"key":"push.open_budget_tokens","value":600} '
                             '或 {"preset":"pause"}')
            return 400

        if not self.semaphore.acquire(blocking=False):
            self._send_error(503, "busy", "并发连接已满（上限 8）", "等一秒再重试")
            return 503
        try:
            if path == PUSH_CONFIG_PATH:
                if "key" not in body:
                    self._send_error(400, "bad_key", "缺 key：要写哪个配置项？",
                                     f"可写：{', '.join(fish_push_audit.WRITABLE_KEYS)}")
                    return 400
                if "value" not in body:
                    self._send_error(400, "bad_value", "缺 value：要写成什么？",
                                     'body 形如 {"key":"…","value":20}')
                    return 400
                payload = api_push_write_config(self.db_path, body["key"], body["value"])
            else:
                if "preset" not in body:
                    self._send_error(400, "bad_value", "缺 preset：要哪一个？",
                                     'body 形如 {"preset":"pause"}')
                    return 400
                payload = api_push_apply_preset(self.db_path, body["preset"])
        except fish_push_audit.BadKey as exc:
            self._send_error(400, "bad_key", str(exc),
                             f"可写：{', '.join(fish_push_audit.WRITABLE_KEYS)}")
            return 400
        except fish_push_audit.BadValue as exc:
            self._send_error(400, "bad_value", str(exc),
                             "看 GET /api/push/overview 的 config[].type/min/max")
            return 400
        except ValueError as exc:
            # 兜底：数据面里其它 ValueError 也都是「你的输入不对」，不是 500。
            self._send_error(400, "bad_value", str(exc),
                             "看 GET /api/push/overview 的 config[].type/min/max")
            return 400
        except (sqlite3.Error, OSError) as exc:
            log("push_write_failed", path=path, error=f"{type(exc).__name__}: {exc}")
            self._send_error(500, "internal_error", f"{type(exc).__name__}: {exc}",
                             "看服务端 stderr；库可能被别的进程长时间占着")
            return 500
        finally:
            # 写口与读口共用同一个并发闸（上限 8）：**不释放**就等于每写一次漏一个槽，
            # 连写 8 次之后所有请求都 503 —— 那是「用着用着整屏坏了」。
            self.semaphore.release()
        self._send_json(200, payload, allow_origin=origin)
        return 200

    def do_GET(self):  # noqa: N802
        started = time.perf_counter()
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        status = 200
        try:
            allowed, origin = self._origin_allowed()
            if not allowed:
                status = 403
                self._send_error(403, "origin_rejected",
                                 "只接受来自 http://127.0.0.1 或 http://localhost 的请求",
                                 "从本机页面或 curl 访问；Origin: null 会被拒绝")
                return
            if path.startswith("/api/"):
                status = self._handle_api(path, query, origin)
            elif path in ("/", "/index.html"):
                self._serve_static("/")
            else:
                self._serve_static(parsed.path)
        except BrokenPipeError:
            status = 499
        except Exception as exc:  # noqa: BLE001  读口出错要说出来，不静默
            status = 500
            log("request_error", path=path, error=f"{type(exc).__name__}: {exc}")
            try:
                self._send_error(500, "internal_error", f"{type(exc).__name__}: {exc}",
                                 "看服务端 stderr 的那行 JSON 日志；这是读口的缺陷，不是你的输入问题")
            except OSError:
                pass  # A档：同上——客户端已断开，回写注定再失败一次；真错误已由 log(request_error) 落盘
        finally:
            log("request", path=path, status=status,
                ms=round((time.perf_counter() - started) * 1000, 2))

    # ── API ─────────────────────────────────────────────────────
    def _handle_api(self, path: str, query: dict, origin: str) -> int:
        if not self.semaphore.acquire(blocking=False):
            self._send_error(503, "busy", "并发连接已满（上限 8）",
                             "等一秒再重试；这一期没有连接池")
            return 503
        try:
            return self._dispatch(path, query, origin)
        finally:
            self.semaphore.release()

    def _dispatch(self, path: str, query: dict, origin: str) -> int:
        now = now_dt()
        as_of = iso(now)

        if path == "/api/health":
            conn = open_conn(self.db_path)
            counts: dict = {}
            missing = False
            try:
                for table in ("tool_activity", "board_posts", "push_log", "events"):
                    try:
                        counts[table] = conn.execute(
                            f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    except sqlite3.Error:
                        counts[table] = None
                        missing = True
            finally:
                conn.close()
            self._send_json(200, {
                "schema": SCHEMA, "asOf": as_of, "ok": not missing,
                "availability": {"state": "missing" if missing else "ok",
                                 "detail": "缺表：读口起来了，但库里有表不存在" if missing else None},
                "data": {"tables": counts,
                         "fold": {"thresholdSec": FOLD_SEC, "minItems": FOLD_MIN,
                                  "push": {"enabled": FOLD_PUSH,
                                           "gapSec": FOLD_PUSH_SEC}}},
                "sources": [], "pid": os.getpid(),
            }, allow_origin=origin)
            return 200

        if path == "/api/meta":
            self._send_json(200, api_meta(self.db_path), allow_origin=origin)
            return 200

        # ── 宿主接入层（设置页）：列表 / 详情；重探走 POST ──────────────
        if path == "/api/hosts":
            host_filter = (query.get("host") or [""])[0].strip() or None
            self._send_json(200, api_hosts(self.db_path, host_filter), allow_origin=origin)
            return 200

        host_match = re.fullmatch(r"/api/hosts/([a-z][a-z0-9-]{1,31})", path)
        if host_match:
            host_id = host_match.group(1)
            if host_id not in fish_hosts.REGISTRY:
                self._send_error(404, "unknown_host", f"未登记的宿主 id：{host_id}",
                                 f"可用：{', '.join(fish_hosts.REGISTRY)}")
                return 404
            self._send_json(200, api_host(self.db_path, host_id), allow_origin=origin)
            return 200

        if REFRESH_PATH_RE.fullmatch(path):
            # 刷新是 POST：GET 上不提供（否则「重探」与「读」会被混成一个动作）。
            self._send_error(405, "method_not_allowed",
                             "重新探测要用 POST（GET 只读，不该有「重探一次」的语义）",
                             f"POST {path}")
            return 405

        if path == "/api/facets":
            try:
                win, _ = parse_window((query.get("win") or ["24h"])[0], now)
            except ValueError as exc:
                self._send_error(400, "bad_window", str(exc), "win=24h / 7d / all")
                return 400
            self._send_json(200, api_facets(self.db_path, win), allow_origin=origin)
            return 200

        if path == "/api/stream":
            try:
                win, _ = parse_window((query.get("win") or ["24h"])[0], now)
                scope_kind, project, board_id = parse_scope(query)
                limit = parse_int(query, "limit", LIMIT_DEFAULT, 1, LIMIT_MAX)
            except ValueError as exc:
                self._send_error(400, "bad_param", str(exc),
                                 "win=24h|7d|all；scope=all|project:<名>|project:__none__|board:<id>；limit≤500")
                return 400
            cursor = (query.get("since") or [""])[0].strip() or None
            before = (query.get("before") or [""])[0].strip() or None
            num_cursor = cursor or before
            direction = "newer" if cursor else "older"
            if num_cursor and not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\|\d\|\d{12}",
                                               num_cursor):
                self._send_error(400, "bad_cursor", "游标形如 2026-11-23T12:52:44Z|9|000000040471",
                                 "用上一次响应里的 cursor/topCursor 原样回传")
                return 400
            # 页面固定窗口起点（R-01）：翻页时把第一页的 `window.since` 原样带回来，
            # 整段翻页就用同一个 since——不会因为窗口滑走而在末端少 1–2 行。
            since_fixed = (query.get("wsince") or [""])[0].strip() or None
            if since_fixed and not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
                                                since_fixed):
                self._send_error(400, "bad_param", "wsince 形如 2026-11-22T16:00:00Z",
                                 "用上一次响应里的 window.since 原样回传；不传就按 win 现算")
                return 400
            self._send_json(200, api_stream(
                self.db_path, scope_kind=scope_kind, project=project, board_id=board_id,
                win=win, limit=limit, num_cursor=num_cursor, direction=direction,
                since_fixed=since_fixed),
                allow_origin=origin)
            return 200

        if path == "/api/stats":
            try:
                days = parse_int(query, "days", 1, 1, 30)
                top = parse_int(query, "top", 5, 1, 20)
            except ValueError as exc:
                self._send_error(400, "bad_param", str(exc), "days=1..30；top=1..20")
                return 400
            self._send_json(200, api_stats(self.db_path, days=days, top=top),
                            allow_origin=origin)
            return 200

        if path == "/api/entry":
            key = (query.get("id") or query.get("key") or [""])[0].strip()
            if not key:
                self._send_error(400, "bad_param", "缺 id",
                                 "id=tool:40424 / post:443 / push:5967 / event:2978")
                return 400
            if not re.fullmatch(r"(tool|post|push|event):\d+", key):
                self._send_error(400, "bad_key",
                                 "id 形如 tool:40424 / post:443 / push:5967 / event:2978",
                                 "用流水里 item.key 原样回传")
                return 400
            payload = api_entry(self.db_path, key)
            if payload is None:
                self._send_error(404, "not_found", f"这条不在库里：{key}",
                                 "可能是被清理了；刷新流水后重新点一条")
                return 404
            self._send_json(200, payload, allow_origin=origin)
            return 200

        # ── 推送面板（设置页第二项）：读两个，写两个（写只认 POST）──────────
        if path == "/api/push/overview":
            try:
                days = parse_int(query, "days", fish_push_audit.DEFAULT_DAYS,
                                 fish_push_audit.DAYS_MIN, fish_push_audit.DAYS_MAX)
            except ValueError as exc:
                self._send_error(400, "bad_param", str(exc), "days=7|30")
                return 400
            self._send_json(200, api_push_overview(self.db_path, days), allow_origin=origin)
            return 200

        if path == "/api/push/events":
            try:
                limit = parse_int(query, "limit", fish_push_audit.EVENTS_LIMIT_DEFAULT, 1,
                                  fish_push_audit.EVENTS_LIMIT_MAX)
                channel = fish_push_audit.check_channel((query.get("channel") or [""])[0])
                # `reaction` **不传 = 不过滤**；`reaction=pending` = 只要「待判」（库里是 NULL）。
                # **空串不是契约**：`parse_qs` 默认把 `reaction=` 丢成「没有这个参数」，
                # 所以这个读口的「待判」过滤用显式哨兵 `pending`（评审 🟠-2）。
                raw_reaction = (query.get("reaction") or [None])[0]
                reaction = (None if raw_reaction is None
                            else fish_push_audit.check_reaction(raw_reaction))
            except ValueError as exc:
                self._send_error(400, "bad_param", str(exc),
                                 "limit≤100；channel=inject|prompt|tool_after；"
                                 "reaction=pending|accepted|ignored；不传=不过滤")
                return 400
            payload = api_push_events(self.db_path, limit=limit, channel=channel,
                                      reaction=reaction)
            self._send_json(200, payload, allow_origin=origin)
            return 200

        if path in (PUSH_CONFIG_PATH, PUSH_PRESET_PATH):
            # 写口**只认 POST**：GET 上给一个明确的 405，而不是 404——
            # 404 会让人以为"端点不存在"，于是去猜另一个路径名。
            self._send_error(405, "method_not_allowed",
                             "写配置要用 POST（GET 只读，不该有「改一个值」的语义）",
                             f"POST {path}  body={{\"key\":\"…\",\"value\":…}}"
                             if path == PUSH_CONFIG_PATH else
                             f"POST {path}  body={{\"preset\":\"pause\"|\"resume\"}}")
            return 405

        self._send_error(404, "not_found", f"没有这个端点：{path}",
                         "可用：/api/facets /api/stream /api/stats /api/entry /api/meta "
                         "/api/hosts /api/hosts/<id> /api/health "
                         "/api/push/overview /api/push/events（写：POST /api/push/config、"
                         "/api/push/preset）")
        return 404

    # ── 静态 ────────────────────────────────────────────────────
    def _serve_static(self, url_path: str) -> None:
        if not self.dist_dir or not os.path.isdir(self.dist_dir):
            self._dist_hint()
            return
        target, status = resolve_static(self.dist_dir, url_path)
        if status != 200 or not target:
            self._send_error(404, "not_found", f"静态文件不存在：{url_path}",
                             "先 cd ui 跑 npm run build，再用 --dist ui/dist 启动")
            return
        ext = os.path.splitext(target)[1].lower()
        with open(target, "rb") as handle:
            body = handle.read()
        self._send_bytes(200, body, CONTENT_TYPES.get(ext, "application/octet-stream"))


# ── 启动 ────────────────────────────────────────────────────────────────
def pick_db(explicit: str | None) -> str:
    if explicit:
        return explicit
    for candidate in _DB_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return _DB_CANDIDATES[0]


def selftest(db_path: str) -> int:
    """不起服务，跑四个端点并打印读数（人工/脚本对拍用）。"""
    global _CURRENT_DB
    _CURRENT_DB = db_path
    now = now_dt()
    win, since = parse_window("24h", now)
    out: dict = {}
    conn = open_conn(db_path)
    try:
        facets = collect_facets(conn, win=win, since=since)
        out["facets"] = {
            "projects": [(p["label"], p["count"]) for p in facets["data"]["projects"][:5]],
            "unattributed": (facets["data"]["unattributed"] or {}).get("count"),
            "boards": [(b["label"], b["memberCount"], b["postCount"], b["runningCount"])
                       for b in facets["data"]["boards"][:5]],
        }
        stream = collect_stream(conn, scope_kind="all", project=None, board_id=None,
                                win=win, since=since, limit=LIMIT_DEFAULT,
                                num_cursor=None, direction="older")
        items = stream["data"]["items"]
        out["stream"] = {
            "returned": len(items),
            "srcs": sorted({i["src"] for i in items}),
            "cursor": stream["data"]["cursor"],
            "fold": stream["data"]["fold"],
            "total": stream["data"]["counts"]["total"],
            "first3": [i["text"] for i in items[:3]],
        }
        stats = collect_stats(conn, days=1, top=5)
        out["stats"] = {
            "motion": stats["data"]["motion"],
            "openStints": {k: stats["data"]["openStints"][k] for k in ("total", "oldestDays")},
            "idleDeclared": {k: stats["data"]["idleDeclared"][k]
                             for k in ("auto", "declared", "unclear")},
            "push": {k: stats["data"]["push"][k]
                     for k in ("total", "acceptRate", "pending")},
            "tools": {"in24h": stats["data"]["tools"]["in24h"],
                      "cumulative": stats["data"]["tools"]["cumulative"]},
            "citizens": {k: stats["data"]["citizens"][k]
                         for k in ("total", "active24h")},
            "hotAccessWindow": stats["data"]["hotAccess"]["window"][:2],
        }
        if items:
            entry = collect_entry(conn, items[0]["key"])
            out["entry"] = {
                "key": items[0]["key"],
                "label": entry["data"]["actor"]["label"],
                "host": entry["data"]["actor"]["host"],
                "session": entry["data"]["actor"]["session"],
                "ledgerTools": len(entry["data"]["ledger"]["tools"]),
                "ledgerEvents": len(entry["data"]["ledger"]["events"]),
            }
    finally:
        conn.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fish_workbench_api.py",
        description="fish 工作台只读读口：五源合流 + 切片器 + 判断面（不给假数据）",
    )
    parser.add_argument("--db", default=None, help="主库路径（默认自动探测）")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help="监听地址（默认只绑回环；非回环时写口默认关闭）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="端口（默认 8790）")
    parser.add_argument("--dist", default=None, help="前端产物目录（如 ui/dist）；不给就只发 API")
    parser.add_argument("--allow-remote-write", action="store_true",
                        dest="allow_remote_write",
                        help="在非回环地址上也开放两个推送写端点（默认不开放：非回环时写口 403）")
    parser.add_argument("--selftest", action="store_true", help="不启服务，跑四个端点打印读数")
    args = parser.parse_args(argv)

    db_path = pick_db(args.db)
    if not os.path.exists(db_path):
        log("db_missing", path=db_path)
        print(f"[fish_workbench_api] 库不存在：{db_path}", file=sys.stderr)
        return 2

    if args.selftest:
        return selftest(db_path)

    global _CURRENT_DB
    _CURRENT_DB = db_path
    Handler.db_path = db_path
    Handler.dist_dir = os.path.abspath(args.dist) if args.dist else None
    Handler.bind_host = args.host
    # 写口闸：回环上默认开；非回环必须显式 --allow-remote-write（评审 🟡-3）。
    Handler.write_allowed = is_loopback(args.host) or args.allow_remote_write

    conn = open_conn(db_path)   # 启动即证明「只读」成立（写被拒绝）
    try:
        counts = {}
        for table in ("tool_activity", "board_posts", "push_log", "events"):
            try:
                counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error as exc:
                # 缺表不是启动失败的理由：读口要能起来并**如实回答「缺」**（见 _missing_envelope）。
                counts[table] = f"缺表：{type(exc).__name__}"
    finally:
        conn.close()
    log("started", host=args.host, port=args.port, db=db_path,
        dist=Handler.dist_dir, writeAllowed=Handler.write_allowed,
        fold={"sec": FOLD_SEC, "min": FOLD_MIN,
              "push": {"enabled": FOLD_PUSH, "gapSec": FOLD_PUSH_SEC}},
        cursorPushdown=CURSOR_PUSHDOWN, eventProject=EVENT_PROJECT_MODE, counts=counts)
    if not is_loopback(args.host):
        # **大声警告**：这不是"顺便提醒"，是"你现在暴露了什么"。写成多行是为了在
        # 满屏 JSON 日志里也一眼能被看到（一行 JSON 会被当成普通请求日志跳过）。
        if Handler.write_allowed:
            print(
                "\n"
                "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
                f"!! 警告：读口绑在 {args.host}:{args.port}（**非回环**），而且带了\n"
                "!!       --allow-remote-write：任何能访问这个地址的人都可以改\n"
                "!!       push.* 配置（白名单 + 类型/范围仍生效，审计照写）。\n"
                "!!       只在本来就可信的内网/隧道里这么用；用完就停。\n"
                "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n",
                file=sys.stderr)
            log("remote_write_open", host=args.host, port=args.port,
                note="write endpoints open to non-loopback clients (--allow-remote-write)")
        else:
            print(
                "\n"
                "------------------------------------------------------------------------\n"
                f"!! 提示：读口绑在 {args.host}:{args.port}（**非回环**），写口已关闭：\n"
                "!!       POST /api/push/config|preset 一律 403 write_requires_loopback。\n"
                "!!       读端点（overview / events / hosts / stream …）照常可用。\n"
                "!!       真要远程写：重启时加 --allow-remote-write（且确认网络可信）。\n"
                "------------------------------------------------------------------------\n",
                file=sys.stderr)
            log("write_requires_loopback", host=args.host, port=args.port)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    # 预热放后台线程：它花的是进程启动成本，不该让用户读屏时等（见 warm_up 的 docstring）。
    threading.Thread(target=warm_up, args=(db_path,), daemon=True,
                     name="fish-wb-warmup").start()
    print(f"[fish_workbench_api] 只读读口已起：http://{args.host}:{args.port}/",
          file=sys.stderr)
    print(f"[fish_workbench_api] 库：{db_path}"
          "（mode=ro + query_only；写探针被拒，证据见上面那行 readonly_proven JSON）",
          file=sys.stderr)
    if Handler.dist_dir:
        print(f"[fish_workbench_api] 静态：{Handler.dist_dir}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[fish_workbench_api] 收到中断，退出", file=sys.stderr)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
