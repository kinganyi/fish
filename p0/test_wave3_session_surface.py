#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Wave 3：会话累计占用面（`presence.session_surface`，2026-11-22）。

## 为什么需要第二列

`auto_surface` 是 **30 分钟窗的瞬时面**（回答"此刻谁在这块"），它服务重叠检测。
但"这个人在这块做过什么"是另一个问题，而且不能靠拉长它的窗口来回答——
把瞬时面改成累计，任何"先后做过"都会被判成"同时在做"，假阳性暴涨。

所以新增一列，语义分离：

    auto_surface     30 分钟瞬时  → 重叠检测（判据一个字不改）
    session_surface  会话累计     → 观测 / 给看板多一格

## 三个刻意的边界

  1. **不进 `_PRESENCE_V3_COLS`**：那是重建时的拷贝源，老表没这列会 SQL 报错。
     它是缓存，重建后由下一次活动重算。
  2. **只在"累计 ≠ 瞬时"时展示**：两者相同说两遍是噪音。
  3. **`session_dirs=None` 时完全不碰这一列**：既有调用方（不传这个参数）
     行为逐字节不变。

## 判据

    W1  迁移后 `presence` 有 `session_surface` 列
    W2  `session_dirs` 给了就写；不给就不碰（旧行为不变）
    W3  累计 ≠ 瞬时 → 看板多一格「累计」
    W4  累计 = 瞬时 → 不重复说
"""

import argparse
import importlib.util
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

import fish_activity  # noqa: E402
import fish_board     # noqa: E402

CID = "01W3SESS00000000000001"
SK = "dsh:session-w3aaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class SessionSurfaceTest(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO citizens (citizen_id, display_name, kind, status,"
            " created_at, is_owner) VALUES (?,?,'role','active',?,'0')",
            (CID, "会话面测试", fish.now_utc()))
        self.conn.commit()
        fish.cmd_project_add(argparse.Namespace(
            root=str(self.root), prefix="W3PROJ", name="Wave3 测试"))
        self.board = fish.board_checkin(
            self.conn, CID, "在改", surface="", scope="project",
            scope_key="W3PROJ", session_key=SK)

    def tearDown(self):
        self.conn.close()

    def _cell(self, sql, *params):
        return self.conn.execute(sql, params).fetchone()

    # ------------------------------------------------------------------ W1
    def test_W1_column_exists_after_migrate(self):
        """迁移后 `presence` 必须有 `session_surface`。

        变异：把 `_PRESENCE_NEW_COLS` 里那一项删掉 → 本条变红。
        """
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(presence)")}
        self.assertIn("session_surface", cols,
                      "旧库不会自己长列，迁移没补上")

    def test_W1b_column_is_not_in_the_rebuild_copy_list(self):
        """`session_surface` **不能**进 `_PRESENCE_V3_COLS`。

        那条是重建时的 `INSERT ... SELECT` 列清单；老表没有这一列，
        进了就会 `no such column` —— 整个升级路径崩在启动时。

        变异：把它加进 `_PRESENCE_V3_COLS` → 本条变红。
        """
        import fish_schema
        self.assertNotIn("session_surface", fish_schema._PRESENCE_V3_COLS)
        self.assertIn("session_surface",
                      [n for n, _d in fish_schema._PRESENCE_NEW_COLS])

    # ------------------------------------------------------------------ W2
    def test_W2_writes_cumulative_when_given(self):
        """`session_dirs` 给了就写进 `session_surface`。

        变异：把 `refresh_presence_cache` 里那个 `if cum is None` 分支反向
        （恒走旧分支）→ 本条变红。
        """
        ok = fish_activity.refresh_presence_cache(
            self.conn, citizen_id=CID, project="W3PROJ",
            dirs=["p0"], seen_at=fish.now_utc(), min_interval_sec=0,
            session_key=SK, session_dirs=["p0", "docs"])
        self.assertTrue(ok)
        got = self._cell(
            "SELECT session_surface FROM presence WHERE citizen_id=?", CID)[0]
        self.assertEqual(got, "p0,docs")

    def test_W2b_leaves_column_alone_when_not_given(self):
        """**不传 `session_dirs` 时一个字都不碰**——既有调用方行为不变。

        变异：把 `if cum is None` 判断删掉（总是写）→ 本条变红。
        """
        self.conn.execute(
            "UPDATE presence SET session_surface='KEEP' WHERE citizen_id=?", (CID,))
        self.conn.commit()
        fish_activity.refresh_presence_cache(
            self.conn, citizen_id=CID, project="W3PROJ",
            dirs=["p0"], seen_at=fish.now_utc(), min_interval_sec=0,
            session_key=SK)
        got = self._cell(
            "SELECT session_surface FROM presence WHERE citizen_id=?", CID)[0]
        self.assertEqual(got, "KEEP", "没传参数却改了这一列")

    # ------------------------------------------------------------------ W3
    def test_W3_board_shows_cumulative_when_it_differs(self):
        """累计 ≠ 瞬时 → 注入包多出一格「累计」。

        变异：把 `board_block` 里那段 `if _cum and _cum != ...` 删掉 → 本条变红。
        """
        self.conn.execute(
            "UPDATE presence SET auto_surface='p0', session_surface='p0,docs'"
            " WHERE citizen_id=?", (CID,))
        self.conn.commit()
        blk = fish.board_block(self.conn, project="W3PROJ", citizen_id=CID,
                               session_key=SK)
        # 断言用**带分隔符的标记**，不用裸词"累计"——本测试的公民名叫
        # 「累计面测试」，裸词会被它满足（同类假绿我这一轮已经踩到第三次）。
        self.assertIn("｜累计 ", blk or "")

    def test_W4_board_stays_quiet_when_cumulative_equals_instant(self):
        """累计 = 瞬时 → 不重复说（说两遍是噪音）。

        变异：把那个 `!=` 判断改成恒真 → 本条变红。
        """
        self.conn.execute(
            "UPDATE presence SET auto_surface='p0', session_surface='p0'"
            " WHERE citizen_id=?", (CID,))
        self.conn.commit()
        blk = fish.board_block(self.conn, project="W3PROJ", citizen_id=CID,
                               session_key=SK)
        self.assertNotIn("｜累计 ", blk or "",
                         "累计和瞬时一样时不该再说一遍")


if __name__ == "__main__":
    unittest.main(verbosity=2)
