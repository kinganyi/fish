#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""接入体验判据（2026-10-24）——「让失败能自解释」

背景（一个真实会话接入 fish 花掉 1 小时）
-----------------------------------------
三件让它卡住的事，全都不是"功能错了"，而是"错的时候看不出错在哪"：
  ① `register(anyi1)` 之后 `whoami` 仍显示 `dsh-session-7ca3`
     → 它以为 register = 换身份（其实 register 只建公民，身份只认 token）；
  ② 注入包写「项目 : （未声明）」，但看不出是「hook 没传 cwd」
     「cwd 不在映射里」还是「算法坏了」三种里的哪一种；
  ③ `fish外部评估` 有两个同名公民（幂等判据漏了一条）。

本文件钉住 ① ② 的修法，以及前缀退役计划（dry-run）的只读性。

判据（每条都配一个变异体，见 mutation_onboarding.py）
-----------------------------------------------------
A  register 提示**只在名不同时**出现   | 变异：无条件加提示 → register(自己) 也有 → 红
B  register 行为不变（不换归属）        | 变异：顺手改归属 → 红
C  hook 列「cwd 之下已映射的项目」      | 变异：不查候选 → 缺候选 → 红
D  无候选时不啰嗦                       | 变异：无条件加候选段 → 含「可声明」→ 红
E  候选查询是只读                       | 变异：顺手自动声明 → 行数变 → 红
F  `project retire` 计划只读            | 变异：计划里顺手写库 → 状态变 → 红

口径提醒：**本文件的测试只碰 tempdir**，一行都不碰生产库（`p0/data`）。
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
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import fish          # noqa: E402
import mcp_server    # noqa: E402

PY = sys.executable
FISH_PY = str(HERE / "fish.py")
HOOK_PY = str(HERE / "fish_dsh_hook.py")
PREFIX = "ONB"

# 判据里的三个关键短语——**判据就是措辞**：文案改了，这里就该红（然后人来看改得对不对）。
NEEDLE_STILL = "仍指向"            # A：凭证没变
NEEDLE_ONLY_CITIZEN = "这只建了公民"  # A：只建了公民
NEEDLE_CANDIDATE = "可声明"         # D：有候选才允许出现
NEEDLE_UNMAPPED = "该路径不在 workspace_map 里"   # D：无候选时的原样文案


