#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""治理与体检：`forget` / `rebuild` / `doctor` / 审计清理（原始对话语料层 · T05 · 设计 §8）。

三条不可让步的口径
----------------
1. **tombstone 先写后删**（D14）。反过来的话，进程在"删了段、还没写台账"之间被杀，
   下次 `rebuild` 就把删掉的东西全部复活——而且是静默复活：库里看起来"又是完整的一份"。
   先写台账，最坏结果是"台账说有、库里没删"，那是**可解释**的，`forget` 重跑即收敛。

2. **rebuild 跳过 tombstone**（C-P0-8 ①）。投影可以从源日志重建，这是语料层的优点；
   优点同时也是隐私缺口——"重建"必须尊重删除意愿，否则 `forget` 只是一次性动作。

3. **审计永不存 query 原文**（C-P0-8 ②）。`corpus_access_log` 只有 `query_hash`
   与 `qlen`：能回答"有人搜过多长的串"，不能回答"他搜了什么"。

为什么 `rebuild` 不自己解析日志
-----------------------------
重放走的是**采集管线本身**（`ingest_fn` 由门面注入）：另写一份"重放用的解析"一定会
与采集侧漂开，而漂开的表现是"重建后段数少了 3%"——这种失真没人会当场发现。
所以这里只做三件编排工作：清空目标会话的段、把游标退回 0、按台账把不该回来的再删掉。
"""
from __future__ import annotations

import datetime

import fish_corpus_redact as redact_mod
import fish_corpus_store as store

SCOPE_SESSION = "session"
SCOPE_HOST = "host"
SCOPE_POINTER = "pointer"
SCOPE_RANGE = "range"
SCOPES = (SCOPE_SESSION, SCOPE_HOST, SCOPE_POINTER, SCOPE_RANGE)

ACCESS_LOG_KEEP_DAYS = 30
DOCTOR_SAMPLE_LIMIT = 10

# 体检要扫的**全部可回显文本列**（S2）：(表, 列, 行标识标签)。
# 为什么不能只扫 `corpus_segment.text`：`title` / `cwd` / `project_note` /
# `degraded.reason` / `source.path` 都会出现在回执、推送行与 `corpus stats` 输出里，
# 脏数据在这些列上照样外泄——旧体检对它们完全"看不见"（实测：title 带 token 仍全绿）。
DOCTOR_TEXT_COLUMNS = (
    ("corpus_session", "title", "title"),
    ("corpus_session", "cwd", "cwd"),
    ("corpus_session", "project_note", "note"),
    ("corpus_degraded", "reason", "degraded"),
    ("corpus_source", "path", "source"),
)


class GovError(ValueError):
    """治理动作的参数错误（scope 形态不认识、range 边界不是 UTC 等）。"""


# ------------------------------------------------------------------ 台账


def load_tombstones(conn, scope_type=None) -> list:
    return store.list_tombstones(conn, scope_type)


def _skip_sets(tombstones) -> tuple:
    """台账 → `(跳过的会话集合, 跳过整个宿主的集合)`（**解析只有一份**：`store`）。

    为什么不在这里也解析 `pointer`/`range`：那是**段级**作用域，落在段表上，
    采集/重放统一由 `store.purge_segment_tombstones` 收口——三处各解析一遍，
    迟早有一侧漏掉（复审 🟠-1 的成因就是这个）。
    """
    scopes = store.scopes_from_tombstones(tombstones)
    return scopes["sessions"], scopes["hosts"]


# ------------------------------------------------------------------ forget


def _normalize_scope(conn, scope) -> dict:
    """把调用方给的 scope 归一成 `{type, scope_key, where, args, label}`。

    scope 的四种形态（设计 §8）：
      `{"type":"session","host":h|None,"session_id":sid}`
      `{"type":"host","host":h}`
      `{"type":"pointer","pointer":p}`
      `{"type":"range","host":h|None,"from":A,"to":B}`
    未知形态/缺字段 → `GovError`（**绝不"猜一个最像的"**：猜错就是删错数据）。
    """
    if not isinstance(scope, dict):
        raise GovError("scope 必须是 dict，收到 %r" % type(scope).__name__)
    kind = str(scope.get("type") or "").strip().lower()
    host = (scope.get("host") or "").strip() or None
    if kind == SCOPE_SESSION:
        sid = str(scope.get("session_id") or "").strip()
        if not sid:
            raise GovError("session 范围需要 session_id")
        where = "session_id=?"
        args = [sid]
        if host:
            where = "host=? AND " + where
            args = [host, sid]
        key = store.session_key(host or "*", sid)
        return {"type": kind, "scope_key": key, "where": where, "args": args,
                "label": "会话 %s%s" % (sid, "" if host else "（所有宿主）")}
    if kind == SCOPE_HOST:
        if not host:
            raise GovError("host 范围需要 host")
        return {"type": kind, "scope_key": host, "where": "host=?", "args": [host],
                "label": "宿主 %s 的全部语料" % host}
    if kind == SCOPE_POINTER:
        pointer = str(scope.get("pointer") or "").strip()
        if not pointer:
            raise GovError("pointer 范围需要 pointer")
        parsed = store.resolve_pointer(conn, pointer)
        where = "host=? AND session_id=? AND loc=? AND part=?"
        args = [parsed["host"], parsed["session_id"], parsed["loc"], parsed["part"]]
        key = store.canonical_pointer(parsed["host"], parsed["session_id"],
                                      parsed["loc"], parsed["part"])
        return {"type": kind, "scope_key": key, "where": where, "args": args,
                "label": "单条 %s" % key}
    if kind == SCOPE_RANGE:
        start, end = str(scope.get("from") or ""), str(scope.get("to") or "")
        for name, value in (("from", start), ("to", end)):
            if not value:
                raise GovError("range 范围需要 %s（UTC，形如 2026-10-01T00:00:00Z）" % name)
            if not store.is_utc_stamp(value):
                raise GovError("range.%s 必须是 UTC 'Z' 形式，收到 %r"
                               "（本地时间会静默差 8 小时）" % (name, value))
        where, args = "occurred_at>=? AND occurred_at<=?", [start, end]
        if host:
            where = "host=? AND " + where
            args = [host, start, end]
        key = "%s:%s..%s" % (host or "*", start, end)
        return {"type": kind, "scope_key": key, "where": where, "args": args,
                "label": "区间 %s ~ %s%s" % (start, end, "" if host else "（所有宿主）")}
    raise GovError("不认识的 scope.type：%r（只认 %s）" % (kind, "/".join(SCOPES)))


def _ids_for(conn, where, args) -> list:
    return [int(row[0]) for row in conn.execute(
        "SELECT id FROM corpus_segment WHERE " + where, args).fetchall()]


def _sessions_for(conn, where, args) -> list:
    return [(row[0], row[1]) for row in conn.execute(
        "SELECT DISTINCT host, session_id FROM corpus_segment WHERE " + where,
        args).fetchall()]


def forget(conn, scope, *, reason=None, dry_run=False, via="api", citizen_id=None,
           session_key=None, erase=True) -> dict:
    """删一段/一会话/一宿主/一时间区间：**先写台账，再删段**，最后落审计。

    返回 `{scope, planned, segments_deleted, tombstone_key, dry_run, erase}`。
    `dry_run=True` 只报计划（连台账都不写）。

    M1（2026-10-27）：删完还要**墓碑化**会话行（清 title/cwd/workspace + 标
    `project_source='forgotten'`）。不清标题与路径，`forget` 只删掉了正文，
    "这段对话讲过 XX 凭证"这句话还挂在标题里，检索/推送都还在回显它。
    M5：最后 `secure_erase`（secure_delete + VACUUM + WAL truncate）——`DELETE`
    只把页标成空闲，明文仍留在 db/`-wal` 的字节里。
    安全 DEL-01（2026-10-27）：擦除回执带 `ok`——`checkpoint busy=1`（有并发读者、
    WAL 没截断）**不再算成功**，由 `erase.error` 说清原因并重试过一次；CLI 必须打印。
    """
    plan = _normalize_scope(conn, scope)
    ids = _ids_for(conn, plan["where"], plan["args"])
    targets = _sessions_for(conn, plan["where"], plan["args"])
    result = {"scope": {"type": plan["type"], "key": plan["scope_key"],
                        "label": plan["label"]},
              "planned": len(ids), "segments_deleted": 0,
              "tombstone_key": plan["scope_key"], "dry_run": bool(dry_run),
              "sessions_touched": len(targets), "erase": None, "erase_ok": None}
    if dry_run:
        return result
    with store.write_txn(conn):
        # ① 台账先写（D14）：崩溃窗口里最坏是"记了没删"，可以重跑收敛。
        store.upsert_tombstone(conn, plan["type"], plan["scope_key"], reason=reason,
                               segments_deleted=len(ids))
        # ② 再删（FTS 与正文同一事务：索引里留着已删段的 token 就是"搜得到读不到"）
        deleted = store.delete_segments(conn, ids)
        for host, sid in targets:
            store.refresh_session_counts(conn, host, sid)
            # 墓碑化**只在整份会话被删时**做（`session`/`host` 作用域）。`pointer`/`range`
            # 删的是段，标题/cwd 仍属于会话里**没被删的那部分**——一起抹掉就是越权删除，
            # 而且重采后标题会"回来"，让删除看起来生效过（2026-10-27 复审 🟠-1）。
            if plan["type"] in (SCOPE_SESSION, SCOPE_HOST):
                store.mark_session_forgotten(conn, host, sid)
        store.write_access_log(conn, action="forget", via=via, citizen_id=citizen_id,
                               session_key=session_key, result_count=deleted,
                               pointers=_json_pointers(ids))
    result["segments_deleted"] = deleted
    if erase:
        # ③ 物理擦除（必须在事务外：VACUUM 不能在事务里跑）
        result["erase"] = store.secure_erase(conn)
        result["erase_ok"] = bool((result["erase"] or {}).get("ok"))
    return result


def _json_pointers(ids, limit=20) -> str:
    return ("%d 段" % len(ids)) if len(ids) > limit else ",".join(str(i) for i in ids)


# ------------------------------------------------------------------ rebuild


def _scope_sources(sources, scope=None, host=None) -> list:
    """按 scope 过滤游标行（rebuild 的目标集合）。"""
    if host:
        sources = [row for row in sources if row["host"] == host]
    if not scope:
        return list(sources)
    kind = str(scope.get("type") or "").strip().lower()
    if kind == SCOPE_SESSION:
        sid = str(scope.get("session_id") or "")
        host_filter = (scope.get("host") or "").strip() or None
        return [row for row in sources if row["session_id"] == sid
                and (host_filter is None or row["host"] == host_filter)]
    if kind == SCOPE_HOST:
        want = str(scope.get("host") or "")
        return [row for row in sources if row["host"] == want]
    return list(sources)


def rebuild(conn, root, *, ingest_fn, scope=None, dry_run=True, hosts=(), roots=None,
            project_of=None, citizen_of=None, candidates_of=None, host=None, limit=None,
            via="api", citizen_id=None, session_key=None) -> dict:
    """全量重放（**跳过 tombstone**）：清段 → 退游标 → 重采 → 补删台账命中。

    `ingest_fn` 由门面注入（设计 §2 依赖方向：gov 不许 import 门面，
    否则 `fish_corpus → gov → fish_corpus` 是环）。
    """
    tombstones = load_tombstones(conn)
    skip_sessions, skip_hosts = _skip_sets(tombstones)
    scope = scope if isinstance(scope, dict) and scope else None
    if scope and str(scope.get("type") or "").lower() == SCOPE_SESSION:
        pass                                    # 允许"只重建一个会话"
    sources = _scope_sources(store.list_sources(conn, host), scope, host)
    planned, tombstoned = 0, 0
    keep, skipped = [], []
    for row in sources:
        key = (row["host"], row["session_id"])
        count = store.count_segments(conn, *key)
        # 台账判据只认 store 那一份（评审 🟠-1）：`*:<sid>`（CLI 不带 host 的 `forget
        # session:<sid>`）在那里展开成"所有宿主"，重放才不会把它复活。
        if store.is_session_forgotten(conn, key[0], key[1], skip_sessions, skip_hosts):
            tombstoned += count
            skipped.append({"host": row["host"], "session_id": row["session_id"],
                            "segments": count})
            continue
        planned += count
        keep.append(row)
    result = {"dry_run": bool(dry_run), "scope": scope or {"type": "all"},
              "sessions": len(sources), "sessions_replayed": len(keep),
              "planned": planned, "written": 0, "segments_now": sum(
                  store.count_segments(conn, r["host"], r["session_id"]) for r in keep),
              "skipped_by_tombstone": tombstoned,
              "sessions_skipped_by_tombstone": len(skipped),
              "skipped_sessions": skipped[:20],
              "tombstones": len(tombstones), "via": via}
    if dry_run:
        return result

    # ① 清段 + 游标退回 0（"全量重放"必须真的从头读，否则 unchanged 会把重建变成空转）
    for row in keep:
        ids = _ids_for(conn, "host=? AND session_id=?", [row["host"], row["session_id"]])
        with store.write_txn(conn):
            store.delete_segments(conn, ids)
            store.upsert_source(conn, {
                "host": row["host"], "session_id": row["session_id"],
                "path": row["path"], "format": row["format"], "bytes_total": 0,
                "mtime_ns": 0, "events_seen": 0, "segments_kept": 0,
                "last_seq_seen": None, "last_line_seen": None,
                "status": store.SOURCE_OK,
                "first_ingested_at": row.get("first_ingested_at")})
            store.refresh_session_counts(conn, row["host"], row["session_id"])

    # ② 重采（tombstone 命中的会话在 ingest 里就被跳过，不会先复活再删）
    ingest_hosts = tuple(h for h in (hosts or tuple({r["host"] for r in keep}))
                         if h not in skip_hosts)
    ingest_result = {}
    if keep and ingest_hosts:
        ingest_result = ingest_fn(
            hosts=ingest_hosts, root=root, conn=conn, roots=roots, limit=limit,
            use_lock=False, project_of=project_of, citizen_of=citizen_of,
            candidates_of=candidates_of, skip_sessions=skip_sessions)

    # ③ 指针/区间台账：这两种作用域是"段级"的，只能在重采之后按内容再删一次。
    # 解析与删除都走 store 的同一份实现（复审 🟠-1）：`ingest` 收口时已经按同一份
    # 台账回收过，这里再跑一次是幂等兜底（直接调 gov.rebuild 的调用方也覆盖到）。
    purged = store.purge_segment_tombstones(conn)
    result["written"] = int(ingest_result.get("segments_new") or 0)
    result["reaped_by_pointer_tombstone"] = int(purged["deleted"])
    result["tombstone_scopes"] = {"pointers": purged["pointers"],
                                  "ranges": purged["ranges"]}
    result["degraded"] = ingest_result.get("degraded") or []
    result["segments_now"] = sum(
        store.count_segments(conn, r["host"], r["session_id"]) for r in keep)
    store.write_access_log(conn, action="rebuild", via=via, citizen_id=citizen_id,
                           session_key=session_key, result_count=result["segments_now"])
    conn.commit()
    return result


# ------------------------------------------------------------------ doctor


def doctor(conn, *, root=None, via="api", citizen_id=None, session_key=None,
           record=True, max_samples=DOCTOR_SAMPLE_LIMIT) -> dict:
    """全库体检：**凭证形态命中必须 0**；关掉脱敏（`FISH_CORPUS_NO_REDACT=1`）必须红。

    为什么"关脱敏也要红"（C-P0-7 红①）：那条开关能让落盘前脱敏失效，
    一个"把秘密写进库"的配置如果体检还说 OK，这条判据就只是装饰品。
    """
    reasons, hits = [], []
    if redact_mod.disabled():
        reasons.append("redact_disabled")
    scanned = 0
    scanned_extra = 0
    for row in conn.execute(
            "SELECT id, host, session_id, loc, part, text FROM corpus_segment"):
        scanned += 1
        found = redact_mod.scan(row["text"])
        if found:
            hits.append({"pointer": store.canonical_pointer(
                row["host"], row["session_id"], row["loc"], row["part"]),
                "column": "corpus_segment.text",
                "types": sorted({name for name, _s, _e in found})})
            if len(hits) >= max_samples:
                break
    # S2：**可回显文本列**全部扫（旧版只扫正文，title/cwd/reason 带 token 仍全绿）。
    # 计数分成 `scanned`（段）与 `scanned_extra`（其它列）：既有判据锁着"scanned==段数"，
    # 把两种口径混成一个数会让那条判据只能靠改数字才过。
    for table, column, tag in DOCTOR_TEXT_COLUMNS:
        if len(hits) >= max_samples:
            break
        sql = ("SELECT host, session_id, %s AS value FROM %s WHERE %s IS NOT NULL"
               % (column, table, column))
        try:
            rows = conn.execute(sql).fetchall()
        except Exception:                       # noqa: BLE001 —— 旧库缺列就跳过
            continue
        for row in rows:
            scanned_extra += 1
            found = redact_mod.scan(str(row["value"]))
            if not found:
                continue
            hits.append({
                "pointer": "%s#%s:%s" % (store.session_key(row["host"], row["session_id"]),
                                        tag, column),
                "column": "%s.%s" % (table, column),
                "types": sorted({name for name, _s, _e in found})})
            if len(hits) >= max_samples:
                break
    if hits:
        reasons.append("plaintext_secrets")
    segments = int(conn.execute("SELECT COUNT(*) FROM corpus_segment").fetchone()[0])
    fts = int(conn.execute("SELECT COUNT(*) FROM corpus_fts").fetchone()[0])
    fts_gap = segments - fts
    if fts_gap:
        reasons.append("fts_gap")
    tombstones = load_tombstones(conn)
    degraded = store.list_degraded(conn)
    orphans = [row for row in store.list_sources(conn) if row.get("status") == store.SOURCE_ORPHAN]
    access_rows = int(conn.execute(
        "SELECT COUNT(*) FROM corpus_access_log").fetchone()[0])
    unmapped = int(conn.execute(
        "SELECT COUNT(*) FROM corpus_session WHERE project=''").fetchone()[0])
    sessions = int(conn.execute("SELECT COUNT(*) FROM corpus_session").fetchone()[0])
    # 停滞哨兵（C-P0-9 红②）：读数在 `corpus_meta`，键名从 store 取——与 `fish_corpus`
    # 同一份定义（复审 🟡：报告说"doctor 打印 stall"，代码里其实一处都没读，现在对齐）。
    try:
        stall_streak = int(store.get_meta(conn, store.META_STALL_STREAK, "0") or 0)
    except (TypeError, ValueError):
        stall_streak = 0
    stall_alarm = store.get_meta(conn, store.META_STALL_ALARM) or None
    if stall_alarm:
        reasons.append("stalled")
    result = {
        "ok": not reasons, "reasons": reasons, "as_of": store.now_utc(),
        "segments": segments, "sessions": sessions, "scanned": scanned,
        "scanned_extra": scanned_extra,
        "secret_hits": hits, "secret_hit_count": len(hits), "fts_gap": fts_gap,
        "tombstones": len(tombstones), "degraded": degraded,
        "orphans": len(orphans), "access_log_rows": access_rows,
        "unmapped_sessions": unmapped, "redact_disabled": redact_mod.disabled(),
        "rules": list(redact_mod.RULE_NAMES), "via": via,
        "alarm": stall_alarm,
        "stall": {"zero_streak": stall_streak, "alarm": stall_alarm},
        "db_exists": bool(root is not None and store.db_path(root).exists()),
    }
    if record:
        try:
            store.write_access_log(conn, action="doctor", via=via,
                                   citizen_id=citizen_id, session_key=session_key,
                                   result_count=len(reasons))
            conn.commit()
        except Exception:                       # noqa: BLE001 —— 审计失败不改变体检结论
            pass
    result["hits"] = result["secret_hits"]      # 兼容旧字段名
    return result


# ------------------------------------------------------------------ 审计清理


def prune_access_log(conn, keep_days=ACCESS_LOG_KEEP_DAYS, *, dry_run=False,
                     now=None) -> dict:
    """删掉 `keep_days` 之前的访问审计（日报一步）。返回 `{cutoff, planned, deleted}`。"""
    days = int(keep_days or 0)
    if days <= 0:
        return {"cutoff": None, "planned": 0, "deleted": 0, "skipped": "disabled"}
    now_dt = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = (now_dt - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    planned = int(conn.execute(
        "SELECT COUNT(*) FROM corpus_access_log WHERE ts < ?", (cutoff,)).fetchone()[0])
    if dry_run:
        return {"cutoff": cutoff, "planned": planned, "deleted": 0, "dry_run": True}
    cur = conn.execute("DELETE FROM corpus_access_log WHERE ts < ?", (cutoff,))
    conn.commit()
    return {"cutoff": cutoff, "planned": planned, "deleted": int(cur.rowcount or 0)}
