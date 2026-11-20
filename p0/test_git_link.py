#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆↔提交的关联层（fish_git_link）+ 归属判定入口（2026-11-20）。

## 这份测试补的是什么

`git_link` 的四个函数此前**一条判据都没有**——上一轮我在临时脚本里手工验证过
行为，但那不留下任何会自动变红的东西。手工验证过 ≠ 有判据；下次改动没人拦得住。

`set_ownership` 同理：`ownership` 字段建了很久，但没有任何代码会写它。
现在补上入口，也补上判据。

## 变异体（去掉 → 必红）

· `link_remove` 改成物理删除（`DELETE FROM`）→ 软删除判据红（行没了就没法追溯）
· `link_restore` 不实现 → 回滚判据红
· `set_ownership` 去掉值校验 → 非法值判据红
· `set_ownership` 不写 `ownership_evidence` → 证据判据红
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

P0 = Path(__file__).resolve().parent
sys.path.insert(0, str(P0))

import fish  # noqa: E402
import fish_git_link  # noqa: E402
import fish_git_store  # noqa: E402


class _DbCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-gitlink-"))
        self.conn = fish.connect(self.tmp / "db")
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)

    def tearDown(self):
        try:
            self.conn.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)


class LinkSoftDeleteTest(_DbCase):
    """J10：关联的软删除可回滚。"""

    def test_create_then_query(self):
        lid = fish_git_link.link_create(
            self.conn, src_kind="memory", src_id="ANYI-261120-01",
            dst_kind="commit", dst_id="abc123", rel="implemented_by",
            evidence="手工关联")
        self.conn.commit()
        rows = fish_git_link.links_of(self.conn, src_id="ANYI-261120-01")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], lid)
        self.assertEqual(rows[0]["rel"], "implemented_by")

    def test_remove_is_soft_not_physical(self):
        """**J10 的核心**：软删之后行必须还在（否则「撤销」无从追溯）。

        变异体：把 `UPDATE ... SET removed_at` 改成 `DELETE FROM` → 本判据红。
        """
        lid = fish_git_link.link_create(
            self.conn, src_kind="memory", src_id="m1",
            dst_kind="commit", dst_id="c1", rel="implemented_by")
        self.conn.commit()
        self.assertTrue(fish_git_link.link_remove(self.conn, lid), "软删没命中")
        self.conn.commit()

        self.assertEqual(fish_git_link.links_of(self.conn, src_id="m1"), [],
                         "默认查询不该返回已删的")
        kept = fish_git_link.links_of(self.conn, src_id="m1", include_removed=True)
        self.assertEqual(len(kept), 1, "行被物理删掉了——撤销已无从追溯")
        self.assertIsNotNone(kept[0]["removed_at"], "removed_at 没记时间戳")

    def test_restore_rolls_back(self):
        lid = fish_git_link.link_create(
            self.conn, src_kind="memory", src_id="m2",
            dst_kind="commit", dst_id="c2", rel="produced")
        self.conn.commit()
        fish_git_link.link_remove(self.conn, lid)
        self.conn.commit()
        self.assertTrue(fish_git_link.link_restore(self.conn, lid), "回滚没命中")
        self.conn.commit()
        rows = fish_git_link.links_of(self.conn, src_id="m2")
        self.assertEqual(len(rows), 1, "回滚后应当重新可见")
        self.assertIsNone(rows[0]["removed_at"])

    def test_duplicate_create_returns_same_id(self):
        """重复建立返回已有 id —— 调用方不需要「先查再建」（并发下会变两行）。"""
        a = fish_git_link.link_create(self.conn, src_kind="memory", src_id="m3",
                                      dst_kind="commit", dst_id="c3", rel="revised_in")
        b = fish_git_link.link_create(self.conn, src_kind="memory", src_id="m3",
                                      dst_kind="commit", dst_id="c3", rel="revised_in")
        self.conn.commit()
        self.assertEqual(a, b)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM git_link").fetchone()[0], 1)

    def test_create_after_remove_resurrects(self):
        """被软删后再建立一次 = 恢复（语义是「它又成立了」）。"""
        lid = fish_git_link.link_create(self.conn, src_kind="memory", src_id="m4",
                                        dst_kind="commit", dst_id="c4",
                                        rel="discussed_in")
        self.conn.commit()
        fish_git_link.link_remove(self.conn, lid)
        self.conn.commit()
        again = fish_git_link.link_create(self.conn, src_kind="memory", src_id="m4",
                                          dst_kind="commit", dst_id="c4",
                                          rel="discussed_in")
        self.conn.commit()
        self.assertEqual(again, lid)
        self.assertEqual(len(fish_git_link.links_of(self.conn, src_id="m4")), 1,
                         "重新建立后应当可见")

    def test_query_by_dst(self):
        """反方向也要能查（「这个提交实现了哪些记忆」）。"""
        fish_git_link.link_create(self.conn, src_kind="memory", src_id="m5",
                                  dst_kind="commit", dst_id="c5", rel="implemented_by")
        fish_git_link.link_create(self.conn, src_kind="memory", src_id="m6",
                                  dst_kind="commit", dst_id="c5", rel="produced")
        self.conn.commit()
        rows = fish_git_link.links_of(self.conn, dst_kind="commit", dst_id="c5")
        self.assertEqual(len(rows), 2)


class OwnershipTest(_DbCase):
    """`ownership` 的显式赋值入口。"""

    def _mk_repo(self):
        rid = fish_git_store.ensure_repo(
            self.conn, identity="github.com/o/r", identity_kind="remote",
            root=str(self.tmp / "r"))
        self.conn.commit()
        return rid

    def test_default_is_unknown(self):
        """**保守默认**：新登记的仓库必须是 unknown，不能是 owned。

        变异体：把 DDL 默认改成 'owned' → 本判据红。这条守着方案 P10
        （发现器一跑，第三方仓库全被标成「自己的」→ 只读红线形同虚设）。
        """
        rid = self._mk_repo()
        row = self.conn.execute(
            "SELECT ownership, ownership_evidence FROM git_repo WHERE repo_id=?",
            (rid,)).fetchone()
        self.assertEqual(row[0], "unknown")
        self.assertIsNone(row[1])

    def test_set_ownership_with_evidence(self):
        rid = self._mk_repo()
        self.assertTrue(fish_git_store.set_ownership(
            self.conn, rid, "owned", evidence="远端 owner=kinganyi 是主人 GitHub 账号"))
        self.conn.commit()
        row = self.conn.execute(
            "SELECT ownership, ownership_evidence FROM git_repo WHERE repo_id=?",
            (rid,)).fetchone()
        self.assertEqual(row[0], "owned")
        self.assertIn("kinganyi", row[1], "ownership_evidence 没写进去")

    def test_rejects_invalid_value(self):
        """非法值必须**响亮失败**，不能静默存进去。

        变异体：去掉值校验 → 本判据红。（CHECK 约束也会拦，但那会抛 sqlite3 错误，
        对调用方不友好；这里要求 ValueError 带可用的取值表。）
        """
        rid = self._mk_repo()
        with self.assertRaises(ValueError):
            fish_git_store.set_ownership(self.conn, rid, "mine")

    def test_missing_repo_returns_false(self):
        self.assertFalse(fish_git_store.set_ownership(self.conn, 999999, "owned"))


if __name__ == "__main__":
    unittest.main()
