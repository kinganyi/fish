#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部审查 F-P0-1 判据：记忆写入必须**单事务**。

## 审查方实测的问题（已复核成立）

`record_memory` 旧实现是三次独立提交：

    conn.execute("INSERT INTO code_index ...")
    conn.commit()                            # ← 投影先落
    append_event(conn, "memory.recorded")    # ← 事件后写（内部 commit）
    _index_one_fts(conn, code, ...)          # ← FTS 最后（内部硬编码 commit）

事件或 FTS 失败 → **投影有、事件无、FTS 无**。而 `query` 照常返回那条记忆，
所以**没有任何东西会红**——直到有人去重建或审计才发现。

## 这批判据为什么能失败

关键是**注入真失败**，不是"看代码写了 try"。所以用 monkeypatch 把
`_index_one_fts` / `append_event` 换成会抛异常的版本，然后断言三张表
**行数全部回到写前**、且**正文文件不残留**。

如果实现退回"先 commit 投影"，第一条断言立刻红。
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish  # noqa: E402


class AtomicWriteTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()
        self.conn = fish.connect(self.data)
        self.conn.executescript(fish.SCHEMA)
        # 编码分配要查 code_registry；测试里直接登记前缀（`cmd_project_add` 会自己
        # 开连接，不适合内存/临时库）
        self.conn.execute(
            "INSERT INTO code_registry (prefix, namespace, last_date, last_seq)"
            " VALUES ('P','P','2026-11-18',0)")
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def _counts(self):
        return {
            "code_index": self.conn.execute("SELECT COUNT(*) FROM code_index").fetchone()[0],
            "events": self.conn.execute(
                "SELECT COUNT(*) FROM events WHERE type='memory.recorded'").fetchone()[0],
            "fts": self.conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0],
        }

    def _files(self):
        p = self.data / "projects" / "P"
        return sorted(x.name for x in p.glob("*.md")) if p.is_dir() else []

    def _record(self):
        return fish.record_memory(self.conn, self.data, project="P", title="原子性测试",
                                  body="# 正文\n\n内容\n", mem_type="fact")

    # ------------------------------------------------------------- 主判据
    def test_fts_failure_rolls_back_everything(self):
        """**F-P0-1 的直接探针**：FTS 写失败时，投影/事件/正文全部不许留下。"""
        before = self._counts()
        real = fish._index_one_fts

        def boom(*a, **kw):
            raise sqlite3.OperationalError("模拟 FTS 写失败")

        with mock.patch.object(fish, "_index_one_fts", boom):
            with self.assertRaises(sqlite3.OperationalError):
                self._record()

        after = self._counts()
        self.assertEqual(after, before,
                         f"FTS 失败后状态不一致：{before} → {after}"
                         "（投影/事件/FTS 应全部回滚）")
        self.assertEqual(self._files(), [], "正文文件没被清理（留下孤儿文件）")

    def test_event_failure_rolls_back_everything(self):
        """事件写失败同样必须整体回滚——这是"投影先 commit"最典型的暴露口。"""
        before = self._counts()
        real_append = fish.append_event

        def boom(*a, **kw):
            raise sqlite3.OperationalError("模拟事件写失败")

        with mock.patch.object(fish, "append_event", boom):
            with self.assertRaises(sqlite3.OperationalError):
                self._record()

        after = self._counts()
        self.assertEqual(after, before, f"事件失败后状态不一致：{before} → {after}")
        self.assertEqual(self._files(), [], "正文文件没被清理")

    # ------------------------------------------------------------- 对照组
    def test_success_writes_all_three(self):
        """**未变异的对照组必须绿**——否则"全红"可能只是判据读空。"""
        before = self._counts()
        code, *_ = self._record()
        after = self._counts()
        self.assertEqual(after["code_index"], before["code_index"] + 1)
        self.assertEqual(after["events"], before["events"] + 1)
        self.assertEqual(after["fts"], before["fts"] + 1)
        self.assertEqual(len(self._files()), 1)
        hits = self.conn.execute(
            "SELECT code FROM memory_fts WHERE code=?", (code,)).fetchall()
        self.assertEqual(len(hits), 1, "写成功却查不到全文索引")

    def test_retry_after_failure_does_not_duplicate(self):
        """失败后重试不许留下重复（审查方的验收判据之一）。"""
        def boom(*a, **kw):
            raise sqlite3.OperationalError("模拟失败")

        with mock.patch.object(fish, "_index_one_fts", boom):
            with self.assertRaises(sqlite3.OperationalError):
                self._record()
        # 重试（这次不注入失败）
        self._record()
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM code_index").fetchone()[0], 1,
            "重试后出现重复投影")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM events WHERE type='memory.recorded'")
            .fetchone()[0], 1, "重试后出现重复事件")


class FtsCommitParamTest(unittest.TestCase):
    """`_index_one_fts` 必须能被调用方控制事务边界。"""

    def test_commit_false_does_not_commit(self):
        import fish_common
        conn = sqlite3.connect(":memory:")
        conn.executescript(fish.SCHEMA)
        # 先塞一条，然后不提交地删掉——若函数内部偷偷 commit，回滚就失效
        conn.execute("INSERT INTO memory_fts (code,title_idx,body_idx) VALUES ('A','a','a')")
        conn.commit()
        fish_common._index_one_fts(conn, "B", "b", [], "b", commit=False)
        conn.rollback()
        rows = [r[0] for r in conn.execute("SELECT code FROM memory_fts ORDER BY code")]
        self.assertEqual(rows, ["A"],
                         f"commit=False 仍提交了（回滚没生效）：{rows}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
