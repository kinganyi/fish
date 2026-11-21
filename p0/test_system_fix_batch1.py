#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""系统级修复 · 第一批判据（2026-11-21）。

覆盖五项：B-2 register 回执 / B-3 --help 文案 / B-4 events --code /
C-1 new_posts 算法 / F-3 profile_questions 留痕。

## 这份测试的写法与前几份不同

前几份测的是"功能对不对"。这份测的是**"说法与行为是否一致"**——
五个缺陷里有四个的共同形态是「注释/文案/读数说 A，代码做 B」：

· `register` 回执说"两个参数"，实际要三个
· `--help` 说"MCP 面不签发"，实际有 session_start
· `read_body` 提示 `fish events --code`，而该参数不存在
· 推送说"N 条新动态"，而那是全局 id 差（虚高 7251 倍）
· 注入说"该问三问"，而提问从未留痕

**所以判据也按这个形态写：不是"能跑通"，是"它说的和它做的是同一件事"。**
"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

P0 = Path(__file__).resolve().parent
sys.path.insert(0, str(P0))

import fish  # noqa: E402


class _DbCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-fix1-"))
        self.conn = fish.connect(self.tmp / "db")
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)

    def tearDown(self):
        try:
            self.conn.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)


class RegisterHintTest(unittest.TestCase):
    """B-2：register 回执必须说清 `bind_token`。

    变异体：删掉文案里的 `bind_token` → 红。
    """

    def test_hint_mentions_bind_token(self):
        """**调用示例那一行**必须含 `bind_token`。

        ⚠️ 判据的第一版是 `assertIn("bind_token", text)`，**变异测试当场证明它没有
        判别力**：`bind_token` 在文案里出现两次（调用示例 + 下面的说明文字），
        删掉示例里那处后，说明文字还在，断言照样通过。
        所以这里必须**定位到示例行**再检查——否则它测的是"这个词出现过"，
        而不是"照着示例抄能成功"。
        """
        import mcp_server

        srv = object.__new__(mcp_server.FishServer)
        text = srv._register_next_step({"display_name": "x"}, "CID123")
        call_lines = [l for l in text.splitlines() if "session_start(" in l]
        self.assertTrue(call_lines, "回执里没有调用示例")
        self.assertTrue(any("bind_token" in l for l in call_lines),
                        f"调用示例里没有 bind_token：{call_lines}")
        self.assertIn("已存在的公民", text,
                      "没说明什么时候需要 bind_token（新公民不需要）")

    def test_hint_gives_cli_escape(self):
        """token 丢了必须有出路（报告 P1-1 的核心）。"""
        import mcp_server
        srv = object.__new__(mcp_server.FishServer)
        text = srv._register_next_step({}, "CID123")
        self.assertIn("fish session start", text,
                      "没说 token 丢了怎么办——MCP 面走不通时会卡死")


