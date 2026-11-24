#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 适配器：`projects/<ws>/<sid>.jsonl` 行流 → 对话段（T02 · 设计 §4.2）。

与 DSH 的**结构差异**（决定了两个适配器不能合并）
----------------------------------------------
- DSH 是 zstd 多帧 + 事件模型（`user/message` / `assistant/message` …）；
- WB 是**裸 JSONL 行模型**（`message` / `reasoning` / `function_call` …），
  一行一条记录、按行追加，所以游标是**行边界字节**、定位是 `line=<n>`、
  偏移空间是**原文件**（没有解压这一层）。

三个现场事实
------------
1. **体量**：实测 4.1 GB、单会话可达 674 MB，渲染产物 txt 混在目录里（F:42-49）。
   → 只认 `.jsonl`（按扩展名白名单），其余计入 `stats.skipped_files`。
2. **噪音**：674 MB 样本前 4000 行里 `function_call` 1122 / `function_call_result` 1122
   / `reasoning` 865 / `message/assistant` 514 / `file-history-snapshot` 340 /
   `message/user` 33 —— 只留 `message` 行，其余计数跳过（PRD §3.1）。
3. **注入上下文**：`message/user` 的正文里带 `<system-reminder …>` 块
   （`<user_info>` / `<user_references>` / 工作区路径）。**必须剥除**，
   否则"用户说的话"里会混进我们自己的注入块（M19 变异体）。

流式纪律（C-P0-2 红②）
---------------------
`for line in fh` 会整行读进内存——674 MB 的单行会直接把内存打爆。所以这里用
**有上限的行读取器**：读满 `max_line_bytes` 还没见到换行，就把这一行**丢掉**并
继续读到行尾（`oversize_line`），内存峰值只跟上限有关，不跟文件大小有关。

W3a（113 §4.1 · 2026-11-25）：血缘四列不再硬编码
----------------------------------------------
语料的 `origin` / `parent_session` / `delegation_depth` / `role` 四列以前是**写死**的
（`None/None/0/None`），于是 742 个 `agent-*` transcript 在库里全是
`origin=main / basis=default / parent=NULL`——WB 把父写在**路径**里、把角色写在**首行**
`providerData.agent` 里，这两处证据以前一条都没被用上（110 §2.3）。

现在四列一律取自 `fish_lineage_wb`（树投影用的**同一份**判定，见 `_lineage_columns`）：
`_header_of` 只是把判定结果贴到会话头，`origin_basis` 因此从 `default` 变成 `header`。
`session_id` 的来源**一个字节没动**（仍是 `path.stem`，§1-7 的 742 行主键不许漂）。

三条边界（都是实测，不是假想）：
1. **多数 transcript 没有 `session-meta` 首行**：实测 2,099 份里 **2,099 份**首行都是
   `message`，所以只改 `_header_of` 是徒劳的——"会话不存在"那条分支（`ingest_file`
   建行）也必须走同一个 `_header_of`，否则四列永远是默认值（库里 341 行
   `{"header_missing": 1}` 就是这条路的指纹）。
2. **首行要单独读一次**（`read_header`，≤1 MiB 只读一行）：建行时还没进行循环，
   拿不到流里的 first row；而角色证据就在它身上。
3. **语料 `origin` 的值域只有 `main|subagent`**（DDL 与 `fish_corpus_tag` 的常量），
   `unknown` 无处安放：判不到父的 `agent-*`（挪出 `subagents/`）**不主张**来源、
   只把 `unknown/missing` 的判定写进 `extra`——写 `main` 是 113 §4.2 ③ 明令禁止的兜底。
   实测这种行 = **0**（2,099/2,099 都有父）。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import fish_corpus_redact as redact_mod
import fish_corpus_store as store
import fish_lineage_wb as lineage_wb
from fish_corpus_tag import TagError, Tagger

HOST = "workbuddy"
FORMAT = store.FORMAT_WB_JSONL
OFFSET_SPACE = "file"
DEFAULT_MAX_LINE_BYTES = 8 << 20                  # 8 MiB（设计 §4.2）
DEFAULT_CHUNK = 1 << 20
DEFAULT_BATCH_ROWS = 512

TEXT_PART_TYPES = ("input_text", "output_text", "text")
ATTACHMENT_TYPES = ("image", "image_url", "file", "document")
ROLE_MAP = {"user": "user", "assistant": "assistant", "human": "user", "ai": "assistant"}

# 有意不入库的行类型（§4.2 表格；实测分布见模块 docstring）。
SKIP_ROW_TYPES = frozenset((
    "reasoning", "function_call", "function_call_result", "file-history-snapshot",
    "resend-fork-notice", "tool_call", "tool_result",
))

