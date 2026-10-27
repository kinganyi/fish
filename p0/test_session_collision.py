#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""撞车自撞修复 · T03 判据（判定五处：把「自己」从会话维度上摘干净）。

## 来源

设计：`45-撞车自撞修复-设计.md` §3（逐处判定改动）+ §3.2/§3.3（两张期望行为表）
+ §7.1（判据清单）。核心一句话：「一个公民 = 一个执行体」这个假设失效之后，
"谁"必须精确到 `(citizen_id, session_key)`——少改一处，误报就会换个入口回来。

## 判据清单

  S4  只有一行在场 → 不报重叠
  S5  同公民**不同会话**声明同一面 → 要报，且标 `same_citizen`
  S6  不同公民声明同一面 → 照报（回归：`a`/`b` 仍是 citizen_id、单会话无会话后缀）
  S7  同会话「声明 vs 实际」不算重叠（并用"复制成两行"验证防御式跳过）
  S8  同公民**另一会话**的实际面 → 报 `kind=mixed`
  S9  推送候选排除**自己的会话**
  S10 推送候选包含**同公民另一会话**（标题带会话短标识）
  S12 `find_collisions` 报同公民另一会话（标签带短标识、不标 unlabeled）
  S13 快照的 `session_key` 来自**列**（不是 agent_id 推断）
  S22 「在动但未签到」列出同公民**另一会话**（旧实现会漏）
  S23 `''` 的三态（对 `''` 不报；对同公民有值报且标 `session_unlabeled`；对另一公民报）
  S25 兜底 F1 可观测（`agent_id:legacy` / `column` / `none`）
  S26 兜底 F2 第三档不退让（有会话键但该会话没活动 → 空，不借同公民别的会话的面）
  S27 兜底 F3 不崩（行里缺 `session_key`/`session_short` 时三条判定全程无 KeyError）
