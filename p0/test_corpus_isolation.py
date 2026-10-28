#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 判据（外部审查 🔴8）：语料隔离与脱敏。

C-1 跨公民推送被挡（`conv_push_candidates` 里必须有 other_citizen 分支）
C-2 私钥整段置空（base64 正文不残留、复扫 0）——审查官实测旧实现 `residual_base64=True`
C-3 不给 citizen_id 时保持旧行为（兼容既有调用点）
"""
import inspect
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fish_corpus          # noqa: E402
import fish_corpus_redact   # noqa: E402


class CorpusIsolationTest(unittest.TestCase):

    # C-1
    def test_C1_cross_citizen_filter_exists(self):
        src = inspect.getsource(fish_corpus.conv_push_candidates)
        self.assertIn("other_citizen", src,
                      "按公民过滤的分支缺失（外部审查 🔴8：A 会话会被推 B 会话的内容）")
        self.assertIn("citizen_id", src)
        # 参数默认 None = 兼容旧调用（没有身份信息时不误杀）
        sig = inspect.signature(fish_corpus.conv_push_candidates)
        self.assertIn("citizen_id", sig.parameters)
        self.assertIsNone(sig.parameters["citizen_id"].default)

    # C-2
    def test_C2_private_key_fully_masked(self):
        body = ("-----BEGIN RSA PRIVATE KEY-----\n"
                "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7VJTUt9Us8cKj\n"
                "-----END RSA PRIVATE KEY-----")
        out, hits, kinds = fish_corpus_redact.redact(body)
        self.assertGreaterEqual(hits, 1)
        self.assertNotIn("MIIEvQ", out, "私钥正文（base64）必须整段清掉")
        self.assertNotIn("END RSA", out)
        self.assertEqual(fish_corpus_redact.scan_count(out), 0, "复扫必须 0 命中")

    # C-3
    def test_C3_no_citizen_means_old_behavior(self):
        # 不给 citizen_id 时不该出现 other_citizen 分支的拦截（旧调用兼容）
        src = inspect.getsource(fish_corpus.conv_push_candidates)
        self.assertIn("if citizen_id and", src,
                      "过滤必须受 citizen_id 存在性保护（不给 = 旧行为）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
