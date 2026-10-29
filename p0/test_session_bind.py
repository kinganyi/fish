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

    # S-2（2026-10-29 边界收窄后）：`self:` 重跑 = 同一个会话，**幂等**（J7/T3 契约）。
    def test_S2_self_alias_rerun_is_idempotent(self):
        first = fish.start_session(self.conn, alias="probe-ai", kind="visitor")
        again = fish.start_session(self.conn, alias="probe-ai", kind="visitor")
        self.assertEqual(again["citizen_id"], first["citizen_id"],
                         "同一 self: 会话重跑必须幂等（否则正常重跑会被挡死）")
        # 轮换：旧 token 被吊销，新 token 有效
        self.assertIsNone(fish.validate_session_token(self.conn, first["token"]))
        self.assertEqual(fish.validate_session_token(self.conn, again["token"]),
                         first["citizen_id"])

    # S-8（外部审查 🔴1）：**从未签发过 token** 的公民（register 的产物）也不能无证明绑定
    def test_S8_citizen_without_any_token_needs_proof(self):
        cid = fish.ulid()
        self.conn.execute(
            "INSERT INTO citizens (citizen_id, display_name, status) VALUES (?,?,?)",
            (cid, "victim-no-token", "active"))
        self.conn.commit()
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM session_tokens WHERE citizen_id=?", (cid,)
        ).fetchone()[0], 0)
        with self.assertRaises(ValueError) as ctx:
            fish.start_session(self.conn, citizen=cid, alias="x-8", kind="visitor")
        self.assertIn("证明", str(ctx.exception),
                      "无活凭证的公民也必须要求证明（旧实现这里放行 = 无证明接管）")

    # S-3
    def test_S3_proven_claim_succeeds(self):
        first = fish.start_session(self.conn, alias="probe-ai", kind="visitor")
        again = fish.start_session(self.conn, alias="probe-ai", kind="visitor",
                                   bind_token=first["token"])
        self.assertEqual(again["citizen_id"], first["citizen_id"])
        self.assertFalse(again["created"])

    # S-4（2026-10-29 窄化后）：**签发过凭证**的公民允许无证明重连——hook 依赖这条
    # （`fish_dsh_hook.py` 在会话开场显式传 `--citizen`，但它拿不出 token）。
    # 真正的防线在 S-8：从未签发过 token 的公民一律拒绝。
    def test_S4_issued_citizen_allows_proofless_rebind(self):
        first = fish.start_session(self.conn, alias="probe-ai", kind="visitor")
        again = fish.start_session(self.conn, citizen=first["citizen_id"],
                                   alias="other-ai", kind="visitor")
        self.assertEqual(again["citizen_id"], first["citizen_id"],
                         "签发过 token 的公民允许无证明重连（hook 的幂等路径）")

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

    # S-7（外部审查 🔴4）：第二次、第三次重绑都必须成功——旧 token 已被轮换吊销，
    # 但"持有过"就是证明。旧实现要求"有效 token"，与轮换互斥 → 永久锁死。
    def test_S7_rebind_with_rotated_token_works(self):
        first = fish.start_session(self.conn, alias="rebind-ai", kind="visitor")
        second = fish.start_session(self.conn, alias="rebind-ai", kind="visitor",
                                    bind_token=first["token"])
        self.assertEqual(second["citizen_id"], first["citizen_id"])
        # 第三次：first 的 token 此时已被第二次轮换吊销，仍然可以用它重绑
        third = fish.start_session(self.conn, alias="rebind-ai", kind="visitor",
                                   bind_token=first["token"])
        self.assertEqual(third["citizen_id"], first["citizen_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
