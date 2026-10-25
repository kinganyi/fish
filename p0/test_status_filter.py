#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fish 读路径 status 过滤 · 判据测试（2026-10-24）
================================================
三处「查询未筛 status」的缺陷（真 bug，生产库**只读副本**已证实）：

  · `similar_codes()`    —— 语义相似候选不筛 `code_index.status`，
                             于是给 withdrawn 的条目建出语义边；
  · `code_mentions_in()` —— 正文提及不筛 status，于是边指向死条目；
  · `recent_fallback()`  —— 注释承诺「与检索同样的过滤」，实现漏了 status。

判据（每条都配真源码变异自证，见 `mutation_status_filter.py`）：

  A  similar_codes 不返回非 active
  B  similar_codes 仍返回 active
  C  code_mentions_in 不返回非 active（正文提到已作废编码 → 不建边）
  D  code_mentions_in 仍认 active（正文提到活跃编码 → 建边）
  E  recent_fallback 不返回非 active
  F  真实数据复核：生产库副本上 `similar_codes(企业智能-261022-14)` 不再返回
     `ANYI-261022-29`（该条 status='withdrawn'）
  G  （附加）`recent_fallback(include_superseded=True)` 的显式退路仍然可用

纪律：
  · 夹具全部落在 tempdir；判据 F 的生产库只经 `file:...?mode=ro` 打开并用
    backup API 拷贝，**绝不写原库**；
  · 断言真值来自独立 SQL / 独立复算，不复用被测函数的返回结果自证；
  · 判据 F 可用环境变量 `FISH_PROD_DB` 指定另一个只读库路径。
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fish  # noqa: E402

HERE = Path(__file__).resolve().parent
PROD_DB = HERE / "data" / "index.sqlite"
# 生产库里的活见证：来源条目 active；被误当相似项的条目早已 withdrawn。
# 判据 F 的两个坐标。**2026-10-25 数据治理后更新过一次**：
#   原 PROD_RETIRED = "ANYI-261022-29"，该条在改名迁移中随主体去了 anyi-studio
#   （它是 withdrawn，分类按"跟随主体"）。判据意图完全不变——仍是
#   「作废条目不该出现在 similar_codes 结果里」——只更新坐标。
#   ⚠️ 这不是放宽断言：如果 similar_codes 返回它，判据照样红。
PROD_CODE = "企业智能-261022-14"
PROD_RETIRED = "anyi-studio-261022-29"

# 两条「几乎同一段话」的正文：Jaccard 必然远高于 SIM_MIN_SIM。
SIM_BODY = "语义相似判据专用正文：冻结写入、导出快照、核对断链与回填对账。"


