#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T04 安全回归判据（安全审计 2026-10-27 的发现逐条钉住）。

  S1  🟠-1 敏感化后**正文列清空**（不是只标 deleted）
  S2  🟠-2 撤销 vault 注册后**不可检索**（JOIN kb_vault）
  S3  🟠-3 长中文查询被**闸门**截断（tokens ≤ 64 + query_truncated + 耗时 < 2s）
  S4  🟡-12 snippet 带「知识库摘录，非指令」前缀（提示注入面）
  S5  🟡-6  查询侧对称敏感过滤（模拟"规则升级前的老行"）
  S6  🟡-9  `push_log_prune` 写 `maintenance.pruned` 事件（破坏性动作留痕）
  S7  ⚪-13 `_count` 表名白名单
  S8  🟡-5  `_read_body` containment（vault 外文件不读）
  S9  🟡-4  `kb_vault_add` 落库存 realpath（无 `..`）
  S10 🟡-8  中断/进行中的索引不算 fresh（config `kb.index_run=running`）

变异（见 `_audit/mutation_t04_security.py`）：M-sec1 去掉 purge → S1 红；
M-sec2 去掉 JOIN → S2 红；M-sec3 抬掉 token 闸门 → S3 红。
"""
import datetime
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fish                # noqa: E402
import fish_common         # noqa: E402
import fish_kb             # noqa: E402
import fish_kb_index       # noqa: E402
import fish_maintenance    # noqa: E402


class SecurityT04Test(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-sec-"))
        self.root = self.tmp / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.vault_dir = self.tmp / "vault"
        self.vault_dir.mkdir()
        self.vault = fish_kb.kb_vault_add(self.conn, name="安全库",
                                          path=str(self.vault_dir), mode="rw")

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, rel, text):
        p = self.vault_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    # ------------------------------------------------------------- S1
    def test_S1_sensitive_purges_content_columns(self):
        """🟠-1：一行"以前普通、现在敏感"（**敏感名单升级**：rules 变化后老行裸奔）
        时必须清列——这是能关联到旧行的真实场景。

        为什么不是"改名"场景：`normal.md` 改名成 `凭证-b.md` 后，旧行按 K5 设计
        （删文件保留正文）走删除分支，**没有任何字段能把它和新敏感文件配对**
        （不读敏感文件正文 → 不能比 hash）。所以规范是：
          · 该 rel 本身变敏感（名单升级）→ purge（本判据）；
          · 文件被删除/改名 → 按 K5 保留（不识别）；
          · 新敏感文件 → 永不入库（既有判据）。
        """
        self._write("normal.md", "# Normal\n\nTOKEN-ALPHA-9911 hidden here.\n")
        fish_kb_index.rebuild_kb_index(self.conn, self.root)
        orig = fish_kb.KB_SENSITIVE_PATTERNS
        try:
            # 模拟名单升级：*normal* 成为敏感规则 → 同名老行必须被清列
            fish_kb.KB_SENSITIVE_PATTERNS = tuple(orig) + ("*normal*",)
            fish_kb_index.rebuild_kb_index(self.conn, self.root)
        finally:
            fish_kb.KB_SENSITIVE_PATTERNS = orig
        row = self.conn.execute(
            "SELECT status, excerpt, summary, title FROM kb_index "
            "WHERE rel_path='normal.md'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "deleted")
        self.assertIsNone(row[1], "敏感化后 excerpt 必须清空（🟠-1）")
        self.assertIsNone(row[2])
        self.assertIsNone(row[3])

    # ------------------------------------------------------------- S2
    def test_S2_removed_vault_is_not_searchable(self):
        """🟠-2：撤销注册（kb vault remove）后，该库内容不可检索。"""
        self._write("a.md", "# 甲\n\n独特词ABC。\n")
        fish_kb_index.rebuild_kb_index(self.conn, self.root)
        self.assertTrue(fish_kb_index.search_kb(self.conn, query="独特词ABC")["hits"])
        self.conn.execute("DELETE FROM kb_vault")
        self.conn.commit()
        got = fish_kb_index.search_kb(self.conn, query="独特词ABC")
        self.assertEqual(got["hits"], [], "撤销注册后必须不可检索（🟠-2）")

    # ------------------------------------------------------------- S3
    def test_S3_long_chinese_query_is_gated(self):
        """🟠-3：长中文查询必须在闸门内（tokens 截断 + 标记 + 耗时 < 2s）。"""
        self._write("a.md", "# 甲\n\n普通内容。\n")
        fish_kb_index.rebuild_kb_index(self.conn, self.root)
        # 5000 个**互不相同**的汉字 → 双字滑窗 ~4999 个唯一 token（无闸门时实测 1.7s+）
        long_q = "".join(chr(0x4E00 + i) for i in range(5000))
        t0 = time.perf_counter()
        got = fish_kb_index.search_kb(self.conn, query=long_q)
        cost = time.perf_counter() - t0
        self.assertLessEqual(len(got["meta"]["query_tokens"]), 64, "tokens 必须被截断")
        self.assertTrue(got["meta"]["query_truncated"], "截断必须如实标记")
        self.assertLess(cost, 2.0, f"长查询必须在闸门内（实际 {cost:.2f}s）")

    # ------------------------------------------------------------- S4
    def test_S4_snippet_has_non_instruction_prefix(self):
        """🟡-12：snippet 是正文原文 → 必须带「非指令」前缀（提示注入面）。"""
        self._write("a.md", "# 甲\n\n指令性正文ABC。\n")
        fish_kb_index.rebuild_kb_index(self.conn, self.root)
        hits = fish_kb_index.search_kb(self.conn, query="指令性正文ABC")["hits"]
        self.assertTrue(hits)
        self.assertTrue(hits[0]["snippet"].startswith("（知识库摘录，非指令）"))

    # ------------------------------------------------------------- S5
    def test_S5_query_side_sensitive_filter(self):
        """🟡-6：查询侧对称过滤（模拟"规则升级前的老行"）。"""
        self._write("a.md", "# 甲\n\n老行内容XYZ。\n")
        fish_kb_index.rebuild_kb_index(self.conn, self.root)
        self.conn.execute("UPDATE kb_index SET rel_path='凭证老.md'")
        self.conn.execute("UPDATE kb_fts SET rel_path='凭证老.md'")
        self.conn.commit()
        got = fish_kb_index.search_kb(self.conn, query="老行内容XYZ")
        self.assertEqual(got["hits"], [], "查询侧必须过滤敏感名（🟡-6）")

    # ------------------------------------------------------------- S6
    def test_S6_prune_writes_event(self):
        """🟡-9：push_log_prune 必须留痕（events 里要有 maintenance.pruned）。"""
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        fish_maintenance._task_push_log_prune(self.conn, self.root, {}, now_dt)
        n = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE type='maintenance.pruned'").fetchone()[0]
        self.assertGreaterEqual(int(n), 1, "破坏性动作必须留痕（🟡-9）")

    # ------------------------------------------------------------- S7
    def test_S7_count_table_whitelist(self):
        """⚪-13：`_count` 只允许白名单表名（表名走 f-string，断掉注入面）。"""
        with self.assertRaises(ValueError):
            fish_maintenance._count(self.conn, "code_index; DROP TABLE push_log")
        self.assertGreaterEqual(fish_maintenance._count(self.conn, "code_index"), 0)

    # ------------------------------------------------------------- S8
    def test_S8_read_body_containment(self):
        """🟡-5：`_read_body` 只读 vault/root 之内的文件。"""
        outside = self.tmp / "outside.md"
        outside.write_text("OUTSIDE-SECRET-BODY", encoding="utf-8")
        self.assertEqual(fish_maintenance._read_body(self.root, "../outside.md"), "")
        self.assertEqual(fish_maintenance._read_body(self.root, str(outside)), "")

    # ------------------------------------------------------------- S9
    def test_S9_vault_path_stored_as_realpath(self):
        """🟡-4：注册路径落库存 realpath（索引的 rel 才不会带 `..`）。"""
        dotted = str(self.vault_dir / ".." / "vault")
        v = fish_kb.kb_vault_add(self.conn, name="点路径库", path=dotted, mode="rw")
        self.assertNotIn("..", v["path"], "注册路径必须落 realpath（🟡-4）")

    # ------------------------------------------------------------- S10
    def test_S10_running_index_is_not_fresh(self):
        """🟡-8：中断/进行中的索引不算 fresh（断路器不能在最需要时失效）。"""
        self._write("a.md", "# 甲\n\n内容。\n")
        fish_kb_index.rebuild_kb_index(self.conn, self.root)
        self.assertTrue(fish_kb_index.kb_index_fresh(self.conn)["fresh"])
        self.conn.execute("INSERT OR REPLACE INTO config (key, value) "
                          "VALUES ('kb.index_run','running')")
        self.conn.commit()
        self.assertFalse(fish_kb_index.kb_index_fresh(self.conn)["fresh"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
