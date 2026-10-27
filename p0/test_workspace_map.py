#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作区 → 项目映射判据（R1 根治，2026-10-24）。

背景
----
外部评估 EVAL-261024-02 的 🔴 R1：`inject` 的 L1「当前项目」是"最近更新者"猜测
（`build_injection_pack` 里仍是 `GROUP BY project ORDER BY MAX(recorded_at)`）。评估者
亲手复现：读完 EVAL 的两条记忆后，注入包立刻把 `EVAL` 报成当前项目，而实际在干的是
ANYI。

病根不是代码马虎，是**项目归属没有权威来源**。本轮的三个新东西：
  · `workspace_map` 表（路径前缀 → 项目）——环境事实，可维护、可审计；
  · `resolve_project_by_cwd`（最长前缀 + 路径分量边界）——解析规则，无匹配返回 None；
  · `build_injection_pack(project=..., cwd=...)` 四档证据链：
      显式参数 → 会话档期 → workspace_map → **标注过的**"最近更新"兜底；
      连兜底都没有时明说"未声明"。

判据与变异（每条判据都配一个"把实现改坏就必须红"的变异用例）
------------------------------------------------------------
A 精确匹配 + 归一化        | 变异：不归一化 → 尾斜杠/大小写不匹配 → 红
B 最长前缀优先             | 变异：取第一个匹配（`/a` 排在 `/a/b` 前）→ 红
C 路径分量边界             | 变异：改成裸 startswith → `/a/bc` 被误判 → 红
D 无匹配返回 None          | 变异：返回空串（假装"有归属"）→ 红
E L1 优先用会话档期        | 变异：忽略档期 → 退回最近更新 → 显示企业智能 → 红
F 兜底必须标注"推断"       | 变异：去掉标注文字 → 红
G 显式参数最高优先         | 变异：档期压过显式参数 → 红
H 多档期列出"其他项目"     | 变异：只返回主项目 → 红
I workspace_map 参与解析   | 变异：解析时跳过 workspace_map → 红
J 无任何证据 → 明说未声明  | 变异：换成"未知/猜一个"的措辞 → 红
K 旧库升级走 migrate()     | 变异：migrate 不建表 → 红
L 映射 CRUD（声明式覆盖）  | 覆盖性判据（无变异）：set/list/remove 的手脚必须是通的
M MCP inject 透传 project  | 覆盖性判据（无变异）：工具面与实现面的接线
N seed 只读 + 冲突分级     | 覆盖性判据（无变异）：只建议不写库、冲突带原因
N2 seed 共享父目录/降噪    | 覆盖性判据（无变异）：父目录不整片归属、子路径不重复列

为什么 A–K 要"变异自证"：判据只证明"现在是对的"，变异才证明"它真的在测这件事"。
每条变异都作用在**被测实现**上（替换一个函数/常量），不是改测试数据——改数据只能
证明测试会随数据变，证明不了实现被破坏时会红。

