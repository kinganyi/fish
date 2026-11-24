#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话树 W3 · WorkBuddy 适配器验收（113 §4.3，2026-11-25）。

## 这一波判什么

W3 的承诺是"把 WB 的 transcript 投影成节点，**父从哪来必须说得清**"（113 §4.2）。
所以判据分两层：

  **A 层（可复现：临时树）** —— 四条来源优先级、冲突、孤儿、兄弟、无 transcript 成员、
      `session_id` 只取文件名。每条都配了能在源码上打靶的变异（`mutation_lineage_wb.py`）。
  **B 层（实况：真宿主只读）** —— 2,099 份 transcript 全量投影后，逐条锚"例外为 0"：
      父 id 全落在父目录内、31/31 在 `sessions` 表、`basis='default'`=0、
      `origin='main' AND agent_id LIKE 'agent-%'`=0、成员父=lead 且成员之间不互为父。
      **绝对数只打印、不锚死**（活宿主每跑一个子代理就 +1，113 §0.1 第 2 条）。

## 三层读数必须分开（不许"缺证据=通过"）

  · 绿：跑了、断言过了；
  · SKIP：本机没有 WB 树（B 层整类跳过，会打印原因）；
  · 判不到：数据库打不开 / runtime 读坏 → 对应判据**退到 path** 并把原因记在行上，
    绝不把"判不到"报成"没有"（`WbSessions.available` / `WbRuntime.failed` 就是为此存在）。

## 为什么 `origin='main'` 那条判据不是空断言

朴素写法（"所有行都是 subagent，所以 main 行数=0"）恒绿、没有判别力。B 层把
lead 会话经 `sessions` 表解析成 `origin='main'` 的**根节点**一起投影，"main 行"因此
真的存在过；判据是"这些 main 行的 `agent_id` 必须为 NULL"——把 `agent_id` 填成文件名
（变异 M4）立刻红。这就是 J3③ 的判别力来源。
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fish_lineage          # noqa: E402
import fish_lineage_wb as wb  # noqa: E402

#: 真宿主（只读）：B 层判据的对象。换机器/无 WB 时 B 层整类 SKIP，不是"通过"
REAL_ROOT = wb.default_root()

#: 合法 basis 集合 = 113 §2.3 DDL 的 CHECK 值域里本适配器会产出的四个。
#: 刻意不含 'default'：那是 W3 明令禁止的兜底（`fish_corpus_tag.py:240` 的反面）。
ALLOWED_BASES = frozenset({"runtime", "path", "missing", "db"})

#: transcript 首行里的 `sessionId`（实测形态：UUID，**不是** `agent-<uuid>`）
HEADER_SESSION_ID = "01a1e955-3697-7bcc-9556-efe6b8e0e695"


# ============================================================ 共用工具


def _new_projection_db(data_dir: Path):
    """一个空的投影库（`fish.SCHEMA` 建表）：判据要在**真表**上验，不在内存假表上验。"""
    import fish                     # 只在这里 import：本文件的顶层不碰业务模块

    data_dir.mkdir(parents=True, exist_ok=True)
    conn = fish.connect(data_dir)
    conn.executescript(fish.SCHEMA)
    return conn


def _project(conn, nodes) -> None:
    """把节点写进投影表（**一次事务**：半批落盘 = 树有洞，113 §2.4 的事务纪律）。"""
    for node in nodes:
        fish_lineage.upsert_node(conn, node)
    conn.commit()


