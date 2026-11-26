#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-6 + P2-6#1/#10（外部审查 2026-11-25）判据：重绑吊销 / 死代码 / 锚语义。

## P1-6 修的是什么

`ensure_session` 在"本次要签给**另一个**公民"（显式 `--citizen`，或身份锚改了）时
只签发新 token，**不吊销旧的**。实测：`start(alice)` → token A；`ensure(citizen=bob)`
之后 `validate_session_token(A)` 仍返回 alice，同一会话键两条活凭证。旧 token 若已
泄露，重绑收不回旧身份。修法=签发前先 `revoke_session_tokens(session_key=key)`。

判据（**每条都能被变异打红**）：
  · 重绑后旧 token `validate_session_token` → None，且 `revoked_at` 非空；
  · 同一 session_key 的活凭证数 = 1；
  · 返回里 `rotated` 与 `session.ensured` 事件的 `revoked` 都如实计数；
  · 对照组：**同一公民**重跑 ensure 仍不轮换（`token_source='existing'`、`rotated=0`）
    ——没有它，"每次都吊销重签"也能让上面几条变绿，而那会把 hook 互踢的 bug 请回来。

## P2-6#1：`start_session` 里的 `and False:` 死块

那一段永不执行（`and False`），却留着一次 `_prove_citizen_binding` 调用，让读者以为
`self:` 认领还需要证明。删掉后行为不变，用两条判据钉住：源码里不再有死分支 /
`start_session` 内不再直接调证明；`self:` 会话重跑 start 仍不要求证明。

## P2-6#10：锚 `sessions: []` 的语义统一

`fish_dsh_hook._identity_anchor` 旧语义是"空 = 不限制"（照样传 `--citizen`），
而 `fish_trust.identity_anchor_grants` 对空白名单是**拒绝** → 每个会话的 `ensure`
都缺证明降级。现在两边统一成"锚必须点名会话"。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish  # noqa: E402
import fish_dsh_hook  # noqa: E402
import fish_trust  # noqa: E402


class _TempRoot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="fish-rebind-")
        self.root = Path(self._tmp) / "data"
        self._old_root_env = os.environ.get("FISH_ROOT")
        os.environ["FISH_ROOT"] = str(self.root)      # hook 侧 _data_root() 读它
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        if self._old_root_env is None:
            os.environ.pop("FISH_ROOT", None)
        else:
            os.environ["FISH_ROOT"] = self._old_root_env
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _start(self, alias):
        """`alias` 走自报名 → session_key = `self:<alias>`（不依赖宿主 env）。"""
        res = fish.start_session(self.conn, alias=alias, environ={})
        self.assertEqual(res["session_key"], f"self:{alias}")
        return res

    def _live_tokens(self, session_key):
        return self.conn.execute(
            "SELECT token_hash,citizen_id FROM session_tokens "
            "WHERE session_key=? AND revoked_at IS NULL", (session_key,)).fetchall()


class EnsureRebindRevokesTest(_TempRoot):
    """P1-6：重绑必须吊销旧活凭证。"""

    def test_rebind_revokes_old_token(self):
        """**核心**：alice 的会话重绑到 bob 后，alice 那条 token 立刻失效。"""
        alice = self._start("alice")
        bob = self._start("bob")
        a_token, a_key = alice["token"], alice["session_key"]
        before = fish.validate_session_token(self.conn, a_token)
        self.assertEqual(before, alice["citizen_id"], "前置：alice 的 token 本来有效")

        res = fish.ensure_session(
            self.conn, session_key=a_key, host="cli", evidence="self-reported",
            citizen=bob["citizen_id"], bind_token=bob["token"])

        self.assertEqual(res["citizen_id"], bob["citizen_id"], "身份没绑到 bob")
        self.assertEqual(res["token_source"], "issued", "重绑必须签发新凭证")
        self.assertIsNone(fish.validate_session_token(self.conn, a_token),
                          "**旧 token 重绑后仍然有效** —— 这就是 P1-6：旧身份收不回来")
        info = fish.lookup_session_token(self.conn, a_token)
        self.assertTrue(info["revoked_at"], "旧 token 的 revoked_at 必须非空（只标不删）")
        live = self._live_tokens(a_key)
        self.assertEqual(len(live), 1,
                         f"同一会话键应只剩 1 条活凭证，实际 {len(live)} 条：{live}")
        self.assertEqual(live[0][1], bob["citizen_id"], "唯一活凭证必须属于 bob")

    def test_revoke_count_is_reported(self):
        """吊销条数要能被读到：返回 `rotated` 与 `session.ensured` 事件同口径。"""
        alice = self._start("alice")
        bob = self._start("bob")
        res = fish.ensure_session(
            self.conn, session_key=alice["session_key"], host="cli",
            evidence="self-reported", citizen=bob["citizen_id"],
            bind_token=bob["token"])
        self.assertEqual(res["rotated"], 1, f"应当吊销 1 条旧活凭证：{res!r}")
        payload = json.loads(self.conn.execute(
            "SELECT payload FROM events WHERE type='session.ensured' "
            "ORDER BY seq DESC LIMIT 1").fetchone()[0])
        self.assertEqual(payload.get("revoked"), 1,
                         f"事件里没有如实记 revoked：{payload}")

    def test_same_citizen_ensure_does_not_revoke(self):
        """**对照组**：同一公民重跑 ensure 仍不轮换（否则 hook 会互相踢掉凭证）。"""
        alice = self._start("alice")
        res = fish.ensure_session(
            self.conn, session_key=alice["session_key"], host="cli",
            evidence="self-reported")
        self.assertEqual(res["token_source"], "existing", "同一公民不该重签")
        self.assertEqual(res["rotated"], 0, "同一公民不该吊销任何凭证")
        self.assertEqual(fish.validate_session_token(self.conn, alice["token"]),
                         alice["citizen_id"], "原 token 必须继续有效")

    def test_first_ensure_without_live_token_revokes_nothing(self):
        """没有活凭证的会话：ensure 只签发，`rotated=0`（没人可收回）。"""
        res = fish.ensure_session(self.conn, session_key="self:fresh", host="cli",
                                  evidence="self-reported")
        self.assertEqual(res["token_source"], "issued")
        self.assertEqual(res["rotated"], 0)
        self.assertTrue(fish.validate_session_token(self.conn, res["token"]))


