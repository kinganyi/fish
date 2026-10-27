#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-1 判据：知识库命中进入推送候选（`kb_related` / kind="kb"）。

## 为什么要有它

对外承诺的「做设计时自动从知识库推相关文档」此前只做到"能查能搜"——
`build_push_candidates` 的六类候选里没有知识库。本组判据把这段接上：
命中就产候选、没库/空查询不噪音、24h 不重复推、提示词时机也走。
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fish  # noqa: E402

CID = "01PUSHKB0000000000000A1"


class KbPushBase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-push-kb-"))
        self.root = self.tmp / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,status,created_at,host) "
            "VALUES (?,?,'visitor','active',?,'dsh')", (CID, "AI-甲", fish.now_utc()))
        self.conn.commit()
        self.vault_dir = self.tmp / "vault"
        self.vault_dir.mkdir()

    def tearDown(self):
        try:
            self.conn.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def _add_doc(self, name, body):
        path = self.vault_dir / name
        path.write_text(body, encoding="utf-8")
        return path

    def _register_vault(self):
        return fish.kb_vault_add(self.conn, name="测试知识库",
                                 path=str(self.vault_dir), mode="ro")


class KbCandidateTest(KbPushBase):
    """S-KB1：有注册 vault + 命中 → 产 kb 候选；字段可读、可去重。"""

    def test_SKB1_vault_hit_produces_candidate(self):
        self._register_vault()
        self._add_doc("设计-01.md", "# 海报排版\n\n留白与层级是核心，视觉重心要稳。\n")
        cands = fish._kb_push_candidates(
            self.conn, self.root, query="海报排版", project="PJ", limit=2)
        self.assertEqual(len(cands), 1, cands)
        c = cands[0]
        self.assertEqual(c["kind"], "kb")
        self.assertTrue(c["code"].startswith("kb:测试知识库:"), c["code"])
        self.assertIn("设计-01.md", c["title"])
        self.assertIn("测试知识库", c["reason"])
        self.assertGreater(c["score"], 0)
        self.assertEqual(c["kb"]["vault"], "测试知识库")

    def test_SKB2_no_vault_no_candidate(self):
        self._add_doc("设计-01.md", "# 海报排版\n\n留白。\n")
        self.assertEqual(
            fish._kb_push_candidates(self.conn, self.root, query="海报排版", limit=2), [])

    def test_SKB3_empty_query_no_candidate(self):
        self._register_vault()
        self._add_doc("设计-01.md", "# 海报排版\n\n留白。\n")
        self.assertEqual(fish._kb_push_candidates(self.conn, self.root, query="  "), [])

    def test_SKB4_recently_pushed_not_repeated(self):
        self._register_vault()
        self._add_doc("设计-01.md", "# 海报排版\n\n留白。\n")
        first = fish._kb_push_candidates(self.conn, self.root, query="海报排版", limit=2)
        self.assertEqual(len(first), 1)
        fish.append_push_log(self.conn, [first[0]["code"]], channel="tool_after",
                             citizen_id=CID, session_key="dsh:test")
        again = fish._kb_push_candidates(self.conn, self.root, query="海报排版", limit=2)
        self.assertEqual(again, [], "24 小时内同一文件不重复推")


class PromptTimingTest(KbPushBase):
    """S-KB5：prompt 时机也递知识库（用户原话就是 query）。"""

    def test_SKB5_prompt_candidates_include_kb(self):
        self._register_vault()
        self._add_doc("设计-01.md", "# 海报排版\n\n留白与层级是核心。\n")
        cands = fish.build_prompt_candidates(
            self.conn, self.root, prompt="海报排版怎么做", citizen_id=CID,
            session_key="dsh:test", project=None)
        kb = [c for c in cands if c.get("kind") == "kb"]
        self.assertEqual(len(kb), 1, [c.get("code") for c in cands])
        self.assertIn("设计-01.md", kb[0]["title"])


if __name__ == "__main__":
    unittest.main()
