#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Wave 2：占用面提示 + 帖子关联记忆（2026-11-22）。

## 这一轮要解决的两个"数据在，但用不上"

**① 占用面（surface）**：实测手工 `checkin` 的填写率是 **93.3%**——意愿没问题；
拉低到 23% 的是自动签到（占 85%，且按纪律不编造）。所以问题不是"AI 不愿意填"，
而是**没人告诉它为什么值得填**。旧 CLI help 只有一句"占用面（文件/目录前缀）"。

这里加一条**只对当前会话、只在其没填时**出现的提示（避免逐行刷屏）。

**② 帖子→记忆的追溯**：412 条帖子里只有 43 条（10.4%）带记忆编码，其余 369 条
是孤立的。数据源用 `events` 的 `session_actor`（实测 75.5% 覆盖）而不是
`code_index`——后者只有 `owner`，区分不出同一公民的不同会话。

## 判据

    S1  `_surface_hint` 在「有 doing、没 surface」时出提示
    S2  已经填了 surface 就不说（不能每轮都念）
    S3  连 doing 都没有就不说（那是"还没登记"，归 checkin 的引导管）
    S4  `board_post` 把 `session_key` 存进库
    S5  `autolink_memory_code` 在正文没编码时补上本会话最近那条，并**标明是自动推断**
    S6  正文已有编码就不补（不重复、不覆盖作者原意）
    S7  开关关掉就完全不补
    S8  不跨会话关联（别的会话写的记忆不许贴到这条帖子上）
"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

import fish_board  # noqa: E402

CID = "01WAVE2SURF000000000001"
CID2 = "01WAVE2OTHR000000000002"
SK = "dsh:session-wave2aaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SK2 = "dsh:session-wave2zzzz-yyyy-xxxx-wwww-vvvvvvvvvvvv"


