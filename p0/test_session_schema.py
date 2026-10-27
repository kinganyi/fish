#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""撞车自撞修复 · T01 判据（数据模型 / 迁移 / 回滚）。

## 来源

设计：`45-撞车自撞修复-设计.md` §2（数据模型与迁移）+ §7.1（判据清单）。
本轮把 `presence` 的会话维度**放进主键**（`(board_id, citizen_id, session_key)`），
所以这里钉的是"结构对不对、迁移丢不丢数据、回滚还原不还原"。

## 判据清单

  S2  同会话重复签到幂等（一行、首签时间不变、doing 取最新）
  S3  空 `session_key` 回退旧语义（两次签到仍是一行）
  S14 迁移幂等（第二次 `rebuilt=False`、行数不变、**不再产备份**）
  S15 回填规则（`dsh:` 这种空 id 不许被当成会话；非标准形态回填 `''`）
  S16 迁移备份可读（行数 = 迁移前、仍是**两列**主键 → 证明它是"前"快照）
  S17 回滚可执行（合并会话行、首签取最早、被丢的行可列举、dry-run 不写库）
  S24 主键列序 + `session_key` 的 NOT NULL DEFAULT ''
  另：备份写不进去 → **中止**且原表一字不动（R1）

## 为什么每条都能失败（而不是自评）

  · S2/S3 的"首签不变"是靠 `UPDATE presence SET first_seen_at=<很久以前>` 再签一次；
    若 DO UPDATE 里写了 `first_seen_at=excluded.first_seen_at`，这条必红；
  · S14 的"不再产备份"靠**备份文件计数**；删掉幂等出口就会多出一份；
  · S15 的四种形态各是一条断言；回填表达式放宽成"非空就回填"必红；
  · S16 的"仍是两列主键"是关键：只比行数的话，"迁移后再备份"也能过；
  · S17 的"首签取最早"与"主行取最新"是两个方向，写反必红。
