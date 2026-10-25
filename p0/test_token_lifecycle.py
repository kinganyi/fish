#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话凭证生命周期：**token 的生命周期 = 会话的生命周期**（2026-10-25 用户裁定）。

## 需求原话

> 我们的 AI 公民以后就以会话为最小单位，那么当这个会话被创建后，AI 第一次来访
> 得到 token 后，这个 token 就属于这个 AI 公民了，**也不需要过期了吧**
>
> 会话归档默认**无限期**，N 不取数，未来开放给用户前端进行配置

## 为什么 24h TTL 是错的（取证结论）

1. **公民与会话本来就是 1:1**：`find_session_citizen(conn, session_key)` 按
   `host_session_key` 查 `status='active'` 的公民——一个会话一个公民。
   所以 token 从设计上就是「会话的身份牌」。
2. **但有效期按墙钟算**，两者从来没对上过：
   · 短会话（1 小时）：token 多活 23 小时，纯浪费且制造僵尸行
   · 长会话（跨天）：第 25 小时 token 过期，**会话还好好的，身份却没了**，
     此后它写的记忆全部变成 `unattributed`，而它自己不知道
     —— 这正是 `EVAL-261025-21`「一次性失误 = 永久脏数据」的结构性来源
3. **token 没有权限语义**（全库 `permission` / `can_write` 命中 0 行）。
   一个只是"署名笔"的东西，按时间过期不带来任何安全收益。

## 判据

  T1  默认签发 → `expires_at IS NULL`（永不过期）
  T2  不过期的 token **有效**（`validate_session_token` 返回 citizen_id）
  T3  **重签吊销旧的**（同一 session_name 重签 → 旧 token 失效、公民不变）
  T4  显式 `ttl_hours > 0` **仍能过期**（保留给子代理等临时场景）
  T5  归档配置默认 **0 = 不自动归档**（用户明确"N 不取数"）
  T6  **手动吊销仍然有效**（`session end` 是真正的"我要结束它"）
  T7  重开会话拿到**同一个公民**（会话恢复不该变成新公民）
"""
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)


class TokenLifecycleTest(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES ('TT','TT',NULL,0,'active')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.root.parent, ignore_errors=True)

    def _info(self, token):
        return fish.lookup_session_token(self.conn, token)

    # ------------------------------------------------------------------ T1
    def test_T1_default_never_expires(self):
        """默认签发 → `expires_at IS NULL`。"""
        r = fish.start_session(self.conn, alias="lifecycle-demo")
        self.assertIsNone(r["expires_at"], f"默认应不过期，实际 {r['expires_at']!r}")
        row = self.conn.execute(
            "SELECT expires_at FROM session_tokens WHERE token_hash=?",
            (fish.hash_session_token(r["token"]),)).fetchone()
        self.assertIsNone(row[0], "库里也应存 NULL")

    def test_T1_config_default_is_zero(self):
        """配置默认值必须是 0（= 永不过期），不是 24。"""
        self.assertEqual(fish.get_config(self.conn, "session.token_ttl_hours"), 0,
                         "默认 TTL 应为 0；改成正数会让长会话中途失去身份")

    # ------------------------------------------------------------------ T2
    def test_T2_never_expiring_token_is_valid(self):
        """不过期的 token 必须**有效**（这是整条链路的前提）。"""
        r = fish.start_session(self.conn, alias="valid-demo")
        self.assertEqual(fish.validate_session_token(self.conn, r["token"]),
                         r["citizen_id"])
        info = self._info(r["token"])
        self.assertFalse(info["expired"], "expires_at=None 不该被判过期")
        self.assertTrue(info["valid"])

    def test_T2_null_expiry_survives_far_future(self):
        """即使把"现在"拨到很久以后，不过期的 token 仍然有效。"""
        r = fish.start_session(self.conn, alias="future-demo")
        far = "2099-01-01T00:00:00Z"
        self.assertEqual(
            fish.validate_session_token(self.conn, r["token"], at=far),
            r["citizen_id"], "永不过期的 token 在 2099 年也该有效")

    # ------------------------------------------------------------------ T3
    def test_T3_reissue_revokes_old_keeps_citizen(self):
        """重签：**旧 token 失效、公民不变**（凭证轮换的正确形态）。"""
        r1 = fish.start_session(self.conn, alias="rotate-demo")
        r2 = fish.start_session(self.conn, alias="rotate-demo")
        self.assertEqual(r1["citizen_id"], r2["citizen_id"], "公民必须不变")
        old = self._info(r1["token"])
        new = self._info(r2["token"])
        self.assertFalse(old["valid"], "旧 token 应失效")
        self.assertIsNotNone(old["revoked_at"], "旧 token 应被吊销（不是过期）")
        self.assertTrue(new["valid"])
        self.assertIsNone(new["expires_at"])

    # ------------------------------------------------------------------ T4
    def test_T4_explicit_ttl_still_expires(self):
        """显式给 `ttl_hours > 0` 时**仍会过期**（保留给子代理等临时场景）。"""
        r = fish.start_session(self.conn, alias="short-demo", ttl_hours=1)
        self.assertIsNotNone(r["expires_at"], "显式 TTL 应产生到期时刻")
        # 拨到 2 小时后 → 失效
        later = "2099-01-01T00:00:00Z"
        self.assertIsNone(fish.validate_session_token(self.conn, r["token"], at=later),
                          "显式 TTL 的 token 过期后应失效")

    # ------------------------------------------------------------------ T5
    def test_T5_archive_default_is_zero(self):
        """归档阈值默认 **0 = 不自动归档**（用户明确"N 不取数"）。"""
        self.assertEqual(fish.get_config(self.conn, "session.archive_after_days"), 0,
                         "默认不该自动归档会话；N 留给用户前端配置")

    # ------------------------------------------------------------------ T6
    def test_T6_manual_revoke_still_works(self):
        """手动吊销（`session end`）仍然有效——这是真正的"我要结束它"。"""
        r = fish.start_session(self.conn, alias="revoke-demo")
        self.assertEqual(fish.validate_session_token(self.conn, r["token"]),
                         r["citizen_id"])
        fish.end_session(self.conn, token=r["token"])
        self.assertIsNone(fish.validate_session_token(self.conn, r["token"]),
                          "吊销后必须失效")
        info = self._info(r["token"])
        self.assertIsNotNone(info["revoked_at"])
        self.assertIsNone(info["expires_at"], "吊销不是过期——expires_at 仍应为 NULL")

    # ------------------------------------------------------------------ T7
    def test_T7_reopen_same_session_same_citizen(self):
        """同一个会话恢复 → **同一个公民**（不该因为"重开"变成新公民）。"""
        r1 = fish.start_session(self.conn, alias="reopen-demo")
        r2 = fish.start_session(self.conn, alias="reopen-demo")
        self.assertEqual(r1["citizen_id"], r2["citizen_id"])
        self.assertFalse(r2.get("created"), "第二次不该新建公民")
        # 直接数"这个公民有几行"——不猜 session_key 的前缀格式
        n = self.conn.execute("SELECT COUNT(*) FROM citizens WHERE citizen_id=?",
                              (r1["citizen_id"],)).fetchone()[0]
        self.assertEqual(n, 1, f"应只有一个会话公民，实际 {n}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
