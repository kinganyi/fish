#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W4 · 递归吊销（113 §5.5 / §5.8 · J-D2 / J-W4-IDX · 2026-11-25）。

## 为什么这一波必须与"放开一跳"同一波做

`revoke_session_tokens(parent_hash=…)` 今天只吊销**直接子级**，而它今天是安全的——
因为 `assert_agent_parent_allowed` 只允许 machine 级父，"链最多两层，直接子级恰好
等于全部后代"（110 §3.6.4）。W4 放开链式签发之后，单跳吊销立刻成真缺口：
结束 A，C/D 的凭证还活着。**只做前一半比今天更糟。**

## 四个坑（110 实测，判据逐条钉住）

① `UNION` 不是 `UNION ALL`（去重防环）；② 保留 `revoked_at IS NULL`（幂等）；
③ `WITH … UPDATE` 的 `cursor.rowcount` 返回 **-1**（返回值必须走 `SELECT changes()`）；
④ `children_revoked` 语义从"直接子级数"变成"全部后代数"——这是可观测行为变化。
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish  # noqa: E402

#: 变异用：把递归 CTE 换回"只吊销直接子级"（113 §5.5 的坑 ②/③ 都还在）。
_SINGLE_HOP = ("UPDATE session_tokens SET revoked_at=:now "
               " WHERE parent_token=:root AND revoked_at IS NULL")


