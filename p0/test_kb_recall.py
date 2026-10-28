#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-④ 判据（外部审查 🔴1）：KB 自然语言召回的两段式。

R-1 长句（全 AND 必空）→ 回退 OR 后**有命中**，且 `meta.fallback` 如实标记
R-2 精确单词 → AND 直接命中，**不回退**（`meta.fallback` 不存在）
R-3 无 active 行 → 空 hits，且不标记 fallback（"没有内容"不是"降级"）
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fish                 # noqa: E402
import fish_kb_index        # noqa: E402


class KbRecallTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-kbrecall-"))
        self.conn = fish.connect(self.tmp / "data")
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        fish_kb_index.ensure_kb_index(self.conn)
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        vid = "vtest"
        self.conn.execute(
            "INSERT INTO kb_vault (vault_id, name, path, path_key, mode, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?)",
            (vid, "测试库", str(self.vault), "k1", "ro",
             "2026-10-28T00:00:00Z", "2026-10-28T00:00:00Z"))
        rows = [("a.md", "漫剧剧本的写作要点", "分镜、对白、节奏是基本功。"),
                ("b.md", "海报排版的三条规则", "海报排版讲究留白与层级。")]
        for rel, title, body in rows:
            (self.vault / rel).write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
            self.conn.execute(
                "INSERT INTO kb_index (vault_id, rel_path, mtime, size, content_hash, "
                "title, headings, summary, excerpt, word_count, indexed_at, status, error) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (vid, rel, 0.0, len(body), "h" + rel, title,
                 f"# {title}", body[:120], body, 10,
                 "2026-10-28T00:00:00Z", "active", None))
            rowid = self.conn.execute(
                "SELECT rowid FROM kb_index WHERE vault_id=? AND rel_path=?",
                (vid, rel)).fetchone()[0]
            self.conn.execute(
                "INSERT INTO kb_fts (rowid, rel_path, vault_id, title, headings, summary, "
                "body_excerpt) VALUES (?,?,?,?,?,?,?)",
                (rowid, rel, vid, fish.fts_text(title), fish.fts_text(title),
                 fish.fts_text(body[:120]), fish.fts_text(body)))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # R-1
    def test_R1_long_sentence_falls_back_to_or(self):
        res = fish_kb_index.search_kb(self.conn, query="漫剧剧本怎么写", limit=5)
        self.assertTrue(res["hits"], f"长句必须有命中（回退 OR）：{res['meta']}")
        self.assertEqual(res["meta"].get("fallback"), "or_retry",
                         "回退必须如实标记（不许静默降级）")

    # R-2
    def test_R2_precise_word_does_not_fall_back(self):
        res = fish_kb_index.search_kb(self.conn, query="漫剧", limit=5)
        self.assertTrue(res["hits"])
        self.assertNotIn("fallback", res["meta"], "精确查询不该回退")

    # R-3
    def test_R3_no_active_rows_means_no_hits(self):
        self.conn.execute("UPDATE kb_index SET status='deleted'")
        self.conn.commit()
        res = fish_kb_index.search_kb(self.conn, query="漫剧剧本怎么写", limit=5)
        self.assertEqual(res["hits"], [])
        self.assertNotIn("fallback", res["meta"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