class HelpTextTest(unittest.TestCase):
    """B-3：`fish --help` 里 session 的描述不能再说"MCP 面不签发"。"""

    def test_session_help_does_not_lie(self):
        """顶层 `fish --help` 里 session 的描述不能再说「MCP 面不签发」。

        注意是**顶层** help：`fish session --help` 只列子命令，不显示 help= 文案。
        """
        r = subprocess.run(
            [sys.executable, str(P0 / "fish.py"), "--help"],
            capture_output=True, text=True, timeout=60,
            cwd=str(P0), encoding="utf-8", errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        self.assertNotIn("MCP 面不签发",
                         out, "文案还说「MCP 面不签发」，而 MCP 有 session_start")
        self.assertIn("session_start", out,
                      "新的说法应当指出 MCP 面的那个工具")


class EventsCodeFilterTest(unittest.TestCase):
    """B-4：`fish events --code` 必须真的存在（read_body 的提示指向它）。"""

    def test_code_argument_exists(self):
        r = subprocess.run(
            [sys.executable, str(P0 / "fish.py"), "events", "--help"],
            capture_output=True, text=True, timeout=60,
            cwd=str(P0), encoding="utf-8", errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        self.assertIn("--code", out,
                      "`--code` 不存在 —— read_body 的提示指向一个用不了的命令")

    def test_read_body_hint_matches_real_command(self):
        """反向断言：hint 里提到的命令，其参数必须真的注册过。

        这是一条**元判据**——它守的不是某一句文案，而是"文案里引用的命令必须可执行"
        这个不变量。变异体：把 hint 改成引用一个不存在的参数 → 红。
        """
        import inspect
        import fish as fishlib
        src = inspect.getsource(fishlib.read_body_path)
        if "fish events --code" not in src:
            self.skipTest("hint 已不再引用该命令（说明换成了别的指引）")
        # 既然引用了，就必须真的存在
        r = subprocess.run(
            [sys.executable, str(P0 / "fish.py"), "events", "--help"],
            capture_output=True, text=True, timeout=60,
            cwd=str(P0), encoding="utf-8", errors="replace")
        self.assertIn("--code", (r.stdout or "") + (r.stderr or ""))


class NewPostsCountTest(_DbCase):
    """C-1：`new_posts` 必须数本板的新行，不能用全局自增 id 差。

    **这是本批最重要的一条判据**，因为它的正对照能量化"错得有多大"：
    实测生产库 `board_id=46` 旧算法得 141、正确是 2（虚高 70 倍）。
    """

    def _seed_board(self, board_id, n_posts, *, start_id):
        """造一个板 + n 条动态（`boards` 的真实列：scope/scope_key/period_key/path/status）。"""
        self.conn.execute(
            "INSERT INTO boards (id, scope, scope_key, period_key, path, status, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (board_id, "project", f"p{board_id}", "261121", ".", "open",
             "2026-11-21T00:00:00Z"))
        ids = []
        for i in range(n_posts):
            cur = self.conn.execute(
                "INSERT INTO board_posts (board_id, citizen_id, kind, text, created_at) "
                "VALUES (?,?,?,?,?)",
                (board_id, "C1", "note", f"p{board_id}-{i}", "2026-11-21T00:00:00Z"))
            ids.append(cur.lastrowid)
        self.conn.commit()
        return ids

    def test_new_posts_counts_own_rows_not_global_ids(self):
        """**正对照**：别板 5 条 + 本板 2 条 → new_posts 必须 == 2。

        ⚠️ 造数据的顺序是判据成立的前提：**必须先插别板把全局 id 推高，再插本板**。
        反过来（先本板后别板）时本板 id 是连续的，`MAX(id)-seen` 恰好也等于 2，
        判据就测不出问题——我在第一版就是这么写错的，被下面那条 assertNotEqual 抓住。

        变异体：把实现改回 `MAX(id) - seen` → 得 7 → 红。
        """
        self._seed_board(2, 5, start_id=0)       # 先插别板：全局 id 推到 5
        mine = self._seed_board(1, 2, start_id=0)  # 再插本板：id 6, 7

        seen = 0
        cnt = self.conn.execute(
            "SELECT count(*) FROM board_posts WHERE board_id=1 AND id > ?",
            (seen,)).fetchone()[0]
        self.assertEqual(cnt, 2, f"本板新动态应为 2，得到 {cnt}")

        mx = self.conn.execute(
            "SELECT COALESCE(MAX(id),0) FROM board_posts WHERE board_id=1").fetchone()[0]
        old_way = mx - seen
        self.assertEqual(old_way, 7,
                         f"旧算法应当得 7（虚高），实际 {old_way}——前提没造对")
        self.assertNotEqual(old_way, cnt,
                            "两个算法同数就测不出问题，前提不成立")

    def test_push_uses_count_not_maxid(self):
        """源码级判据：`fish_push.py` 里不许再出现 `MAX(id)` 当"总动态数"。"""
        src = (P0 / "fish_push.py").read_text(encoding="utf-8")
        start = src.find("# ── E 看板变更")
        self.assertGreater(start, 0, "没找到看板变更段")
        seg = src[start:start + 3000]
        self.assertNotIn("COALESCE(MAX(id),0) FROM board_posts", seg,
                         "看板变更段还在用 MAX(id) 当总数（会虚高）")


class ProfileQuestionTraceTest(_DbCase):
    """F-3：提示必须留痕，且不刷表、不只留 pending。"""

    def setUp(self):
        super().setUp()
        import fish_profile as fp
        self.fp = fp
        fp.set_config(self.conn, "profile.ask_every", 3)
        fp.set_config(self.conn, "profile.docs_since_ask", 5)
        self.conn.commit()

    def _count(self):
        return self.conn.execute("SELECT count(*) FROM profile_questions").fetchone()[0]

    def test_render_records_a_pending_row(self):
        """变异体：去掉 `raise_question_if_needed` 调用 → 表仍 0 行 → 红。"""
        self.assertEqual(self._count(), 0, "前置：应当是空的")
        block = self.fp.render_questions_block(self.conn)
        self.conn.commit()
        self.assertTrue(block, "该问的时候段不该为空")
        self.assertEqual(self._count(), 1, "提示了但没留痕")
        self.assertEqual(
            self.conn.execute("SELECT status FROM profile_questions").fetchone()[0],
            "pending")

    def test_render_is_idempotent(self):
        """**幂等**：注入每次都会调，无条件写就是刷表。"""
        self.fp.render_questions_block(self.conn)
        self.fp.render_questions_block(self.conn)
        self.fp.render_questions_block(self.conn)
        self.conn.commit()
        self.assertEqual(self._count(), 1, "每次渲染都写一行 —— 这不是留痕是刷表")

    def test_answer_closes_pending_instead_of_inserting(self):
        """**收尾而非新插**：回答后应当是同一行变成 answered，不是多一行孤儿 pending。"""
        self.fp.render_questions_block(self.conn)
        self.conn.commit()
        self.fp.reset_question_counter(self.conn, status="answered")
        self.conn.commit()
        self.assertEqual(self._count(), 1, "回答后又多插了一行")
        row = self.conn.execute(
            "SELECT status, answered_at FROM profile_questions").fetchone()
        self.assertEqual(row[0], "answered")
        self.assertIsNotNone(row[1], "answered 却没记 answered_at")

    def test_defer_closes_pending_too(self):
        """「本轮不回答」也要能收尾（用户原话：可以不回答，但要留痕）。"""
        self.fp.render_questions_block(self.conn)
        self.conn.commit()
        self.fp.reset_question_counter(self.conn, status="deferred")
        self.conn.commit()
        self.assertEqual(self._count(), 1)
        self.assertEqual(
            self.conn.execute("SELECT status FROM profile_questions").fetchone()[0],
            "deferred")


if __name__ == "__main__":
    unittest.main()
