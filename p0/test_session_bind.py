#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S-01 判据（安全审计 2026-10-28）：绑定已有公民必须证明。

S-1 新建公民（新 self: 会话名）→ 不需要证明
S-2 同会话名**匿名**认领 → 拒绝（否则猜名字即可接管，还会吊销受害者 token）
S-3 带 `bind_token` 认领 → 成功且**同一公民**
S-4 `citizen=<id>` 无证明 → 拒绝
S-5 `citizen=<id>` + **别人的** token → 拒绝（防错绑）
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fish  # noqa: E402


class SessionBindTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-bind-"))
        self.conn = fish.connect(self.tmp / "data")
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        # 清掉宿主注入的 DSH_*：它们会让 session_key 变成不可猜的 dsh:...，测不到 self: 路径
        self._env = mock.patch.dict(
            os.environ, {k: v for k, v in os.environ.items()
                         if not k.startswith("DSH_")}, clear=True)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # S-1
    def test_S1_new_citizen_needs_no_proof(self):
        res = fish.start_session(self.conn, alias="probe-ai", kind="visitor")
        self.assertTrue(res["created"])
        self.assertTrue(res["session_key"].startswith(fish.SELF_SESSION_PREFIX))

    # S-2
    def test_S2_anonymous_claim_rejected(self):
        first = fish.start_session(self.conn, alias="probe-ai", kind="visitor")
        with self.assertRaises(ValueError) as ctx:
            fish.start_session(self.conn, alias="probe-ai", kind="visitor")
        self.assertIn("证明", str(ctx.exception))
        # 受害者原 token 没被吊销（拒绝发生在轮换之前）
        self.assertEqual(fish.validate_session_token(self.conn, first["token"]),
                         first["citizen_id"])

    # S-3
    def test_S3_proven_claim_succeeds(self):
        first = fish.start_session(self.conn, alias="probe-ai", kind="visitor")
        again = fish.start_session(self.conn, alias="probe-ai", kind="visitor",
                                   bind_token=first["token"])
        self.assertEqual(again["citizen_id"], first["citizen_id"])
        self.assertFalse(again["created"])

    # S-4
    def test_S4_citizen_bind_without_proof_rejected(self):
        first = fish.start_session(self.conn, alias="probe-ai", kind="visitor")
        with self.assertRaises(ValueError):
            fish.start_session(self.conn, citizen=first["citizen_id"],
                               alias="other-ai", kind="visitor")

    # S-5
    def test_S5_wrong_proof_rejected(self):
        a = fish.start_session(self.conn, alias="ai-a", kind="visitor")
        b = fish.start_session(self.conn, alias="ai-b", kind="visitor")
        with self.assertRaises(ValueError) as ctx:
            fish.start_session(self.conn, citizen=a["citizen_id"],
                               alias="ai-c", kind="visitor",
                               bind_token=b["token"])
        self.assertIn("不属于", str(ctx.exception))

    # S-6（边界修正）：宿主键（dsh:）**允许**幂等重跑——伪造它需要"能设子进程 env"，
    # 属同信任域；S-01 只守可猜的 `self:` 与显式 `citizen=`（那两条才是真实攻击面）。
    def test_S6_host_key_allows_idempotent_reopen(self):
        env = {"DSH_SESSION_ID": "session-victim-0001"}
        first = fish.start_session(self.conn, environ=env, kind="visitor")
        self.assertFalse(first["session_key"].startswith(fish.SELF_SESSION_PREFIX))
        again = fish.start_session(self.conn, environ=env, kind="visitor")
        self.assertEqual(again["citizen_id"], first["citizen_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