运行：`python -m unittest test_workspace_map -v`
"""
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

# 工作区夹具的父目录：**刻意不用 tempfile 默认目录（AppData\Local\Temp）**。
# 原因：seed 的降噪规则会把 `/appdata/` 整片跳过（那里是工具状态，不是工作区），
# 用 AppData 里的目录做夹具，候选会被规则正确地过滤掉，判据就永远看不到建议。
# 放在工作区根（learn）下与项目里既有的 `.tmp-*` 目录习惯一致，tearDown 会删干净。
WS_FIXTURE_BASE = HERE.parents[1]


class WorkspaceMapTest(unittest.TestCase):
    """R1：项目归属从"猜"变成"查"的 11 条判据（各带变异）+ 4 条接线判据（L/M/N/N2）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fish-wsmap-")
        self.root = Path(self.tmp) / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.cid = "01WSMAP000000000000000001"
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (self.cid, "映射测试会话", "visitor", "engineer", "dsh", "active",
             fish.now_utc()))
        self.conn.commit()
        self._extra_dirs = []

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        for d in self._extra_dirs:
            shutil.rmtree(d, ignore_errors=True)

    # ------------------------------------------------------------ 夹具

    def _map(self, path, project, note=None):
        """走生产写入口（set_workspace_mapping），不手插 SQL。"""
        return fish.set_workspace_mapping(self.conn, path, project, note=note)

    def _stint(self, project):
        """直接插档期：夹具不该依赖被测的"开档期逻辑"（那是 test_stint_multi 的事）。"""
        self.conn.execute(
            "INSERT INTO project_stints (citizen_id,project,from_ts) VALUES (?,?,?)",
            (self.cid, project, fish.now_utc()))
        self.conn.commit()

    def _mem(self, code, project, recorded_at="2026-10-01T00:00:00Z", status="active"):
        self.conn.execute(
            "INSERT INTO code_index (code,title,project,owner,mem_type,visibility,status,"
            "tags,file_path,occurred_at,recorded_at,access_count,dormant) "
            "VALUES (?,?,?,?,?,'shared',?,'[]',NULL,?,?,0,0)",
            (code, f"{project} 的条目", project, self.cid, "fact", status,
             recorded_at, recorded_at))
        self.conn.commit()

    def _registry(self, prefix):
        self.conn.execute(
            "INSERT OR IGNORE INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES (?,?,?,?,?)", (prefix, prefix, "", 0, "active"))
        self.conn.commit()

    def _ws_dir(self, name):
        """造一个**真实存在**的工作区目录（seed 只建议真实存在的目录）。"""
        base = Path(tempfile.mkdtemp(prefix="fish-wsmap-ws-", dir=str(WS_FIXTURE_BASE)))
        self._extra_dirs.append(base)
        d = base / name
        d.mkdir(parents=True)
        return d

    def _expect_red(self, check, expected, where):
        """跑一次判据断言，要求它**因为预期的原因**变红。

        为什么不是裸 `assertRaises(AssertionError)`：任何断言失败都会让它通过，包括
        "夹具写错了"这种假红。要求失败消息里出现预期片段，才证明变异打在判据的要害上；
        变异**不红**则直接 fail——那说明这条判据没在测它声称要测的东西。
        """
        try:
            check()
        except AssertionError as e:
            msg = str(e)
            self.assertIn(expected, msg,
                          f"{where}：变异红了，但不是预期的断言（实际失败：{msg}）")
        else:
            self.fail(f"{where}：变异没有让判据变红——判据没在测它声称要测的东西")

    # ============================================================ A
    # 精确匹配 + 归一化：正斜杠/反斜杠、尾斜杠、大小写都必须落到同一条映射上。

    def _check_A(self):
        stored = self._map("C:\\Work\\Alpha", "ANYI")
        self.assertEqual(stored["path_prefix"], "c:/work/alpha",
                         "写入口必须把路径归一成小写/正斜杠/无尾斜杠")
        self.assertEqual(fish.resolve_project_by_cwd(self.conn, "C:/Work/Alpha"), "ANYI")
        self.assertEqual(fish.resolve_project_by_cwd(self.conn, "c:/work/alpha/"), "ANYI")
        self.assertEqual(fish.resolve_project_by_cwd(self.conn, "C:\\Work\\Alpha\\"), "ANYI")

    def test_A_exact_match_with_normalization(self):
        self._check_A()

    def test_M_A_no_normalization_goes_red(self):
        """变异：不归一化（只 strip）→ 尾斜杠/大小写不匹配 → 判据 A 必红。"""
        def raw(path):
            return "" if path is None else str(path).strip()

        with mock.patch.object(fish, "normalize_workspace_path", raw):
            self._expect_red(self._check_A, "写入口必须把路径归一成", "A")

    # ============================================================ B
    # 最长前缀：`/a` 与 `/a/b` 同时存在时，`/a/b/c` 属于更具体的 `/a/b`。

    def _check_B(self):
        self._map("/a", "P-A")
        self._map("/a/b", "P-B")
        self.assertEqual(fish.resolve_project_by_cwd(self.conn, "/a/b/c"), "P-B",
                         "必须选最长前缀（父子目录可以各有归属）")
        self.assertEqual(fish.resolve_project_by_cwd(self.conn, "/a/x"), "P-A")

    def test_B_longest_prefix_wins(self):
        self._check_B()

    def test_M_B_first_match_goes_red(self):
        """变异：取查询顺序里第一个匹配（`/a`）→ 判据 B 必红。"""
        def first_match(conn, cwd):
            target = fish.normalize_workspace_path(cwd)
            for prefix, project in conn.execute(
                    "SELECT path_prefix, project FROM workspace_map ORDER BY path_prefix"):
                if target == prefix or target.startswith(prefix + "/"):
                    return project
            return None

        with mock.patch.object(fish, "resolve_project_by_cwd", first_match):
            self._expect_red(self._check_B, "必须选最长前缀", "B")

    # ============================================================ C
    # 路径分量边界：`/a/b` 不覆盖 `/a/bc`（字符串前缀 ≠ 父路径）。

    def _check_C(self):
        self._map("/a/b", "P-A")
        self.assertIsNone(fish.resolve_project_by_cwd(self.conn, "/a/bc"),
                          "`/a/bc` 不是 `/a/b` 的子路径——裸 startswith 会误匹配")
        self.assertIsNone(fish.resolve_project_by_cwd(self.conn, "/a/bc/d"))
        self.assertEqual(fish.resolve_project_by_cwd(self.conn, "/a/b/child"), "P-A",
                         "反例自证：夹具本身是通的（同一映射在真子路径上能命中）")

    def test_C_component_boundary(self):
        self._check_C()

    def test_M_C_plain_startswith_goes_red(self):
        """变异：用裸 startswith（不看分量边界）→ `/a/bc` 被误判 → 判据 C 必红。"""
        def naive(conn, cwd):
            target = fish.normalize_workspace_path(cwd)
            best = None
            for prefix, project in conn.execute(
                    "SELECT path_prefix, project FROM workspace_map"):
                if target.startswith(prefix):
                    if best is None or len(prefix) > best[0]:
                        best = (len(prefix), project)
            return best[1] if best else None

        with mock.patch.object(fish, "resolve_project_by_cwd", naive):
            self._expect_red(self._check_C, "不是 `/a/b` 的子路径", "C")

    # ============================================================ D
    # 无匹配 → None（不是空串、不是猜一个）。R1 的核心纪律。

    def _check_D(self):
        self._map("/a/b", "P-A")
        self.assertIsNone(fish.resolve_project_by_cwd(self.conn, "/zzz"),
                          "没有匹配必须返回 None——猜一个比不显示危险得多")
        self.assertIsNone(fish.resolve_project_by_cwd(self.conn, "/"),
                          "根路径不匹配任何具体前缀")
        self.assertIsNone(fish.resolve_project_by_cwd(self.conn, ""),
                          "空路径不是「未归属的某个目录」，它就是没有输入")
        self.assertIsNone(fish.resolve_project_by_cwd(self.conn, None))

    def test_D_no_match_returns_none(self):
        self._check_D()

    def test_M_D_empty_string_fallback_goes_red(self):
        """变异：无匹配时返回空串（上层会把它当"有归属但名字是空"）→ 判据 D 必红。"""
        def empty_fallback(conn, cwd):
            real = fish.resolve_project_by_cwd
            # 这里不递归：直接按"没匹配就给空串"的坏实现重写一遍最短逻辑
            target = fish.normalize_workspace_path(cwd)
            for prefix, project in conn.execute(
                    "SELECT path_prefix, project FROM workspace_map"):
                if target == prefix or target.startswith(prefix + "/"):
                    return project
            return ""

        with mock.patch.object(fish, "resolve_project_by_cwd", empty_fallback):
            self._expect_red(self._check_D, "没有匹配必须返回 None", "D")

    # ============================================================ E
    # L1 优先用会话档期：档期说 ANYI，而最近更新在企业智能 → 必须显示 ANYI。

    def _seed_two_projects(self):
        self._stint("ANYI")
        self._mem("ANYI-261024-01", "ANYI", "2026-10-01T00:00:00Z")
        self._mem("ANYI-261024-02", "ANYI", "2026-10-01T00:00:00Z")
        for i in range(1, 4):
            self._mem(f"QIYE-261023-0{i}", "企业智能", "2026-10-23T00:00:00Z")

    def _check_E(self):
        self._seed_two_projects()
        pack, meta = fish.build_injection_pack(self.conn, self.root, self.cid)
        self.assertEqual(meta["project"], "ANYI",
                         "档期是会话事实，优先于「谁最近更新」")
        self.assertEqual(meta["project_source"], "stint")
        self.assertIn("当前项目：ANYI", pack)
        self.assertNotIn("当前项目：企业智能", pack,
                         "旧实现（MAX(recorded_at)）会在这里显示企业智能")
        self.assertNotIn("据最近更新推断", pack, "有档期时不该出现推断标注")

    def test_E_l1_uses_stint_not_recent_update(self):
        self._check_E()

    def test_M_E_ignoring_stints_goes_red(self):
        """变异：解析时忽略档期（等价于旧实现：只看最近更新）→ 判据 E 必红。"""
        with mock.patch.object(fish, "current_open_stints", lambda conn, cid: []):
            self._expect_red(self._check_E, "档期是会话事实", "E")

    # ============================================================ F
    # 兜底必须标注"据最近更新推断"，不能让用户以为那是声明过的项目。

    def _check_F(self):
        self._mem("TESTP-261024-01", "TESTP", "2026-10-01T00:00:00Z")
        pack, meta = fish.build_injection_pack(self.conn, self.root, self.cid)
        self.assertEqual(meta["project_source"], "inferred")
        self.assertIn("据最近更新推断", pack, "兜底必须自曝是猜的")
        self.assertIn("fish session start --project", pack,
                      "标注要给出下一步动作（怎么声明才有权威）")
        self.assertIn("当前项目：TESTP", pack, "老判据依赖的项目行仍要在（可见地）存在")

    def test_F_fallback_is_labeled(self):
        self._check_F()

    def test_M_F_dropping_hint_goes_red(self):
        """变异：去掉推断标注（把兜底当成事实输出）→ 判据 F 必红。"""
        with mock.patch.object(fish, "_INFERRED_PROJECT_NOTE", ""):
            self._expect_red(self._check_F, "兜底必须自曝是猜的", "F")

    # ============================================================ G
    # 显式参数最高优先：传 project="CDQL"、档期是 ANYI → 必须是 CDQL。

    def _check_G(self):
        self._stint("ANYI")
        self._mem("ANYI-261024-01", "ANYI", "2026-10-01T00:00:00Z")
        self._mem("CDQL-261024-01", "CDQL", "2026-10-02T00:00:00Z")
        pack, meta = fish.build_injection_pack(self.conn, self.root, self.cid,
                                               project="CDQL")
        self.assertEqual(meta["project"], "CDQL", "显式参数压过档期")
        self.assertEqual(meta["project_source"], "explicit")
        self.assertIn("当前项目：CDQL", pack)
        self.assertNotIn("当前项目：ANYI", pack)

    def test_G_explicit_project_wins(self):
        self._check_G()

    def test_M_G_stint_overriding_explicit_goes_red(self):
        """变异：把档期放到显式参数前面 → 判据 G 必红。"""
        def stint_first(conn, citizen_id, project=None, cwd=None):
            stints = fish.current_open_stints(conn, citizen_id)
            if stints:
                return (stints[-1]["project"], "stint",
                        [s["project"] for s in stints[:-1]])
            if project:
                return project.strip(), "explicit", []
            return None, "undeclared", []

        with mock.patch.object(fish, "_resolve_injection_project", stint_first):
            self._expect_red(self._check_G, "显式参数压过档期", "G")

    # ============================================================ H
    # 多档期：主项目之外的那些要在 L1 里列成"同时在其他项目"。

    def _check_H(self):
        self._stint("ANYI")
        self._stint("企业智能")
        self._mem("ANYI-261024-01", "ANYI", "2026-10-01T00:00:00Z")
        self._mem("QIYE-261023-01", "企业智能", "2026-10-23T00:00:00Z")
        pack, meta = fish.build_injection_pack(self.conn, self.root, self.cid)
        self.assertEqual(meta["project"], "企业智能", "主项目 = 最近开的那个")
        self.assertEqual(meta["other_projects"], ["ANYI"],
                         "主项目之外的项目必须列出（多项目并行是合法状态）")
        self.assertIn("同时在其他项目：ANYI", pack,
                      "多项目并行是合法状态，必须让 AI 知道自己在同时服务哪些项目")

    def test_H_multi_stints_list_others(self):
        self._check_H()

    def test_M_H_dropping_others_goes_red(self):
        """变异：只返回主项目、不列其他项目 → 判据 H 必红。"""
        real = fish._resolve_injection_project

        def only_primary(conn, citizen_id, project=None, cwd=None):
            name, source, _others = real(conn, citizen_id, project=project, cwd=cwd)
            return name, source, []

        with mock.patch.object(fish, "_resolve_injection_project", only_primary):
            self._expect_red(self._check_H, "主项目之外的项目必须列出", "H")

    # ============================================================ I
    # workspace_map 参与解析（第三档）：调用方能给 cwd 时，路径归属就是证据。

    def _check_I(self):
        self._map("I:/app/anyi-studio", "ANYI")
        self._mem("ANYI-261024-01", "ANYI", "2026-10-01T00:00:00Z")
        self._mem("QIYE-261023-01", "企业智能", "2026-10-23T00:00:00Z")
        pack, meta = fish.build_injection_pack(
            self.conn, self.root, self.cid, cwd="I:/app/anyi-studio/sub/dir")
        self.assertEqual(meta["project"], "ANYI", "cwd 命中映射时，不再看谁最近更新")
        self.assertEqual(meta["project_source"], "workspace_map")
        self.assertIn("当前项目：ANYI", pack)
        self.assertNotIn("据最近更新推断", pack, "有映射证据时不该标注为推断")

    def test_I_workspace_map_used_by_cwd(self):
        self._check_I()

    def test_M_I_skipping_workspace_map_goes_red(self):
        """变异：解析时跳过 workspace_map → 退回最近更新（企业智能）→ 判据 I 必红。"""
        with mock.patch.object(fish, "resolve_project_by_cwd", lambda conn, cwd: None):
            self._expect_red(self._check_I, "cwd 命中映射时", "I")

    # ============================================================ J
    # 没有任何证据（空库）→ 明说"未声明"，不许编一个项目名。

    def _check_J(self):
        pack, meta = fish.build_injection_pack(self.conn, self.root, self.cid)
        self.assertIsNone(meta["project"])
        self.assertEqual(meta["project_source"], "undeclared")
        self.assertIn("未声明", pack, "没有证据时必须明说，而不是给出一个看起来像事实的名字")
        self.assertNotIn("当前项目：TESTP", pack)

    def test_J_undeclared_is_explicit(self):
        self._check_J()

    def test_M_J_vague_wording_goes_red(self):
        """变异：把"未声明"换成模糊措辞（"未知"）→ 判据 J 必红（措辞就是判据）。"""
        with mock.patch.object(fish, "_UNDECLARED_PROJECT_LINE", "当前项目：未知"):
            self._expect_red(self._check_J, "没有证据时必须明说", "J")

    # ============================================================ K
    # 旧库升级：migrate() 必须能把缺失的 workspace_map 建回来（幂等）。

    def _check_K(self):
        self.conn.execute("DROP TABLE workspace_map")
        self.conn.commit()
        fish.migrate(self.conn)
        info = [tuple(r) for r in self.conn.execute("PRAGMA table_info(workspace_map)")]
        self.assertEqual([r[1] for r in info],
                         ["path_prefix", "project", "note", "created_at"],
                         "migrate 必须把旧库缺的 workspace_map 建回来")
        self.assertEqual(info[0][5], 1, "path_prefix 必须是主键（否则同一路径能有两条映射）")
        fish.migrate(self.conn)          # 幂等：重复升级不报错、结构不变
        again = [tuple(r) for r in self.conn.execute("PRAGMA table_info(workspace_map)")]
        self.assertEqual(again, info, "重复 migrate 不该改结构")

    def test_K_legacy_db_gains_workspace_map(self):
        self._check_K()

    def test_M_K_migrate_not_creating_table_goes_red(self):
        """变异：migrate 不建表（只靠 SCHEMA）→ 旧库缺表补不回来 → 判据 K 必红。"""
        with mock.patch.object(fish, "_ensure_workspace_table", lambda conn: None):
            self._expect_red(self._check_K,
                             "migrate 必须把旧库缺的 workspace_map 建回来", "K")

    # ============================================================ L
    # 映射 CRUD（CLI 的手脚）：set 幂等覆盖 / list / remove 幂等。

    def test_L_mapping_crud(self):
        first = self._map("I:/App/Team/", "ANYI", note="主代码库")
        self.assertTrue(first["created"])
        self.assertEqual(first["path_prefix"], "i:/app/team")
        second = self._map("i:\\app\\team", "企业智能")
        self.assertFalse(second["created"], "同一路径第二次 set 是更新，不是新增")
        rows = fish.list_workspace_mappings(self.conn)
        self.assertEqual(len(rows), 1, "同一路径只允许一条映射（主键）")
        self.assertEqual(rows[0]["project"], "企业智能")
        self.assertTrue(fish.remove_workspace_mapping(self.conn, "I:/APP/TEAM/"))
        self.assertFalse(fish.remove_workspace_mapping(self.conn, "i:/app/team"),
                         "删除必须幂等（不存在 → False，不抛异常）")
        self.assertEqual(fish.list_workspace_mappings(self.conn), [])

    # ============================================================ M
    # 契约/接线：MCP inject 的 project 入参真的传到实现层（不是只写在 schema 里）。

    def test_M_mcp_inject_passes_project_through(self):
        self._mem("ANYI-261024-01", "ANYI", "2026-10-01T00:00:00Z")
        self._mem("QIYE-261023-01", "企业智能", "2026-10-23T00:00:00Z")
        props = next(t for t in mcp_server.TOOLS if t["name"] == "inject")[
            "inputSchema"]["properties"]
        self.assertIn("project", props, "inject 必须有可选的 project 入参")
        srv = mcp_server.FishServer(self.root)
        try:
            text = srv.call_tool("inject", {"project": "ANYI"})
        finally:
            srv.conn.close()
        self.assertIn("当前项目：ANYI", text,
                      "MCP 层必须把 project 透传给 build_injection_pack（不能只加 schema）")
        self.assertNotIn("据最近更新推断", text)

    # ============================================================ N
    # seed：从正文反推候选，但**绝不写库**；冲突路径不给建议。

    def test_N_seed_suggests_but_never_writes(self):
        self._registry("ANYI")
        self._registry("企业智能")
        ws = self._ws_dir("anyi-studio")
        shown = str(ws).replace("\\", "/")
        fish.record_memory(self.conn, self.root, project="ANYI", title="工作区线索",
                           body=f"主代码库在 {shown} 下，另见 I:/nonexistent/ghost-dir。")

        res = fish.workspace_seed_candidates(self.conn, self.root)
        paths = [it["path"] for it in res["suggest"]]
        self.assertIn(fish.normalize_workspace_path(shown), paths,
                      "正文里真实存在的工作区目录应被建议")
        self.assertNotIn("i:/nonexistent/ghost-dir", paths,
                         "不存在的目录不该被建议（写进映射只会留下永远匹配不上的幽灵行）")
        self.assertEqual(fish.list_workspace_mappings(self.conn), [],
                         "seed 是只读建议器：**一行都不许写**")

        # 记忆根目录内部是数据仓库，不是工作区：正文提到它也不该被建议
        fish.record_memory(self.conn, self.root, project="ANYI", title="记忆根自身",
                           body=f"索引在 {str(self.root).replace(chr(92), '/')}/projects 下。")
        res_root = fish.workspace_seed_candidates(self.conn, self.root)
        self.assertNotIn(fish.normalize_workspace_path(str(self.root / "projects")),
                         [it["path"] for it in res_root["suggest"]],
                         "记忆根目录内部的路径不是工作区（否则建议表会被数据路径污染）")

        # 同一路径被第二个项目提到 → 冲突，不再给建议
        fish.record_memory(self.conn, self.root, project="企业智能", title="同一目录的另一种说法",
                           body=f"这个目录也是我们项目的：{shown}")
        res2 = fish.workspace_seed_candidates(self.conn, self.root)
        conflict_paths = [it["path"] for it in res2["conflicts"]]
        self.assertIn(fish.normalize_workspace_path(shown), conflict_paths,
                      "同一路径被多个项目提到时必须标冲突，交给人判断")
        self.assertNotIn(fish.normalize_workspace_path(shown),
                         [it["path"] for it in res2["suggest"]])
        self.assertEqual([it["reason"] for it in res2["conflicts"]
                          if it["path"] == fish.normalize_workspace_path(shown)],
                         ["multi_project"])

    def test_N2_seed_marks_shared_parent_and_prunes_children(self):
        """共享父目录不能整片归属；父子都提到时只留父路径。"""
        self._registry("ANYI")
        self._registry("企业智能")
        shared = self._ws_dir("shared-root")
        anyi_dir = shared / "anyi"
        qiye_dir = shared / "qiye"
        deep = anyi_dir / "sub" / "deep"
        for d in (anyi_dir, qiye_dir, deep):
            d.mkdir(parents=True, exist_ok=True)
        fish.record_memory(self.conn, self.root, project="ANYI", title="ANYI 的两个层级",
                           body=f"根 {str(shared).replace(chr(92), '/')}，"
                                f"本项目的目录 {str(anyi_dir).replace(chr(92), '/')}，"
                                f"深层 {str(deep).replace(chr(92), '/')}")
        fish.record_memory(self.conn, self.root, project="企业智能", title="企业智能的目录",
                           body=f"目录 {str(qiye_dir).replace(chr(92), '/')}")

        res = fish.workspace_seed_candidates(self.conn, self.root)
        norm = fish.normalize_workspace_path
        suggest = {it["path"] for it in res["suggest"]}
        conflicts = {it["path"]: it for it in res["conflicts"]}
        self.assertIn(norm(str(shared)), conflicts,
                      "共享父目录下有两个项目的路径 → 它不能整片归属")
        self.assertEqual(conflicts[norm(str(shared))]["reason"], "shared_parent")
        self.assertIn(norm(str(anyi_dir)), suggest)
        self.assertIn(norm(str(qiye_dir)), suggest)
        self.assertNotIn(norm(str(deep)), suggest,
                         "父路径已被建议时，子路径不单列（前缀语义已覆盖）")


