#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W5 判据 · 读取路由与 UI（113 §9，2026-11-25）。

## 这一波守什么

W5 不新增"真相"，只新增**读法**：会话树的三键（父 / 代理 / 关系）、树视图、清退
`presence.agent_id` 里的假代理。三处全是"判不到怎么办"的形状——最容易写成恒绿，
所以每条都配变异（`mutation_w5.py`）。

## 判据清单

  J7（actor 三键）
      新返回的 actor **键集合 ⊇ 旧 5 键 + parentSession/agent/relation**；四档
      （system / 公民 / 会话 / 未命名）一个都不许少键；判不到时三键是 `None`
      （不是 `"main"`、不是 `""`）——"判不到"与"它是根"在界面上长得一样、语义相反。
  另：`events.actor`（库内 JSON）**一个键都不许加**（三种老形状各有消费方，113 §1-3）。

  J-W5-legacy（假代理清零）
      ① 搬列（列空 + 旧列形如会话键 → 搬进 `session_key`）；
      ② 清假代理（`agent_id` 抄的就是会话键 → 清空）；
      ③ 标 legacy（两列都空 → `auto_source='legacy'`）；
      且**四处写入点**不再产生假代理（含 110 漏的降级分支）。幂等：连跑两次第二次是空操作。

  J6（团队会话的代理维）
      同一 team 会话在树里 **≥2 行且 `agent_role` 非空**；树视图每行带
      `depth`/`relation`/`basis`；撞车视图（PreToolUse）的命中行带 `agent_role`
      且**如实标注"离线投影、非实时"**；判不到就是 `None` + `basis='missing'`。

## 环境边界

