#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立评审 🔴-1 判据：**有凭证 ≠ 有授权**。

## 评审官实测的问题（我自己复核成立）

`enforce_mem_type_auth` 只问 `has_credential`——"你有有效 token 吗"。
但 MCP 的 `session_start` 对任何调用方**无门槛**签发 visitor 公民 + token。
完整链路：

    session_start(kind="visitor") -> token -> remember(mem_type="rule")
    -> 成功 -> 该规矩进所有人的注入包，且无 untrusted 标记

生产库实测：**71 条 rule 里 10 条作者是 kind=visitor 的公民**。

## 我原来的推断错在哪

F-P0-3 里我查了"rule 71 条 / 未归属 0"，据此说"凭证门有效"。
**错在把「有归属」当成「有授权」**：一条 rule 可以既有明确作者、又不该被允许写。
归属回答"是谁写的"，授权回答"他凭什么能写"——这是两个问题。

## 这批判据为什么能失败

把授权门删掉，第一条就红。而它红的时候，陌生人已经能给所有人写 AI 行为约束了。
"""
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish  # noqa: E402
import fish_trust  # noqa: E402


class RuleAuthorizationTest(unittest.TestCase):

    def test_visitor_cannot_write_rule(self):
        """**核心**：visitor 即使有有效凭证，也不许写 rule/identity。"""
        for kind in ("rule", "identity"):
            with self.assertRaises(ValueError, msg=f"{kind} 没被拦") as ctx:
                fish_trust.enforce_mem_type_auth(
                    kind, has_credential=True, citizen_kind="visitor")
            msg = str(ctx.exception)
            self.assertIn("被授予", msg, f"报错没说清是授权问题：{msg[:160]}")
            self.assertIn("resident", msg, "报错应指出怎么获得授权")

    def test_resident_and_role_can_write_rule(self):
        """对照组：resident / role 必须能写——否则"一律拒绝"也能让上面变绿。"""
        for kind in ("resident", "role"):
            fish_trust.enforce_mem_type_auth(
                "rule", has_credential=True, citizen_kind=kind)

    def test_unknown_kind_is_refused(self):
        """判不到 kind 时**不放行**——严格优先，因为放行的代价是"陌生人能改 AI 行为"。"""
        with self.assertRaises(ValueError):
            fish_trust.enforce_mem_type_auth(
                "rule", has_credential=True, citizen_kind=None)

    def test_no_credential_still_refused(self):
        """无凭证本来就该拒——这条保证授权门没把旧的门挤掉。"""
        with self.assertRaises(ValueError):
            fish_trust.enforce_mem_type_auth(
                "rule", has_credential=False, citizen_kind="resident")

    def test_free_types_unaffected(self):
        """fact / lesson 等免凭证类型不受影响——授权门只管约束类。"""
        for kind in ("fact", "episode", "lesson", "artifact"):
            fish_trust.enforce_mem_type_auth(kind, has_credential=False)
            fish_trust.enforce_mem_type_auth(kind, has_credential=True,
                                             citizen_kind="visitor")

    def test_citizen_kind_of_does_not_guess(self):
        """`citizen_kind_of` 查不到就返回 None，**不猜**。

        "查不到就当 resident" 正是这条缺陷的成因形态：把判不到的事实当成有利结论。
        """
        tmp = tempfile.mkdtemp(prefix="fish-trust-")
        root = Path(tmp) / "data"
        root.mkdir(parents=True)
        conn = fish.connect(root)
        try:
            conn.executescript(fish.SCHEMA)
            fish.migrate(conn)
            self.assertIsNone(fish_trust.citizen_kind_of(conn, "C-NOBODY"))
            self.assertIsNone(fish_trust.citizen_kind_of(conn, None))
            self.assertIsNone(fish_trust.citizen_kind_of(conn, ""))
            conn.execute(
                "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,"
                "created_at) VALUES (?,?,?,?,?,?,?)",
                ("C-V", "访客", "visitor", None, None, "active", fish.now_utc()))
            conn.commit()
            self.assertEqual(fish_trust.citizen_kind_of(conn, "C-V"), "visitor")
        finally:
            conn.close()
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_authorized_kinds_are_the_documented_ones(self):
        """授权档位是**明确枚举**的，不是一个宽泛的正则或"非 visitor"——"""
        self.assertEqual(tuple(fish_trust.AUTHORIZED_CITIZEN_KINDS),
                         ("resident", "role"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
