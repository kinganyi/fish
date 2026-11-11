#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部审查 🔴1 的线上故障判据：KB 索引"新鲜度"不能只看 `MAX(indexed_at)`。

事故形态（2026-11-11 生产实测）：
  · `kb_index` 296 行，`indexed_at` 全在 2026-10-27T20:43（一次性全量建库）；
  · `maintenance_task.kb_reindex.last_run_at = 2026-11-10T15:00:13Z`，`status=ok`，
    `last_affected=0`（vault 没改动，增量无行可刷）；
  · 旧口径取 `MAX(indexed_at)` → age 345.7h > 168h 阈值 → `fresh=False`；
  · K7 断路器随即让 KB 候选**恒为 0 且完全静默** —— 索引没错、维护正常跑，却被判陈旧。

判据（成对，任一侧失效都会红）：
  F1  台账"成功且更新" → fresh=True（核心修复：台账也是新鲜证据）
  F2  无台账行 → fresh=False（未跑过维护时不该假装新鲜）
  F3  台账 status != ok → fresh=False（失败/中断的运行不算证据）
  F4  台账也旧 → fresh=False（两个证据都旧才是真陈旧）
  F5  台账更新但 index 也更新 → 取更新者，仍 True
  F6  无 active 行 → fresh=False（"没建过"与"刚建好"必须能区分）