class SelfClaimNeedsNoProofTest(_TempRoot):
    """P2-6#1：`self:` 认领不要求证明（死分支已删，行为不变）。"""

    def test_self_session_restart_needs_no_proof(self):
        """重跑 start 必须幂等成功——若死块被"复活"，这里会因缺证明红。"""
        first = self._start("probe")
        second = self._start("probe")
        self.assertEqual(second["citizen_id"], first["citizen_id"],
                         "重跑 start 换了公民（幂等被破坏）")
        self.assertGreaterEqual(second["rotated"], 1, "重跑 start 应当轮换掉旧的 1 条")

    def test_source_has_no_dead_false_branch(self):
        """源码判据（AST）：`start_session` 里不再有 `… and False` 死分支，也不直调证明。

        为什么用 AST 而不是 `assertNotIn("and False", src)`：注释里会提到这个片段
        （本文件的 docstring 与 fish.py 的注释都要说清"删了什么"），字符串判据会把
        **说明**当成**代码**。AST 只看真正的 `if` 测试表达式。
        """
        import ast
        # 2026-11-26（W3-a · 144 刀一）：`start_session` 随身份/会话域搬进 fish_session.py。
        src = (HERE / "fish_session.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        func = next((n for n in tree.body
                     if isinstance(n, ast.FunctionDef) and n.name == "start_session"), None)
        self.assertIsNotNone(func, "找不到 start_session")

        def _has_false_constant(node):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and sub.value is False:
                    return True
            return False

        dead = [n.lineno for n in ast.walk(func)
                if isinstance(n, ast.If) and _has_false_constant(n.test)]
        self.assertEqual(dead, [],
                         f"`start_session` 里还有恒假的 `if … and False` 分支（行 {dead}）")
        proof_calls = [n.lineno for n in ast.walk(func)
                       if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                       and n.func.id == "_prove_citizen_binding"]
        self.assertEqual(proof_calls, [],
                         f"start_session 内又出现直接调证明（行 {proof_calls}）："
                         "`self:` 那条路径不该要求证明")


class IdentityAnchorEmptySessionsTest(_TempRoot):
    """P2-6#10：锚 `sessions: []` 时 hook 不传 `--citizen`，与 fish_trust 同语义。"""

    CID = "C-ANCHOR-0001"

    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,host,status,created_at)"
            " VALUES (?,?,?,?,?,?)",
            (self.CID, "锚主人", "resident", "dsh", "active", fish.now_utc()))
        self.conn.commit()
        self.anchor_dir = Path(self._tmp) / "anchor"
        self.anchor_dir.mkdir()
        self._old_here = fish_dsh_hook.HERE
        fish_dsh_hook.HERE = self.anchor_dir      # hook 的锚文件路径可注入

    def tearDown(self):
        fish_dsh_hook.HERE = self._old_here
        super().tearDown()

    def _write_anchor(self, **data):
        (self.anchor_dir / "fish_identity.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return self.anchor_dir / "fish_identity.json"

    def _citizen_exists(self):
        """前置：hook 侧的 `_citizen_exists` 必须能在临时库里查到（否则锚一律作废）。"""
        return fish_dsh_hook._citizen_exists(self.CID)

    def test_empty_sessions_anchor_grants_nothing(self):
        """**核心**：`sessions: []` → hook 不认这个锚（也就不传 `--citizen`）。"""
        self.assertTrue(self._citizen_exists(), "前置：临时库里必须有这个公民（否则判据是空的）")
        path = self._write_anchor(citizen=self.CID, sessions=[])
        self.assertEqual(fish_dsh_hook._identity_anchor("sess-1"), {},
                         "空 sessions 仍被当成「不限制」——hook 会传 --citizen 而 ensure 拒绝证明")
        self.assertFalse(fish_trust.identity_anchor_grants(
            self.CID, "dsh:sess-1", anchor_path=path),
            "fish_trust 侧必须同样拒绝（两边语义要一致）")

    def test_named_session_anchor_works(self):
        """对照组：白名单点名了本会话 → 两边都放行（不是"一律拒绝"）。"""
        path = self._write_anchor(citizen=self.CID, sessions=["dsh:sess-1"])
        anchor = fish_dsh_hook._identity_anchor("sess-1")
        self.assertEqual(anchor.get("citizen"), self.CID, "点名的会话应当拿到锚")
        self.assertTrue(fish_trust.identity_anchor_grants(
            self.CID, "dsh:sess-1", anchor_path=path))

    def test_other_session_is_not_granted(self):
        """对照组：锚里没有本会话 → 不授权（白名单仍然有效）。"""
        path = self._write_anchor(citizen=self.CID, sessions=["dsh:other"])
        self.assertEqual(fish_dsh_hook._identity_anchor("sess-1"), {})
        self.assertFalse(fish_trust.identity_anchor_grants(
            self.CID, "dsh:sess-1", anchor_path=path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