# `<system-reminder …>…</system-reminder>`：跨行、可能有属性；未闭合的也要吃掉
# （截断的历史消息里出现过半块，留着就是把注入内容当用户原话）。
_SR_BLOCK = re.compile(r"<system-reminder\b[^>]*>.*?</system-reminder\s*>",
                       re.DOTALL | re.IGNORECASE)
_SR_OPEN = re.compile(r"<system-reminder\b[^>]*>.*\Z", re.DOTALL | re.IGNORECASE)


class AdapterError(RuntimeError):
    """适配器层面的失败（目录不存在、行读取越界等）。"""


def discover(roots) -> list:
    """扫出所有会话文件（mtime 倒序）。目录名**只用于定位**，不参与判项目（R1）。"""
    if isinstance(roots, (str, os.PathLike)):
        roots = [roots]
    found = []
    for root in roots or ():
        base = Path(root)
        if not base.exists():
            continue
        for path in base.rglob("*.jsonl"):
            if path.name.endswith(".zstd"):        # 防 `*.jsonl.zstd` 混进来
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            found.append({
                "host": HOST,
                "path": str(path),
                "session_id": path.stem,
                "format": FORMAT,
                "size": int(st.st_size),
                "mtime_ns": int(st.st_mtime_ns),
            })
    found.sort(key=lambda item: item["mtime_ns"], reverse=True)
    return found


# ------------------------------------------------------------------ 行流


def _read_line(fh, max_bytes: int) -> tuple:
    """读一行（含换行），返回 `(raw, consumed, oversize)`。

    `raw` 不含换行；`consumed` 是这一行**在文件里**占的字节数（含换行），
    游标就前进这么多——即使这一行是坏的/超长的也要前进，否则下次会在同一行卡死。
    """
    buf = bytearray()
    consumed = 0
    oversize = False
    while True:
        piece = fh.readline(DEFAULT_CHUNK)
        if not piece:
            break
        consumed += len(piece)
        if not oversize:
            buf.extend(piece)
            if len(buf) > max_bytes:
                oversize = True
                buf = bytearray()                  # 超长行不保留内容（内存上限）
        if piece.endswith(b"\n"):
            break
    raw = bytes(buf)
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if raw.endswith(b"\r"):
        raw = raw[:-1]
    return raw, consumed, oversize


def iter_rows(path, offset: int = 0, *, max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
              base_lines=None, stats=None):
    """从 `offset`（**必须是行边界**）开始逐行 yield `(line_no, raw, byte_from, consumed, oversize)`。

    `line_no` 是**文件内绝对行号**（从 1 起），不随游标重置：重启续传后同一行的门牌
    必须还是同一个数字，否则 `read(line=5678)` 会指到别的内容上去。

    `base_lines`（2026-10-27 修 M16）：`offset` 之前已有多少行。游标里本来就存着
    `last_line_seen`，直接传进来即可——旧实现每次都把已采前缀**重读一遍**数行
    （674 MB 的会话续传就是重读 674 MB）。`None` = 自己数（自证口径，测试与
    "游标不可信"时用）。`stats` 收到时记 `prefix_bytes_read` / `prefix_lines_counted`，
    便于判据直接量"续传读了多少字节"。
    """
    if base_lines is None:
        base_lines, prefix_bytes = _count_lines_before(path, int(offset))
    else:
        base_lines, prefix_bytes = int(base_lines), 0
    if stats is not None:
        stats["prefix_bytes_read"] = prefix_bytes
        stats["prefix_lines_counted"] = base_lines
    line_no = base_lines
    with open(path, "rb") as fh:
        fh.seek(int(offset))
        while True:
            byte_from = fh.tell()
            raw, consumed, oversize = _read_line(fh, max_line_bytes)
            if consumed == 0:
                return
            line_no += 1
            yield (line_no, raw, byte_from, consumed, oversize)


def _count_lines_before(path, offset: int,
                        max_line_bytes: int = DEFAULT_MAX_LINE_BYTES) -> tuple:
    """数 `offset` 之前有多少行，返回 `(行数, 读过的字节数)`。

    代价：续传时多读一遍已采部分——所以正常路径**不该**走这里（游标带 `last_line_seen`，
    `iter_rows(base_lines=…)` 直接用）。保留它是为了"游标缺行号"的旧库与自证口径。
    """
    n = 0
    with open(path, "rb") as fh:
        read = 0
        while read < offset:
            piece = fh.readline(DEFAULT_CHUNK)
            if not piece:
                break
            read += len(piece)
            if read <= offset:
                n += 1
    return n, min(read, int(offset))


