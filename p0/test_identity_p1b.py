#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fish 身份层 · P1-b（session 命令族）验收
=========================================
依据 `18-身份层实现设计-P1b.md`。本轮把 **签发** 落地（P1-a 只建了结构）。

  一句话：MCP 面拿不到真实会话身份（env 被 scrub），所以身份只能由"能读到 env 的
  一方"签发，再交给 MCP 校验。这就是 `session` 命令族存在的全部理由。

九条判据（A–H 是任务书要求的，I 是本轮自己加的）：

  A  start 能签发：输出 token / 公民入库 / 凭证入库（库里只有 sha256）
  B  幂等：同 env 连跑两次 → 同一个 citizen_id，公民只多 1 行；凭证轮换（旧的行留、revoked_at 有值）
  C  evidence 分级：有宿主 env → machine；只有 --alias → self-reported；什么都没有 → 匿名自报
  D  档期唯一：切项目只有一个开档期、旧的有 to_ts；同项目重跑不切（档期是时间段不是打卡）
  E  子 agent 一跳：父 machine → 子 inherited；拿 inherited / self-reported 当父 → 被拒
  F  end 吊销：revoked_at 有值、validate 失败、档期关闭、子凭证级联、重复调幂等
  G  token 不落盘：扫遍 root 与 cwd 的每个文件都找不到明文；事件表里只有 hash
  H  不注册 MCP 工具：工具表仍是 11 个、分发表里没有 session，而 CLI 侧确实有这个命令
  I  会话公民的 SQL 面冻结（P1-a 判据 G 只允许"声明过的"新增引用，这里把那 4 个函数的
     语句逐条钉死——"声明"不该是一张空白支票）

纪律（与 test_identity_p1a / test_push_p0 同一套）：**每条判据都要先证明它在坏输入下会红**。
所以每条判据都配一条 `*_mutation_*`：把对应的一环弄坏，再把**同一组断言**重跑一遍，
它必须报错。若它照样绿，说明断言没有判别力（恒绿判据）。
真正的"改源码 → 必红 → 字节还原"由 `mutation_identity_p1b.py` 做，两层互为补充：
本文件守"判据有判别力且可进 CI"，那个脚本守"真删了源码真的会红"。

两条必须写死的测试纪律：
  · 跑子进程前**清掉**所有身份类 env：否则 evidence 由"跑测试的机器"决定而不是用例
    （本机若恰好设了 DSH_SESSION_ID，判据 C 就变成恒绿/恒红的噪声）；
  · token 明文只许留在内存里：断言失败消息里也不拼它——失败输出会被 CI 收走。
