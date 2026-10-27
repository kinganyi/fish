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
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import fish_corpus_redact as redact_mod
import fish_corpus_store as store
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


def _header_of(row: dict, path_session_id, fallback_cwd=None) -> dict:
    sid = row.get("sessionId") or path_session_id
    return {
        "session_id": str(sid),
        "cwd": row.get("cwd") or fallback_cwd,
        "title": None,
        "origin": None,
        "parent_session": None,
        "delegation_depth": 0,
        "agent_preset": None,
        "role": None,
        "created_at": _to_utc(row.get("timestamp")),
        "extra": {"source": "session-meta"},
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
    session = store.get_session(conn, HOST, sid)
    if session is None:
        session = tagger.tag_session({"session_id": sid, "extra": {"header_missing": 1}}, HOST)
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
        """
        nonlocal session
        row_cwd = row.get("cwd")
        if not row_cwd or session.get("cwd"):
            return
        tagged = tagger.tag_session(
            {"session_id": sid, "cwd": row_cwd, "title": session.get("title"),
             "created_at": session.get("started_at")}, HOST)
        tagged["title"] = session.get("title") or tagged.get("title")
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
                header = _header_of(payload, sid)
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