本机 `session_lineage` **实测 0 行**（W6 回填未跑）。所以树视图的 live 兜底
（现读宿主会话头）不是可选项——只读投影的话 `session tree` 在任何真实会话上都返回
空树，而"空树"与"没有父子关系"是两件事。本文件两种来源都测：
投影路径（造临时库）与 live 路径（造临时 DSH 会话头目录）。
"""
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fish              # noqa: E402
import fish_activity     # noqa: E402
import fish_board        # noqa: E402
import fish_pretool_hook  # noqa: E402
import fish_workbench_api as WB  # noqa: E402

OLD_FIVE = {"name", "labelKind", "citizenId", "host", "sessionTag"}
NEW_THREE = {"parentSession", "agent", "relation"}

CID = "C0AAAAAAAAAAAAAAAAAAAAAAAA"
OTHER = "C1BBBBBBBBBBBBBBBBBBBBBBBB"


def _row(session_key="", citizen_id=CID, **extra):
    """`actor_of` 只读这三列（与 `_UNION_COLS` 同口径），测试用最小的 dict。"""
    row = {"session_key": session_key, "citizen_id": citizen_id}
    row.update(extra)
    return row


class _Base(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-w5-"))
        self.root = self.tmp / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)

    def tearDown(self):
        try:
            self.conn.close()
        except sqlite3.Error:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ 会话树夹具
    def _node(self, sid, *, parent=None, relation=None, origin="main",
              agent_role=None, agent_id=None, basis="header", host="dsh",
              depth=0):
        self.conn.execute(
            "INSERT INTO session_lineage (host, session_id, parent_session, depth, "
            "origin, relation, agent_id, agent_role, basis, source, observed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,'test',?)",
            (host, sid, parent, depth, origin, relation, agent_id, agent_role, basis,
             fish.now_utc()))
        self.conn.commit()


class J7ActorKeysTest(_Base):
    """J7：actor 键集合 ⊇ 旧 5 + 三新；判不到是 None，不是 main/空串。"""

    def test_j7_all_four_branches_keep_five_and_gain_three(self):
        cases = {
            "system": _row("dsh:s1", "system"),
            "citizen": _row("dsh:s2", CID),
            "session": _row("dsh:s3", ""),
            "none": _row("", ""),
        }
        labels = {CID: "我-主AI"}
        for name, row in cases.items():
            actor = WB.actor_of(row, labels)
            self.assertTrue(OLD_FIVE <= set(actor),
                            f"{name} 档缺了旧 5 键：{sorted(OLD_FIVE - set(actor))}")
            self.assertTrue(NEW_THREE <= set(actor),
                            f"{name} 档缺了三新键：{sorted(NEW_THREE - set(actor))}")
            self.assertEqual(len(actor), 8, f"{name} 档键数不是 8：{sorted(actor)}")

    def test_j7_lineage_fills_three_keys(self):
        self._node("child-1", parent="parent-1", relation="spawn", origin="subagent",
                   agent_role="dafish-team", agent_id="agent-7")
        lineage = WB.lineage_of(self.conn, ["dsh:child-1"])
        actor = WB.actor_of(_row("dsh:child-1", CID), {CID: "我"}, lineage=lineage)
        self.assertEqual(actor["parentSession"], "parent-1",
                         "父必须是裸 id（不带 host: 前缀，113 §2.3）")
        self.assertEqual(actor["agent"], {"id": "agent-7", "role": "dafish-team"})
        self.assertEqual(actor["relation"], "spawn")

    def test_j7_no_lineage_gives_none_not_main(self):
        """判不到 → 三键 `None`。拿 `"main"` 或 `""` 冒充会让"根"与"判不到"混成一件事。"""
        actor = WB.actor_of(_row("dsh:nowhere", CID), {CID: "我"})
        self.assertIsNone(actor["parentSession"])
        self.assertIsNone(actor["agent"])
        self.assertIsNone(actor["relation"])

    def test_j7_agent_none_when_both_columns_empty(self):
        """节点在、但代理两列都空 → `agent=None`（不是 `{"id":null,"role":null}`）。"""
        self._node("plain-1", parent=None, origin="main")
        actor = WB.actor_of(_row("dsh:plain-1", CID), {CID: "我"},
                            lineage=WB.lineage_of(self.conn, ["dsh:plain-1"]))
        self.assertIsNone(actor["agent"], "两列皆空时挂一个空对象 = 假装有代理")

    def test_j7_lineage_of_maps_hosts_and_skips_unknown(self):
        """`codebuddy:` → `workbuddy`（同一宿主两个前缀）；未知前缀/裸键**不查也不猜**。"""
        self._node("wb-1", agent_role="reviewer", host="workbuddy")
        got = WB.lineage_of(self.conn, ["codebuddy:wb-1", "nohost:wb-1", "wb-1"])
        self.assertIn("codebuddy:wb-1", got, "codebuddy 前缀没映射到 workbuddy 宿主")
        self.assertNotIn("nohost:wb-1", got, "未知前缀被当成了会话键")
        self.assertNotIn("wb-1", got, "裸键没有宿主，不该被猜成 dsh")

    def test_j7_events_actor_has_no_new_keys(self):
        """`events.actor`（库内 JSON）**不加键**：父链是读时叠加，不是改写不可变表。"""
        fish.append_event(self.conn, "w5.probe",
                          {"citizen_id": CID, "evidence_level": "machine"},
                          {"kind": "probe"}, {"x": 1})
        self.conn.commit()
        raw = self.conn.execute(
            "SELECT actor FROM events WHERE type='w5.probe'").fetchone()[0]
        keys = set(json.loads(raw))
        self.assertEqual(keys, {"citizen_id", "evidence_level"},
                         f"事件表里的 actor 被加了键：{sorted(keys)}")
        self.assertFalse(keys & NEW_THREE, "三键泄漏进了 events.actor")


class PresenceLegacyRetireTest(_Base):
    """J-W5-legacy：三步清退 + 四处写入点不再产生假代理。"""

    def _presence(self, *, citizen=CID, session_key="", agent_id=None,
                  auto_source=None, board_id=None, first_seen_at=None):
        if board_id is None:      # presence.board_id 有外键：必须先有那块板
            board_id = fish_board.board_ensure(self.conn, "project", "PJ")["id"]
        self.conn.execute(
            "INSERT INTO presence (board_id, citizen_id, session_key, agent_id, status, "
            "doing, surface, first_seen_at, last_seen_at, auto_source) "
            "VALUES (?,?,?,?,'active','x','',?,?,?)",
            (board_id, citizen, session_key, agent_id,
             first_seen_at or fish.now_utc(), first_seen_at or fish.now_utc(), auto_source))
        self.conn.commit()

    def _count(self, sql, *params):
        return self.conn.execute(sql, params).fetchone()[0]

    def _fake(self):
        # 口径与 ② 同一条：`agent_id` **非空**且等于会话键。`'' = ''` 不算假代理
        # （两列都空的行由 ③ 标 legacy，两者别混在一个计数里）。
        return self._count("SELECT COUNT(*) FROM presence "
                           "WHERE agent_id IS NOT NULL AND agent_id <> '' "
                           "  AND agent_id = session_key")

    def test_j_w5_legacy_three_steps(self):
        self._presence(session_key="", agent_id="dsh:legacy-1")     # ① 搬
        self._presence(session_key="dsh:real-2", agent_id="dsh:real-2",
                       citizen=OTHER)                               # ② 清
        # ③ 标：必须带**上线前的 first_seen_at**（截点存在的理由见 fish._RETIRE_LEGACY_BEFORE）
        self._presence(session_key="", agent_id="", auto_source=None,
                       first_seen_at="2026-10-01T00:00:00Z",
                       citizen="C2CCCCCCCCCCCCCCCCCCCCCCCC")
        self._presence(session_key="", agent_id="codebuddy-主AI/ffd54d84",
                       citizen="C3DDDDDDDDDDDDDDDDDDDDDDDD")        # 非标准形态：不动
        self.assertEqual(self._fake(), 1, "夹具没造出假代理，判据会恒绿")

        out = fish._retire_presence_agent_ids(self.conn)
        self.assertEqual(out["counts"], [1, 1, 1],
                         f"三步计数不对（搬/清/标）：{out}")
        self.assertTrue(out["backup"], "有变化却没备份：回退值无处可取")
        self.assertTrue(Path(out["backup"]).exists(), f"备份文件不存在：{out['backup']}")
        self.assertEqual(self._fake(), 0, "J-W5-legacy：假代理必须清零")
        moved = self.conn.execute(
            "SELECT session_key, agent_id FROM presence WHERE session_key='dsh:legacy-1'"
        ).fetchone()
        self.assertEqual(tuple(moved), ("dsh:legacy-1", None),
                         "① 搬列后旧列必须清空（不是两边都留着）")
        marked = self.conn.execute(
            "SELECT auto_source FROM presence WHERE citizen_id='C2CCCCCCCCCCCCCCCCCCCCCCCC'"
        ).fetchone()[0]
        self.assertEqual(marked, "legacy")
        untouched = self.conn.execute(
            "SELECT agent_id FROM presence WHERE citizen_id='C3DDDDDDDDDDDDDDDDDDDDDDDD'"
        ).fetchone()[0]
        self.assertEqual(untouched, "codebuddy-主AI/ffd54d84",
                         "非标准形态（带 / 无冒号）不是会话键，不许猜、不许清")

    def test_j_w5_legacy_new_blank_row_is_not_marked(self):
        """**上线之后**新建的"无键无代理"行不许被标 legacy（截点）。

        没有截点会怎样：`board render` 这类命令每次 run 都把手工签到行标成 `legacy`，
        还会顺带写一份 38MB 备份——`test_board_home_root` 的"render 不许改 data_root"
        就是这么红的（实测）。
        """
        self._presence(session_key="", agent_id="", auto_source=None)   # first_seen_at = 现在
        out = fish._retire_presence_agent_ids(self.conn)
        self.assertEqual(out["counts"], [0, 0, 0], f"新行被当成存量清了：{out}")
        self.assertIsNone(out["backup"], "没有存量可清时不许写备份")
        got = self.conn.execute(
            "SELECT auto_source FROM presence WHERE citizen_id=?", (CID,)).fetchone()[0]
        self.assertIsNone(got, "新行被安上了它没有的来历（auto_source='legacy'）")

    def test_j_w5_legacy_idempotent(self):
        self._presence(session_key="dsh:real-3", agent_id="dsh:real-3")
        first = fish._retire_presence_agent_ids(self.conn)
        second = fish._retire_presence_agent_ids(self.conn)
        self.assertEqual(first["counts"], [0, 1, 0])
        self.assertEqual(second["counts"], [0, 0, 0], "第二次必须全 0（幂等）")
        self.assertIsNone(second["backup"], "没变化就不该再写备份")

    def test_j_w5_legacy_migrate_runs_it(self):
        """真入口：`migrate()` 就带清退（不需要谁手工跑一条命令）。"""
        self._presence(session_key="dsh:real-4", agent_id="dsh:real-4")
        fish.migrate(self.conn)
        self.assertEqual(self._fake(), 0, "migrate 没带清退：假代理还在")

    def test_j_w5_legacy_checkin_writes_no_fake_agent(self):
        fish.board_checkin(self.conn, CID, "改这块", surface="p0/x.py",
                           scope="project", scope_key="PJ", session_key="dsh:s9")
        fish.board_auto_checkin(self.conn, OTHER, project="PJ", session_key="dsh:s10",
                                source="session_start")
        self.assertEqual(self._fake(), 0, "写入点还在拿会话键冒充代理")
        rows = self.conn.execute(
            "SELECT session_key, agent_id FROM presence ORDER BY session_key").fetchall()
        self.assertEqual([r[1] for r in rows], [None, None],
                         f"判不到代理时必须写 NULL：{rows}")

    def test_j_w5_legacy_degraded_two_column_path(self):
        """**110 漏的第四处**：两列主键的降级分支也不许抄会话键。"""
        self.conn.execute("DROP TABLE presence")
        self.conn.execute(
            "CREATE TABLE presence (board_id INTEGER NOT NULL, citizen_id TEXT NOT NULL, "
            "agent_id TEXT, status TEXT, doing TEXT, surface TEXT, first_seen_at TEXT, "
            "last_seen_at TEXT, auto_surface TEXT, auto_seen_at TEXT, auto_source TEXT, "
            "session_surface TEXT, PRIMARY KEY (board_id, citizen_id))")
        self.conn.commit()
        fish.board_checkin(self.conn, CID, "降级档签到", surface="p0/y.py",
                           scope="project", scope_key="PJ", session_key="dsh:degraded")
        got = self.conn.execute(
            "SELECT agent_id FROM presence WHERE citizen_id=?", (CID,)).fetchone()[0]
        self.assertIsNone(got, "降级分支把会话键抄进了 agent_id（110 漏的第四处）")

    def test_j_w5_legacy_source_closed_set(self):
        """渲染面 `session_key_source` 的闭集：`column` / `legacy_readonly` / `none`。"""
        board = fish.board_checkin(self.conn, CID, "在改", surface="p0/z.py",
                                   scope="project", scope_key="PJ")
        self.conn.execute("UPDATE presence SET agent_id='dsh:oldstyle' WHERE board_id=?",
                          (board["id"],))
        self.conn.commit()
        rows = fish_board.board_presence_rows(self.conn, board)
        self.assertEqual(rows[0]["session_key"], "dsh:oldstyle")
        self.assertEqual(rows[0]["session_key_source"], "legacy_readonly",
                         "旧列兜底必须是**只读**语义（不再有新写入），值叫 legacy_readonly")
        self.conn.execute("UPDATE presence SET agent_id=NULL WHERE board_id=?",
                          (board["id"],))
        self.conn.commit()
        rows = fish_board.board_presence_rows(self.conn, board)
        self.assertEqual(rows[0]["session_key_source"], "none")


class J6TeamAgentDimensionTest(_Base):
    """J6：同一 team 会话 ≥2 行且 `agent_role` 非空；树视图/撞车面都带代理维。"""

    def _team(self):
        self._node("team-root", agent_role="dafish-team")
        self._node("team-a", parent="team-root", relation="spawn", origin="subagent",
                   agent_role="dafish-team", agent_id="agent-a")
        self._node("team-b", parent="team-root", relation="spawn", origin="subagent",
                   agent_role="dafish-team", agent_id="agent-b")

    def test_j6_tree_projection_has_two_children_with_role(self):
        self._team()
        tree = WB.session_tree(self.conn, "dsh:team-root", depth=1)
        self.assertEqual(tree["source"], "projection", f"该走投影：{tree}")
        rows = tree["rows"]
        self.assertGreaterEqual(len(rows), 2, f"团队会话至少 2 行：{rows}")
        kids = [r for r in rows if r["depth"] == 1]
        self.assertEqual(len(kids), 2)
        self.assertEqual(sorted(r["agent_id"] for r in kids), ["agent-a", "agent-b"],
                         "代理维丢了 agent_id：只剩角色等于只说'有个代理'、不说是谁")
        for kid in kids:
            self.assertTrue(kid["agent_role"], f"子行缺 agent_role：{kid}")
            self.assertEqual(kid["basis"], "header")
            self.assertEqual(kid["relation"], "spawn")
            self.assertEqual(kid["depth"], 1)

    def test_j6_tree_depth_and_lazy_limit(self):
        self._team()
        self._node("team-a-1", parent="team-a", relation="spawn", origin="subagent",
                   agent_role="dafish-team")
        only_root = WB.session_tree(self.conn, "dsh:team-root", depth=0)
        self.assertEqual([r["session_id"] for r in only_root["rows"]], ["team-root"],
                         "depth=0 时只许有根（默认 1 层是口子，0 是显式只要根）")
        deep = WB.session_tree(self.conn, "dsh:team-root", depth=2)
        self.assertIn("team-a-1", [r["session_id"] for r in deep["rows"]])
        capped = WB.session_tree(self.conn, "dsh:team-root", depth=2, limit=2)
        self.assertEqual(len(capped["rows"]), 2, "LIMIT 没生效（懒加载要靠它）")
        self.assertTrue(capped["truncated"], "截断了却不说 = 静默丢行")

    def test_j6_cycle_row_is_not_expanded(self):
        """`relation='cycle'` 的行**不展开**（113 §9.3），且不许无限递归。"""
        self._node("cyc-a", parent="cyc-b", relation="cycle", origin="unknown",
                   basis="conflict")
        self._node("cyc-b", parent="cyc-a", relation="cycle", origin="unknown",
                   basis="conflict")
        tree = WB.session_tree(self.conn, "dsh:cyc-a", depth=5)
        ids = [r["session_id"] for r in tree["rows"]]
        self.assertEqual(ids, ["cyc-a"], f"cycle 行被展开了（或死循环）：{ids}")

    def test_j6_tree_text_carries_three_columns(self):
        self._team()
        text = WB.session_tree_text(WB.session_tree(self.conn, "dsh:team-root", depth=1))
        kids = [ln for ln in text.splitlines() if "depth=1" in ln]
        self.assertEqual(len(kids), 2, f"渲染面漏了子行：{text}")
        for ln in kids:
            self.assertIn("relation=", ln)
            self.assertIn("basis=", ln)
        self.assertIn("agent_role=dafish-team", text)

    # ---------------------------------------------------------- 撞车面（hook）
    def _snapshot(self, *, session="dsh:other", surface="i:/proj/p0/fish.py"):
        # 绝对面 + workspace_map 锚点（与 test_pretool_ask 的夹具同口径）：
        # 相对面要靠 cwd 绝对化，缺 cwd 时 hook 会按"宁可漏报"放行——那样测不到撞车。
        self.conn.execute(
            "INSERT OR IGNORE INTO workspace_map (path_prefix, project, note, created_at) "
            "VALUES ('i:/proj','PJ',NULL,?)", (fish.now_utc(),))
        fish.board_checkin(self.conn, OTHER, "在改这块", surface=surface,
                           scope="project", scope_key="PJ", session_key=session)
        fish_activity.build_snapshot(self.conn, out_path=self.snapshot_path,
                                     window_min=90)

    @property
    def snapshot_path(self):
        return self.root / fish_activity.SNAPSHOT_RELPATH

    def _evaluate(self):
        payload = {"session_id": "me", "cwd": "i:/proj", "tool_name": "write",
                   "tool_input": {"file_path": "i:/proj/p0/fish.py"}}
        return fish_pretool_hook.evaluate(payload, host="dsh", data_dir=self.root)

    def test_j6_collision_view_has_agent_role(self):
        self._snapshot()
        self._node("other", agent_role="reviewer", agent_id="agent-r")
        res = self._evaluate()
        self.assertEqual(res["decision"], "warn", f"夹具没造出撞车：{res}")
        hit = res["hits"][0]
        self.assertEqual(hit["agent_role"], "reviewer",
                         f"撞车面没带代理维：{hit}")
        self.assertEqual(hit["agent_role_basis"], "projection")
        self.assertIn("代理：reviewer", res["reason"])
        self.assertIn("离线投影", res["reason"], "必须如实标非实时")
        self.assertIn("非实时", res["reason"])

    def test_j6_collision_without_projection_row_says_none(self):
        """投影**查过了但没这一行** → `agent_role=None` + `basis='projection'`（判到了"没有"）。

        这与"判不了"（`basis='missing'`，见下一条）不是一回事：前者是证据、后者是缺口。
        """
        self._snapshot()
        res = self._evaluate()
        hit = res["hits"][0]
        self.assertIsNone(hit["agent_role"])
        self.assertEqual(hit["agent_role_basis"], "projection")
        self.assertNotIn("代理：", res["reason"], "判不到代理时不许说代理")

    def test_j6_collision_unknown_host_says_missing(self):
        """会话键前缀不属已知宿主 → `basis='missing'`（**没查**，不是"没有"）。"""
        self._snapshot(session="self:other")
        res = self._evaluate()
        hit = res["hits"][0]
        self.assertIsNone(hit["agent_role"])
        self.assertEqual(hit["agent_role_basis"], "missing",
                         f"判不了要说判不了：{hit}")


class TreeLiveFallbackTest(_Base):
    """投影 0 行时现读宿主会话头（本机实测就是 0 行）——`source='live'` 且标非实时。"""

    def _fake_dsh(self):
        """造一棵最小 DSH 会话头：root(main) ← child(subagent)，走真适配器。"""
        import fish_lineage_dsh as dsh
        roots = self.tmp / "dsh-sessions"
        ws = roots / "ws-one"
        for sid in ("root-1", "child-1"):
            (ws / sid).mkdir(parents=True)

        def _write(sid, header):
            import zstandard as zstd            # noqa: PLC0415 —— 与适配器同一编码
            frame = ws / sid / dsh.FRAME_NAME
            frame.write_bytes(zstd.ZstdCompressor().compress(
                (json.dumps(header, ensure_ascii=False) + "\n").encode("utf-8")))

        _write("root-1", {"type": "session", "sessionId": "root-1",
                          "cwd": "/w", "agentPreset": "dafish-team",
                          "createdAt": "2026-11-25T00:00:00Z"})
        _write("child-1", {"type": "session", "sessionId": "child-1",
                           "cwd": "/w", "origin": "subagent",
                           "parentSession": "root-1", "delegationDepth": 1,
                           "agentPreset": "dafish-team",
                           "createdAt": "2026-11-25T00:01:00Z"})
        return roots

    def test_live_tree_reads_headers_when_projection_empty(self):
        try:
            import zstandard  # noqa: F401
        except ImportError:                       # pragma: no cover —— 判不到就是判不到
            self.skipTest("没有 zstandard：造不出宿主会话头（判不到，不是通过）")
        import fish_lineage_dsh as dsh
        roots = self._fake_dsh()
        adapter = dsh.DshLineageAdapter(roots=roots)
        # 适配器自带 roots 只在本进程有效；`session_tree` 走注册表 → 用 patch 换掉
        from unittest import mock
        with mock.patch.object(WB.fish_lineage, "get_adapter", return_value=adapter):
            tree = WB.session_tree(self.conn, "dsh:root-1", depth=1)
        self.assertEqual(tree["source"], "live", f"投影为空时没退现读：{tree}")
        ids = [r["session_id"] for r in tree["rows"]]
        self.assertEqual(ids, ["root-1", "child-1"], f"live 树不对：{ids}")
        self.assertIn("非实时", tree["note"], "现读的树必须标非实时")
        child = [r for r in tree["rows"] if r["depth"] == 1][0]
        self.assertEqual(child["relation"], "spawn")
        self.assertEqual(child["agent_role"], "dafish-team")

    def test_live_tree_unknown_session_is_not_a_root(self):
        tree = WB.session_tree(self.conn, "dsh:no-such-session", depth=1)
        self.assertEqual(tree["rows"], [], "判不到的会话不许被当成根")
        self.assertIn("判不到", tree["note"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
