#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSH 会话头 → 会话树投影（113 §3 · W2 · 2026-11-25）。

## 这一波只做一件事：把会话头翻译成 `SessionNode`

DSH 是**唯一能判父的宿主**（`SessionStart` / `PostToolUse` 的 payload 里没有
`agent_id`，110 §1.1 v3），父链只存在于会话日志的**第一帧**——`session` 事件上的
`parentSession` / `isSeeded` / `delegationDepth` / `agentPreset`。所以本模块的全部
输入是那一个 JSON 对象，输出是 `fish_lineage.SessionNode` 形状的 dict。

## 三条纪律（都是"不这么做就会静默错"的那种）

1. **只读、不写库**：本模块没有 `sqlite3` 这个 import，也不接收连接——写投影表只走
   `fish_lineage.upsert_node`（全仓唯一写口，113 §2.4）。这样"适配器偷偷写一行"在
   结构上不可能发生，而不是靠"我记得别写"。
2. **适配器不猜深度**：`depth` 一律留空（取表 DEFAULT 0），宿主值只落 `depth_host`；
   真深度由 `fish_lineage.recompute_depth` 从链上重算（113 §3.3）。省掉这条，宿主那个
   `delegationDepth` 就会冒充结论——它自己都不信任它（110 §3.6.1 三条证据）。
3. **判不到就说判不到**：父在宿主里没写 → `unknown`，父指向一个不存在的会话 →
   `unknown` + `basis='missing'`。**任何一条路径都不许兜成 `main`**（110 §3.6.3）。

## 依赖方向：顶层不碰业务模块

顶层只 import stdlib + `fish_common`（轻量子模块）与 `zstandard`（登记过的依赖）。
`fish_corpus_dsh`（目录发现/时间格式）与 `fish_lineage`（类型）都在**函数内/类型检查
期**才 import：`fish_corpus_*` 最终会把 `fish` 拖进来，而这个模块将来会进
SessionStart 的 hook 路径（113 §5.7 的 W4 链路），顶层 import 等于给每次会话启动
加一份主库的导入成本。`test_lineage_dsh.LayeringTest` 用 AST 把这条钉住。

## 与 113 §3.2 的一处口径补充

§3.2 的判据 6 只写了"其余（有 `origin` 无父等）→ `main`/取 origin"。但有两种
**宿主自述自相矛盾**的形状必须显式处理，否则会撞库级 CHECK（`origin='main'` 不许有父）
或者把 fork 写成没父的笑话：`fork` 无父、`main` 有父。两者都落 `unknown` + `inferred`
（`extra.infer_rule` 写明是哪一种）。今天的数据里这两种形状都是 0 行。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

import fish_common

try:                                            # zstandard 是登记过的依赖（见依赖说明）
    import zstandard
except ImportError:                             # pragma: no cover —— 缺依赖时也要给人话
    zstandard = None

if TYPE_CHECKING:                               # 只给类型检查：运行时不 import 主库
    from fish_lineage import SessionNode

HOST = "dsh"

#: 会话文件名（与 `fish_corpus_dsh.FRAME_NAME_V4` 同值）。测试里有一条判据对拍两者，
#: 因为"发现"是语料层的既有实现（113 §3.2：scan 委托 `discover`，不写第二套）。
FRAME_NAME = "session.v4.jsonl.zstd"
FRAME_NAMES = (FRAME_NAME, "session.v3.jsonl.zstd", "session.jsonl.zstd")

#: 首帧最多读多少压缩字节（实测首帧几 KB；4 MiB 是"帧坏掉也别读整份 58 MB"的上限）
HEADER_READ_BYTES = 64 << 10
HEADER_MAX_BYTES = 4 << 20

SOURCE_HEADER = "dsh:header"
SOURCE_DEPTH = "dsh:depth"

#: `session_lineage.origin` 的闭集（DDL 的 CHECK 同值）。六判据的产出必须落在这里面。
ORIGINS = ("main", "subagent", "fork", "unknown")

#: 判据 3（矛盾行）的推断规则名：J9 的过滤条件会认这个名字（`source` 必须 `:depth` 结尾）
INFER_SELF_DEPTH = "self:depth>0"


class HeaderError(RuntimeError):
    """一个会话文件读不出 `session` 事件。`reason_code` 进失败清单（**不许静默跳过**）。"""

    def __init__(self, reason_code: str, message: str, path=None):
        super().__init__(message)
        self.reason_code = reason_code
        self.path = str(path) if path is not None else None


# ---------------------------------------------------------------- 读头（只读）