def is_line_boundary(path, offset: int) -> bool:
    """`offset` 是不是一条行的起点（前一个字节必须是换行）。

    与 DSH 的帧边界检查同理由：源文件被重写后，旧游标会落在行中间，`readline` 会把
    半行当成一整行——JSON 解析失败 → 跳行 → **静默少采**。宁可归零重采（幂等）。
    """
    if offset <= 0:
        return True
    try:
        with open(path, "rb") as fh:
            fh.seek(offset - 1)
            return fh.read(1) in (b"\n",)
    except OSError:
        return False


# ------------------------------------------------------------------ 行映射


def _to_utc(value):
    if value is None or value == "":
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num > 1e11:
        num = num / 1000.0
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(num))


def strip_system_reminders(text: str) -> str:
    """剥掉注入块（`<system-reminder …>…</system-reminder>`）与未闭合的残块。"""
    if not text:
        return ""
    out = _SR_BLOCK.sub("", text)
    out = _SR_OPEN.sub("", out)
    return out.strip()


def _text_parts(content) -> tuple:
    texts = []
    attachments = {}
    if isinstance(content, str):
        return [content], attachments
    if not isinstance(content, list):
        return texts, attachments
    for item in content:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind in TEXT_PART_TYPES:
            value = item.get("text")
            if value:
                texts.append(str(value))
        elif kind in ATTACHMENT_TYPES:
            attachments[kind] = attachments.get(kind, 0) + 1
    return texts, attachments


def _attach_placeholders(texts: list, attachments: dict) -> list:
    if not attachments or not texts:
        return texts
    tag = "〔附件:" + "+".join("%s×%d" % (k, v) for k, v in sorted(attachments.items())) + "〕"
    texts[-1] = (texts[-1] + "\n" + tag) if texts[-1] else tag
    return texts


def map_row(row: dict):
    """行 → `(kind, payload)`；kind∈{meta,title,message,skip,unknown}。"""
    if not isinstance(row, dict):
        return "unknown", None
    rtype = row.get("type") or ""
    if rtype == "session-meta":
        return "meta", row
    if rtype == "ai-title":
        return "title", row
    if rtype == "message":
        role = ROLE_MAP.get(str(row.get("role") or "").lower())
        if not role:
            return "skip", "message:%s" % (row.get("role") or "no-role")
        return "message", row
    if rtype in SKIP_ROW_TYPES:
        return "skip", rtype
    return "unknown", rtype


#: 顶层会话文件的证据口径（`origin_basis` 只有 header|default，这是 header 的具体来源）
BASIS_ROOT_PATH = "root-path"
SOURCE_ROOT = "wb:root-path"

#: 只带 cwd/title 的补标不许碰的列：血缘四列 + `origin_basis` + `extra`（证据）。
#: 详见 `ingest_file.backfill_cwd` 的 docstring（实测这条冲掉了 742 行的 origin）。
LINEAGE_CARRY_FIELDS = ("origin", "origin_basis", "parent_session", "delegation_depth",
                        "role", "extra")


def _is_root_file(path: Path) -> bool:
    """WB **顶层会话文件**：`<…>/projects/<ws>/<非 agent-*.jsonl>`。

    为什么这条判据成立：WB 把主会话写在 `projects/<ws>/<sid>.jsonl`（**两层**），把子代理
    写在 `projects/<ws>/<父 sid>/subagents/agent-*.jsonl`（四层）。所以"两层 + 名字不是
    `agent-*`"就是"这是顶层会话"的**形态证据**，不是猜的。

    只按名字判（`not is_transcript_name`）不够：挪出 `subagents/` 的 `agent-*.jsonl`
    也在 `projects/<ws>/<父 sid>/` 下，那是**判不到**（stray），不是根。
    """
    if lineage_wb.is_transcript_name(path.name):
        return False
    parent = path.parent
    return parent.parent.name.lower() == lineage_wb.PROJECTS_DIR_NAME


#: `_wb_runtime` 的进程内缓存：`{根的字符串: (指纹, WbRuntime)}`
_WB_RUNTIME_CACHE: dict = {}


def _runtime_stamp(teams: Path) -> tuple:
    """`teams/*/runtime.json` 的 (目录名, mtime_ns, 字节数) 指纹（10 次 stat，~0.1 ms）。"""
    try:
        entries = sorted(teams.glob("*/runtime.json"))
    except OSError:
        return ()
    stamp = []
    for entry in entries:
        try:
            info = entry.stat()
        except OSError:                     # 刚好被宿主编走：不算一个指纹位
            continue
        stamp.append((entry.parent.name, info.st_mtime_ns, info.st_size))
    return tuple(stamp)