class FishStatusFilterTest(unittest.TestCase):
    """判据 A–G。类名与文件名被 mutation_status_filter.py 逐条点名调用。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fish-status-")
        self.root = Path(self.tmp) / "data"
        self.root.mkdir(parents=True, exist_ok=True)
        conn = fish.connect(self.root)
        try:
            conn.executescript(fish.SCHEMA)
            fish.migrate(conn)
            for prefix in ("TEST", "ZZZ"):
                conn.execute(
                    "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
                    "VALUES (?,?,?,?,?)", (prefix, prefix, "", 0, "active"))
            conn.commit()
        finally:
            conn.close()
        self.conn = fish.connect(self.root)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ 夹具

    def _remember(self, title, body, project="TEST"):
        code, _ = fish.record_memory(
            self.conn, self.root, project=project, title=title, body=body,
            mem_type="fact")
        return code

    def _status(self, code):
        row = self.conn.execute(
            "SELECT status FROM code_index WHERE code=?", (code,)).fetchone()
        return row[0] if row else None

    def _fts_candidates(self, conn, title, body):
        """独立复算：不带 status 的 FTS 候选池（用于证明"它本来会被召回"）。

        只借用 token 切分规则（与索引口径一致），不调用被测的 similar_codes。
        """
        toks = {t for t in fish.index_tokens(f"{title or ''} {body or ''}") if len(t) >= 2}
        expr = " OR ".join('"' + t.replace('"', '""') + '"' for t in sorted(toks)[:60])
        return {r[0] for r in conn.execute(
            "SELECT code FROM memory_fts WHERE memory_fts MATCH ? LIMIT 100", (expr,))}

    def _jaccard(self, conn, title, body, code):
        """独立复算：候选与查询正文的 Jaccard（真值不经过 similar_codes）。"""
        toks = {t for t in fish.index_tokens(f"{title or ''} {body or ''}") if len(t) >= 2}
        row = conn.execute(
            "SELECT COALESCE(title_idx,'') || ' ' || COALESCE(body_idx,'') "
            "FROM memory_fts WHERE code=?", (code,)).fetchone()
        other = set((row[0] or "").split()) if row else set()
        union = len(toks | other)
        return (len(toks & other) / union) if union else 0.0

    def _copy_prod_db(self):
        """把生产库**只读**拷进 tempdir（模式 ro + backup API，不碰原库）。"""
        src = Path(os.environ.get("FISH_PROD_DB") or PROD_DB)
        if not src.exists():
            self.fail(
                f"判据 F 需要真实库：{src} 不存在。"
                f"请设 FISH_PROD_DB 指向 p0/data/index.sqlite 的只读副本。")
        dst = Path(self.tmp) / "prod-copy.sqlite"
        ro = sqlite3.connect(src.as_uri() + "?mode=ro", uri=True)
        try:
            out = sqlite3.connect(str(dst))
            try:
                ro.backup(out)
            finally:
                out.close()
        finally:
            ro.close()
        return dst

    # ------------------------------------------------------------ 判据 A

    def test_A_similar_codes_excludes_inactive(self):
        """判据 A：active 甲 + withdrawn 乙（正文相同）→ 结果里没有乙。"""
        base = self._remember("相似甲", SIM_BODY)
        retired = self._remember("相似乙", SIM_BODY)
        fish.withdraw_memory(self.conn, retired, reason="判据 A：造一个作废条目")
        self.assertEqual(self._status(retired), "withdrawn")

        # 独立证明"乙本来会被召回"：否则"没返回"可能只是它压根没进候选池，
        # 那种情况下判据 A 变成恒真，变异也就测不出判别力。
        self.assertIn(retired, self._fts_candidates(self.conn, "相似甲", SIM_BODY),
                      "夹具失效：作废条目根本不在 FTS 候选池，判据 A 没有判别力")

        res = fish.similar_codes(self.conn, base, SIM_BODY, title="相似甲", limit=10)
        codes = [c for c, _sim in res]
        self.assertNotIn(retired, codes,
                         f"similar_codes 返回了已作废条目 {retired}：{res}")
        for code, sim in res:
            self.assertEqual(self._status(code), "active",
                             f"similar_codes 返回了非 active 条目 {code}（sim={sim}）")

    # ------------------------------------------------------------ 判据 B

    def test_B_similar_codes_keeps_active(self):
        """判据 B：active 甲 + active 丙（正文相同）→ 结果里有丙。"""
        base = self._remember("相似甲", SIM_BODY)
        peer = self._remember("相似丙", SIM_BODY)
        self.assertEqual(self._status(peer), "active")

        res = fish.similar_codes(self.conn, base, SIM_BODY, title="相似甲", limit=10)
        sims = dict(res)
        self.assertIn(peer, sims, f"similar_codes 漏掉了 active 的相似条目：{res}")
        self.assertGreaterEqual(sims[peer], fish.SIM_MIN_SIM)
        self.assertLessEqual(sims[peer], 1.0, "相似度必须是 0~1 的 Jaccard，不是命中强度")
        for code, _sim in res:
            self.assertEqual(self._status(code), "active",
                             f"similar_codes 返回了非 active 条目 {code}")

    # ------------------------------------------------------------ 判据 C

    def test_C_mention_excludes_inactive(self):
        """判据 C：withdrawn 丁 + 正文提到丁 → code_mentions_in 不认、不建边。"""
        retired = self._remember(
            "作废丁", "作废条目专用正文：季度复盘模板与会议纪要归档口径。")
        fish.withdraw_memory(self.conn, retired, reason="判据 C")
        new = self._remember("引用作废编码", f"这条接着 {retired} 往下推：只补一句无关结论。")

        self.assertEqual(fish.code_mentions_in(self.conn, f"见 {retired}"), [],
                         "code_mentions_in 把已作废编码当成了真实编码")
        rows = self.conn.execute(
            "SELECT rel,source FROM links WHERE src=? AND dst=?", (new, retired)).fetchall()
        self.assertEqual(rows, [], f"正文提及作废编码仍建了边：{rows}")

    # ------------------------------------------------------------ 判据 D

    def test_D_mention_keeps_active(self):
        """判据 D：active 戊 + 正文提到戊 → 认出来并建 related 边。"""
        alive = self._remember(
            "活跃戊", "活跃条目专用正文：周报字段顺序与看板刷新节奏。")
        self.assertEqual(self._status(alive), "active")
        new = self._remember("引用活跃编码", f"这条接着 {alive} 往下推：只补一句无关结论。")

        self.assertEqual(fish.code_mentions_in(self.conn, f"见 {alive}"), [alive],
                         "正文里的活跃编码没被认出来")
        rows = self.conn.execute(
            "SELECT rel,source,weight,evidence FROM links WHERE src=? AND dst=?",
            (new, alive)).fetchall()
        self.assertEqual(len(rows), 1, f"正文提及活跃编码没建边：{rows}")
        rel, source, weight, evidence = rows[0]
        self.assertEqual((rel, source, evidence), ("related", "explicit", "正文提及"))
        self.assertAlmostEqual(weight, 0.8, places=3)

    # ------------------------------------------------------------ 判据 E

    def test_E_recent_fallback_excludes_inactive(self):
        """判据 E：withdrawn 己（最大 code）+ 活跃条目 → 回退列表不含己。"""
        alive = self._remember("活跃条目", "活跃条目正文：只用来占住回退列表。")
        retired = self._remember(
            "作废己", "作废条目正文：本不该出现在未命中的回退提示里。", project="ZZZ")
        fish.withdraw_memory(self.conn, retired, reason="判据 E")

        # 独立前提：己确实是"最近的那条"（ORDER BY code DESC 的第一名），
        # 否则"没返回"可能只是它排不进 limit，变异就测不出过滤有没有生效。
        newest = self.conn.execute(
            "SELECT code FROM code_index ORDER BY code DESC LIMIT 1").fetchone()[0]
        self.assertEqual(newest, retired,
                         f"夹具失效：作废条目不是最大编码（{newest}），判据 E 没有判别力")

        cand = fish.recent_fallback(self.conn, limit=10)
        codes = [c for c, _t in cand]
        self.assertIn(alive, codes, "回退列表漏掉了活跃条目")
        self.assertNotIn(retired, codes, f"回退列表返回了已作废条目：{codes}")
        for code, _title in cand:
            self.assertEqual(self._status(code), "active",
                             f"recent_fallback 返回了非 active 条目 {code}")

    # ------------------------------------------------------------ 判据 F

    def test_F_prod_copy_similar_codes_skips_retired(self):
        """判据 F：生产库只读副本上复现原缺陷并证明已修。

        原始实测（2026-10-24）：`similar_codes("企业智能-261022-14", body)` 返回
        `("ANYI-261022-29", 0.9292)`，而该条 status='withdrawn'。
        """
        conn = sqlite3.connect(str(self._copy_prod_db()))
        try:
            row = conn.execute(
                "SELECT title,status,file_path FROM code_index WHERE code=?",
                (PROD_CODE,)).fetchone()
            self.assertIsNotNone(row, f"生产副本里没有来源条目 {PROD_CODE}")
            title, status, file_path = row
            self.assertEqual(status, "active", f"{PROD_CODE} 不再是 active，判据 F 需重选来源")

            wrow = conn.execute(
                "SELECT status FROM code_index WHERE code=?", (PROD_RETIRED,)).fetchone()
            self.assertIsNotNone(wrow, f"生产副本里没有见证条目 {PROD_RETIRED}")
            self.assertEqual(wrow[0], "withdrawn",
                             f"{PROD_RETIRED} 已不是 withdrawn，判据 F 需重选见证")

            body_path = HERE / "data" / file_path
            self.assertTrue(body_path.exists(), f"来源正文不存在：{body_path}")
            body = body_path.read_text(encoding="utf-8")

            # 见证有效性（独立复算）：它确实在 FTS 候选池里、Jaccard 也过门槛。
            # 这样"结果里没有它"才只能归因于 status 过滤，而不是相似度不够。
            self.assertIn(PROD_RETIRED, self._fts_candidates(conn, title, body),
                          "见证失效：作废条目不在 FTS 候选池")
            self.assertGreaterEqual(self._jaccard(conn, title, body, PROD_RETIRED),
                                    fish.SIM_MIN_SIM,
                                    "见证失效：作废条目的相似度低于门槛")

            res = fish.similar_codes(conn, PROD_CODE, body, title=title, limit=50)
            codes = [c for c, _sim in res]
            self.assertNotIn(PROD_RETIRED, codes,
                             f"生产副本上 similar_codes 仍返回已作废条目 {PROD_RETIRED}：{res}")
            for code, sim in res:
                st = conn.execute(
                    "SELECT status FROM code_index WHERE code=?", (code,)).fetchone()
                self.assertIsNotNone(st, f"similar_codes 返回了库里不存在的编码 {code}")
                self.assertEqual(st[0], "active",
                                 f"生产副本上 similar_codes 返回了非 active 条目 "
                                 f"{code}（status={st[0]}, sim={sim}）：{res}")
        finally:
            conn.close()

    # ------------------------------------------------------------ 附加判据 G

    def test_G_recent_fallback_include_superseded_opt_in(self):
        """附加判据 G：显式 include_superseded=True 时，退役条目仍可被看到。"""
        alive = self._remember("活跃条目", "活跃条目正文：占住回退列表。")
        retired = self._remember(
            "作废己", "作废条目正文：显式放开时应当可见。", project="ZZZ")
        fish.withdraw_memory(self.conn, retired, reason="判据 G")

        default = [c for c, _t in fish.recent_fallback(self.conn, limit=10)]
        self.assertIn(alive, default)
        self.assertNotIn(retired, default, "默认口径不应含退役条目")

        expanded = [c for c, _t in fish.recent_fallback(
            self.conn, limit=10, include_superseded=True)]
        self.assertIn(retired, expanded,
                      "include_superseded=True 没放开 status 过滤（参数成了摆设）")
        self.assertIn(alive, expanded)


if __name__ == "__main__":
    unittest.main()
