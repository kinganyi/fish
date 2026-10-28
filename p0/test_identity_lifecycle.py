#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 判据（外部审查 🔴5 / 🔴10 / 🔴11）：身份生命周期闭环。

V-1 retired 公民的 bearer token **立即失效**（validate → None）
V-2 merged 公民的 bearer token 也失效
V-3 `citizen-retire` 真的**吊销**了活凭证（revoked_at 非空），不是只改状态
V-4 `citizen-merge` **拒绝** dst 是系统主人（提权路径）
V-5 `citizen-merge` 遇 `project_stints` 同项目唯一约束**不崩**，且只迁无冲突的
"""
import argparse
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fish  # noqa: E402


class IdentityLifecycleTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-lifecycle-"))
        self.conn = fish.connect(self.tmp)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _new_citizen(self, alias):
        # environ={} 很重要：否则会读到本机 DSH_* 环境 → 同 session_key → 同一个公民
        res = fish.start_session(self.conn, alias=alias, kind="visitor", environ={})
        return res["citizen_id"], res["token"]

    def _retire(self, cid):
        args = argparse.Namespace(root=str(self.tmp), citizen_id=cid,
                                  migrate_to=None, force=True, reason="test")
        fish.cmd_citizen_retire(args)

    def _revoked_at(self, cid):
        return self.conn.execute(
            "SELECT COUNT(*) FROM session_tokens WHERE citizen_id=? AND revoked_at IS NOT NULL",
            (cid,)).fetchone()[0]

    # V-1
    def test_V1_retired_token_invalid(self):
        cid, token = self._new_citizen("life-a")
        self.assertEqual(fish.validate_session_token(self.conn, token), cid)
        self.conn.execute("UPDATE citizens SET status='retired' WHERE citizen_id=?", (cid,))
        self.conn.commit()
        self.assertIsNone(fish.validate_session_token(self.conn, token),
                          "退役后 token 必须失效（旧实现只查 revoked/expires）")

    # V-2
    def test_V2_merged_token_invalid(self):
        src, src_token = self._new_citizen("life-b")
        dst, dst_token = self._new_citizen("life-c")
        fish.citizen_merge(self.conn, src=src, dst=dst, apply=True)
        self.assertIsNone(fish.validate_session_token(self.conn, src_token),
                          "合并后 src 的 token 必须失效")
        self.assertEqual(fish.validate_session_token(self.conn, dst_token), dst)

    # V-3
    def test_V3_retire_revokes_tokens(self):
        cid, _token = self._new_citizen("life-d")
        self._retire(cid)
        self.assertGreater(self._revoked_at(cid), 0,
                           "退役必须吊销活凭证（不是只改 status）")

    # V-4
    def test_V4_merge_refuses_dst_owner(self):
        src, _ = self._new_citizen("life-e")
        dst, _ = self._new_citizen("life-f")
        self.conn.execute("UPDATE citizens SET is_owner=1 WHERE citizen_id=?", (dst,))
        self.conn.commit()
        with self.assertRaises(ValueError) as ctx:
            fish.citizen_merge(self.conn, src=src, dst=dst, apply=True)
        self.assertIn("主人", str(ctx.exception))

    # V-5
    def test_V5_merge_survives_stint_conflict(self):
        src, _ = self._new_citizen("life-g")
        dst, _ = self._new_citizen("life-h")
        for idx, cid in enumerate((src, dst)):
            self.conn.execute(
                "INSERT INTO project_stints (citizen_id, project, from_ts, set_by) "
                "VALUES (?,?,?,?)", (cid, "P1", "2026-10-28T00:00:00Z", "test"))
        self.conn.execute(
            "INSERT INTO project_stints (citizen_id, project, from_ts, set_by) "
            "VALUES (?,?,?,?)", (src, "P2", "2026-10-28T00:00:00Z", "test"))
        self.conn.commit()
        # 不该抛 IntegrityError
        out = fish.citizen_merge(self.conn, src=src, dst=dst, apply=True)
        self.assertIn("project_stints.citizen_id", out["moved"])
        # 无冲突的 P2 迁过去了，同项目 P1 留在 src（历史可查）
        self.assertEqual(out["moved"]["project_stints.citizen_id"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
