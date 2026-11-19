#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生产数据护栏（INC-20261119-01 的直接防护）。

## 事故原样

2026-11-19，我在校验公开镜像的测试产物时，对**生产树**的 `p0/data/` 执行了
`Remove-Item -Recurse -Force`。前几次因为 `index.sqlite` 被运行中的进程锁着而
**静默失败**，我以为没删掉就重复试；某次锁一松动：

  · `data/projects/` —— **565 条记忆正文**（8 个历史树合并只救回 279，**286 条永久丢失**）
  · `data/corpus.sqlite` —— 147 MB 语料库
  · `data/blobs/` —— 附件

根因不是"手滑"，是**没有任何东西拦着我删生产数据**。

## 护栏守什么

  · 测试进程内对 `p0/data` 的 `shutil.rmtree` **直接抛错**；
  · 会话结束时 `data/` 的文件数**变少就报错**（兜住别的删法）。

这条判据验的就是这两层**真的在**——不是验注释里写了没有。
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import conftest  # noqa: E402


class ProductionDataGuardTest(unittest.TestCase):

    def test_rmtree_on_production_data_is_refused(self):
        """**核心**：删 `p0/data` 下的东西必须抛错。"""
        data = HERE / "data"
        if not data.is_dir():
            self.skipTest("这棵树没有 data/（公开镜像）——判不到，不是通过")
        class _C:      # pytest_configure 只要一个 config 参数，内容不用
            pass
        conftest.pytest_configure(_C())
        try:
            with self.assertRaises(AssertionError) as cm:
                shutil.rmtree(data / "projects")
            self.assertIn("INC-20261119-01", str(cm.exception),
                          "拦住了，但没说明为什么——下一个人会以为这是误拦")
        finally:
            conftest.pytest_unconfigure(_C())

    def test_rmtree_on_temp_still_works(self):
        """**不许误伤**：临时目录是测试自己的东西，必须还能删。

        护栏如果宽到把 `tempfile.mkdtemp()` 也拦了，那所有测试都会崩——
        一个把正常工作也拦掉的护栏，会被下一个人直接拆掉。
        """
        data = HERE / "data"
        if not data.is_dir():
            self.skipTest("这棵树没有 data/——判不到")
        class _C:
            pass
        conftest.pytest_configure(_C())
        try:
            t = tempfile.mkdtemp(prefix="fish-guard-ok-")
            shutil.rmtree(t)      # 不抛就是对的
            self.assertFalse(Path(t).exists(), "临时目录没删掉")
        finally:
            conftest.pytest_unconfigure(_C())

    def test_guard_is_installed_by_conftest(self):
        """护栏必须**由 conftest 自动装上**，不是靠某个测试记得调。

        事故当时没人记得——所以"记得调"不是护栏。
        """
        src = (HERE / "conftest.py").read_text(encoding="utf-8")
        self.assertIn("def pytest_configure", src,
                      "conftest 没有 pytest_configure——护栏不会自动装")
        self.assertIn("guarded_rmtree", src,
                      "conftest 没有替换 rmtree")
        self.assertIn("INC-20261119-01", src,
                      "conftest 里没记这次事故——下一个人不知道这道护栏为什么在")


if __name__ == "__main__":
    unittest.main(verbosity=2)
