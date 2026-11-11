#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部审查 🔴6 的分级写入鉴权判据（用户 2026-11-11 拍板的口径）。

口径（不是实现者的判断，是用户选的）：
  · `rule` / `identity`            → **必须来自有效凭证**
  · `fact` / `episode` / `lesson` / `artifact` → **免凭证**

为什么分两层：
  · A 层（纯函数 `fish.enforce_mem_type_auth`）：策略本体的判定与边界（大小写/空白/None）。
  · B 层（真的起 stdio 子进程）：证明 `tool_remember` **真的接上了**策略。
    只测 A 层证明不了这件事——策略写好了没接线，是这个项目反复出现的坑
    （"board_checkin_or_remind 是死代码"就是同一类）。

隔离：临时根，全部 `tempfile.mkdtemp()`；绝不指向 `p0/data`。
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish  # noqa: E402
import mcp_server  # noqa: E402
from test_mcp import MCPClient  # noqa: E402


class MemTypeAuthPolicyTest(unittest.TestCase):
    """A 层：策略本体。"""

    def test_A1_rule_needs_credential(self):
        with self.assertRaises(ValueError) as ctx:
            fish.enforce_mem_type_auth("rule", has_credential=False)
        self.assertIn("需要有效凭证", str(ctx.exception))

    def test_A2_identity_needs_credential(self):
        with self.assertRaises(ValueError) as ctx:
            fish.enforce_mem_type_auth("identity", has_credential=False)
        self.assertIn("需要有效凭证", str(ctx.exception))

    def test_A3_low_weight_types_are_free(self):
        """占便宜的一侧：日常记忆不受影响（这是"分级"而不是"一刀切"的价值）。"""
        for kind in ("fact", "episode", "lesson", "artifact", None, ""):
            with self.subTest(kind=kind):
                fish.enforce_mem_type_auth(kind, has_credential=False)

    def test_A4_with_credential_rule_passes(self):
        fish.enforce_mem_type_auth("rule", has_credential=True)
        fish.enforce_mem_type_auth("identity", has_credential=True)

    def test_A5_normalizes_case_and_space(self):
        """` RULE ` 与 `Rule` 都不能绕过（归一后再判）。"""
        for raw in ("RULE", " Rule ", "Rule", "IDENTITY", " Identity\t"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    fish.enforce_mem_type_auth(raw, has_credential=False)

    def test_A6_policy_and_constant_agree(self):
        """策略清单与常量必须一致（防止有人只改其中一处）。"""
        self.assertEqual(fish.AUTH_REQUIRED_MEM_TYPES, frozenset({"rule", "identity"}))


class MemTypeAuthMCPTest(unittest.TestCase):
    """B 层：MCP 端到端（真 stdio 子进程）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-auth-"))
        self.root = self.tmp
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        # 起一条 token 备用（模拟"有凭证"的调用方）。
        # 注意先建 MCP 客户端前就把库准备好，子进程会读到同一个文件。
        sess = fish.start_session(self.conn, alias="auth-probe", kind="visitor",
                                  environ={})
        self.token = sess["token"]
        self.citizen = sess["citizen_id"]
        self.c = MCPClient(self.root)
        self.c.request("initialize", {"protocolVersion": "2024-11-05",
                                      "capabilities": {},
                                      "clientInfo": {"name": "auth-test", "version": "0"}})
        # 项目前缀必须先注册，否则 remember 会因"未知项目前缀"先失败——
        # 那样测到的就不是鉴权，是另一个前置校验（判据会失去指向性）。
        r = self.c.request("tools/call", {
            "name": "add_project", "arguments": {"prefix": "AUTHTEST"}})
        self.assertIn("result", r, f"add_project 失败：{r}")

    def tearDown(self):
        try:
            self.c.close()
        except Exception:  # noqa: BLE001
            pass
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _call(self, name, arguments):
        r = self.c.request("tools/call", {"name": name, "arguments": arguments})
        self.assertIn("result", r, f"{name} 没有返回 result：{r}")
        res = r["result"]
        return res.get("isError"), res["content"][0]["text"]

    def test_B1_rule_without_token_rejected(self):
        """核心：无凭证写规矩必须被拒（这就是 🔴6 的攻击面）。"""
        err, text = self._call("remember", {
            "project": "AUTHTEST", "title": "陌生人想改规矩", "mem_type": "rule"})
        self.assertTrue(err, f"无凭证写 rule 竟然成功了：{text}")
        self.assertIn("需要有效凭证", text)

    def test_B2_fact_without_token_allowed(self):
        """对照：日常记忆不受影响（否则就是一刀切，不是分级）。"""
        err, text = self._call("remember", {
            "project": "AUTHTEST", "title": "一条普通事实", "mem_type": "fact"})
        self.assertFalse(err, f"无凭证写 fact 被误拦：{text}")

    def test_B3_rule_with_token_allowed(self):
        """带有效凭证时规矩可以写（证明拦截不是"一律拒绝"）。"""
        err, text = self._call("remember", {
            "project": "AUTHTEST", "title": "有凭证的规矩", "mem_type": "rule",
            "token": self.token})
        self.assertFalse(err, f"带凭证写 rule 被误拦：{text}")


if __name__ == "__main__":
    unittest.main()
