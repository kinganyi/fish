#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T03 判据 · 看板（自动在场 + 两栏 + 超时分档 + 可读名）。

## 用户 2026-10-26 拍板两条，这里各钉一组判据

1. **看板「自动行」算登记** → SessionStart/写记忆建的行就是"已登记"，
   软门不再说"你还没签到"；但自动行过旧时要提醒**补一句 doing**。
2. **看板显示可读 AI 名 + 项目名，不要 ULID** → 渲染文件/注入包里 26 位公民 ID
   命中 0；重名用 `名字·host#2` 消歧；当前会话缺名时给一行**可执行**提示。

## 判据清单

  R1  自动签到：无行 → 建（doing 标「自动登记」）；有自动行 → 刷新时间；**不覆盖手工 doing**
  R2  软门：新鲜自动行 → `(True, None)`；过旧自动行 → `False` + 含「分钟没更新」+「自动登记」
  R3  可读名：渲染文件 ULID 命中 0；重名两行标签不同且都不含 ID
  R4  两栏：「实际在动」出目录与次数；声明面仍在（不丢信息）
  R5  超时分档：40 分钟前动作 → `offline（推断）`；刚动 → `active`
  R6  注入段：含项目名/两栏/最多 3 人；`meta.layers.board` 在合理区间
  R7  容量：新列之后 32KB 判据仍绿（造 500 条动态）
  R8  变异：把 who_label 改回短 id → R3 红；自动刷新覆盖 doing → R1 红；阈值改 0 → R5 红
  R9  自动签到后**重渲染**：签到 → 看板文件里能看到刚签到的自动行（QA Round 1 · E2）
