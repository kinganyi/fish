#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fish 引入 git：写库层（2026-11-20 · Phase 3）。

## 为什么从 `fish_git.py` 拆出来

`fish_git.py` 一度同时承担六个职责（发现 / 身份归一 / 探针 / 扫描 / 写库 /
观测），涨到 589 行、超了它的 J4 窗口（250–550）。

**抬窗是最差的选项**——那是放松门禁。正确做法是承认它本来就有两个职责：

· `fish_git.py`  —— **读**：从磁盘/仓库读事实（发现器、身份归一、L0/L1 探针、扫描）
· `fish_git_store.py` —— **写**：把事实落到五张表（仓库 / 工作副本 / 观测快照）

分层方向：**store → git 是向下**（写层依赖读层的解析结果），反向绝不允许。
所以 `fish_git.py` 一个字都不 import 本模块。

## 写库层的三条纪律

**① `identity` 为 NULL 时不能做等值查找。**
SQL 里 `NULL = NULL` 恒为 false。无远端仓库（空仓库、本地仓库）如果按
`WHERE identity = NULL` 查，每次都会新建一行——同一个仓库被算成 N 个。
所以 `ensure_repo` 在 identity 为空时改用「这个路径已属于哪个 repo」反向锚定。

**② 一个仓库 N 份工作副本。**
脏净/分支/ahead 是**工作副本**的属性，不是仓库的属性。两份 clone 可以一个净
一个脏。`git_checkout.path_key` 的 UNIQUE 保证同一物理路径不登记两次，而
`repo_id` 相同保证「两份 clone 是同一个仓库」（J2 判据）。

