#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文档治理判据（序4，2026-11-11）。

## 它守什么

工作区有 96 份 Markdown，其中 94 份带编号，而编号被**五族共用**同一个空间：
`20-系统全景…` 与 `20-外部评估…` 并存，**20 组撞号**。加上**七套阶段编号**并存
（最要命的一句在 `66` §4.1：**"同一个「P0」在不同文档里含义完全不同"**），
以及同一指标多个口径（README 徽章曾写 1,647，实际 1,737）。

这三件事的共同点：**没有任何机制能让它们"变红"**。于是它们只会越漂越远。

这组判据补上那个机制：

  G1  四条不变式（覆盖 / 唯一 / 定状态 / 引用完整）全过
  G2  索引与目录**不漂移**：重新生成必须逐字一致（手改会被发现）
  G3  **指标单一出处**：README 徽章里的数必须等于**现测**的数
  G4  阶段口径**只有一套现行**（A–D），历史编号必须写明出处
  G5  撞号被**显式登记**（不能悄悄消失，也不能悄悄变多）

## 判据为什么这么设计

G2 是这份文件里最重要的一条：它能抓住"有人手改了 `71-文档索引`"。
如果索引可以手改，它就会变成**第二份会漂的真相**——那还不如没有。

G3 抓的是"数字被抄进第二个地方"。修法不是"记得更新"，而是**只能通过
`tools/doc_index.py --stamp` 写那个数字**，判据比对它与实测。
"""
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
WS = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

sys.path.insert(0, str(HERE / "tools"))
import doc_index   # noqa: E402


class DocGovernanceTest(unittest.TestCase):

    # 公开包（`fish-public`）只镜像 `p0/`，**没有工作区那 96 份文档**。
    # 所以这组判据在那种树上必须 **SKIP**（"判不到，不是通过"），
    # 而不是报一堆"文档不存在"的假红——假红会训练人忽略判据。
    MIN_DOCS_FOR_GOVERNANCE = 50

    @classmethod
    def setUpClass(cls):
        cls.docs = doc_index.scan(WS)
        cls.errs, cls.warns = doc_index.check(cls.docs)
        if len(cls.docs) < cls.MIN_DOCS_FOR_GOVERNANCE:
            raise unittest.SkipTest(
                f"这棵树只有 {len(cls.docs)} 份工作区文档（公开包不镜像文档）"
                f"——判不到，不是通过")

    # ------------------------------------------------------------------ G1
    def test_G1_index_invariants_hold(self):
        """四条不变式：覆盖 / 唯一 / 定状态 / 引用完整。"""
        self.assertEqual(self.errs, [], f"文档索引不变式失败：{self.errs[:5]}")

    # ------------------------------------------------------------------ G5
    def test_G5_collisions_are_explicitly_registered(self):
        """20 组撞号必须被**显式登记**为告警。

        如果哪天撞号消失了，这条会红——那是好事，但得有人**主动**把期望值改小；
        如果撞号变多了，它也会红——那是**新的债**。两种变化都必须显式发生。
        """
        import collections
        bynum = collections.defaultdict(list)
        for d in self.docs:
            if d["num"] is not None:
                bynum[d["num"]].append(d)
        dups = {n: v for n, v in bynum.items() if len(v) > 1}
        self.assertEqual(len(dups), 20,
                         f"撞号组数变了（现 {len(dups)} 组）："
                         f"新增或消失都必须显式登记，不能悄悄变")
        warned = [w for w in self.warns if "被" in w and "份共用" in w]
        self.assertEqual(len(warned), 20,
                         f"撞号告警数 {len(warned)} 与撞号组数 {len(dups)} 不一致")

    # ------------------------------------------------------------------ G2
    def test_G2_generated_index_is_not_drifted(self):
        """索引必须与生成器一致——**手改 71 号文档会被这里抓住**。"""
        path = WS / doc_index.INDEX_NAME
        self.assertTrue(path.exists(), "索引不存在：先跑 `python p0/tools/doc_index.py`")
        expected = doc_index.render_markdown(self.docs, self.errs, self.warns)
        actual = path.read_text(encoding="utf-8")
        self.assertEqual(actual, expected,
                         "索引与生成器输出不一致：别手改它，跑 `python p0/tools/doc_index.py`")
        catalog = WS / doc_index.CATALOG_NAME
        self.assertTrue(catalog.exists(), "机器目录不存在")
        got = json.loads(catalog.read_text(encoding="utf-8"))
        self.assertEqual(len(got), len(self.docs), "机器目录与扫描结果条数不一致")

    # ------------------------------------------------------------------ G3
    def test_G3_metrics_have_a_single_source(self):
        """**指标单一出处**：README 徽章里的数 = 现测的数。

        这条防的是"数字被抄进第二个地方然后静静过期"——README 曾写 1,647，
        实际 1,737，而中间没有任何东西会红。
        """
        readme = (WS / "README.md").read_text(encoding="utf-8")
        m = doc_index.BADGE_TESTS_RE.search(readme)
        self.assertIsNotNone(m, "README 里找不到 tests 徽章")
        badge = int(m.group(2).replace("%2C", ""))
        measured = self._count_criteria()
        self.assertEqual(
            badge, measured,
            f"README 徽章 {badge} ≠ 实测判据数 {measured}；"
            f"跑 `python p0/tools/doc_index.py --stamp` 更新（**唯一写入口**）")

        m2 = doc_index.BADGE_LINES_RE.search(readme)
        self.assertIsNotNone(m2, "README 里找不到 fish.py 行数徽章")
        badge_lines = int(m2.group(2).replace("%2C", ""))
        real_lines = len((HERE / "fish.py").read_text(encoding="utf-8").split("\n")) - 1
        self.assertEqual(badge_lines, real_lines,
                         f"README 徽章行数 {badge_lines} ≠ 实测 {real_lines}")

    def _count_criteria(self):
        """只收集不执行（约 3 秒），避免让治理判据变成一次全量跑。"""
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q",
             "--no-header", "-p", "no:cacheprovider"],
            cwd=str(HERE), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=600)
        m = re.search(r"(\d+) tests? collected", r.stdout or "")
        self.assertIsNotNone(m, f"收集判据失败：{(r.stdout or '')[-400:]}")
        return int(m.group(1))

    # ------------------------------------------------------------------ G4
    def test_G4_one_canonical_phase_scheme(self):
        """阶段口径只有一套现行（A–D），其余六套必须写明"历史编号"。"""
        canon = doc_index.PHASE_CANON
        legacy = doc_index.PHASE_LEGACY
        self.assertEqual(len(canon), 4, "现行口径应是 A–D 四阶段")
        self.assertEqual([c[0] for c in canon], ["A", "B", "C", "D"])
        self.assertEqual(len(legacy), 6, "历史编号应有六套（七套总额 - 现行一套）")
        for _name, src, _meaning, rule in legacy:
            self.assertIn("历史编号", rule,
                          f"历史编号 {_name} 的规则里必须出现「历史编号」字样——"
                          f"否则读者会把它当成现行口径")
            self.assertTrue(src, f"历史编号 {_name} 必须写明出处（{src!r}）")

    # ------------------------------------------------------------------ 额外
    def test_every_numbered_doc_has_an_id(self):
        """覆盖：带编号的文档一份都不能漏（漏了就等于没被治理）。"""
        numbered = [d for d in self.docs if d["num"] is not None]
        self.assertGreaterEqual(len(numbered), 90,
                                f"带编号文档只有 {len(numbered)} 份，扫描可能失明")
        for d in numbered:
            self.assertTrue(d["id"], f"{d['file']} 没分到 ID")
            self.assertRegex(d["id"], r"^[A-Z]-?[0-9]|^[A-Z]\d+[a-z]?$",
                             f"{d['file']} 的 ID 形态不对：{d['id']}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
