#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F-P2-5：库没有 schema 版本号。

外部审查（2026-11-18）：`PRAGMA user_version=0`；旧库/新环境无法判断版本；
列级迁移无统一备份。本项目 2026-11-19 复核：生产库仍是 **0**。

## 为什么这条重要

`migrate()` 是"缺列就补"的幂等补列，所以它过去不需要版本号。
但幂等只对"补列"成立——一旦以后出现需要判断顺序的迁移（改类型/拆表/回填），
"缺什么补什么"就答不了"这一步做过没有"。

版本号存在的意义不是好看，是**让"这一步做过没有"变成可判定**。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fish  # noqa: E402
import fish_meta  # noqa: E402


class SchemaVersionTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="fish-p25-")
        self.root = Path(self._tmp) / "data"
        self.root.mkdir(parents=True)
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:      # noqa: BLE001
            pass

    def test_migrate_stamps_the_version(self):
        """**核心**：`migrate()` 补完列之后必须盖章。"""
        self.assertEqual(fish_meta.read_version(self.conn), 0,
                         "新建库的 user_version 应当是 0（SQLite 默认）")
        fish.migrate(self.conn)
        self.assertEqual(fish_meta.read_version(self.conn),
                         fish_meta.SCHEMA_VERSION,
                         "migrate 之后版本号没写进去——旧库永远判不出结构")

    def test_stamp_is_idempotent(self):
        """重复 migrate 不许把版本号改来改去。"""
        fish.migrate(self.conn)
        v1 = fish_meta.read_version(self.conn)
        fish.migrate(self.conn)
        self.assertEqual(fish_meta.read_version(self.conn), v1)

    def test_describe_distinguishes_unversioned_from_same(self):
        """**三态分明**：0（老库）与"版本相同"不是一回事。"""
        d0 = fish_meta.describe(self.conn)
        self.assertEqual(d0["state"], "unversioned",
                         f"0 应当是 unversioned 而不是同版本：{d0}")
        fish.migrate(self.conn)
        d1 = fish_meta.describe(self.conn)
        self.assertEqual(d1["state"], "same", f"盖章后应当是 same：{d1}")

    def test_code_version_is_positive(self):
        """版本号必须是个正数——0 是 SQLite 的默认值，不能拿它当"已版本化"。"""
        self.assertGreater(fish_meta.SCHEMA_VERSION, 0)


    def test_stamp_never_downgrades(self):
        """**第七轮评审 🟡**：`stamp()` 会把比代码更新的库**静默降级**。

        实测：造一个 `user_version=5` 的库，`describe()` 说 `newer`，
        而 `stamp()` 照样写 1 —— 5 → 1。

        版本号存在的意义是"让这步做过没有可判定"；能往下盖的版本号做不到这件事。
        库比代码新通常意味着**代码比库旧**（回滚过）——那种情况下**库是对的**。
        """
        self.conn.execute("PRAGMA user_version = 5")
        self.conn.commit()
        fish_meta.stamp(self.conn)
        self.assertEqual(fish_meta.read_version(self.conn), 5,
                         "比代码新的库被降级了——版本号不许往下盖")

    def test_stamp_writes_on_a_readonly_connection_raises(self):
        """那个 `# A档` 吞异常的注释曾经描述了一个**不会发生**的场景。

        第七轮评审实测：只读连接上 `PRAGMA user_version=N` **在 execute 就抛**，
        根本走不到 commit。所以 `stamp()` 现在**不吞**——让调用方决定
        （`migrate()` 会吞，因为列已经对齐、只是版本记不下来）。
        """
        import sqlite3 as _s
        ro = _s.connect(f"file:{self.root / 'index.sqlite'}?mode=ro", uri=True)
        try:
            with self.assertRaises(_s.OperationalError):
                fish_meta.stamp(ro, version=999)
        finally:
            ro.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
