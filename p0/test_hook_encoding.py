#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hook stdout 编码纪律的守护测试（2026-10-25 根治轮）。

两个层面，缺一不可：

  A. **行为**：每个 hook 的真实出口函数跑一遍，断言输出**纯 ASCII**，
     且 JSON 解码后中文完好。判据不是"看起来对"，是字节层面：
     `ensure_ascii=False` 时中文字节会被宿主按错误编码解成乱码。

  B. **静态**：扫描所有 `fish_*hook.py`，禁止 stdout 形态的
     `ensure_ascii=False))`（文件日志形态 `...) + "\\n")` 允许）。
     B 是为了"新增 hook 又写一遍"——历史已经因为 4 份实现漂移过 3 次
     （2026-10-24 修 dsh hook，2026-10-25 push hook 又犯，其余两个同时漏）。
"""
import io
import json
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import hook_io  # noqa: E402


def _capture(fn, *args):
    old = sys.stdout
    buf = io.StringIO()
    sys.stdout = buf
    try:
        fn(*args)
    finally:
        sys.stdout = old
    return buf.getvalue()


class HookEncodingTest(unittest.TestCase):
    """A. 行为：真实出口函数 → 纯 ASCII + 中文可还原。"""

    def _assert_ascii_and_chinese(self, out: str) -> None:
        self.assertTrue(out.strip(), "出口函数没有输出")
        self.assertTrue(out.isascii(), f"输出含非 ASCII 字节：{out!r}")
        payload = json.loads(out)
        self.assertIn("hookSpecificOutput", payload)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("中文", ctx)
        self.assertIn("hookEventName", payload["hookSpecificOutput"])

    def test_hook_io_emit_ascii(self):
        out = _capture(hook_io.emit, "PostToolUse", "（fish 提示：中文测试）")
        self._assert_ascii_and_chinese(out)

    def test_push_hook_emit_ascii(self):
        import fish_push_hook
        out = _capture(fish_push_hook._emit, "（fish 提示：中文测试）")
        self._assert_ascii_and_chinese(out)

    def test_dsh_hook_emit_ascii(self):
        import fish_dsh_hook
        out = _capture(fish_dsh_hook._emit, "（中文测试：凭证已签发）")
        self._assert_ascii_and_chinese(out)

    def test_posttool_hook_emit_ascii(self):
        import fish_posttool_hook
        out = _capture(fish_posttool_hook._emit, "（大肥鱼生命系统提示：中文测试）")
        self._assert_ascii_and_chinese(out)

    def test_session_hook_emit_ascii(self):
        import fish_session_hook
        out = _capture(fish_session_hook._emit, "（大肥鱼生命系统注入：中文测试）")
        self._assert_ascii_and_chinese(out)


class HookStdoutScanTest(unittest.TestCase):
    """B. 静态：新增 hook 不许再把 stdout 写成 ensure_ascii=False。"""

    def test_no_stdout_ensure_ascii_false(self):
        bad = []
        for p in sorted(HERE.glob("fish_*hook.py")):
            src = p.read_text(encoding="utf-8")
            for m in re.finditer(r"ensure_ascii=False\)\)", src):
                line = src.count("\n", 0, m.start()) + 1
                bad.append(f"{p.name}:{line}")
        self.assertEqual(bad, [], f"stdout 出口不得 ensure_ascii=False：{bad}")

    def test_hooks_delegate_to_hook_io(self):
        """每个 hook 的出口都委托 hook_io（防止又写一份 json.dumps）。"""
        for p in sorted(HERE.glob("fish_*hook.py")):
            src = p.read_text(encoding="utf-8")
            self.assertIn("hook_io.emit", src,
                          f"{p.name} 的 stdout 出口没有委托 hook_io.emit")

    def test_hooks_have_no_direct_stdout(self):
        """hook 里禁止任何直接 stdout 操作（必须经 hook_io.emit）。

        补 2026-10-25 QA 独立验证发现的缺口：
        `sys.stdout.write(json.dumps(..., ensure_ascii=False) + "\\n")`
        这种形态不会被 `False))` 正则抓住，所以从「禁止 stdout 原语」一侧兜住。
        """
        banned = []
        for p in sorted(HERE.glob("fish_*hook.py")):
            src = p.read_text(encoding="utf-8")
            for m in re.finditer(r"sys\.stdout|\bprint\(", src):
                line = src.count("\n", 0, m.start()) + 1
                banned.append(f"{p.name}:{line}:{m.group(0)}")
        self.assertEqual(banned, [], f"hook 不得直接写 stdout：{banned}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
