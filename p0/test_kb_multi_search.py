# -*- coding: utf-8 -*-
"""`_kb_search_multi` 的判据（2026-10-27 · 知识库检索一次 walk 化）。

## 这个文件在守什么

`_kb_push_candidates` 原本对每个检索词各调一次 `kb_search`，而 `kb_search`
每次都完整遍历 vault（实测 297 md ≈ 0.9s）。3 个词 = 3 次全遍历 ≈ 2.7s，
把 `prompt-push` 顶到 hook 的 10s 超时线上（实测 5.4–16.7s，不稳定）。

`_kb_search_multi` 把「遍历 + 读文件 + 编码检测」做成一次。本文件守三件事：

  M1 **等价性**：multi 的 hits 与逐个调 `kb_search` **逐条一致**
     （rel / occurrences / 行号 / 片段）。这是"重构不变行为"的硬判据。
  M2 **一次 walk**：`_kb_walk` 的调用次数 == 1（不是 N 个词就 N 次）。
     变异：把 multi 改成对每个词各 walk 一遍 → M2b 必红。
  M3 **边界**：空 / 空白 / 不存在的 vault → `{}`，不抛、不污染审计。

## 判据必须能变红（本文件的变异自证）

见 `mutation_kb_multi.py`：
  V1 把 multi 的 `occurrences` 改成恒 1 → M1 红
  V2 把 multi 改成循环调用 kb_search（即退回旧实现）→ M2 红
  V3 去掉空 needles 的早退 → M3 红
"""
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fish
import fish_kb  # noqa: E402 —— M2 要 patch 模块内的 `_kb_walk`（不是 re-export 的副本）


def _make_vault(root: Path) -> Path:
    """造一个固定的小 vault（3 个 md，含中文/英文/重复词）。"""
    v = root / "vault"
    v.mkdir(parents=True, exist_ok=True)
    (v / "a.md").write_text(
        "# 漫剧方法论\n\n这里是漫剧的第一段。\n漫剧漫剧（重复两次）。\n", encoding="utf-8")
    (v / "b.md").write_text(
        "# Fish 笔记\n\nfish appears here.\n\nAnother line about fish and 剧本.\n",
        encoding="utf-8")
    (v / "c.md").write_text(
        "# 剧本\n\n这是剧本正文，含剧本两次：剧本。\n", encoding="utf-8")
    # 专供 M1b：**单行内 5 次**（这样"数出现次数"与"数命中行数"会被区分开——
    # 变异成 occurrences += 1 时它会变成 1，精确断言立刻红）。
    (v / "d.md").write_text(
        "# 计数探针\n\n重复词重复词重复词重复词重复词\n", encoding="utf-8")
    (v / "skip.txt").write_text("fish should not be searched\n", encoding="utf-8")
    sub = v / ".obsidian"
    sub.mkdir(exist_ok=True)
    (sub / "ignored.md").write_text("# fish in ignored dir\n", encoding="utf-8")
    return v


class KbMultiSearchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-kbmulti-"))
        self.root = self.tmp / "data"
        self.root.mkdir()
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.vault_dir = _make_vault(self.tmp)
        # 登记 vault（走真实 API，不手写 SQL——否则测的是我自己的假设）
        # 真实签名：kb_vault_add(conn, *, name, path, mode="ro", ignore=(), ...) -> dict
        self.vault = fish.kb_vault_add(
            self.conn, name="测试库", path=str(self.vault_dir), mode="ro",
            ignore=[".obsidian/**", ".trash/**"])

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── M1 等价性 ────────────────────────────────────────────────
    def test_M1_equivalent_to_per_needle_search(self):
        """multi 的 hits 必须与逐个调 kb_search 逐条一致。"""
        needles = ["fish", "漫剧", "剧本", "不存在的词ZZZ"]
        expected = {}
        for n in needles:
            r = fish.kb_search(self.conn, self.vault, n, limit=2)
            expected[n] = [
                (h["rel"], h["occurrences"], [ln["lineno"] for ln in h["lines"]])
                for h in (r.get("hits") or [])]
        got = fish._kb_search_multi(self.conn, self.vault, needles, limit_per_needle=2)
        for n in needles:
            actual = [
                (h["rel"], h["occurrences"], [ln["lineno"] for ln in h["lines"]])
                for h in (got.get(n, {}).get("hits") or [])]
            self.assertEqual(expected[n], actual,
                             f"needle={n!r} 的命中与逐个 kb_search 不一致")

    def test_M1b_occurrences_are_counted_not_constant(self):
        """`occurrences` 必须是**真实出现次数**，不是恒 1、也不是"命中行数"。

        d.md 里「重复词」在**同一行出现 5 次**：
          · 正确实现 → occurrences = 5
          · `occurrences += 1`（数行）→ 1
          · `occurrences = 1`（恒 1）→ 1
        所以断言必须是**精确等于 5**，不能用 `> 1`（那会让"数行"的实现逃逸）。
        """
        got = fish._kb_search_multi(self.conn, self.vault, ["重复词"], limit_per_needle=2)
        hits = got["重复词"]["hits"]
        self.assertEqual(len(hits), 1, "「重复词」只出现在 d.md，应只有 1 个文件命中")
        self.assertEqual(int(hits[0]["occurrences"]), 5,
                         f"同一行 5 次必须数出 5，实际 {hits[0]['occurrences']}")
        # 行号也要对（正文第 3 行）
        self.assertEqual([ln["lineno"] for ln in hits[0]["lines"]], [3])

    def test_M1c_ignores_non_md_and_ignored_dirs(self):
        """非 .md 与被忽略目录里的内容不可被搜到。"""
        got = fish._kb_search_multi(self.conn, self.vault,
                                    ["should not be searched", "ignored dir"],
                                    limit_per_needle=5)
        for n in ("should not be searched", "ignored dir"):
            self.assertEqual(got[n]["hits"], [],
                             f"{n!r} 应被忽略（非 md / .obsidian 目录）")

    # ── M2 一次 walk ─────────────────────────────────────────────
    def test_M2_walks_vault_exactly_once(self):
        """`_kb_walk` 的调用次数必须 == 1（不是 N 个词 N 次）。

        ⚠️ **必须 patch `fish_kb._kb_walk`，不是 `fish._kb_walk`**：
        `_kb_search_multi` 住在 `fish_kb.py`，它调 `_kb_walk` 走的是**本模块全局名**；
        `fish._kb_walk` 只是 re-export 的同一个对象引用，patch 它不会影响模块内调用。
        （这条判据第一次跑就是因此红的 —— 0 次 walk 说明 patch 打空了。）
        """
        calls = {"n": 0}
        real = fish_kb._kb_walk

        def counting(vault_row, start_abs):
            calls["n"] += 1
            return real(vault_row, start_abs)

        orig = fish_kb._kb_walk
        fish_kb._kb_walk = counting
        try:
            fish._kb_search_multi(self.conn, self.vault,
                                  ["fish", "漫剧", "剧本", "a", "b"],
                                  limit_per_needle=2)
        finally:
            fish_kb._kb_walk = orig
        self.assertEqual(calls["n"], 1,
                         f"5 个词只应 walk 1 次，实际 {calls['n']} 次"
                         "（0 次说明 patch 打在了 fish 而不是 fish_kb）")

    def test_M2b_faster_than_per_needle(self):
        """同一组词，multi 必须**明显快于**逐个调 kb_search。

        阈值取 1.5x（保守）：真实实测树上是 2.65–5.49x，
        这里用"必须至少快 1.5 倍"避免机器抖动导致的假红。
        """
        # 造更多文件，让遍历成本占主导（否则噪声会淹没信号）
        big = self.vault_dir / "bulk"
        big.mkdir(exist_ok=True)
        for i in range(120):
            (big / f"n{i:03d}.md").write_text(
                f"# 第 {i} 篇\n\n" + ("填充文本 fish 漫剧 剧本\n" * 40), encoding="utf-8")
        needles = ["fish", "漫剧", "剧本"]

        t0 = time.perf_counter()
        for n in needles:
            fish.kb_search(self.conn, self.vault, n, limit=2)
        t_old = time.perf_counter() - t0

        t0 = time.perf_counter()
        fish._kb_search_multi(self.conn, self.vault, needles, limit_per_needle=2)
        t_new = time.perf_counter() - t0

        self.assertGreater(t_old, 0.0, "旧实现耗时必须可测")
        ratio = t_old / max(t_new, 1e-6)
        self.assertGreater(ratio, 1.5,
                           f"multi 应至少快 1.5x，实际 {ratio:.2f}x"
                           f"（old={t_old:.3f}s new={t_new:.3f}s）")

    # ── M3 边界 ─────────────────────────────────────────────────
    def test_M3a_empty_needles_returns_empty(self):
        self.assertEqual(fish._kb_search_multi(self.conn, self.vault, []), {})

    def test_M3b_blank_needles_returns_empty(self):
        got = fish._kb_search_multi(self.conn, self.vault, ["", "   ", "\t"])
        self.assertEqual(got, {}, "全空白词应返回 {}（不抛、不扫）")

    def test_M3c_missing_vault_returns_empty(self):
        bad = dict(self.vault, path=str(self.tmp / "no-such-dir"))
        got = fish._kb_search_multi(self.conn, bad, ["fish"], limit_per_needle=2)
        self.assertEqual(got, {}, "vault 不存在应返回 {}，不抛")

    def test_M3d_limit_is_honored_and_capped(self):
        got = fish._kb_search_multi(self.conn, self.vault, ["fish", "漫剧"],
                                    limit_per_needle=999)
        for n, r in got.items():
            self.assertLessEqual(len(r["hits"]), fish.KB_MAX_LIMIT,
                                 f"{n} 的 hits 超过 KB_MAX_LIMIT")

    # ── M4 _kb_push_candidates 仍然工作 ──────────────────────────
    def test_M4_push_candidates_uses_multi(self):
        """`_kb_push_candidates` 必须走 multi（不再逐个调 kb_search）。

        T02 拆包：`_kb_push_candidates` 已搬进 `fish_push`——它调 `_kb_search_multi`
        走的是 **fish_push 模块全局**（`from fish_kb import _kb_search_multi` 的绑定）；
        patch `fish.*` 只是改导入副本（from-import 快照），生效不了。
        注意：`fish_push` **没有** `kb_search` 名字（旧回退已删），所以只 spy multi。
        """
        import fish_push
        calls = {"multi": 0}
        real_multi = fish_push._kb_search_multi

        def cm(*a, **k):
            calls["multi"] += 1
            return real_multi(*a, **k)

        fish_push._kb_search_multi = cm
        try:
            out = fish_push._kb_push_candidates(self.conn, self.root,
                                                query="漫剧剧本怎么写", limit=2)
        finally:
            fish_push._kb_search_multi = real_multi
        self.assertGreaterEqual(calls["multi"], 1, "应至少调一次 multi")
        self.assertIsInstance(out, list)
        # 评审 🟡-9（2026-10-27）：T02 迁移时把「不许逐个调 kb_search」的负向断言整条删了。
        # 改成**结构断言**：fish_push 里不存在 kb_search 绑定（旧回退路径已删），
        # 所以"逐个调"在结构上不可能发生——比引用计数更稳。
        self.assertFalse(hasattr(fish_push, "kb_search"),
                         "fish_push 不应再持有 kb_search 绑定（旧回退已删）")

    # ── M5 `_kb_judge_component` 的 lru_cache（2026-10-27 第三轮优化）────
    def test_M5_lru_cache_does_not_change_result(self):
        """缓存不得改变任何输出：清缓存前后、同一输入必须同一结果。

        判据来源：`_kb_judge_component` 是纯函数（只读 part、只调 normcase/rstrip）。
        加缓存后必须**逐字节等价**，否则就是静默行为变更。
        """
        probes = ["**", ".obsidian", "90-模板", "ABC", "x. ", "  ", None, "",
                  "übung", "a/b", ".TRASH"]
        fish_kb._kb_judge_component.cache_clear()
        cold = [fish_kb._kb_judge_component(p) for p in probes]
        # 再取一遍（此时全是缓存命中）
        warm = [fish_kb._kb_judge_component(p) for p in probes]
        self.assertEqual(cold, warm, "清缓存后首次结果 与 缓存命中结果 必须一致")
        # 再清一次，仍必须一致（证明缓存没"记住错的东西"）
        fish_kb._kb_judge_component.cache_clear()
        again = [fish_kb._kb_judge_component(p) for p in probes]
        self.assertEqual(cold, again, "再次清缓存后结果仍须一致")

    def test_M5b_lru_cache_is_actually_hitting(self):
        """缓存必须**真的产生净收益**：`hits > misses`（重复调用多于唯一值）。

        ## 为什么不用"命中率 > 80%"

        命中率**随库规模上升**：真实 vault（297 个 md）实测 92.8%
        （hits=27,814 / misses=2,158），但本测试的夹具只有 4 个 md，
        命中率只有 ~76%。**拿小夹具断言 80% 是在测夹具规模，不是在测缓存是否有效。**

        `hits > misses` 才是本质判据：它说明"同一个路径分量的重复判定多于唯一值"，
        而这正是缓存有净收益的前提。两档规模下都成立。

        **变异**：去掉 `@functools.lru_cache` 装饰器 → 无 `cache_info` → 本判据红。
        """
        self.assertTrue(hasattr(fish_kb._kb_judge_component, "cache_info"),
                        "_kb_judge_component 应被 lru_cache 装饰（否则无 cache_info）")
        fish_kb._kb_judge_component.cache_clear()
        fish._kb_search_multi(self.conn, self.vault, ["fish", "漫剧", "剧本"],
                              limit_per_needle=2)
        info = fish_kb._kb_judge_component.cache_info()
        self.assertGreater(info.hits, 0, "缓存必须被命中过（否则等于没加）")
        self.assertGreater(info.hits, info.misses,
                           f"缓存的重复收益应大于唯一值成本："
                           f"hits={info.hits} 必须 > misses={info.misses}"
                           f"（真实库实测 27,814 > 2,158）")
        # 掩码判定：misses 应等于"不同 part 数"，量级与文件数×glob 数成正比
        self.assertLessEqual(info.misses, 4096,
                             f"misses={info.misses} 过多，说明缓存 key 粒度不对")


if __name__ == "__main__":
    unittest.main(verbosity=2)
