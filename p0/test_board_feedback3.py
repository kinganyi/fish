#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看板 · 外部评估第三批（`EVAL-261025-16`）的 3 个新功能判据（2026-10-25）。

## 来源（用户拍板"都做"）

外部 AI 在 `EVAL-261025-16` 里给看板提了 4 条建议：

  1. 入场表显示**可读名**（原来只有 ULID，人每次要对照 whoami 才知道是谁）
  2. `simulate` 让撞车检测**可自助验证**（只有一个会话时永远测不出来）
  3. `remember` 时**自动兜底**写 presence（"很多人不 checkin 不是不愿意，是记不住"）
  4. 与旧 `active-sessions/*.md` 明确二选一（→ 已写进 `38` 号文档附录 C）

## 判据

  U1  `who_label` 可读名：有名字 → `名字（短 id）`；无名字 → 只给短 id，**不编造**
  U2  `simulate` 单人可验证：`assume_surface` 走**同一套** `surface_hit`，且**不写库**
  U3  自动兜底 presence 的三条红线：
        · **只在无行时写**（绝不覆盖真实 doing）
        · doing **标注来源**（能看出这不是谁声明的）
        · **不编造 surface**（编造的会制造假重叠）
"""
import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

import mcp_server  # noqa: E402


class BoardFeedback3Test(unittest.TestCase):

    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.root = self.base / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES ('UU','UU',NULL,0,'active')")
        self.conn.commit()
        self.srv = mcp_server.FishServer(self.root)
        self.anon = self.srv._resolve_identity(None)
        self._drop = {v for _h, v in fish.HOST_SESSION_ENVS} | set(fish.SESSION_ALIAS_ENVS)
        self._clean = {k: v for k, v in os.environ.items() if k not in self._drop}

    def tearDown(self):
        try:
            self.srv.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.base, ignore_errors=True)

    def _ident(self, name):
        import re
        with mock.patch.dict(os.environ, self._clean, clear=True):
            o = self.srv.tool_session_start({"session_name": name}, self.anon)
        return self.srv._resolve_identity(re.search(r"token:\s*(fsh_\S+)", o).group(1))

    # ------------------------------------------------------------------ U1
    def test_U1_who_label_prefers_readable_name(self):
        """有名字 → `名字（短 id）`；**保留短 id** 因为同名时还要能区分。"""
        row = {"citizen_id": "17929164334384A67C1B1F95C4",
               "display_name": "codebuddy-8a83d218-8b2"}
        label = fish.who_label(row)
        self.assertIn("codebuddy-8a83d218-8b2", label)
        self.assertIn("1792916", label, "短 id 必须保留（重名时靠它区分）")

    def test_U1_who_label_no_name_no_invention(self):
        """没名字（老数据/匿名）→ **只给短 id，不编造**。"""
        # 短 id 是**前 8 位**（与 who_label 的实现一致）
        self.assertEqual(fish.who_label({"citizen_id": "ABCDEFGH1234", "display_name": ""}),
                         "ABCDEFGH")

    def test_U1_assumed_label(self):
        """`simulate` 的虚拟占用者有个明确标签，不冒充真实公民。"""
        self.assertEqual(fish.who_label({"citizen_id": "ASSUMED", "display_name": "x"}),
                         "（simulate 假设的人）")

    def test_U1_board_shows_name(self):
        """端到端：入场表里出现可读名，而不只是 ULID。"""
        who = self._ident("readable-dev")
        self.srv.tool_board({"action": "checkin", "doing": "干活",
                             "surface": "p0/x.py", "project": "UU"}, who)
        out = self.srv.tool_board({"action": "show", "project": "UU"}, who)
        self.assertIn("readable-dev", out, f"入场表应显示可读名：{out[:500]}")
        self.assertIn(who.citizen_id[:8], out, "短 id 也应在（区分同名）")

    # ------------------------------------------------------------------ U2
    def test_U2_simulate_alone_can_verify(self):
        """**单人也能验证撞车检测**——这是这条功能的全部理由。

        `EVAL-261025-16`：只有一个会话时怎么 checkin 都不触发警告，
        **没有第二个人就无法验证核心卖点是否工作**。
        """
        who = self._ident("solo-dev")
        self.srv.tool_board({"action": "checkin", "doing": "改 fish",
                             "surface": "p0/fish.py", "project": "UU"}, who)
        # 无假设：对比 0 人
        out1 = self.srv.tool_board({"action": "simulate", "surface": "docs/x.md",
                                    "project": "UU"}, who)
        self.assertIn("0 个人", out1, f"应如实说对比了 0 人：{out1[:300]}")
        # 有假设：当场看到撞车
        out2 = self.srv.tool_board({"action": "simulate", "surface": "p0",
                                    "assume_surface": "p0/fish.py",
                                    "project": "UU"}, who)
        self.assertIn("会撞上", out2, f"假设档应触发警告：{out2[:400]}")
        self.assertIn("p0", out2)

    def test_U2_simulate_uses_same_logic(self):
        """`assume_surface` 走的必须是**同一套** `surface_hit`（不是另一条假路径）。

        判据方式：目录前缀语义在 simulate 里也要生效（`p0` 命中 `p0/fish.py`）。
        """
        who = self._ident("same-logic")
        out = self.srv.tool_board({"action": "simulate", "surface": "p0",
                                   "assume_surface": "p0/deep/x.py",
                                   "project": "UU"}, who)
        self.assertIn("会撞上", out, "目录前缀语义必须在 simulate 里同样生效")

    def test_U2_simulate_writes_nothing(self):
        """**不写库**：simulate 不该产生 presence / 动态 / 渲染文件。"""
        who = self._ident("no-write")
        board = fish.board_ensure(self.conn, "project", "UU")
        n_pres = self.conn.execute(
            "SELECT COUNT(*) FROM presence WHERE board_id=?", (board["id"],)).fetchone()[0]
        n_posts = self.conn.execute(
            "SELECT COUNT(*) FROM board_posts WHERE board_id=?", (board["id"],)).fetchone()[0]
        self.srv.tool_board({"action": "simulate", "surface": "p0",
                             "assume_surface": "p0/fish.py", "project": "UU"}, who)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM presence WHERE board_id=?", (board["id"],)).fetchone()[0],
            n_pres, "simulate 不该写 presence")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM board_posts WHERE board_id=?", (board["id"],)).fetchone()[0],
            n_posts, "simulate 不该写动态")

    def test_U2_simulate_needs_surface(self):
        who = self._ident("need-surface")
        with self.assertRaises(ValueError):
            self.srv.tool_board({"action": "simulate", "project": "UU"}, who)

    # ------------------------------------------------------------------ U3
    def test_U3_auto_presence_marks_source(self):
        """自动兜底写的行，`doing` 必须**标注来源**（能看出这不是谁声明的主张）。"""
        who = self._ident("auto-dev")
        fish.board_auto_presence(self.conn, who.citizen_id, "UU")
        board = fish.board_ensure(self.conn, "project", "UU")
        doing = self.conn.execute(
            "SELECT doing FROM presence WHERE board_id=? AND citizen_id=?",
            (board["id"], who.citizen_id)).fetchone()[0]
        self.assertIn("自动登记", doing, f"doing 要标注来源：{doing}")

    def test_U3_auto_presence_never_overwrites(self):
        """**红线 1**：只在无行时写——绝不覆盖别人声明的 `doing`。"""
        who = self._ident("real-work")
        self.srv.tool_board({"action": "checkin",
                             "doing": "我正在认真地改 fish.py 的判定逻辑",
                             "surface": "p0/fish.py", "project": "UU"}, who)
        fish.board_auto_presence(self.conn, who.citizen_id, "UU")
        board = fish.board_ensure(self.conn, "project", "UU")
        doing, surface = self.conn.execute(
            "SELECT doing, surface FROM presence WHERE board_id=? AND citizen_id=?",
            (board["id"], who.citizen_id)).fetchone()
        self.assertIn("认真地改", doing, f"真实 doing 被覆盖了：{doing}")
        self.assertEqual(surface, "p0/fish.py", "真实 surface 被覆盖了")

    def test_U3_auto_presence_no_fake_surface(self):
        """**红线 3**：不编造 surface（编造的会制造假重叠）。"""
        who = self._ident("no-surface")
        fish.board_auto_presence(self.conn, who.citizen_id, "UU")
        board = fish.board_ensure(self.conn, "project", "UU")
        surface = self.conn.execute(
            "SELECT surface FROM presence WHERE board_id=? AND citizen_id=?",
            (board["id"], who.citizen_id)).fetchone()[0]
        self.assertIn(surface, (None, ""), f"自动行不该有 surface：{surface!r}")

    def test_U3_remember_triggers_auto_presence(self):
        """端到端：`remember` 会触发兜底登记（看板不再 0 人）。"""
        who = self._ident("forgetful")
        self.srv.tool_remember({"project": "UU", "title": "忘记签到也要有痕迹"}, who)
        board = fish.board_ensure(self.conn, "project", "UU")
        n = self.conn.execute(
            "SELECT COUNT(*) FROM presence WHERE board_id=? AND citizen_id=?",
            (board["id"], who.citizen_id)).fetchone()[0]
        self.assertEqual(n, 1, "写记忆应自动登记一条 presence")

    def test_U3_auto_presence_no_overlap_noise(self):
        """自动行没有 surface → **不会**制造假重叠（红线 3 的连带验证）。"""
        a, b = self._ident("auto-a"), self._ident("auto-b")
        self.srv.tool_board({"action": "checkin", "doing": "真活",
                             "surface": "p0/fish.py", "project": "UU"}, a)
        fish.board_auto_presence(self.conn, b.citizen_id, "UU")
        board = fish.board_ensure(self.conn, "project", "UU")
        self.assertEqual(fish.board_overlaps(self.conn, board), [],
                         "自动行没有 surface，不该产生重叠")


if __name__ == "__main__":
    unittest.main(verbosity=2)
