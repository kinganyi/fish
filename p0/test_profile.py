#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主人画像判据（PRD `09` §4.2 · R1-1 / R1-2 / R1-4 / R1-5 / R1-6）。

## 这组判据守的是"画像不会变质"

用户要求画像"**永不衰减**"，而系统里其它一切都会衰减。这就带来一个结构性风险：
**不衰减 = 没有自然清理机制 = 迟早线性膨胀**。`09` §4.2 的原话是
"永不衰减的这类信息，本质上有变质和越来越大的风险"。

所以画像是**固定槽位表**（一格一值，写入是覆盖不是追加），不是"再来一类记忆"。
下面的判据逐个钉住这个设计：
  · P1-1 槽位是**固定表**，写一个不存在的槽必须报错（可扩展无边界 = 退化成追加）
  · P1-2 写入是**覆盖**：同槽再写，行数不变、版本号 +1
  · P1-3 **AI 不得直写**：MCP 面只有 show/propose，没有 set（接口层，不是自觉）
  · P1-4 人写路径通，且写事件（否则重建不了 = D-10 那类事故）
  · P1-5 建议队列：propose 不改画像；accept 才改；reject 不改
  · P1-6 采纳后可回滚（历史在事件里）
  · P1-7 **事件重放保真**（含变异自证）
  · P1-8 注入分级（R1-6）：volatile 必须带 as-of，且**过期不注入**
  · P1-9 提问节律（R1-4）：写够篇数才问；"本轮不回答"要留痕并清零
  · P1-10 文件即界面：改文件 → sync 生效；未改 → 幂等
