#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""系统级修复 · 第二批 A-1：断链清理的判据（2026-11-21）。

## 这份测试守的是什么

用户拍板「全部清理，避免以后推送和存储垃圾内容」。而「清理」在 fish 里有个
硬约束：**`events` 是 append-only**（`ev_no_delete` 触发器），所以物理删除
既做不到、也不该做。正确做法是**正式作废**（`memory.retired` + `withdrawn`）。

于是判据必须证明四件事：

1. 作废后 `recall` 不再返回它们（`status='withdrawn'`）；
2. **重建之后仍然不返回**——这条最关键，它证明「作废」进了事件流而不只是改了投影；
3. 事件是**追加**的，不是删的（`events` 行数增加，且历史事件仍在）；
4. 全文索引清了（`memory_fts` 是独立表，不删的话 `MATCH` 仍会命中）。

## 变异体（去掉 → 必红）

· `purge_broken` 只 UPDATE 不写 `memory.retired` → 判据 2 红（rebuild 复活）
· `purge_broken` 不删 `memory_fts` → 判据 4 红
· `purge_broken` 物理删 `code_index` 行 → 判据 3 红（或者事件没了）
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

P0 = Path(__file__).resolve().parent
sys.path.insert(0, str(P0))

import fish  # noqa: E402
import fish_purge  # noqa: E402


class _PurgeCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-purge-t-"))
        self.data = self.tmp / "data"
        self.data.mkdir(parents=True)
        self.conn = fish.connect(self.data)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO code_registry (prefix, namespace, status) VALUES ('T','测试','active')")
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def _make(self, title, *, body="正文", make_broken=False):
        """写一条记忆；`make_broken=True` 时把正文文件删掉（模拟迁移丢失）。"""
        code, rel = fish.record_memory(
            self.conn, self.data, project="T", title=title, body=body)
        self.conn.commit()
        if make_broken:
            p = self.data / rel
            if p.exists():
                p.unlink()
        return code

    def _broken_active(self):
        """当前仍然 active 的断链数（**这是判据的核心读数**）。"""
        rows = self.conn.execute(
            "SELECT code, file_path FROM code_index WHERE status='active'").fetchall()
        return sum(1 for _, fp in rows if fp and not (self.data / fp).exists())


