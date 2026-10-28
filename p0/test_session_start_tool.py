#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 面的签发口 `session_start`（2026-10-25 · 外部评估 `EVAL-261025-05`）。

背景
----
在此之前 MCP 面 11 个工具里**没有任何一个能产出 token**：`whoami` / `inject` 有 token
**入参**，但签发口在 CLI 的 `session start`。于是 AI 只能离开 MCP 去跑命令行——
而两边 `host_session_key` 不同，`find_or_create` 会**新建一个公民**，
两条通道的归属从此对不上、且无法合并。

用户 2026-10-25 批准把工具数从 14 加到 15，换这个缺口被真正补上。

判据
----
  S1  同一 `session_name` 两次调用 → **同一个公民**（幂等），只换 token
  S2  `citizen=` 显式绑定时 → 落在那个公民上（不新建）
  S3  `session_name` 缺失 → **响亮失败**（不传就每次建新公民，那正是要治的病）
  S4  明文 token 可用：拿它调 `whoami` 能解出同一个 citizen
  S5  库表里**不存明文**（只有 sha256）
  S6  `citizen=` 指向不存在的公民 → 响亮失败（静默绑错比不绑更糟）
"""
import importlib.util
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

import mcp_server  # noqa: E402


class SessionStartToolTest(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / "data"
        self.srv = mcp_server.FishServer(self.root)
        self.anon = self.srv._resolve_identity(None)

    def tearDown(self):
        try:
            self.srv.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.root.parent, ignore_errors=True)

    def _citizen_of(self, out):
        import re
        m = re.search(r"公民 (\w+)", out)
        self.assertIsNotNone(m, f"回执里找不到公民 id：{out[:200]}")
        return m.group(1)

    def _token_of(self, out):
        import re
        m = re.search(r"token:\s*(fsh_\S+)", out)
        self.assertIsNotNone(m, f"回执里找不到 token：{out[:300]}")
        return m.group(1)

    # ------------------------------------------------------------------ S1
    def test_S1_same_session_name_reuses_citizen(self):
        """同一 session_name 两次调用必须是同一个公民——这是幂等锚的意义。"""
        o1 = self.srv.tool_session_start({"session_name": "demo-dev"}, self.anon)
        o2 = self.srv.tool_session_start({"session_name": "demo-dev"}, self.anon)
        c1, c2 = self._citizen_of(o1), self._citizen_of(o2)
        self.assertEqual(c1, c2,
                         f"同一 session_name 建出了两个公民：{c1} / {c2}\n"
                         "那正是这个工具要治的病")
        self.assertIn("复用已有", o2, "第二次应标明复用")
        # token 必须换新（轮换），不能复用同一个明文
        self.assertNotEqual(self._token_of(o1), self._token_of(o2),
                            "两次调用应轮换 token")

    # ------------------------------------------------------------------ S2
    def test_S2_bind_to_existing_citizen(self):
        """`citizen=` 显式绑定：落在那个公民上，不新建。"""
        o1 = self.srv.tool_session_start({"session_name": "dev-a"}, self.anon)
        cid = self._citizen_of(o1)
        n_before = self.srv.conn.execute("SELECT COUNT(*) FROM citizens").fetchone()[0]
        # S-01：显式绑定已有公民要出示证明（你现有的 token）
        o2 = self.srv.tool_session_start(
            {"session_name": "another-session", "citizen": cid,
             "bind_token": self._token_of(o1)}, self.anon)
        self.assertEqual(self._citizen_of(o2), cid, "应绑到指定的公民上")
        n_after = self.srv.conn.execute("SELECT COUNT(*) FROM citizens").fetchone()[0]
        self.assertEqual(n_before, n_after, f"不该新建公民（{n_before} → {n_after}）")

    # ------------------------------------------------------------------ S3
    def test_S3_missing_session_name_fails_loudly(self):
        """session_name 缺失必须响亮失败（不是默默建一个新公民）。"""
        with self.assertRaises(ValueError) as ctx:
            self.srv.tool_session_start({}, self.anon)
        msg = str(ctx.exception)
        self.assertIn("session_name", msg)
        self.assertIn("幂等", msg, "错误信息要讲清为什么它是必填的")

    # ------------------------------------------------------------------ S4
    def test_S4_token_works(self):
        """签出来的 token 真的能用：拿它解析身份 → 同一个 citizen。"""
        out = self.srv.tool_session_start({"session_name": "verify-me"}, self.anon)
        cid, tok = self._citizen_of(out), self._token_of(out)
        ident = self.srv._resolve_identity(tok)
        self.assertEqual(ident.status, mcp_server.IDENTITY_VALID,
                         f"token 解析失败：{ident.status}")
        self.assertEqual(ident.citizen_id, cid,
                         f"token 解出的公民与回执不一致：{ident.citizen_id} vs {cid}")

    # ------------------------------------------------------------------ S5
    def test_S5_no_plaintext_in_db(self):
        """库表里**不存明文 token**，只存 sha256。"""
        out = self.srv.tool_session_start({"session_name": "leak-check"}, self.anon)
        tok = self._token_of(out)
        db = self.root / "index.sqlite"
        con = sqlite3.connect("file:" + str(db) + "?mode=ro", uri=True)
        try:
            # 全文扫一遍：明文不许出现在任何表的任何文本列里
            hits = []
            for (name,) in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"):
                try:
                    cols = [r[1] for r in con.execute(f"PRAGMA table_info({name})")]
                except sqlite3.Error:
                    continue
                for c in cols:
                    try:
                        n = con.execute(
                            f"SELECT COUNT(*) FROM {name} WHERE CAST({c} AS TEXT) LIKE ?",
                            (f"%{tok}%",)).fetchone()[0]
                    except sqlite3.Error:
                        continue
                    if n:
                        hits.append(f"{name}.{c}")
            self.assertEqual(hits, [], f"明文 token 出现在这些列里：{hits}")
        finally:
            con.close()

    # ------------------------------------------------------------------ S6
    def test_S6_bad_citizen_fails_loudly(self):
        """绑到不存在的公民 → 响亮失败（静默绑错比不绑更糟）。"""
        with self.assertRaises(ValueError) as ctx:
            self.srv.tool_session_start(
                {"session_name": "x", "citizen": "01NOSUCHCITIZEN00000000000"}, self.anon)
        self.assertIn("不存在", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