class RealpathMatchTest(unittest.TestCase):
    """C（2026-10-27 · 采集完整性）：同一物理目录的**两种写法**必须归到同一个项目。

    现场：`C:\\Users\\<user>\\.dsh` 是 junction → `I:\\app\\DSH-Home\\home`。hook 报的 cwd 走前者
    （进程的真实工作目录），人在 `workspace_map` 里映射的是后者（按真实位置写）——
    只做文本归一的话两边永远是两个字符串，项目归属静默变成"未声明"（注入包写
    「当前项目：未声明」，看着像没配对，其实是路径没归一）。

    夹具用**本机真实 junction**，不新建任何链接；机器上没有这个 junction 就 skip
    （skip 不等于通过：换机后要把夹具换成那台机器上真实存在的链接）。
    """

    LINK = Path.home() / ".dsh"

    @classmethod
    def setUpClass(cls):
        cls.real = Path(os.path.realpath(str(cls.LINK)))
        if os.path.normcase(str(cls.real)) == os.path.normcase(os.path.abspath(str(cls.LINK))):
            raise unittest.SkipTest("本机 %s 不是 junction/symlink，这条判据没有夹具" % cls.LINK)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fish-wsreal-")
        self.root = Path(self.tmp) / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        try:
            self.conn.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def _map(self, path, project):
        return fish.set_workspace_mapping(self.conn, str(path), project)

    def test_link_cwd_matches_mapping_written_in_real_path(self):
        resolve = fish.resolve_project_by_cwd
        self.assertEqual(fish._canonical_path(str(self.LINK)),
                         fish.normalize_workspace_path(str(self.real)),
                         "物理归一没把 junction 折到真实目录（这条判据的前提失效）")
        target = str(self.LINK / "sessions")
        # 前提自证：只做文本归一时这两个写法**匹配不上**，所以下面的绿必须来自 realpath
        self.assertFalse(
            fish.normalize_workspace_path(target).startswith(
                fish.normalize_workspace_path(str(self.real)) + "/"),
            "夹具无效：这两种写法文本归一后就相等了（测不到 realpath）")
        self._map(self.real, "DSHHOME")
        self.assertEqual(resolve(self.conn, target), "DSHHOME",
                         "junction 写法没命中以真实路径写的映射（realpath 比对没生效）")
        self.assertEqual(resolve(self.conn, str(self.real / "sessions")), "DSHHOME",
                         "反例自证：真实路径写法也必须命中")

    def test_mapping_written_in_link_path_matches_real_cwd(self):
        resolve = fish.resolve_project_by_cwd
        mapped = str(self.LINK / "sessions" / "--I-app-anyi--")
        self._map(mapped, "LINKPROJ")
        self.assertEqual(resolve(self.conn, str(self.real / "sessions" / "--I-app-anyi--" / "deeper")),
                         "LINKPROJ", "反向也不行：映射写 junction、cwd 报真实路径时匹配不上")

    def test_longest_prefix_semantics_survive_realpath(self):
        """realpath 只补一次比对，**不许**把"最长前缀 + 分量边界"改成"谁先命中谁赢"。"""
        resolve = fish.resolve_project_by_cwd
        self._map(self.real, "OUTER")
        self._map(self.real / "sessions", "INNER")
        self.assertEqual(resolve(self.conn, str(self.LINK / "sessions" / "abc")), "INNER",
                         "更具体的前缀没赢（链接路径把最长前缀语义弄丢了）")
        self.assertEqual(resolve(self.conn, str(self.LINK / "other")), "OUTER")
        self.assertEqual(resolve(self.conn, str(self.LINK / "sessionsX")), "OUTER",
                         "分量边界被破坏：`sessionsX` 被 `sessions` 吞了（裸 startswith）")

    def test_unrelated_path_still_none(self):
        self._map(self.real, "DSHHOME")
        self.assertIsNone(fish.resolve_project_by_cwd(self.conn, "C:/Windows/System32"),
                          "无关路径被链接归一误伤了（realpath 不能把不同目录折到一起）")

    def test_realpath_failure_falls_back_to_text(self):
        """盘不存在/没权限时 `realpath` 会失败：**不许抛**，退回文本归一继续比。"""
        self._map("Q:/no-such-drive/proj", "GHOST")
        boom = mock.patch.object(fish.os.path, "realpath", side_effect=OSError("boom"))
        with boom:
            self.assertEqual(fish._canonical_path("Q:/no-such-drive/proj"),
                             "q:/no-such-drive/proj", "realpath 失败没退回文本归一")
            self.assertEqual(fish.resolve_project_by_cwd(self.conn, "Q:/no-such-drive/proj/sub"),
                             "GHOST", "realpath 失败时整条解析抛了/断了")


if __name__ == "__main__":
    unittest.main(verbosity=2)
