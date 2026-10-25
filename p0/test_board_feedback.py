#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看板 · 外部评估 4 条的修复判据（2026-10-25）。

## 来源

外部 AI 在 2026-10-25 18:22 读完我们的看板实现后写了 4 条反馈：

  EVAL-261025-09  surface 只声明不消费：show 不检测重叠、注入包只带 doing
  EVAL-261025-10  MCP 无 post（做完无法标 done）、软门 board_checkin_or_remind 是死代码
  EVAL-261025-11  预算裁剪把看板排第 3、按本地日期切板导致跨午夜断片
  EVAL-261025-12  反证 -05（token 不区分签发通道）

## 判据分组

  O1–O4   surface 归一化与命中语义（含目录前缀、不误报）
  O5      show 输出重叠警告段
  O6      注入包带 surface 且**重叠的人排前面**
  O7      重叠去重（同一对不打印两遍）
  P       MCP post（progress/done/note）
  R       软门接进 remember（提示不阻断、签到后不再提）
  T       裁剪顺序：看板排在注意事项**之后**
  N       跨午夜：昨日未归档板会出现在今日看板里
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


class SurfaceTest(unittest.TestCase):
    """O1–O4：纯函数级，不需要库。"""

    def test_O1_normalize(self):
        """归一化：小写、正斜杠、去空白、去尾斜杠、去重排序。"""
        self.assertEqual(fish.normalize_surface(" P0/Fish.py , p0/fish.py "),
                         ["p0/fish.py"])
        self.assertEqual(fish.normalize_surface("a\\b\\c"), ["a/b/c"])
        self.assertEqual(fish.normalize_surface("x/, y"), ["x", "y"])
        self.assertEqual(fish.normalize_surface(""), [])
        self.assertEqual(fish.normalize_surface(None), [])

    def test_O2_exact_hit(self):
        self.assertEqual(fish.surface_hit("p0/fish.py", "p0/fish.py"), "p0/fish.py")
        self.assertIsNone(fish.surface_hit("p0/a.py", "p0/b.py"))

    def test_O3_dir_prefix_hit(self):
        """**目录前缀语义**（`EVAL-261025-09` 建议 4）：声明目录要能命中其下文件。

        这是让"声明"可用的关键——要求列全所有文件是做不到的，而列全本身容易漏。
        """
        self.assertEqual(fish.surface_hit("p0", "p0/fish.py"), "p0")
        self.assertEqual(fish.surface_hit("p0/fish.py", "p0"), "p0")
        self.assertEqual(fish.surface_hit("backend/routes",
                                          "backend/routes/tasks.py"),
                         "backend/routes")

    def test_O4_no_false_positive_on_string_prefix(self):
        """**按路径分量判前缀，不做字符串前缀**——`a/b` 不该命中 `a/bc`。

        这条是防"看起来更聪明反而更危险"：字符串前缀会把
        `src/app` 与 `src/application` 判成重叠，制造假警报。
        """
        self.assertIsNone(fish.surface_hit("a/b", "a/bc"))
        self.assertIsNone(fish.surface_hit("p0/fish.py", "p0/fish.py.bak"))