def _wb_runtime(path):
    """本棵 WB 树的 `teams/*/runtime.json` 索引；**推不出根就 `None`**。

    语料侧只拿到文件路径，根由 `lineage_wb.root_of_path` 反推。推不出来时宁可只有
    路径证据，也绝不回落到 `~/.workbuddy`——临时树/测试树踩空会读到生产 runtime，
    给出一个与本文件无关的父（那是比"判不到"糟得多的错）。

    **进程内按"这一刻的 runtime 指纹"缓存**：实测本机 10 份 runtime 共 4.5 MB、
    单次 `load_runtime` 738 ms（每个成员都要 `is_file()`）；一次全量 ingest 要过
    2,099 个文件，不缓存就是 +26 分钟。指纹取 10 份文件的 `(名, mtime_ns, size)`，
    所以宿主新增/改写一份 runtime 之后缓存立刻失效——缓存的是**同这一刻的真相**，
    不是"今天第一次看到的真相"。
    """
    root = lineage_wb.root_of_path(path)
    if root is None:
        return None
    teams = lineage_wb.teams_dir(root)
    stamp = _runtime_stamp(teams)
    key = str(root)
    cached = _WB_RUNTIME_CACHE.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    runtime = lineage_wb.load_runtime(root)
    if len(_WB_RUNTIME_CACHE) > 8:          # 临时树/判据会造很多根：别让缓存无界长
        _WB_RUNTIME_CACHE.clear()
    _WB_RUNTIME_CACHE[key] = (stamp, runtime)
    return runtime


def _lineage_columns(path, first_row, *, runtime=None) -> dict:
    """W3a：语料**四列** = `fish_lineage_wb` 的血缘判定（113 §4.1）。

    语料 `origin` 的值域只有 `main|subagent`，`unknown` 无处安放，所以三种形态分开处理，
    **没有一条是"看不出来就写 main"**：

      · 有父（路径父 / runtime 的 lead）→ `subagent` + 父 + `delegation_depth=1`；
      · WB 顶层会话文件（`_is_root_file`）→ 显式 `main`（`origin_basis` 落 `header`，
        **不是** tagger 的 `default` 兜底）+ 深度 0；
      · 其余（挪出 `subagents/` 的 `agent-*`）→ **不主张**（`origin=None`），把
        `unknown/missing` 与原因写进 `extra`。写 `main` 是 113 §4.2 ③ 的后备兜底，
        正是 `fish_corpus_tag.py:240` 被当反面教材的那条。

    返回 `{origin, parent_session, delegation_depth, role, evidence}`；`evidence` 进
    `corpus_session.extra`，让"这一行凭什么这么判"可对账（basis/source/reason）。
    """
    path = None if path is None else Path(path)
    decision = lineage_wb.classify_transcript(path, first_row, runtime=runtime)
    evidence = {"lineage_basis": decision["basis"], "lineage_source": decision["source"]}
    if decision.get("role_from"):
        evidence["role_from"] = decision["role_from"]
    role = decision.get("role")
    if role:                       # `role` 也是宿主文本：与 title 走同一份脱敏（安全口径）
        role = redact_mod.redact(str(role))[0]
    columns = {"origin": None, "parent_session": None, "delegation_depth": 0,
               "role": role, "evidence": evidence}
    if decision["origin"] == "subagent":
        columns.update(origin="subagent", parent_session=decision["parent_session"],
                       delegation_depth=1)
        return columns
    if path is not None and _is_root_file(path):
        # 形态即证据：顶层会话文件 → 显式 main（basis 落 header，不是 default）。
        # 这里**不带** `reason='not_in_subagents'`：那是"判不到"的原因，不是根的原因。
        columns.update(origin="main", delegation_depth=0)
        evidence.update(lineage_origin="main", lineage_basis=BASIS_ROOT_PATH,
                        lineage_source=SOURCE_ROOT)
        return columns
    for key in ("reason", "unverified_parent"):
        if decision.get(key):
            evidence[key] = decision[key]
    evidence["lineage_origin"] = "unknown"     # 判不到：留证据，不主张（见 docstring ③）
    return columns