"""
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fish_kb_index as ki  # noqa: E402
import fish_maintenance as fm  # noqa: E402  —— 台账表 DDL 在维护模块里


def _stamp(hours_ago):
    """相对当前时刻的时间戳（**不硬编码日期**）。

    为什么必须相对：`kb_index_fresh` 的判据是"距今多少小时 < 阈值"，
    写死一个日期会让本测试在 N 天后自己变红（那时它测的不再是逻辑，
    而是"日历有没有往前走"）。
    """
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


OLD = _stamp(24 * 30)   # 30 天前：远超 168h 阈值
NEW = _stamp(1)         # 1 小时前：明显新鲜


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(ki._KB_INDEX_DDL)
    conn.executescript(fm._TASK_TABLE_DDL)
    return conn


def _index_row(conn, rel="a.md", stamp=OLD, status="active"):
    conn.execute(
        "INSERT INTO kb_index (vault_id, rel_path, mtime, size, indexed_at, status) "
        "VALUES (?,?,?,?,?,?)", ("v1", rel, 1.0, 10, stamp, status))
    conn.commit()


def _ledger(conn, stamp, status="ok", task="kb_reindex"):
    conn.execute(
        "INSERT OR REPLACE INTO maintenance_task "
        "(task_name, enabled, interval_hours, last_run_at, last_status, last_affected) "
        "VALUES (?,?,?,?,?,?)", (task, 1, 24.0, stamp, status, 0))
    conn.commit()


class KbFreshnessTest(unittest.TestCase):

    def test_F1_ledger_ok_and_recent_makes_fresh(self):
        """核心：台账成功跑过（哪怕 0 行变更），索引就是新鲜的。"""
        conn = _db()
        _index_row(conn, stamp=OLD)
        _ledger(conn, NEW, "ok")
        out = ki.kb_index_fresh(conn)
        self.assertTrue(out["fresh"], f"台账 {NEW} 成功但被判陈旧：{out}")
        self.assertEqual(out["last_indexed_at"], NEW,
                         "新鲜度应取两者中更新的那个")
        self.assertEqual(out["last_reindex_run_at"], NEW)
        conn.close()

    def test_F2_no_ledger_stays_stale(self):
        """没有台账行时不得假装新鲜（未跑过维护 = 无证据）。"""
        conn = _db()
        _index_row(conn, stamp=OLD)
        out = ki.kb_index_fresh(conn)
        self.assertFalse(out["fresh"])
        self.assertIsNone(out["last_reindex_run_at"])
        conn.close()

    def test_F3_ledger_failed_not_counted(self):
        """台账状态不是 ok（失败/中断）不算新鲜证据。"""
        conn = _db()
        _index_row(conn, stamp=OLD)
        _ledger(conn, NEW, "error")
        out = ki.kb_index_fresh(conn)
        self.assertFalse(out["fresh"], f"失败的运行被当成新鲜证据：{out}")
        conn.close()

    def test_F4_both_old_stays_stale(self):
        """两个证据都旧 → 真陈旧（这条防"无脑放行"）。"""
        conn = _db()
        _index_row(conn, stamp=OLD)
        _ledger(conn, OLD, "ok")
        out = ki.kb_index_fresh(conn)
        self.assertFalse(out["fresh"])
        conn.close()

    def test_F5_index_newer_than_ledger_uses_index(self):
        """索引比台账更新时取索引（取更新者是双向的）。"""
        conn = _db()
        _index_row(conn, stamp=NEW)
        _ledger(conn, OLD, "ok")
        out = ki.kb_index_fresh(conn)
        self.assertTrue(out["fresh"])
        self.assertEqual(out["last_indexed_at"], NEW)
        conn.close()

    def test_F6_no_active_rows_not_fresh(self):
        """只有非 active 行 = 没建过，不算新鲜。"""
        conn = _db()
        _index_row(conn, rel="b.md", stamp=NEW, status="orphan")
        _ledger(conn, NEW, "ok")
        out = ki.kb_index_fresh(conn)
        self.assertFalse(out["fresh"])
        self.assertEqual(out["active_rows"], 0)
        conn.close()

    def test_F7_other_task_ledger_ignored(self):
        """别的维护任务的台账不能冒充 kb_reindex。"""
        conn = _db()
        _index_row(conn, stamp=OLD)
        _ledger(conn, NEW, "ok", task="fts_verify")
        out = ki.kb_index_fresh(conn)
        self.assertFalse(out["fresh"], "拿别的任务的台账冒充了新鲜证据")
        conn.close()


class KbHealthLinesTest(unittest.TestCase):
    """🔴1c：`fish healthcheck` 必须能看见 KB 索引状态（否则静默失效查不出来）。

    这条判据的价值不在"能打印"，而在**陈旧时必须把后果说出来**——
    2026-11-11 那次的静默失效，正是因为 healthcheck 里根本没有 KB 这一段。
    """

    def test_H1_not_built(self):
        conn = _db()
        lines = " ".join(ki.health_lines(conn))
        self.assertIn("未建", lines)
        conn.close()

    def test_H2_fresh_says_fresh_without_breaker(self):
        conn = _db()
        _index_row(conn, stamp=OLD)
        _ledger(conn, NEW, "ok")
        lines = " ".join(ki.health_lines(conn))
        self.assertIn("新鲜", lines)
        self.assertNotIn("K7", lines, "新鲜时不该报断路器")
        conn.close()

    def test_H3_stale_names_the_breaker_consequence(self):
        """**核心**：陈旧时必须说清"KB 候选恒为 0"这个后果，而不只是"旧了"。"""
        conn = _db()
        _index_row(conn, stamp=OLD)
        _ledger(conn, OLD, "ok")
        lines = " ".join(ki.health_lines(conn))
        self.assertIn("陈旧", lines)
        self.assertIn("K7", lines, "陈旧时必须点名断路器，否则用户不知道该结什么果")
        self.assertIn("恒为 0", lines)
        conn.close()

    def test_H4_error_rows_are_reported(self):
        conn = _db()
        _index_row(conn, rel="ok.md", stamp=OLD)
        _index_row(conn, rel="bad.md", stamp=OLD, status="error")
        _ledger(conn, NEW, "ok")
        lines = " ".join(ki.health_lines(conn))
        self.assertIn("error 状态", lines)
        conn.close()

    def test_H5_never_raises_and_says_unknown(self):
        """三态分明：体检函数**绝不抛**，且真异常必须写"判不到"（不许静默当"没问题"）。

        用 monkeypatch 造真异常（而不是伪造一个坏 conn）——因为 `_kb_index_tables`
        本身就把"读不到表"吞成 False，伪造坏 conn 只会走到"未建"那条正常分支，
        测不到"判不到"。
        """
        from unittest import mock
        conn = _db()
        _index_row(conn, stamp=OLD)
        with mock.patch.object(ki, "kb_index_stats",
                               side_effect=RuntimeError("boom")):
            lines = " ".join(ki.health_lines(conn))
        self.assertIn("判不到", lines)
        self.assertIn("RuntimeError", lines)
        conn.close()


if __name__ == "__main__":
    unittest.main()
