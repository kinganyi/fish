#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
身份层 P1-c · MCP 用会话凭证识别身份（判据 A–I）
=================================================
守的是什么（真实事故，2026-10-23 23:01:55）：
  审计里出现 `actor=fish-main, session_actor=fish-主AI`，而那个会话名是**另一个会话**
  在 17:36:06 调 register 时写进 MCP 进程的。根因是结构错的：**DSH 一个宿主只起一个
  MCP server 进程（pid 37192），被所有会话共享**——任何"进程内存里的当前身份"都只能
  等于"最后一个 register 的人"。后果是**所有记忆的归属都可能串号**。

本轮的做法（`18` §7 / `16` §2.2）：
  · 删掉进程态身份（`self._actor` / `self._session_actor` / `_hint_shown`）；
  · 每个工具多一个**可选** `token`（旧调用不带照旧能跑）；
  · token → `session_tokens` 查表（唯一判定入口 `fish.validate_session_token`）；
  · 没凭证 → `owner='unattributed'` + 固定提示，**绝不冒充别人**（评审 R-2）。

判据与变异（每条都有变异自证：改真源码 → 必红 → 字节还原，见 mutation_identity_p1c.py）：
  A  有 token → 正确归属（owner / access_log.citizen_id 都来自 token）
  B  无 token → unattributed + 明确提示（不是 None、不是空串、不是 unknown）
  C  假 token → 拒绝（归 unattributed + 提示"凭证无效"）
  D  吊销 / 过期 token → 拒绝（判定必须看 revoked_at / expires_at）
  E  **两会话不串号**（同一个 server 对象先后服务两个会话，归属各归各的）
  F  whoami 无 token 不返回别人身份（连 env 里的 FISH_ACTOR 也不认）
  G  工具签名只多了可选 token（工具数量/名字不变，其余字段一字不动）
  H  **进程里已无身份状态**（静态 AST 扫描 + 运行时 __dict__ 双重自证）
  I  token 明文不落盘、不进审计、不回显（铁律⑮）

与任务书的已知差异（**须由主理人确认**）：
  任务书写"工具表仍是 13 个"，但仓库现状是 **11 个**，且 P1-b 判据 H（
  test_identity_p1b.py::MCP_TOOL_NAMES）与 test_mcp / test_push_p0 都把工具表
  冻结在 11；`18` §4 又明令 MCP 面不得有 session 签发入口（所以 13 里的 session
  不该存在）。本轮口径：**不加任何新工具，保持 11 个**，判据 G 按 11 冻结。
