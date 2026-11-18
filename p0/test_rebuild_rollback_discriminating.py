#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""判据（提议）：**重建中途崩溃后，投影必须还在**——与坏事件排第几无关。

## 为什么现有的 `test_unchecked_field_type_rolls_back` 不具判别力

它把坏事件 `INSERT` 在两条正常事件**之后**（setUp 先写完两条记忆），
而 `rebuild_projection` 的重放按 `seq` 升序。于是崩溃发生在**最后一条**：
DELETE 早已执行，前两行也早已重放，`COUNT(*)` 恰好还是 2 ——
`conn.rollback()` 换成 `pass`，断言的读数一模一样。

## 这条怎么才有判别力

不依赖"某条 payload 恰好排在前面"，而是**站在 DELETE 与第一次 INSERT 之间**
制造崩溃：DELETE 已执行、一行都还没重放。此刻投影必为 0 行。
于是"有没有回滚"直接决定异常之后 `COUNT(*)` 是 0 还是 N。

   · 有回滚 → 异常之后（调用方还 commit 了）投影仍是 N 行 → 绿
   · 没回滚 → 投影永久 0 行 → 红

**它判的是结构性质，不是"我又想到了哪个字段"**：以后预检漏掉什么字段、
重放里哪一行抛什么异常都不影响这条判据 —— 只要抛异常的地方在 DELETE 之后。
"""
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish          # noqa: E402
import fish_rebuild  # noqa: E402


class _CrashAfterDelete:
    """连接代理：DELETE 之后、第一次写投影之前抛异常。

    只拦 `execute`，其余属性（commit/rollback/close…）原样转发——
    这样"被测实现有没有调 rollback"完全由它自己决定，判据不替它做。
    """

    def __init__(self, conn):
        self._conn = conn
        self._deleted = False
        self._crashed = False

    def execute(self, sql, *args):
        text = str(sql)
        if "DELETE FROM code_index" in text:
            self._deleted = True
        elif self._deleted and not self._crashed and "INSERT" in text.upper():
            self._crashed = True
            raise RuntimeError("注入的崩溃：DELETE 之后第一行写入前")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class RebuildRollbackDiscriminatingTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()
        self.conn = fish.connect(self.data)
        self.conn.executescript(fish.SCHEMA)
        self.conn.execute(
            "INSERT INTO code_registry (prefix, namespace, last_date, last_seq)"
            " VALUES ('P','P','2026-11-18',0)")
        self.conn.commit()
        for t in ("甲", "乙"):
            fish.record_memory(self.conn, self.data, project="P", title=t,
                               body=f"# {t}\n", mem_type="fact")
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_crash_right_after_delete_leaves_projection_intact(self):
        before = self.conn.execute("SELECT COUNT(*) FROM code_index").fetchone()[0]
        self.assertEqual(before, 2, "基线不对")
        with self.assertRaises(RuntimeError):
            fish_rebuild.rebuild_projection(_CrashAfterDelete(self.conn), self.data)
        # 调用方在异常之后照样 commit（真实调用方就是这么干的）
        self.conn.commit()
        # 换一条独立连接读：证明它是**落盘的**，不是同一连接里的未提交状态
        other = fish.connect(self.data)
        try:
            durable = other.execute("SELECT COUNT(*) FROM code_index").fetchone()[0]
        finally:
            other.close()
        self.assertEqual(
            durable, before,
            f"崩溃发生在 DELETE 之后（投影一度为 0 行），最终却只剩 {durable} 行——"
            "DELETE 与重放没有包进一个会回滚的事务。")


if __name__ == "__main__":
    unittest.main(verbosity=2)
