#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""112 · W-D（改名消歧）判据：C6 / C7 / C8 / C9。

四对"同名不同义"的处理分两种，判据也分两组：

  · **改名**（C6 `now_utc` / C8 `_coerce` / C9 `_read_state`·`_write_state`）：
    名字必须各自表达语义——全仓不许再出现"同名但返回值/策略不同"；
  · **下沉原语 + 策略留名**（C7 `_age_sec`）：解析/相减只有一份（`fish_common.age_sec`），
    `inf` 与 `None` 两种策略留在调用点并各自命名。

## 判据为什么这么写

`now_utc` 一对的差别是**返回值类型**（str vs datetime），`_coerce` 一对的差别是
**失败策略**（默认值 vs 抛 `BadValue`），状态文件一对的差别是**原子性/返回值**。
所以判据既查"名字的唯一性"（结构），也查"名字与语义对得上"（行为）——
只查名字会漏掉"改了个名字、语义还是两份"。

变异：
  · 把 `fish_push_audit.now_utc_dt` 改回 `now_utc` → 同名扫描红；
  · 把 `fish_activity._age_sec_or_inf` 改成返回 None（合并策略）→ C-D2 红。
"""
import datetime
import re
import sys
import unittest
from pathlib import Path

P0 = Path(__file__).resolve().parent
sys.path.insert(0, str(P0))

import fish_activity  # noqa: E402
import fish_common  # noqa: E402
import fish_git_hook  # noqa: E402
import fish_push_audit  # noqa: E402


def _production_modules():
    for path in sorted(P0.glob("*.py")):
        if path.name.startswith("test_") or path.name.startswith("mutation_"):
            continue
        yield path


def _defs(name: str) -> list:
    """`def <name>(` 出现在哪些生产模块里（`文件名` 列表，不锚行号——行号会腐坏）。"""
    pattern = re.compile(r"^\s*def\s+%s\s*\(" % re.escape(name), re.M)
    found = []
    for path in _production_modules():
        if pattern.search(path.read_text(encoding="utf-8")):
            found.append(path.name)
    return found


class SameNameDifferentMeaningTest(unittest.TestCase):
    """C-D1：三对同名不同义的函数名必须消解。"""

    def test_now_utc_is_str_only(self):
        self.assertEqual(_defs("now_utc"), ["fish_common.py"],
                         "`now_utc` 又出现在多个模块里（一返 str、一返 datetime 的老问题）")
        # `now_utc_dt` 允许出现在审计与语料排序两处：**同名同义**（都返 datetime）。
        # `fish_corpus_rank` 那份 112 未列入本轮（不是"同名不同义"，不构成歧义）；
        # 判据要钉的是"名字不撒谎"，不是"全仓只有一个 datetime 助手"。
        self.assertEqual(sorted(_defs("now_utc_dt")),
                         ["fish_corpus_rank.py", "fish_push_audit.py"],
                         "`now_utc_dt` 的定义集合变了（检查是不是有人把 str 版改了回来）")

    def test_coerce_reads_defaults_while_config_coerce_raises(self):
        self.assertEqual(_defs("_coerce"), ["fish_activity.py"],
                         "`_coerce` 只许留下读侧（转不了给默认值）那一份")
        self.assertEqual(_defs("_coerce_config"), ["fish_push_audit.py"],
                         "写侧（转不了抛 BadValue）应改名为 `_coerce_config`")

    def test_state_file_accessors_are_renamed(self):
        self.assertEqual(_defs("_read_state"), [],
                         "`_read_state` 还有旧名（两份不同实现的残留）")
        self.assertEqual(_defs("_write_state"), [],
                         "`_write_state` 还有旧名（atomic vs warn 两种策略的残留）")
        for name in ("_read_state_file", "_write_state_file"):
            self.assertEqual(sorted(_defs(name)), ["fish_activity.py", "hook_fastpath.py"],
                             "%s 应恰好出现在 activity 与 fastpath 两处（同名同义：读/写状态文件）"
                             % name)

    def test_names_now_match_return_values(self):
        """行为面：名字改了，语义就必须真的对得上——不能只改字面。"""
        import fish_corpus_rank
        self.assertIsInstance(fish_common.now_utc(), str,
                              "`now_utc` 必须仍返库里的定长 UTC 字符串")
        self.assertIsInstance(fish_push_audit.now_utc_dt(), datetime.datetime,
                              "`now_utc_dt` 必须返 datetime（名字里的 dt 是承诺）")
        self.assertIsInstance(fish_corpus_rank.now_utc_dt(), datetime.datetime,
                              "同名同义：另一处 `now_utc_dt` 也必须返 datetime")
        self.assertEqual(fish_activity._coerce("not-a-number", 7), 7,
                         "读侧转换转不了要给默认值")
        with self.assertRaises(fish_push_audit.BadValue):
            fish_push_audit._coerce_config("push.enabled", "not-a-bool")
        self.assertEqual(fish_activity._read_state_file("/no/such/file.json"), {},
                         "读状态文件失败给 {}（既有契约）")

    def test_audit_module_no_longer_calls_its_own_now_utc(self):
        src = (P0 / "fish_push_audit.py").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"\bnow_utc\(", src),
                          "fish_push_audit 里还有 `now_utc()` 调用（应为 `now_utc_dt()`）")


class AgePrimitiveStrategyTest(unittest.TestCase):
    """C-D2：原语一份、策略两份，且**各自**保住原行为。"""

    def test_primitive_is_one_implementation(self):
        self.assertEqual(_defs("age_sec"), ["fish_common.py"],
                         "时间差原语应在 fish_common 里唯一一份")
        self.assertEqual(_defs("_age_sec"), [],
                         "`_age_sec` 两份同名实现都应消失（改名成带策略的名字）")
        self.assertEqual(_defs("_age_sec_or_inf"), ["fish_activity.py"])
        self.assertEqual(_defs("_age_sec_or_none"), ["fish_git_hook.py"])

    def test_primitive_returns_none_on_bad_input(self):
        self.assertIsNone(fish_common.age_sec("不是时间"),
                          "原语只做机制：解析不了返回 None，策略由调用点定")
        self.assertIsNone(fish_common.age_sec(None))
        self.assertAlmostEqual(fish_common.age_sec(
            "2026-11-24T00:00:00Z", "2026-11-24T00:00:30Z"), 30.0)

    def test_policies_stay_at_call_sites(self):
        """变异靶点：把两种策略并成一个 → 必有一侧红。"""
        self.assertEqual(fish_activity._age_sec_or_inf("不是时间"), float("inf"),
                         "过期判据解析不了必须给无穷大（原行为：视为早已过期）")
        self.assertEqual(fish_activity._age_sec_or_inf("2026-11-24T00:00:00Z",
                                                       "2026-11-24T00:01:00Z"), 60.0)
        self.assertIsNone(fish_git_hook._age_sec_or_none("不是时间"),
                          "节流判据解析不了必须给 None（原行为：不猜，照采）")
        self.assertEqual(fish_git_hook._age_sec_or_none(None), None)
        self.assertEqual(fish_git_hook._age_sec_or_none("2026-11-24T00:00:00Z",
                                                        "2026-11-24T00:01:00Z"), 60.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
