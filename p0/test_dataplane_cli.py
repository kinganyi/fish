#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部审查 F-P1-5 判据：数据面的结果必须能被**产品命令**复现。

## 审查方指出的问题（已复核成立）

`fish_dataplane.py` 有 10 个公开函数：`classify_bytes` / `classify_file` /
`walk_vault` / `scan_vault` / `build_referenced_by` / `make_thumbnails` /
`asset_stats` / `deliver`。它们产出的正是 W1–W4 那些数：

    2,196 条资产 / 10.71 GB / 1,920 张缩略图
    681 条被引用 / 1,515 条无引用（6.7 GB 可归档）

**而 CLI 里对它们的引用是零。** 这些数字是我开发时用临时脚本跑出来的，
产品里没有任何命令能让别人复现。一个只能被作者临时脚本触发的功能，等于不存在。

## 这批判据为什么能失败

主判据直接**解析 argparse 的注册表**，断言 `dataplane` 存在且四个动作都在。
把注册删掉、或只留一个动作，它就红——它不看实现，只看"有没有入口"。

第二条更狠：它调**真 CLI**（子进程）跑 `stats`，断言输出里有资产条数与 GB 数。
入口在但动作没接上（`set_defaults` 写错函数）时，这条红。
"""
import re
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish_cli  # noqa: E402


def _has_assets():
    """这个树里有没有资产数据？没有就 skip（不是 fail）。

    第四轮评审后补：公开镜像的 `data/` 是测试产物，没有 kb_asset。
    判"stats 打印真实资产数"在缺数据时是**判不到**，不是失败。
    """
    import sqlite3
    db = HERE / "data" / "index.sqlite"
    if not db.is_file():
        return False
    try:
        c = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        try:
            return c.execute("SELECT COUNT(*) FROM kb_asset").fetchone()[0] > 0
        finally:
            c.close()
    except sqlite3.Error:
        return False


def _run(*argv):
    return subprocess.run(
        [sys.executable, str(HERE / "fish.py"), *argv],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", cwd=str(HERE), timeout=600)


class DataplaneCliExistsTest(unittest.TestCase):
    """入口存在性——不跑真数据，只看注册表。"""

    def setUp(self):
        src = (HERE / "fish_cli.py").read_text(encoding="utf-8")
        self.src = src

    def test_dataplane_command_is_registered(self):
        self.assertIn('add_parser("dataplane"', self.src,
                      "`fish dataplane` 没有注册——数据面仍然没有产品入口")

    def test_all_four_actions_are_registered(self):
        """**核心**：stats / scan / refs / thumbs 四个动作都要在。"""
        m = re.search(r'for _name, _help in \((.*?)\):', self.src, re.S)
        self.assertIsNotNone(m, "找不到动作注册那段循环")
        block = m.group(1)
        missing = [a for a in ("stats", "scan", "refs", "thumbs")
                   if f'("{a}"' not in block]
        self.assertEqual(missing, [], f"这些动作没注册：{missing}")

    def test_actions_default_to_dry_run(self):
        """**反向判据**：每个动作都必须先有 `--apply` 才写库。

        这些操作遍历 10.7 GB。默认就写库意味着一次手滑改掉整个资产表。
        """
        self.assertIn('"--apply"', self.src, "没有 --apply 开关")
        # 实现里也要真的尊重它：`dry_run=not getattr(..., "apply", False)`
        impl = (HERE / "fish_dataplane.py").read_text(encoding="utf-8")
        self.assertIn('dry_run=not getattr(args, "apply", False)', impl,
                      "实现没把 --apply 接到 dry_run 上（开关是装饰）")

    def test_classify_is_wired(self):
        """`classify` 让"为什么这个文件算 image"有一个可查的答案。"""
        self.assertIn('add_parser("classify"', self.src)


class DataplaneCliWorksTest(unittest.TestCase):
    """真子进程：入口在 ≠ 动作接上了。"""

    def test_stats_prints_real_asset_counts(self):
        """**核心**：跑真 CLI，输出里必须有资产条数与 GB 读数。

        `set_defaults` 接错函数、或 `cli()` 签名与 `main()` 的
        `rc = args.func(args)` 不匹配时，这条红（实测踩过：三参数签名）。
        """
        if not _has_assets():
            self.skipTest("没有资产数据可读（公开镜像/裸树）——判不到，不是通过")
        r = _run("dataplane", "stats")
        self.assertEqual(r.returncode, 0,
                         f"`fish dataplane stats` 失败：\n{(r.stderr or '')[-600:]}")
        self.assertRegex(r.stdout, r"资产\s*\d+\s*条",
                         f"没有资产条数：{r.stdout[:300]}")
        self.assertRegex(r.stdout, r"\d+\.\d+\s*GB", f"没有 GB 读数：{r.stdout[:300]}")

    def test_classify_does_not_trust_the_extension(self):
        """"分类只看魔数"（审查方 I5）——把 .sqlite 当目标，kind 必须是 other。

        如果实现按扩展名判，`index.sqlite` 会被猜成某个类型而不是 other。
        """
        target = HERE / "data" / "index.sqlite"
        if not target.exists():
            self.skipTest("没有生产库可抽样——判不到，不是通过")
        r = _run("dataplane", "classify", str(target))
        self.assertEqual(r.returncode, 0, (r.stderr or "")[-400:])
        self.assertIn("kind  = other", r.stdout,
                      f"按扩展名猜了类型？{r.stdout[:300]}")

    def test_unknown_action_is_a_clear_error(self):
        r = _run("dataplane", "nope")
        self.assertNotEqual(r.returncode, 0, "未知动作居然成功了")
        self.assertNotIn("Traceback", r.stderr, "不该抛裸异常")


class DataplaneFunctionsAreReachableTest(unittest.TestCase):
    """十个函数的入口一个都不能是孤岛。"""

    def test_every_public_function_is_used_somewhere(self):
        impl = (HERE / "fish_dataplane.py").read_text(encoding="utf-8")
        cli_src = (HERE / "fish_cli.py").read_text(encoding="utf-8")
        names = re.findall(r"^def ([a-z][a-z0-9_]*)\(.*?\) -> ", impl, re.M)
        # CLI 层至少要知道这几个（其余由测试直接覆盖）
        covered = {"cli"}
        orphans = [n for n in names
                   if n not in covered and n not in cli_src
                   and f"self.{n}" not in impl and f" {n}(" not in impl.split("def " + n)[-1][:2000]]
        # 允许 deliver / classify_bytes 这类被其它函数内部调用的
        orphans = [n for n in orphans if impl.count(n + "(") <= 1]
        self.assertEqual(orphans, [],
                         f"这些公开函数没有任何调用点（孤岛）：{orphans}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
