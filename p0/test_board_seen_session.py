#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-3 判据：`board_seen` 升会话维度（修"同公民多会话互相吞推送"）。

## 为什么单独成文件

`board_seen` 记的是"每个会话上次注入看到的看板快照"；旧实现主键是
`(board_id, citizen_id)`——同一公民的两个窗口只有一份快照，A 看过之后
B 就再也收不到"板上有人动了"。本次升三列主键，判据必须同时覆盖：
迁移（旧两列 → 新三列）、写入（两会话各一行）、读取隔离（A 看过不影响 B）、
降级（迁移未跑时写侧自动两列）。
"""
import datetime
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fish  # noqa: E402

CID = "01BOARDSEENSESSION0000A1"
OTHER = "01BOARDSEENSESSION0000B2"
SK_A = "dsh:session-boardseen-aaaa-0001"
SK_B = "dsh:session-boardseen-bbbb-0002"
OLD_BOARD_SEEN_DDL = """CREATE TABLE board_seen (
  board_id         INTEGER NOT NULL,
  citizen_id       TEXT NOT NULL,
  last_post_id     INTEGER NOT NULL DEFAULT 0,
  last_presence_at TEXT NOT NULL DEFAULT '',
  updated_at       TEXT NOT NULL,
  PRIMARY KEY (board_id, citizen_id)
);"""


class BoardSeenBase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-board-seen-"))
        self.root = self.tmp / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        for cid, name in ((CID, "AI-甲"), (OTHER, "AI-乙")):
            self.conn.execute(
                "INSERT INTO citizens (citizen_id,display_name,kind,status,created_at,host) "
                "VALUES (?,?,'visitor','active',?,'dsh')", (cid, name, fish.now_utc()))
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def _board(self, key="PJ"):
        return fish.board_ensure(self.conn, "project", key)


class MigrationTest(BoardSeenBase):
    """S-BS1：旧两列 → 三列，行数守恒、旧行 session_key=''、备份、幂等。"""

    def test_SBS1_old_table_upgraded_with_backup_and_idempotent(self):
        board = self._board()
        self.conn.execute("DROP TABLE board_seen")
        self.conn.execute(OLD_BOARD_SEEN_DDL)
        self.conn.execute(
            "INSERT INTO board_seen (board_id,citizen_id,last_post_id,last_presence_at,updated_at) "
            "VALUES (?,?,?,?,?)", (board["id"], CID, 3, "2026-10-26T00:00:00Z", fish.now_utc()))
        self.conn.commit()
        self.assertEqual(fish._presence_pk_cols_of(self.conn, "board_seen"),
                         ("board_id", "citizen_id"))

        result = fish._ensure_board_seen_session(self.conn, data_root=self.root)
        self.assertTrue(result["rebuilt"], result)
        self.assertEqual(result["rows_before"], 1)
        self.assertEqual(result["rows_after"], 1)
        self.assertTrue(Path(result["backup"]).exists(), "迁移必须留备份")
        self.assertEqual(fish._presence_pk_cols_of(self.conn, "board_seen"),
                         ("board_id", "citizen_id", "session_key"))
        row = self.conn.execute(
            "SELECT session_key,last_post_id FROM board_seen").fetchone()
        self.assertEqual(row[0], "", "旧行应迁成无会话归属")
        self.assertEqual(row[1], 3, "行内容不许丢")

        again = fish._ensure_board_seen_session(self.conn, data_root=self.root)
        self.assertFalse(again["rebuilt"], "第二次必须幂等（不重建）")
        self.assertIsNone(again["backup"], "幂等出口不产新备份")


class WriteTest(BoardSeenBase):
    """S-BS2：同公民两会话各一行；更新只影响自己那一行。"""

    def test_SBS2_two_sessions_two_rows(self):
        board = self._board()
        fish.board_block(self.conn, "PJ", citizen_id=CID, session_key=SK_A)
        fish.board_block(self.conn, "PJ", citizen_id=CID, session_key=SK_B)
        rows = self.conn.execute(
            "SELECT session_key,last_post_id FROM board_seen WHERE board_id=? "
            "AND citizen_id=? ORDER BY session_key", (board["id"], CID)).fetchall()
        self.assertEqual([r[0] for r in rows], [SK_A, SK_B], f"两会话应各一行：{rows}")

        # 新版动态后只更新 A → B 的快照保持旧值（不会被 A 覆盖）
        self.conn.execute(
            "INSERT INTO board_posts (board_id,citizen_id,kind,text,created_at) "
            "VALUES (?,?,?,?,?)", (board["id"], OTHER, "note", "新动态", fish.now_utc()))
        self.conn.commit()
        fish.board_block(self.conn, "PJ", citizen_id=CID, session_key=SK_A)
        last = dict(self.conn.execute(
            "SELECT session_key,last_post_id FROM board_seen WHERE board_id=? "
            "AND citizen_id=?", (board["id"], CID)).fetchall())
        self.assertEqual(last[SK_A], 1, "A 的快照应更新到最新动态")
        self.assertEqual(last[SK_B], 0, "B 的快照不许被 A 的更新覆盖")


class ReadIsolationTest(BoardSeenBase):
    """S-BS3：A 看过之后，B 仍能收到"看板有更新"候选（核心漏推修复）。"""

    def test_SBS3_seen_by_one_session_does_not_silence_other(self):
        board = self._board()
        # B 先 inject 一次（建立 B 的快照：last_post_id=0）
        fish.board_block(self.conn, "PJ", citizen_id=CID, session_key=SK_B)
        # 板上出现新动态
        self.conn.execute(
            "INSERT INTO board_posts (board_id,citizen_id,kind,text,created_at) "
            "VALUES (?,?,?,?,?)", (board["id"], OTHER, "note", "有人在动", fish.now_utc()))
        self.conn.commit()
        # A 后 inject（A 的快照更新到最新，含这条动态）
        fish.board_block(self.conn, "PJ", citizen_id=CID, session_key=SK_A)

        def board_candidates(session_key):
            cands = fish.build_push_candidates(
                self.conn, self.root, citizen_id=CID, session_key=session_key,
                project="PJ", timing="tool_after")
            return [c for c in cands if str(c.get("code", "")).startswith("board:")]

        self.assertEqual(board_candidates(SK_A), [],
                         "A 已经看过，不该再收到同一条看板更新")
        self.assertEqual(len(board_candidates(SK_B)), 1,
                         "B 的快照停在 0（没被 A 覆盖），必须收到——这正是修掉的漏推")


class DegradeTest(BoardSeenBase):
    """S-BS4：迁移未跑（旧两列）时写侧自动降级，不崩。"""

    def test_SBS4_old_shape_degrades(self):
        board = self._board()
        self.conn.execute("DROP TABLE board_seen")
        self.conn.execute(OLD_BOARD_SEEN_DDL)
        self.conn.commit()
        has_sk, conflict = fish._board_seen_write_shape(self.conn)
        self.assertFalse(has_sk)
        self.assertEqual(conflict, "board_id, citizen_id")
        fish.board_block(self.conn, "PJ", citizen_id=CID, session_key=SK_A)
        fish.board_block(self.conn, "PJ", citizen_id=CID, session_key=SK_B)
        rows = self.conn.execute(
            "SELECT board_id FROM board_seen WHERE board_id=?", (board["id"],)).fetchall()
        self.assertEqual(len(rows), 1, "降级态按公民一行（旧行为）")
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(board_seen)")]
        self.assertNotIn("session_key", cols, "旧表没有 session_key 列")


if __name__ == "__main__":
    unittest.main()
