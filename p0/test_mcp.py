#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP 协议验证：真的起一个子进程，走 stdio JSON-RPC，逐条核对。
不是单元测试替身——这里验证的是「宿主能不能跟它说话」。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


class MCPClient:
    def __init__(self, root, env=None):
        e = {**os.environ, **(env or {})}
        self.p = subprocess.Popen(
            [sys.executable, str(HERE / "mcp_server.py"), "--root", root],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1, env=e)
        self._id = 0

    def request(self, method, params=None):
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self.p.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.p.stdin.flush()
        line = self.p.stdout.readline()
        if not line:
            raise RuntimeError(f"server closed; stderr={self.p.stderr.read()[:500]}")
        return json.loads(line)

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.p.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.p.stdin.flush()

    def close(self):
        for s in (self.p.stdin, self.p.stdout, self.p.stderr):
            try:
                if s:
                    s.close()
            except Exception:
                pass
        try:
            self.p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.p.kill()


class MCPProtocolTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fish-mcp-")
        self.root = os.path.join(self.tmp, "data")
        os.makedirs(self.root, exist_ok=True)
        # 预置：一个项目 + 一个公民
        sys.path.insert(0, str(HERE))
        import fish
        conn = fish.connect(Path(self.root))
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        conn.execute("INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
                     "VALUES (?,?,?,?,?)", ("TEST", "t", "", 0, "active"))
        conn.execute("INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at) "
                     "VALUES (?,?,?,?,?,?,?)",
                     ("01CIT00000000000000000001", "测试公民", "resident", "owner", None,
                      "active", fish.now_utc()))
        conn.execute("INSERT OR REPLACE INTO config (key,value,type,updated_at) "
                     "VALUES ('default_citizen','01CIT00000000000000000001','string',?)",
                     (fish.now_utc(),))
        conn.commit()
        conn.close()
        self.c = MCPClient(self.root)

    def tearDown(self):
        self.c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_01_initialize(self):
        r = self.c.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        })
        self.assertEqual(r["jsonrpc"], "2.0")
        res = r["result"]
        self.assertEqual(res["protocolVersion"], "2024-11-05")
        self.assertIn("tools", res["capabilities"])
        self.assertEqual(res["serverInfo"]["name"], "fish")
        # 通知不应有响应（这里只验证不报错）
        self.c.notify("notifications/initialized")

    def test_02_tools_list(self):
        self.c.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                      "clientInfo": {"name": "t", "version": "0"}})
        r = self.c.request("tools/list")
        names = {t["name"] for t in r["result"]["tools"]}
        # 2026-10-25：阶段 B 加 rename_citizen（名字认领）、阶段 D 加 close/reopen（项目三态）、
        # 晚加 session_start（用户批准：MCP 面缺签发口，AI 只能跑 CLI 而 CLI 会建出第二个公民）。
        self.assertEqual(names, {"remember", "recall", "neighbors", "resolve_code",
                                 "read_body", "whoami", "register", "rename_citizen",
                                 "close_project", "reopen_project",
                                 "inject", "healthcheck", "add_project", "supersede",
                                 "session_start", "board"})
        # 每个工具必须有 inputSchema
        for t in r["result"]["tools"]:
            self.assertIn("inputSchema", t)
            self.assertEqual(t["inputSchema"]["type"], "object")

    def test_03_remember_then_recall(self):
        self.c.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                      "clientInfo": {"name": "t", "version": "0"}})
        r1 = self.c.request("tools/call", {
            "name": "remember",
            "arguments": {"project": "TEST", "title": "MCP 端到端验证",
                          "body": "由 stdio 客户端写入", "mem_type": "lesson",
                          "tags": ["mcp", "e2e"]},
        })
        self.assertFalse(r1["result"]["isError"])
        text = r1["result"]["content"][0]["text"]
        code = text.splitlines()[0]
        self.assertRegex(code, r"^TEST-\d{6}-\d{2}$")

        r2 = self.c.request("tools/call", {
            "name": "recall", "arguments": {"query": code}})
        self.assertFalse(r2["result"]["isError"])
        self.assertIn(code, r2["result"]["content"][0]["text"])

        r3 = self.c.request("tools/call", {
            "name": "resolve_code", "arguments": {"code": code}})
        self.assertIn("MCP 端到端验证", r3["result"]["content"][0]["text"])

    def test_04_resource_injection(self):
        self.c.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                      "clientInfo": {"name": "t", "version": "0"}})
        r = self.c.request("resources/read", {"uri": "fish://me"})
        text = r["result"]["contents"][0]["text"]
        self.assertIn("测试公民", text)
        self.assertIn("三条铁律", text)

    def test_05_unknown_method(self):
        r = self.c.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                          "clientInfo": {"name": "t", "version": "0"}})
        r = self.c.request("no/such/method")
        self.assertIn("error", r)
        self.assertEqual(r["error"]["code"], -32601)

    def test_06_tool_error_is_returned_not_crash(self):
        """工具内部异常必须变成 isError 结果，不能把服务打挂。"""
        self.c.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                      "clientInfo": {"name": "t", "version": "0"}})
        r = self.c.request("tools/call", {
            "name": "remember", "arguments": {"project": "NOPE", "title": "x"}})
        self.assertTrue(r["result"]["isError"])
        self.assertIn("未知项目前缀", r["result"]["content"][0]["text"])
        # 服务仍活着（ping 在 2026-07-28 已移除，但 dual-era 对 legacy 仍应答）
        r2 = self.c.request("ping")
        self.assertEqual(r2["result"]["resultType"], "complete")


    # ================= 以下为 2026-07-28 规范对齐测试 =================

    def _modern(self, method, params=None, version="2026-07-28"):
        p = dict(params or {})
        p["_meta"] = {
            "io.modelcontextprotocol/protocolVersion": version,
            "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1.0"},
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        return self.c.request(method, p)

    def test_07_server_discover(self):
        """规范硬要求：server/discover 必须实现，返回 supportedVersions。"""
        r = self._modern("server/discover")
        res = r["result"]
        self.assertEqual(res["resultType"], "complete")
        self.assertIn("2026-07-28", res["supportedVersions"])
        self.assertIn("tools", res["capabilities"])
        self.assertEqual(res["_meta"]["io.modelcontextprotocol/serverInfo"]["name"], "fish")
        self.assertIn("ttlMs", res, "discover 支持缓存，必须带 ttlMs")
        self.assertIn("cacheScope", res)

    def test_08_modern_request_carries_server_info(self):
        """modern 请求的 result 必须带 _meta.serverInfo。"""
        r = self._modern("tools/list")
        res = r["result"]
        self.assertEqual(res["resultType"], "complete")
        self.assertEqual(res["_meta"]["io.modelcontextprotocol/serverInfo"]["name"], "fish")
        self.assertIn("ttlMs", res)
        self.assertIn("cacheScope", res)
        # legacy 请求（无 _meta）不应带 modern 的 _meta
        r2 = self.c.request("tools/list")
        self.assertNotIn("_meta", r2["result"])

    def test_09_unsupported_version_returns_32022(self):
        """版本不支持必须返回 -32022，并列出 supported。"""
        r = self._modern("tools/list", version="1900-01-01")
        self.assertIn("error", r)
        self.assertEqual(r["error"]["code"], -32022)
        self.assertIn("2026-07-28", r["error"]["data"]["supported"])
        self.assertEqual(r["error"]["data"]["requested"], "1900-01-01")

    def test_10_all_results_have_result_type(self):
        """所有 result 必须带 resultType（客户端把缺失当 complete，但我们要显式）。"""
        for method, params in (("tools/list", None), ("resources/list", None)):
            r = self._modern(method, params)
            self.assertEqual(r["result"]["resultType"], "complete", f"{method} 缺 resultType")

    def test_11_resource_not_found_is_32602(self):
        """2026-07-28：资源不存在用 -32602（原 -32002）。"""
        r = self._modern("resources/read", {"uri": "fish://nope"})
        self.assertIn("error", r)
        self.assertEqual(r["error"]["code"], -32602)

    def test_12_legacy_still_works(self):
        """dual-era：老客户端走 initialize 仍然可用。"""
        r = self.c.request("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "legacy", "version": "0"}})
        self.assertEqual(r["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(r["result"]["serverInfo"]["name"], "fish")
        r2 = self.c.request("tools/list")
        self.assertIn("tools", r2["result"])


    # ================= 身份分离（2026-10-22 修复 / 2026-10-24 P1-c 改取证方式） =================
    #
    # 判据没变：whoami 必须区分行动者与主人，AI 不能自称主人。
    # 取证方式变了（P1-c）：MCP 里**不再有进程内的"当前身份"**——一个 MCP 进程被宿主的
    # 所有会话共享，`FISH_ACTOR` 对每个会话都是同一个值，"谁是行动者"只能由本次调用带的
    # token 决定（事故与理由见 mcp_server 模块头）。所以这里先用 CLI 侧的签发路径
    # （`fish session start`，它能读真 env）拿一条真凭证，再带 token 调工具。

    def _issue_token(self, alias, environ=None):
        """在测试进程里跑一次 session start，返回 (token, citizen_id)。

        走的是生产签发路径（fish.start_session），不是在库里手插一行：
        手插的行能证明"表里有条 token"，但证明不了"签发的 token 能用"。
        """
        import fish
        conn = fish.connect(fish.Path(self.root))
        try:
            res = fish.start_session(conn, alias=alias, environ=environ or {})
        finally:
            conn.close()
        return res["token"], res["citizen_id"]

    def test_13_identity_separation(self):
        """whoami 必须区分行动者与主人；不能让 AI 自称主人。

        两段取证：
          ① 进程 env 里明明写着 FISH_ACTOR=wb-session（旧实现会据此回答"我是 wb-session"），
             但不带 token → 只能说"身份未核"：env 是**进程级**事实，不是会话身份；
          ② 带上 session start 签发的 token → 才返回行动者 wb-session + 主人 测试公民。
        """
        token, cid = self._issue_token("wb-session", {"FISH_ACTOR": "wb-session"})
        c = MCPClient(self.root, env={"FISH_ACTOR": "wb-session"})
        try:
            c.request("initialize", {"protocolVersion": "2026-07-28", "capabilities": {},
                                     "clientInfo": {"name": "t", "version": "0"}})
            # ① 不带 token：不能凭 env 回答"我是谁"
            r = c.request("tools/call", {"name": "whoami", "arguments": {}})
            bare = r["result"]["content"][0]["text"]
            self.assertIn("身份未核", bare, "无凭证时必须说'身份未核'")
            self.assertNotIn("登记身份：wb-session", bare,
                             "进程 env 里的 FISH_ACTOR 被当成了会话身份（正是串号根因）")
            self.assertIn("主人：测试公民", bare, "主人与行动者必须分开报")

            # ② 带 token：返回的是**凭证解出的**行动者
            r2 = c.request("tools/call", {"name": "whoami",
                                          "arguments": {"token": token}})
            text = r2["result"]["content"][0]["text"]
            self.assertIn("登记身份：wb-session", text, "应返回行动者")
            self.assertIn(f"公民 {cid}", text, "返回的公民必须是 token 解出的那个")
            self.assertIn("主人：测试公民", text, "应返回主人")
            self.assertNotIn("登记身份：测试公民", text, "绝不能把主人当成'你'")
            self.assertIn("与你的宿主身份不冲突", text, "必须说明这是登记身份")
        finally:
            c.close()

    def test_14_register(self):
        """register 建公民、幂等复用；身份只从 token 来（不再写进进程内存）。"""
        import fish
        c = MCPClient(self.root, env={"FISH_ACTOR": "new-agent"})
        try:
            c.request("initialize", {"protocolVersion": "2026-07-28", "capabilities": {},
                                     "clientInfo": {"name": "t", "version": "0"}})
            r0 = c.request("tools/call", {"name": "whoami", "arguments": {}})
            self.assertIn("身份未核", r0["result"]["content"][0]["text"])

            r1 = c.request("tools/call", {"name": "register",
                                          "arguments": {"display_name": "new-agent",
                                                        "kind": "visitor",
                                                        "host": "workbuddy"}})
            text1 = r1["result"]["content"][0]["text"]
            self.assertIn("new-agent", text1)
            # 2026-10-25 改：下一步从"跑 CLI"变成"调本进程内的 session_start"。
            # 判据钉意图（必须告诉调用方怎么拿凭证），不钉具体路径字符串。
            self.assertIn("session_start", text1,
                          "register 必须告诉调用方：身份靠 session_start 的凭证")

            r2 = c.request("tools/call", {"name": "register",
                                          "arguments": {"display_name": "new-agent"}})
            self.assertIn("已存在", r2["result"]["content"][0]["text"], "重复登记应幂等")

            # 关键（P1-c）：register 之后**进程里仍然没有身份** —— whoami 还是"身份未核"。
            # 旧实现在这里会回答"我是 new-agent"，那正是 2026-10-23 串号事故的形状。
            r3 = c.request("tools/call", {"name": "whoami", "arguments": {}})
            text3 = r3["result"]["content"][0]["text"]
            self.assertIn("身份未核", text3, "register 不该把身份写进进程内存")
            self.assertNotIn("登记身份：new-agent", text3,
                             "register 之后 whoami 直接报身份 = 进程态身份没删干净")

            # 给这个公民签一条真凭证 → whoami 才认识他（身份只从 token 来）
            conn = fish.connect(fish.Path(self.root))
            try:
                cid = conn.execute("SELECT citizen_id FROM citizens WHERE display_name=?",
                                   ("new-agent",)).fetchone()[0]
                token = fish.issue_session_token(
                    conn, citizen_id=cid, session_key="self:new-agent",
                    evidence="self-reported", channel="cli")["token"]
                conn.commit()
            finally:
                conn.close()
            r4 = c.request("tools/call", {"name": "whoami",
                                          "arguments": {"token": token}})
            text4 = r4["result"]["content"][0]["text"]
            self.assertIn("登记身份：new-agent", text4)
            self.assertIn(f"公民 {cid}", text4, "whoami 必须报 token 解出的公民")
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