def _scalar(conn, sql: str, params=()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


# ------------------------------------------------------------ 可复用判据（变异脚本共用）
def assert_basis_allowed(case: unittest.TestCase, nodes, where: str = "") -> None:
    """W3 的 `basis` 值域判据：只许四个来源，**`default` 不在其中**（113 §4.2）。

    测试与变异脚本共用这一份：分两份写必然各自漂开，而"basis 兜了个 default"
    正是这一波最容易被顺手加回来的东西（`fish_corpus_tag.py:240` 的前科）。
    """
    seen = {node["basis"] for node in nodes}
    case.assertEqual(sorted(seen - ALLOWED_BASES), [],
                     f"{where}basis 越界：{sorted(seen - ALLOWED_BASES)}")
    case.assertNotIn("default", seen, f"{where}出现了 default 兜底")


def assert_missing_rows_are_not_main(case: unittest.TestCase, node, where: str = "") -> None:
    """**判不到 ≠ main**：`missing` 行必须是 `unknown` + 无父（110 §6 J-D4 / 113 §4.2 ③）。

    这是 W3 最容易悄悄写错的一条：把兜底从 `main` 换成 `unknown` 不改变任何"看起来
    正常"的树（根节点照样渲染），只有这条判据看得出来。
    """
    case.assertIsNotNone(node, f"{where}节点不存在")
    case.assertEqual(node["origin"], "unknown", f"{where}判不到的行必须 origin=unknown")
    case.assertEqual(node["basis"], "missing", f"{where}判不到的行必须 basis=missing")
    case.assertIsNone(node["parent_session"], f"{where}判不到就不许编一个父")
    case.assertNotEqual(node["origin"], "main", f"{where}判不到的行绝不兜回 main")


def _read_only_sessions(root: Path) -> dict:
    """独立读一次 `sessions` 表（判据自带一把尺子，不复用被测代码的读取结果）。"""
    path = root / "workbuddy.db"
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
    try:
        return {str(r[0]): {"cwd": r[1], "updated_at": _iso_ms(r[2])}
                for r in conn.execute("SELECT id, cwd, updated_at FROM sessions")}
    finally:
        conn.close()


def _iso_ms(value):
    if value in (None, ""):
        return None
    num = float(value)
    if num > 1e11:
        num = num / 1000.0
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(num))


def _independent_transcripts(root: Path) -> dict:
    """**不经过适配器**地枚举 transcript：`{stem: 路径}`。

    为什么判据要自带这把尺子：拿被测代码的输出当参照，"行数=文件数"永远是 0=0 的
    同义反复。这里只做两件事——`<ws>/<父>/subagents/` 下扫 `agent-*.jsonl`、
    父 id 取**上一级目录名**；与适配器的 `path_parent()` 是两条独立的路径。
    """
    found: dict[str, Path] = {}
    projects = root / "projects"
    if not projects.is_dir():
        return found
    for ws in sorted(os.scandir(projects), key=lambda e: e.name):
        if not ws.is_dir():
            continue
        for parent in sorted(os.scandir(ws.path), key=lambda e: e.name):
            if not parent.is_dir():
                continue
            sub = os.path.join(parent.path, wb.SUBAGENTS_DIR)
            if not os.path.isdir(sub):
                continue
            for entry in os.scandir(sub):
                if entry.is_file() and wb.is_transcript_name(entry.name):
                    found[Path(entry.name).stem] = Path(entry.path)
    return found


def _independent_parents(found: dict) -> set:
    """父 id 集合，来自**独立**枚举（不调 `wb.path_parent`）。"""
    parents = set()
    for path in found.values():
        parents.add(path.parent.parent.name)
    return parents


# ============================================================ A 层：临时树