"""
import datetime
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fish  # noqa: E402
import fish_naming  # noqa: E402
import fish_surface  # noqa: E402

ULID_RE = re.compile(r"(?<![0-9A-HJKMNP-TV-Z])[0-9A-HJKMNP-TV-Z]{26}(?![0-9A-HJKMNP-TV-Z])")
CID_A = "01BOARDREADABLE00000000A1"
CID_B = "01BOARDREADABLE00000000B2"


def _ts(minutes_ago=0):
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class ReadableBase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-board-rd-"))
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
        for cid, name, host in ((CID_A, "AI-甲", "dsh"), (CID_B, "AI-乙", "dsh")):
            self.conn.execute(
                "INSERT INTO citizens (citizen_id,display_name,kind,status,created_at,host)"
                " VALUES (?,?,'resident','active',?,?)", (cid, name, fish.now_utc(), host))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _activity(self, citizen, directory="p0", minutes_ago=0, count=3):
        for i in range(count):
            self.conn.execute(
                "INSERT INTO tool_activity (host, session_key, citizen_id, project, "
                "tool_name, action, path, path_dir, path_abs, args_digest, ok, seen_at) "
                "VALUES ('dsh','dsh:s',?,'PJ','write','write',?,?,?,'x',1,?)",
                (citizen, f"{directory}/f{i}.py", directory,
                 f"i:/proj/{directory}/f{i}.py", _ts(minutes_ago)))
        self.conn.commit()


class AutoCheckinTest(ReadableBase):
    """R1：自动行算登记，但**绝不覆盖人的声明**。"""

    def test_creates_auto_row(self):
        res = fish.board_auto_checkin(self.conn, CID_A, project="PJ", source="session_start")
        self.assertTrue(res["created"])
        board = fish.board_ensure(self.conn, "project", "PJ")
        doing, auto_source, surface = self.conn.execute(
            "SELECT doing, auto_source, surface FROM presence WHERE board_id=? "
            "AND citizen_id=?", (board["id"], CID_A)).fetchone()
        self.assertIn("自动登记", doing)
        self.assertIn("会话开始", doing, "来源要写进文案（能看出是谁签的）")
        self.assertEqual(auto_source, "session_start")
        self.assertEqual(surface, "", "自动行**不编造**占用面")

    def test_refresh_keeps_auto_placeholder_fresh(self):
        fish.board_auto_checkin(self.conn, CID_A, project="PJ", source="session_start")
        self.conn.execute("UPDATE presence SET last_seen_at=?, auto_seen_at=?",
                          (_ts(90), _ts(90)))
        self.conn.commit()
        res = fish.board_auto_checkin(self.conn, CID_A, project="PJ", source="session_start")
        self.assertTrue(res["refreshed"])
        board = fish.board_ensure(self.conn, "project", "PJ")
        last = self.conn.execute(
            "SELECT last_seen_at FROM presence WHERE board_id=? AND citizen_id=?",
            (board["id"], CID_A)).fetchone()[0]
        self.assertGreater(last, _ts(5), "自动行要被刷新（否则软门一直说过旧）")

    def test_never_overwrites_manual_doing(self):
        fish.board_checkin(self.conn, CID_A, "我正在改 fish 的判定逻辑",
                           surface="p0/fish.py", scope="project", scope_key="PJ")
        fish.board_auto_checkin(self.conn, CID_A, project="PJ", source="remember")
        board = fish.board_ensure(self.conn, "project", "PJ")
        doing, surface = self.conn.execute(
            "SELECT doing, surface FROM presence WHERE board_id=? AND citizen_id=?",
            (board["id"], CID_A)).fetchone()
        self.assertIn("认真地", doing + "认真地")     # 文案本身不该被改写
        self.assertEqual(doing, "我正在改 fish 的判定逻辑")
        self.assertEqual(surface, "p0/fish.py")

    def test_config_off_disables_auto(self):
        fish.set_config(self.conn, "board.auto_presence", False)
        res = fish.board_auto_checkin(self.conn, CID_A, project="PJ")
        self.assertFalse(res["created"])
        self.assertIn("auto_presence", res["skipped"] or "")

    def test_remember_fallback_delegates(self):
        """`board_auto_presence` 只是 `board_auto_checkin(source='remember')` 的薄包装。"""
        self.assertTrue(fish.board_auto_presence(self.conn, CID_A, "PJ"))
        self.assertFalse(fish.board_auto_presence(self.conn, CID_A, "PJ"),
                         "已存在行时返回 False（保持旧返回语义）")
        board = fish.board_ensure(self.conn, "project", "PJ")
        doing = self.conn.execute(
            "SELECT doing FROM presence WHERE board_id=? AND citizen_id=?",
            (board["id"], CID_A)).fetchone()[0]
        self.assertIn("正在写记忆", doing)


class AutoRenderTest(ReadableBase):
    """R9：自动签到之后**顺手重渲染看板文件**（QA Round 1 · E2）。

    真实现场：`presence` 已是 1 人，`projects/TESTP/看板-*.md` 仍写「在场（0 人）」——
    同一份事实两个读数，读文件的人会以为没人开工。判据落在**文件内容**上，
    因为"有没有渲染"只有文件能证明（DB 侧本来就是对 的）。
    """

    def _file_text(self, scope, key=""):
        board = fish.board_ensure(self.conn, scope, key)
        path = fish.board_out_path(self.root, board)
        self.assertTrue(path.exists(), f"看板文件没被渲染：{board['path']}")
        return path.read_text(encoding="utf-8")

    def test_project_board_rendered_after_auto_checkin(self):
        done = fish._auto_checkin_after_start(
            self.conn, {"citizen_id": CID_A, "projects": ["PJ"],
                        "session_key": "dsh:s1"}, "i:/proj")
        self.assertTrue(done and done[0][1], f"自动签到应建行：{done}")
        text = self._file_text("project", "PJ")
        self.assertIn("AI-甲", text, f"渲染文件里没有刚签到的自动行：{text[:300]}")
        self.assertIn("自动登记", text)
        self.assertEqual(ULID_RE.findall(text), [], "渲染文件仍不许出 ULID")
        root_text = self._file_text("root")
        self.assertIn("AI-甲", root_text, "根板必须跟着刷（由 board_render 内部处理）")
        self.assertIn("1 人", root_text, f"根板读数必须是 1 人：{root_text[:300]}")

    def test_root_only_checkin_renders_root_file(self):
        """没有项目时签根板：根板文件同样要被渲染（否则根板永远停在 0 人）。"""
        fish._auto_checkin_after_start(
            self.conn, {"citizen_id": CID_A, "projects": [], "session_key": None}, None)
        root_text = self._file_text("root")
        self.assertIn("AI-甲", root_text)
        self.assertIn("自动登记", root_text)

    def test_without_render_the_file_stays_stale(self):
        """变异自证：把渲染这一步掐掉 → 上面那条判据必须变红（证明它有判别力）。"""
        with mock.patch.object(fish, "_render_board_quietly", lambda *a, **k: False):
            fish._auto_checkin_after_start(
                self.conn, {"citizen_id": CID_A, "projects": ["PJ"],
                            "session_key": "dsh:s1"}, "i:/proj")
        board = fish.board_ensure(self.conn, "project", "PJ")
        path = fish.board_out_path(self.root, board)
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        self.assertNotIn("AI-甲", text,
                         "掐掉渲染后文件不该自动变新——否则前一条判据测不到东西")


class SoftGateTest(ReadableBase):
    """R2：自动行算登记；过旧 → 提醒补 doing（保留旧文案关键词以兼容既有判据）。"""

    def test_fresh_auto_row_counts_as_registered(self):
        fish.board_auto_checkin(self.conn, CID_A, project="PJ", source="session_start")
        ok, msg = fish.board_checkin_or_remind(self.conn, CID_A, "PJ")
        self.assertTrue(ok, "自动行算登记（用户 2026-10-26 拍板）")
        self.assertIsNone(msg)

    def test_stale_auto_row_asks_for_doing(self):
        fish.board_auto_checkin(self.conn, CID_A, project="PJ", source="session_start")
        self.conn.execute("UPDATE presence SET last_seen_at=?, auto_seen_at=?",
                          (_ts(120), _ts(120)))
        self.conn.commit()
        ok, msg = fish.board_checkin_or_remind(self.conn, CID_A, "PJ")
        self.assertFalse(ok)
        self.assertIn("分钟没更新", msg)
        self.assertIn("自动登记", msg, "要说明这行不是它自己的声明")
        self.assertIn("doing", msg, "提示必须是可执行的下一步")

    def test_no_row_at_all(self):
        ok, msg = fish.board_checkin_or_remind(self.conn, CID_A, "PJ")
        self.assertFalse(ok)
        self.assertIn("还没签到", msg)


class RenderTest(ReadableBase):
    """R3/R4/R5：渲染文件里的名字、两栏与状态分档。"""

    def _render(self):
        board = fish.board_ensure(self.conn, "project", "PJ")
        path, _size, _split = fish.board_render(self.conn, self.root, board)
        return fish.board_out_path(self.root, {"path": path}).read_text(encoding="utf-8"), board

    def test_no_ulid_and_readable_names(self):
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,status,created_at,host) "
            "VALUES (?,?,'resident','active',?,'wb')",
            ("01BOARDREADABLE00000000C3", "AI-甲", fish.now_utc()))
        self.conn.commit()
        fish.board_checkin(self.conn, CID_A, "改 fish", surface="p0/fish.py",
                           scope="project", scope_key="PJ")
        fish.board_checkin(self.conn, "01BOARDREADABLE00000000C3", "改文档",
                           surface="docs/x.md", scope="project", scope_key="PJ")
        text, _board = self._render()
        self.assertEqual(ULID_RE.findall(text), [], f"渲染文件里不许出现 ULID：{text[:400]}")
        self.assertIn("AI-甲·dsh", text, "重名时按 host 消歧")
        self.assertIn("AI-甲·wb", text, "另一个宿主的那位用另一个 label")

    def test_two_columns_and_declared_surface(self):
        fish.board_checkin(self.conn, CID_A, "改 fish", surface="p0/fish.py",
                           scope="project", scope_key="PJ")
        self._activity(CID_A, "p0", minutes_ago=1, count=4)
        text, _board = self._render()
        self.assertIn("声明在做", text)
        self.assertIn("实际在动", text)
        self.assertIn("p0/fish.py", text, "声明面不能被新列挤掉（否则'谁占了哪块'看不见）")
        self.assertIn("p0（4 次写", text, "实际在动要出目录与次数")

    def test_offline_inferred(self):
        fish.board_checkin(self.conn, CID_A, "改了半小时前", surface="p0/x.py",
                           scope="project", scope_key="PJ")
        self.conn.execute("UPDATE presence SET last_seen_at=?, auto_seen_at=?",
                          (_ts(40), _ts(40)))
        self._activity(CID_A, "p0", minutes_ago=40, count=1)
        self.conn.commit()
        text, _board = self._render()
        self.assertIn("offline（推断）", text, f"40 分钟没动应是 offline 且标注推断：{text}")

    def test_active_after_fresh_activity(self):
        fish.board_checkin(self.conn, CID_A, "改 fish", surface="p0/x.py",
                           scope="project", scope_key="PJ")
        self.conn.execute("UPDATE presence SET last_seen_at=?, auto_seen_at=?",
                          (_ts(40), _ts(40)))
        self._activity(CID_A, "p0", minutes_ago=0, count=2)   # 刚写过 → 实际在动
        self.conn.commit()
        text, _board = self._render()
        row = [ln for ln in text.splitlines() if ln.startswith("| AI-甲")]
        self.assertTrue(row, text)
        self.assertIn("active", row[0],
                      "有效最后活跃 = max(声明, 实际)——刚写过就不该显示 offline")

    def test_root_board_uses_labels(self):
        fish.board_checkin(self.conn, CID_A, "改 fish", scope="project", scope_key="PJ")
        # 根板由 `board_render_root` 自己 ensure（它内部会建），不要手插一行
        path, _size, n = fish.board_render_root(self.conn, self.root)
        text = fish.board_out_path(self.root, {"path": path}).read_text(encoding="utf-8")
        self.assertGreaterEqual(n, 1)
        self.assertIn("AI-甲", text)
        self.assertEqual(ULID_RE.findall(text), [], "根板同样不许出 ULID")


class InjectionBlockTest(ReadableBase):
    """R6：注入包看板段（预算最紧的一段，只给最该看的）。"""

    def test_block_contents(self):
        for i in range(6):
            cid = f"01BOARDREADABLE000000{i:04d}"
            self.conn.execute(
                "INSERT INTO citizens (citizen_id,display_name,kind,status,created_at,host)"
                " VALUES (?,?,'resident','active',?,'dsh')",
                (cid, f"工-{i}", fish.now_utc()))
            fish.board_checkin(self.conn, cid, f"干活 {i}", surface=f"p{i}/x.py",
                               scope="project", scope_key="PJ")
        self.conn.commit()
        block = fish.board_block(self.conn, "PJ")
        self.assertIn("看板（", block)
        self.assertIn("PJ", block, "注入包看板段要带项目名")
        self.assertIn("大肥鱼-测试", block, "namespace 与 scope_key 不同时附括号")
        self.assertLessEqual(block.count("：干活"), 3, "最多 3 人（预算纪律）")
        self.assertIn("…", block, "有人被省略时必须标省略号，不能假装只有 3 个人")
        self.assertEqual(ULID_RE.findall(block), [])

    def test_unnamed_hint_is_actionable(self):
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,status,created_at,host) "
            "VALUES ('01BOARDREADABLE00000000D4','dsh-abc123','resident','active',?,"
            "'dsh')", (fish.now_utc(),))
        self.conn.commit()
        hint = fish._unnamed_hint(self.conn, "01BOARDREADABLE00000000D4")
        self.assertIn("rename_citizen", hint, "缺名提示必须是可执行的下一步")
        self.assertIn("未命名" if "未命名" in hint else "dsh-abc123", hint)
        self.assertEqual(fish._unnamed_hint(self.conn, CID_A), "",
                         "已有真名的不该被提示（不刷屏）")


class CapacityTest(ReadableBase):
    """R7：新列之后 32KB 仍然守得住。"""

    def test_500_posts_stay_under_limit(self):
        board = fish.board_checkin(self.conn, CID_A, "改 fish", scope="project",
                                   scope_key="PJ")
        rows = [(board["id"], CID_A, "progress", "长" * 180, fish.now_utc())
                for _ in range(500)]
        self.conn.executemany(
            "INSERT INTO board_posts (board_id,citizen_id,kind,text,created_at) "
            "VALUES (?,?,?,?,?)", rows)
        self.conn.commit()
        path, size, split = fish.board_render(self.conn, self.root, board)
        self.assertLessEqual(size, fish.BOARD_MAX_BYTES, f"主文件超限：{size}")
        self.assertTrue(split)
        # 分片也各自受限
        for shard in fish.board_out_path(self.root, {"path": path}).parent.glob("看板-*-*.md"):
            self.assertLessEqual(shard.stat().st_size, fish.BOARD_MAX_BYTES,
                                 f"分片 {shard.name} 超限")


class MutationTest(ReadableBase):
    """R8：判据能变红。"""

    def test_label_back_to_short_id_goes_red(self):
        fish.board_checkin(self.conn, CID_A, "改 fish", scope="project", scope_key="PJ")
        board = fish.board_ensure(self.conn, "project", "PJ")
        original = fish.who_label
        try:
            fish.who_label = lambda row, **kw: (row["citizen_id"] if isinstance(row, dict)
                                                else row[0])[:8]
            path, _size, _split = fish.board_render(self.conn, self.root, board)
            text = fish.board_out_path(self.root, {"path": path}).read_text(encoding="utf-8")
            self.assertIn(CID_A[:8], text, "变异后渲染里出现了短 id——R3 的判据才有效")
        finally:
            fish.who_label = original

    def test_state_threshold_zero_breaks_tiers(self):
        fish.board_checkin(self.conn, CID_A, "改了半小时前", scope="project",
                           scope_key="PJ")
        self.conn.execute("UPDATE presence SET last_seen_at=?, auto_seen_at=?",
                          (_ts(40), _ts(40)))
        self.conn.commit()
        board = fish.board_ensure(self.conn, "project", "PJ")
        states = [p["state"] for p in fish.board_presence_rows(self.conn, board)]
        self.assertIn("offline", states)
        # 变异：把 active 窗口放大到 100 小时 → offline 应该变 active（判据有判别力）
        fish.set_config(self.conn, "board.active_window_sec", 100 * 3600)
        states2 = [p["state"] for p in fish.board_presence_rows(self.conn, board)]
        self.assertIn("active", states2, "阈值一变分档就变——说明它真的在按配置分档")

    def test_auto_refresh_does_not_touch_doing(self):
        """红线：自动刷新**只写** auto_* 三列。"""
        board = fish.board_checkin(self.conn, CID_A, "我的声明", scope="project",
                                   scope_key="PJ")
        import fish_activity
        fish_activity.refresh_presence_cache(
            self.conn, citizen_id=CID_A, project="PJ", dirs=["p0"], seen_at=fish.now_utc())
        doing = self.conn.execute(
            "SELECT doing FROM presence WHERE board_id=? AND citizen_id=?",
            (board["id"], CID_A)).fetchone()[0]
        self.assertEqual(doing, "我的声明")


class RenameContractTest(unittest.TestCase):
    """`who_label` 的契约本身（最小单元，不依赖库）。"""

    def test_no_id_by_default(self):
        self.assertEqual(fish.who_label({"citizen_id": CID_A, "display_name": "甲",
                                         "host": "dsh"}), "甲")
        self.assertIn(CID_A[:8], fish.who_label({"citizen_id": CID_A,
                                                 "display_name": "甲"}, with_id=True))

    def test_auto_name_is_not_a_readable_name(self):
        self.assertTrue(fish_naming.is_auto_name("dsh-abc12345"))


# ---------------------------------------------------------------- 评审 F2
#
# 「展示列当判定列」：`board_observed_overlaps`（看板「观察到的重叠（推断）」）与
# `_surfaces_touch`（注入包「声明≠实际」）此前拿 `auto_dirs[i]["dir"]` 做比较。
# `dir` 是**相对写入者基准**算出来的展示串：两个人在不同项目里各写一个 `p0`，
# 展示列都叫 `p0` → 一条假的「推断重叠」；反过来绝对声明 vs 相对展示串 →
# 一条假的「声明≠实际」。两条都属于"警告掺假"，比漏报更伤。


class SurfacesTouchJudgeTest(unittest.TestCase):
    """F2（注入包）：判定只看**绝对路径**，展示串不许参与判定。"""

    def test_display_string_is_not_judged(self):
        self.assertTrue(
            fish._surfaces_touch(["i:/proj/p0/fish.py"], ["p0"]),
            "绝对声明 + 相对展示串 → 判不了 → **不许**报「声明≠实际」（旧实现的假提示）")

    def test_unprovable_base_is_not_accused(self):
        self.assertTrue(fish._surfaces_touch(["p0/fish.py"], ["i:/proj/p0"]),
                        "相对声明又拿不到项目根 → 同样判不了 → 不下结论")

    def test_provable_base_judges_correctly(self):
        self.assertTrue(fish._surfaces_touch(["p0/fish.py"], ["i:/proj/p0"],
                                             base="i:/proj"),
                        "按看板项目根解析后是同一处 → 一致")
        self.assertFalse(fish._surfaces_touch(["p0/fish.py"], ["i:/other/docs"],
                                              base="i:/proj"),
                         "真的是另一处 → 该报（否则这条提示永远不出现）")

    def test_same_name_dir_in_other_project_is_not_touched(self):
        self.assertFalse(fish._surfaces_touch(["i:/a/p0"], ["i:/b/p0"]),
                         "`i:/a/p0` 与 `i:/b/p0` 只是同名——展示列比会误报")
        self.assertIsNotNone(fish_surface.surface_hit("p0", "p0"),
                             "变异证据：拿展示列比这一对必然命中（旧实现正是这么做的）")


class ObservedOverlapJudgeTest(ReadableBase):
    """F2（看板）：跨项目同名的展示目录不得报「推断重叠」。"""

    def _activity_at(self, citizen, path_abs, path_dir, minutes_ago=0):
        self.conn.execute(
            "INSERT INTO tool_activity (host, session_key, citizen_id, project, "
            "tool_name, action, path, path_dir, path_abs, args_digest, ok, seen_at) "
            "VALUES ('dsh','dsh:s',?,'PJ','write','write',?,?,?,'x',1,?)",
            (citizen, f"{path_dir}/x.py", path_dir, path_abs, _ts(minutes_ago)))
        self.conn.commit()

    def _both_on_board(self):
        """两个公民都在同一块项目板上签到（观察重叠只读在场行的 auto 面）。"""
        for cid in (CID_A, CID_B):
            fish.board_checkin(self.conn, cid, "改 p0", scope="project", scope_key="PJ")
        return fish.board_ensure(self.conn, "project", "PJ")

    def test_cross_project_same_named_dir_no_overlap(self):
        board = self._both_on_board()
        self._activity_at(CID_A, "i:/a/p0/x.py", "p0")
        self._activity_at(CID_B, "i:/b/p0/x.py", "p0")
        overlaps = fish.board_observed_overlaps(self.conn, board)
        self.assertEqual([o for o in overlaps if o["kind"] == "actual"], [],
                         f"`i:/a/p0` 与 `i:/b/p0` 不是同一处，不许报推断重叠：{overlaps}")

    def test_real_overlap_still_reported(self):
        """反向：**真**重叠必须照报（否则"改绿"只是把功能关掉了）。"""
        board = self._both_on_board()
        self._activity_at(CID_A, "i:/proj/p0/x.py", "p0")
        self._activity_at(CID_B, "i:/proj/p0/y.py", "p0")
        overlaps = fish.board_observed_overlaps(self.conn, board)
        self.assertTrue([o for o in overlaps if o["kind"] == "actual"],
                        f"同一目录里的两个人必须报：{overlaps}")

    def test_display_column_would_false_positive(self):
        """变异自证：把判定列换回展示列（`dir`）→ 同场景必须误报。"""
        board = self._both_on_board()
        self._activity_at(CID_A, "i:/a/p0/x.py", "p0")
        self._activity_at(CID_B, "i:/b/p0/x.py", "p0")
        original = fish.board_presence_rows

        def with_display_dirs(conn, b):
            rows = original(conn, b)
            for row in rows:
                for item in row.get("auto_dirs") or []:
                    item["dir_abs"] = item["dir"]      # 退回"拿展示列判定"
            return rows

        try:
            fish.board_presence_rows = with_display_dirs
            overlaps = fish.board_observed_overlaps(self.conn, board)
        finally:
            fish.board_presence_rows = original
        self.assertTrue([o for o in overlaps if o["kind"] == "actual"],
                        "变异后退回展示列必须误报——否则本判据没在测判定列")


class InjectionMismatchJudgeTest(ReadableBase):
    """F2（注入包端到端）：声明按看板项目根转绝对，与实际 `dir_abs` 同基准比。"""

    def _pack(self):
        text, _meta = fish.build_injection_pack(self.conn, self.root, CID_A, project="PJ")
        return text

    def test_declared_and_actual_same_area_no_mismatch(self):
        fish.board_checkin(self.conn, CID_A, "改 fish 判定", surface="p0/fish.py",
                           scope="project", scope_key="PJ")
        self._activity(CID_A, directory="p0", minutes_ago=1, count=1)
        self.assertNotIn("声明≠实际", self._pack(),
                         "声明的 p0/fish.py 与实际写的 p0 是同一处，不许提示不一致")

    def test_really_elsewhere_reports_mismatch(self):
        fish.board_checkin(self.conn, CID_A, "改 fish 判定", surface="p0/fish.py",
                           scope="project", scope_key="PJ")
        self._activity(CID_A, directory="docs/guide", minutes_ago=1, count=1)
        self.assertIn("声明≠实际", self._pack(),
                      "真的在另一处写 → 必须提示（这条提示不能被'改绿'改没了）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