class SurfaceHintTest(unittest.TestCase):
    """Wave 2 ①：占用面提示。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        for cid, name in ((CID, "占用面测试AI"), (CID2, "另一个AI")):
            self.conn.execute(
                "INSERT INTO citizens (citizen_id, display_name, kind, status,"
                " created_at, is_owner) VALUES (?,?,'role','active',?,'0')",
                (cid, name, fish.now_utc()))
        self.conn.commit()
        self.board = fish.board_ensure(self.conn, "project", "W2PROJ")

    def tearDown(self):
        self.conn.close()

    def _checkin(self, cid, doing, surface="", sk=SK):
        fish.board_checkin(self.conn, cid, doing, surface=surface,
                           scope="project", scope_key="W2PROJ", session_key=sk)
        # board_checkin 会建/取板，重新取一遍以防万一
        self.board = fish.board_ensure(self.conn, "project", "W2PROJ")
        return self.board

    # ------------------------------------------------------------------ S1
    def test_S1_hint_fires_when_surface_missing(self):
        """有 doing、没 surface → 出提示，且说清"为什么值得填"。

        变异：删掉 `_surface_hint` 里 `if surface or not doing: return ""` 的
        `not doing` 部分不影响本条；M1 删的是 `surface` 判断 → S2 红（本条仍绿，
        因为本条本来就是"没填"场景）。所以本条的变异目标是**接线**：
        把 `board_block` 里那两行 `surf = ...` 删掉 → 本条变红。
        """
        self._checkin(CID, "在改看板", surface="", sk=SK)
        hint = fish_board._surface_hint(self.conn, self.board, CID, SK)
        self.assertTrue(hint, "没填占用面却没收到提示")
        self.assertIn("surface", hint)
        self.assertIn("看得见", hint, "必须说清它的价值是'让人看见'")

    # ------------------------------------------------------------------ S2
    def test_S2_silent_when_surface_filled(self):
        """已经填了 surface → 不再提示。

        变异：M1 删掉 `if surface or not doing: return ""` 里的 `surface` 判断
        （改成 `if not doing: return ""`）→ 本条变红。
        """
        self._checkin(CID, "在改看板", surface="p0/fish.py", sk=SK)
        self.assertEqual(
            fish_board._surface_hint(self.conn, self.board, CID, SK), "",
            "已经填了占用面还在劝，就是噪音")

    # ------------------------------------------------------------------ S3
    def test_S3_silent_when_not_checked_in(self):
        """连 doing 都没有 → 不说（那是"还没登记"，归 checkin 引导管）。

        变异：把 `if surface or not doing` 改成 `if surface` → 本条变红。
        """
        self.conn.execute(
            "INSERT INTO presence (board_id, citizen_id, session_key, status, doing,"
            " surface, first_seen_at, last_seen_at) VALUES (?,?,?,'active','','',?,?)",
            (self.board["id"], CID, SK, fish.now_utc(), fish.now_utc()))
        self.conn.commit()
        self.assertEqual(
            fish_board._surface_hint(self.conn, self.board, CID, SK), "",
            "没有 doing 时不该念占用面——读者会以为是两件事")

    # ------------------------------------------------------------------ S1b
    def test_S1b_hint_reaches_the_injection_block(self):
        """提示必须真的进注入包（不能只是函数返回对了）。

        这是"零件对了、装配没接"那类坑的探针——本仓里
        `render_questions_block` 就这么死过一周。

        变异：删掉 `board_block` 里 `surf = _surface_hint(...)` 那两行 → 本条变红。
        """
        self._checkin(CID, "在改看板", surface="", sk=SK)
        blk = fish.board_block(self.conn, project="W2PROJ", citizen_id=CID,
                               session_key=SK)
        self.assertIsNotNone(blk)
        # ⚠️ 断言必须用**用户可见的特征句**，不能用"占用面"这种词——
        # 代码注释 `# 占用面提示（...）` 里就含那三个字，删掉接线后注释还在，
        # 断言依然为真。变异测试 M6 第一次跑就是绿的，抓到的正是这个假绿。
        self.assertIn("看得见你在动哪几个文件", blk,
                      "占用面提示没有真正进注入包（只有注释不算）")


class PostAutolinkTest(unittest.TestCase):
    """Wave 2 ②：帖子绑会话 + 关联记忆。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO citizens (citizen_id, display_name, kind, status,"
            " created_at, is_owner) VALUES (?,?,'role','active',?,'0')",
            (CID, "关联测试AI", fish.now_utc()))
        self.conn.commit()
        # `record_memory` 要求前缀先在 `code_registry` 里登记（同 test_board.py 的做法：
        # 走真实的 cmd_project_add，而不是手工插 registry）
        import argparse
        fish.cmd_project_add(argparse.Namespace(
            root=str(self.root), prefix="W2PROJ", name="Wave2 测试项目"))
        self.board = fish.board_ensure(self.conn, "project", "W2PROJ")

    def tearDown(self):
        self.conn.close()

    def _record(self, code, session_actor=SK, title="测试记忆"):
        # `record_memory` 返回 `(code, body_ref)` 元组；这里只关心 code
        return fish.record_memory(
            self.conn, self.root, project="W2PROJ", title=f"{title}-{code}",
            body="正文", owner=CID, session_actor=session_actor)[0]

    # ------------------------------------------------------------------ S4
    def test_S4_board_post_stores_session_key(self):
        """`board_post` 必须把 `session_key` 存进库。

        变异：M4 把 INSERT 的 session_key 列去掉 → 本条变红。
        """
        fish.board_post(self.conn, self.board, CID, "note", "一条动态",
                        session_key=SK)
        got = self.conn.execute(
            "SELECT session_key FROM board_posts ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        self.assertEqual(got, SK)

    # ------------------------------------------------------------------ S5
    def test_S5_autolink_appends_recent_memory_of_same_session(self):
        """正文没编码 → 补上本会话最近那条，并**标明是自动推断**。

        变异：M2 删掉 `if _POST_CODE_RE.search(body): return body` 之外的补码逻辑
        （把 `return f"{body}（自动关联 {code}）"` 改成 `return body`）→ 本条变红。
        """
        code = self._record("W2LINK-1")
        out = fish_board.autolink_memory_code(self.conn, "跑通了", SK)
        self.assertIn("跑通了", out)
        self.assertIn(code, out, "没补上编码")
        self.assertIn("自动关联", out, "必须标明是自动推断，不能冒充作者指定")

    # ------------------------------------------------------------------ S6
    def test_S6_autolink_does_not_touch_text_that_already_has_a_code(self):
        """正文已有编码 → 原样返回（不重复、不覆盖作者原意）。

        变异：M2 删掉 `if _POST_CODE_RE.search(body): return body` → 本条变红。
        """
        self._record("W2LINK-2")
        body = "修完了，见 W2PROJ-261122-99 那条"
        self.assertEqual(fish_board.autolink_memory_code(self.conn, body, SK), body)

    # ------------------------------------------------------------------ S7
    def test_S7_autolink_can_be_switched_off(self):
        """开关关掉 → 完全不补。

        变异：M3 把 `if not enabled or not body: return body` 里的 `not enabled`
        去掉 → 本条变红。
        """
        self._record("W2LINK-3")
        out = fish_board.autolink_memory_code(self.conn, "跑通了", SK, enabled=False)
        self.assertEqual(out, "跑通了", "开关关掉了还在补")

    # ------------------------------------------------------------------ S8
    def test_S8_autolink_does_not_cross_sessions(self):
        """**不跨会话**：别的会话写的记忆不许贴到这条帖子上。

        这条是最重要的边界——跨会话关联就是编造因果。

        变异：把 `recent_memory_code_for_session` 的 `AND session_actor=?` 去掉
        → 本条变红。
        """
        self._record("W2LINK-4", session_actor=SK2)     # 别的会话写的
        out = fish_board.autolink_memory_code(self.conn, "跑通了", SK)
        self.assertEqual(out, "跑通了", "把别的会话的记忆贴过来了")

    def test_S8b_no_cross_day_autolink(self):
        """超出时间窗的记忆也不关联（两天前的记忆与今天的帖子没有因果关系）。"""
        # `events` 有"不可变"触发器，不能 UPDATE；但可以直插一条**老**事件来造场景。
        self.conn.execute(
            "INSERT INTO events (seq, event_id, ts, type, actor, subject, payload,"
            " cause, refs, schema) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (900001, "ev-w2old", "2000-01-01T00:00:00Z", "memory.recorded",
             "system", "W2PROJ", json.dumps({"code": "W2LINK-OLD-5",
                                             "session_actor": SK}),
             None, None, 1))
        self.conn.commit()
        out = fish_board.autolink_memory_code(self.conn, "跑通了", SK)
        self.assertEqual(out, "跑通了", "跨天的记忆被关联了")


if __name__ == "__main__":
    unittest.main(verbosity=2)
