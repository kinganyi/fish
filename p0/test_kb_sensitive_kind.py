#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F-P1-4（2026-11-25 外部审查）：知识库敏感面的"二次授权"不能由调用方自授权。

## 审查方主张（已复核成立）

`mcp_server.py` 的 `kb`（`action=open`）与 `kb_write` 对 `allow_sensitive` 只判
`ident.status == IDENTITY_VALID`。于是**任何有效 token（含 visitor）**都能读/写
敏感名单命中的文件（凭证/密钥/.env）。

而 visitor 是**自助登记**的身份：MCP 的 `session_start` 对任何调用方无门槛签发。
两条合起来 = "调用方自己给自己发钥匙"。判定本体与 `fish_trust.enforce_mem_type_auth`
同源（`fish_trust.py:161-184` 记着同款链路：有凭证 != 有授权）。

## 修法

`allow_sensitive=true` 时额外要求公民 kind ∈ (`resident`, `role`)——判定只有一处
（`FishServer._kb_require_sensitive_grant`，内部调 `fish_trust.citizen_kind_of`）。
读面与写面都调它；**CLI 不受影响**（`fish kb open --allow-sensitive` 走 argparse
直连 `fish.kb_open`，是人在本机终端里的显式动作）。

## 这批判据为什么能失败

  ① visitor + `allow_sensitive` → 必须报错（删掉 kind 判定就红）；
  ② resident / role + `allow_sensitive` → 必须放行（"全部拒绝"也能让①变绿）；
  ③ CLI `--allow-sensitive` → 必须照旧能读（把门修到 CLI 头上就是矫枉过正）；
  ④ 两个工具面必须调**同一个**判定方法（AST 级），防下一个入口各写一份。

## 隔离