class _Chain(unittest.TestCase):
    """A（machine）→ B（inherited）→ C（inherited）：三层链，一个临时库。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-w4-revoke-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.conns = []
        self.conn = self._conn("chain")

    def tearDown(self):
        for conn in self.conns:
            try:
                conn.close()
            except Exception:      # noqa: BLE001
                pass

    def _conn(self, name):
        conn = fish.connect(self.tmp / name)
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        self.conns.append(conn)
        return conn

    def _chain(self, conn=None):
        conn = conn or self.conn
        a = fish.start_session(conn, environ={"DSH_SESSION_ID": "rev-A"})
        b = fish.issue_agent_token(conn, parent_token=a["token"], role="reviewer")
        c = fish.issue_agent_token(conn, parent_token=b["token"], role="sub")
        return a, b, c

    @staticmethod
    def _revoked(conn, token):
        row = conn.execute("SELECT revoked_at FROM session_tokens WHERE token_hash=?",
                           (fish.hash_session_token(token),)).fetchone()
        return row[0] is not None

    def _assert_dead(self, conn, token, where=""):
        self.assertTrue(self._revoked(conn, token), f"{where}：凭证必须已失效")


class RecursiveRevokeTest(_Chain):

    def test_JD2_ending_A_revokes_all_descendants(self):
        a, b, c = self._chain()
        # 对照组：无关会话的凭证必须**活着**（否则"全吊销"也能让主判据绿）
        x = fish.start_session(self.conn, environ={"DSH_SESSION_ID": "rev-X"})
        out = fish.end_session(self.conn, token=a["token"])
        self.assertEqual(out["children_revoked"], 2,
                         "children_revoked 必须是**全部后代数**（语义变化要看得见）")
        self._assert_dead(self.conn, a["token"], "A 自己")
        self._assert_dead(self.conn, b["token"], "B（直接子级）")
        self._assert_dead(self.conn, c["token"], "C（孙级 —— 单跳实现会漏掉它）")
        self.assertFalse(self._revoked(self.conn, x["token"]), "对照组：无关会话 X 必须仍活")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM session_tokens WHERE session_key='dsh:rev-C' "
            "AND revoked_at IS NULL").fetchone()[0], 0, "C 不许留下活凭证")

    def test_JD2_repeat_end_is_idempotent(self):
        a, _b, _c = self._chain()
        fish.end_session(self.conn, token=a["token"])
        again = fish.end_session(self.conn, token=a["token"])
        self.assertTrue(again["already"], "重复 end 必须走幂等分支（不是报错）")
        self.assertEqual(again["children_revoked"], 0,
                         "第二次没有可吊销的：`revoked_at IS NULL` 是幂等的唯一来源")

    # ------------------------------------------------------------ 变异
    def test_JD2_mutation_single_hop_leaves_C_alive(self):
        """变异 ①：SQL 改回单跳 → C 仍活 → J-D2 必红。"""
        a, b, c = self._chain()
        with mock.patch.object(fish, "_REVOKE_DESCENDANTS", _SINGLE_HOP):
            out = fish.end_session(self.conn, token=a["token"])
        self.assertEqual(out["children_revoked"], 1, "变异没生效：单跳应只吊销 B")
        self._assert_dead(self.conn, b["token"], "B")
        self.assertFalse(self._revoked(self.conn, c["token"]),
                         "变异下 C 必须还活着（否则判据测的不是递归）")
        with self.assertRaises(AssertionError, msg="J-D2 的『C 也失效』失去判别力"):
            self._assert_dead(self.conn, c["token"], "C（孙级）")

    def test_JD2_changes_not_rowcount(self):
        """变异 ②：`WITH … UPDATE` 的 rowcount 是 -1 —— 必须走 `SELECT changes()`。

        这条**不是**读代码下的结论：它在真链上直接执行同一段 SQL，把两个读数都打出来。
        """
        a, _b, _c = self._chain()
        now = fish.now_utc()
        cur = self.conn.execute(fish._REVOKE_DESCENDANTS,
                                {"root": a["token_hash"], "now": now})
        self.assertEqual(cur.rowcount, -1,
                         "本机 sqlite 的 WITH…UPDATE rowcount 不是 -1 了——重新取证")
        self.assertEqual(self.conn.execute("SELECT changes()").fetchone()[0], 2,
                         "changes() 才是真实吊销数（B、C）")

    def test_JW4IDX_parent_token_index_exists(self):
        names = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='session_tokens'")}
        self.assertIn("idx_tokens_parent", names,
                      f"parent_token 上必须有索引（递归吊销的取数口）：{sorted(names)}")
        # 变异：删索引 → 同一查询必须看不见它（证明这不是"恒真"的判据）
        self.conn.execute("DROP INDEX idx_tokens_parent")
        names2 = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='session_tokens'")}
        with self.assertRaises(AssertionError):
            self.assertIn("idx_tokens_parent", names2, "变异：索引已删")


class RevokeSurvivesMigrationTest(_Chain):
    """CHECK 重建（113 §5.3）不能把 session_tokens 的索引/行数弄丢。"""

    def test_index_and_rows_survive_evidence_rebuild(self):
        a = fish.start_session(self.conn, environ={"DSH_SESSION_ID": "rebuild-A"})
        before = self.conn.execute("SELECT COUNT(*) FROM session_tokens").fetchone()[0]
        # 造一个"旧库"形态：把 evidence CHECK 改回三值（= W4 之前的库）
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute("ALTER TABLE session_tokens RENAME TO session_tokens_old")
        self.conn.execute(fish._SESSION_TOKENS_DDL.replace(
            "'machine','inherited','self-reported','provisional'",
            "'machine','inherited','self-reported'"))
        self.conn.execute(
            "INSERT INTO session_tokens SELECT * FROM session_tokens_old")
        self.conn.execute("DROP TABLE session_tokens_old")
        self.conn.execute("COMMIT")
        self.conn.execute("PRAGMA foreign_keys=ON")
        report = fish._ensure_session_evidence_levels(self.conn)
        self.assertTrue(report["rebuilt"], f"旧 CHECK 必须触发重建：{report}")
        self.assertEqual(report["rows_before"], report["rows_after"], "重建必须不丢行")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM session_tokens").fetchone()[0], before)
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM session_tokens WHERE token_hash=?",
            (a["token_hash"],)).fetchone(), "重建后原行必须还在")
        self.assertIn("idx_tokens_parent", {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='session_tokens'")})
        # 重建后 provision 必须被接受（这就是重建的唯一目的）
        fish.issue_session_token(self.conn, citizen_id=a["citizen_id"],
                                 session_key="dsh:rebuild-P", evidence="provisional",
                                 channel="cli")
        self.assertTrue(Path(report["backup"]).exists(), "备份文件必须留档")


if __name__ == "__main__":
    unittest.main(verbosity=2)