def _header_of(row, path_session_id, fallback_cwd=None, *, path=None,
               runtime=None, first_row=None) -> dict:
    """`session-meta` 行（或文件首行）→ 打标用的会话头。

    W3a（113 §4.1）：`origin`/`parent_session`/`delegation_depth`/`role` 四列不再硬编码，
    一律来自 `_lineage_columns`（= `fish_lineage_wb` 的判定）。`session_id` 的来源
    **一个字节没动**（`row.sessionId or 文件名`；调用方再按文件名覆盖，§1-7 的护栏）。

    两个入参各管一件事，**不合并**：
      · `row` 给 `cwd`/`sessionId`/`created_at`（有 `session-meta` 时就是它）；
      · `first_row` 给**角色证据**（`providerData.agent`）——文件首行往往不是
        `session-meta`，而角色恰好在首行上；不给就用 `row` 自己。
    """
    data = row if isinstance(row, dict) else {}
    evidence_row = first_row if first_row is not None else data
    lineage = _lineage_columns(path, evidence_row, runtime=runtime)
    # `header_missing` 是"这一行不是会话头"的既有指纹（库里 341 行的形状），保持不变
    extra = ({"source": "session-meta"} if data.get("type") == "session-meta"
             else {"header_missing": 1})
    extra.update(lineage["evidence"])
    return {
        "session_id": str(data.get("sessionId") or path_session_id),
        "cwd": data.get("cwd") or fallback_cwd,
        "title": None,
        # ↓ W3a 四列：唯一来源 `_lineage_columns`（旧实现是 None/None/0/None 硬编码）
        "origin": lineage["origin"],
        "parent_session": lineage["parent_session"],
        "delegation_depth": lineage["delegation_depth"],
        "role": lineage["role"],
        # ↑ 四列
        "agent_preset": None,
        "created_at": _to_utc(data.get("timestamp")),
        "extra": extra,
    }


def _safe_title(title):
    """标题写入前脱敏（S3）：标题会进 `search`/`read` 回执与推送行，属可回显列。"""
    if not title:
        return title
    text, _hits, _kinds = redact_mod.redact(str(title))
    return text


def _segments_of(row, role, loc, byte_from, consumed, session) -> tuple:
    """`(若干段, 是否截断)`；正文超 `store.MAX_PART_CHARS` 时截断并声明（M6）。"""
    texts, attachments = _text_parts(row.get("content"))
    texts = [strip_system_reminders(t) for t in texts]
    texts = [t for t in texts if t]
    texts = _attach_placeholders(texts, attachments)
    if not texts:
        return [], False
    occurred = _to_utc(row.get("timestamp")) or session.get("started_at") or store.now_utc()
    segs = []
    truncated_any = False
    for part, text in enumerate(texts):
        redacted, hits, _ = redact_mod.redact(text)
        redacted, truncated = store.cap_part_text(redacted)
        truncated_any = truncated_any or truncated
        segs.append({
            "host": HOST, "session_id": session["session_id"], "loc": loc, "part": part,
            "role": role,
            "msg_id": str(row.get("id")) if row.get("id") else None,
            "turn": None, "step": None,
            "occurred_at": occurred,
            "text": redacted,
            "chars": len(redacted),
            "redacted_count": hits,
            "raw_sha256": store.text_sha256(text),
            "byte_from": byte_from + 0,
            "byte_to": byte_from + consumed,
            "offset_space": OFFSET_SPACE,
            "shadowed": 0,
        })
    return segs, truncated_any


# ------------------------------------------------------------------ 单文件采集


