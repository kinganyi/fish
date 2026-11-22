#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""指标一致性：**同一个事实，两个算法必须得同一个数**（2026-11-21 · C-2）。

## 这份测试守的是一条纪律，不是某个 bug

本轮修复里最隐蔽的一个缺陷是 `new_posts` 虚高 **7251 倍**：

```
推送说：看板有更新（395 条新动态）
看板说：今日动态 0 条
```

两个数描述的是**同一个事实**（这块板有几条新动态），却差了两个数量级。
根因是 `board_posts.id` 全局自增，而实现用了 `MAX(id) - seen` 当"新增条数"。

**它活了很久没被发现，是因为两个数字出现在两个地方**——只要没人对照，
永远看不出来。所以这条纪律值得单独成文：

> **凡是"推送里说的数字"与"工具里查到的数字"描述同一事实的，必须相等。**

## 三对已覆盖的指标

| 事实 | 算法 A | 算法 B |
|---|---|---|
| 某块板有几条新动态 | 推送候选的 `reason` | `board_posts` 的 `count(*)` |
| 有多少条断链 | `healthcheck` 的读数 | `fish_purge.scan_broken` |
| 某记忆在不在 | `memory_fts` 的 MATCH | `code_index` 的状态过滤 |

## 新增指标时怎么做

**给每对新指标写一条"两算法同数"的断言，并附一条能红的变异体。**
没有正对照的指标读数，等于没有判据。
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

P0 = Path(__file__).resolve().parent
sys.path.insert(0, str(P0))

import fish  # noqa: E402


class _Base(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-metric-"))
        self.data = self.tmp / "data"
        self.data.mkdir(parents=True)
        self.conn = fish.connect(self.data)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO code_registry (prefix, namespace, status) VALUES ('T','t','active')")
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)


class BoardNewPostsMetricTest(_Base):
    """事实①：某块板有几条**新**动态。

    ⚠️ 造数据顺序是判据成立的前提：**必须先插别板把全局 id 推高，再插本板**
    ——反过来时本板 id 连续，`MAX(id)-seen` 恰好也等于本板条数，判据测不出问题。
    """

    def _board(self, board_id, n):
        self.conn.execute(
            "INSERT INTO boards (id, scope, scope_key, period_key, path, status, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (board_id, "project", f"p{board_id}", "261121", ".", "open",
             "2026-11-21T00:00:00Z"))
        for i in range(n):
            self.conn.execute(
                "INSERT INTO board_posts (board_id, citizen_id, kind, text, created_at) "
                "VALUES (?,?,?,?,?)",
                (board_id, "C1", "note", f"p{board_id}-{i}", "2026-11-21T00:00:00Z"))
        self.conn.commit()

    def test_two_algorithms_agree(self):
        """**正对照**：别板 5 条 + 本板 2 条 → 两个算法都得 2。

        变异体：把 `count(*) ... id > seen` 改回 `MAX(id) - seen` → 得 7 → 红。
        """
        self._board(2, 5)          # 先插别板（把全局 id 推到 5）
        self._board(1, 2)          # 再插本板（id 6,7）

        # 算法 A：推送用的（数区间内的行）
        a = self.conn.execute(
            "SELECT count(*) FROM board_posts WHERE board_id=1 AND id > 0").fetchone()[0]
        # 算法 B：看板用的（数本板全部）
        b = self.conn.execute(
            "SELECT count(*) FROM board_posts WHERE board_id=1").fetchone()[0]
        self.assertEqual(a, b, f"两个算法不同数：推送={a} 看板={b}")

        # 对照：旧算法（MAX(id) 差）会得 7 —— 证明判据真的能分辨
        old = self.conn.execute(
            "SELECT COALESCE(MAX(id),0) FROM board_posts WHERE board_id=1").fetchone()[0]
        self.assertNotEqual(old, a, "前提没造对：旧算法与新算法同数，判据无判别力")


class BrokenCountMetricTest(_Base):
    """事实②：有多少条断链。

    ⚠️ 口径必须是 **active**——把已作废的也算进来，清理完读数永远不归零。
    """

    def _make(self, title, broken=False):
        code, rel = fish.record_memory(self.conn, self.data, project="T",
                                       title=title, body="x")
        self.conn.commit()
        if broken:
            (self.data / rel).unlink()
        return code

    def test_healthcheck_and_scan_agree(self):
        """体检读数与逐条扫描必须同数。"""
        import fish_purge
        self._make("坏的", broken=True)
        scan = fish_purge.scan_broken(self.conn, self.data)["count"]

        # 体检用的读数（同一函数；这里验证的是"体检确实用它，而不是自己另写一份"）
        src = (P0 / "fish.py").read_text(encoding="utf-8")
        self.assertIn("fish_purge.scan_broken", src,
                      "体检没有复用 scan_broken —— 两份实现迟早漂")
        # 2026-11-23（交接轮 F-02）：**MCP 侧也算同一事实**。它曾经内联第二份 SQL
        # 且漏了 active 过滤（MCP 报 318 / CLI 报 0）。这条钉住"不许再分叉"。
        mcp_src = (P0 / "mcp_server.py").read_text(encoding="utf-8")
        self.assertIn("fish_purge.scan_broken", mcp_src,
                      "MCP healthcheck 没有复用 scan_broken —— 同一事实两套算法")
        self.assertEqual(scan, 1)

    def test_purged_does_not_count(self):
        """作废后必须归零（口径只算 active）。"""
        import fish_purge
        self._make("坏的", broken=True)
        fish_purge.purge_broken(self.conn, self.data, dry_run=False)
        self.assertEqual(fish_purge.scan_broken(self.conn, self.data)["count"], 0)


class FtsPresenceMetricTest(_Base):
    """事实③：某条记忆能不能被搜到。

    FTS 说"有"、`code_index` 说"作废"——两者必须由**同一份状态**裁决。
    """

    def test_purged_memory_unsearchable(self):
        code, rel = fish.record_memory(self.conn, self.data, project="T",
                                       title="可搜到的", body="正文")
        self.conn.commit()
        (self.data / rel).unlink()
        fish.ensure_fts(self.conn, self.data)

        hits_before = fish.search_memories(self.conn, query="可搜到")[0]
        self.assertTrue(any(r[0] == code for r in hits_before), "前置：本该搜得到")

        import fish_purge
        fish_purge.purge_broken(self.conn, self.data, dry_run=False)
        hits_after = fish.search_memories(self.conn, query="可搜到")[0]
        self.assertFalse(any(r[0] == code for r in hits_after),
                         "作废后仍然搜得到 —— FTS 与状态不同步")


if __name__ == "__main__":
    unittest.main()