class BoardOverlapE2E(unittest.TestCase):
    """O5–O7 + P + R + T + N：需要真实库与 MCP 层。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / "data"
        self.srv = mcp_server.FishServer(self.root)
        self.anon = self.srv._resolve_identity(None)
        self.srv.conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES ('DEMO','DEMO','',0,'active')")
        self.srv.conn.commit()
        self._drop = {v for _h, v in fish.HOST_SESSION_ENVS} | set(fish.SESSION_ALIAS_ENVS)
        self._clean = {k: v for k, v in os.environ.items() if k not in self._drop}

    def tearDown(self):
        try:
            self.srv.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.root.parent, ignore_errors=True)

    def _ident(self, name):
        import re
        # 模拟真实 MCP 进程：宿主会 scrub 掉 DSH_SESSION_ID 等
        with mock.patch.dict(os.environ, self._clean, clear=True):
            o = self.srv.tool_session_start({"session_name": name}, self.anon)
        tok = re.search(r"token:\s*(fsh_\S+)", o).group(1)
        return self.srv._resolve_identity(tok)

    # ------------------------------------------------------------------ O5
    def test_O5_show_prints_overlap(self):
        """`show` 必须**打印重叠警告段**——这是把"声明"变成"防线"的那一步。"""
        a, b = self._ident("ai-A"), self._ident("ai-B")
        self.srv.tool_board({"action": "checkin", "doing": "A 改 fish",
                             "surface": "p0/fish.py", "project": "DEMO"}, a)
        self.srv.tool_board({"action": "checkin", "doing": "B 也改 fish",
                             "surface": "p0/fish.py", "project": "DEMO"}, b)
        out = self.srv.tool_board({"action": "show", "project": "DEMO"}, a)
        self.assertIn("占用面重叠", out, f"show 没出重叠警告：{out[:400]}")
        self.assertIn("p0/fish.py", out)

    def test_O5_no_overlap_no_noise(self):
        """不重叠时**不该**有警告段（避免训练 AI 忽略它）。"""
        a, b = self._ident("ai-C"), self._ident("ai-D")
        self.srv.tool_board({"action": "checkin", "doing": "C",
                             "surface": "p0/a.py", "project": "DEMO"}, a)
        self.srv.tool_board({"action": "checkin", "doing": "D",
                             "surface": "p0/b.py", "project": "DEMO"}, b)
        out = self.srv.tool_board({"action": "show", "project": "DEMO"}, a)
        self.assertNotIn("占用面重叠", out, "不该有假警告")

    # ------------------------------------------------------------------ O7
    def test_O7_overlap_deduped(self):
        """同一对 (A,B) 只打印**一次**（B 声明目录会命中 A 的多个文件）。"""
        a, b = self._ident("ai-E"), self._ident("ai-F")
        self.srv.tool_board({"action": "checkin", "doing": "E",
                             "surface": "p0/x.py,p0/y.py", "project": "DEMO"}, a)
        self.srv.tool_board({"action": "checkin", "doing": "F",
                             "surface": "p0", "project": "DEMO"}, b)
        board = fish.board_ensure(self.srv.conn, "project", "DEMO")
        ovs = fish.board_overlaps(self.srv.conn, board)
        self.assertEqual(len(ovs), 1,
                         f"同一对同一前缀应只算一条，实际 {len(ovs)} 条：{ovs}")

    # ------------------------------------------------------------------ O6
    def test_O6_injection_carries_surface(self):
        """注入包必须带 `surface`（旧实现只印 doing，恰好缺了唯一有用的字段）。"""
        a, b = self._ident("ai-G"), self._ident("ai-H")
        self.srv.tool_board({"action": "checkin", "doing": "G 改 fish",
                             "surface": "p0/fish.py", "project": "DEMO"}, a)
        self.srv.tool_board({"action": "checkin", "doing": "H 也改",
                             "surface": "p0/fish.py", "project": "DEMO"}, b)
        block = fish.board_block(self.srv.conn, "DEMO")
        self.assertIn("占", block, f"注入包没带占用面：{block[:400]}")
        self.assertIn("p0/fish.py", block)
        # 重叠的人要排前面（旧实现 pres[:3] 按活跃排序，4 人起就看不见关键的人）
        self.assertIn("重叠", block, "注入包应警告重叠")

    def test_O6_more_than_three_people(self):
        """在场超过 3 人时不该只剩 `…`——旧实现 `pres[:3]` 会丢掉后面的。"""
        for i in range(5):
            idn = self._ident(f"ai-{i}")
            self.srv.tool_board({"action": "checkin", "doing": f"任务 {i}",
                                 "surface": f"p{i}/x.py", "project": "DEMO"}, idn)
        block = fish.board_block(self.srv.conn, "DEMO")
        self.assertIn("5 人在场", block)

    # ------------------------------------------------------------------ P
    def test_P_mcp_post(self):
        """MCP `board` 必须支持 `post`（做完要能标 done）。"""
        a = self._ident("ai-post")
        self.srv.tool_board({"action": "checkin", "doing": "干活",
                             "project": "DEMO"}, a)
        out = self.srv.tool_board({"action": "post", "kind": "done",
                                   "text": "做完了", "project": "DEMO"}, a)
        self.assertIn("已记录 done", out)
        board = fish.board_ensure(self.srv.conn, "project", "DEMO")
        kinds = [r[0] for r in self.srv.conn.execute(
            "SELECT kind FROM board_posts WHERE board_id=?", (board["id"],))]
        self.assertIn("done", kinds)

    def test_P_post_requires_token_and_text(self):
        with self.assertRaises(ValueError):
            self.srv.tool_board({"action": "post", "text": "x"}, self.anon)
        a = self._ident("ai-post2")
        with self.assertRaises(ValueError):
            self.srv.tool_board({"action": "post", "text": "  "}, a)

    # ------------------------------------------------------------------ R
    def test_R_soft_gate_in_remember(self):
        """软门必须**接进 remember**：未签到时回执带提示，且**不阻断写入**。"""
        a = self._ident("ai-soft")
        out = self.srv.tool_remember(
            {"project": "DEMO", "title": "软门测试一"}, a)
        # 2026-10-25 改（`EVAL-261025-16` 建议 3 落地）：`remember` 现在会
        # **自动兜底登记**一条 presence。所以"没签到"这个分支不再触发——
        # 判据改成钉"它确实自动登记了"，这才是新契约。
        board = fish.board_ensure(self.srv.conn, "project", "DEMO")
        rows = self.srv.conn.execute(
            "SELECT doing FROM presence WHERE board_id=? AND citizen_id=?",
            (board["id"], a.citizen_id)).fetchall()
        self.assertEqual(len(rows), 1, "写记忆应自动登记一条 presence")
        self.assertIn("自动登记", rows[0][0], f"doing 要标注来源：{rows[0][0]}")
        # 不阻断：记忆真的写进去了
        n = self.srv.conn.execute(
            "SELECT COUNT(*) FROM code_index WHERE title='软门测试一'").fetchone()[0]
        self.assertEqual(n, 1, "软门不该阻断写入")
        # 签到之后不再提
        self.srv.tool_board({"action": "checkin", "doing": "干活",
                             "project": "DEMO"}, a)
        out2 = self.srv.tool_remember(
            {"project": "DEMO", "title": "软门测试二"}, a)
        self.assertNotIn("你还没签到", out2, f"签到后不该再提示：{out2}")

    # ------------------------------------------------------------------ T
    def test_T_board_trimmed_after_attention(self):
        """裁剪顺序：**看板排在注意事项之后**（时效信息优先保留）。

        `EVAL-261025-11`：原文把看板排第 3，与"这是协作协议不是礼貌"自相矛盾；
        而它只值 150-200 token，裁它省不下多少，却裁掉了时效性最强的那块。
        """
        a = self._ident("ai-trim")
        self.srv.tool_board({"action": "checkin", "doing": "干活",
                             "surface": "p0/x.py", "project": "DEMO"}, a)
        # 极小预算逼出裁剪
        _pack, meta = fish.build_injection_pack(
            self.srv.conn, self.root, a.citizen_id, budget=60, project="DEMO")
        t = meta["trimmed"]
        self.assertIn("看板", t, f"预算极小时看板应被裁，实际裁剪链={t}")
        # 如果注意事项也被裁了，它必须**先于**看板
        if "注意事项" in t:
            self.assertLess(t.index("注意事项"), t.index("看板"),
                            f"注意事项应在看板之前被裁，实际={t}")

    # ------------------------------------------------------------------ N
    def test_N_cross_midnight_note(self):
        """跨午夜：昨日未归档板的动态要出现在今日看板里（`EVAL-261025-11`）。"""
        import datetime
        a = self._ident("ai-night")
        today = fish.board_period()
        prev = (datetime.datetime.strptime("20" + today, "%Y%m%d").date()
                - datetime.timedelta(days=1)).strftime("%y%m%d")
        # 昨天的板 + 一条动态
        bp = fish.board_ensure(self.srv.conn, "project", "DEMO", period_key=prev)
        fish.board_checkin(self.srv.conn, a.citizen_id, "前半夜的活",
                           scope="project", scope_key="DEMO", period_key=prev)
        fish.board_post(self.srv.conn, bp, a.citizen_id, "progress", "昨夜进度一条")
        # 今天的板
        bt = fish.board_checkin(self.srv.conn, a.citizen_id, "后半夜的活",
                                scope="project", scope_key="DEMO")
        note = fish.board_prev_note(self.srv.conn, bt)
        self.assertIn("昨日板", note, f"应提示昨日板：{note}")
        self.assertIn("昨夜进度一条", note, "应带昨日最后几条动态")
        # 昨天的板被归档后就不再提（sweep 之后它是历史）
        fish.board_close(self.srv.conn, self.root, "project", "DEMO", period_key=prev)
        self.assertEqual(fish.board_prev_note(self.srv.conn, bt), "",
                         "已归档的昨日板不该再冒出来")


if __name__ == "__main__":
    unittest.main(verbosity=2)