"""
import ast
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fish  # noqa: E402
import mcp_server  # noqa: E402

FISH = HERE / "fish.py"
# T02 拆包（2026-10-27）：`_session_labels_for` 已搬进 `fish_push.py`——判据 I 扫**模块树**
# 而不是单文件（SQL 冻结的语义不变：同名函数的 citizens 语句仍逐条比对）。
FISH_PUSH = HERE / "fish_push.py"
MCP = HERE / "mcp_server.py"

# 身份类 env：跑子进程前必须全部清掉（见模块头）。
SCRUB_ENVS = ("DSH_SESSION_ID", "CODEBUDDY_SESSION_ID", "CLAUDE_SESSION_ID",
              "FISH_SESSION_ACTOR", "FISH_ACTOR")

# stdout 的格式契约（18 §2 第 6 步）。用行首锚定，避免把正文里的同名文本抓进来。
TOKEN_RE = re.compile(r"^token: (\S+)$", re.M)


def clean_env(**overrides):
    """当前进程的环境变量副本，去掉所有身份类变量后再叠加用例指定的值。"""
    env = {k: v for k, v in os.environ.items() if k not in SCRUB_ENVS}
    env.update({k: v for k, v in overrides.items() if v is not None})
    return env


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def field_of(out, name):
    """从 start/agent 的输出里取一行字段（格式是契约，取不到就是契约破了）。"""
    m = re.search(rf"^{re.escape(name)}: (.*)$", out, re.M)
    return m.group(1).strip() if m else None


# 判据 I 的基线：P1-b 的 4 个会话公民读写点，按「函数 → 语句」逐条冻结。
# 为什么单独钉一遍：P1-a 的判据 G 只要求"新增引用函数**声明**出来"，
# 声明本身不约束语句内容；没有这一条，"声明"就等于允许这 4 个函数随意改写查询面。
SESSION_CITIZEN_SQL = {
    "find_session_citizen": (
        "SELECT citizen_id,display_name,kind,role,host,status,account_id,host_session_key,"
        "last_seen_at,archived_at FROM citizens_all WHERE host_session_key=? "
        "AND status='active' ORDER BY created_at LIMIT 1",
    ),
    "find_or_create_session_citizen": (
        "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at,"
        "is_owner,account_id,device_id,host_session_key,last_seen_at,archived_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        "UPDATE citizens SET account_id=? WHERE citizen_id=?",
    ),
    "load_citizen": (
        "SELECT citizen_id,display_name,kind,role,host,status,account_id,host_session_key,"
        "last_seen_at,archived_at FROM citizens_all WHERE citizen_id=?",
    ),
    "touch_citizen": (
        "UPDATE citizens SET last_seen_at=? WHERE citizen_id=?",
    ),
    # 2026-10-25（外部评估 `EVAL-261025-05` 修复）：`start_session` 新增 2 条
    # citizens 语句（校验 `--citizen` 指向的公民是否存在；补 account_id）。
    # 这是 **声明出来的契约变更**，上面 4 个函数的语句一字未改。
    # 为什么必须有它：`--citizen` 是"同一 AI 在 MCP 与 CLI 两条通道归属分裂"的
    # 唯一显式出口——绑到一个不存在的公民上，比不绑更糟（它会静默成功）。
    "start_session": (
        "SELECT citizen_id, display_name, account_id, archived_at FROM citizens "
        "WHERE citizen_id=?",
        "UPDATE citizens SET account_id=? WHERE citizen_id=?",
    ),
    # 2026-10-26（撞车自撞修复 T02）：`_session_labels_for`——推送候选要按**会话**取
    # 可读名（`名字·dsh#88f9`），所以它查一次 citizens 拿 display_name / host /
    # created_at。这是**声明出来的新读点**（上面 5 个函数的语句一字未改），
    # 而且从今往后它的 SQL 也被这条判据逐字冻结。
    "_session_labels_for": (
        "SELECT citizen_id, display_name, host, created_at FROM citizens "
        "WHERE citizen_id IN (",
    ),
}

# 派生的常量（改一处忘另一处就会在判据 I 里当场红）。
EXPECTED_NEW_FUNCS = frozenset(SESSION_CITIZEN_SQL)
EXPECTED_STMT_COUNT = sum(len(v) for v in SESSION_CITIZEN_SQL.values())

# MCP 工具表基线：**独立抄一份**，不从 mcp_server.TOOLS 反推（反推等于自己证自己）。
# 2026-10-25（阶段 B）：新增 `rename_citizen`（名字认领）。这不是"放宽基线"，是
# 有意的契约变更——用户裁定"开场指定名字却无法绑定"是 BUG，必须给它一条工具路径。
# 变更点在 docstring 里声明，且工具**必须有 token 才能改自己**（判据 R/M1/M2 钉住）。
#
# 2026-10-25 晚：新增 `session_start`（外部评估 `EVAL-261025-05`）。
# **这是用户明确批准的工具数变更（14 → 15）**，理由：在此之前 MCP 面没有任何
# 产出 token 的口子，AI 只能去跑 CLI，而 CLI 的会话键不同 → 建出第二个公民、
# 归属分裂且无法合并。`session_start` 是那个缺口的唯一补齐方式。
#
# 2026-10-26（T05 / B3）：新增 `kb`（读：vaults/tree/open/search）与 `kb_write`
# （写：create/append/update）。**读与写刻意分成两个工具**——它们是两个权限位
# （裁决 C2）；合成一个后，"只想看目录"的调用也会拿到一份写权限的说明书。
# 这两个工具的 schema 与 `docs/架构设计-B1B2B3-v0.1.md` §3.3 逐字一致，
# 且**都不含 token**（由 `_attach_token` 统一补，手抄一份会在 import 期抛错）。
MCP_TOOL_NAMES = ["remember", "recall", "neighbors", "resolve_code", "read_body",
                  "add_project", "supersede", "whoami", "register", "rename_citizen",
                  "close_project", "reopen_project", "inject", "healthcheck",
                  "session_start", "board", "kb", "kb_write",
                  # 2026-10-27（语料层 T03）：18 → 19，新增 `corpus`
                  # （search/read/stats 三读动作；主理人已批准的设计契约变更）。
                  "corpus"]

_SQL_START = re.compile(r"^(SELECT|INSERT|UPDATE|DELETE|PRAGMA|ALTER|CREATE|REPLACE)\b", re.I)


def _normalize(text):
    return " ".join(text.split())


def _node_sql(node):
    """一个节点下所有"以 SQL 关键字开头且提到 citizens"的字符串（f-string 用 {} 占位）。"""
    found = []
    for sub in ast.walk(node):
        text = None
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            text = _normalize(sub.value)
        elif isinstance(sub, ast.JoinedStr):
            text = _normalize("".join(
                str(v.value) if isinstance(v, ast.Constant) else "{}" for v in sub.values))
        if text and "citizens" in text and _SQL_START.match(text):
            found.append(text)
    return found


def citizens_sql_by_function(path):
    """{函数名: (SQL 语句...)} —— 从源码机械取（与 P1-a 判据 G 同一套口径）。"""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            hits = _node_sql(node)
            if hits:
                found[node.name] = tuple(sorted(set(hits)))
    return found


class FishIdentityP1bTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fish-id-p1b-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ 夹具

    def _root(self, name):
        root = Path(self.tmp) / name
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _conn(self, root):
        """生产装配路径：connect + SCHEMA + migrate（与 cmd_* 完全一致）。"""
        conn = fish.connect(root)
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        return conn

    def _run(self, root, args, env=None, cwd=None):
        """把 CLI 当真正的进程跑：env → 签发 → stdout，这才是判据 A/G 的观测面。"""
        proc = subprocess.run(
            [sys.executable, str(FISH), "--root", str(root)] + list(args),
            capture_output=True, encoding="utf-8", errors="replace",
            cwd=str(cwd or self.tmp), env=clean_env() if env is None else env)
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    def _start(self, root, env=None, *args):
        """跑 session start，返回 (token, stdout)。**token 只留在内存里。**"""
        rc, out, err = self._run(root, ["session", "start"] + list(args), env)
        self.assertEqual(rc, 0, f"session start 失败（退出码 {rc}）：{err}")
        m = TOKEN_RE.search(out)
        self.assertIsNotNone(m, f"stdout 里没有 `token: ` 行——输出契约破了：{out}")
        return m.group(1), out

    def _token_count(self, conn):
        return conn.execute("SELECT COUNT(*) FROM session_tokens").fetchone()[0]

    def _citizen_count(self, conn):
        return conn.execute("SELECT COUNT(*) FROM citizens").fetchone()[0]

    # ------------------------------------------------------------ 共享断言（变异用例要重跑）

    def _assert_same_citizen(self, first, second, where=""):
        """判据 B 的断言：两次签发落在同一个公民身上。"""
        self.assertEqual(first, second, f"{where}：同一会话必须是同一个公民")

    def _assert_token_persisted(self, conn, token, where="", evidence=None, channel="cli"):
        """判据 A 的断言：凭证行在、hash 对得上、默认 TTL 有值、未被吊销。"""
        row = conn.execute(
            "SELECT token_hash,citizen_id,session_key,evidence,channel,issued_at,"
            "expires_at,revoked_at FROM session_tokens").fetchall()
        self.assertEqual(len(row), 1, f"{where}：应恰好一条凭证，实际 {len(row)}")
        thash, cid, skey, ev, ch, issued, expires, revoked = row[0]
        self.assertEqual(thash, token_hash(token), f"{where}：库里存的不是这条 token 的 sha256")
        self.assertNotEqual(thash, token, f"{where}：库里存了明文（凭证纪律破了）")
        self.assertEqual(len(thash), 64, f"{where}：token_hash 应是 sha256 十六进制")
        self.assertEqual(ch, channel, f"{where}：channel 不对")
        if evidence:
            self.assertEqual(ev, evidence, f"{where}：evidence 不对")
        self.assertIsNotNone(issued, f"{where}：issued_at 不能为空")
        # 2026-10-25 契约变更（用户裁定）：**默认不过期**。
        # 公民与会话是 1:1，token 就是"会话的身份牌"，它的生命周期该跟会话一致，
        # 而不是按墙钟 24h（长会话会在第 25 小时静默失去身份，短会话则白多活 23 小时）。
        # 见 test_token_lifecycle 的 T1/T2 与 config `session.token_ttl_hours: 0`。
        self.assertIsNone(expires,
                          f"{where}：默认应不过期（expires_at=None），实际 {expires!r}")
        self.assertIsNone(revoked, f"{where}：刚签发的凭证不该是吊销态")
        self.assertIsNotNone(cid, f"{where}：凭证必须指向一个公民")
        self.assertTrue(skey, f"{where}：session_key 不能为空")

    def _assert_one_open_stint(self, conn, citizen_id, project, where=""):
        """判据 D 的断言：该公民**恰好一个**开档期，且是 project（单项目场景用）。

        多项目场景不要用它（那本来就该有多条），用 `_assert_open_projects`。
        """
        rows = conn.execute(
            "SELECT project FROM project_stints WHERE citizen_id=? AND to_ts IS NULL",
            (citizen_id,)).fetchall()
        self.assertEqual(len(rows), 1,
                         f"{where}：同一公民必须恰好一个开档期，实际 {len(rows)} 个")
        self.assertEqual(rows[0][0], project, f"{where}：开档期应是 {project}")

    def _assert_open_projects(self, conn, citizen_id, expected, where=""):
        """判据 D（多项目版）的断言：开档期的项目集合/顺序必须是 expected。

        用**有序**比对（from_ts 升序）：顺序是 `_print_session_status` 的排版依据，
        也是"最近开的那个"（current_open_stint）的判定依据，不能只比集合。
        """
        rows = conn.execute(
            "SELECT project FROM project_stints WHERE citizen_id=? AND to_ts IS NULL "
            "ORDER BY from_ts ASC, id ASC", (citizen_id,)).fetchall()
        self.assertEqual([r[0] for r in rows], list(expected),
                         f"{where}：开档期项目应为 {list(expected)}，实际 {[r[0] for r in rows]}")

    def _assert_evidence(self, res, want, where=""):
        """判据 C 的断言：证据级别（以及它与 session_key 前缀的一致性）。"""
        self.assertEqual(res["evidence"], want, f"{where}：evidence 应为 {want}")
        if want == "machine":
            self.assertEqual(res["channel"], "cli", f"{where}：CLI 渠道")
            self.assertRegex(res["session_key"], r"^(dsh|codebuddy|claude):",
                             f"{where}：machine 级的 session_key 必须带宿主前缀")
        if want == "self-reported":
            self.assertTrue(res["session_key"].startswith(("self:", "anon:")),
                            f"{where}：自报级的 session_key 必须带 self:/anon: 前缀")

    def _assert_agent_refused(self, conn, parent_token, role, where=""):
        """判据 E 的断言：拿这条父凭证**不能**签发子凭证（抛 ValueError）。"""
        before = self._token_count(conn)
        with self.assertRaises(ValueError, msg=f"{where}：父 {parent_token[:6]}… 不该被允许背书"):
            fish.issue_agent_token(conn, parent_token=parent_token, role=role)
        conn.rollback()
        self.assertEqual(self._token_count(conn), before,
                         f"{where}：被拒的签发不该留下任何行")

    def _assert_token_revoked(self, conn, token, where=""):
        """判据 F 的断言：吊销标记在、校验失败（MCP 侧看到的就该是 None）。"""
        row = conn.execute("SELECT revoked_at FROM session_tokens WHERE token_hash=?",
                           (token_hash(token),)).fetchone()
        self.assertIsNotNone(row, f"{where}：凭证行不见了——end 不该删行（审计要留）")
        self.assertIsNotNone(row[0], f"{where}：revoked_at 还是空的（没真正吊销）")
        self.assertIsNone(fish.validate_session_token(conn, token),
                          f"{where}：end 之后校验必须失败")

    def _assert_no_token_on_disk(self, bases, token, where=""):
        """判据 G 的断言：这些目录树下每一个**可读**文件都不含 token 明文。

        2026-10-27：`data/corpus.sqlite-shm` 这类运行时文件会被采集进程独占锁住
        （Windows 上 PermissionError），旧实现直接崩成 ERROR——那不是"发现泄漏"，
        是"判据不会处理判不到"。改成：跳过被锁文件并**记录**；被锁文件必须全部在
        `data/`（运行时）下，否则说明判据有盲区，响亮失败。
        """
        needle = token.encode("utf-8")
        hits, skipped = [], []
        for base in bases:
            for path in Path(base).rglob("*"):
                if not path.is_file():
                    continue
                try:
                    data = path.read_bytes()
                except PermissionError:
                    skipped.append(str(path))
                    continue
                except OSError:
                    continue
                if needle in data:
                    hits.append(str(path.relative_to(Path(base).parent)))
        self.assertEqual(hits, [], f"{where}：token 明文出现在这些文件里：{hits}")
        outside_data = [s for s in skipped if "/data/" not in s.replace("\\", "/")]
        self.assertEqual(outside_data, [],
                         f"{where}：非运行时文件读不了，判据 G 不完整：{outside_data[:5]}")

    def _assert_tool_table_frozen(self, where=""):
        """判据 H 的断言：MCP 工具表一字未变，且没有 session 类入口。"""
        names = [t["name"] for t in mcp_server.TOOLS]
        self.assertEqual(names, MCP_TOOL_NAMES,
                         f"{where}：MCP 工具表变了（本轮不许加/删/改工具）")
        for tool in mcp_server.TOOLS:
            self.assertNotIn("session", tool["inputSchema"]["properties"],
                             f"{where}：{tool['name']} 的入参里混进了 session 相关字段")

    def _assert_no_session_dispatch(self, srv, where=""):
        """判据 H 的断言：分发表里没有 session（调它必须抛未知工具）。"""
        with self.assertRaises(ValueError, msg=f"{where}：MCP 分发居然认得 session"):
            srv.call_tool("session", {})
        resp = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        listed = [t["name"] for t in resp["result"]["tools"]]
        self.assertEqual(listed, MCP_TOOL_NAMES, f"{where}：tools/list 暴露的表变了")

    # ------------------------------------------------------------ 判据 A

    def test_A_start_issues_token(self):
        """判据 A：跑一次 start → 输出 token、公民入库、凭证入库（库里只有 sha256）。"""
        root = self._root("a")
        token, out = self._start(root, clean_env(DSH_SESSION_ID="sess-A"),
                                 "--project", "DEMO", "--alias", "demo-dev")
        # 输出契约（前五行按 18 §2 第 6 步的顺序）
        self.assertEqual(out.count(token), 1, "token 明文在 stdout 里应恰好出现一次")
        # 前缀不是审美：token_urlsafe 的字母表含 `-`，约 1.3% 的 token 会以 `-` 开头，
        # 而 CLI 走 argparse，以 `-` 开头的值会被当成选项（2026-10-24 实测踩到）。
        self.assertTrue(token.startswith("fsh_"), f"token 应带 fsh_ 前缀：{token[:4]}…")
        self.assertEqual(len(token), 47, "fsh_ (4) + token_urlsafe(32) 的 43 字符")
        self.assertRegex(token[4:], r"^[A-Za-z0-9_-]{43}$", "后半段必须是 urlsafe 字母表")
        self.assertEqual(field_of(out, "evidence"), "machine")
        self.assertEqual(field_of(out, "project"), "DEMO")
        self.assertEqual(field_of(out, "session_key"), "dsh:sess-A")
        self.assertEqual(field_of(out, "display_name"), "demo-dev")
        conn = self._conn(root)
        try:
            self._assert_token_persisted(conn, token, "判据 A", evidence="machine")
            cid = conn.execute("SELECT citizen_id FROM session_tokens").fetchone()[0]
            self.assertEqual(field_of(out, "citizen_id"), cid,
                             "stdout 的 citizen_id 必须就是库里那条")
            citizen = conn.execute(
                "SELECT host_session_key,kind,status,last_seen_at,account_id "
                "FROM citizens WHERE citizen_id=?", (cid,)).fetchone()
            self.assertIsNotNone(citizen, "会话公民必须入库")
            self.assertEqual(citizen[0], "dsh:sess-A", "host_session_key 必须记下来（幂等的锚）")
            self.assertEqual((citizen[1], citizen[2]), ("visitor", "active"))
            self.assertIsNotNone(citizen[3], "last_seen_at 应有值")
            # 2026-10-25 契约变更（外部评估 `EVAL-261025-03`）：
            # **account_id 不再从 --alias 推断**。alias 是会话标签、account 是归属锚点，
            # 把前者当后者会静默建出第二个公民且无法合并。
            # 所以这里断言的是"**没有** account 时不编一个出来"——空着比编出来诚实。
            self.assertIsNone(citizen[4],
                              "account_id 不该从 --alias 推断（EVAL-261025-03 已修）")
            self._assert_one_open_stint(conn, cid, "DEMO", "判据 A")
            ev = conn.execute("SELECT payload FROM events WHERE type='session.opened'"
                              ).fetchone()
            self.assertIsNotNone(ev, "签发必须留一条 session.opened 事件（审计）")
            self.assertNotIn(token, ev[0], "事件表不可变——明文绝对不能写进去")
            self.assertIn(token_hash(token), ev[0], "事件里应留 hash 以便追溯")
        finally:
            conn.close()

    def test_A_mutation_token_not_persisted_goes_red(self):
        """变异自证：签发了但不写 session_tokens → 判据 A 必红。"""
        conn = self._conn(self._root("mut-a"))
        try:
            real = fish.issue_session_token

            def forgetful(conn_, **kwargs):
                issued = real(conn_, **kwargs)
                conn_.execute("DELETE FROM session_tokens")     # 等价于"根本没写"
                conn_.commit()
                return issued

            with mock.patch.object(fish, "issue_session_token", forgetful):
                res = fish.start_session(conn, environ={"DSH_SESSION_ID": "sess-AM"})
            self.assertTrue(res["token"], "变异没生效：token 还是签出来了")
            with self.assertRaises(AssertionError):
                self._assert_token_persisted(conn, res["token"], "变异：凭证未入库")
        finally:
            conn.close()

    # ------------------------------------------------------------ 判据 B

    def test_B_idempotent_same_env(self):
        """判据 B：同 env 连跑两次 → 同一个 citizen_id、公民只多 1 行、凭证轮换。"""
        root = self._root("b")
        env = clean_env(DSH_SESSION_ID="sess-B")
        token1, out1 = self._start(root, env, "--project", "P1")
        token2, out2 = self._start(root, env, "--project", "P1")
        self._assert_same_citizen(field_of(out1, "citizen_id"),
                                  field_of(out2, "citizen_id"), "判据 B")
        self.assertNotEqual(token1, token2, "每次签发都是新凭证（轮换）")
        self.assertEqual(field_of(out2, "citizen"), "复用")
        self.assertEqual(field_of(out2, "rotated"), "1", "重跑应轮换掉 1 条旧凭证")
        conn = self._conn(root)
        try:
            self.assertEqual(self._citizen_count(conn), 1, "公民表只该多 1 行")
            rows = conn.execute("SELECT token_hash,revoked_at FROM session_tokens "
                                "ORDER BY issued_at").fetchall()
            self.assertEqual(len(rows), 2, "轮换保留历史行（审计需要）")
            live = [r for r in rows if r[1] is None]
            self.assertEqual(len(live), 1, "任一时刻同一会话只有一条活凭证")
            self.assertEqual(live[0][0], token_hash(token2), "活的必须是新签的那条")
            self.assertIsNotNone(rows[0][1], "旧凭证必须被标记吊销")
            self.assertIsNone(fish.validate_session_token(conn, token1),
                              "旧凭证已轮换掉，校验必须失败")
            self.assertIsNotNone(fish.validate_session_token(conn, token2))
        finally:
            conn.close()

    def test_B_archived_citizen_still_found(self):
        """判据 B 的延伸：身份查询走 citizens_all——公民搬进归档表后，同一会话仍认得自己。

        守的是 17 §2.7 的"归档后 owner 不悬空"：若只查 citizens，归档一次就等于把正在
        干活的会话变成陌生人（而 P1-d 的归档任务不会问"有没有会话正在用它"）。
        """
        conn = self._conn(self._root("b-arc"))
        try:
            cols = ", ".join(fish._CITIZENS_ALL_COLUMNS)
            conn.execute(
                "INSERT INTO citizens (citizen_id,display_name,kind,host,status,created_at,"
                "host_session_key) VALUES (?,?,?,?,?,?,?)",
                ("C-ARC", "dsh-arc", "visitor", "dsh", "active", fish.now_utc(),
                 "dsh:arc-1"))
            conn.commit()
            conn.execute(f"INSERT INTO citizens_archive ({cols}) "
                         f"SELECT {cols} FROM citizens WHERE citizen_id='C-ARC'")
            conn.execute("DELETE FROM citizens WHERE citizen_id='C-ARC'")
            conn.commit()
            found = fish.find_session_citizen(conn, "dsh:arc-1")
            self.assertIsNotNone(found, "归档后的会话公民必须仍能被找到（走 citizens_all）")
            self.assertEqual(found["citizen_id"], "C-ARC")
            self.assertEqual(self._citizen_count(conn), 0, "前提：热表里已经没有它")
        finally:
            conn.close()

    def test_B_mutation_always_create_goes_red(self):
        """变异自证：每次 start 都新建公民（查不到已有会话）→ 判据 B 必红。"""
        conn = self._conn(self._root("mut-b"))
        try:
            env = {"DSH_SESSION_ID": "sess-BM"}
            first = fish.start_session(conn, environ=env)
            with mock.patch.object(fish, "find_session_citizen",
                                   lambda conn_, key: None):
                second = fish.start_session(conn, environ=env)
            self.assertNotEqual(first["citizen_id"], second["citizen_id"],
                                "变异没生效：还是复用了同一个公民")
            self.assertEqual(self._citizen_count(conn), 2, "变异后公民表多了 2 行")
            with self.assertRaises(AssertionError):
                self._assert_same_citizen(first["citizen_id"], second["citizen_id"],
                                          "变异：每次新建公民")
        finally:
            conn.close()

    # ------------------------------------------------------------ 判据 C

    def test_C_evidence_grading(self):
        """判据 C：有宿主 env → machine；只有 --alias → self-reported；都没有 → 匿名自报。"""
        root = self._root("c")
        # ① 宿主 env → machine（键带宿主前缀）
        _, out_m = self._start(root, clean_env(DSH_SESSION_ID="sess-C1"))
        self.assertEqual(field_of(out_m, "evidence"), "machine")
        self.assertEqual(field_of(out_m, "session_key"), "dsh:sess-C1")
        self.assertEqual(field_of(out_m, "host"), "dsh")
        # ② 只有 --alias → self-reported（键带 self: 前缀，且是幂等键）
        token_s, out_s = self._start(root, clean_env(), "--alias", "self-dev")
        self.assertEqual(field_of(out_s, "evidence"), "self-reported")
        self.assertEqual(field_of(out_s, "session_key"), "self:self-dev")
        _, out_s2 = self._start(root, clean_env(), "--alias", "self-dev")
        self.assertEqual(field_of(out_s2, "citizen_id"), field_of(out_s, "citizen_id"),
                         "自报级也要幂等：同一个别名重跑 → 同一个公民（18 §10）")
        # ③ 宿主配置的会话名（FISH_SESSION_ACTOR）也只是自报级
        _, out_a = self._start(root, clean_env(FISH_SESSION_ACTOR="wb-dev"))
        self.assertEqual(field_of(out_a, "evidence"), "self-reported")
        self.assertEqual(field_of(out_a, "session_key"), "self:wb-dev")
        # ④ 什么都没有 → 匿名自报，且**两次是不同的公民**（没有可比标识就不许假装认识）
        _, out_n1 = self._start(root, clean_env())
        _, out_n2 = self._start(root, clean_env())
        self.assertEqual(field_of(out_n1, "evidence"), "self-reported")
        self.assertTrue(field_of(out_n1, "session_key").startswith("anon:"))
        self.assertNotEqual(field_of(out_n1, "citizen_id"), field_of(out_n2, "citizen_id"),
                            "匿名会话之间没有标识 → 必须各自新建")
        conn = self._conn(root)
        try:
            grades = dict(conn.execute(
                "SELECT evidence,COUNT(*) FROM session_tokens GROUP BY evidence").fetchall())
            self.assertEqual(set(grades), {"machine", "self-reported"},
                             f"不该出现别的证据级别：{grades}")
            self.assertEqual(grades["machine"], 1, "只有带宿主 env 的那一次是 machine")
            self.assertTrue(token_s)
        finally:
            conn.close()

    def test_C_mutation_always_machine_goes_red(self):
        """变异自证：让"机器事实"永远成立（等价于永远签 machine）→ 判据 C 必红。"""
        conn = self._conn(self._root("mut-c"))
        try:
            fake = {"host": "dsh", "session_id": "forced", "session_key": "dsh:forced",
                    "evidence": "machine", "source": "forced"}
            with mock.patch.object(fish, "host_session_facts",
                                   lambda environ=None: dict(fake)):
                res = fish.start_session(conn, alias="self-dev", environ={})
            self.assertEqual(res["evidence"], "machine", "变异没生效：还是 self-reported")
            with self.assertRaises(AssertionError):
                self._assert_evidence(res, "self-reported", "变异：永远 machine")
        finally:
            conn.close()

    # ------------------------------------------------------------ 判据 D

    def test_D_multi_project_stints(self):
        """判据 D：多项目并行（2026-10-24 语义变更）——**切项目不关旧档期**。

        三段：
          D1 换项目 → 新开一条，**旧档期仍是开着的**（to_ts 保持 NULL、无 closed_reason）；
          D2 同一项目重跑 → reused，不多切一条；
          D3 两个项目的开档期同时存在（这正是旧版"先关后开"要禁止、现在允许的状态）。
        """
        root = self._root("d")
        env = clean_env(DSH_SESSION_ID="sess-D")
        _, out1 = self._start(root, env, "--project", "P-A")
        cid = field_of(out1, "citizen_id")
        self.assertEqual(field_of(out1, "stint"), "opened P-A")
        _, out2 = self._start(root, env, "--project", "P-B")
        self.assertEqual(field_of(out2, "stint"), "opened P-B")
        conn = self._conn(root)
        try:
            self._assert_open_projects(conn, cid, ["P-A", "P-B"], "判据 D（换项目）")
            rows = conn.execute(
                "SELECT project,from_ts,to_ts,closed_reason FROM project_stints "
                "WHERE citizen_id=? ORDER BY id", (cid,)).fetchall()
            self.assertEqual(len(rows), 2, "两个项目各一条档期（不是切换）")
            self.assertEqual([r[0] for r in rows], ["P-A", "P-B"])
            self.assertIsNone(rows[0][2], "切到新项目**不许**关掉旧项目的档期")
            self.assertIsNone(rows[0][3], "旧档期没关，就不该有关闭原因")
            self.assertIsNone(rows[1][2], "新档期必须是开着的")
            self.assertGreaterEqual(rows[1][1], rows[0][1], "新档期不能早于旧档期")
        finally:
            conn.close()
        # D2 同项目重跑：档期是"时间段"不是"打卡记录"，不许多切一条
        _, out3 = self._start(root, env, "--project", "P-B")
        self.assertEqual(field_of(out3, "stint"), "reused P-B",
                         "同项目重跑应是 reused（复用，不新开）")
        conn = self._conn(root)
        try:
            n = conn.execute("SELECT COUNT(*) FROM project_stints WHERE citizen_id=?",
                             (cid,)).fetchone()[0]
            self.assertEqual(n, 2, "同一项目重跑 start 不该再切一次档期")
            self._assert_open_projects(conn, cid, ["P-A", "P-B"], "判据 D（同项目重跑）")
        finally:
            conn.close()

    def test_D_mutation_legacy_index_goes_red(self):
        """变异自证：把库退回**旧的一列索引** → 多项目并行被拦 → 判据 D 必红。

        为什么换掉老的变异（"不关旧档期 → ux_stint_open 拦下"）：本轮把不变量本身改了，
        老的变异在新语义下**本就是正当行为**，它再也红不了（一条永远绿的变异等于没有）。
        新变异要守的是新边界：**索引必须是两列**。把索引换回一列版 = "迁移没做"，
        而这正是本轮最容易回归的地方：`CREATE UNIQUE INDEX IF NOT EXISTS` 见同名
        旧索引就跳过，旧索引会原地活下来（见 `_ensure_stint_open_index` 的说明）。

        刻意**不经过 CLI**：CLI 的 `session start` 会跑 migrate，而 migrate 会把一列
        索引改回两列——那样"旧库"造不出来。这里直接走 `fish.start_session`（不做迁移），
        模拟"库还没升级就有人开档期"的最坏顺序。
        """
        root = self._root("mut-d")
        conn = self._conn(root)                    # 先按新语义建好库与索引
        try:
            conn.execute("DROP INDEX ux_stint_open")
            conn.execute("CREATE UNIQUE INDEX ux_stint_open "
                         "ON project_stints(citizen_id) WHERE to_ts IS NULL")
            conn.commit()
            cols = tuple(r[2] for r in conn.execute("PRAGMA index_info(ux_stint_open)"))
            self.assertEqual(cols, ("citizen_id",), "夹具前提：库应处于旧的一列索引状态")
            fish.start_session(conn, projects=["P-A"], environ={"DSH_SESSION_ID": "sess-DM"})
            conn.commit()
            with self.assertRaises(sqlite3.IntegrityError,
                                   msg="一列索引下居然开出了第二个项目的档期"):
                fish.start_session(conn, projects=["P-B"],
                                   environ={"DSH_SESSION_ID": "sess-DM"})
            conn.rollback()
            open_n = conn.execute(
                "SELECT COUNT(*) FROM project_stints WHERE to_ts IS NULL").fetchone()[0]
            self.assertEqual(open_n, 1, "被拦下的那次不该留下半条档期")
        finally:
            conn.close()

    # ------------------------------------------------------------ 判据 E

    def test_E_agent_one_hop(self):
        """判据 E：父 machine → 子 inherited 成功；拿 inherited / self-reported 当父 → 被拒。"""
        root = self._root("e")
        env = clean_env(DSH_SESSION_ID="sess-E")
        parent, out_p = self._start(root, env)
        parent_cid = field_of(out_p, "citizen_id")
        rc, out_c, err_c = self._run(
            root, ["session", "agent", "--parent", parent, "--role", "reviewer"], env)
        self.assertEqual(rc, 0, f"父是 machine 时子 agent 应能签发：{err_c}")
        child = TOKEN_RE.search(out_c).group(1)
        self.assertEqual(field_of(out_c, "evidence"), "inherited")
        self.assertEqual(field_of(out_c, "session_key"), "dsh:sess-E#reviewer")
        conn = self._conn(root)
        try:
            row = conn.execute(
                "SELECT citizen_id,session_key,evidence,channel,parent_token "
                "FROM session_tokens WHERE token_hash=?", (token_hash(child),)).fetchone()
            self.assertIsNotNone(row, "子凭证必须入库")
            self.assertEqual(row[0], parent_cid, "子 agent 共享父公民（citizen_agents 还没建表）")
            self.assertEqual(row[1], "dsh:sess-E#reviewer", "子会话靠 #role 后缀区分")
            self.assertEqual((row[2], row[3]), ("inherited", "parent"))
            self.assertEqual(row[4], token_hash(parent), "parent_token 必须指向父的 hash")
            # 孙：拿 inherited 当父 → 必须被拒（"不能链式继承"）
            before = self._token_count(conn)
            with self.assertRaises(ValueError) as ctx:
                fish.issue_agent_token(conn, parent_token=child, role="sub")
            self.assertIn("链式", str(ctx.exception), "拒绝理由要能被读到（不是静默失败）")
            conn.rollback()
            self.assertEqual(self._token_count(conn), before, "被拒的签发不该留下行")
            # 自报级当父也不许背书：否则自报值会被抬成 inherited（证据升级）
            _, out_s = self._start(root, clean_env(), "--alias", "e-self")
            self_reported = TOKEN_RE.search(out_s).group(1)
            self._assert_agent_refused(conn, self_reported, "sub", "判据 E（自报级父）")
        finally:
            conn.close()
        # CLI 侧的孙也必须非零退出（AI 必须能看出"被拒了"）
        rc2, _, err2 = self._run(
            root, ["session", "agent", "--parent", child, "--role", "sub"], env)
        self.assertNotEqual(rc2, 0, "链式继承必须在 CLI 上失败")
        self.assertIn("链式", err2)

    def test_E_mutation_chain_allowed_goes_red(self):
        """变异自证：把"一跳守卫"摘掉（等价于允许链式）→ 判据 E 必红。"""
        conn = self._conn(self._root("mut-e"))
        try:
            res = fish.start_session(conn, environ={"DSH_SESSION_ID": "sess-EM"})
            child = fish.issue_agent_token(conn, parent_token=res["token"], role="reviewer")
            with mock.patch.object(fish, "assert_agent_parent_allowed",
                                   lambda parent: None):
                grand = fish.issue_agent_token(conn, parent_token=child["token"],
                                               role="sub")
                self.assertEqual(grand["evidence"], "inherited",
                                 "变异没生效：孙还是被拒了")
                # 同一组断言在变异下重跑：它必须报错，否则说明它没有判别力
                with self.assertRaises(AssertionError):
                    self._assert_agent_refused(conn, child["token"], "sub",
                                               "变异：允许链式继承")
        finally:
            conn.close()

    # ------------------------------------------------------------ 判据 F

    def test_F_end_revokes(self):
        """判据 F：end 后凭证吊销、校验失败、档期关闭、子凭证级联、重复调幂等。"""
        root = self._root("f")
        env = clean_env(DSH_SESSION_ID="sess-F")
        token, out = self._start(root, env, "--project", "PF")
        cid = field_of(out, "citizen_id")
        rc, out_c, err_c = self._run(
            root, ["session", "agent", "--parent", token, "--role", "qa"], env)
        self.assertEqual(rc, 0, err_c)
        child = TOKEN_RE.search(out_c).group(1)
        conn = self._conn(root)
        try:
            # 把 last_seen_at 推到过去：只有真的刷新过，下面的断言才有判别力
            conn.execute("UPDATE citizens SET last_seen_at='2020-01-01T00:00:00Z' "
                         "WHERE citizen_id=?", (cid,))
            conn.commit()
        finally:
            conn.close()
        rc, out_e, err_e = self._run(root, ["session", "end", "--token", token], env)
        self.assertEqual(rc, 0, f"session end 失败：{err_e}")
        self.assertNotIn(token, out_e, "end 的输出里不许回显 token 明文")
        conn = self._conn(root)
        try:
            self._assert_token_revoked(conn, token, "判据 F（父）")
            self._assert_token_revoked(conn, child, "判据 F（子凭证级联）")
            st = conn.execute("SELECT to_ts,closed_reason FROM project_stints "
                              "WHERE citizen_id=? ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
            self.assertIsNotNone(st[0], "end 必须关档期")
            self.assertEqual(st[1], "session_end", "关闭原因要写清楚")
            self.assertNotEqual(
                conn.execute("SELECT last_seen_at FROM citizens WHERE citizen_id=?",
                             (cid,)).fetchone()[0],
                "2020-01-01T00:00:00Z", "end 应刷新 last_seen_at")
        finally:
            conn.close()
        # status 仍要能说清"为什么不能用"（失效不等于失忆）
        rc, out_s, _ = self._run(root, ["session", "status", "--token", token], env)
        self.assertEqual(rc, 0)
        self.assertIn("已吊销", out_s, "status 必须报出失效原因")
        self.assertNotIn(token, out_s, "status 不许回显明文")
        # 幂等：重复 end 不报错、不再关档期
        rc2, out_e2, err_e2 = self._run(root, ["session", "end", "--token", token], env)
        self.assertEqual(rc2, 0, f"重复 end 必须幂等：{err_e2}")
        self.assertIn("本来已是吊销态", out_e2)

    def test_F_end_without_token_uses_env(self):
        """判据 F 的另一半：不带 --token 时按 env 定位会话，把该会话的活凭证全部吊销。

        这条路径不能省：AI 上下文被压缩后常常只记得"我在这个会话里"，记不得 token
        （18 §10 的坦白）。若 end 只认 --token，那种情况就变成"想收工却收不掉"。
        """
        root = self._root("f-env")
        env = clean_env(DSH_SESSION_ID="sess-F-env")
        # 会话还没开始：end 必须响亮失败，而不是静默成功
        rc0, _, err0 = self._run(root, ["session", "end"], env)
        self.assertEqual(rc0, 2, "没有登记过的会话调 end 应报错（退出码 2）")
        self.assertIn("没有登记过", err0)
        _, out = self._start(root, env, "--project", "PF-ENV")
        cid = field_of(out, "citizen_id")
        rc, out_e, err_e = self._run(root, ["session", "end"], env)
        self.assertEqual(rc, 0, f"end（按 env）失败：{err_e}")
        self.assertIn("已结束会话", out_e)
        conn = self._conn(root)
        try:
            live = conn.execute("SELECT COUNT(*) FROM session_tokens WHERE "
                                "session_key='dsh:sess-F-env' AND revoked_at IS NULL"
                                ).fetchone()[0]
            self.assertEqual(live, 0, "该会话的活凭证应全部吊销")
            st = conn.execute("SELECT to_ts FROM project_stints WHERE citizen_id=?",
                              (cid,)).fetchone()
            self.assertIsNotNone(st[0], "档期必须关闭")
        finally:
            conn.close()

    def test_F_mutation_no_revoke_goes_red(self):
        """变异自证：end 不真吊销（UPDATE 变空操作）→ 判据 F 必红。"""
        conn = self._conn(self._root("mut-f"))
        try:
            res = fish.start_session(conn, project="PF",
                                     environ={"DSH_SESSION_ID": "sess-FM"})
            with mock.patch.object(fish, "revoke_session_tokens", lambda *a, **k: 0):
                out = fish.end_session(conn, token=res["token"])
            self.assertIsNotNone(out["citizen_id"], "变异没生效：end 本身还是跑完了")
            with self.assertRaises(AssertionError):
                self._assert_token_revoked(conn, res["token"], "变异：不吊销")
        finally:
            conn.close()

    # ------------------------------------------------------------ 判据 G

    def test_G_token_never_written_to_disk(self):
        """判据 G：跑完 start 后，扫遍 root 与 cwd 的每个文件都找不到 token 明文。"""
        root = self._root("g")
        work = Path(self.tmp) / "g-cwd"
        work.mkdir()
        env = clean_env(DSH_SESSION_ID="sess-G")
        rc, out, err = self._run(root, ["session", "start", "--project", "PG"], env,
                                 cwd=work)
        self.assertEqual(rc, 0, err)
        m = TOKEN_RE.search(out)
        self.assertIsNotNone(m, "前提：token 必须真的被输出过（否则'找不到'是恒真）")
        token = m.group(1)
        self._assert_no_token_on_disk([root, work], token, "判据 G")
        # 连项目目录一起扫：token 落进手写的 .fish-token / 调试脚本里也算落盘。
        # 扫的是"本次这个随机 token"，所以不会误伤历史记录（每次都不同）。
        self._assert_no_token_on_disk([HERE], token, "判据 G（项目目录）")
        self.assertNotIn(token, err, "stderr 常被重定向进日志——token 不许出现在那里")
        conn = self._conn(root)
        try:
            payloads = "\n".join(r[0] for r in conn.execute("SELECT payload FROM events"))
            self.assertNotIn(token, payloads, "不可变的事件表里绝对不能有明文")
            self.assertIn(token_hash(token), payloads, "反证：事件里应留下 hash")
        finally:
            conn.close()
        # status / end 也不许把明文带到别处
        rc, out_s, err_s = self._run(root, ["session", "status", "--token", token], env,
                                     cwd=work)
        self.assertEqual(rc, 0)
        self.assertNotIn(token, out_s, "status 只该露 hash 前缀")
        self.assertNotIn(token, err_s)
        self._assert_no_token_on_disk([root, work], token, "判据 G（status 之后）")

    def test_G_mutation_leaked_token_goes_red(self):
        """变异自证：把一个含 token 的文件放进目录树 → 扫描器必须抓到 → 判据红。"""
        token = "LEAKED-TOKEN-do-not-use-1234567890"
        leak = Path(self.tmp) / "leak" / "fish-token"
        leak.parent.mkdir(parents=True, exist_ok=True)
        leak.write_text(f"token: {token}\n", encoding="utf-8")
        with self.assertRaises(AssertionError, msg="扫描器没抓到落盘的 token"):
            self._assert_no_token_on_disk([Path(self.tmp)], token, "变异：token 落盘")

    # ------------------------------------------------------------ 判据 H

    def test_H_no_mcp_session_tool(self):
        """判据 H：MCP 面没有 session（工具表 11 个、分发表没有），而 CLI 侧确实有。"""
        self._assert_tool_table_frozen("判据 H")
        self.assertNotIn("session", MCP_TOOL_NAMES)
        srv = mcp_server.FishServer(self._root("h"))
        try:
            self._assert_no_session_dispatch(srv, "判据 H")
        finally:
            srv.conn.close()
        # 反面证明：session 在 **CLI** 侧（否则"没注册"可能只是"根本没实现"）
        root = self._root("h-cli")
        rc, out, err = self._run(root, ["session", "status"], clean_env())
        self.assertEqual(rc, 0, f"CLI 侧应有 session 子命令：{err}")
        self.assertIn("会话身份", out)
        # 4 个子命令都必须能解析（不靠 help 文本猜）
        for action in ("start", "status", "end", "agent"):
            rc_a, _, err_a = self._run(root, ["session", action, "--help"], clean_env())
            self.assertEqual(rc_a, 0, f"`session {action}` 没注册：{err_a}")

    def test_H_mutation_registering_tool_goes_red(self):
        """变异自证：往 MCP 工具表里塞一个 session 工具 → 判据 H 必红。"""
        forged = {"name": "session", "title": "会话身份",
                  "description": "伪造的签发入口", "inputSchema":
                  {"type": "object", "properties": {"alias": {"type": "string"}}}}
        with mock.patch.object(mcp_server, "TOOLS", mcp_server.TOOLS + [forged]):
            self.assertEqual([t["name"] for t in mcp_server.TOOLS][-1], "session",
                             "变异没生效")
            with self.assertRaises(AssertionError):
                self._assert_tool_table_frozen("变异：注册了 session 工具")

    # ------------------------------------------------------------ 判据 I

    def test_I_session_identity_sql_frozen(self):
        """判据 I：P1-b 的 4 个会话公民读写点，SQL 逐条与本轮基线一致。

        P1-a 的判据 G 允许"声明过的新增函数"出现 citizens 引用；这里把"声明"落到实处——
        那 4 个函数的语句一旦被改（哪怕只是加一个 OR），本判据立刻红。
        """
        counts = {name: len(v) for name, v in SESSION_CITIZEN_SQL.items()}
        # 2026-10-25：4 → 5。多出来的是 `start_session`（`--citizen` 校验）。
        # 2026-10-26：5 → 6（`_session_labels_for`，会话维度的可读名）。
        # 两次都是**声明过的契约变更**（见 SESSION_CITIZEN_SQL 里的注释），
        # 不是放宽判据——原来那些函数的语句一字未改，仍是逐条冻结的。
        self.assertEqual(len(counts), 6, "基线自检：应为 6 个函数")
        self.assertEqual(EXPECTED_STMT_COUNT, 8, "基线自检：应为 8 条语句")
        got = {}
        for path in (FISH, FISH_PUSH):
            if path.exists():
                got.update(citizens_sql_by_function(path))
        for func, stmts in SESSION_CITIZEN_SQL.items():
            with self.subTest(func=func):
                self.assertIn(func, got,
                              f"模块树（fish.py/fish_push.py）:: {func} 不见了（被改名/删除？）")
                self.assertEqual(got[func], tuple(sorted(set(stmts))),
                                 f"{func} 的 citizens 语句被改过")
        # 反向：不许再多出**未声明**的会话公民引用函数（P1-a 判据 G 的同一条纪律）
        extra = {f for f in got if "session" in f or f in ("load_citizen", "touch_citizen")}
        self.assertEqual(extra, set(EXPECTED_NEW_FUNCS),
                         f"多出了未登记的会话公民读写点：{sorted(extra - EXPECTED_NEW_FUNCS)}")

    def test_I_mutation_editing_session_sql_goes_red(self):
        """变异自证：把会话查找里的 status='active' 去掉 → 判据 I 必红（在临时副本上改）。"""
        source = FISH.read_text(encoding="utf-8")
        marker = "WHERE host_session_key=? AND status='active' ORDER BY created_at LIMIT 1"
        self.assertIn(marker, source, "变异夹具失效：源码里找不到这句")
        broken = source.replace(marker, "WHERE host_session_key=? ORDER BY created_at LIMIT 1")
        copy = Path(self.tmp) / "fish_broken.py"
        copy.write_text(broken, encoding="utf-8")
        got = citizens_sql_by_function(copy)
        with self.assertRaises(AssertionError):
            self.assertEqual(got["find_session_citizen"],
                             tuple(sorted(set(
                                 SESSION_CITIZEN_SQL["find_session_citizen"]))),
                             "改过的语句不该与基线相等")


if __name__ == "__main__":
    unittest.main(verbosity=2)