class OnboardingJudgeTest(unittest.TestCase):
    """判据 A–F。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-onb-"))
        # Windows：sqlite 连接不关 → index.sqlite 被占用 → rmtree 静默失败（QA Round 2 实测）。
        # 用 addCleanup（LIFO：后注册的 conn.close 先跑，再删目录），**不要**放 tearDown——
        # cleanup 在 tearDown 之后执行，裸 rmtree 照样漏。
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = self.tmp / "data"
        conn = self._connect()
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        conn.execute(
            "INSERT OR IGNORE INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES (?,?,?,?,?)", (PREFIX, PREFIX, "", 0, "active"))
        conn.commit()
        conn.close()

    # ------------------------------------------------------------ 夹具

    def _connect(self):
        """生产装配路径（与 cmd_* / FishServer 完全一致）。"""
        conn = fish.connect(self.data)
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        return conn

    def _server(self):
        """真的 FishServer（不 patch 任何 env：本实现根本不读）。"""
        srv = mcp_server.FishServer(self.data)
        self.addCleanup(srv.conn.close)   # Windows：连接不关 → rmtree 静默失败
        return srv

    def _start(self, alias):
        """签一条真凭证（CLI 侧路径：显式 environ，隔开宿主 env）。"""
        conn = self._connect()
        try:
            return fish.start_session(
                conn, alias=alias,
                environ={"FISH_ACTOR": alias, "FISH_SESSION_ACTOR": alias})
        finally:
            conn.close()

    def _citizen_of(self, token):
        """凭证现在指向谁（**独立连接读真值**，不经过被测实现）。"""
        conn = self._connect()
        try:
            info = fish.lookup_session_token(conn, token)
            return None if info is None else info["citizen_id"]
        finally:
            conn.close()

    def _map(self, path, project):
        conn = self._connect()
        try:
            fish.set_workspace_mapping(conn, path, project)
        finally:
            conn.close()

    def _display_name(self, citizen_id):
        conn = self._connect()
        try:
            return fish.load_citizen(conn, citizen_id)["display_name"]
        finally:
            conn.close()

    def _workspace_rows(self):
        conn = self._connect()
        try:
            return [tuple(r) for r in conn.execute(
                "SELECT path_prefix,project,note,created_at FROM workspace_map "
                "ORDER BY path_prefix")]
        finally:
            conn.close()

    def _count(self, table):
        conn = self._connect()
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()

    def _count_like(self, table, where):
        """按条件数行（where 是本文件自己写的固定片段，不接受外部输入）。"""
        conn = self._connect()
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]
        finally:
            conn.close()

    def _run_cli(self, *args):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [PY, FISH_PY, "--root", str(self.data), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)

    def _hook(self, payload, root=None):
        """跑一次真 hook，取 additionalContext。root=None → 用本夹具的 data 目录。"""
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["FISH_ROOT"] = str(self.data if root is None else root)
        r = subprocess.run([PY, HOOK_PY], input=json.dumps(payload),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env)
        self.assertEqual(r.returncode, 0, f"hook 退出码必须恒为 0；stderr={r.stderr[:400]}")
        return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]

    # ------------------------------------------------------------ 判据 A
    def test_A_warning_only_when_display_name_differs(self):
        """A：register(乙) → 含「仍指向」；register(甲=当前公民) → 一个字都不多。"""
        res = self._start("甲")
        token, cid = res["token"], res["citizen_id"]
        srv = self._server()

        # 名不同：必须明确说"身份没换"，并给出唯一的换法
        text_other = srv.call_tool("register", {"display_name": "乙", "token": token})
        self.assertIn(NEEDLE_STILL, text_other,
                      f"register 名不同却没提示「凭证仍指向原公民」：{text_other!r}")
        self.assertIn(NEEDLE_ONLY_CITIZEN, text_other, "必须说明「这只建了公民」")
        self.assertIn(self._display_name(cid), text_other,
                      "提示里要写出**当前凭证指向谁**（否则人还是不知道自己是谁）")
        self.assertIn("fish session start --alias 乙", text_other,
                      "要给出唯一的换法：重签凭证")

        # 同一个名字再来一次（走"已存在"分支）：判定条件一样，仍要提示
        again = srv.call_tool("register", {"display_name": "乙", "token": token})
        self.assertIn("已存在", again, "前提：第二次 register 应走幂等分支")
        self.assertIn(NEEDLE_STILL, again, "幂等分支同样要提示身份没换")

        # 名相同：这正是"就是你自己"，多一句都是噪音（变异体的红点在这条）
        same = srv.call_tool("register", {"display_name": "甲", "token": token})
        self.assertNotIn(NEEDLE_STILL, same,
                         f"register(当前公民自己) 不该出现「仍指向」：{same!r}")
        self.assertNotIn(NEEDLE_ONLY_CITIZEN, same, "名字相同却仍在警示 = 无条件加提示的变异")

    def test_A2_no_warning_without_credential(self):
        """A 的边界：没带 token 时不做比较（由 call_tool 的统一提示负责）。"""
        srv = self._server()
        text = srv.call_tool("register", {"display_name": "无凭证公民"})
        self.assertNotIn(NEEDLE_STILL, text,
                         "没有凭证就没有「指向谁」这回事，不许编一个出来")
        self.assertNotIn(NEEDLE_ONLY_CITIZEN, text)

    # ------------------------------------------------------------ 判据 B
    def test_B_register_does_not_change_attribution(self):
        """B：register 前后，凭证解出的 citizen_id 不变；whoami 仍报凭证里的公民。"""
        res = self._start("丙")
        token, cid = res["token"], res["citizen_id"]
        before = self._citizen_of(token)
        self.assertEqual(before, cid, "前提：刚签的凭证应指向刚建的公民")

        srv = self._server()
        text = srv.call_tool("register", {"display_name": "丁", "token": token})
        self.assertIn(NEEDLE_STILL, text, "前提：这次名不同，应带警示")

        after = self._citizen_of(token)
        self.assertEqual(after, before,
                         "register 改了凭证的归属——那正是铁律「不改身份语义」禁止的事")

        who = srv.call_tool("whoami", {"token": token})
        self.assertIn(cid, who, "whoami 必须仍按 token 报公民")
        self.assertNotIn("丁", who, "whoami 报出了刚 register 的名字 = 身份被 register 改写了")

    # ------------------------------------------------------------ 判据 C
    def test_C_hook_lists_candidates_under_cwd(self):
        """C：cwd=/ws（未映射），但 /ws/projA→A、/ws/projB→B → 注入里两个都在。"""
        self._map("/ws/projA", "PROJA")
        self._map("/ws/projB", "PROJB")
        ctx = self._hook({"session_id": "onb-c1", "cwd": "/ws",
                          "hook_event_name": "SessionStart"})
        self.assertIn("PROJA", ctx, f"没列出 /ws/projA 这个候选：{ctx[:400]}")
        self.assertIn("PROJB", ctx, f"没列出 /ws/projB 这个候选：{ctx[:400]}")
        self.assertIn(NEEDLE_CANDIDATE, ctx, "候选段要有「可声明」这层意思")
        self.assertIn("/ws/proja", ctx, "候选要带**具体子目录**（否则人不知道 set 哪条路径）")
        self.assertIn("fish workspace set", ctx, "要给出可直接抄的命令")
        self.assertIn("（未声明）", ctx, "项目行仍是未声明（候选不等于已声明）")

    def test_C2_candidate_query_semantics(self):
        """C 的单元级：只要**严格子目录**，祖先 / 自身 / 兄弟都不要。"""
        self._map("/ws", "PARENT")
        self._map("/ws/projA", "PROJA")
        self._map("/ws/projB/deep", "PROJB")
        self._map("/ws2", "SIBLING")
        conn = self._connect()
        try:
            got = fish.list_workspace_candidates(conn, "/ws")
            self.assertEqual([c["project"] for c in got], ["PROJA", "PROJB"],
                             f"应只返回 /ws 之下（由浅到深）；实际 {got}")
            self.assertEqual(fish.list_workspace_candidates(conn, "/ws/projA"), [],
                             "cwd 自身那条不算候选（它命中的话根本走不到未声明分支）")
            self.assertEqual(fish.list_workspace_candidates(conn, "/empty"), [],
                             "没有候选时必须返回空列表（调用方据此决定不写废话）")
            self.assertEqual(fish.list_workspace_candidates(conn, None), [])
            self.assertEqual(fish.list_workspace_candidates(conn, "  "), [])
            # 归一化：大小写/尾斜杠/反斜杠都必须命中同一批候选
            self.assertEqual(
                [c["project"] for c in fish.list_workspace_candidates(conn, "C:/WS/")],
                [], "C:/WS 与 /ws 是不同的根，不该互相命中")
        finally:
            conn.close()

    # ------------------------------------------------------------ 判据 D
    def test_D_no_candidates_keeps_old_wording(self):
        """D：cwd=/empty 下没有任何映射 → 注入里**不含**「可声明」。"""
        ctx = self._hook({"session_id": "onb-d1", "cwd": "/empty",
                          "hook_event_name": "SessionStart"})
        self.assertNotIn(NEEDLE_CANDIDATE, ctx,
                         f"没有候选却写了候选段（啰嗦）：{ctx[:400]}")
        self.assertIn(NEEDLE_UNMAPPED, ctx, "无候选时要保留原来那句诊断")
        self.assertIn("fish workspace set", ctx, "仍要给一条最小的修法")

    def test_D2_hook_survives_missing_db(self):
        """D 的边界：库都不存在时，hook 照旧输出 + 不崩（诊断绝不影响启动）。"""
        empty = self.tmp / "no-db-here"
        empty.mkdir()
        ctx = self._hook({"session_id": "onb-d2", "cwd": "/empty"}, root=empty)
        self.assertIn(NEEDLE_UNMAPPED, ctx)
        self.assertNotIn(NEEDLE_CANDIDATE, ctx)

    # ------------------------------------------------------------ 判据 E
    def test_E_candidate_lookup_is_readonly(self):
        """E：跑 hook 前后 workspace_map 行数/内容不变，且没有新的映射事件。

        注意**不能**拿 `events` 总数当判据：hook 里还有一次合法的 `session start`
        （它本来就会写 session.opened）。要钉的是"候选查询"这一件事，所以只数
        `workspace.*` 事件——判据必须对准被测行为，否则红的会是正确的实现。
        """
        self._map("/ws/projA", "PROJA")
        self._map("/ws/projB", "PROJB")
        before_rows = self._workspace_rows()
        before_map_events = self._count_like("events", "type LIKE 'workspace.%'")

        ctx = self._hook({"session_id": "onb-e1", "cwd": "/ws",
                          "hook_event_name": "SessionStart"})
        self.assertIn("PROJA", ctx, "前提：这一次真的走了候选分支（否则等于没测）")
        self._hook({"session_id": "onb-e2", "cwd": "/ws/projA/far/below",
                    "hook_event_name": "SessionStart"})

        self.assertEqual(self._workspace_rows(), before_rows,
                         "候选查询改了 workspace_map——它必须是纯只读")
        self.assertEqual(self._count_like("events", "type LIKE 'workspace.%'"),
                         before_map_events,
                         "候选查询写了映射事件——它必须是纯只读")

    # ------------------------------------------------------------ 判据 F
    def test_F_project_retire_plan_is_readonly(self):
        """F：`project retire` 的计划（不加 --apply）不写库；有记忆时拒绝执行。"""
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO code_registry (prefix,namespace,last_date,last_seq,status) "
                "VALUES (?,?,?,?,?)", ("测试项目", "测试项目", "", 0, "active"))
            conn.commit()
            before = [tuple(r) for r in conn.execute(
                "SELECT prefix,status,last_date,last_seq FROM code_registry ORDER BY prefix")]
            events_before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            for _ in range(3):        # 跑多遍：只读的判据是"跑多少遍都不变"
                plan = fish.plan_project_status_change(conn, "测试项目", "retired")
                self.assertTrue(plan["found"], f"空白前缀应能查到：{plan}")
                self.assertEqual(plan["current"], "active")
                self.assertEqual(plan["counts"]["memories"], 0)
                self.assertEqual(plan["blockers"], [], "0 条记忆的前缀不该被拦")
                self.assertTrue(any("不删行" in c for c in plan["changes"]),
                                f"计划必须写明不删行（ADR-003）：{plan['changes']}")
            after = [tuple(r) for r in conn.execute(
                "SELECT prefix,status,last_date,last_seq FROM code_registry ORDER BY prefix")]
            self.assertEqual(after, before, "计划阶段不许改库")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                             events_before, "计划阶段不许写事件")

            # 有记忆时：--apply 级别的执行必须被拦住（除非 --force）
            code, _rel = fish.record_memory(conn, self.data, project=PREFIX, title="占位")
            self.assertTrue(code.startswith(PREFIX))
            conn.commit()
            blocked = fish.plan_project_status_change(conn, PREFIX, "retired")
            self.assertEqual(blocked["counts"]["memories"], 1)
            self.assertTrue(blocked["blockers"], "有记忆却没拦住——退役会让归属无声失去解释")
            forced = fish.plan_project_status_change(conn, PREFIX, "retired", force=True)
            self.assertEqual(forced["blockers"], [], "--force 应放行（只停分配、不动记忆）")
        finally:
            conn.close()

    def test_F2_project_retire_cli_prints_plan_then_applies_only_with_flag(self):
        """F 的端到端：CLI 默认只打印；--apply 才改状态，且**行还在**（ADR-003）。"""
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO code_registry (prefix,namespace,last_date,last_seq,status) "
                "VALUES (?,?,?,?,?)", ("测试项目", "测试项目", "", 0, "active"))
            conn.commit()
        finally:
            conn.close()

        dry = self._run_cli("project", "retire", "测试项目", "--reason", "空壳前缀，与 TESTP 同义")
        self.assertEqual(dry.returncode, 0, f"dry-run 应成功；stderr={dry.stderr[:400]}")
        self.assertIn("只是打印计划", dry.stdout, f"要明说没落库：{dry.stdout[:400]}")
        self.assertIn("不删行", dry.stdout, "计划里必须写明不删行（ADR-003）")
        self.assertEqual(self._project_status("测试项目"), "active",
                         "dry-run 竟然改了状态——这正是判据 F 要挡的")

        applied = self._run_cli("project", "retire", "测试项目",
                                "--reason", "空壳前缀，与 TESTP 同义", "--apply")
        self.assertEqual(applied.returncode, 0, f"stderr={applied.stderr[:400]}")
        self.assertEqual(self._project_status("测试项目"), "retired")
        conn = self._connect()
        try:
            self.assertIsNotNone(
                conn.execute("SELECT 1 FROM code_registry WHERE prefix='测试项目'").fetchone(),
                "退役把行删了——ADR-003：前缀不可回收，只改状态")
            self.assertIsNotNone(
                conn.execute("SELECT 1 FROM events WHERE type='project.retired' "
                             "AND payload LIKE '%测试项目%'").fetchone(),
                "改状态必须留事件，否则无法追溯谁在什么时候退役的")
        finally:
            conn.close()

        restored = self._run_cli("project", "restore", "测试项目", "--apply")
        self.assertEqual(restored.returncode, 0, f"stderr={restored.stderr[:400]}")
        self.assertEqual(self._project_status("测试项目"), "active", "restore 应能改回 active")

        # 已在目标状态 → 幂等（不报错、不重复写事件）
        idem = self._run_cli("project", "restore", "测试项目")
        self.assertEqual(idem.returncode, 0)
        self.assertIn("幂等", idem.stdout)

        # 不存在的前缀 → 明确报错（不凭空建一个）
        missing = self._run_cli("project", "retire", "不存在的项目")
        self.assertEqual(missing.returncode, 2, "对不存在的前缀必须显式失败")
        self.assertIn("没有前缀", missing.stdout)

    def _project_status(self, prefix):
        conn = self._connect()
        try:
            row = conn.execute("SELECT status FROM code_registry WHERE prefix=?",
                               (prefix,)).fetchone()
            return None if row is None else row[0]
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