**③ 四态在写入时定型。**
`clean` / `dirty` / `unknown` / `degraded`。浅克隆与 partial clone 一律降级为
`degraded`——它们的「净」不可靠（浅仓 worktree 可能缺对象）。
「没采到」是 `unknown`，**绝不是 clean**（J4 的正反两向断言靠这条）。
"""
import sqlite3
import time as _time
from datetime import datetime, timezone
from pathlib import Path

import fish_common
import fish_db
import fish_git
import fish_surface


def _iso(ts):
    """git 的 unix 时间戳 → UTC ISO（`%Y-%m-%dT%H:%M:%SZ`）。判不到返回 None。"""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return None


def ensure_repo(conn, *, identity=None, identity_kind="unknown",
                identity_source="discovered", display_name=None, root=None,
                now=None):
    """找到或创建仓库行，返回 `repo_id`。

    查找顺序（**顺序本身是判据**，见 J2/J3）：

    1. `identity` 非空 → 按 identity 查。跨路径稳定：同一个远端的两份独立克隆
       会命中同一行。这是 J2 的判据基础。
    2. identity 为空（无远端仓库）→ **按已有 checkout 反查**：如果这个 root
       已经登记过工作副本，就复用它的 repo_id。
       为什么不能 `SELECT ... WHERE identity=NULL`：SQL 里 `NULL = NULL` 恒为
       false，那样每次都会新建一行，同一个无远端仓库会被算成 N 个。
    3. 都没有 → 新建。

    `identity_source` 记的是「这个身份是怎么来的」（manual/discovered/inferred）——
    不是装饰：手工登记与自动发现的身份在冲突时优先级不同。
    """
    stamp = now or fish_common.now_utc()
    if identity:
        row = conn.execute(
            "SELECT repo_id FROM git_repo WHERE identity=?", (identity,)).fetchone()
        if row:
            conn.execute("UPDATE git_repo SET updated_at=? WHERE repo_id=?",
                         (stamp, row[0]))
            return row[0]

    # 无稳定身份：靠「这个路径已属于哪个仓库」反查（NULL 不能做等值比较）
    if root:
        norm = fish_surface.normalize_path(root)
        if norm:
            row = conn.execute(
                "SELECT repo_id FROM git_checkout WHERE path_key=?", (norm,)).fetchone()
            if row:
                return row[0]

    cur = conn.execute(
        "INSERT INTO git_repo (identity, identity_kind, identity_source, "
        "display_name, ownership, created_at, updated_at) "
        "VALUES (?,?,?,?, 'unknown', ?,?)",
        (identity, identity_kind, identity_source,
         display_name or (Path(root).name if root else None), stamp, stamp))
    return cur.lastrowid


def ensure_checkout(conn, *, repo_id, path, now=None):
    """找到或创建工作副本行，返回 `checkout_id`。

    `path_key` 是 UNIQUE：同一个物理路径不会登记两次（哪怕大小写或 junction
    写法不同——`normalize_path` 已经统一过）。这是「一个仓库 N 份 checkout」
    能被正确表达的保证：同一 identity 两个 path = 两个 checkout 一个 repo。
    """
    stamp = now or fish_common.now_utc()
    norm = fish_surface.normalize_path(path)
    if not norm:
        return None
    row = conn.execute(
        "SELECT checkout_id, repo_id FROM git_checkout WHERE path_key=?",
        (norm,)).fetchone()
    if row:
        # 路径不变但 repo 归属可能变（比如这次才加上远端）——以调用方为准
        if repo_id and row[1] != repo_id:
            conn.execute("UPDATE git_checkout SET repo_id=? WHERE checkout_id=?",
                         (repo_id, row[0]))
        conn.execute("UPDATE git_checkout SET last_seen_at=?, missing_since=NULL "
                     "WHERE checkout_id=?", (stamp, row[0]))
        return row[0]
    cur = conn.execute(
        "INSERT INTO git_checkout (repo_id, path, path_key, is_primary, last_seen_at) "
        "VALUES (?,?,?, 0, ?)", (repo_id, norm, norm, stamp))
    return cur.lastrowid


def record_obs(conn, *, checkout_id, probe, shallow=False, now=None, probe_ms=None):
    """把一次 L1 探针结果落成 `git_obs` 行，返回 `id`（重复采样同一秒则跳过）。

    四态在这里**定型**（J4 的判据基础）：
    · `probe["state"]` 已经是 clean/dirty/degraded/unknown 之一；
    · **浅克隆一律降级为 degraded**——它们的「净」不可靠。
    返回 None 表示没有写入（同一 checkout 同一秒已有采样，UNIQUE 约束）。
    """
    stamp = now or fish_common.now_utc()
    state = probe.get("state") or "unknown"
    degraded_reason = probe.get("degraded_reason")
    if shallow and state in ("clean", "dirty"):
        state = "degraded"
        degraded_reason = degraded_reason or "shallow"
    try:
        cur = conn.execute(
            "INSERT INTO git_obs (checkout_id, sampled_at, branch, head_sha, upstream, "
            "ahead, behind, dirty, staged_count, unstaged_count, untracked_count, "
            "state, degraded_reason, probe_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (checkout_id, stamp, probe.get("branch"), probe.get("head_sha"),
             probe.get("upstream"), probe.get("ahead"), probe.get("behind"),
             probe.get("dirty"), probe.get("staged"), probe.get("unstaged"),
             probe.get("untracked"), state, degraded_reason, probe_ms))
    except sqlite3.IntegrityError:
        # A档：同一 checkout 同一秒重复采样撞 UNIQUE——预期行为，不是错误。
        # 只捕这一种；其它 sqlite3.Error 要经 warn_ignored 出声（下面那条）。
        return None
    except sqlite3.Error as exc:
        fish_common.warn_ignored("fish_git_store.py record_obs", exc)
        return None
    return cur.lastrowid


def record_commit(conn, *, root, repo_id, branch=None, now=None):
    """采集 HEAD 的提交，写事件（真源）+ 投影（派生表）。返回 dict 或 None。

    ## 幂等与增量：**用 sha，不用时间**

    J9 要求「commit 增量不重不漏，两个方向都测」。这里刻意**不使用任何时间水位**：

    · **不漏**：`git commit --amend` 会换 sha，但 author-date 通常被保留
      （改的是 committer-date）。按 author-date 做水位会**漏采**（实测 0 条 vs
      按 ref 判 1 条）；按 committer-date 做水位又会在 rebase 后**重采**。
    · **不重**：`git_commit.sha` 是 PRIMARY KEY，同一 sha 第二次进来直接跳过。

    所以两个方向都由「sha 是主键」这一个事实同时保证，不需要额外水位表。

    ## 先写事件、再写投影

    `events` 是真相源，`git_commit` 可由事件重放重建。事务边界由调用方控制
    （`append_event(commit=False)`），这样「事件 + 投影」要么一起成、要么一起不成。
    """
    info = fish_git.read_commit(root, "HEAD")
    if not info:
        return None
    sha = info["sha"]
    if conn.execute("SELECT 1 FROM git_commit WHERE sha=?", (sha,)).fetchone():
        return {"sha": sha, "recorded": False, "reason": "already_present"}
    stamp = now or fish_common.now_utc()
    event_id = fish_db.append_event(
        conn, "git.commit",
        actor={"kind": "system", "id": "fish_git"},
        subject={"repo_id": repo_id, "sha": sha},
        payload={"sha": sha, "repo_id": repo_id,
                 "content_root": info["content_root"],
                 "branch_seen": branch, "subject": info["subject"]},
        cause="hook:git commit", commit=False)
    conn.execute(
        "INSERT OR IGNORE INTO git_commit (sha, repo_id, authored_at, committed_at, "
        "author_email, author_name, subject, is_merge, branch_seen, content_root, "
        "event_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (sha, repo_id, _iso(info["authored_at"]) or stamp,
         _iso(info["committed_at"]) or stamp,
         info["author_email"], info["author_name"], info["subject"],
         info["is_merge"], branch, info["content_root"], event_id))
    return {"sha": sha, "recorded": True, "event_id": event_id}


def rebuild_commit_projection(conn):
    """从 `events` 重放 `git.commit` 事件，重建 `git_commit` 投影（P4-1 判据）。

    这是「投影可从事件重建」的可执行证明：清空投影表后调它，行数应当回到重放前的值。
    **只重建投影，不动 events**（events 是不可变的真源）。
    返回写入的行数。
    """
    rows = conn.execute(
        "SELECT event_id, payload FROM events WHERE type='git.commit' ORDER BY seq"
    ).fetchall()
    restored = 0
    for event_id, payload_json in rows:
        try:
            import json as _json
            p = _json.loads(payload_json)
        except Exception:                  # 坏 payload：跳过，不猜
            continue
        sha, repo_id = p.get("sha"), p.get("repo_id")
        if not sha or not repo_id:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO git_commit (sha, repo_id, authored_at, committed_at, "
            "subject, content_root, event_id) VALUES (?,?,?,?,?,?,?)",
            (sha, repo_id, p.get("committed_at") or "", p.get("committed_at") or "",
             p.get("subject"), p.get("content_root"), event_id))
        restored += cur.rowcount or 0
    return restored


def link_create(conn, *, src_kind, src_id, dst_kind, dst_id, rel,
                source="explicit", confidence=1.0, evidence=None,
                content_hash=None, now=None):
    """建立一条记忆↔提交（或 ↔仓库/工作副本）的关联，返回 `git_link.id`。

    **为什么独立表不扩展 `links`**：`links` 两端必须是 `code_index` 里的编码
    （`gov_apply.py` 的健康检查 + `fish_maintenance.py` 的 24h 维护都强制），
    把 commit SHA 塞进去会立刻变成悬空边、24 小时后被 `suppressed=1` 压掉。

    重复建立同一条关联（同一 src/dst/rel）返回已有的 id，不报错——
    调用方（hook / 手工工具）不需要先查再建。
    """
    stamp = now or fish_common.now_utc()
    row = conn.execute(
        "SELECT id, removed_at FROM git_link WHERE src_kind=? AND src_id=? AND "
        "dst_kind=? AND dst_id=? AND rel=?",
        (src_kind, src_id, dst_kind, dst_id, rel)).fetchone()
    if row:
        # 已存在：若曾被软删，这次建立视为「恢复」
        if row[1]:
            conn.execute("UPDATE git_link SET removed_at=NULL WHERE id=?", (row[0],))
        return row[0]
    cur = conn.execute(
        "INSERT INTO git_link (src_kind, src_id, dst_kind, dst_id, rel, source, "
        "confidence, evidence, content_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (src_kind, src_id, dst_kind, dst_id, rel, source, confidence,
         evidence, content_hash, stamp))
    return cur.lastrowid


def link_remove(conn, link_id, *, now=None):
    """**软删除**一条关联（写 `removed_at`），返回是否命中。

    J10 的判据就在这一行：物理删除之后「撤销」无从谈起（追溯不到原来关联过什么），
    所以这里只打时间戳。`link_restore` 把时间戳清回 NULL 即完成回滚。
    """
    cur = conn.execute("UPDATE git_link SET removed_at=? WHERE id=? AND removed_at IS NULL",
                       (now or fish_common.now_utc(), link_id))
    return bool(cur.rowcount)


def link_restore(conn, link_id):
    """撤销软删除（回滚），返回是否命中。"""
    cur = conn.execute("UPDATE git_link SET removed_at=NULL WHERE id=? AND removed_at IS NOT NULL",
                       (link_id,))
    return bool(cur.rowcount)


def links_of(conn, *, src_kind=None, src_id=None, dst_kind=None, dst_id=None,
             include_removed=False):
    """按 src 或 dst 查关联；默认**不含**已软删的。

    两个维度都支持，因为真实问题两个方向都会问：「这条记忆改过哪些提交」
    和「这个提交实现了哪些记忆」。
    """
    where, params = [], []
    for col, val in (("src_kind", src_kind), ("src_id", src_id),
                     ("dst_kind", dst_kind), ("dst_id", dst_id)):
        if val is not None:
            where.append(f"{col}=?")
            params.append(val)
    if not include_removed:
        where.append("removed_at IS NULL")
    sql = ("SELECT id, src_kind, src_id, dst_kind, dst_id, rel, source, confidence, "
           "evidence, content_hash, created_at, removed_at FROM git_link")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id"
    return [dict(zip(("id", "src_kind", "src_id", "dst_kind", "dst_id", "rel",
                      "source", "confidence", "evidence", "content_hash",
                      "created_at", "removed_at"), r)) for r in conn.execute(sql, params)]


def latest_obs(conn, checkout_id):
    """读某工作副本的**最近一次**观测；没有返回 None。

    **没有就是 None，不是「干净」**（J4 的反向断言靠这条）。
    """
    row = conn.execute(
        "SELECT sampled_at, branch, head_sha, dirty, state, degraded_reason, "
        "ahead, behind FROM git_obs WHERE checkout_id=? "
        "ORDER BY sampled_at DESC, id DESC LIMIT 1", (checkout_id,)).fetchone()
    if not row:
        return None
    return {
        "sampled_at": row[0], "branch": row[1], "head_sha": row[2],
        "dirty": row[3], "state": row[4], "degraded_reason": row[5],
        "ahead": row[6], "behind": row[7],
    }


def observe(conn, path, *, now=None):
    """对 `path` 所在仓库做一次完整采集并落库，返回结构化结果（P3-1 入口）。

    它**总是**返回一个 dict（含 `state`），从不抛异常：调用方（hook）需要的是
    「有一次观测」而不是「异常」。但失败不许静默——走 `warn_ignored`，并把原因
    写进 `degraded_reason`（形如 `db_error:IntegrityError`）。
    """
    started = _time.time()
    norm_root = fish_git.discover_root(path)
    result = {"repo_id": None, "checkout_id": None, "root": norm_root,
              "identity": None, "identity_kind": "unknown", "obs_id": None,
              "state": "unknown", "degraded_reason": None, "probe_ms": None}
    if not norm_root:
        result["degraded_reason"] = "not_a_repo"
        return result

    remotes = fish_git.read_remotes(norm_root)
    identity, url_kind = (None, "unknown")
    if remotes:
        identity, url_kind = fish_git.normalize_remote_url(remotes[0][1])
    result["identity"], result["identity_kind"] = identity, url_kind

    try:
        repo_id = ensure_repo(conn, identity=identity,
                              identity_kind=fish_git.identity_kind_of(url_kind),
                              root=norm_root, now=now)
        result["repo_id"] = repo_id
        for name, url in remotes:
            conn.execute(
                "INSERT INTO git_remote (repo_id, name, url, url_kind, created_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(repo_id, name) DO UPDATE SET url=excluded.url",
                (repo_id, name, url, fish_git.normalize_remote_url(url)[1],
                 now or fish_common.now_utc()))
        checkout_id = ensure_checkout(conn, repo_id=repo_id, path=norm_root, now=now)
        result["checkout_id"] = checkout_id
        if checkout_id:
            probe = fish_git.probe_l1(norm_root)
            probe_ms = int((_time.time() - started) * 1000)
            result["probe_ms"] = probe_ms
            result["state"] = probe["state"]
            result["degraded_reason"] = probe.get("degraded_reason")
            result["obs_id"] = record_obs(conn, checkout_id=checkout_id, probe=probe,
                                          shallow=fish_git.probe_shallow(norm_root),
                                          now=now, probe_ms=probe_ms)
    except Exception as exc:               # 旁路失败不拖垮调用方，但不许静默
        fish_common.warn_ignored("fish_git_store.py observe", exc)
        result["degraded_reason"] = "db_error:%s" % type(exc).__name__
        result["state"] = "unknown"
    return result