def _as_int(value):
    """`delegationDepth` 之类转 int：转不了返回 None（**不猜 0**——"没有"与"是根"不可混）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _first_frame_text(path: Path) -> str:
    """解出**首帧**的文本（逐片喂解压器，读到帧尾就停）。

    只解首帧是 113 §3.2 的明文要求：一个会话文件最多 2,773 帧 / 12.5 MB（`fish_corpus_dsh`
    的现场取证），而我们要的东西在第一帧的第一行——解第二帧纯属浪费，且在活会话上还会
    读到正在追加的半帧。
    """
    if zstandard is None:
        raise HeaderError("missing_dependency",
                          "缺 zstandard 依赖：pip install zstandard（见 docs/依赖说明-corpus.md）",
                          path)
    try:
        handle = open(path, "rb")
    except OSError as exc:
        raise HeaderError("unreadable", "%s: %s" % (type(exc).__name__, exc), path) from exc
    parts, total = [], 0
    decompressor = zstandard.ZstdDecompressor()
    with handle:
        dobj = decompressor.decompressobj()
        while total < HEADER_MAX_BYTES:
            piece = handle.read(HEADER_READ_BYTES)
            if not piece:
                break
            total += len(piece)
            try:
                out = dobj.decompress(piece)
            except zstandard.ZstdError as exc:
                raise HeaderError("bad_frame", "zstd: %s" % exc, path) from exc
            if out:
                parts.append(out)
            if dobj.eof:
                break
    return b"".join(parts).decode("utf-8", "replace")


def _created_at(value):
    """epoch 毫秒 → `2026-11-25T02:55:00Z`。

    复用 `fish_corpus_dsh._to_utc`（同一宿主、同一格式）：`fish_corpus_wb` 已经有一份
    拷贝了，本模块**不写第三份**。函数内 import 的理由见模块 docstring 的依赖方向。
    """
    if value in (None, ""):
        return None
    import fish_corpus_dsh as dsh_mod       # noqa: PLC0415 —— 只在真读头时付出导入成本
    return dsh_mod._to_utc(value)           # noqa: SLF001 —— 同宿主时间口径的唯一实现


def header_from_event(event: dict, path) -> dict:
    """`session` 事件 → 会话头（**宿主的原话**，解释留给 `classify`）。

    字段名与 `fish_corpus_dsh._header_of` 对齐（`session_id` / `parent_session` /
    `delegation_depth` / `agent_preset` / `cwd`），多了 `is_seeded`——那正是 fork 判据
    的第三根柱子，语料层没采它，但会话树必须采。
    """
    path = Path(path)
    dir_id = path.parent.name
    sid = event.get("id") or event.get("sessionId") or ""
    header = {
        "session_id": str(sid) if sid else str(dir_id),
        "path_session_id": str(dir_id),
        "parent_session": (str(event["parentSession"]) if event.get("parentSession") else None),
        "origin": (str(event["origin"]) if event.get("origin") else ""),
        "is_seeded": event.get("isSeeded"),
        "delegation_depth": _as_int(event.get("delegationDepth")),
        "agent_preset": event.get("agentPreset") or None,
        "cwd": event.get("cwd") or None,
        "created_at": _created_at(event.get("createdAt")),
        "version": event.get("version"),
    }
    # 目录名与头部 id 理论上同一个值（113 §0.3 实测 789/789 一致）；不一致以**头部**为准，
    # 并留痕——这类行将来在树上是"目录名查不到"的那种，没有标记就永远查不出来。
    if sid and str(sid) != str(dir_id):
        header["path_id_mismatch"] = 1
    return header


def read_header(path) -> dict:
    """读一个会话文件的**首帧**，取出 `session` 事件。

    失败一律 `HeaderError`（不返回空 dict）：空头会被下游当成"根会话"，而"读不出来"
    与"是个根"是两件完全不同的事。首帧解不出**任何字节**时记 `bad_frame`
    （实测 zstd 的 `decompressobj` 对"魔数后全是垃圾"不抛异常，只返回空串——
    把它当成"没有 session 事件"会丢掉"文件坏了"这个更有用的定性）。
    """
    path = Path(path)
    text = _first_frame_text(path)
    if not text.strip():
        raise HeaderError("bad_frame", "首帧解不出任何字节（文件损坏或不是 zstd 帧）", path)
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "session":
            return header_from_event(event, path)
    raise HeaderError("no_session_event",
                      "首帧里没有 type='session' 的事件（前 %d 行都没有）" % text.count("\n"),
                      path)


# ---------------------------------------------------------------- 判据（六条，顺序即优先级）


def _verdict(origin: str, relation, basis: str, source: str, rule: str,
             extra: dict | None = None) -> dict:
    """一条判据的产出。`rule` 只用于统计与排查，**不进库**（库里没有这一列）。"""
    if origin not in ORIGINS:
        raise ValueError("判据产出了闭集外的 origin %r（DDL 的 CHECK 会拒，别等到写入才炸）"
                         % (origin,))
    return {"origin": origin, "relation": relation, "basis": basis, "source": source,
            "extra": dict(extra or {}), "rule": rule}


def classify(header: dict, *, known_ids: set | None = None) -> dict:
    """六条判据（113 §3.2 的表格，**顺序即优先级**）→ `{origin, relation, basis, source, extra, rule}`。

    为什么顺序不能换：一条 `isSeeded=true` 且 `delegationDepth=1` 的子代理同时满足判据 1
    与判据 2 的前半段（实测 4 行）。判据 1 要求 `delegationDepth == 0`，所以它落在
    **判据 2（spawn）**——先判 fork 就错了，先判 subagent 才对。同理"种子但没父"（实测 8 行）
    必须落判据 4 的 `main`：seed 与 parent 是两个独立字段，脱钩时不许拿 seed 当父。

    `known_ids` 是**本轮扫描到的全部会话 id**（可选）：给了它就多做一次"父到底存不存在"
    的检查，父缺失 → `unknown` + `basis='missing'`（110 §3.6.3 的孤儿诚实）。判据顺序不动，
    这是**结论之后的覆盖**：先按宿主原话定性，再按"父在不在"降级。
    """
    parent = header.get("parent_session") or None
    depth = header.get("delegation_depth")
    seeded = header.get("is_seeded") is True
    origin = header.get("origin") or ""

    # 判 1：fork = 三条**同时**成立（seed + 有父 + depth==0）。这条写成"或"，实测 16 行就
    # 会混进 690 行普通子代理里（113 §1-6 的实测桶），树上一层变一片。
    if seeded and parent and depth == 0:
        verdict = _verdict("fork", "fork", "header", SOURCE_HEADER, "fork")
    # 判 2：宿主自己说它是子代理且有父 → spawn。**seed=true 但 depth=1 的 4 行落这里**。
    elif origin == "subagent" and parent:
        verdict = _verdict("subagent", "spawn", "header", SOURCE_HEADER, "subagent")
    # 判 3：有父、宿主没说 origin、也不是 seed（实测 1 行）→ **矛盾行**。
    # 唯一允许的一条推断规则是"自己 depth>0"（113 §3.6 的 110 §7-2 纪律：不许从
    # title/cwd 猜父）。origin 必须落 unknown：落 main 撞 CHECK、落 subagent 是替宿主发言。
    elif parent and not origin and not seeded:
        verdict = _verdict("unknown", "spawn", "inferred", SOURCE_DEPTH, "conflict",
                           {"infer_rule": INFER_SELF_DEPTH})
    # 判 4：seed 但没父（实测 8 行）→ 根。**不许标 fork**：fork 必须有父。
    elif not parent and seeded:
        verdict = _verdict("main", None, "header", SOURCE_HEADER, "seeded-root")
    # 判 5：没父、没 origin、depth==0（实测 68 行）→ 根。
    elif not parent and not origin and depth == 0:
        verdict = _verdict("main", None, "header", SOURCE_HEADER, "root")
    # 判 6：其余（有 origin 无父等，本期 0 行）。宿主的话能直接用就直接用，
    # 但两种**自相矛盾**的形状必须显式纠正，否则要么撞 CHECK 要么把 fork 写成没父。
    elif parent and origin == "main":
        verdict = _verdict("unknown", "spawn", "inferred", SOURCE_HEADER,
                           "main-with-parent", {"infer_rule": "host:main-with-parent"})
    elif not parent and origin == "fork":
        verdict = _verdict("unknown", None, "inferred", SOURCE_HEADER,
                           "fork-no-parent", {"infer_rule": "fork-no-parent"})
    elif origin in ORIGINS:
        verdict = _verdict(origin, "spawn" if origin == "subagent" else None,
                           "header", SOURCE_HEADER, "host-origin")
    else:
        # 有 origin 但不在闭集（宿主换了词）：不认识的词不许硬塞进去，落 unknown。
        verdict = _verdict("unknown", "spawn" if parent else None, "inferred", SOURCE_HEADER,
                           "unknown-origin", {"infer_rule": "origin:%s" % (origin or "empty")})

    if parent and known_ids is not None and parent not in known_ids:
        # 父指向一个不存在的会话：宿主的链接是"真的写了"，但那条会话在这批日志里没有。
        # 保留 relation（信息："它本该有父"），origin/basis 降级（110 §3.6.3 / J-D4）。
        verdict.update(origin="unknown", basis="missing", source=SOURCE_HEADER,
                       extra=dict(verdict["extra"], missing_parent=1))
    return verdict


def node_from_header(header: dict, *, observed_at: str | None = None,
                     known_ids: set | None = None, verdict: dict | None = None):
    """会话头 → `SessionNode` 形状的 dict（**不写库**：调用方交给 `upsert_node`）。

    `depth` / `lineage_path` **不给**（取表 DEFAULT）：适配器不猜深度，真深度由
    `recompute_depth` 从链上重算（113 §3.3）。注意 `upsert_node` 是整行覆盖，所以
    **重新投影会把重算过的 depth 打回 0**——扫描完必须紧跟一次重算，这是调用顺序的硬约束。
    """
    verdict = verdict or classify(header, known_ids=known_ids)
    extra = dict(verdict["extra"])
    if header.get("path_id_mismatch"):
        extra["path_id_mismatch"] = 1
    node = {
        "host": HOST,
        "session_id": header["session_id"],
        "parent_session": header.get("parent_session"),
        "depth_host": header.get("delegation_depth"),
        "origin": verdict["origin"],
        "relation": verdict["relation"],
        "agent_id": None,                 # DSH 恒 NULL（110 §3.2 的列语义）
        "agent_role": header.get("agent_preset"),
        "agent_label": None,              # catalog 的 label 要读父日志，本期不做（113 §3.6）
        "agent_path": None,               # 预留列，本期恒 NULL，不许为对称先填
        "cwd": header.get("cwd"),
        "agent_preset": header.get("agent_preset"),
        "basis": verdict["basis"],
        "source": verdict["source"],
        "extra": extra or None,
        "observed_at": observed_at or fish_common.now_utc(),
    }
    return node


# ---------------------------------------------------------------- 适配器


def _roots_of(roots) -> list:
    """扫描根：显式给的优先，否则走 `fish_corpus.default_roots('dsh')`（唯一来源）。"""
    if roots is not None:
        return [roots] if isinstance(roots, (str, Path)) else list(roots)
    from fish_corpus import default_roots      # noqa: PLC0415 —— 根目录解析的唯一实现
    return default_roots(HOST)


class DshLineageAdapter:
    """DSH 适配器：一个会话头 → 一个 `SessionNode`（113 §3.2 的 `resolve` / `scan`）。

    `roots` 可显式给（测试与"别的机器"都用得上），缺省读 `$FISH_DSH_SESSIONS` /
    `$DSH_HOME/sessions`。**无连接、无写入**：本类的方法都不接收 sqlite3 连接。
    """

    host = HOST

    def __init__(self, roots=None):
        self.roots = roots
        #: 最近一次 `scan` 的对账报告（文件数 / 解析数 / **失败清单** / 桶分布）
        self.last_report: dict | None = None

    # -------------------------------------------------- 单会话
    def find_path(self, session_id: str):
        """按会话 id 找文件（目录名 = 会话 id，113 §0.3 实测 789/789 一致）。

        走"根 → 工作区目录 → 会话目录"三层直查，不用 `discover` 全盘 rglob：
        `resolve` 会被 SessionStart 每会话调用一次（W4），全盘扫描放进热路径是自找的。
        找不到返回 None（**不猜路径**）。
        """
        sid = str(session_id or "")
        if not sid or "/" in sid or "\\" in sid or sid in (".", ".."):
            return None                       # 非会话 id 形状：不许拼路径
        for root in _roots_of(self.roots):
            base = Path(root)
            try:
                children = list(base.iterdir())
            except OSError:
                continue
            for workspace in children:
                try:
                    if not workspace.is_dir():
                        continue
                except OSError:
                    continue
                candidate = workspace / sid
                if not candidate.is_dir():
                    continue
                for name in FRAME_NAMES:
                    frame = candidate / name
                    if frame.is_file():
                        return frame
                try:                       # 不认识的帧名：取**最新**那份（与 scan 的去重口径一致）
                    frames = sorted(candidate.glob("session*.jsonl.zstd"),
                                    key=lambda p: p.stat().st_mtime_ns, reverse=True)
                except OSError:
                    continue
                if frames:
                    return frames[0]
        return None

    def resolve(self, session_id: str, *, hint: dict | None = None):
        """判一个会话（判不到返回 None，**不许拿 main 冒充**）。

        `hint` 支持 `{"path": ...}`：调用方已经知道文件在哪时省掉一次目录查找。
        目录名与头部 id 不一致的行照 `fish_corpus_dsh._header_of:469` 的口径处理——
        **以头部为准**，并把 `path_id_mismatch` 记进 `extra`。
        """
        path = (hint or {}).get("path") if isinstance(hint, dict) else None
        if path is None:
            path = self.find_path(session_id)
        if path is None:
            return None
        return node_from_header(read_header(path))

    # -------------------------------------------------- 批量
    def scan(self, *, since: str | None = None, limit: int | None = None) -> Iterable[dict]:
        """枚举全部 DSH 会话（`since` / `limit` 只影响**产出**，不影响"父在不在"的判定）。

        目录发现委托 `fish_corpus_dsh.discover`（113 §3.2：不写第二套扫描）。父存在性
        检查要对**全量**会话做，所以 `since`/`limit` 不能在发现阶段就砍——否则被砍掉的那
        一半会让剩下的子会话全部误判成"父缺失"。
        """
        nodes, _report = self.scan_with_report(since=since, limit=limit)
        return nodes

    def scan_with_report(self, *, since: str | None = None, limit: int | None = None):
        """`scan` 的对账面：返回 `(nodes, report)`。

        报告里的 `failed` 是**逐条列名**的失败清单（文件 + `reason_code`）。空列表 =
        这一轮所有会话头都读出来了——这是 113 §3.4「失败集合为空」那条锚的取值处。

        **同一会话多份文件**（实测 11 个会话目录里同时留着 `session.jsonl.zstd` 与
        `session.v4.jsonl.zstd`：版本迁移的历史残留）只投影**最新的一份**（`discover`
        按 mtime 倒序，所以先到的那份最新），其余进 `duplicate_ids` 清单。不这么做的话
        (host, session_id) 主键会把后到的覆盖掉——"谁赢"取决于目录遍历顺序，同一份数据
        两次扫描可能读出不同结果，而表面上看起来完全正常。

        报告口径（三条恒等式，测试逐条对拍）：
          · `files == parsed + len(failed)`   —— 每个文件都要有着落；
          · `parsed == unique + Σ(len(paths)-1)` —— 重复文件逐份记账；
          · `projected` = `unique` 再过 `since`/`limit`（只砍产出，不砍"父在不在"）。

        `since` 按会话头自己的 `createdAt` 比（ISO 串直接字典序）；**没有 createdAt 的行
        不砍**——判不到就不猜，比"按文件 mtime 猜创建时间"诚实（会话被压缩重写时 mtime 会变）。
        """
        import fish_corpus_dsh as dsh_mod       # noqa: PLC0415 —— 语料层不进 hook 路径
        roots = _roots_of(self.roots)
        discovered = dsh_mod.discover(roots)
        observed_at = fish_common.now_utc()

        headers, failed, seen, parsed = [], [], {}, 0
        for item in discovered:
            try:
                header = read_header(item["path"])
            except HeaderError as exc:
                failed.append({"path": item["path"], "reason": exc.reason_code,
                               "detail": str(exc)})
                continue
            parsed += 1
            sid = header["session_id"]
            if sid in seen:
                seen[sid]["paths"].append(item["path"])
                continue
            seen[sid] = {"session_id": sid, "paths": [item["path"]]}
            header["path"] = item["path"]
            headers.append(header)
        duplicate_ids = [item for item in seen.values() if len(item["paths"]) > 1]

        known_ids = {h["session_id"] for h in headers}
        nodes, rules = [], {}
        for header in headers:
            verdict = classify(header, known_ids=known_ids)
            rules[verdict["rule"]] = rules.get(verdict["rule"], 0) + 1
            node = node_from_header(header, observed_at=observed_at, verdict=verdict)
            created = header.get("created_at") or ""
            if since and created and created < since:
                continue
            nodes.append(node)
        skipped_since = len(headers) - len(nodes)
        truncated = 0
        if limit is not None and limit >= 0 and len(nodes) > limit:
            truncated = len(nodes) - limit
            nodes = nodes[:limit]

        self.last_report = {
            "host": HOST,
            "roots": [str(r) for r in roots],
            "files": len(discovered),
            "parsed": parsed,
            "failed": failed,
            "duplicate_ids": duplicate_ids,
            "unique": len(headers),
            "projected": len(nodes),
            "skipped_since": skipped_since,
            "truncated_by_limit": truncated,
            "rules": rules,
            "observed_at": observed_at,
        }
        return nodes, self.last_report