class PurgeTest(_PurgeCase):

    def test_scan_finds_only_broken(self):
        self._make("好的", body="有正文")
        bad = self._make("坏的", make_broken=True)
        found = fish_purge.scan_broken(self.conn, self.data)
        self.assertEqual(found["count"], 1)
        self.assertEqual(found["codes"], [bad])

    def test_dry_run_writes_nothing(self):
        """**默认必须 dry-run**：作废不可逆，不能一句话就清了。"""
        self._make("坏的", make_broken=True)
        before_events = self.conn.execute("SELECT count(*) FROM events").fetchone()[0]
        res = fish_purge.purge_broken(self.conn, self.data, dry_run=True)
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["purged"], 0)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM events").fetchone()[0], before_events)
        self.assertEqual(self._broken_active(), 1, "dry-run 居然改了状态")

    def test_purge_marks_withdrawn(self):
        """J-A1a：作废后不再是 active。"""
        self._make("坏的", make_broken=True)
        res = fish_purge.purge_broken(self.conn, self.data, dry_run=False)
        self.assertEqual(res["purged"], 1)
        self.assertEqual(self._broken_active(), 0, "作废后仍然是 active")

    def test_purge_appends_events_does_not_delete(self):
        """J-A1c：**事件是追加的**，不是删的。

        变异体：物理删 `code_index` 行而不写事件 → 这里仍然通过（事件数不变），
        但下面 `test_survives_rebuild` 会红——两条一起才守得住。
        """
        self._make("坏的", make_broken=True)
        before = self.conn.execute("SELECT count(*) FROM events").fetchone()[0]
        fish_purge.purge_broken(self.conn, self.data, dry_run=False)
        after = self.conn.execute("SELECT count(*) FROM events").fetchone()[0]
        self.assertGreater(after, before, "没有追加任何事件——作废没进事件流")
        n_retired = self.conn.execute(
            "SELECT count(*) FROM events WHERE type='memory.retired'").fetchone()[0]
        self.assertEqual(n_retired, 1)
        # 原始 memory.recorded 必须还在（append-only 的意义）
        n_rec = self.conn.execute(
            "SELECT count(*) FROM events WHERE type='memory.recorded'").fetchone()[0]
        self.assertEqual(n_rec, 1, "memory.recorded 被删了——那是在篡改历史")

    def test_survives_rebuild(self):
        """**J-A1b：重建之后仍然干净。这条是整套清理成立与否的试金石。**

        `rebuild_projection` 从 `memory.recorded` 重建 `code_index`；
        如果作废只改了投影、没进事件流，重建会把它复活成 `active`。

        变异体：`purge_broken` 只 UPDATE 不写 `memory.retired` → 本判据红。
        """
        self._make("坏的", make_broken=True)
        fish_purge.purge_broken(self.conn, self.data, dry_run=False)
        self.assertEqual(self._broken_active(), 0, "作废后不干净")

        fish.rebuild_projection(self.conn, self.data)
        self.conn.commit()
        self.assertEqual(self._broken_active(), 0,
                         "重建把它复活了——作废没进事件流（只是改了投影）")

    def test_fts_entries_removed(self):
        """J-A1d：全文索引必须清（它是独立虚表，不删的话 MATCH 仍会命中）。

        ⚠️ 查询要用**单个 bigram 或英文词**，不能用整词：`memory_fts` 把中文按
        bigram 切分（标题"独一无二"存成 `独一 一无 无二`），`MATCH "独一无二"`
        永远 0 命中——我第一版就是这么写的，前置断言当场就红了。
        用英文词 `ftsuuid` 最稳（不会被切）。
        """
        code = self._make("标题ftsuuid", body="正文内容", make_broken=True)
        hit_before = self.conn.execute(
            "SELECT count(*) FROM memory_fts WHERE memory_fts MATCH ?",
            ('"ftsuuid"',)).fetchone()[0]
        self.assertEqual(hit_before, 1, "前置：FTS 里本该有它")
        fish_purge.purge_broken(self.conn, self.data, dry_run=False)
        hit_after = self.conn.execute(
            "SELECT count(*) FROM memory_fts WHERE memory_fts MATCH ?",
            ('"ftsuuid"',)).fetchone()[0]
        self.assertEqual(hit_after, 0, f"FTS 没清，仍能搜到 {code}")

    def test_good_memories_untouched(self):
        """**反向断言**：清理只动断链，不许误伤有正文的。

        变异体：去掉 exists 判断（全清）→ 红。
        """
        good = self._make("好的", body="有正文")
        self._make("坏的", make_broken=True)
        fish_purge.purge_broken(self.conn, self.data, dry_run=False)
        row = self.conn.execute(
            "SELECT status FROM code_index WHERE code=?", (good,)).fetchone()
        self.assertEqual(row[0], "active", "有正文的记忆被误作废了")


class ReportTest(_PurgeCase):
    """A-3：一致性巡检必须给出**结构**，不只是一个数字。"""

    def test_report_has_date_structure(self):
        """断链的日期分布是定位根因的关键（85 号调查里正是靠它看出来的）。"""
        self._make("坏的", make_broken=True)
        rep = fish_purge.consistency_report(self.conn, self.data)
        self.assertEqual(rep["broken"], 1)
        self.assertIn("broken_by_date", rep)
        self.assertEqual(sum(rep["broken_by_date"].values()), 1,
                         "按日期分组的计数和总数对不上")
        self.assertIn("broken_by_project", rep)


if __name__ == "__main__":
    unittest.main()
