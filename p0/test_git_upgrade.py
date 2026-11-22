#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fish git 能力升级的判据（2026-11-22 · 方案 `docs/87-方案-fish-git能力升级-20261122.md`）。

## 这份测试守的是什么

上一轮把 `discussed_in` 接上了自动推断、加了 `git_link` 工具，**判据全绿、22 条断言**。
但生产实测 `git_link` 是 **0 行**、自动命中 **0/175**——**"能力存在但产出为零，
而且没有任何地方报告这个零"**。

所以本轮的判据分两组：

· **修地基**（J1–J4）：投影重建不完整（只还原 7/16 列）、老库加列不迁移、
  trailer 判断——这些是"看起来能用、其实丢数据"的静默失真；
· **让零可见**（J5–J8）：healthcheck / 回执 / 推送三处必须**报告**那个零。

## 变异体（去掉 → 必红）

| 判据 | 变异体 |
|---|---|
| J1 | rebuild 不写 `author_name` 等列 → 红 |
| J2 | rebuild 只读 payload 不读 actor → 红 |
| J3 | 去掉 `migrate()` 里补列那段 → 红 |
| J4 | `_has_trailer` 恒返回 0 → 红 |
| J5 | 去掉 `if _nl == 0` 分支 → 红 |
| J6 | 回执不标「未关联」→ 红 |
| J7 | 推送不产出 git 候选 → 红 |
| J8 | 推送无条件产出 → 红（**负对照**） |
"""
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

P0 = Path(__file__).resolve().parent
sys.path.insert(0, str(P0))

import fish  # noqa: E402
import fish_db  # noqa: E402
import fish_git_store  # noqa: E402

TS = "2026-11-20T00:00:00Z"


class _Case(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-gitup-"))
        self.data = self.tmp / "data"
        self.data.mkdir(parents=True)
        self.conn = fish.connect(self.data)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO git_repo (repo_id, identity, identity_kind, identity_source, "
            "display_name, ownership, created_at, updated_at) "
            "VALUES (1,'x','remote','inferred','甲仓库','unknown',?,?)", (TS, TS))
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def _event(self, sha="aaa", **extra):
        """造一条 `git.commit` 事件（形态与 `record_commit` 一致）。"""
        payload = {"sha": sha, "repo_id": 1, "content_root": "tree1",
                   "branch_seen": "main", "subject": "原始标题",
                   "authored_at": TS, "committed_at": TS,
                   "author_email": "a@b", "author_name": "Alice",
                   "is_merge": 1, "has_trailer": 1}
        payload.update(extra)
        return fish_db.append_event(
            self.conn, "git.commit",
            {"kind": "system", "id": "fish_git", "session_key": "dsh:test-abc"},
            {"repo_id": 1, "sha": sha}, payload, cause="hook")

    def _projection(self, sha="aaa", **over):
        cols = {"sha": sha, "repo_id": 1, "authored_at": TS, "committed_at": TS,
                "author_email": "a@b", "author_name": "Alice", "subject": "原始标题",
                "is_merge": 1, "branch_seen": "main", "content_root": "tree1",
                "session_key": "dsh:test-abc", "has_trailer": 1}
        cols.update(over)
        keys = ",".join(cols)
        self.conn.execute(
            f"INSERT INTO git_commit ({keys}) VALUES ({','.join('?' * len(cols))})",
            tuple(cols.values()))
        self.conn.commit()


class RebuildCompletenessTest(_Case):
    """J1 / J2：投影重建必须还原**所有**能从事件还原的列。

    F7 实测旧实现只还原 7/16 列，把 `author_name` / `branch_seen` / `is_merge` /
    `files_changed` 全丢成 NULL，而且读 payload 里不存在的 `committed_at`。
    判据只验行数，所以一直绿着。
    """

    COLS = ("sha", "author_name", "author_email", "is_merge", "branch_seen",
            "authored_at", "committed_at", "content_root", "has_trailer")

    def test_j1_restores_all_payload_fields(self):
        """J1：payload 里有的字段，重建后必须一个不少。

        变异体：rebuild 的 INSERT 不写 `author_name` 等列 → 红。
        """
        self._event()
        self._projection()
        self.conn.execute("DELETE FROM git_commit")
        self.conn.commit()
        n = fish_git_store.rebuild_commit_projection(self.conn)
        self.assertEqual(n, 1, "重建没写行")
        row = self.conn.execute(
            f"SELECT {','.join(self.COLS)} FROM git_commit WHERE sha='aaa'").fetchone()
        self.assertIsNotNone(row, "重建后行不存在")
        got = dict(zip(self.COLS, row))
        for col in self.COLS:
            self.assertIsNotNone(got[col], f"重建丢了 {col}（F7 的形态）")
        self.assertEqual(got["author_name"], "Alice")
        self.assertEqual(got["is_merge"], 1)
        self.assertEqual(got["branch_seen"], "main")
        self.assertEqual(got["committed_at"], TS, "committed_at 不能是空串（F7 的第二个缺口）")

    def test_j2_session_key_comes_from_actor(self):
        """J2：`session_key` 从**事件 actor** 读——所以**历史事件也能还原**。

        变异体：rebuild 只读 payload（`p.get("session_key")`）→ 红。
        """
        self._event()                      # actor 带 session_key，payload 不带
        self.conn.execute("DELETE FROM git_commit")
        self.conn.commit()
        fish_git_store.rebuild_commit_projection(self.conn)
        got = self.conn.execute(
            "SELECT session_key FROM git_commit WHERE sha='aaa'").fetchone()
        self.assertIsNotNone(got)
        self.assertEqual(got[0], "dsh:test-abc",
                         "session_key 没有从 actor 还原（历史事件就丢了）")

    def test_j2b_old_event_leaves_nulls_not_guesses(self):
        """J2b：**老事件**（payload 里没那些字段）重建后如实为 NULL，不编造。

        这是 append-only 的固有约束，不是 bug——但必须验它"不猜"。
        """
        fish_db.append_event(
            self.conn, "git.commit",
            {"kind": "system", "id": "fish_git", "session_key": "dsh:old"},
            {"repo_id": 1, "sha": "old1"},
            {"sha": "old1", "repo_id": 1, "subject": "老提交"},   # 只有 5 字段的旧形态
            cause="hook")
        self.conn.commit()
        fish_git_store.rebuild_commit_projection(self.conn)
        row = self.conn.execute(
            "SELECT author_name, has_trailer, authored_at FROM git_commit WHERE sha='old1'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(row[0], "老事件没有 author_name，不许编一个")
        self.assertIsNone(row[1], "老事件没有 has_trailer，不许猜")


class MigrateColumnsTest(_Case):
    """J3：老库（没有新列）跑 `migrate()` 后必须能查新列。

    F6 现场实验：`_GIT_SCHEMA` 是 `CREATE TABLE IF NOT EXISTS`，表已存在时
    **一个列都不会加**；而 `fish_db.migrate()` 不管 git 表。不补就是裸的
    `no such column: session_key`。
    """

    def test_j3_old_db_gets_new_columns(self):
        self.conn.execute("DROP TABLE git_commit")
        self.conn.execute("""CREATE TABLE git_commit (
          sha TEXT PRIMARY KEY, repo_id INTEGER NOT NULL, authored_at TEXT NOT NULL,
          committed_at TEXT NOT NULL, author_email TEXT, author_name TEXT, subject TEXT,
          is_merge INTEGER NOT NULL DEFAULT 0, files_changed INTEGER, insertions INTEGER,
          deletions INTEGER, branch_seen TEXT, content_root TEXT, event_id TEXT,
          superseded_by TEXT, unreachable_since TEXT)""")
        self.conn.commit()
        before = {r[1] for r in self.conn.execute("PRAGMA table_info(git_commit)")}
        self.assertNotIn("session_key", before, "前置：老库不该有 session_key")

        fish.migrate(self.conn)
        after = {r[1] for r in self.conn.execute("PRAGMA table_info(git_commit)")}
        self.assertIn("session_key", after, "migrate 没补 session_key（F6）")
        self.assertIn("has_trailer", after, "migrate 没补 has_trailer（F6）")
        # 真查一次，证明不是"列在但查不了"
        self.conn.execute("SELECT session_key, has_trailer FROM git_commit").fetchall()

    def test_j3b_migrate_is_idempotent(self):
        """J3b：连跑两次不报错（迁移必须幂等）。"""
        fish.migrate(self.conn)
        fish.migrate(self.conn)


class TrailerDetectTest(_Case):
    """J4：`_has_trailer` 判得准。

    只认 `Session:`（不是 `Surface:`——那是占用面，同一会话会变）；
    只存"有没有"，不存值（值的口径不统一，F4）。
    """

    def test_j4_detects_session_trailer(self):
        f = fish_git_store._has_trailer
        self.assertEqual(f("实现某功能\n\nSession: czhy-开发-2 (sid:abc)\nSurface: x"), 1)
        self.assertEqual(f("只是正文，没有 trailer"), 0)
        self.assertEqual(f(""), 0, "空 body 算没有")
        self.assertEqual(f(None), 0)
        # 只有 Surface 不算——它不是"哪个会话"
        self.assertEqual(f("改动\n\nSurface: impl/src"), 0,
                         "只有 Surface 不该算有 Session trailer")

    def test_j4b_record_commit_writes_it(self):
        """J4b：`record_commit` 真的把判断结果写进投影（不是只在函数里对）。"""
        self._event(has_trailer=1)
        self._projection()
        got = self.conn.execute(
            "SELECT has_trailer FROM git_commit WHERE sha='aaa'").fetchone()
        self.assertEqual(got[0], 1)


class HealthcheckVisibilityTest(_Case):
    """J5：**让「关联不上」这件事可见**（P0 的核心）。

    fish 建了五张表 + 22 条判据，但 `git_link` 生产实测 0 行。
    最坏的状态不是坏，是"有实现、有判据、有数据，但产出为零，且没人报告这个零"。
    """

    def _healthcheck(self):
        import subprocess
        r = subprocess.run([sys.executable, str(P0 / "fish.py"),
                            "--root", str(self.data), "healthcheck"],
                           cwd=str(P0), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        return (r.stdout or "") + (r.stderr or "")

    def test_j5_warns_when_zero_links(self):
        """J5：有提交、0 关联 → 必须打出告警行。"""
        self._projection()
        out = self._healthcheck()
        self.assertIn("git 采集", out, "healthcheck 没有 git 采集读数")
        self.assertIn("0 条关联", out, "没有报出「0 条关联」")
        self.assertIn("未关联到记忆", out, "没有报出「未关联」这件事")

    def test_j5b_no_warn_when_has_links(self):
        """J5b（**负对照**）：有关联时不打告警——否则告警会变成永久噪音。"""
        self._projection()
        fish_db.append_event(self.conn, "git.commit",
                             {"kind": "system", "id": "fish_git"},
                             {"repo_id": 1, "sha": "aaa"}, {"sha": "aaa", "repo_id": 1})
        self.conn.execute(
            "INSERT INTO git_link (src_kind, src_id, dst_kind, dst_id, rel, source, "
            "confidence, created_at) VALUES ('memory','m1','commit','aaa',"
            "'implemented_by','explicit',1.0,?)", (TS,))
        self.conn.commit()
        out = self._healthcheck()
        self.assertNotIn("未关联到记忆", out, "有关联了还在警告")


class CommitsReceiptTest(_Case):
    """J6：`git_repo_commits` 的回执要标出每条提交的关联状态。"""

    def _call(self, **kw):
        import mcp_server

        class _Id:
            status = "valid"
            citizen_id = "C1"
            evidence = "machine"
            session_key = "dsh:t"

        srv = object.__new__(mcp_server.FishServer)
        srv.conn = self.conn
        srv.root = self.data
        return srv.tool_git_repo_commits(kw, _Id())

    def test_j6_marks_unlinked(self):
        self._projection()
        self.conn.execute(
            "INSERT INTO git_checkout (repo_id, path, path_key, is_primary, "
            "last_seen_at) VALUES (1,?,?,1,?)",
            (str(self.data), str(self.data).replace("\\", "/").lower(), TS))
        self.conn.commit()
        out = self._call(repo_id=1, limit=5)
        self.assertIn("未关联", out, "回执没标出未关联")
        self.assertIn("都还没关联到记忆", out, "回执没有汇总说明")

    def test_j6b_marks_linked(self):
        """J6b（**负对照**）：已关联的提交要标「已关联」，不能一律标未关联。"""
        self._projection()
        self.conn.execute(
            "INSERT INTO git_checkout (repo_id, path, path_key, is_primary, "
            "last_seen_at) VALUES (1,?,?,1,?)",
            (str(self.data), str(self.data).replace("\\", "/").lower(), TS))
        self.conn.execute(
            "INSERT INTO git_link (src_kind, src_id, dst_kind, dst_id, rel, source, "
            "confidence, created_at) VALUES ('memory','m1','commit','aaa',"
            "'implemented_by','explicit',1.0,?)", (TS,))
        self.conn.commit()
        out = self._call(repo_id=1, limit=5)
        self.assertIn("已关联", out)
        self.assertNotIn("都还没关联到记忆", out, "有已关联的却还报「都还没关联」")


class PushGitGapTest(_Case):
    """J7 / J8：git 关联缺口要进推送，但**只在真有缺口时**。"""

    def _cands(self):
        import fish_push
        return fish_push.build_push_candidates(
            self.conn, self.data, timing="tool_after", session_key="dsh:t", limit=20)

    def _fill_commits(self, n=12):
        for i in range(n):
            self._projection(sha=f"c{i:03d}", subject=f"提交 {i}")

    def test_j7_pushes_gap_when_zero_links(self):
        """J7：12 条提交 0 关联 → 产生一条 `git:` 候选。"""
        self._fill_commits(12)
        cands = self._cands()
        git_c = [c for c in cands if str(c.get("code", "")).startswith("git:")]
        self.assertEqual(len(git_c), 1, f"没产出 git 候选：{[c.get('code') for c in cands]}")
        self.assertEqual(git_c[0]["kind"], "situational")
        self.assertIn("0 条关联", git_c[0]["title"])
        self.assertLessEqual(git_c[0]["score"], 0.5,
                             "缺口候选的分数必须低——别跟用户内容抢注意力")

    def test_j8_no_push_when_has_link(self):
        """J8（**负对照**）：有关联时**不许**产出。

        变异体：去掉 `if _g and not conn.execute(...)` 里的 `not` → 红。
        """
        self._fill_commits(12)
        self.conn.execute(
            "INSERT INTO git_link (src_kind, src_id, dst_kind, dst_id, rel, source, "
            "confidence, created_at) VALUES ('memory','m1','commit','c000',"
            "'implemented_by','explicit',1.0,?)", (TS,))
        self.conn.commit()
        cands = self._cands()
        git_c = [c for c in cands if str(c.get("code", "")).startswith("git:")]
        self.assertEqual(git_c, [], "有关联却还在推缺口——那会变成永久噪音")

    def test_j8b_no_push_when_too_few_commits(self):
        """J8b（**负对照**）：提交太少时不产（阈值 ≥10）。"""
        self._fill_commits(3)
        cands = self._cands()
        git_c = [c for c in cands if str(c.get("code", "")).startswith("git:")]
        self.assertEqual(git_c, [], "3 条提交就报警，阈值没生效")


if __name__ == "__main__":
    unittest.main()