全部 `tempfile.mkdtemp()`：临时 data 根 + 临时 vault，**绝不**指向 `p0/data`
或真实 `E:\\Obsidian\\安逸知识库`。
"""
import ast
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish  # noqa: E402
import mcp_server as M  # noqa: E402

PROBE = "SECRET-PROBE-FP14-3e7a"
SENSITIVE_REL = "80-资料库/凭证管理.md"
PLAIN_REL = "普通笔记.md"

VISITOR = "01VIS0000000000000000P14"
RESIDENT = "01RES0000000000000000P14"
ROLE = "01ROL0000000000000000P14"


class KBSensitiveGrantTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fish-p14-kb-")
        self.root = Path(self.tmp) / "data"
        self.root.mkdir(parents=True)
        self.vault_ro = Path(self.tmp) / "vault-ro"
        self.vault_rw = Path(self.tmp) / "vault-rw"
        self._make_vaults()
        conn = fish.connect(self.root)
        try:
            conn.executescript(fish.SCHEMA)
            fish.migrate(conn)
            fish.kb_vault_add(conn, name="测试库RO", path=str(self.vault_ro),
                              mode="ro", is_default=True)
            fish.kb_vault_add(conn, name="测试库RW", path=str(self.vault_rw), mode="rw")
            for cid, kind in ((VISITOR, "visitor"), (RESIDENT, "resident"),
                              (ROLE, "role")):
                conn.execute(
                    "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,"
                    "created_at) VALUES (?,?,?,?,?,?,?)",
                    (cid, f"公民{dict(visitor='访', resident='居', role='角')[kind]}",
                     kind, "engineer", None, "active", fish.now_utc()))
            conn.commit()
        finally:
            conn.close()
        self.srv = M.FishServer(self.root)

    def tearDown(self):
        try:
            self.srv.conn.close()
        except Exception:
            pass
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ 夹具

    def _make_vaults(self):
        sensitive = self.vault_ro / "80-资料库"
        sensitive.mkdir(parents=True)
        (sensitive / "凭证管理.md").write_text(f"# 凭证\n\n{PROBE}\n", encoding="utf-8")
        (self.vault_ro / PLAIN_REL).write_text("# 普通\n\n没有敏感内容的笔记。\n",
                                               encoding="utf-8")
        self.vault_rw.mkdir(parents=True)
        # 写面只写**已存在**目录（`kb_resolve_target` 不自动建目录），所以父目录先建好
        (self.vault_rw / "80-资料库").mkdir()
        (self.vault_rw / "草稿.md").write_text("# 草稿\n\n旧正文\n", encoding="utf-8")

    def _identity(self, citizen_id):
        """用**真实 token 路径**解析身份（不是手搓 Identity）。

        为什么要走 `_resolve_identity`：审查方的链路是"任何有效 token"，所以判据必须
        证明"token 真的有效"，而不是"我构造了一个 valid 的 Identity 对象"。
        """
        conn = fish.connect(self.root)
        try:
            res = fish.issue_session_token(
                conn, citizen_id=citizen_id, session_key=f"self:{citizen_id[-8:]}",
                evidence="self-reported", channel="cli")
            conn.commit()
            token = res["token"]
        finally:
            conn.close()
        ident = self.srv._resolve_identity(token)
        self.assertEqual(ident.status, M.IDENTITY_VALID,
                         f"夹具失败：token 没被判成有效（{ident.status}）")
        self.assertEqual(ident.citizen_id, citizen_id)
        return token, ident

    def _read(self, ident, rel=SENSITIVE_REL, allow=True):
        return self.srv.tool_kb({"action": "open", "vault": "测试库RO", "path": rel,
                                 "allow_sensitive": allow}, ident)

    # ------------------------------------------------------------ 核心判据：读

    def test_visitor_cannot_read_sensitive(self):
        """**核心判据**：visitor + `allow_sensitive=true` 必须被拒，且不回正文。

        删掉 kind 判定（只留 `IDENTITY_VALID`）→ 这条红。审查方给出的正是这个复现：
        有效 token + kind=visitor + allow_sensitive=true → 正文照读。
        """
        _token, ident = self._identity(VISITOR)
        with self.assertRaises(ValueError) as ctx:
            self._read(ident)
        msg = str(ctx.exception)
        self.assertIn("visitor", msg, f"报错没说清当前身份是什么：{msg[:200]}")
        self.assertIn("resident", msg, "报错没给出可执行的升级路径")
        self.assertIn("citizen-set-kind", msg, "报错必须点名那条本机命令")
        self.assertIn("CLI", msg, "报错要说清 CLI 是另一条合法路径")
        self.assertNotIn(PROBE, msg, "拒绝时报错里泄露了敏感正文")

    def test_resident_can_read_sensitive(self):
        """对照组：resident 必须**放行**——"全部拒绝"也能让上面那条变绿。"""
        _token, ident = self._identity(RESIDENT)
        text = self._read(ident)
        self.assertIn(PROBE, text, "resident 被挡在敏感面外（过滤过头）")

    def test_role_can_read_sensitive(self):
        """role（项目级角色）与 resident 同档。"""
        _token, ident = self._identity(ROLE)
        self.assertIn(PROBE, self._read(ident), "role 被挡在敏感面外（过滤过头）")

    def test_role_reads_plain_note_without_sensitive_flag(self):
        """反向判据：普通笔记**不需要**这道门（否则"敏感面"变成"知识库整体"）。"""
        _token, ident = self._identity(VISITOR)
        text = self._read(ident, rel=PLAIN_REL, allow=False)
        self.assertIn("没有敏感内容的笔记", text, "普通笔记被误挡")

    def test_anonymous_still_gets_the_credential_message(self):
        """合并前的旧判据不许被新门顶掉：无 token 时仍要指向怎么拿凭证。"""
        with self.assertRaises(ValueError) as ctx:
            self._read(M.IDENTITY_ANONYMOUS)
        self.assertIn("session_start", str(ctx.exception),
                      "无 token 的拒绝文案丢了（旧判据被顶掉）")

    # ------------------------------------------------------------ 核心判据：写

    def test_visitor_cannot_write_sensitive(self):
        """写面同一道门：visitor 不许改凭证文件，而且磁盘上不能留下半成品。"""
        _token, ident = self._identity(VISITOR)
        rel = "80-资料库/凭证-探针.md"
        with self.assertRaises(ValueError) as ctx:
            self.srv.tool_kb_write({"action": "create", "vault": "测试库RW",
                                    "path": rel, "content": "被写进去了\n",
                                    "allow_sensitive": True}, ident)
        self.assertIn("resident", str(ctx.exception))
        self.assertFalse((self.vault_rw / "80-资料库" / "凭证-探针.md").exists(),
                         "被拒绝的写动了磁盘")

    def test_resident_can_write_sensitive(self):
        """写面对照组：resident 放行（否则"全部拒绝"也能让上面变绿）。"""
        _token, ident = self._identity(RESIDENT)
        rel = "80-资料库/凭证-探针.md"
        out = self.srv.tool_kb_write({"action": "create", "vault": "测试库RW",
                                      "path": rel, "content": "resident 合法写入\n",
                                      "allow_sensitive": True}, ident)
        self.assertIn("改了", out)
        self.assertTrue((self.vault_rw / "80-资料库" / "凭证-探针.md").exists(),
                        "resident 写了但文件不在")

    # ------------------------------------------------------------ 协议边界

    def test_error_becomes_is_error_at_the_protocol_boundary(self):
        """工具错误必须在 JSON-RPC 层变成 `isError: true`，不是崩服务或静默成功。"""
        token, _ident = self._identity(VISITOR)
        resp = self.srv.handle({
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "kb", "arguments": {
                "action": "open", "vault": "测试库RO", "path": SENSITIVE_REL,
                "allow_sensitive": True, "token": token}}})
        result = (resp or {}).get("result") or {}
        self.assertIs(result.get("isError"), True, f"拒绝没变成 isError：{resp}")
        text = result["content"][0]["text"]
        self.assertNotIn(PROBE, text, "协议回包里泄露了敏感正文")

    # ------------------------------------------------------------ CLI 不受影响

    def test_cli_allow_sensitive_is_unaffected(self):
        """CLI `--allow-sensitive` 必须照旧能读：它是人在本机终端里的显式动作。

        这条判据防的是"把门修到 CLI 头上"——那会把主人自己的逃生口一起关掉，
        而审查方指出的漏洞只在 MCP 面（模型可以自己调用的通道）。
        """
        r = subprocess.run(
            [sys.executable, "fish.py", "--root", str(self.root),
             "kb", "open", SENSITIVE_REL, "--vault", "测试库RO", "--allow-sensitive"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(HERE), timeout=180)
        out = (r.stdout or "") + (r.stderr or "")
        self.assertEqual(r.returncode, 0, f"CLI 读敏感文件失败：{out[:400]}")
        self.assertIn(PROBE, out, "CLI 的 --allow-sensitive 不再返回正文")

    # ------------------------------------------------------------ 判据的判据

    def test_both_surfaces_call_the_same_gate(self):
        """**判据的判据**：读面与写面调同一个判定方法，且都不许内联第二份 kind 判定。

        为什么要有这条：本项目最贵的 bug 形态是"同一件事两套实现"
        （`enforce_mem_type_auth` 的注释里记着同款）。只判行为的话，
        下一个人完全可以给 `kb_write` 再写一份 `if kind not in (...)` —— 行为判据
        全绿，而两份实现从那天起开始漂。
        """
        tree = ast.parse((HERE / "mcp_server.py").read_text(encoding="utf-8"))

        def names_in(fname):
            out = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == fname:
                    for n in ast.walk(node):
                        if isinstance(n, ast.Name):
                            out.add(n.id)
                        elif isinstance(n, ast.Attribute):
                            out.add(n.attr)
            return out

        for surface in ("tool_kb", "tool_kb_write"):
            names = names_in(surface)
            self.assertIn("_kb_require_sensitive_grant", names,
                          f"{surface} 没有调统一授权判定")
            self.assertNotIn("citizen_kind_of", names,
                             f"{surface} 内联了第二份 kind 判定（必然漂）")

        gate = names_in("_kb_require_sensitive_grant")
        self.assertIn("citizen_kind_of", gate,
                      "授权判定没有真的去查 kind（那它判的是什么？）")
        self.assertIn("AUTHORIZED_CITIZEN_KINDS", gate,
                      "白名单没走 fish_trust 的唯一常量")


if __name__ == "__main__":
    unittest.main(verbosity=2)
