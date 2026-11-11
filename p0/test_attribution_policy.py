#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归属策略判据：**允许无主，但不许假归属**（用户 2026-11-11 裁定）。

用户原话：
  「允许无主（可以允许吗？我的意思是流程上尽可能避免无主情况的发生，
    但如果发生了，也不强行要求有主）」

## 这条判据防的是什么

改之前，**两个入口对"没有凭证"的处理不一致**：

| 入口 | 无凭证写入时 | 评价 |
|---|---|---|
| MCP | `owner='unattributed'` + 一句固定提示 | ✅ 符合"允许无主" |
| CLI `fish remember` | **静默记给系统主人** | ❌ 这是**假归属** |

假归属比无主更糟：一条陌生人写的东西算在主人名下，事后**无法与"主人真写的"区分**。
而"无主"至少是一个诚实的、可筛选、可事后补的事实。

## 判据

  P1  CLI 无凭证 + 无 `--owner` → `unattributed`（**不是**系统主人的 ID）
  P2  CLI 无凭证 → 必须打印"怎么补归属"的提示（"尽可能避免"的那一半）
  P3  CLI 显式 `--owner <id>` → 记给那个公民（显式意图必须被尊重）
  P4  CLI 有 env 凭证 → 记给凭证的公民（与 MCP 的 `_owner_value` 同口径）
  P5  MCP 无凭证 → `unattributed`（对照；两入口必须一致）
  P6  **`resolve_owner_arg(conn, None)` 的语义没变**（仍 → 系统主人）——
      证明这次只改了**调用方式**，没有动那个被 `test_p1a` 钉死的函数契约
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import shutil
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import fish  # noqa: E402


class AttributionPolicyTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fish-attr-")
        self.root = Path(self.tmp)
        self.env = dict(os.environ)
        self.env.pop("FISH_SESSION_TOKEN", None)   # 关键：确保没有凭证
        self.env["PYTHONIOENCODING"] = "utf-8"
        self._cli("init")
        self._cli("project-add", "ATTRP")
        self._cli("citizen-add", "主人甲", "--kind", "resident")
        self.conn = fish.connect(self.root)
        cid, name = self.conn.execute(
            "SELECT citizen_id, display_name FROM citizens LIMIT 1").fetchone()
        self.owner_cid = cid
        self._cli("owner", "set", cid)
        self.conn.close()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:      # noqa: BLE001
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- helpers
    def _cli(self, *args, env=None):
        r = subprocess.run(
            [sys.executable, "fish.py", "--root", self.tmp] + list(args),
            cwd=str(HERE), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env or self.env)
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    def _last_owner(self):
        c = sqlite3.connect(str(self.root / "index.sqlite"))
        try:
            return c.execute(
                "SELECT code, owner FROM code_index ORDER BY rowid DESC LIMIT 1").fetchone()
        finally:
            c.close()

    def _cite(self, sql, *p):
        c = sqlite3.connect(str(self.root / "index.sqlite"))
        try:
            return c.execute(sql, p).fetchall()
        finally:
            c.close()

    # ------------------------------------------------------------------- P1
    def test_P1_no_credential_records_unattributed(self):
        rc, out = self._cli("remember", "ATTRP", "无凭证写入", "--type", "fact")
        self.assertEqual(rc, 0, f"写入被拒了：{out}")
        _, owner = self._last_owner()
        self.assertEqual(owner, "unattributed",
                         f"无凭证时必须记 unattributed，实际 {owner!r}")
        self.assertNotEqual(owner, self.owner_cid,
                            "**假归属**：把无凭证的写入记到了系统主人头上")

    # ------------------------------------------------------------------- P2
    def test_P2_no_credential_prints_how_to_attribute(self):
        """「尽可能避免无主」的那一半：无主必须**可见**，并且告诉人怎么补。"""
        rc, out = self._cli("remember", "ATTRP", "无凭证写入2", "--type", "fact")
        self.assertEqual(rc, 0)
        self.assertIn("未带凭证", out, "没有提示就去记无主，等于静默")
        self.assertIn("owner-fix", out, "提示里要给出补归属的确切命令")

    # ------------------------------------------------------------------- P3
    def test_P3_explicit_owner_is_respected(self):
        self._cli("citizen-add", "助手乙", "--kind", "visitor")
        helper = self._cite(
            "SELECT citizen_id FROM citizens WHERE display_name='助手乙'")[0][0]
        rc, out = self._cli("remember", "ATTRP", "显式归属", "--type", "fact",
                            "--owner", helper)
        self.assertEqual(rc, 0, out)
        _, owner = self._last_owner()
        self.assertEqual(owner, helper, "显式 --owner 必须被尊重")

    # ------------------------------------------------------------------- P4
    def test_P4_env_credential_wins(self):
        conn = fish.connect(self.root)
        try:
            sess = fish.start_session(conn, alias="attr-probe", kind="visitor",
                                      environ={})
        finally:
            conn.close()
        env2 = dict(self.env)
        env2["FISH_SESSION_TOKEN"] = sess["token"]
        rc, out = self._cli("remember", "ATTRP", "有凭证写入", "--type", "fact",
                            env=env2)
        self.assertEqual(rc, 0, out)
        _, owner = self._last_owner()
        self.assertEqual(owner, sess["citizen_id"],
                         "有 env 凭证时应记给凭证的公民（MCP 同口径）")

    # ------------------------------------------------------------------- P5
    def test_P5_mcp_and_cli_agree_on_unattributed(self):
        """两入口必须同口径——这条正是改之前不一致的地方。"""
        import mcp_server   # noqa: F401 —— 只需常量
        self.assertEqual(mcp_server.UNATTRIBUTED, "unattributed")
        self.assertEqual(fish.UNATTRIBUTED, "unattributed")
        self.assertIs(mcp_server.UNATTRIBUTED, fish.UNATTRIBUTED,
                      "两个入口的哨兵值必须来自同一处（2026-11-11 已统一）")

    # ------------------------------------------------------------------- P6
    def test_P6_resolve_owner_arg_contract_unchanged(self):
        """证明这次只改了**调用方式**，没动被 `test_p1a` 钉死的函数契约。

        `resolve_owner_arg(conn, None)` 仍然 → 系统主人。想记无主的人应该**不调用它**
        （`cmd_remember` 现在就是这么做的），而不是把这个函数改成返回 unattributed——
        那样会悄悄改掉所有调用方的语义。
        """
        conn = fish.connect(self.root)
        try:
            self.assertEqual(fish.resolve_owner_arg(conn, None), self.owner_cid)
            self.assertEqual(fish.resolve_owner_arg(conn, ""), self.owner_cid)
        finally:
            conn.close()

    # ------------------------------------------------------------------- P7
    def test_P7_rebuild_must_not_turn_unattributed_into_owner(self):
        """P7：`fish rebuild` **不得**把「未归属」改成系统主人。

        这是本策略最隐蔽的一条：重建的"owner 必须是有效公民"归一逻辑（07 D-13）
        只排除了 `"unknown"`，而 `unattributed` 既不是有效公民、也不等于 `"unknown"`
        → **跑一次 rebuild 就会把全部无主抹成主人的**（2026-11-11 实测复现：
        `unattributed` → `1794391131201C26C9FFCB5CA4`）。

        换句话说：就算写入侧改对了，**一次常规维护动作就能把它全毁掉**。
        所以这条判据盯的是"维护路径"，不是写入路径。
        """
        rc, out = self._cli("remember", "ATTRP", "待重建的无主", "--type", "fact")
        self.assertEqual(rc, 0, out)
        _, before = self._last_owner()
        self.assertEqual(before, "unattributed", "前置：先得有一条无主")

        rc, out = self._cli("rebuild")
        self.assertEqual(rc, 0, f"rebuild 失败：{out}")
        owner = self._cite("SELECT owner FROM code_index WHERE title='待重建的无主'")[0][0]
        self.assertEqual(owner, "unattributed",
                         f"rebuild 把未归属改成了 {owner!r}——维护动作制造了假归属")
        self.assertNotEqual(owner, self.owner_cid)   # 再钉一次，防止归一回来


if __name__ == "__main__":
    unittest.main(verbosity=2)