"""
import datetime
import os
import posixpath
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fish  # noqa: E402
import fish_activity  # noqa: E402

CID_A = "01SESSIONCOLLIDE00000A1"
CID_B = "01SESSIONCOLLIDE00000B2"


def _ts(minutes_ago=0):
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class CollisionBase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-session-collide-"))
        self.root = self.tmp / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES ('PJ','大肥鱼-测试',NULL,0,'active')")
        self.conn.execute(
            "INSERT INTO workspace_map (path_prefix,project,note,created_at) "
            "VALUES ('i:/proj','PJ',NULL,?)", (fish.now_utc(),))
        for cid, name in ((CID_A, "AI-甲"), (CID_B, "AI-乙")):
            self.conn.execute(
                "INSERT INTO citizens (citizen_id,display_name,kind,status,created_at,host)"
                " VALUES (?,?,'resident','active',?,'dsh')",
                (cid, name, fish.now_utc()))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- 夹具
    def _checkin(self, cid, session_key, surface="", doing="干活", project="PJ"):
        return fish.board_checkin(self.conn, cid, doing, surface=surface,
                                  scope="project", scope_key=project,
                                  session_key=session_key)

    def _activity(self, cid, session_key, path_rel="p0/fish.py", *, project="PJ",
                  minutes_ago=0, count=1):
        """造写动作事实：`path_dir` 是展示列，`path_abs` 是判定列。"""
        for _ in range(count):
            self.conn.execute(
                "INSERT INTO tool_activity (host, session_key, citizen_id, project, "
                "tool_name, action, path, path_dir, path_abs, args_digest, ok, seen_at) "
                "VALUES ('dsh',?,?,?,'write','write',?,?,?,'x',1,?)",
                (session_key, cid, project, path_rel, posixpath.dirname(path_rel),
                 f"i:/proj/{path_rel}", _ts(minutes_ago)))
        self.conn.commit()

    def _snapshot(self):
        return fish_activity.build_snapshot(
            self.conn, out_path=self.root / fish_activity.SNAPSHOT_RELPATH,
            window_min=90)


class OverlapJudgeTest(CollisionBase):
    """S4 / S5 / S6：在场者之间的声明重叠。"""

    def test_S4_single_row_never_overlaps_itself(self):
        board = self._checkin(CID_A, "dsh:aaa1111", surface="p0/fish.py")
        self.assertEqual(fish.board_overlaps(self.conn, board), [],
                         "只有一行时不可能有重叠（旧实现里这一行会跟自己比）")

    def test_S5_same_citizen_two_sessions_overlap(self):
        self._checkin(CID_A, "dsh:aaa1111", surface="p0/fish.py")
        board = self._checkin(CID_A, "dsh:bbb2222", surface="p0/fish.py")
        ovs = fish.board_overlaps(self.conn, board)
        self.assertEqual(len(ovs), 1, f"同公民两会话声明同一面必须报一条：{ovs}")
        one = ovs[0]
        self.assertTrue(one["same_citizen"], "同一公民内部的重叠要能标出来")
        self.assertNotEqual(one["a_session"], one["b_session"],
                            "两个不同的会话才是真撞车（同一会话不该出现）")
        self.assertEqual({one["a"], one["b"]}, {CID_A}, "`a`/`b` 仍是公民 ID（机器面契约）")
        self.assertIn("·dsh#", one["a_label"] + one["b_label"],
                      "同公民多会话的标签要带会话短标识，否则看板认不出是两个窗口")

    def test_S6_different_citizens_still_reported(self):
        """回归：跨公民的行为**一条都不许变**。"""
        self._checkin(CID_A, "dsh:aaa1111", surface="p0/fish.py")
        board = self._checkin(CID_B, "dsh:bbb2222", surface="p0/fish.py")
        ovs = fish.board_overlaps(self.conn, board)
        self.assertEqual(len(ovs), 1, f"跨公民的重叠必须照报：{ovs}")
        one = ovs[0]
        self.assertFalse(one["same_citizen"])
        self.assertEqual({one["a"], one["b"]}, {CID_A, CID_B})
        self.assertNotIn("·dsh#", one["a_label"] + one["b_label"],
                         "单会话公民不该出现会话后缀（既有文本保持不变）")


class ObservedJudgeTest(CollisionBase):
    """S7 / S8：声明 ∪ 实际的推断重叠。"""

    def test_S7_same_session_declared_vs_actual_not_overlap(self):
        board = self._checkin(CID_A, "dsh:aaa1111", surface="i:/proj/p0")
        self._activity(CID_A, "dsh:aaa1111", "p0/fish.py")
        self.assertEqual(fish.board_observed_overlaps(self.conn, board), [],
                         "自己的声明 vs 自己的实际不是重叠")
        # 防御式判据（设计 §3.5 第 3 条）：把同一行复制成两行（模拟将来拆"声明/实际"行），
        # 同 `(citizen, session)` 的两行之间仍不许报——否则拆行那天会立刻自撞。
        original = fish.board_presence_rows

        def duplicated(conn, b):
            rows = original(conn, b)
            return rows + [dict(row) for row in rows]

        try:
            fish.board_presence_rows = duplicated
            self.assertEqual(fish.board_observed_overlaps(self.conn, board), [],
                             "同一 (citizen, session) 的两行之间不算重叠")
            self.assertEqual(fish.board_overlaps(self.conn, board), [],
                             "`board_overlaps` 也必须跳过同键对")
        finally:
            fish.board_presence_rows = original

    def test_S8_other_session_actual_is_reported(self):
        self._checkin(CID_A, "dsh:aaa1111", surface="i:/proj/p0")
        board = self._checkin(CID_A, "dsh:bbb2222")
        self._activity(CID_A, "dsh:bbb2222", "p0/x.py")
        ovs = fish.board_observed_overlaps(self.conn, board)
        mixed = [o for o in ovs if o["kind"] == "mixed"]
        self.assertEqual(len(mixed), 1, f"同公民另一会话的实际面必须报出来：{ovs}")
        self.assertTrue(mixed[0]["same_citizen"])
        # 谁在 a、谁在 b 取决于在场表的排序（按 last_seen 倒序），所以比对**两面**
        self.assertEqual({mixed[0]["a_session"], mixed[0]["b_session"]},
                         {"dsh:aaa1111", "dsh:bbb2222"})


class PushCandidateTest(CollisionBase):
    """S9 / S10：推送候选的排除键。"""

    def test_S9_candidates_exclude_own_session(self):
        self._checkin(CID_A, "dsh:aaa1111", surface="p0/fish.py")
        self._activity(CID_A, "dsh:aaa1111", "p0/fish.py")
        cands = fish._activity_overlap_candidates(
            self.conn, project="PJ", citizen_id=CID_A, session_key="dsh:aaa1111")
        self.assertEqual(cands, [], f"自己那一行不许变成自己的候选：{cands}")

    def test_S9_other_session_declaration_is_not_mine(self):
        """M1 的靶点：同公民**另一会话**声明的面，绝不能被当成"我的声明"。

        现场代价：我（S1）根本没声明过 `p0/fish.py`，是我另一个窗口（S2）声明的；
        别人写那块时如果按公民合并声明面，我收到一句「你也占了这块」——
        而我无法据此判断该不该让路（那句话是假的）。
        """
        self._checkin(CID_A, "dsh:bbb2222", surface="p0/fish.py")
        self._activity(CID_B, "dsh:other1", "p0/fish.py")
        cands = fish._activity_overlap_candidates(
            self.conn, project="PJ", citizen_id=CID_A, session_key="dsh:aaa1111")
        self.assertEqual(cands, [],
                         f"我没声明过那块（是我另一个窗口声明的）→ 不该收到候选：{cands}")

    def test_S10_candidates_include_other_session_of_same_citizen(self):
        self._checkin(CID_A, "dsh:aaa1111", surface="p0/fish.py")
        self._activity(CID_A, "dsh:bbb2222", "p0/fish.py")
        cands = fish._activity_overlap_candidates(
            self.conn, project="PJ", citizen_id=CID_A, session_key="dsh:aaa1111")
        self.assertEqual(len(cands), 1, f"同公民另一会话的写入必须报出来：{cands}")
        title = cands[0]["title"]
        self.assertIn("·dsh#", title, f"标题要带会话短标识：{title}")
        self.assertIn("dsh#bbb2", title, "标题要能指向**那个**会话（4 位短标识）")


class SnapshotJudgeTest(CollisionBase):
    """S12 / S13 / S23 / S25：快照条目与 `find_collisions`。"""

    def test_S12_find_collisions_reports_other_session(self):
        self._checkin(CID_A, "dsh:aaa1111", surface="i:/proj/p0/fish.py")
        self._checkin(CID_A, "dsh:bbb2222", surface="i:/proj/p0/fish.py")
        snap = self._snapshot()
        hits = fish_activity.find_collisions(
            snap, target_abs="i:/proj/p0/fish.py", own_session_key="dsh:aaa1111",
            min_segments=2)
        self.assertEqual(len(hits), 1, f"只该报另一会话那一条：{hits}")
        self.assertEqual(hits[0]["session_key"], "dsh:bbb2222")
        self.assertIn("·dsh#bbb2", hits[0]["label"])
        self.assertFalse(hits[0]["session_unlabeled"], "有会话键的行不该被标成未标注")

    def test_S13_snapshot_key_comes_from_column(self):
        # 关键：让**列与旧列不同**。只写同一个值时"列优先"与"从 agent_id 推断"
        # 会给出一样的答案 → 判据恒绿（变异 M3 会证明这一点）。
        fish.board_checkin(self.conn, CID_A, "改 fish 判定", surface="p0/fish.py",
                           scope="project", scope_key="PJ",
                           session_key="dsh:column1", agent_id="dsh:legacy9")
        snap = self._snapshot()
        self.assertEqual(len(snap["entries"]), 1)
        entry = snap["entries"][0]
        self.assertEqual(entry["session_key"], "dsh:column1",
                         "快照的会话键必须来自列（旧实现靠 agent_id 里有没有冒号推断）")
        self.assertEqual(entry["session_key_source"], "column",
                         "来源记 column；记成 agent_id:legacy 说明列没被优先读")

    def test_S25_legacy_agent_id_fallback_is_observable(self):
        board = self._checkin(CID_A, "", surface="p0/fish.py")
        self.conn.execute("UPDATE presence SET agent_id='dsh:legacy1' WHERE board_id=?",
                          (board["id"],))
        self.conn.commit()
        entry = self._snapshot()["entries"][0]
        self.assertEqual(entry["session_key"], "dsh:legacy1",
                         "F1：列空时用 `agent_id` 兜底（既有判据就是靠这条注入会话键的）")
        self.assertEqual(entry["session_key_source"], "agent_id:legacy",
                         "来源必须可观测（不静默）")
        self.conn.execute("UPDATE presence SET agent_id=NULL")
        self.conn.commit()
        entry = self._snapshot()["entries"][0]
        self.assertEqual(entry["session_key"], "")
        self.assertEqual(entry["session_key_source"], "none")

    def test_S23_blank_session_three_states(self):
        # ① '' vs 有值（同公民）：报，但标 session_unlabeled
        self._checkin(CID_A, "", surface="i:/proj/p0/fish.py")
        self._checkin(CID_A, "dsh:bbb2222", surface="i:/proj/docs")
        snap = self._snapshot()
        hits = fish_activity.find_collisions(
            snap, target_abs="i:/proj/p0/fish.py", own_session_key="dsh:zzz9999",
            min_segments=2)
        unlabeled = [h for h in hits if h["session_unlabeled"]]
        self.assertEqual(len(unlabeled), 1, f"无会话归属的行要照报并标注：{hits}")
        self.assertEqual(unlabeled[0]["session_key"], "")
        # ② '' vs ''：同一参与方，不报
        hits2 = fish_activity.find_collisions(
            snap, target_abs="i:/proj/p0/fish.py", own_session_key="", min_segments=2)
        self.assertEqual([h for h in hits2 if not h["session_key"]], [],
                         "'' 与 '' 是同一参与方（两次无会话判定不该互相报警）")
        # ③ '' vs 另一公民：照报
        self._checkin(CID_B, "", surface="i:/proj/p0/fish.py")
        snap = self._snapshot()
        hits3 = fish_activity.find_collisions(
            snap, target_abs="i:/proj/p0/fish.py", own_session_key="dsh:zzz9999",
            min_segments=2)
        self.assertEqual({h["citizen_id"] for h in hits3}, {CID_A, CID_B},
                         f"两个无会话归属的**不同公民**仍要互相报：{hits3}")


class ActualSurfacesTest(CollisionBase):
    """S22 / S26 / S27：实际面的取数与边界。"""

    def test_S22_unchecked_lists_other_session(self):
        board = self._checkin(CID_A, "dsh:aaa1111", surface="p0/fish.py")
        self._activity(CID_A, "dsh:bbb2222", "p0/fish.py")
        data = fish.board_unchecked_sessions(self.conn, board)
        kept = [s["session_key"] for s in data["sessions"]]
        self.assertEqual(kept, ["dsh:bbb2222"],
                         f"同公民另一会话在动而没签到 → 要列出来：{data['sessions']}")

    def test_S22_checked_session_is_not_listed_again(self):
        board = self._checkin(CID_A, "dsh:aaa1111", surface="p0/fish.py")
        self._activity(CID_A, "dsh:aaa1111", "p0/fish.py")
        data = fish.board_unchecked_sessions(self.conn, board)
        self.assertEqual(data["sessions"], [], f"已签到的会话不许被重复列：{data}")

    def test_S26_session_without_activity_gets_no_borrowed_dirs(self):
        board = self._checkin(CID_A, "dsh:X", surface="p0/fish.py")
        self._activity(CID_A, "dsh:Y", "p0/fish.py")
        rows = fish.board_presence_rows(self.conn, board)
        mine = [r for r in rows if r["session_key"] == "dsh:X"][0]
        self.assertEqual(mine["auto_dirs"], [],
                         "该会话自己没有活动 → 空；不许拿同公民另一会话的面冒充（F2 第三档）")
        other = [r for r in rows if r["session_key"] == "dsh:Y"]
        self.assertEqual(other, [], "Y 没有签到行 → 不在场表里（未登记 = 未发生）")

    def test_S27_rows_without_session_fields_do_not_crash(self):
        board = self._checkin(CID_A, "dsh:X", surface="p0/fish.py")
        self._checkin(CID_B, "dsh:Y", surface="p0/fish.py")
        original = fish.board_presence_rows

        def stripped(conn, b):
            rows = original(conn, b)
            for row in rows:
                row.pop("session_key", None)      # 既有判据会 monkeypatch 造"没有新字段"的行
                row.pop("session_short", None)
            return rows

        try:
            fish.board_presence_rows = stripped
            self.assertEqual(len(fish.board_overlaps(self.conn, board)), 1,
                             "缺字段时按 '' 处理，判定仍成立（F3：读侧一律 .get()）")
            fish.board_observed_overlaps(self.conn, board)
            block = fish.board_block(self.conn, "PJ")
            self.assertIn("看板", block)
        finally:
            fish.board_presence_rows = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