"""
import ast
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fish  # noqa: E402
import mcp_server  # noqa: E402

MCP_SOURCE = HERE / "mcp_server.py"
PREFIX = "P1C"

# 判据 G 的基线：11 个工具的入参字段，**独立抄一份**。
# 为什么独立抄：从 mcp_server.TOOLS 反推基线等于"自己证自己"——实现漏了个字段，
# 基线也跟着漏，判据永远绿。
# 2026-10-24 R1（EVAL-261024-02）：inject 新增可选 `project`（显式声明当前项目）。
# 这是**声明过的**基线变更，不是漏抄——只加这一项，其余字段一字不动。
BASELINE_TOOL_PROPS = {
    "remember": {"project", "title", "body", "mem_type", "tags", "cause",
                 "occurred_at", "supersedes", "attachments"},
    "recall": {"query", "code", "project", "tags", "scope", "limit",
               "include_superseded"},
    "neighbors": {"code", "n", "strategy"},
    "resolve_code": {"code"},
    "read_body": {"code"},
    "add_project": {"prefix", "namespace"},
    "supersede": {"code", "by", "reason"},
    # 2026-10-26（反馈批 C · FB-03）：whoami 新增可选 `all`（true = 附带相关公民
    # 清单：同会话键 / 同名 / 同账号）。这是**声明过的契约变更**——外部评估
    # `EVAL-261026-06` 要求"双视图"能够被看见（register 说 A、token 说 B 时不报错）。
    # 只加一个可选字段：不传 = 旧回执一个字都不多（判据 C1/C4 分别钉住两档）。
    "whoami": {"all"},
    "register": {"display_name", "kind", "role", "host", "session_name"},
    # 2026-10-25（阶段 B）：新增 rename_citizen（名字认领）。同样是**声明过的**基线
    # 变更——用户裁定"开场指定名字却无法绑定"是 BUG，必须给它一条工具路径。
    "rename_citizen": {"name", "reason"},
    # 2026-10-25（阶段 D）：项目三态落地，新增 close/reopen 两个工具。
    # 同样是声明过的基线变更——决策 16 要求"AI 显式调用 close_project + 写 artifact 依据"。
    "close_project": {"prefix", "reason"},
    "reopen_project": {"prefix", "reason"},
    # 2026-10-26（反馈批 B · FB-02）：inject 新增可选 `sections` + `include_board`
    # （段选择 / "别看板"的便捷写法）。这是**声明过的**基线变更——B 批落地时漏登记，
    # 导致本条判据自那以后一直是红的（C 批收尾时同步基线，不是放宽判据）。
    "inject": {"budget", "project", "sections", "include_board"},
    "healthcheck": set(),
    # 2026-10-25 加（用户批准的工具数变更）：MCP 面唯一的签发口。
    # 入参刻意**不含 token**（它本身就是拿 token 的地方），只有 `_attach_token`
    # 统一加的那个可选字段——所以这里列的是业务字段。
    "session_start": {"citizen", "display_name", "session_name", "project", "kind"},
    # 2026-10-25 加（用户要求补）：看板的 MCP 面。show 只读、checkin 要凭证。
    # 2026-10-25 补 post（EVAL-261025-10）：MCP 面做完无法标 done，看板丢掉"动到哪了"。
    # 2026-10-25 再加 status（EVAL-261025-14）：left 在 DDL 里有、入口没有，
    # 想"收工下板"的人做不到。补上后 left 才从词汇变成动作。
    # 2026-10-25 再加 assume_surface（EVAL-261025-16）：单人时 simulate 对比 0 人，
    # 验证不了撞车检测；这个参数让 AI 假设一个占用者，走同一套 surface_hit。
    "board": {"action", "project", "doing", "surface", "kind", "text",
              "status", "assume_surface"},
    # 2026-10-26（T05 / B3）：知识库读 / 写两个工具。字段与架构设计 §3.3 逐字一致，
    # **都不含 token**（`_attach_token` 统一补），所以这里列的是业务字段。
    # 第二轮（R15）：`kb` 增了可选 `allow_sensitive`（敏感文件正文的二次授权）。
    # 第四轮（S6）：`kb_write` 也增了可选 `allow_sensitive`（凭证/.env 默认拒写）。
    "kb": {"action", "vault", "path", "query", "depth", "limit", "max_chars",
           "allow_sensitive"},
    "kb_write": {"action", "vault", "path", "content", "frontmatter",
                 "base_sha256", "reason", "allow_sensitive"},
    # 2026-10-27（语料层 T03）：18 → 19，新增 `corpus`（search/read/stats 三读动作）。
    # 与其它工具同一口径：token 由 `_attach_token` 统一补，这里只列业务字段。
    "corpus": {"action", "host", "limit", "max_chars", "pointer", "project",
               "query", "ranker", "role"},
}

# 进程态身份的名字——判据 H 的扫描目标（出现即红，注释不算：只看语法树里的属性访问）。
FORBIDDEN_STATE_ATTRS = ("_actor", "_session_actor", "_hint_shown", "actor_name",
                         "current_actor", "_citizen")
# FishServer 允许持有的实例属性：只有装配事实（根目录 + 连接）。
ALLOWED_INSTANCE_ATTRS = {"root", "conn"}


class FishIdentityP1cTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fish-id-p1c-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ 夹具

    def _root(self, name):
        root = Path(self.tmp) / name
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _conn(self, root):
        """生产装配路径：connect + SCHEMA + migrate（与 cmd_* / FishServer 完全一致）。"""
        conn = fish.connect(root)
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        self._seed_prefix(conn)
        return conn

    def _seed_prefix(self, conn):
        conn.execute(
            "INSERT OR IGNORE INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES (?,?,?,?,?)", (PREFIX, PREFIX, "", 0, "active"))
        conn.commit()

    def _start(self, conn, alias):
        """签一条真凭证（CLI 侧的路径：能读 env；这里用显式 environ 隔开宿主 env）。"""
        return fish.start_session(conn, alias=alias,
                                  environ={"FISH_ACTOR": alias,
                                           "FISH_SESSION_ACTOR": alias})

    def _server(self, root):
        """起一个真的 FishServer（**不 patch 任何环境变量**：本实现根本不读它们）。"""
        return mcp_server.FishServer(root)

    @staticmethod
    def _code_of(text):
        return text.splitlines()[0].strip()

    def _owner_row(self, root, code):
        """独立连接读真值（不经过被测实现）。"""
        conn = fish.connect(root)
        try:
            return conn.execute(
                "SELECT owner,session_actor FROM code_index WHERE code=?", (code,)).fetchone()
        finally:
            conn.close()

    def _access_rows(self, root, via=None):
        conn = fish.connect(root)
        try:
            return fish.access_log_rows(conn, via=via, limit=50)
        finally:
            conn.close()

    def _remember(self, srv, token=None, title="判据记忆", body="判据正文"):
        args = {"project": PREFIX, "title": title, "body": body}
        if token is not None:
            args["token"] = token
        return srv.call_tool("remember", args)

    def _call_via_protocol(self, srv, name, args):
        """走**协议层**调一个工具（`tools/call`）。

        与 `call_tool` 的区别：协议层才会写 `_audit/tool-calls.jsonl`。
        要审"审计里记了什么"的判据（I）必须走这条路——只在工具层调用的话，
        审计文件根本不存在，那样的判据是恒真（扫描面失效）。
        """
        resp = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": args}})
        return resp["result"]["content"][0]["text"]

    # ------------------------------------------------------------ 判据 A

    def _assert_token_attribution(self, root, out, token_res, where=""):
        """判据 A 的断言（变异用例会重跑它，必须能红）。"""
        code = self._code_of(out)
        owner, sess = self._owner_row(root, code)
        self.assertEqual(owner, token_res["citizen_id"],
                         f"{where}：owner 必须是 token 解出的公民，实际 {owner!r}")
        self.assertEqual(sess, token_res["session_key"],
                         f"{where}：code_index.session_actor 必须是凭证的 session_key，"
                         f"实际 {sess!r}")
        self.assertNotIn("未归属", out, f"{where}：有凭证的调用不该带'未归属'提示")

    def test_A_token_attribution(self):
        """判据 A：session start 拿 token → remember 带 token → owner=该公民。

        同时验 access_log.citizen_id 的来源（要求 5：两个写入点都必须来自 token，
        不能一个来自 token、另一个还在用进程内存）。
        """
        root = self._root("a")
        conn = self._conn(root)
        try:
            res = self._start(conn, "p1c-a")
        finally:
            conn.close()
        self.assertTrue(res["citizen_id"], "前提：session start 必须签出公民")
        srv = self._server(root)
        try:
            out = self._remember(srv, token=res["token"], title="A 带凭证")
            self._assert_token_attribution(root, out, res, "判据 A")
            code = self._code_of(out)
            # access_log 的写入点：recall 带同一个 token
            srv.call_tool("recall", {"query": code, "token": res["token"]})
        finally:
            srv.conn.close()
        rows = self._access_rows(root, via="recall")
        self.assertTrue(rows, "前提：recall 必须写一条访问记录")
        for code, cit, sess, proj, via, payload, ts in rows:
            self.assertEqual(cit, res["citizen_id"],
                             f"access_log.citizen_id 应来自 token，实际 {cit!r}（via={via}）")
            self.assertEqual(sess, res["session_key"],
                             f"access_log.session_key 应来自凭证，实际 {sess!r}（via={via}）")

    def test_A_mutation_process_identity_goes_red(self):
        """变异自证：身份不再来自 token，而是"进程态身份"（不校验 token 直接给一个公民）。"""
        def process_state(self, token):
            return mcp_server.Identity("C-PROC-ACTOR", "self:proc", "machine",
                                       mcp_server.IDENTITY_VALID)

        root = self._root("mut-a")
        conn = self._conn(root)
        try:
            res = self._start(conn, "p1c-am")
        finally:
            conn.close()
        srv = self._server(root)
        try:
            with mock.patch.object(mcp_server.FishServer, "_resolve_identity", process_state):
                out = self._remember(srv, token=res["token"])
        finally:
            srv.conn.close()
        with self.assertRaises(AssertionError):
            self._assert_token_attribution(root, out, res, "变异：进程态身份")

    # ------------------------------------------------------------ 判据 B

    def _assert_unattributed(self, root, out, where=""):
        """判据 B 的断言：owner 是 'unattributed' + 提示两句都在。"""
        code = self._code_of(out)
        owner, sess = self._owner_row(root, code)
        self.assertEqual(owner, "unattributed",
                         f"{where}：无凭证时必须记 'unattributed'，实际 {owner!r}")
        self.assertIsNotNone(owner, f"{where}：owner 不能是 None（NULL 会被读侧当成'老数据'）")
        self.assertNotEqual(owner, "", f"{where}：owner 不能是空串")
        self.assertIsNone(sess, f"{where}：无凭证时 session_actor 必须为空，实际 {sess!r}")
        self.assertIn("未归属", out, f"{where}：回执必须明说'未归属'")
        self.assertIn("未带会话凭证", out, f"{where}：回执必须说明是'没带凭证'")
        # 2026-10-25 改（用户批准加 `session_start` 工具后）：下一步从"跑 CLI"
        # 变成了"调本进程内的 session_start"。判据钉**意图**（必须给出拿 token 的路径），
        # 不钉具体是 CLI 还是工具——那正是这一轮改掉的东西。
        self.assertIn("session_start", out, f"{where}：回执必须给出下一步（拿 token）")

    def test_B_no_token_is_unattributed(self):
        """判据 B：不带 token 调 remember → owner='unattributed' + 明确提示。"""
        root = self._root("b")
        conn = self._conn(root)
        conn.close()
        srv = self._server(root)
        try:
            out = self._remember(srv, title="B 无凭证")
        finally:
            srv.conn.close()
        self._assert_unattributed(root, out, "判据 B")
        # 反例排除：'unknown' 是旧实现的占位值，不能用它冒充"未归属"
        code = self._code_of(out)
        self.assertNotEqual(self._owner_row(root, code)[0], "unknown",
                            "无凭证时不许用 'unknown' 之类的旧占位值")

    def test_B_mutation_owner_unknown_goes_red(self):
        """变异自证：把未归属写成旧占位 'unknown' → 判据 B 必红。"""
        with mock.patch.object(mcp_server.FishServer, "_owner_value",
                               lambda self, ident: ident.citizen_id or "unknown"):
            root = self._root("mut-b1")
            self._conn(root).close()
            srv = self._server(root)
            try:
                out = self._remember(srv, title="变异 unknown")
            finally:
                srv.conn.close()
        with self.assertRaises(AssertionError):
            self._assert_unattributed(root, out, "变异：owner=unknown")

    def test_B_mutation_hint_off_goes_red(self):
        """变异自证：不追加归属提示（提示表清空）→ 判据 B 必红。"""
        with mock.patch.object(mcp_server, "IDENTITY_HINTS", {}):
            root = self._root("mut-b2")
            self._conn(root).close()
            srv = self._server(root)
            try:
                out = self._remember(srv, title="变异 无提示")
            finally:
                srv.conn.close()
        with self.assertRaises(AssertionError):
            self._assert_unattributed(root, out, "变异：没有提示")

    # ------------------------------------------------------------ 判据 C

    def _assert_rejected_token(self, root, out, where="", needle="凭证无效"):
        code = self._code_of(out)
        owner, sess = self._owner_row(root, code)
        self.assertEqual(owner, "unattributed",
                         f"{where}：坏凭证必须归 'unattributed'，实际 {owner!r}")
        self.assertIsNone(sess, f"{where}：坏凭证不该带 session_key，实际 {sess!r}")
        self.assertIn(needle, out, f"{where}：回执必须说明凭证为什么不能用（缺「{needle}」）")
        self.assertIn("未归属", out, f"{where}：回执必须明说归属为'未归属'")

    def test_C_fake_token_rejected(self):
        """判据 C：带随机 token → 归 unattributed + 提示"凭证无效"。"""
        root = self._root("c")
        self._conn(root).close()
        srv = self._server(root)
        fake = "fsh_" + "A" * 43          # 形状合法、库里不存在
        try:
            out = self._remember(srv, token=fake, title="C 假凭证")
        finally:
            srv.conn.close()
        self._assert_rejected_token(root, out, "判据 C")
        # 假 token 也不许出现在回执里（回执复读凭证 = 变相把明文传播出去）
        self.assertNotIn(fake, out, "回执里不许复读 token 明文")

    def test_C_mutation_accepting_fake_token_goes_red(self):
        """变异自证：假 token 也接受（只看"有没有 token"，不查表）→ 判据 C 必红。"""
        def lenient(self, token):
            if not token:
                return mcp_server.IDENTITY_ANONYMOUS
            return mcp_server.Identity("C-FAKE", "self:fake", "self-reported",
                                       mcp_server.IDENTITY_VALID)

        root = self._root("mut-c")
        self._conn(root).close()
        srv = self._server(root)
        try:
            with mock.patch.object(mcp_server.FishServer, "_resolve_identity", lenient):
                out = self._remember(srv, token="fsh_" + "B" * 43, title="变异 假凭证也收")
        finally:
            srv.conn.close()
        with self.assertRaises(AssertionError):
            self._assert_rejected_token(root, out, "变异：假 token 也接受")

    # ------------------------------------------------------------ 判据 D

    def test_D_revoked_and_expired_tokens_rejected(self):
        """判据 D：session end 之后 / 超过 TTL 之后，旧 token 必须被拒。

        两种失效各验一次——它们走的是**两个不同的字段**（revoked_at / expires_at），
        只查其中一个的实现在另一条上会静默通过。
        """
        root = self._root("d")
        conn = self._conn(root)
        try:
            revoked = self._start(conn, "p1c-d-revoked")
            expired = self._start(conn, "p1c-d-expired")
            fish.end_session(conn, token=revoked["token"])
            # 造过期：直接改 expires_at 到过去（不靠 sleep，判据要的是"窗口判定"这件事）
            conn.execute("UPDATE session_tokens SET expires_at=? WHERE token_hash=?",
                         ("2000-01-01T00:00:00Z", fish.hash_session_token(expired["token"])))
            conn.commit()
            self.assertIsNone(fish.validate_session_token(conn, expired["token"]),
                              "夹具前提：这条 token 必须真的已过期")
        finally:
            conn.close()

        srv = self._server(root)
        try:
            out_revoked = self._remember(srv, token=revoked["token"], title="D 已吊销")
            out_expired = self._remember(srv, token=expired["token"], title="D 已过期")
        finally:
            srv.conn.close()
        self._assert_rejected_token(root, out_revoked, "判据 D（吊销）", needle="吊销")
        self._assert_rejected_token(root, out_expired, "判据 D（过期）", needle="过期")

    def test_D_mutation_ignoring_validity_goes_red(self):
        """变异自证：只查"这行在不在"、不查 revoked_at / expires_at → 判据 D 必红。"""
        real_lookup = fish.lookup_session_token

        def blind(conn_, token_, at=None):
            info = real_lookup(conn_, token_)      # 只看行在不在（不判有效性）
            return info["citizen_id"] if info else None

        root = self._root("mut-d")
        conn = self._conn(root)
        try:
            revoked = self._start(conn, "p1c-dm")
            fish.end_session(conn, token=revoked["token"])
        finally:
            conn.close()
        srv = self._server(root)
        try:
            with mock.patch.object(fish, "validate_session_token", blind):
                out = self._remember(srv, token=revoked["token"], title="变异 不判吊销")
        finally:
            srv.conn.close()
        with self.assertRaises(AssertionError):
            self._assert_rejected_token(root, out, "变异：不查 revoked_at", needle="吊销")

    # ------------------------------------------------------------ 判据 E（本轮核心）

    def _assert_no_cross_talk(self, root, pairs, where=""):
        """判据 E 的断言：每个编码的归属必须等于**写它的那个 token** 的公民。

        pairs: [(token_res, out), ...]。真值每次都从独立连接读。
        """
        citizens = {token_res["citizen_id"] for token_res, _ in pairs}
        self.assertEqual(len(citizens), 2,
                         f"{where}：夹具不合格——两个 token 必须属于不同公民，实际 {citizens}")
        for token_res, out in pairs:
            code = self._code_of(out)
            owner, sess = self._owner_row(root, code)
            self.assertEqual(owner, token_res["citizen_id"],
                             f"{where}：{code} 应归 {token_res['citizen_id']}，实际 {owner!r}"
                             "（串号：一个会话拿到了另一个会话的身份）")
            self.assertEqual(sess, token_res["session_key"],
                             f"{where}：{code} 的 session_key 应是 "
                             f"{token_res['session_key']}，实际 {sess!r}")

    def test_E_two_sessions_do_not_cross_talk(self):
        """判据 E：一个 MCP 进程服务两个会话 → 归属各归各的，且顺序无关。

        这是本轮的核心判据：旧实现（进程内存态身份）在这个夹具上**必然串号**——
        第二次调用会拿到第一次的身份（谁最后 register 谁生效）。
        """
        root = self._root("e")
        conn = self._conn(root)
        try:
            a = self._start(conn, "p1c-e-A")
            b = self._start(conn, "p1c-e-B")
        finally:
            conn.close()
        self.assertNotEqual(a["citizen_id"], b["citizen_id"], "前提：两个会话是不同公民")

        srv = self._server(root)           # **同一个 server 对象**：一个进程，多个会话
        try:
            # 顺序 1：先 A 后 B
            out_a = self._remember(srv, token=a["token"], title="E-A 写的")
            out_b = self._remember(srv, token=b["token"], title="E-B 写的")
            # 顺序 2：对调再来一遍（排除"第一次对、第二次错"的偶然）
            out_b2 = self._remember(srv, token=b["token"], title="E-B 又写")
            out_a2 = self._remember(srv, token=a["token"], title="E-A 又写")
            # access_log 也不能串：A 的 token 去 recall B 写的条目
            code_a = self._code_of(out_a)
            srv.call_tool("recall", {"query": code_a, "token": b["token"]})
        finally:
            srv.conn.close()
        self._assert_no_cross_talk(root, [(a, out_a), (b, out_b), (b, out_b2), (a, out_a2)],
                                   "判据 E")
        rows = self._access_rows(root, via="recall")
        self.assertTrue(rows, "前提：recall 应写一条访问记录")
        for code, cit, sess, proj, via, payload, ts in rows:
            self.assertEqual(cit, b["citizen_id"],
                             f"access_log 串号：本次调用带的是 B 的 token，实际记到 {cit!r}")
            self.assertEqual(sess, b["session_key"],
                             f"access_log 的 session_key 串号：实际 {sess!r}")

    def test_E_mutation_process_memory_goes_red(self):
        """变异自证（核心）：把身份放回进程内存（第一次解析后一直复用）→ 判据 E 必红。

        这就是 2026-10-23 那台 pid 37192 的形状：进程记住了一个身份，后来的会话沿用它。
        """
        real_call = mcp_server.FishServer.call_tool

        def memory_call(self, name, args, ident=None):
            if not hasattr(self, "_mut_identity"):
                self._mut_identity = mcp_server.FishServer._resolve_identity(
                    self, (args or {}).get(mcp_server.TOKEN_ARG))
            return real_call(self, name, args, self._mut_identity)

        root = self._root("mut-e")
        conn = self._conn(root)
        try:
            a = self._start(conn, "p1c-em-A")
            b = self._start(conn, "p1c-em-B")
        finally:
            conn.close()
        srv = self._server(root)
        try:
            with mock.patch.object(mcp_server.FishServer, "call_tool", memory_call):
                out_a = self._remember(srv, token=a["token"], title="变异 E-A")
                out_b = self._remember(srv, token=b["token"], title="变异 E-B")
        finally:
            srv.conn.close()
        with self.assertRaises(AssertionError):
            self._assert_no_cross_talk(root, [(a, out_a), (b, out_b)], "变异：进程内存态")

    # ------------------------------------------------------------ 判据 F

    def test_F_whoami_without_token_reveals_nobody(self):
        """判据 F：先 register 一个公民，再不带 token 调 whoami → 不能返回那个公民。

        而且这里刻意把 `FISH_ACTOR` / `FISH_SESSION_ACTOR` 设成那个公民的名字：
        旧实现正是从环境变量里把名字捞出来当身份的（事故里的 `actor=fish-main`）。
        """
        root = self._root("f")
        self._conn(root).close()
        srv = self._server(root)
        try:
            reg = srv.call_tool("register", {"display_name": "p1c-ghost",
                                             "kind": "visitor", "host": "dsh"})
            self.assertIn("p1c-ghost", reg, "前提：register 必须建出这个公民")
            with mock.patch.dict(os.environ, {"FISH_ACTOR": "p1c-ghost",
                                              "FISH_SESSION_ACTOR": "p1c-ghost"}):
                text = srv.call_tool("whoami", {})
            # 对照组：证明"没有输出身份"不是因为 whoami 根本没跑
            self.assertIn("身份未核", text, "无凭证时 whoami 必须说'身份未核'")
            self.assertIn("主人：", text, "whoami 仍要报主人（它是系统事实，不是本次身份）")
            self.assertNotIn("p1c-ghost", text,
                             "whoami 把 register 过的公民 / env 里的名字当成了'你'——"
                             "这正是串号事故的取证来源")
            self.assertNotIn("登记身份", text, "无凭证时不许出现'登记身份'这一行")
        finally:
            srv.conn.close()

    def test_F_mutation_env_fallback_goes_red(self):
        """变异自证：无 token 时退回 env 身份（旧实现）→ 判据 F 必红。"""
        real_whoami = mcp_server.FishServer.tool_whoami

        def env_whoami(self, a, ident):
            if ident.status != mcp_server.IDENTITY_VALID:
                found = fish.find_citizen_by_name(self.conn, os.environ.get("FISH_ACTOR", ""))
                if found:
                    ident = mcp_server.Identity(found[0], None, "self-reported",
                                                mcp_server.IDENTITY_VALID)
            return real_whoami(self, a, ident)

        root = self._root("mut-f")
        self._conn(root).close()
        srv = self._server(root)
        try:
            srv.call_tool("register", {"display_name": "p1c-ghost", "kind": "visitor"})
            with mock.patch.dict(os.environ, {"FISH_ACTOR": "p1c-ghost"}), \
                    mock.patch.object(mcp_server.FishServer, "tool_whoami", env_whoami):
                text = srv.call_tool("whoami", {})
        finally:
            srv.conn.close()
        with self.assertRaises(AssertionError):
            self.assertNotIn("p1c-ghost", text, "变异没生效：env 身份还是被捞出来了")

    # ------------------------------------------------------------ 判据 G

    def _assert_signature_only_token(self, where=""):
        """判据 G 的断言：工具表 = 基线 11 个，字段 = 基线 + 可选 token（+ inject 的 project）。"""
        names = [t["name"] for t in mcp_server.TOOLS]
        # 2026-10-25（阶段 B/D）：11 → 12（rename_citizen）→ 14（close/reopen）
        # → 15（session_start，用户批准：MCP 面缺签发口导致通道归属分裂）
        # → 16（board，用户批准：看板"看得见、动不了"，签到闭环断在 MCP 面没有签到工具）。
        # 2026-10-26（T05 / B3）：16 → 18（kb 读 + kb_write 写；读/写是两个权限位，裁决 C2）。
        # 2026-10-27（语料层 T03）：18 → 19（新增 `corpus`：search/read/stats 三读动作；
        # 主理人已批准的设计契约变更；18 个旧工具的描述/schema 由 golden 逐字锁住）。
        self.assertEqual(len(names), 19,
                         f"{where}：工具数量变了（{len(names)} 个：{names}）")
        self.assertEqual(sorted(names), sorted(BASELINE_TOOL_PROPS),
                         f"{where}：工具集合变了：{names}")
        for tool in mcp_server.TOOLS:
            props = tool["inputSchema"]["properties"]
            self.assertIn("token", props, f"{where}：{tool['name']} 缺可选 token 入参")
            self.assertEqual(set(props), BASELINE_TOOL_PROPS[tool["name"]] | {"token"},
                             f"{where}：{tool['name']} 的入参被改了"
                             "（只允许新增可选 token；inject 的 project 已在基线里声明）")
            required = tool["inputSchema"].get("required") or []
            self.assertNotIn("token", required,
                             f"{where}：{tool['name']} 把 token 变成了必填——旧调用会断")
            # 反向：不给任何工具留 session 类签发入口（18 §4 铁律）
            self.assertNotIn("session", props, f"{where}：{tool['name']} 混进了 session 字段")

    def test_G_tool_signature_only_adds_optional_token(self):
        """判据 G：11 个工具逐个比对，只允许新增 token（+ 已声明的 inject.project）可选字段。"""
        self._assert_signature_only_token("判据 G")
        # 线上暴露面（tools/list）必须与常量一致：包装函数不能只在常量上生效
        srv = self._server(self._root("g"))
        try:
            resp = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                               "params": {}})
        finally:
            srv.conn.close()
        listed = resp["result"]["tools"]
        self.assertEqual([t["name"] for t in listed],
                         [t["name"] for t in mcp_server.TOOLS],
                         "tools/list 暴露的工具与 TOOLS 常量不一致")
        for tool in listed:
            self.assertIn("token", tool["inputSchema"]["properties"],
                          f"tools/list 里的 {tool['name']} 没有 token（包装漏了？）")

    def test_G_mutation_extra_field_goes_red(self):
        """变异自证：给某个工具多加一个字段 → 判据 G 必红。"""
        mutated = [dict(t, inputSchema=dict(t["inputSchema"],
                                            properties=dict(t["inputSchema"]["properties"])))
                   for t in mcp_server.TOOLS]
        for tool in mutated:
            if tool["name"] == "remember":
                tool["inputSchema"]["properties"]["owner"] = {"type": "string"}
        with mock.patch.object(mcp_server, "TOOLS", mutated):
            with self.assertRaises(AssertionError):
                self._assert_signature_only_token("变异：多一个字段")

    def test_G_mutation_token_required_goes_red(self):
        """变异自证：把 token 塞进 required → 判据 G 必红（旧调用会断）。"""
        mutated = [dict(t, inputSchema=dict(t["inputSchema"],
                                            properties=dict(t["inputSchema"]["properties"])))
                   for t in mcp_server.TOOLS]
        for tool in mutated:
            if tool["name"] == "whoami":
                tool["inputSchema"]["required"] = ["token"]
        with mock.patch.object(mcp_server, "TOOLS", mutated):
            with self.assertRaises(AssertionError):
                self._assert_signature_only_token("变异：token 变必填")

    # ------------------------------------------------------------ 判据 H（自证：进程里无身份）

    @staticmethod
    def _forbidden_state_hits(source_path):
        """扫源码里对**进程态身份属性**的访问（属性名或函数名命中即算）。

        只看语法树：注释里出现 `self._actor` 是允许的（那是解释历史），
        真正的赋值/读取才是状态。
        """
        tree = ast.parse(Path(source_path).read_text(encoding="utf-8"))
        hits = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_STATE_ATTRS:
                hits.append(f"Attribute .{node.attr} @line {node.lineno}")
            elif isinstance(node, ast.FunctionDef) and node.name in FORBIDDEN_STATE_ATTRS:
                hits.append(f"FunctionDef {node.name} @line {node.lineno}")
        return hits

    def _assert_no_process_identity(self, source_path, where=""):
        hits = self._forbidden_state_hits(source_path)
        self.assertEqual(hits, [],
                         f"{where}：源码里仍有进程态身份（{hits}）——"
                         "进程被所有会话共享，任何'当前身份'都是错的")

    def test_H_no_process_identity_state(self):
        """判据 H：进程里已无身份状态——静态（AST）+ 运行时（__dict__）双证。

        静态证：mcp_server.py 里没有任何 `self._actor` / `_session_actor` /
                `current_actor` / `_citizen` / `_hint_shown` 的访问或定义；
        运行时证：跑过"带凭证调用 + register"之后，FishServer 实例上仍然只有
                root / conn 两个属性——没有哪次调用把身份留在了对象上。
        """
        self._assert_no_process_identity(MCP_SOURCE, "判据 H（静态）")

        root = self._root("h")
        conn = self._conn(root)
        try:
            res = self._start(conn, "p1c-h")
        finally:
            conn.close()
        srv = self._server(root)
        try:
            self._remember(srv, token=res["token"], title="H 带凭证")
            srv.call_tool("whoami", {"token": res["token"]})
            srv.call_tool("register", {"display_name": "p1c-h-ghost", "kind": "visitor"})
            srv.call_tool("whoami", {})
            attrs = set(vars(srv))
        finally:
            srv.conn.close()
        self.assertEqual(attrs, ALLOWED_INSTANCE_ATTRS,
                         f"FishServer 实例上多出了属性 {sorted(attrs - ALLOWED_INSTANCE_ATTRS)}"
                         "——身份一旦落在对象上，下一个会话就会继承它")

    def test_H_mutation_reintroducing_state_goes_red(self):
        """变异自证（静态）：在临时副本的 __init__ 里加回 `self._actor = None` → 判据红。

        在**临时副本**上改，真源码一个字节都不动（与 P1-a 判据 G 的变异同一套做法）。
        """
        source = MCP_SOURCE.read_text(encoding="utf-8")
        marker = "        fish.migrate(self.conn)\n"
        self.assertIn(marker, source, "变异夹具失效：源码里找不到装配行")
        broken = source.replace(marker, marker + "        self._actor = None\n", 1)
        copy = Path(self.tmp) / "mcp_server_broken.py"
        copy.write_bytes(broken.encode("utf-8"))
        with self.assertRaises(AssertionError):
            self._assert_no_process_identity(copy, "变异：把 self._actor 加回来")

    # ------------------------------------------------------------ 判据 I（凭证不落盘）

    def _assert_token_not_on_disk(self, root, token, where=""):
        needle = token.encode("utf-8")
        hits = []
        for path in Path(root).rglob("*"):
            if path.is_file() and needle in path.read_bytes():
                hits.append(str(path.relative_to(Path(root))))
        self.assertEqual(hits, [], f"{where}：token 明文出现在 {hits}")

    def test_I_token_never_persisted(self):
        """判据 I：调用带 token 之后，返回值/审计/磁盘都没有 token 明文。

        审计是**必查点**：事故那天最难查的就是"审计里那一行到底是谁写的"，
        修法很容易顺手把 token 一起写进去——那样就把凭证泄漏进了长期日志。
        """
        root = self._root("i")
        conn = self._conn(root)
        try:
            res = self._start(conn, "p1c-i")
        finally:
            conn.close()
        token = res["token"]
        srv = self._server(root)
        try:
            # 走协议层（tools/call）：只有这条路会写审计，判据才有扫描面
            out = self._call_via_protocol(
                srv, "remember",
                {"project": PREFIX, "title": "I 凭证不落盘", "token": token})
            self._call_via_protocol(srv, "recall",
                                    {"query": self._code_of(out), "token": token})
        finally:
            srv.conn.close()
        self.assertNotIn(token, out, "回执里不许复读 token 明文")
        self._assert_token_not_on_disk(root, token, "判据 I")
        # 反证：审计真的写了（否则"扫描不到"可能是恒真）
        audit = root / "_audit" / "tool-calls.jsonl"
        self.assertTrue(audit.exists(), "审计文件不存在：判据 I 的扫描面失效")
        rows = [line for line in audit.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertTrue(rows, "审计文件是空的：判据 I 的扫描面失效")
        self.assertIn('"identity_status": "valid"', "\n".join(rows),
                      "审计应记下 identity_status=valid（否则这次调用没真正归属）")

    def test_I_mutation_leaking_token_goes_red(self):
        """变异自证：把 token 写进 <root>/_audit/ 目录 → 判据 I 必红。"""
        real_resolve = mcp_server.FishServer._resolve_identity

        def leaky(self, token):
            if token:
                d = self.root / "_audit"
                d.mkdir(exist_ok=True)
                (d / "leak-token.txt").write_text(token, encoding="utf-8")
            return real_resolve(self, token)

        root = self._root("mut-i")
        conn = self._conn(root)
        try:
            res = self._start(conn, "p1c-im")
        finally:
            conn.close()
        srv = self._server(root)
        try:
            with mock.patch.object(mcp_server.FishServer, "_resolve_identity", leaky):
                self._call_via_protocol(
                    srv, "remember",
                    {"project": PREFIX, "title": "变异 凭证落盘", "token": res["token"]})
        finally:
            srv.conn.close()
        with self.assertRaises(AssertionError):
            self._assert_token_not_on_disk(root, res["token"], "变异：token 落盘")


if __name__ == "__main__":
    unittest.main(verbosity=2)
