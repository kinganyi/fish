#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可见性判据（用户 2026-10-29 拍板：`private` 是**可选**项，先不强制）。

V-1 开关**关**时：`private` 条目照常检索到（逐字节等价旧行为 = 默认状态）
V-2 开关**开** + viewer 本人：看得到自己的 private
V-3 开关**开** + 别人（或没有 viewer）：private 被剔除
V-4 取值校验：`visibility` 只接受 shared / private，拼错要响（不能静默降级成 shared）
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fish          # noqa: E402


class VisibilityTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-vis-"))
        self.root = self.tmp
        self.conn = fish.connect(self.tmp)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        # 编码分配要求前缀已在 `code_registry` 注册（`record_memory` 会校验）
        self.conn.execute(
            "INSERT INTO code_registry (prefix, namespace, last_date, last_seq, status) "
            "VALUES (?,?,?,?,?)", ("VIS", "可见性测试", "2026-10-29", 0, "active"))
        self.conn.commit()
        self.private_code = self._remember("私密笔记", "private")
        self.shared_code = self._remember("公开笔记", "shared")

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _remember(self, title, visibility, owner="cit-owner"):
        res = fish.record_memory(
            self.conn, self.root, project="VIS", title=title, body="正文",
            visibility=visibility, owner=owner)
        return res[0]

    def _set(self, key, value):
        self.conn.execute(
            "INSERT OR REPLACE INTO config (key,value) VALUES (?,?)",
            (key, str(value)))
        self.conn.commit()

    def _codes(self, viewer=None):
        # 用关键词检索（`search_memories` 的 query=None 是"未检索"语义，
        # 列模式走 `list_memories`；这里要测的是检索路径上的可见性过滤）
        rows, _ = fish.search_memories(
            self.conn, query="笔记", project="VIS", limit=20,
            viewer_citizen_id=viewer)
        return [r[0] for r in rows]

    # V-1
    def test_V1_off_by_default_private_visible(self):
        self.assertFalse(fish._visibility_enforced(self.conn))
        self.assertIn(self.private_code, self._codes(viewer="cit-other"),
                      "开关关着时 private 只是标签，不该影响检索（零回归）")

    # V-2
    def test_V2_enforced_owner_sees_own_private(self):
        self._set("privacy.visibility_enforced", "true")
        got = self._codes(viewer="cit-owner")
        self.assertIn(self.private_code, got, "owner 必须看得到自己的 private")
        self.assertIn(self.shared_code, got)

    # V-3
    def test_V3_enforced_other_cannot_see_private(self):
        self._set("privacy.visibility_enforced", "true")
        got = self._codes(viewer="cit-other")
        self.assertNotIn(self.private_code, got, "别人的 private 必须被剔除")
        self.assertIn(self.shared_code, got, "shared 不受影响")
        # 没有 viewer 时宁严勿松：private 一律剔除
        self.assertNotIn(self.private_code, self._codes(viewer=None))

    # V-4
    def test_V4_invalid_visibility_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self._remember("拼错的可见性", "机密")
        self.assertIn("visibility", str(ctx.exception))
        # 大小写/空白要归一，不能因为写了 "PRIVATE " 就静默存成原样
        code = self._remember("大写 private", " PRIVATE ")
        row = self.conn.execute(
            "SELECT visibility FROM code_index WHERE code=?", (code,)).fetchone()
        self.assertEqual(row[0], "private")

    # V-5（评审 🔴1 的守卫）：**CLI 自己声明过的 `public`** 也必须被拒，且不能留空号/孤儿
    def test_V5_rejection_happens_before_code_allocation(self):
        """拒绝必须发生在 `next_code` 之前。

        评审官实测：旧版校验在分配编码与正文落盘**之后** →
        `--visibility public` 崩溃的同时烧掉一个编码号（last_seq 1→2）
        并在磁盘上留一个孤儿正文文件（编码不可回收，这条不变量本函数为
        `occurred_at` / `project_writable` 都写过）。
        """
        def last_seq():
            return self.conn.execute(
                "SELECT last_seq FROM code_registry WHERE prefix='VIS'").fetchone()[0]

        before = last_seq()
        with self.assertRaises(ValueError):
            self._remember("cli 声明过的 public", "public")
        after = last_seq()
        self.assertEqual(before, after, "拒绝必须发生在 next_code 之前（不能烧号）")
        orphans = list((self.root / "projects" / "VIS").glob("*cli*"))
        self.assertEqual(orphans, [], f"拒绝不能留孤儿正文文件：{orphans}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
