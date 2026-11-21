#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆作废：把「元数据还在、正文已丢」的条目正式清理（2026-11-21）。

## 为什么需要这个模块

2026-11-20 凌晨一次复制迁移没搬全，导致 10-28~11-18 这 22 天写入的 318 条
记忆**正文文件永久丢失**（根因见 85 号调查报告）。它们仍然 `status='active'`，
于是 `recall` / `inject` 会把它们当正常结果端出来——**"看起来有、点进去是空的"**。

## 为什么复用 `memory.retired` 而不是物理删除

· `events` 是 append-only（`ev_no_delete` 触发器），物理删会被拒；
· 就算能删，`rebuild_projection` 会从 `memory.recorded` 把它们重建回来；
· `memory.retired`（ADR-003 的 `withdrawn` 状态）是**已有的**入口，
  `rebuild_projection` 已经支持重放它（`fish_rebuild.py:391-401`）。

**所以「清理」在这里的正确语义是「正式作废 + 记账」，不是抹掉痕迹。**
用户 2026-11-21 拍板"全部清理吧，避免以后推送和存储垃圾内容"——
作废满足这个目的（recall 不再返回、推送不再产出），同时保留了可追溯性。

## 分层

`purge → fish_db`（`append_event`）+ `fish_common`（`now_utc`）。
**不 import `fish`**——清理是纯 SQL + 状态机操作，不该被门面的加载期副作用绑架。
"""
import sqlite3
from pathlib import Path

from fish_common import now_utc, warn_ignored
from fish_db import append_event

# 作废原因写死在模块里而不是让调用方随便传：这句会进事件流，
# 半年后有人问"这 318 条为什么不见了"，答案必须自解释。
DEFAULT_REASON = (
    "正文文件丢失：2026-11-20 迁移未搬全（10-28~11-18 批次）。"
    "元数据与事件完好，但正文已不可恢复；本条据用户指示正式作废。"
)


def scan_broken(conn, root):
    """扫出「`file_path` 非空但磁盘上不存在」的记忆。

    `root` 是 **data 目录**（`file_path` 形如 `projects/<项目>/<文件名>.md`，
    相对的是 data）。

    ## 为什么返回按日期/项目的分组，而不只是一个计数

    `healthcheck` 早就有「断链 318」这个读数，但它是**一个汇总量**，看不出结构。
    85 号调查里正是按 `recorded_at` 的日期一分，才立刻看出「10-28~11-18
    连续 22 天 100% 断链」——**同一个数字，有没有结构，决定了它能否指向根因。**
    所以这个函数把结构一起交出来。
    """
    root = Path(root)
    # ⚠️ **必须只扫 `status='active'`**：第一版没加这个条件，于是作废完再跑 dry-run
    # 仍然报 318 条——已 `withdrawn` 的条目还被算作"断链"，清理看起来永远没生效。
    # 这不是显示问题：它会让"清理干净了吗"这个判据永远为假。
    rows = conn.execute(
        "SELECT code, project, file_path, recorded_at, status FROM code_index "
        "WHERE status='active' AND file_path IS NOT NULL AND file_path != '' "
        "ORDER BY code").fetchall()
    broken = []
    for code, project, fp, rec, status in rows:
        try:
            exists = (root / fp).exists()
        except OSError:
            # 路径里有非法字符等——那本身也是"读不到"，按断链处理但不吞掉
            exists = False
        if not exists:
            broken.append({"code": code, "project": project, "file_path": fp,
                           "recorded_at": rec, "status": status})
    by_date, by_project = {}, {}
    for b in broken:
        day = (b["recorded_at"] or "")[:10] or "(无日期)"
        by_date[day] = by_date.get(day, 0) + 1
        pj = b["project"] or "(未分类)"
        by_project[pj] = by_project.get(pj, 0) + 1
    return {
        "count": len(broken),
        "codes": [b["code"] for b in broken],
        "items": broken,
        "by_date": dict(sorted(by_date.items())),
        "by_project": dict(sorted(by_project.items(), key=lambda kv: -kv[1])),
    }


def purge_broken(conn, root, *, dry_run=True, reason=None, commit=True):
    """把扫出的断链记忆正式作废（`memory.retired` + `status='withdrawn'`）。

    **默认 `dry_run=True`**：这是不可逆操作（虽然事件可追溯，但 recall 从此
    不再返回它们），默认应当先看一遍再决定。

    每一步的理由：

      · `append_event('memory.retired')` —— 真相源留痕，`rebuild_projection` 会重放它；
      · `UPDATE code_index SET status='withdrawn'` —— 让 `search_memories`
        （它过滤 `status='active'`）立刻不再返回；
      · `DELETE FROM memory_fts WHERE code=?` —— 全文索引是独立表（不是 external
        content），不删的话 `memory_fts MATCH` 仍会命中；
      · **不删正文文件** —— 它本来就不在了，删一个不存在的文件只会制造噪音。
    """
    root = Path(root)
    found = scan_broken(conn, root)
    if dry_run or not found["count"]:
        return {"dry_run": True, "count": found["count"],
                "codes": found["codes"], "by_date": found["by_date"],
                "by_project": found["by_project"], "purged": 0}

    why = reason or DEFAULT_REASON
    stamp = now_utc()
    purged = 0
    fts_removed = 0
    for item in found["items"]:
        code = item["code"]
        append_event(
            conn, "memory.retired",
            # 谁做的：这是系统清理，不是某个 AI 的判断。actor 写 system 才如实。
            {"citizen_id": "system", "evidence_level": "signed"},
            {"kind": "memory", "id": code},
            {"code": code, "status": "withdrawn", "reason": why,
             "file_path": item["file_path"], "purged_at": stamp},
            cause="purge-broken")
        conn.execute("UPDATE code_index SET status='withdrawn' WHERE code=?", (code,))
        # FTS 是独立虚表：不删的话 search_memories 的 MATCH 仍会命中它
        try:
            cur = conn.execute("DELETE FROM memory_fts WHERE code=?", (code,))
            fts_removed += cur.rowcount if cur.rowcount > 0 else 0
        except sqlite3.Error as exc:
            # 索引缺失不该阻断作废（状态已经改了，那才是 recall 的依据），
            # 但**不许静默**——`test_silent_audit` 就是为了拦这种 except: pass。
            warn_ignored(f"fish_purge.py purge_broken fts {code}", exc)
        purged += 1
    if commit:
        conn.commit()
    return {"dry_run": False, "count": found["count"], "purged": purged,
            "fts_removed": fts_removed, "by_date": found["by_date"],
            "by_project": found["by_project"], "reason": why}


def consistency_report(conn, root):
    """事件与正文的一致性巡检（A-3）。

    ## 为什么这个读数比「断链 N」有用

    断链数只说"有多少"，不说"什么时候开始的"。这里把 `memory.recorded`
    事件数、有正文的记忆数、断链数放在一起，并给出断链的日期分布——
    **断链集中在一段时间内 = 迁移/批量操作事故；均匀分布 = 偶发删改。**
    两者的处理方式完全不同。
    """
    root = Path(root)
    try:
        n_events = conn.execute(
            "SELECT count(*) FROM events WHERE type='memory.recorded'").fetchone()[0]
    except sqlite3.Error:
        n_events = 0
    found = scan_broken(conn, root)
    total = conn.execute("SELECT count(*) FROM code_index").fetchone()[0]
    with_body = conn.execute(
        "SELECT count(*) FROM code_index WHERE file_path IS NOT NULL AND file_path != ''"
    ).fetchone()[0] - found["count"]
    return {
        "memory_recorded_events": n_events,
        "code_index_total": total,
        "with_body": with_body,
        "broken": found["count"],
        "broken_by_date": found["by_date"],
        "broken_by_project": found["by_project"],
    }