class _WbTree(unittest.TestCase):
    """一棵可复现的 WB 树（临时目录，绝不碰真宿主）。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="fish-w3-")
        self.root = Path(self._tmp) / "wb"
        (self.root / "projects").mkdir(parents=True)
        (self.root / "teams").mkdir()
        self.conn = _new_projection_db(Path(self._tmp) / "data")

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:       # noqa: BLE001 - 收尾不让异常盖住判据结果
            pass
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ------------------------------------------------------------ 造树
    def transcript(self, ws: str, parent: str, stem: str, *, in_subagents: bool = True,
                   cwd: str | None = "/work/wb", role: str | None = None,
                   session_id: str | None = HEADER_SESSION_ID,
                   mtime: float | None = None) -> Path:
        base = self.root / "projects" / ws / parent
        folder = (base / wb.SUBAGENTS_DIR) if in_subagents else base
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{stem}.jsonl"
        row = {"type": "message", "role": "user", "providerData": {"agent": role}}
        if session_id is not None:
            row["sessionId"] = session_id
        if cwd is not None:
            row["cwd"] = cwd
        path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def runtime(self, team: str, lead: str, members: list, *, updated_at: int = 1795000000000,
                raw: str | None = None) -> Path:
        folder = self.root / "teams" / team
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "runtime.json"
        if raw is not None:
            path.write_text(raw, encoding="utf-8")
            return path
        payload = {"version": 1, "teamName": team, "leadSessionId": lead,
                   "leadAgentId": f"team-lead@{team}", "updatedAt": updated_at,
                   "status": "active", "members": members}
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def sessions(self, ids, *, path: Path | None = None) -> Path:
        path = path or (self.root / "workbuddy.db")
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE IF NOT EXISTS sessions "
                     "(id TEXT PRIMARY KEY, cwd TEXT, updated_at INTEGER)")
        for sid in ids:
            conn.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?)",
                         (sid, "/work/wb", 1795000000000))
        conn.commit()
        conn.close()
        return path

    # ------------------------------------------------------------ 跑
    def scan(self, **kwargs):
        kwargs.setdefault("root", self.root)
        return wb.scan(**kwargs)

    def resolve(self, session_id, **kwargs):
        kwargs.setdefault("root", self.root)
        return wb.resolve(session_id, **kwargs)

    def nodes_by_id(self, **kwargs) -> dict:
        return {node["session_id"]: node for node in self.scan(**kwargs)}


class WbSourcePriorityTest(_WbTree):
    """113 §4.2 的四条来源优先级（runtime → path → missing；冲突以 runtime 为准）。"""

    def test_j3_basis_path_when_runtime_has_no_member(self):
        """只有路径：父 = 目录名，role 来自首行 `providerData.agent`，**不是** default。"""
        lead = "11111111-1111-4111-8111-111111111111"
        self.transcript("ws-a", lead, "agent-aaaa", role="software-engineer")
        self.runtime("team-a", lead, [])
        self.sessions([lead])
        nodes = self.nodes_by_id()
        node = nodes["agent-aaaa"]
        self.assertEqual(node["basis"], "path")
        self.assertEqual(node["source"], "wb:path")
        self.assertEqual(node["origin"], "subagent")
        self.assertEqual(node["relation"], "spawn")
        self.assertEqual(node["parent_session"], lead)
        self.assertEqual(node["depth"], 1)
        self.assertEqual(node["lineage_path"], f"/{lead}")
        self.assertEqual(node["agent_id"], "agent-aaaa")
        self.assertEqual(node["agent_role"], "software-engineer")
        self.assertEqual(node["cwd"], "/work/wb")
        self.assertEqual(node["extra"]["role_from"], "header")
        self.assertIn(node["basis"], ALLOWED_BASES)

    def test_j3_runtime_member_gets_lead_parent_and_team_relation(self):
        """runtime 成员：父=lead、relation=team_member、role=agentId 的 `@` 前缀。"""
        lead = "22222222-2222-4222-8222-222222222222"
        member = {"name": "schema-toolchain", "agentId": "schema-toolchain@team-b",
                  "taskDescription": "勘察 schema 工具链", "status": "idle",
                  "transcriptPath": str(self.root / "projects" / "ws-b" / lead
                                       / wb.SUBAGENTS_DIR / "agent-bbbb.jsonl")}
        self.transcript("ws-b", lead, "agent-bbbb", role="ignored-by-runtime")
        self.runtime("team-b", lead, [member])
        self.sessions([lead])
        node = self.nodes_by_id()["agent-bbbb"]
        self.assertEqual(node["basis"], "runtime")
        self.assertEqual(node["source"], "wb:runtime")
        self.assertEqual(node["relation"], "team_member")
        self.assertEqual(node["parent_session"], lead)
        self.assertEqual(node["agent_role"], "schema-toolchain")
        self.assertEqual(node["agent_label"], "勘察 schema 工具链")
        self.assertEqual(node["extra"]["team"], "team-b")
        self.assertEqual(node["extra"]["role_from"], "runtime")

    def test_j3_runtime_beats_path_on_conflict(self):
        """冲突：文件在 A 目录、runtime 说 lead 是 B → 父=B（runtime 赢），路径留证据。"""
        lead = "33333333-3333-4333-8333-333333333333"
        other = "44444444-4444-4444-8444-444444444444"
        member = {"agentId": "arch@team-c", "taskDescription": "架构",
                  "transcriptPath": str(self.root / "projects" / "ws-c" / other
                                       / wb.SUBAGENTS_DIR / "agent-cccc.jsonl")}
        self.transcript("ws-c", other, "agent-cccc")
        self.runtime("team-c", lead, [member])
        self.sessions([lead, other])
        node = self.nodes_by_id()["agent-cccc"]
        self.assertEqual(node["parent_session"], lead)
        self.assertEqual(node["basis"], "runtime")
        self.assertEqual(node["extra"]["path_parent"], other)
        self.assertEqual(node["extra"]["path_conflict"], 1)

    def test_j3_stray_transcript_is_missing_not_main(self):
        """挪出 `subagents/` 的 transcript → unknown/missing，**绝不**兜回 main。"""
        lead = "55555555-5555-4555-8555-555555555555"
        self.transcript("ws-d", lead, "agent-dddd", in_subagents=False)
        self.sessions([lead])
        nodes = self.nodes_by_id()
        node = nodes["agent-dddd"]
        assert_missing_rows_are_not_main(self, node, "【挪出 subagents/】")
        self.assertEqual(node["source"], "wb:missing")
        self.assertEqual(node["extra"]["reason"], "not_in_subagents")

    def test_j3_orphan_parent_not_in_sessions_is_missing(self):
        """路径父在 `sessions` 表里不存在 = 孤儿（J-D4）→ unknown/missing，不猜 main。"""
        self.transcript("ws-e", "ghost-parent", "agent-eeee")
        self.sessions([])
        node = self.nodes_by_id()["agent-eeee"]
        assert_missing_rows_are_not_main(self, node, "【孤儿父】")
        self.assertEqual(node["extra"]["unverified_parent"], "ghost-parent")
        self.assertEqual(node["extra"]["reason"], "parent_not_in_sessions")

    def test_j3_db_unavailable_keeps_path_basis(self):
        """DB 打不开 = 判不到，不是"没有"：path 父不被降级成孤儿。"""
        lead = "66666666-6666-4666-8666-666666666666"
        self.transcript("ws-f", lead, "agent-ffff")
        self.runtime("team-f", lead, [])
        # 不建库
        sessions = wb.load_sessions(self.root)
        self.assertFalse(sessions.available)
        self.assertNotEqual(sessions.notes, [], "判不到必须有原因，不能静默")
        node = self.nodes_by_id()["agent-ffff"]
        self.assertEqual(node["basis"], "path")
        self.assertEqual(node["parent_session"], lead)

    def test_j3_runtime_read_failure_falls_back_to_path(self):
        """runtime.json 读坏 → 退 path，行上记 `source='wb:runtime(miss)'`（退化③）。"""
        lead = "77777777-7777-4777-8777-777777777777"
        self.transcript("ws-g", lead, "agent-gggg")
        self.runtime("team-g", lead, [], raw="{ 这不是 JSON")
        self.sessions([lead])
        runtime = wb.load_runtime(self.root)
        self.assertEqual(len(runtime.failed), 1, "读坏要留痕")
        node = self.nodes_by_id(runtime=runtime)["agent-gggg"]
        self.assertEqual(node["basis"], "path")
        self.assertEqual(node["source"], "wb:runtime(miss)")


class WbSessionIdSourceTest(_WbTree):
    """J-W3-id：`session_id` 只能取文件名（113 §1-7 的护栏，742 行主键不许漂）。"""

    def test_j3_session_id_comes_from_filename_not_header(self):
        lead = "88888888-8888-4888-8888-888888888888"
        path = self.transcript("ws-h", lead, "agent-hhhh", session_id=HEADER_SESSION_ID)
        self.sessions([lead])
        node = self.nodes_by_id()["agent-hhhh"]
        self.assertEqual(node["session_id"], path.stem)
        self.assertNotEqual(node["session_id"], HEADER_SESSION_ID)
        self.assertEqual(node["extra"]["transcript_session_id"], HEADER_SESSION_ID)
        # 解析同一个会话时 id 也必须一致（resolve 与 scan 不许各自取 id）
        self.assertEqual(self.resolve("agent-hhhh")["session_id"], path.stem)

    def test_j3_primary_key_set_equals_transcript_stems(self):
        """主键集合 = 文件名集合（**不是**首行 sessionId 集合）。"""
        lead = "99999999-9999-4999-8999-999999999999"
        self.transcript("ws-i", lead, "agent-1111")
        self.transcript("ws-i", lead, "agent-2222")
        self.sessions([lead])
        stems = {p.stem for p in _independent_transcripts(self.root).values()}
        self.assertEqual({n["session_id"] for n in self.scan()}, stems)
        self.assertEqual(stems, {"agent-1111", "agent-2222"})


class WbMemberTest(_WbTree):
    """110 §7-1：WB 成员是 lead 的子、**成员之间互为兄弟**（不建成员→成员的边）。"""

    def _team(self):
        lead = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        members = []
        for index, role in enumerate(("arch", "engineer", "qa"), start=1):
            stem = f"agent-m{index}"
            members.append({
                "name": role, "agentId": f"{role}@team-m",
                "taskDescription": f"{role} 的任务", "status": "idle",
                "transcriptPath": str(self.root / "projects" / "ws-m" / lead
                                      / wb.SUBAGENTS_DIR / f"{stem}.jsonl")})
            self.transcript("ws-m", lead, stem)
        self.runtime("team-m", lead, members)
        self.sessions([lead])
        return lead

    def test_j3_members_are_siblings_under_lead(self):
        lead = self._team()
        nodes = self.scan()
        self.assertEqual({n["parent_session"] for n in nodes}, {lead},
                         "成员行必须全部挂在 lead 上")
        member_ids = {n["session_id"] for n in nodes}
        # 成员之间**没有**互为父：任何一个成员都不该是另一个成员的 parent_session
        self.assertEqual(member_ids & {n["parent_session"] for n in nodes}, set())
        # 投影进真表后同一判据在 SQL 上也成立（库级验证，不只信内存）
        _project(self.conn, nodes)
        self.assertEqual(_scalar(
            self.conn,
            "SELECT COUNT(*) FROM session_lineage WHERE host='workbuddy' "
            "AND parent_session IN (SELECT session_id FROM session_lineage "
            "WHERE host='workbuddy')"), 0)

    def test_j3_member_without_transcript_has_no_agent_id(self):
        """退化②：3 个无 transcriptPath 的成员 **不许**拿 `agentId` 标签顶上。"""
        lead = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        self.runtime("team-n", lead, [
            {"agentId": "wf-architect@team-n", "status": "unresumable"},
            {"agentId": "wf-engineer@team-n", "status": "unresumable",
             "resumeSessionId": "agent-cccccccccccccccc"},
        ])
        self.sessions([lead])
        runtime = wb.load_runtime(self.root)
        bare = {m["agent_key"]: m for m in runtime.members}
        self.assertTrue(bare["wf-architect@team-n"]["no_transcript"])
        self.assertIsNone(bare["wf-architect@team-n"]["agent_id"],
                          "无 transcriptPath 时 agent_id 必须是 NULL")
        self.assertFalse(str(bare["wf-architect@team-n"]["agent_id"] or "")
                         .endswith("@team-n"))
        # 连 resumeSessionId 都没有 → 没有会话键 → 不造行
        self.assertIsNone(self.resolve("wf-architect@team-n"))
        # 有 resumeSessionId 的 → 节点存在，但 agent_id 仍是 NULL（退化②的形状）
        node = self.resolve("wf-engineer@team-n")
        self.assertIsNotNone(node)
        self.assertEqual(node["basis"], "runtime")
        self.assertEqual(node["extra"]["no_transcript"], 1)
        self.assertIsNone(node["agent_id"])
        self.assertEqual(node["parent_session"], lead)
        self.assertEqual(self.scan(), [], "成员行不进 scan：scan 的行数 = 文件数")


class WbRootTest(_WbTree):
    """J3③ 的另一半：`origin='main'` 的根节点必须有，且 `agent_id` 恒 NULL。"""

    def _tree(self):
        lead = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        self.transcript("ws-o", lead, "agent-oooo")
        self.runtime("team-o", lead, [{
            "agentId": "eng@team-o", "taskDescription": "实现",
            "transcriptPath": str(self.root / "projects" / "ws-o" / lead
                                  / wb.SUBAGENTS_DIR / "agent-oooo.jsonl")}])
        self.sessions([lead])
        return lead

    def test_j3_lead_resolves_to_main_root(self):
        lead = self._tree()
        node = self.resolve(lead)
        self.assertIsNotNone(node)
        self.assertEqual(node["origin"], "main")
        self.assertEqual(node["basis"], "db")
        self.assertEqual(node["source"], "wb:db")
        self.assertIsNone(node["parent_session"])
        self.assertIsNone(node["agent_id"])
        self.assertEqual(node["depth"], 0)

    def test_j3_main_rows_never_carry_agent_ids(self):
        """`origin='main' AND agent_id LIKE 'agent-%'` = 0（**有 main 行**的对照组）。"""
        lead = self._tree()
        nodes = self.scan() + [self.resolve(lead)]
        bases = {n["basis"] for n in nodes}
        self.assertTrue(bases & {"runtime"}, "对照组：先得有 runtime 行")
        self.assertTrue(bases & {"db"}, "对照组：先得有 main 根行")
        assert_basis_allowed(self, nodes, "【根节点】")
        self.assertEqual([n["session_id"] for n in nodes if n["origin"] == "main"
                          and str(n["agent_id"] or "").startswith("agent-")], [])
        _project(self.conn, nodes)
        self.assertEqual(_scalar(
            self.conn, "SELECT COUNT(*) FROM session_lineage WHERE host='workbuddy' "
            "AND origin='main' AND agent_id LIKE 'agent-%'"), 0)
        self.assertEqual(_scalar(
            self.conn, "SELECT COUNT(*) FROM session_lineage WHERE host='workbuddy' "
            "AND basis='default'"), 0)

    def test_j3_resolve_returns_none_instead_of_faking_main(self):
        self._tree()
        self.assertIsNone(self.resolve("agent-does-not-exist"))
        self.assertIsNone(self.resolve("not-a-session-id"))
        self.assertIsNone(self.resolve(""))
        self.assertIsNone(wb.resolve_path(self.root / "projects" / "ws-o"
                                          / "no-such" / "subagents" / "agent-x.jsonl",
                                          runtime=wb.load_runtime(self.root)))


class WbScanOptionsTest(_WbTree):
    """`scan(since/limit)` 与适配器类（协议面）。"""

    def _tree(self):
        lead = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        base = 1_795_000_000.0
        self.transcript("ws-p", lead, "agent-old", mtime=base - 1000)
        self.transcript("ws-p", lead, "agent-mid", mtime=base - 500)
        self.transcript("ws-p", lead, "agent-new", mtime=base)
        self.sessions([lead])
        return lead

    def test_j3_scan_limit_takes_newest_after_sorting(self):
        self._tree()
        nodes = self.scan(limit=2)
        self.assertEqual([n["session_id"] for n in nodes], ["agent-new", "agent-mid"])

    def test_j3_scan_since_filters_by_observed_at(self):
        self._tree()
        cutoff = _iso_ms(1_795_000_000.0 - 500)
        nodes = self.scan(since=cutoff)
        self.assertEqual({n["session_id"] for n in nodes}, {"agent-mid", "agent-new"})

    def test_j3_adapter_implements_protocol_surface(self):
        lead = self._tree()
        adapter = wb.WbLineageAdapter(self.root)
        self.assertEqual(adapter.host, "workbuddy")
        self.assertEqual(sorted(n["session_id"] for n in adapter.scan()),
                         ["agent-mid", "agent-new", "agent-old"])
        self.assertEqual(adapter.resolve("agent-new")["basis"], "path")
        self.assertIsNone(adapter.resolve("agent-nope"))
        # refresh 的意义：runtime/sessions 是**这一轮**的真相，库里新增的行不该被缓存挡住
        late = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        self.sessions([late])
        self.assertIsNone(adapter.resolve(late), "缓存里没有这一行，先判不到")
        adapter.refresh()
        self.assertIsNotNone(adapter.resolve(late), "refresh 之后必须读到")
        self.assertEqual(adapter.resolve(late)["origin"], "main")


class WbLayeringTest(unittest.TestCase):
    """分层护栏：适配器顶层**不许** import fish 任何东西（113 §2.5 的叶子方向）。"""

    def test_no_fish_import_at_module_top(self):
        import ast

        tree = ast.parse((HERE / "fish_lineage_wb.py").read_text(encoding="utf-8"))
        top: set = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                top.add(node.module.split(".")[0])
        self.assertEqual(sorted(m for m in top if m.startswith("fish")), [],
                         "fish_lineage_wb 顶层 import 了 fish 模块（会成环）")
        self.assertIn("sqlite3", top, "只读 DB 是 stdlib，应当出现在顶层 import 里")
        # SessionNode 只能藏在 TYPE_CHECKING 里
        guarded = [n for n in tree.body if isinstance(n, ast.If)
                   and "TYPE_CHECKING" in ast.dump(n.test)]
        self.assertEqual(len(guarded), 1, "SessionNode 要用 TYPE_CHECKING 兜住")
        names = {alias.name for node in ast.walk(guarded[0]) if isinstance(node, ast.ImportFrom)
                 for alias in node.names}
        self.assertIn("SessionNode", names)


# ============================================================ B 层：真宿主（只读）


@unittest.skipUnless((REAL_ROOT / "projects").is_dir(),
                     f"本机没有 WorkBuddy 树（{REAL_ROOT}）：B 层判据跳过，不是通过")
class WbLiveTreeTest(unittest.TestCase):
    """实况投影：**例外为 0 + 失败集合为空**；绝对数只打印。"""

    @classmethod
    def setUpClass(cls):
        cls.runtime = wb.load_runtime(REAL_ROOT)
        cls.sessions = wb.load_sessions(REAL_ROOT)
        cls.files = _independent_transcripts(REAL_ROOT)
        cls.nodes = wb.scan(root=REAL_ROOT, runtime=cls.runtime, sessions=cls.sessions)
        cls.by_id = {node["session_id"]: node for node in cls.nodes}
        cls._tmp = tempfile.mkdtemp(prefix="fish-w3-live-")
        cls.conn = _new_projection_db(Path(cls._tmp) / "data")
        roots = [wb.db_root_node(lead, sessions=cls.sessions)
                 for lead in sorted({t["lead"] for t in cls.runtime.teams.values()
                                     if t["lead"]})]
        _project(cls.conn, cls.nodes + [node for node in roots if node is not None])

    @classmethod
    def tearDownClass(cls):
        try:
            cls.conn.close()
        except Exception:       # noqa: BLE001
            pass
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def test_j3_live_scan_covers_every_transcript(self):
        """行数 = 扫描到的 `agent-*.jsonl` 数（**实测 2,099，只打印不锚死**）。"""
        stems_before = set(self.files)
        missing = stems_before - set(self.by_id)
        after = set(_independent_transcripts(REAL_ROOT))
        unexpected = set(self.by_id) - (stems_before | after)
        print(f"\n[W3 实况] transcript 文件 = {len(self.files)}，投影行 = {len(self.nodes)}，"
              f"父目录 = {len(_independent_parents(self.files))}，"
              f"runtime team = {len(self.runtime.teams)} / 成员 = {len(self.runtime.members)}")
        self.assertEqual(missing, set(), "有文件没投影（扫描漏了）")
        self.assertEqual(unexpected, set(), "有行没有对应文件（凭空造行）")
        self.assertEqual(len(self.by_id), len(self.nodes), "主键重复")

    def test_j3_live_parents_are_real_session_dirs(self):
        """父 id 全落在父目录内；父目录 31/31 都在 `sessions` 表。"""
        merged = dict(self.files)
        merged.update(_independent_transcripts(REAL_ROOT))
        parents = _independent_parents(merged)
        table = _read_only_sessions(REAL_ROOT)
        projected = {n["parent_session"] for n in self.nodes}
        print(f"[W3 实况] 父目录 {len(parents)} 个，在 sessions 表 "
              f"{len({p for p in parents if p in table})}/{len(parents)}；"
              f"投影父 id 数 {len(projected)}")
        self.assertTrue(self.sessions.available, "读不到 sessions 表：判据退化成判不到")
        self.assertLessEqual(projected, parents, "投影出了不属于任何父目录的父")
        self.assertEqual(sorted(parents - table.keys()), [], "有父目录不在 sessions 表里")

    def test_j3_live_basis_and_origin_exceptions_are_zero(self):
        """J3③：`basis='default'`=0；`origin='main' AND agent_id LIKE 'agent-%'`=0。"""
        bases: dict[str, int] = {}
        for node in self.nodes:
            bases[node["basis"]] = bases.get(node["basis"], 0) + 1
        print(f"[W3 实况] basis 分布 = {bases}；origin 分布 = "
              f"{ {o: sum(1 for n in self.nodes if n['origin'] == o) for o in {n['origin'] for n in self.nodes}} }")
        assert_basis_allowed(self, self.nodes, "【实况】")
        offenders = [n["session_id"] for n in self.nodes
                     if n["origin"] == "main" and str(n["agent_id"] or "").startswith("agent-")]
        self.assertEqual(offenders, [])
        # 库级复核（判据在真表上也成立，而不只是在内存 dict 上）
        self.assertEqual(_scalar(self.conn, "SELECT COUNT(*) FROM session_lineage "
                               "WHERE host='workbuddy' AND basis='default'"), 0)
        self.assertEqual(_scalar(self.conn, "SELECT COUNT(*) FROM session_lineage "
                               "WHERE host='workbuddy' AND origin='main' "
                               "AND agent_id LIKE 'agent-%'"), 0)
        main_rows = _scalar(self.conn, "SELECT COUNT(*) FROM session_lineage "
                            "WHERE host='workbuddy' AND origin='main'")
        self.assertGreater(main_rows, 0, "对照组：main 根行必须真的存在过才谈得上'例外为 0'")

    def test_j3_live_runtime_members_hang_under_their_lead(self):
        """团队成员：父 = 该 team 的 `leadSessionId`；成员之间不互为父。"""
        member_ids = {m["agent_id"] for m in self.runtime.members if m["agent_id"]}
        leads = {t["lead"] for t in self.runtime.teams.values() if t["lead"]}
        wrong = [(node["session_id"], node["parent_session"]) for node in self.nodes
                 if node["basis"] == "runtime"
                 and node["parent_session"] not in leads]
        siblings = [node["session_id"] for node in self.nodes
                    if node["parent_session"] in member_ids]
        print(f"[W3 实况] 成员行 = {sum(1 for n in self.nodes if n['basis'] == 'runtime')}，"
              f"lead = {len(leads)}，父不是 lead 的 = {len(wrong)}，互为父的 = {len(siblings)}")
        self.assertEqual(wrong, [], "runtime 行的父必须是某个 team 的 leadSessionId")
        self.assertEqual(siblings, [], "成员之间不许互为父（110 §7-1）")
        # 每个 lead 都得在 sessions 表里（10/10 是 §0.3 的读数，只打印不锚死）
        table = _read_only_sessions(REAL_ROOT)
        absent = sorted(lead for lead in leads if lead not in table)
        self.assertEqual(absent, [], "leadSessionId 在 sessions 表里不存在")
        # runtime 声明的 transcript 不许落成 path（来源优先级被绕过就会出现）
        declared = {m["agent_id"] for m in self.runtime.members if m["agent_id"]}
        leaked = sorted(sid for sid, node in self.by_id.items()
                        if sid in declared and node["basis"] == "path")
        self.assertEqual(leaked, [], "runtime 声明过的 transcript 被判成 path")

    def test_j3_live_j3_1_literal_form_is_documented(self):
        """J3① 的**字面** SQL 在实况上不成立——把读数钉下来，免得下一个人当它绿。

        113 §4.3 写的是"`basis='path'` 且父同时出现在 `basis='runtime'` 行里的 = 0"。
        实况：lead 目录下有 1,419 份 transcript，其中只有 818 份在 `runtime.json` 的
        `members[]` 里——剩下 601 份是**更早的子代理**（runtime 只是当前快照）；
        其中 czhy-p5 那 30 份的 lead 没有任何带 transcript 的成员，所以字面式读数
        实测 **571**（不是 601）。按父聚合必然 >0；真正成立的不变式是**会话级**的：
        runtime 声明过的 transcript 一个都不许落成 path（上一条判据 `leaked == []`）。
        """
        runtime_parents = {n["parent_session"] for n in self.nodes if n["basis"] == "runtime"}
        literal = [n["session_id"] for n in self.nodes
                   if n["basis"] == "path" and n["parent_session"] in runtime_parents]
        print(f"[W3 实况] J3① 字面式（path 行的父也在 runtime 行里）= {len(literal)}"
              f"（预期 >0，因为 runtime 是快照不是全量）")
        self.assertGreater(len(literal), 0, "若为 0，说明取样点变了，重新核对 113 §4.3")
        self.assertEqual([n["session_id"] for n in self.nodes
                          if n["basis"] == "path" and n["session_id"] in
                          {m["agent_id"] for m in self.runtime.members if m["agent_id"]}], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