def ingest_file(conn, path, cursor=None, *, tagger=None, dry_run: bool = False,
                batch_rows: int = DEFAULT_BATCH_ROWS,
                max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
                block_sessions=None, session_id_override=None) -> dict:
    """采一个 WB 会话文件。逐行解析、按批提交，游标只前进到已提交的行边界。

    `session_id_override`（R1 的同一条纪律）：宿主侧给了"目录名 → 真会话 id"的映射时
    按真 id 记游标/段；WB 自己不以目录名判 id，所以这里只是接住门面透传的参数。
    `last_line_seen` 续传（M16）：游标里已有行号时直接用它，**不再重读前缀**数行。
    """
    path = Path(path)
    sid = str(session_id_override or path.stem)
    counters = {
        "host": HOST, "path": str(path), "session_id": sid, "format": FORMAT,
        "status": store.SOURCE_OK, "rows": 0, "events_seen": 0, "segments_new": 0,
        "segments_updated": 0, "segments_skipped": 0, "unknown_events": 0,
        "bytes_consumed": 0, "bytes_total": int(cursor.get("bytes_total") or 0) if cursor else 0,
        "unchanged": False, "dry_run": bool(dry_run), "degraded": [], "orphan": False,
        # 与 DSH 侧同一形状（门面按统一键聚合）：WB 目前不产非降级告警，但键必须在。
        "warnings": [],
    }
    try:
        st = path.stat()
        stat_info = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
    except OSError as exc:
        reason = "%s: %s" % (type(exc).__name__, exc)
        counters["status"] = store.SOURCE_DEGRADED
        counters["degraded"].append({"reason_code": store.REASON_UNREADABLE, "reason": reason,
                                     "segments_kept": 0})
        if not dry_run:
            store.record_degraded(conn, HOST, path, sid, store.REASON_UNREADABLE, reason, 0)
            if cursor:
                store.set_source_status(conn, HOST, sid, store.SOURCE_DEGRADED,
                                        store.REASON_UNREADABLE)
            conn.commit()
        return counters
    if cursor and (int(stat_info["size"]) == int(cursor.get("bytes_total") or 0)
                   and int(stat_info["mtime_ns"]) == int(cursor.get("mtime_ns") or 0)):
        counters["unchanged"] = True
        return counters
    if block_sessions and sid in block_sessions:
        counters["status"] = "blocked"
        return counters

    start = int((cursor or {}).get("bytes_total") or 0)
    if start and not is_line_boundary(path, start):
        counters["cursor_reset"] = True
        start = 0
    first_seen_at = (cursor or {}).get("first_ingested_at")
    committed_offset = start
    last_line = (cursor or {}).get("last_line_seen")
    events_seen_before = int((cursor or {}).get("events_seen") or 0)
    tagger = tagger or Tagger()
    # W3a：四列的证据在这里备好，**建行与 `session-meta` 两条路共用**。
    #   首行只读一行（≤1 MiB，与树投影同一个 `read_header`）——实测 2,099 份 transcript
    #   的首行全是 `message`，角色 `providerData.agent` 就在它上面；不读首行就只剩 null。
    #   runtime 只读本棵树（`<root>/teams/*/runtime.json`），推不出根就不读。
    first_row = lineage_wb.read_header(path)
    wb_runtime = _wb_runtime(path)
    session = store.get_session(conn, HOST, sid)
    if session is None:
        # ⚠️ 这里是**大部分 transcript 唯一会走到的建行分支**（首行不是 session-meta），
        #   所以四列必须在这里就判好；否则 `_header_of` 改得再对也永远不被调用。
        header = _header_of(first_row, sid, path=path, runtime=wb_runtime)
        header["session_id"] = sid     # 游标按文件建：与 meta 分支同一条纪律（§1-7）
        session = tagger.tag_session(header, HOST)
        if not dry_run:
            with store.write_txn(conn):
                store.upsert_session(conn, session)
    batch = []
    batch_stats = {}

    def stat_event(row_type, kept=0, skipped=0):
        slot = batch_stats.setdefault(row_type, [0, 0, 0])
        slot[0] += 1
        slot[1] += kept
        slot[2] += skipped

    def kept_segments(extra=0):
        """"已提交 + 本批待提交 + extra"的段数（台账 `segments_kept` 的真值口径）。"""
        pending = sum(1 for action in batch if action[0] == "segment")
        return store.count_segments(conn, HOST, sid) + pending + int(extra)

    def backfill_cwd(row):
        """`session-meta` 常常没有 cwd，而 `message`/`ai-title` 行上有。

        不补的后果很具体：整类 WB 会话会被标成 `no_cwd`（"连比都没法比"），而它其实
        **有**归属证据。只在会话还没有 cwd 时补一次，且仍然走同一个 `Tagger`
        ——补的不是"猜一个项目"，是把宿主真给的 cwd 喂进同一套解析（R2 不变）。

        **W3a：这个头只带 cwd/title，四列必须原样带过去**（`LINEAGE_CARRY_FIELDS`）。
        实测真树：`session-meta` 行常常没有 cwd，于是"meta 扫掉 cwd → 补 cwd → meta 再扫"
        来回好几轮；而裸头在 `_origin_of` 里是"无 origin 无父"= `main/default`，照抄就把
        子代理行写成主会话。父与深度当时靠 upsert 的 `COALESCE`/`MAX` 侥幸留住了，
        唯独 `origin`/`origin_basis` 被冲干净——**行看起来有父、来源却是 main/default**，
        这种"半对"最难查，所以在这里堵死而不是等对账发现。
        """
        nonlocal session
        row_cwd = row.get("cwd")
        if not row_cwd or session.get("cwd"):
            return
        tagged = tagger.tag_session(
            {"session_id": sid, "cwd": row_cwd, "title": session.get("title"),
             "created_at": session.get("started_at")}, HOST)
        tagged["title"] = session.get("title") or tagged.get("title")
        for key in LINEAGE_CARRY_FIELDS:
            if session.get(key) is not None:
                tagged[key] = session[key]
        session.update(tagged)
        if not dry_run:
            store.upsert_session(conn, session)

    def flush(force: bool = False, upto=None, upto_line=None):
        """提交本批（段 + 标题 + 事件统计 + 游标，同一事务）。

        与 DSH 侧同一条纪律（R2）：`committed_offset` / `last_line` **只在事务成功后**
        前进——旧实现在循环里先推进、再 flush，一旦中途抛错（OSError/TagError），
        已推进的那一截既没落盘也没回退，续传就把它跳过去了。
        `force=True`：0 段文件也要留游标行（P2-1：sessions 与 sources 必须对得上）。
        """
        nonlocal batch, batch_stats, committed_offset, last_line
        if not batch and not force:
            return
        boundary = committed_offset if upto is None else int(upto)
        boundary_line = last_line if upto_line is None else int(upto_line)
        if dry_run:
            batch = []
            batch_stats = {}
            committed_offset, last_line = boundary, boundary_line
            return
        with store.write_txn(conn):
            for action in batch:
                if action[0] == "segment":
                    created = store.upsert_segment(conn, action[1])[1]
                    if created:
                        counters["segments_new"] += 1
                    else:
                        counters["segments_updated"] += 1
                elif action[0] == "title":
                    # 只改标题这一列：走 `upsert_session` 会把未提供的列按缺省值写回去
                    # （`project_source` 默认 `unmapped`），等于把打标结果悄悄降级。
                    conn.execute("UPDATE corpus_session SET title=? WHERE host=? AND session_id=?",
                                 (action[1], HOST, sid))
            for row_type, (seen, kept, skipped) in batch_stats.items():
                store.bump_event_stat(conn, HOST, sid, row_type, seen=seen, kept=kept,
                                      skipped=skipped)
            store.upsert_session(conn, session)
            store.upsert_source(conn, {
                "host": HOST, "session_id": sid, "path": str(path), "format": FORMAT,
                "bytes_total": boundary, "mtime_ns": int(stat_info["mtime_ns"]),
                "events_seen": events_seen_before + counters["events_seen"],
                "segments_kept": store.count_segments(conn, HOST, sid),
                "last_seq_seen": None, "last_line_seen": boundary_line,
                "status": counters["status"], "degraded_reason": (
                    counters["degraded"][-1]["reason_code"] if counters["degraded"] else None),
                "first_ingested_at": first_seen_at})
        batch = []
        batch_stats = {}
        committed_offset, last_line = boundary, boundary_line

    row_stats = {}
    try:
        # 续传时游标里已有行号 → 不重读前缀（M16）。
        #   · start=0（首采或游标被重置）：行号从 0 起，不需要数；
        #   · start>0 且有行号：直接用；
        #   · start>0 但游标缺行号（旧库）：自己数一遍（自证口径）。
        if not start:
            base_lines = 0
        elif last_line is not None:
            base_lines = int(last_line)
        else:
            base_lines = None
        for line_no, raw, byte_from, consumed, oversize in iter_rows(
                path, start, max_line_bytes=max_line_bytes, base_lines=base_lines,
                stats=row_stats):
            counters["rows"] += 1
            pending_line = line_no
            if oversize:
                reason = "第 %d 行超过 %d 字节，整行跳过" % (line_no, max_line_bytes)
                # 整行丢弃 = 声明过的损失：状态/摘要/计数必须一致（安全 🟡）。
                counters["status"] = store.SOURCE_DEGRADED
                kept = kept_segments()
                counters["degraded"].append({"reason_code": store.REASON_OVERSIZE_LINE,
                                             "reason": reason, "segments_kept": kept})
                if not dry_run:
                    store.record_degraded(conn, HOST, path, sid,
                                          store.REASON_OVERSIZE_LINE, reason, kept)
                stat_event("<oversize_line>", skipped=1)
                committed_offset, last_line = byte_from + consumed, pending_line
                continue
            if not raw.strip():
                committed_offset, last_line = byte_from + consumed, pending_line
                continue
            try:
                row = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError as exc:
                reason = "第 %d 行 JSON 坏：%s" % (line_no, exc)
                counters["degraded"].append({"reason_code": store.REASON_BAD_JSON,
                                             "reason": reason, "segments_kept": 0})
                if not dry_run:
                    store.record_degraded(conn, HOST, path, sid, store.REASON_BAD_JSON,
                                          reason, store.count_segments(conn, HOST, sid))
                stat_event("<bad_json>", skipped=1)
                committed_offset, last_line = byte_from + consumed, pending_line
                continue
            counters["events_seen"] += 1
            kind, payload = map_row(row)
            if kind == "meta":
                header = _header_of(payload, sid, path=path, runtime=wb_runtime,
                                    first_row=first_row)
                try:
                    tagged = tagger.tag_session(header, HOST)
                except TagError as exc:
                    raise TagError(str(exc)) from exc
                # 文件名与 `sessionId` 不一致时以**文件名**为主：游标是按文件建的，
                # 换 id 会让同一个文件有两条游标（一文件一会话）；矛盾记进 extra。
                if str(payload.get("sessionId") or "") not in ("", sid):
                    extra = json.loads(tagged.get("extra") or "{}")
                    extra["path_id_mismatch"] = 1
                    tagged["extra"] = json.dumps(extra, ensure_ascii=False, sort_keys=True)
                tagged["session_id"] = sid
                tagged["title"] = session.get("title")
                session.update(tagged)
                if not dry_run:
                    store.upsert_session(conn, session)
                stat_event("session-meta", kept=1)
            elif kind == "title":
                title = payload.get("aiTitle") or payload.get("title")
                if title:
                    safe = _safe_title(str(title))        # S3：标题也是内容
                    batch.append(("title", safe))
                    session["title"] = safe
                backfill_cwd(payload)
                stat_event("ai-title", kept=1 if title else 0)
            elif kind == "message":
                backfill_cwd(payload)
                role = ROLE_MAP[str(payload.get("role") or "").lower()]
                loc = "line=%d" % line_no
                segs, truncated = _segments_of(payload, role, loc, byte_from, consumed,
                                               session)
                if truncated:
                    reason = "loc=%s 的正文超过 %d 字，已截断" % (loc, store.MAX_PART_CHARS)
                    # 与 DSH 侧同一口径：截断是声明过的损失 → 状态 degraded + 真值段数。
                    counters["status"] = store.SOURCE_DEGRADED
                    kept = kept_segments(len(segs))
                    counters["degraded"].append({"reason_code": store.REASON_OVERSIZE_TEXT,
                                                 "reason": reason, "segments_kept": kept})
                    if not dry_run:
                        store.record_degraded(conn, HOST, path, sid,
                                              store.REASON_OVERSIZE_TEXT, reason, kept)
                if not segs:
                    counters["segments_skipped"] += 1
                    stat_event("message:%s" % role, skipped=1)
                else:
                    batch.extend(("segment", seg) for seg in segs)
                    if dry_run:
                        counters["segments_new"] += len(segs)
                    stat_event("message:%s" % role, kept=len(segs), skipped=0)
                    session["last_event_at"] = segs[-1]["occurred_at"]
            elif kind == "skip":
                stat_event(str(payload or "skip"), skipped=1)
            else:
                counters["unknown_events"] += 1
                stat_event(str(payload or "unknown"), skipped=1)
            committed_offset, last_line = byte_from + consumed, pending_line
            if len(batch) >= batch_rows:
                flush(upto=committed_offset, upto_line=last_line)
        flush(force=True, upto=committed_offset, upto_line=last_line)
    except (OSError, UnicodeError) as exc:
        reason = "%s: %s" % (type(exc).__name__, exc)
        code = (store.REASON_ENCODING if isinstance(exc, UnicodeError)
                else store.REASON_UNREADABLE)
        counters["status"] = store.SOURCE_DEGRADED
        counters["degraded"].append({"reason_code": code, "reason": reason,
                                     "segments_kept": 0})
        if not dry_run:
            store.record_degraded(conn, HOST, path, sid, code, reason,
                                  store.count_segments(conn, HOST, sid))
            try:
                flush(upto=committed_offset, upto_line=last_line)
            except Exception:            # noqa: BLE001 —— 落盘失败不掩盖原始错误
                conn.rollback()
            conn.commit()
    except TagError as exc:
        reason = "%s: %s" % (type(exc).__name__, exc)
        counters["status"] = store.SOURCE_DEGRADED
        counters["degraded"].append({"reason_code": store.REASON_BAD_HEADER, "reason": reason,
                                     "segments_kept": 0})
        if not dry_run:
            store.record_degraded(conn, HOST, path, sid, store.REASON_BAD_HEADER, reason,
                                  store.count_segments(conn, HOST, sid))
            store.upsert_source(conn, {
                "host": HOST, "session_id": sid, "path": str(path), "format": FORMAT,
                "bytes_total": committed_offset, "mtime_ns": int(stat_info["mtime_ns"]),
                "events_seen": events_seen_before + counters["events_seen"],
                "segments_kept": store.count_segments(conn, HOST, sid),
                "last_seq_seen": None, "last_line_seen": last_line,
                "status": store.SOURCE_DEGRADED, "degraded_reason": store.REASON_BAD_HEADER,
                "first_ingested_at": first_seen_at})
            conn.commit()
    if not dry_run:
        store.refresh_session_counts(conn, HOST, sid)
        conn.commit()
    counters["bytes_total"] = committed_offset
    counters["prefix_bytes_read"] = int(row_stats.get("prefix_bytes_read") or 0)
    return counters


class WbAdapter:
    """`WbAdapter`（设计 §3 类图）：模块函数的对象门面。"""

    host = HOST
    formats = (FORMAT,)

    def discover(self, roots) -> list:
        return discover(roots)

    def iter_rows(self, path, offset: int = 0):
        return iter_rows(path, offset)

    def map_row(self, row):
        return map_row(row)

    def ingest_file(self, conn, path, cursor=None, **kwargs):
        return ingest_file(conn, path, cursor, **kwargs)
