#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B3 · 知识库工具面端到端判据（T05）。

真的起一个 stdio 子进程（复用 `test_mcp.MCPClient`，不是单元替身）：这一层要验的是
「宿主能不能通过 MCP 读出知识库」——协议握手、schema、回执头、审计归属、拒绝文案，
任何一条坏了都必须在**进程边界**上红。库内单测（`test_kb_*`）证明不了这件事：
它们直接调 `fish.kb_*`，绕过了 JSON-RPC、绕过了 `_attach_token`、绕过了错误→isError 的转换。

隔离（K12）：临时根 + 临时 vault，全部 `tempfile.mkdtemp()`；**绝不**指向 `p0/data`
或真实 `E:\\Obsidian\\安逸知识库`。每条用例都新建临时树，互不共享状态。
"""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish  # noqa: E402
import mcp_server  # noqa: E402           # 只读：验常量与工具表，不实例化服务器
from test_mcp import MCPClient  # noqa: E402

INIT = {"protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "kb-test", "version": "0"}}

# 18 个工具名的**完整名单**（写死字面量，不从 mcp_server 反读）：
# 判据真值必须独立于被测实现——从 TOOLS 里算出来的集合永远等于它自己，抓不住漏加。
TOOL_NAMES = {
    "add_project", "board", "close_project", "healthcheck", "inject", "kb", "kb_write",
    "neighbors", "read_body", "recall", "register", "remember", "rename_citizen",
    "reopen_project", "resolve_code", "session_start", "supersede", "whoami",
}

SOURCE_LINE = "source=知识库（未建索引）"
CITIZEN_ID = "01CIT00000000000000000001"


class MCPKBToolsTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fish-mcp-kb-")
        self.root = os.path.join(self.tmp, "data")
        os.makedirs(self.root, exist_ok=True)
        self.vault_ro = os.path.join(self.tmp, "vault-ro")
        self.vault_rw = os.path.join(self.tmp, "vault-rw")
        self._make_vault_ro()
        self._make_vault_rw()

        conn = fish.connect(Path(self.root))
        try:
            conn.executescript(fish.SCHEMA)
            fish.migrate(conn)
            fish.kb_vault_add(conn, name="测试库RO", path=self.vault_ro,
                              mode="ro", is_default=True)
            fish.kb_vault_add(conn, name="测试库RW", path=self.vault_rw, mode="rw")
            conn.execute(
                "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,"
                "created_at) VALUES (?,?,?,?,?,?,?)",
                (CITIZEN_ID, "知识库测试公民", "role", "engineer", None, "active",
                 fish.now_utc()))
            conn.commit()
        finally:
            conn.close()

        self.c = MCPClient(self.root)
        self.c.request("initialize", INIT)

    def tearDown(self):
        self.c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ 夹具

    def _make_vault_ro(self):
        """只读库：三层结构 + 一个独特词 + 一篇长文（验截断）。

        独特词 `鲸落计划Q7` 只存在于已知库文件里——它同时是 KB-10 边界的样本
        （`recall` 对它 0 命中，`kb search` 必须命中）。
        """
        home = Path(self.vault_ro) / "00-首页"
        home.mkdir(parents=True)
        (home / "README.md").write_text(
            "# 首页\n\n独特词：鲸落计划Q7\n", encoding="utf-8")
        ppt = Path(self.vault_ro) / "20-PPT"
        ppt.mkdir(parents=True)
        (ppt / "示例.md").write_text(
            "# 示例\n\n这是一篇关于拆解报告与版式的笔记。\n", encoding="utf-8")
        deep = Path(self.vault_ro) / "notes" / "sub"
        deep.mkdir(parents=True)
        (deep / "深层.md").write_text("# 深层\n\n第三层目录里的笔记。\n", encoding="utf-8")
        (Path(self.vault_ro) / "attachments").mkdir()
        (Path(self.vault_ro) / "attachments" / "图.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        long_lines = ["# 长文", ""] + [f"第 {i} 段：知识库长文档只用于验证截断。" for i in range(1, 41)]
        (Path(self.vault_ro) / "长文.md").write_text("\n".join(long_lines) + "\n",
                                                     encoding="utf-8")
        # 忽略目录：`.trash/**` 里的东西必须既不出现在 tree 里、也打不开（§5.2）
        trash = Path(self.vault_ro) / ".trash"
        trash.mkdir()
        (trash / "已删.md").write_text("不该被看到\n", encoding="utf-8")

    def _make_vault_rw(self):
        Path(self.vault_rw).mkdir(parents=True)
        (Path(self.vault_rw) / "草稿.md").write_text("# 草稿\n\n旧正文\n", encoding="utf-8")

    # ------------------------------------------------------------ 小工具

    def _call(self, name, arguments):
        r = self.c.request("tools/call", {"name": name, "arguments": arguments})
        self.assertIn("result", r, f"{name} 没有返回 result：{r}")
        res = r["result"]
        return res["isError"], res["content"][0]["text"]

    def _rows(self, sql, params=()):
        conn = fish.connect(Path(self.root))
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _issue_token(self):
        conn = fish.connect(Path(self.root))
        try:
            res = fish.issue_session_token(
                conn, citizen_id=CITIZEN_ID, session_key="self:kb-test",
                evidence="self-reported", channel="cli")
            conn.commit()
        finally:
            conn.close()
        return res["token"]

    def _assert_three_line_header(self, text, third_pattern):
        lines = text.splitlines()
        self.assertGreaterEqual(len(lines), 3, "回执不足三行")
        self.assertEqual(lines[0], SOURCE_LINE, "第 1 行必须是来源声明")
        self.assertTrue(lines[1].startswith("as_of="), f"第 2 行必须是 as_of：{lines[1]!r}")
        self.assertRegex(lines[1][len("as_of="):],
                         r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
        self.assertRegex(lines[2], third_pattern, f"第 3 行形态不对：{lines[2]!r}")

    # ------------------------------------------------------------ 1. 工具面

    def test_01_tools_list_names_are_19(self):
        """工具面 = 18 个旧工具 + `corpus`（T03 加的第 19 个；**名单仍写死**不反读）。

        2026-10-27 三关修复顺手修掉这条**既有红**：T03 加 `corpus` 时这里没跟着改成 19，
        于是本文件在全量跑里一直红——那不是"新发现"，是上一轮漏改的账。
        """
        r = self.c.request("tools/list")
        names = {t["name"] for t in r["result"]["tools"]}
        self.assertEqual(names, TOOL_NAMES | {"corpus"})
        self.assertEqual(len(r["result"]["tools"]), 19)
        for tool in r["result"]["tools"]:
            self.assertIn("inputSchema", tool)
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_02_token_is_optional_everywhere(self):
        """token 由 `_attach_token` 统一补：每个工具都有，且**任何工具都不 required**。"""
        r = self.c.request("tools/list")
        for tool in r["result"]["tools"]:
            props = tool["inputSchema"].get("properties") or {}
            required = tool["inputSchema"].get("required") or []
            self.assertIn("token", props, f"{tool['name']} 缺 token 入参")
            self.assertNotIn("token", required, f"{tool['name']} 把 token 写进了 required")
        # 工具表里也必须是同一份纪律（有人手抄 token 定义会在 import 期就炸）
        self.assertEqual([t["name"] for t in mcp_server.TOOLS
                          if "token" in (t["inputSchema"].get("required") or [])], [])

    def test_03_constants_match(self):
        """K3：两处 'unattributed' 字面量必须一致（fish 与 mcp 各自独立定义）。"""
        self.assertEqual(fish.KB_UNATTRIBUTED, mcp_server.UNATTRIBUTED)
        self.assertEqual(fish.KB_UNATTRIBUTED, "unattributed")

    # ------------------------------------------------------------ 2. 读

    def test_04_kb_tree_three_line_header(self):
        is_error, text = self._call("kb", {"action": "tree", "vault": "测试库RO"})
        self.assertFalse(is_error, text)
        self._assert_three_line_header(text, r"^scan=\d+ files / \d+ bytes / \d+ ms$")
        # 只统计：四种文件都算进去（README/示例/深层/长文/图.png），忽略目录不出现
        self.assertIn("00-首页", text)
        self.assertIn("20-PPT", text)
        self.assertIn("chars=", text)
        self.assertIn("tokens=", text)
        # 生效的忽略规则会**明说**在回执里（`.trash/**`），但被忽略的**内容**不许出现
        self.assertIn(".trash/**", text, "回执没写明生效的忽略规则")
        self.assertNotIn("已删", text, "忽略规则没生效：.trash 里的文件出现在 tree 里")

    def test_05_kb_open_third_line_is_truncated_shown(self):
        is_error, text = self._call("kb", {"action": "open", "vault": "测试库RO",
                                          "path": "长文.md", "max_chars": 20})
        self.assertFalse(is_error, text)
        self._assert_three_line_header(
            text, r"^truncated=true / shown=\d+/\d+ bytes$")
        self.assertIn("来源=知识库", text)
        self.assertIn("已截断", text)

    def test_06_kb_open_rejects_absolute_and_traversal(self):
        for bad in ("E:\\Windows\\win.ini", "../../x.md", ".trash/已删.md"):
            with self.subTest(path=bad):
                is_error, text = self._call("kb", {
                    "action": "open", "vault": "测试库RO", "path": bad})
                self.assertTrue(is_error, f"坏路径被放行了：{bad}")
        # 服务必须还活着（工具错误不崩进程）
        r = self.c.request("ping")
        self.assertEqual(r["result"]["resultType"], "complete")

    def test_07_kb_search_labels_source_and_keeps_query_out_of_audit(self):
        is_error, text = self._call("kb", {"action": "search", "vault": "测试库RO",
                                          "query": "鲸落计划Q7", "limit": 5})
        self.assertFalse(is_error, text)
        self._assert_three_line_header(text, r"^scan=\d+ files / \d+ bytes / \d+ ms$")
        self.assertIn("鲸落计划Q7", text, "命中内容没回来")
        self.assertIn("00-首页/README.md", text, "命中路径没回来")
        rows = self._rows(
            "SELECT citizen_id,via,meta FROM kb_access WHERE action='search' "
            "ORDER BY id DESC LIMIT 1")
        self.assertTrue(rows, "search 没有落 kb_access")
        citizen_id, via, meta = rows[0]
        self.assertEqual(via, "mcp:kb")
        self.assertEqual(citizen_id, "unattributed", "无 token 时必须记成 unattributed")
        self.assertIsNotNone(meta)
        self.assertNotIn("鲸落计划Q7", meta, "query 原文进了审计（§5.6 禁止）")
        self.assertIn("query_hash", meta)

    def test_08_kb_vaults_lists_registered(self):
        is_error, text = self._call("kb", {"action": "vaults"})
        self.assertFalse(is_error, text)
        self._assert_three_line_header(text, r"^scan=\d+ files / \d+ bytes / \d+ ms$")
        self.assertIn("测试库RO", text)
        self.assertIn("测试库RW", text)
        self.assertIn("mode=ro", text)

    def test_09_bad_action_and_missing_path_are_tool_errors(self):
        for args in ({"action": "nope"},
                     {"action": "open", "vault": "测试库RO"},
                     {"action": "search", "vault": "测试库RO", "query": "  "}):
            with self.subTest(args=args):
                is_error, text = self._call("kb", args)
                self.assertTrue(is_error, f"该拒绝的调用被放行了：{args}")

    # ------------------------------------------------------------ 3. 写

    def test_10_kb_write_on_ro_vault_is_error_with_mode_ro(self):
        # R11 起写面要凭证（与 board checkin 同口径）：先拿 token，才能验到 mode=ro 那一层
        token = self._issue_token()
        is_error, text = self._call("kb_write", {
            "action": "create", "vault": "测试库RO", "path": "新笔记.md",
            "content": "不该写进去\n", "token": token})
        self.assertTrue(is_error, "mode=ro 的库竟然写成功了")
        self.assertIn("mode=ro", text)
        # 磁盘一个字节都没动
        self.assertFalse((Path(self.vault_ro) / "新笔记.md").exists())

    def test_11b_kb_write_without_token_is_rejected(self):
        """R11（安全 F3）：MCP 写面**没有有效 token 就不许写**——文案要指向 `session_start`。

        为什么与 board checkin 同口径：写操作必须能回答"是谁、哪个会话改的"；
        审计只记 `unattributed` 等于给知识库开一条无法追责的写通道。
        `token` 仍不在 schema 的 `required` 里（`test_02`）——"不 required"只表示协议允许不带，
        不表示不带就能写。
        """
        is_error, text = self._call("kb_write", {
            "action": "create", "vault": "测试库RW", "path": "匿名-不许写.md",
            "content": "x\n"})
        self.assertTrue(is_error, "无 token 的 kb_write 被放行了")
        self.assertIn("session_start", text, "拒绝文案必须告诉调用方怎么拿凭证")
        self.assertFalse((Path(self.vault_rw) / "匿名-不许写.md").exists(),
                         "被拒绝的写动了磁盘")

    def test_11_kb_write_with_token_creates_backup_and_is_attributed(self):
        """有 token → 写成功、回执三段、审计归属=公民、account_id=会话键（R11）。"""
        token = self._issue_token()
        body = "由 MCP 写入的第一行\n第二行"
        is_error, text = self._call("kb_write", {
            "action": "create", "vault": "测试库RW", "path": "MCP-新建.md",
            "content": body, "frontmatter": {"tags": "mcp"},
            "reason": "端到端判据造样本", "token": token})
        self.assertFalse(is_error, text)
        self.assertIn("① 改了", text)
        self.assertIn("② 备份", text)
        self.assertIn("③ 撤销", text)
        self.assertIn("fish kb rollback", text)
        self.assertNotIn(body, text, "回执里不该回显正文")
        written = Path(self.vault_rw) / "MCP-新建.md"
        self.assertTrue(written.exists(), "文件没写出来")
        raw = written.read_text(encoding="utf-8")
        self.assertIn("tags: mcp", raw, "frontmatter 没写进去")
        self.assertIn("第二行", raw)
        rows = self._rows(
            "SELECT citizen_id,account_id,via,backup_path,action FROM kb_access "
            "WHERE action='create' ORDER BY id DESC LIMIT 1")
        self.assertTrue(rows, "写没有落 kb_access")
        citizen_id, account_id, via, backup_path, action = rows[0]
        self.assertEqual(citizen_id, CITIZEN_ID, "有 token 的写必须归属到那个公民")
        self.assertEqual(account_id, "self:kb-test", "account_id 要记 session_key")
        self.assertEqual(via, "mcp:kb_write")
        self.assertEqual(action, "create")
        self.assertTrue(backup_path and Path(backup_path).is_dir(),
                        f"备份目录不存在：{backup_path}")
        self.assertIn("_backups", str(backup_path), "备份不在 data_root/_backups 下")

    def test_12_kb_write_update_requires_base_sha256(self):
        token = self._issue_token()
        is_error, text = self._call("kb_write", {
            "action": "update", "vault": "测试库RW", "path": "草稿.md",
            "content": "改正文\n", "token": token})
        self.assertTrue(is_error, "update 缺 base_sha256 竟然成功了")
        self.assertIn("base_sha256", text)
        self.assertIn("旧正文", (Path(self.vault_rw) / "草稿.md").read_text("utf-8"),
                      "被拒绝的 update 动了文件")

    def test_13_write_then_read_with_token_is_attributed(self):
        """带 token 读 → kb_access.citizen_id 就是那个公民（不是 unattributed）。"""
        token = self._issue_token()
        is_error, text = self._call("kb", {"action": "tree", "vault": "测试库RO",
                                          "token": token})
        self.assertFalse(is_error, text)
        rows = self._rows(
            "SELECT citizen_id FROM kb_access WHERE action='tree' "
            "ORDER BY id DESC LIMIT 1")
        self.assertEqual(rows[0][0], CITIZEN_ID)

    def test_14_allow_sensitive_requires_token(self):
        """S8（第四轮 · 安全 N6）：`allow_sensitive=true` 没有有效 token → isError。

        与 R11 的 `kb_write` 同口径：二次授权必须能追到人。无 token 时自授权 =
        审计只记 `unattributed`，等于把"读凭证正文"变成一条无法追责的通道。
        """
        probe = "SECRET-PROBE-S8-4c1d"
        sensitive_dir = Path(self.vault_ro) / "80-资料库"
        sensitive_dir.mkdir()
        (sensitive_dir / "凭证管理.md").write_text(
            f"# 凭证\n\n{probe}\n", encoding="utf-8")
        rel = "80-资料库/凭证管理.md"

        is_error, text = self._call("kb", {"action": "open", "vault": "测试库RO",
                                           "path": rel, "allow_sensitive": True})
        self.assertTrue(is_error, "无 token 的 allow_sensitive 被放行了")
        self.assertIn("session_start", text, "拒绝文案必须指向怎么拿凭证")
        self.assertNotIn(probe, text, "拒绝时不许回正文")

        # 不带 allow_sensitive 的普通路径仍是 `sensitive`（两种拒绝都可判，别混成一种）
        is_error2, text2 = self._call("kb", {"action": "open", "vault": "测试库RO",
                                             "path": rel})
        self.assertTrue(is_error2)
        self.assertIn("敏感", text2)

        token = self._issue_token()
        is_error3, text3 = self._call("kb", {"action": "open", "vault": "测试库RO",
                                             "path": rel, "allow_sensitive": True,
                                             "token": token})
        self.assertFalse(is_error3, text3)
        self.assertIn(probe, text3, "有 token 的二次授权必须真的能读到")
        rows = self._rows(
            "SELECT citizen_id FROM kb_access WHERE action='open' "
            "ORDER BY id DESC LIMIT 1")
        self.assertEqual(rows[0][0], CITIZEN_ID, "授权读必须归属到那个公民")


    def test_15_allow_sensitive_needs_explicit_boolean(self):
        """N7（第五轮 · 安全 🟡）：MCP 面同样**只认明确布尔**——`"false"` 不是同意。

        为什么必须在进程边界上验：宿主把布尔渲染成字符串（YAML/模板/环境变量）是常态，
        而 `bool("false") is True` 只在 MCP 这一层被调用（`fish._strict_bool` 之前是
        `bool(a.get(...))`）。库内单测证明不了宿主真的传字符串时会怎样。

        N9（安全 ⚪）：授权成功的审计行 `meta.allow_sensitive` 为 `true`（与读面一致）。
        """
        probe = "SECRET-PROBE-N7-9b2e"
        sensitive_dir = Path(self.vault_ro) / "80-资料库"
        sensitive_dir.mkdir()
        (sensitive_dir / "凭证管理.md").write_text(
            f"# 凭证\n\n{probe}\n", encoding="utf-8")
        rel = "80-资料库/凭证管理.md"
        token = self._issue_token()

        for raw in ("false", "FALSE", "0"):
            with self.subTest(allow_sensitive=raw):
                is_error, text = self._call("kb", {
                    "action": "open", "vault": "测试库RO", "path": rel,
                    "allow_sensitive": raw, "token": token})
                self.assertTrue(is_error, f'allow_sensitive="{raw}" 竟然放行了敏感正文')
                self.assertNotIn(probe, text, "拒绝时不许回正文")

        for raw in ("true", "1"):
            with self.subTest(allow_sensitive=raw):
                is_error, text = self._call("kb", {
                    "action": "open", "vault": "测试库RO", "path": rel,
                    "allow_sensitive": raw, "token": token})
                self.assertFalse(is_error, text)
                self.assertIn(probe, text)
                rows = self._rows(
                    "SELECT meta FROM kb_access WHERE action='open' "
                    "ORDER BY id DESC LIMIT 1")
                self.assertIs(json.loads(rows[0][0]).get("allow_sensitive"), True,
                              "授权读的审计没记 allow_sensitive=true")

        # 未授权读普通文件 → 审计不长这个字段（N9 的"只在为真时记"）
        is_error, text = self._call("kb", {"action": "open", "vault": "测试库RO",
                                           "path": "长文.md", "max_chars": 30,
                                           "token": token})
        self.assertFalse(is_error, text)
        rows = self._rows("SELECT meta FROM kb_access WHERE action='open' "
                          "ORDER BY id DESC LIMIT 1")
        self.assertNotIn("allow_sensitive", json.loads(rows[0][0]),
                         "未授权的读不该长出 allow_sensitive 字段")

        for raw in ("yes", "on"):
            with self.subTest(allow_sensitive=raw):
                is_error, text = self._call("kb", {
                    "action": "open", "vault": "测试库RO", "path": rel,
                    "allow_sensitive": raw, "token": token})
                self.assertTrue(is_error, f'allow_sensitive="{raw}" 被当成同意了')
                self.assertIn("allow_sensitive", text, "错误文案要点名是哪个入参")
                self.assertIn("true", text, "错误文案要给出正确写法")
                self.assertNotIn(probe, text, "报错时不许回正文")

        # 写面同一解析：`"false"` 拒、`"yes"` → bad_usage、`"true"` 放行
        (Path(self.vault_rw) / "80-资料库").mkdir()
        write = {"action": "create", "vault": "测试库RW",
                 "path": "80-资料库/凭证-探针.md", "content": "x\n", "token": token}
        is_error, text = self._call("kb_write", dict(write, allow_sensitive="false"))
        self.assertTrue(is_error, "写面把字符串 \"false\" 读成了同意")
        self.assertIn("敏感", text)
        self.assertFalse((Path(self.vault_rw) / "80-资料库" / "凭证-探针.md").exists(),
                         "被拒绝的写动了磁盘")
        is_error, text = self._call("kb_write", dict(write, allow_sensitive="yes"))
        self.assertTrue(is_error, "写面的模糊写法必须 bad_usage")
        self.assertIn("allow_sensitive", text)
        self.assertFalse((Path(self.vault_rw) / "80-资料库" / "凭证-探针.md").exists())
        is_error, text = self._call("kb_write", dict(write, allow_sensitive="true"))
        self.assertFalse(is_error, text)
        self.assertTrue((Path(self.vault_rw) / "80-资料库" / "凭证-探针.md").exists())
        rows = self._rows("SELECT meta FROM kb_access WHERE action='create' "
                          "ORDER BY id DESC LIMIT 1")
        self.assertIs(json.loads(rows[0][0]).get("allow_sensitive"), True,
                      "授权写的审计没记 allow_sensitive=true（N9）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