"""
import json
import os
import sys
import tempfile
import shutil
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish           # noqa: E402
import fish_profile   # noqa: E402
import fish_common    # noqa: E402


class _ProfileBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fish-prof-")
        self.root = Path(self.tmp)
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:      # noqa: BLE001
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def slot_count(self):
        return self.conn.execute("SELECT COUNT(*) FROM profile_slots").fetchone()[0]

    def events_of(self, etype):
        return self.conn.execute(
            "SELECT payload FROM events WHERE type=? ORDER BY seq", (etype,)).fetchall()


# ============================================================ P1-1 固定槽位
class FixedSlotCatalogTest(_ProfileBase):

    def test_P1_1_unknown_group_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            fish_profile.set_slot(self.conn, "不存在的组", "名字", "x")
        msg = str(ctx.exception)
        self.assertIn("称呼", msg, "报错必须列出全部可用分组（失败要给下一步）")

    def test_P1_1_unknown_slot_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            fish_profile.set_slot(self.conn, "称呼", "身高", "180")
        msg = str(ctx.exception)
        self.assertIn("名字", msg, "报错必须列出该组的可用槽位")
        self.assertEqual(self.slot_count(), 0, "被拒绝的写入不许留下行")

    def test_P1_1_slot_count_is_bounded(self):
        """槽位总数是**常量**——这是"不会越来越大的"的结构保证。"""
        total = len(fish_profile.profile_all_slot_keys())
        self.assertEqual(total, sum(len(s) for _g, _p, s in fish_profile.PROFILE_GROUPS))
        self.assertLessEqual(total, 20, "槽位总数有上限（这是设计，不是巧合）")


# ============================================================ P1-2 覆盖不是追加
class OverwriteNotAppendTest(_ProfileBase):

    def test_P1_2_same_slot_overwrites(self):
        fish_profile.set_slot(self.conn, "称呼", "名字", "安逸")
        fish_profile.set_slot(self.conn, "称呼", "名字", "徐安")
        self.assertEqual(self.slot_count(), 1, "同槽再写必须覆盖，不是追加一行")
        s = fish_profile.get_slot(self.conn, "称呼.名字")
        self.assertEqual(s["value"], "徐安")
        self.assertEqual(s["version"], 2, "版本号要 +1（可追溯改过几次）")

    def test_P1_2_two_events_one_row(self):
        """写两次 = 两条事件 + **一行**投影（追加式记忆会是两行）。"""
        fish_profile.set_slot(self.conn, "称呼", "名字", "A")
        fish_profile.set_slot(self.conn, "称呼", "名字", "B")
        self.assertEqual(len(self.events_of(fish_profile.PROFILE_EVENT_EDITED)), 2)
        self.assertEqual(self.slot_count(), 1)


# ============================================================ P1-3 AI 不得直写
class AiCannotWriteTest(unittest.TestCase):
    """**接口层**判据：MCP 面没有写画像的入口。

    R1-2 原话「AI 只能建议，不得直接写画像；违反时拒绝并回'请提建议'」。
    落地方式是"MCP 根本没有那个工具"，所以判据查的是工具清单——
    只查代码里有没有 `if 是AI: 拒绝` 是不够的（那拦不住新增一个工具）。
    """

    def test_P1_3_mcp_has_propose_but_no_set(self):
        import mcp_server
        names = set()
        for t in mcp_server.TOOLS if hasattr(mcp_server, "TOOLS") else []:
            names.add(t.get("name"))
        # 兼容：工具表可能挂在类属性上
        if not names:
            src = Path(mcp_server.__file__).read_text(encoding="utf-8", errors="replace")
            for cand in ("profile_show", "profile_propose", "profile_set"):
                if f'"name": "{cand}"' in src:
                    names.add(cand)
        self.assertIn("profile_propose", names, "AI 必须有提建议的入口")
        self.assertIn("profile_show", names, "AI 必须能读画像")
        self.assertNotIn("profile_set", names,
                         "**AI 不得直接写画像**（R1-2）：MCP 面不该有写入口")

    def test_P1_3_propose_does_not_touch_slots(self):
        tmp = tempfile.mkdtemp(prefix="fish-prof-ai-")
        try:
            conn = fish.connect(Path(tmp))
            conn.executescript(fish.SCHEMA); fish.migrate(conn)
            fish_profile.propose(conn, "生活", "所在地", "成都",
                                 by="some-ai", rationale="推断")
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM profile_slots").fetchone()[0], 0,
                "提建议不许改画像（那是 propose 与 set 的分界线）")
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM profile_proposals").fetchone()[0], 1)
            conn.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ============================================================ P1-4 人写路径
class HumanWriteTest(_ProfileBase):

    def test_P1_4_set_writes_slot_and_event(self):
        res = fish_profile.set_slot(self.conn, "工作", "身份", "PPT 从业者",
                                    actor="human-anyi", reason="初始")
        self.assertEqual(res["new"], "PPT 从业者")
        self.assertEqual(self.slot_count(), 1)
        ev = self.events_of(fish_profile.PROFILE_EVENT_EDITED)
        self.assertEqual(len(ev), 1, "写入必须落事件（否则重建不保真）")
        payload = json.loads(ev[0][0])
        self.assertEqual(payload["slot_key"], "工作.身份")
        self.assertEqual(payload["new"], "PPT 从业者")

    def test_P1_4_remove_clears_but_keeps_history(self):
        fish_profile.set_slot(self.conn, "称呼", "名字", "安逸")
        fish_profile.remove_slot(self.conn, "称呼.名字", actor="human-anyi")
        self.assertIsNone(fish_profile.get_slot(self.conn, "称呼.名字"))
        evs = self.events_of(fish_profile.PROFILE_EVENT_EDITED)
        self.assertEqual(len(evs), 2, "清空也要落事件（历史不能丢）")
        self.assertEqual(json.loads(evs[1][0])["action"], "remove")


# ============================================================ P1-5 建议队列
class ProposalQueueTest(_ProfileBase):

    def _pid(self):
        return fish_profile.propose(self.conn, "生活", "所在地", "成都",
                                    by="ai-x", rationale="从对话里看出来的")

    def test_P1_5_pending_does_not_change(self):
        self._pid()
        self.assertIsNone(fish_profile.get_slot(self.conn, "生活.所在地"))
        self.assertEqual(len(fish_profile.list_proposals(self.conn)), 1)

    def test_P1_5_accept_changes(self):
        pid = self._pid()
        fish_profile.accept_proposal(self.conn, pid, by="human-anyi")
        self.assertEqual(fish_profile.get_slot(self.conn, "生活.所在地")["value"], "成都")
        self.assertEqual(fish_profile.list_proposals(self.conn), [],
                         "采纳后不该还留在 pending 里")
        # 两个事件都要有：edited（改值）+ accepted（审计）
        self.assertEqual(len(self.events_of(fish_profile.PROFILE_EVENT_EDITED)), 1)
        self.assertEqual(len(self.events_of(fish_profile.PROFILE_EVENT_ACCEPTED)), 1)

    def test_P1_5_reject_does_not_change(self):
        pid = self._pid()
        fish_profile.reject_proposal(self.conn, pid, by="human-anyi", note="不对")
        self.assertIsNone(fish_profile.get_slot(self.conn, "生活.所在地"),
                          "拒绝必须**不改**画像")
        self.assertEqual(len(self.events_of(fish_profile.PROFILE_EVENT_EDITED)), 0)
        self.assertEqual(len(self.events_of(fish_profile.PROFILE_EVENT_REJECTED)), 1)

    def test_P1_5_cannot_decide_twice(self):
        pid = self._pid()
        fish_profile.accept_proposal(self.conn, pid, by="human-anyi")
        with self.assertRaises(ValueError):
            fish_profile.reject_proposal(self.conn, pid, by="human-anyi")

    def test_P1_5_new_proposal_supersedes_old(self):
        first = self._pid()
        second = fish_profile.propose(self.conn, "生活", "所在地", "绵阳", by="ai-y")
        self.assertNotEqual(first, second)
        statuses = {p["proposal_id"]: p["status"]
                    for p in fish_profile.list_proposals(self.conn, status="superseded")}
        self.assertIn(first, statuses, "同槽新建议应把旧建议置 superseded")


# ============================================================ P1-6 可回滚
class RollbackTest(_ProfileBase):

    def test_P1_6_can_revert_by_setting_old_value(self):
        fish_profile.set_slot(self.conn, "称呼", "名字", "安逸")
        fish_profile.set_slot(self.conn, "称呼", "名字", "徐安")
        fish_profile.set_slot(self.conn, "称呼", "名字", "安逸", reason="回滚")
        self.assertEqual(fish_profile.get_slot(self.conn, "称呼.名字")["value"], "安逸")
        evs = [json.loads(r[0]) for r in self.events_of(fish_profile.PROFILE_EVENT_EDITED)]
        self.assertEqual([e["new"] for e in evs], ["安逸", "徐安", "安逸"],
                         "三次改动都必须在事件里（可回滚的前提是历史不丢）")


# ============================================================ P1-7 重建保真
class ReplayFidelityTest(_ProfileBase):

    def _seed(self):
        fish_profile.set_slot(self.conn, "称呼", "名字", "安逸")
        fish_profile.set_slot(self.conn, "工作", "身份", "PPT 从业者")
        pid = fish_profile.propose(self.conn, "生活", "所在地", "成都", by="ai")
        fish_profile.accept_proposal(self.conn, pid, by="human")
        fish_profile.remove_slot(self.conn, "工作.身份")

    def test_P1_7_replay_reconstructs(self):
        self._seed()
        state = fish_profile.replay_slots_from_events(self.conn)
        self.assertEqual(state, {"称呼.名字": "安逸", "生活.所在地": "成都"},
                         "重放结果必须与投影一致（remove 也要重放）")

    def test_P1_7_rebuild_fixes_tampered_projection(self):
        """投影被改坏 → 重建必须校正回来（这是投影可以坏、事件不会坏的意思）。"""
        self._seed()
        self.conn.execute("UPDATE profile_slots SET value='被篡改'"
                          " WHERE slot_key='称呼.名字'")
        self.conn.commit()
        rep = fish_profile.rebuild_slots(self.conn)
        self.assertEqual(rep["fixed"], 1, f"应校正 1 处，实际 {rep}")
        self.assertEqual(fish_profile.get_slot(self.conn, "称呼.名字")["value"], "安逸")

    def test_P1_7_rebuild_is_idempotent(self):
        self._seed()
        fish_profile.rebuild_slots(self.conn)
        rep2 = fish_profile.rebuild_slots(self.conn)
        self.assertEqual(rep2["fixed"], 0, "第二次重建不该再改任何东西")


# ============================================================ P1-8 注入分级
class InjectionGradingTest(_ProfileBase):

    def test_P1_8_stable_always_and_volatile_with_as_of(self):
        fish_profile.set_slot(self.conn, "称呼", "名字", "安逸")
        fish_profile.set_slot(self.conn, "工作", "身份", "PPT 从业者")
        fish_profile.set_slot(self.conn, "近期动态", "在忙什么", "债务清理")
        block = fish_profile.render_profile_block(self.conn)
        self.assertIn("安逸", block)
        self.assertIn("as-of", block, "volatile 槽位**必须**带时效标记（R1-6）")

    def test_P1_8_expired_volatile_not_injected(self):
        """过期的"近期动态"**不注入**——这正是用户担心的"变质"。"""
        fish_profile.set_slot(self.conn, "称呼", "名字", "安逸")
        fish_profile.set_slot(self.conn, "近期动态", "在忙什么", "三个月前的事")
        self.conn.execute("UPDATE profile_slots SET as_of='2020-01-01T00:00:00Z'"
                          " WHERE slot_key='近期动态.在忙什么'")
        self.conn.commit()
        block = fish_profile.render_profile_block(self.conn)
        self.assertNotIn("三个月前的事", block, "过期动态被当成'当下'注入就是误导")
        self.assertIn("安逸", block, "stable 槽位不受时效影响")

    def test_P1_8b_every_lasting_group_gets_a_representative(self):
        """默认注入必须**每组至少一条**——不许有哪一组永远进不来。

        ## 这条判据守的是一个真实缺陷（2026-11-11 实测暴露）

        第一版按"目录顺序"取前 3 条 lasting。目录顺序是 生活→工作→性格，
        于是把 9 格填满之后，注入包里有 所在地/身份/业务线，而
        **沟通偏好 / 决策风格 / 价值观 一条都没有**——偏偏那三条最影响 AI 怎么做事。

        第二版改成"纯按序号取前 3"，结果反过来：性格三条全占满，
        **「工作·身份」被挤掉**，AI 连这个人是干什么的都不知道。

        两个极端都不对。现在的规则是**分组轮转**：每组各取一条，覆盖所有 lasting 组。
        这条判据钉住"不许回到任何一种极端"。
        """
        # **必须把每个 lasting 组填满**，否则这条判据测不出东西。
        # 第一版只填每组一条，结果"按目录顺序取前 3"恰好也能覆盖三组——
        # 变异测试一跑就发现判据没红（判据自己先失明了）。
        # 填满之后，目录顺序会把前 3 条全给「生活」组，工作/性格被排除 → 才能红。
        got_groups = []
        for g, perm, slots in fish_profile.PROFILE_GROUPS:
            if perm != "lasting":
                continue
            for name, _rank in slots:
                fish_profile.set_slot(self.conn, g, name, f"{g}-{name}")
            got_groups.append(g)
        self.assertGreaterEqual(len(got_groups), 3, "lasting 组应有 3 个")
        block = fish_profile.render_profile_block(self.conn)
        for g in got_groups:
            self.assertIn(f"{g}·", block,
                          f"「{g}」组一条都没进默认注入——某个组被系统性排除了")

    def test_P1_8c_personality_outranks_location(self):
        """性格组必须排在生活组之前（越影响相处越靠前），而不是按目录字母序。"""
        order = {g: min(r for _n, r in slots)
                 for g, _p, slots in fish_profile.PROFILE_GROUPS}
        self.assertLess(order["称呼"], order["性格"])
        self.assertLess(order["性格"], order["生活"],
                        "性格组必须排在生活组之前：沟通偏好/决策风格比'他在哪'重要")
        self.assertLess(order["性格"], order["工作"])

    def test_P1_8_empty_profile_renders_nothing(self):
        """空画像连标题都不输出（注入包里出现空壳段 = 噪音 + 白花预算）。"""
        self.assertEqual(fish_profile.render_profile_block(self.conn), "")


# ============================================================ P1-9 提问节律
class QuestionCadenceTest(_ProfileBase):

    def test_P1_9_due_only_after_threshold(self):
        every = fish_common.get_config(self.conn, "profile.ask_every")
        self.assertEqual(every, 10, "缺省 10 篇（用户原话）")
        self.assertFalse(fish_profile.question_due(self.conn))
        for _ in range(every - 1):
            fish_profile.bump_question_counter(self.conn)
        self.assertFalse(fish_profile.question_due(self.conn), "差一篇不该问")
        fish_profile.bump_question_counter(self.conn)
        self.assertTrue(fish_profile.question_due(self.conn), "到点必须问")

    def test_P1_9_defer_resets_and_leaves_trace(self):
        for _ in range(10):
            fish_profile.bump_question_counter(self.conn)
        self.assertTrue(fish_profile.question_due(self.conn))
        res = fish_profile.reset_question_counter(self.conn, status="deferred",
                                                 by="human-anyi")
        self.assertEqual(res["trigger_count"], 10)
        self.assertFalse(fish_profile.question_due(self.conn), "延期后必须清零")
        evs = self.events_of(fish_profile.PROFILE_EVENT_DEFERRED)
        self.assertEqual(len(evs), 1, "**本轮不回答也要留痕**（用户原话允许不回答）")
        self.assertEqual(json.loads(evs[0][0])["trigger_count"], 10)

    def test_P1_9_block_mentions_not_answering(self):
        for _ in range(10):
            fish_profile.bump_question_counter(self.conn)
        block = fish_profile.render_questions_block(self.conn)
        self.assertIn("不回答", block, "提问块里必须明说'可以不回答'")
        self.assertIn("profile defer", block, "要给出不回答的确切命令")


# ============================================================ P1-10 文件即界面
class FileAsUiTest(_ProfileBase):

    def test_P1_10_parse_is_tolerant(self):
        """人的编辑面必须宽容：注释、空行、不是槽位的行都不许把解析弄崩。"""
        text = ("# 主人画像\n\n## 称呼\n- 名字: 安逸\n"
                "随便写点什么都行\n- 怎么叫我: 小安\n## 不存在的组\n- 槽: 值\n")
        got = fish_profile.parse_markdown(text)
        self.assertEqual(got.get("称呼.名字"), "安逸")
        self.assertEqual(got.get("称呼.怎么叫我"), "小安")
        self.assertFalse([k for k in got if k.startswith("不存在")],
                         "不认识的组要**忽略**而不是报错（人的编辑面不能一改就崩）")

    def test_P1_10_sync_applies_and_is_idempotent(self):
        fish_profile.set_slot(self.conn, "称呼", "名字", "安逸")
        text = fish_profile.render_markdown(self.conn)
        rep1 = fish_profile.sync_from_text(self.conn, text)
        self.assertEqual(rep1["changed"], [], "没改文件时 sync 不该产生任何改动")
        # 人改了一格
        changed = text.replace("- 名字: 安逸", "- 名字: 徐安")
        rep2 = fish_profile.sync_from_text(self.conn, changed)
        self.assertIn("称呼.名字", rep2["changed"])
        self.assertEqual(fish_profile.get_slot(self.conn, "称呼.名字")["value"], "徐安")
        rep3 = fish_profile.sync_from_text(self.conn, changed)
        self.assertEqual(rep3["changed"], [], "再 sync 一次必须幂等")

    def test_P1_10_blank_line_clears_slot(self):
        fish_profile.set_slot(self.conn, "称呼", "名字", "安逸")
        text = fish_profile.render_markdown(self.conn).replace("- 名字: 安逸", "- 名字: ")
        rep = fish_profile.sync_from_text(self.conn, text)
        self.assertIn("称呼.名字", rep["cleared"])
        self.assertIsNone(fish_profile.get_slot(self.conn, "称呼.名字"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
