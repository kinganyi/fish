#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`recall --since/--until` 的判据（2026-11-22）。

## 它守的是什么

`daily` 轨要搬进 fish 的 `episode` 类型，靠的就是 `occurred_at` 分组。
但实测发现 `recall` **没有日期过滤**——**写得进去，按日读不回来**。
这个文件守那个缺口补上之后的行为。

## 最容易错的那一条（J3）

`--until 2026-11-21` 必须**包含 21 号全天**。
如果实现里直接拼 `occurred_at <= '2026-11-21'`，那么 `2026-11-21T08:00:00Z`
会被排除——因为 `'…T08:00:00Z' > '2026-11-21'`（字符串比较）。
**用户要"到 21 号"，拿到的是"到 20 号"**，而结果集看起来完全正常，没人会发现。

变异体：把 `_norm_date_bound(..., end=True)` 里的 `T23:59:59Z` 改成 `T00:00:00Z`
（或直接返回原值）→ J3 必红。
"""
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

P0 = Path(__file__).resolve().parent
sys.path.insert(0, str(P0))

import fish  # noqa: E402


class DateBoundTest(unittest.TestCase):
    """J1：日期边界的规范化。"""

    def test_j1_plain_date_gets_time(self):
        f = fish._norm_date_bound
        self.assertEqual(f("2026-11-22"), "2026-11-22T00:00:00Z")
        self.assertEqual(f("2026-11-22", end=True), "2026-11-22T23:59:59Z",
                         "until 的纯日期必须补到当天终点（否则漏当天）")

    def test_j1b_full_iso_passes_through(self):
        f = fish._norm_date_bound
        self.assertEqual(f("2026-11-22T08:00:00Z"), "2026-11-22T08:00:00Z")
        self.assertEqual(f("2026-11-22T08:00:00Z", end=True), "2026-11-22T08:00:00Z",
                         "给了精确时刻就尊重它，不要擅自扩到当天终点")

    def test_j1c_empty_is_none(self):
        f = fish._norm_date_bound
        self.assertIsNone(f(None))
        self.assertIsNone(f(""))
        self.assertIsNone(f("   "))


class RecallDateFilterTest(unittest.TestCase):
    """J2–J5：真实数据上的过滤行为。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-date-"))
        self.data = self.tmp / "data"
        self.data.mkdir(parents=True)
        self.conn = fish.connect(self.data)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT OR IGNORE INTO code_registry (prefix, namespace, status) "
            "VALUES ('T','t','active')")
        self.conn.commit()
        # 造 5 条：19 / 20 / 21(两条，含当天 08:00) / 22
        for occurred in ("2026-11-19T10:00:00Z", "2026-11-20T10:00:00Z",
                         "2026-11-21T00:30:00Z", "2026-11-21T08:00:00Z",
                         "2026-11-22T09:00:00Z"):
            fish.record_memory(self.conn, self.data, project="T",
                               title=f"事件 {occurred}", body="x",
                               occurred_at=occurred)
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def _list(self, **kw):
        rows = fish.list_memories(self.conn, project="T", limit=50, **kw)
        return [r[0] for r in rows]

    def _count(self, **kw):
        return fish.count_memories(self.conn, project="T", **kw)

    def test_j2_no_bound_returns_all(self):
        """J2：不给日期时行为**一个字都不变**（向后兼容）。"""
        self.assertEqual(self._count(), 5)
        self.assertEqual(len(self._list()), 5)

    def test_j3_until_includes_whole_day(self):
        """J3（重点）：`until=2026-11-21` 必须包含 **21 号全天的两条**。

        变异体：`_norm_date_bound` 的 end 分支不补 `T23:59:59Z` → 只剩 19/20 两条 → 红。
        """
        self.assertEqual(self._count(until="2026-11-21"), 4,
                         "漏了 21 号当天的条目（off-by-one）："
                         f"实际拿到 {self._list(until='2026-11-21')}")
        codes = self._list(until="2026-11-21")
        self.assertTrue(any("2026-11-21T08:00:00Z" in c or "08:00" in c for c in codes) or
                        len(codes) == 4)

    def test_j4_since_excludes_before(self):
        """J4：`since=2026-11-22` 只剩 22 号那一条。"""
        self.assertEqual(self._count(since="2026-11-22"), 1)

    def test_j5_bounds_are_additive(self):
        """J5：`since=11-22` 的条数 + `until=11-21` 的条数 == 总数。

        这条是**自洽性判据**：单独验任一边都可能错得对称（比如两边都少一天），
        只有加起来等于总数才说明边界没有重叠也没有缝隙。
        """
        total = self._count()
        later = self._count(since="2026-11-22")
        earlier = self._count(until="2026-11-21")
        self.assertEqual(later + earlier, total,
                         f"边界不自洽：{later} + {earlier} != {total}")

    def test_j6_search_mode_also_filters(self):
        """J6：检索模式（有 query）也要受日期约束——两个入口不能给不同答案。"""
        rows, _ = fish.search_memories(self.conn, query="事件", project="T",
                                       limit=50, since="2026-11-22")
        self.assertEqual(len(rows), 1, f"检索模式没按日期过滤：{[r[0] for r in rows]}")

    def test_j7_null_occurred_falls_back(self):
        """J7：`occurred_at` 为空的条目退回 `recorded_at` 参与过滤，不被误杀。"""
        code, _ = fish.record_memory(self.conn, self.data, project="T",
                                     title="无 occurred_at 的条目", body="x")
        self.conn.execute("UPDATE code_index SET occurred_at=NULL WHERE code=?", (code,))
        self.conn.commit()
        # recorded_at 是"现在"，所以 since=一个很早的日期时应当能查到
        codes = self._list(since="2020-01-01")
        self.assertIn(code, codes, "occurred_at 为空时被误杀（应退回 recorded_at）")


if __name__ == "__main__":
    unittest.main()
