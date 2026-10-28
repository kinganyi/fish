#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-⑥ 判据（外部审查 🔴3）：hook 快通道。

F-1 `enabled()` 读 config：关闭键 → False；键缺失 → default
F-2 冷却**按 hook 隔离**：posttool 冷却不影响 pretool（审查 🟠-H 的反例）
F-3 连续 3 次 timeout → 冷却 600s；记一次 ok 会清 streak
F-4 hook 身份维度：状态文件路径按 hook 名分开
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fish             # noqa: E402
import hook_fastpath    # noqa: E402


class HookFastpathTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hook-fastpath-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _set_cfg(self, key, value):
        # 注意：`fish.connect` 收的是**数据目录**（它自己拼 index.sqlite），
        # 不是 db 文件路径——传错会建出一个同名目录，读侧就永远读到 "库不存在"。
        conn = fish.connect(self.tmp)
        conn.executescript(fish.SCHEMA)
        conn.execute("INSERT OR REPLACE INTO config (key,value) VALUES (?,?)",
                     (key, str(value)))
        conn.commit()
        conn.close()

    # F-1
    def test_F1_enabled_reads_config(self):
        self._set_cfg("push.enabled", "false")
        self.assertFalse(hook_fastpath.enabled(self.tmp, "push.enabled", True))
        self._set_cfg("push.enabled", "true")
        self.assertTrue(hook_fastpath.enabled(self.tmp, "push.enabled", False))
        # 键缺失 → default
        self.assertTrue(hook_fastpath.enabled(self.tmp, "nonexistent.key", True))
        self.assertFalse(hook_fastpath.enabled(self.tmp, "nonexistent.key", False))

    # F-2
    def test_F2_cooldown_is_per_hook(self):
        for _ in range(hook_fastpath.COOLDOWN_AFTER_TIMEOUTS):
            hook_fastpath.record_attempt(self.tmp, "posttool", "timeout")
        self.assertGreater(hook_fastpath.cooldown_remaining(self.tmp, "posttool"), 0)
        self.assertEqual(hook_fastpath.cooldown_remaining(self.tmp, "pretool"), 0,
                         "一个 hook 迟到不该冷却另一个（外部审查 🟠-H）")

    # F-3
    def test_F3_streak_resets_on_ok(self):
        hook_fastpath.record_attempt(self.tmp, "pretool", "timeout")
        hook_fastpath.record_attempt(self.tmp, "pretool", "timeout")
        hook_fastpath.record_attempt(self.tmp, "pretool", "ok")      # 清 streak
        left = hook_fastpath.record_attempt(self.tmp, "pretool", "timeout")
        self.assertEqual(left, 0, "ok 之后重新计数，不该立刻冷却")
        self.assertEqual(hook_fastpath.cooldown_remaining(self.tmp, "pretool"), 0)

    # F-4
    def test_F4_state_files_are_separate(self):
        a = hook_fastpath.hook_state_path(self.tmp, "posttool")
        b = hook_fastpath.hook_state_path(self.tmp, "pretool")
        self.assertNotEqual(a, b)
        hook_fastpath.record_attempt(self.tmp, "posttool", "timeout")
        self.assertTrue(a.exists())
        self.assertFalse(b.exists(), "不同 hook 的状态文件必须分开")


if __name__ == "__main__":
    unittest.main(verbosity=2)