"""
import datetime
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fish  # noqa: E402

CID_A = "01SESSIONSCHEMA000000A1"
CID_B = "01SESSIONSCHEMA000000B2"


# 2026-10-26 QA F4：固定基准——原来每次 `_ts()` 都取墙钟，写库与断言跨一个
# 整秒边界就红（40 次实测 5%），变异脚本因此有约 5% 概率假报 MISSED。
# 判据里的时间是**相对值**（早/晚），不需要随墙钟走；用模块加载时刻既稳定
# 又保持"几秒前"的新鲜度（idle 判定不受影响）。
_BASE_TS = datetime.datetime.now(datetime.timezone.utc)


def _ts(minutes_ago=0):
    return (_BASE_TS - datetime.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class SessionSchemaBase(unittest.TestCase):
    """一个已迁移的空库；需要"旧库"时用 `_legacy()` 把它退回 v2。"""

    def setUp(self):
        import shutil
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-session-schema-"))
        self._shutil = shutil
        self.root = self.tmp / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        self._shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- 夹具
    def _legacy(self, rows, project="PJ"):
        """把 `presence` 退回 v2（两列主键）并塞入 `(citizen_id, agent_id)` 行。

        `_PRESENCE_V2_DDL` 就是回滚脚本用的那份**冻结的历史形态**——用它造旧库，
        于是"旧库"与"回滚后"是同一个形状，判据不会各钉各的。
        """
        self.conn.execute("DROP TABLE presence")
        self.conn.execute(fish._PRESENCE_V2_DDL.replace("presence_v2", "presence"))
        board_id = fish.board_ensure(self.conn, "project", project)["id"]
        for cid, agent in rows:
            self.conn.execute(
                "INSERT INTO presence (board_id, citizen_id, agent_id, status, doing, "
                "surface, first_seen_at, last_seen_at) VALUES (?,?,?,'active','旧行','',?,?)",
                (board_id, cid, agent, _ts(30), _ts(30)))
        self.conn.commit()
        return board_id

    def _backups(self):
        folder = self.root / "_backups"
        return sorted(folder.glob("index-before-presence-session-*.sqlite"))


class CheckinIdempotencyTest(SessionSchemaBase):
    """S2 / S3：写侧内核的两种"重复签到"。"""

    def test_S2_same_session_is_idempotent(self):
        board = fish.board_checkin(self.conn, CID_A, "第一句", scope="project",
                                   scope_key="PJ", session_key="dsh:aaa1111")
        self.conn.execute(
            "UPDATE presence SET first_seen_at=?, last_seen_at=? WHERE board_id=?",
            (_ts(90), _ts(90), board["id"]))
        self.conn.commit()
        fish.board_checkin(self.conn, CID_A, "第二句", scope="project", scope_key="PJ",
                           session_key="dsh:aaa1111")
        rows = self.conn.execute(
            "SELECT doing, first_seen_at FROM presence WHERE session_key='dsh:aaa1111'"
        ).fetchall()
        self.assertEqual(len(rows), 1, "同一会话重复签到必须是一行（主键里有 session_key）")
        self.assertEqual(rows[0][0], "第二句", "doing 取最新")
        self.assertEqual(rows[0][1], _ts(90), "首签时间不许被重复签到改晚")

    def test_S3_blank_session_key_keeps_old_semantics(self):
        fish.board_checkin(self.conn, CID_A, "第一句", scope="project", scope_key="PJ")
        fish.board_checkin(self.conn, CID_A, "第二句", scope="project", scope_key="PJ")
        rows = self.conn.execute(
            "SELECT session_key, doing FROM presence WHERE citizen_id=?", (CID_A,)).fetchall()
        self.assertEqual(len(rows), 1, "不给会话键 → 行为与今天逐字节相同（一行/公民）")
        self.assertEqual(rows[0][0], "")
        self.assertEqual(rows[0][1], "第二句")


class MigrationTest(SessionSchemaBase):
    """S14 / S15 / S16 / S24 + 备份失败即中止（R1）。"""

    def test_S24_pk_order_and_column_decl(self):
        self.assertEqual(fish._presence_pk_cols(self.conn),
                         ("board_id", "citizen_id", "session_key"),
                         "主键列序是契约（S24 按元组全等比）")
        self.assertEqual(fish._presence_write_shape(self.conn),
                         (True, "board_id, citizen_id, session_key"))
        info = {r[1]: r for r in self.conn.execute("PRAGMA table_info(presence)")}
        self.assertEqual(int(info["session_key"][3]), 1, "session_key 必须 NOT NULL")
        self.assertEqual(info["session_key"][4], "''", "缺省必须是空串（不是 NULL）")

    def test_S14_migration_is_idempotent(self):
        self._legacy([(CID_A, "dsh:aaa"), (CID_B, None)])
        first = fish._ensure_presence_session(self.conn, data_root=self.root)
        self.assertTrue(first["rebuilt"], f"旧库必须被重建：{first}")
        self.assertEqual(first["rows_before"], first["rows_after"], "行数守恒")
        backups = len(self._backups())
        self.assertEqual(backups, 1, "重建必须留一份备份")
        second = fish._ensure_presence_session(self.conn, data_root=self.root)
        self.assertFalse(second["rebuilt"], "第二次必须走幂等出口")
        self.assertEqual(second["rows_before"], second["rows_after"])
        self.assertEqual(second["pending"], None)
        self.assertEqual(len(self._backups()), backups, "幂等路径不许再产备份")

    def test_S15_backfill_rules(self):
        rows = [(CID_A, "dsh:abc"), (CID_B, "codebuddy:abc"),
                ("01SESSIONSCHEMA000000C3", "workbuddy:abc"),
                ("01SESSIONSCHEMA000000D4", "cli:abc"),
                ("01SESSIONSCHEMA000000E5", "self:企业微信-主AI"),
                ("01SESSIONSCHEMA000000F6", None),
                ("01SESSIONSCHEMA000000G7", "dsh:"),
                ("01SESSIONSCHEMA000000H8", "codebuddy-主AI/ffd54d84")]
        self._legacy(rows)
        res = fish._ensure_presence_session(self.conn, data_root=self.root)
        self.assertEqual(res["backfilled"], 5, f"应回填 5 条（dsh/codebuddy/workbuddy/cli/self）：{res}")
        self.assertEqual(res["blank"], 3, "NULL / `dsh:` / 带 `/` 的非标准形态 → ''")
        got = dict(self.conn.execute("SELECT citizen_id, session_key FROM presence"))
        self.assertEqual(got[CID_A], "dsh:abc")
        self.assertEqual(got[CID_B], "codebuddy:abc")
        self.assertEqual(got["01SESSIONSCHEMA000000E5"], "self:企业微信-主AI")
        self.assertEqual(got["01SESSIONSCHEMA000000F6"], "", "NULL 不许被当成会话")
        self.assertEqual(got["01SESSIONSCHEMA000000G7"], "",
                         "`dsh:` 是空 id——GLOB 少写 `?` 就会把这种值也当会话（伪造证据）")
        self.assertEqual(got["01SESSIONSCHEMA000000H8"], "",
                         "非标准形态（带 / 无 :）不许猜成会话")
        # agent_id 原样保留：迁移只加列，不改既有列的值（回滚不需要反推）
        self.assertEqual(
            self.conn.execute(
                "SELECT agent_id FROM presence WHERE citizen_id='01SESSIONSCHEMA000000H8'"
            ).fetchone()[0], "codebuddy-主AI/ffd54d84")

    def test_S16_backup_is_readable_and_pre_migration(self):
        self._legacy([(CID_A, "dsh:aaa"), (CID_B, None), ("01SESSIONSCHEMA000000C3", None)])
        res = fish._ensure_presence_session(self.conn, data_root=self.root)
        self.assertTrue(res["backup"], "必须给备份路径")
        path = Path(res["backup"])
        self.assertTrue(path.exists(), f"备份文件不存在：{path}")
        ro = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            count = ro.execute("SELECT COUNT(*) FROM presence").fetchone()[0]
            self.assertEqual(count, res["rows_before"], "备份必须是迁移**前**的行数")
            pk = tuple(r[1] for r in ro.execute("PRAGMA table_info(presence)") if r[5])
            self.assertEqual(pk, ("board_id", "citizen_id"),
                             "备份必须是迁移前的**两列**主键（迁后备份是另一个东西）")
            cols = {r[1] for r in ro.execute("PRAGMA table_info(presence)")}
            self.assertNotIn("session_key", cols)
        finally:
            ro.close()

    def test_migration_aborts_when_backup_fails(self):
        """R1：备份写不进去 → **不迁移**（宁可这次不升级，也不拿生产库赌）。"""
        self._legacy([(CID_A, "dsh:aaa")])
        blocker = self.tmp / "not-a-dir"
        blocker.write_text("我只是一个文件", encoding="utf-8")
        with self.assertRaises(fish.PresenceMigrationError):
            fish._ensure_presence_session(self.conn, data_root=blocker)
        self.assertEqual(fish._presence_pk_cols(self.conn), ("board_id", "citizen_id"),
                         "失败必须原表一字不动")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM presence").fetchone()[0], 1)
        self.assertEqual(fish._presence_write_shape(self.conn),
                         (False, "board_id, citizen_id"),
                         "迁移没跑 → 写侧自动降级两列（故障兜底，不是第二套语义）")

    def test_report_exposes_pending_and_prefix_conflicts(self):
        """`--report` 的两条读数：pending 与"短/长前缀疑似同一会话"（只报告不合并）。"""
        board_id = self._legacy([(CID_A, "dsh:session-779a")])
        self.conn.execute(
            "INSERT INTO presence (board_id, citizen_id, agent_id, status, doing, surface, "
            "first_seen_at, last_seen_at) VALUES (?,?,?,'active','旧行','',?,?)",
            (board_id, CID_B, "dsh:session-779a464a-20df", _ts(30), _ts(30)))
        # 两行不同公民 → v2 允许；为了造"同一公民两行"用第二次插入同一公民会撞 PK，
        # 所以这里把 B 行改挂到 A 公民名下（UPDATE 绕过 PK 检查不成立，改用同公民同板
        # 的另一块板——回填后仍是"同一公民多会话"的形态）。
        self.conn.execute("DELETE FROM presence WHERE citizen_id=?", (CID_B,))
        other = fish.board_ensure(self.conn, "project", "PJ2")["id"]
        self.conn.execute(
            "INSERT INTO presence (board_id, citizen_id, agent_id, status, doing, surface, "
            "first_seen_at, last_seen_at) VALUES (?,?,?,'active','旧行','',?,?)",
            (other, CID_A, "dsh:session-779a464a-20df", _ts(30), _ts(30)))
        self.conn.commit()
        before = fish.presence_session_report(self.conn)
        self.assertEqual(before["pending"], "rebuild", "还没迁移 → pending=rebuild")
        self.assertEqual(before["expected_backfilled"], 2, "迁移前的预期回填数要能算出")
        fish._ensure_presence_session(self.conn, data_root=self.root)
        after = fish.presence_session_report(self.conn)
        self.assertIsNone(after["pending"])
        self.assertTrue(after["has_session_key"])
        pairs = [(c["short"], c["long"]) for c in after["prefix_conflicts"]]
        self.assertIn(("dsh:session-779a", "dsh:session-779a464a-20df"), pairs,
                      "短/长前缀要能报出来（不合并：猜错会把两个会话的历史焊死）")

    def test_S17_rollback_merges_and_keeps_earliest_first_seen(self):
        self._legacy([(CID_A, "dsh:aaa")])
        fish._ensure_presence_session(self.conn, data_root=self.root)
        board = fish.board_ensure(self.conn, "project", "PJ")
        fish.board_checkin(self.conn, CID_A, "会话一", scope="project", scope_key="PJ",
                           session_key="dsh:aaa")
        fish.board_checkin(self.conn, CID_A, "会话二", scope="project", scope_key="PJ",
                           session_key="dsh:bbb")
        self.conn.execute(
            "UPDATE presence SET first_seen_at=?, last_seen_at=? WHERE session_key='dsh:aaa'",
            (_ts(300), _ts(60)))
        self.conn.execute(
            "UPDATE presence SET first_seen_at=?, last_seen_at=? WHERE session_key='dsh:bbb'",
            (_ts(60), _ts(5)))
        self.conn.commit()
        dry = fish.presence_session_rollback(self.conn, data_root=self.root, dry_run=True)
        self.assertFalse(dry["rebuilt"], "dry-run 不许写库")
        self.assertEqual(dry["rows_after"], 1)
        self.assertEqual(len(dry["dropped"]), 1)
        self.assertEqual(fish._presence_pk_cols(self.conn),
                         ("board_id", "citizen_id", "session_key"), "dry-run 后主键不变")
        res = fish.presence_session_rollback(self.conn, data_root=self.root)
        self.assertTrue(res["rebuilt"], f"回滚没执行：{res}")
        self.assertEqual(fish._presence_pk_cols(self.conn), ("board_id", "citizen_id"))
        rows = self.conn.execute(
            "SELECT board_id, citizen_id, doing, first_seen_at FROM presence").fetchall()
        self.assertEqual(len(rows), 1, "每个 (board, citizen) 只许一行")
        self.assertEqual(rows[0][2], "会话二", "主行取 `max(last_seen, auto_seen)` 最新的那行")
        self.assertEqual(rows[0][3], _ts(300), "首签时间取**最早**的（不许被合并改晚）")
        self.assertEqual(res["dropped"][0]["session_key"], "dsh:aaa", "被丢的行要能列举（留证）")
        self.assertEqual(res["dropped"][0]["doing"], "会话一")
        report = fish.presence_session_report(self.conn)
        self.assertEqual(report["pending"], "rebuild",
                         "回滚之后库又变回 v2 形态 → 下一次 migrate 会重建（D5 自动迁移）")


class WriteShapeDegradeTest(SessionSchemaBase):
    """两列主键（迁移未跑）时写侧必须**自动降级**，而且要能签到成功（R2/R3）。"""

    def test_checkin_works_on_legacy_shape(self):
        self._legacy([(CID_A, "dsh:aaa")])
        board = fish.board_checkin(self.conn, CID_A, "降级签到", scope="project",
                                   scope_key="PJ")
        rows = self.conn.execute(
            "SELECT doing FROM presence WHERE board_id=?", (board["id"],)).fetchall()
        self.assertEqual(len(rows), 1, "两列主键下仍是「一行/公民」——这是兜底，不是新语义")
        self.assertEqual(rows[0][0], "降级签到")
        auto = fish.board_auto_checkin(self.conn, CID_B, project="PJ",
                                       session_key="dsh:bbb")
        self.assertTrue(auto.get("created"), f"自动行也必须能在旧形态上建出来：{auto}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
