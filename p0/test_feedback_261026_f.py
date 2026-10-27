#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""反馈修复 F 批（2026-10-26 · F3）：CLI `fish whoami` 补「身份来源 / 凭证状态」两行。

## 病灶

MCP 的 `whoami` 早在 FB-03 / FB-07 就有那两行（`身份来源：…` / `凭证状态：…`），
CLI 的 `fish whoami` 一直只有「登记身份 / 主人 / 机器事实」。同一个 AI 从两个入口问
"我是谁"，得到的是详略不同的两种答案——而**最该被看见的**恰恰只在 MCP 那边：
这次回执凭什么算身份、我手上这条凭证还能用多久。

## 四条判据

F3-1 无凭证 → 含 `身份来源：未归属` 与 `凭证状态：未提供凭证`；
F3-2 无效凭证（库里没有）→ 含 `身份来源：未归属` 与 `凭证状态：未知`；
F3-3 有效凭证（`--token` 或 env `FISH_SESSION_TOKEN`）→ 含
     `身份来源：token（session_tokens）`，凭证状态落在四档之一；
F3-4 既有段不被破坏：`主人：` 仍在，`登记身份：`/`你：` 仍在；
     且两行与 MCP 回执**逐字一致**（同一实现，不是抄一份）。

## 最小运行（不要跑全量：全量归 QA）

    & "C:\\Users\\15596\\AppData\\Local\\Programs\\Python\\Python311\\python.exe" `
      -m unittest test_feedback_261026_f -v

必须在 `fish-fix/p0` 目录下跑（`fish.py` 按相对路径加载，与其他测试一致）。
"""
import argparse
import contextlib
import importlib.util
import io
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

import mcp_server        # noqa: E402

# 五档：没带 / 不在库中 / 已吊销 / **已过期** / 永久或剩余 N 小时。
# 用正则是刻意的：这条判据要钉的是"**必须**落在这几档里"，不钉某个具体的剩余小时数。
# 2026-10-26（批 G · 评审 🟠1）：补 `已过期` 一档——凭证状态从四档变五档，
# 这条"必须落在已知档位里"的判据要跟着认新档，否则它会把**正确的**新档判成红。
CREDENTIAL_RE = re.compile(
    r"^凭证状态：(未提供凭证|未知|已吊销|已过期|永久|剩余 \d+ 小时|到期时刻无法解析)")

_TOKEN_ENV = "FISH_SESSION_TOKEN"
# 会让 whoami 的身份段漂移的环境变量：全部清空，判据才只测"这两行"。
_NOISE_ENVS = ("FISH_ACTOR", "FISH_SESSION_ACTOR", _TOKEN_ENV,
               "DSH_SESSION_ID", "CODEBUDDY_SESSION_ID", "CLAUDE_SESSION_ID")


class WhoamiCliF3Test(unittest.TestCase):
    """CLI whoami 的口径必须与 MCP 完全一致。"""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.root = self.base / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.cid = self._citizen("F3-持证人")

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.base, ignore_errors=True)

    # ---------------------------------------------------------------- 造数据
    def _citizen(self, name, *, kind="visitor"):
        cid = fish.ulid()
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (cid, name, kind, None, "cli", "active", fish.now_utc()))
        self.conn.commit()
        return cid

    def _token(self, citizen_id, *, session_key="fish-f3:sess"):
        res = fish.issue_session_token(
            self.conn, citizen_id=citizen_id, session_key=session_key,
            evidence="self-reported", channel="cli")
        self.conn.commit()
        return res["token"]

    # ---------------------------------------------------------------- 跑 CLI
    @staticmethod
    @contextlib.contextmanager
    def _clean_env(**extra):
        """清掉会让身份段漂移的环境变量；`extra` 用来注入被测的那一个。"""
        env = {name: "" for name in _NOISE_ENVS}
        env.update(extra)
        with mock.patch.dict(os.environ, env):
            yield

    def _whoami(self, token=None):
        args = argparse.Namespace(root=str(self.root), citizen=None, token=token)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fish.cmd_whoami(args)
        return buf.getvalue()

    @staticmethod
    def _line(text, prefix):
        return next((ln for ln in text.splitlines() if ln.startswith(prefix)), None)

    # ---------------------------------------------------------------- F3-1
    def test_F3_1_no_token_declares_unattributed(self):
        with self._clean_env():
            out = self._whoami()
        self.assertIn("身份来源：未归属", out,
                      f"无凭证时必须明写「身份来源：未归属」；实际回执：\n{out}")
        self.assertIn("凭证状态：未提供凭证", out,
                      f"无凭证时必须明写「凭证状态：未提供凭证」；实际回执：\n{out}")
        self.assertNotIn("身份来源：token", out, "没带凭证说成 token 就是在编造归属来源")

    # ---------------------------------------------------------------- F3-2
    def test_F3_2_unknown_token_is_not_token_source(self):
        with self._clean_env():
            out = self._whoami(token="fsh_not_a_real_token")
        self.assertIn("身份来源：未归属", out,
                      f"库里查不到的 token 不算身份来源；实际回执：\n{out}")
        self.assertIn("凭证状态：未知", out,
                      f"假凭证要说清「这条 token 不在库中」；实际回执：\n{out}")

    # ---------------------------------------------------------------- F3-3
    def test_F3_3_valid_token_via_arg(self):
        token = self._token(self.cid)
        with self._clean_env():
            out = self._whoami(token=token)
        self.assertIn("身份来源：token（session_tokens）", out,
                      f"有效 token 的来源必须写 token；实际回执：\n{out}")
        line = self._line(out, "凭证状态：")
        self.assertIsNotNone(line, f"缺「凭证状态」行；实际回执：\n{out}")
        self.assertRegex(line, CREDENTIAL_RE,
                         f"凭证状态必须落在四档之一；实际：{line!r}")

    def test_F3_3_valid_token_via_env(self):
        token = self._token(self.cid, session_key="fish-f3:env")
        with self._clean_env(**{_TOKEN_ENV: token}):
            out = self._whoami()
        self.assertIn("身份来源：token（session_tokens）", out,
                      f"env 里的有效 token 也必须算来源；实际回执：\n{out}")
        self.assertRegex(self._line(out, "凭证状态：") or "", CREDENTIAL_RE,
                         f"凭证状态必须落在四档之一；实际回执：\n{out}")

    def test_F3_3_revoked_token_is_unknown_even_with_env(self):
        token = self._token(self.cid, session_key="fish-f3:dead")
        fish.revoke_session_tokens(self.conn, session_key="fish-f3:dead")
        self.conn.commit()
        with self._clean_env(**{_TOKEN_ENV: token}):
            out = self._whoami()
        self.assertIn("身份来源：未归属", out,
                      f"已吊销的凭证不能算身份来源；实际回执：\n{out}")
        self.assertIn("凭证状态：已吊销", out,
                      f"吊销要写明「已吊销」；实际回执：\n{out}")

    # ---------------------------------------------------------------- F3-4
    def test_F3_4_legacy_sections_survive(self):
        token = self._token(self.cid)
        # 给一个 actor（否则走「你：未登记访客」那条支路，测不到「登记身份」段）。
        with self._clean_env(FISH_ACTOR="F3-持证人"):
            out = self._whoami(token=token)
        self.assertIn("主人：", out, f"既有「主人」段被弄丢了：\n{out}")
        self.assertIn("登记身份：", out, f"既有「登记身份」段被弄丢了：\n{out}")
        self.assertIn("说明：这是你在 fish 的账户名", out,
                      f"既有的账户名说明被弄丢了：\n{out}")

    def test_F3_4_unregistered_actor_keeps_old_wording(self):
        """没有 actor 时旧文案「你：未登记访客」也不许被新行挤掉。"""
        with self._clean_env():
            out = self._whoami()
        self.assertIn("你：未登记访客", out, f"旧文案被弄丢了：\n{out}")
        self.assertIn("主人：", out, f"既有「主人」段被弄丢了：\n{out}")

    def test_F3_4_lines_are_verbatim_same_as_mcp(self):
        """两行必须与 MCP 回执**逐字一致**：同一实现，不是抄一份。"""
        token = self._token(self.cid)
        with self._clean_env():
            out = self._whoami(token=token)
        srv = mcp_server.FishServer(self.root)
        try:
            mcp_text = srv.call_tool("whoami", {"token": token})
        finally:
            srv.conn.close()
        for prefix in ("凭证状态：", "身份来源："):
            self.assertEqual(self._line(out, prefix), self._line(mcp_text, prefix),
                             f"CLI 与 MCP 的「{prefix}」口径漂了："
                             f"\nCLI: {self._line(out, prefix)!r}"
                             f"\nMCP: {self._line(mcp_text, prefix)!r}")
        # 无凭证那档也要一致（最容易各写一句、最容易漂的地方）。
        with self._clean_env():
            out_anon = self._whoami()
        srv_anon = mcp_server.FishServer(self.root)
        try:
            mcp_anon = srv_anon.call_tool("whoami", {})
        finally:
            srv_anon.conn.close()
        for prefix in ("凭证状态：", "身份来源："):
            self.assertEqual(self._line(out_anon, prefix), self._line(mcp_anon, prefix),
                             f"无凭证时 CLI 与 MCP 的「{prefix}」口径漂了："
                             f"\nCLI: {self._line(out_anon, prefix)!r}"
                             f"\nMCP: {self._line(mcp_anon, prefix)!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
