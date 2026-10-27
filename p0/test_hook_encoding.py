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

    # ---- 2026-10-26（T01/T05）：新入口的出口也必须过同一套判据 -------------
    def test_prompt_hook_emit_ascii(self):
        import fish_prompt_hook
        out = _capture(fish_prompt_hook._emit, "（fish 记忆：中文测试）")
        self._assert_ascii_and_chinese(out)

    def test_pretool_hook_decision_ascii_and_shape(self):
        """PreToolUse 走**决策**出口：纯 ASCII、eventName 对得上、reason 可还原。"""
        import fish_pretool_hook
        out = _capture(fish_pretool_hook._emit_decision, "别的会话正在写这块（中文测试）")
        self.assertTrue(out.strip(), "决策出口没有输出")
        self.assertTrue(out.isascii(), f"决策输出含非 ASCII 字节：{out!r}")
        payload = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(payload["hookEventName"], "PreToolUse",
                         "hookEventName 不对 → codec 会整块丢弃")
        self.assertEqual(payload["permissionDecision"], "ask")
        self.assertIn("中文测试", payload["permissionDecisionReason"])

    def test_emit_decision_rejects_unknown(self):
        """决策词表是封闭的：写错值必须**响亮失败**（不许静默丢一条决策）。"""
        with self.assertRaises(ValueError):
            hook_io.emit_decision("PreToolUse", "maybe", "x")

    def test_emit_decision_event_name_matters(self):
        """变异自证的反面：把 eventName 写成别的事件 → 解码出来的就是别的事件。

        这条判据不是在测 json.dumps，而是在钉 **"谁负责传对事件名"**：
        `emit_decision` 不替你猜，调用方（hook）传错就是错——
        所以 hooks 里 PreToolUse 的出口只允许写死 "PreToolUse"。
        """
        out = _capture(hook_io.emit_decision, "PostToolUse", "ask", "x")
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["hookEventName"],
                         "PostToolUse")
        import fish_pretool_hook
        source = (HERE / "fish_pretool_hook.py").read_text(encoding="utf-8")
        self.assertIn('hook_io.emit_decision("PreToolUse"', source,
                      "fish_pretool_hook 必须以 PreToolUse 发决策")


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
        """每个 hook 的出口都委托 hook_io（防止又写一份 json.dumps）。

        2026-10-26（T01）：出口现在有**两种**合法形态——
        `hook_io.emit(...)`（additionalContext）与
        `hook_io.emit_decision(...)`（PreToolUse 的 permissionDecision）。
        判据接受两者，但**必须至少有一个**：不写出口的 hook 要么是死的，
        要么在自己拼 JSON（那就一定会再踩一次编码坑）。
        """
        for p in sorted(HERE.glob("fish_*hook.py")):
            src = p.read_text(encoding="utf-8")
            self.assertTrue(("hook_io.emit(" in src) or ("hook_io.emit_decision(" in src),
                            f"{p.name} 的 stdout 出口没有委托 hook_io")

    def test_hooks_have_no_direct_stdout(self):
        """hook 里禁止任何直接 stdout 操作（必须经 hook_io 出口）。

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

    def test_decision_hooks_still_use_hook_io_for_context(self):
        """反向守护：决策型 hook 也不许绕过出口。

        `fish_pretool_hook` 只有决策出口（它不该谈 additionalContext），
        但 `fish_prompt_hook` 必须有上下文出口——否则提示词推送永远不显示。
        """
        prompt_src = (HERE / "fish_prompt_hook.py").read_text(encoding="utf-8")
        self.assertIn('hook_io.emit("UserPromptSubmit"', prompt_src)
        pretool_src = (HERE / "fish_pretool_hook.py").read_text(encoding="utf-8")
        self.assertIn("hook_io.emit_decision(", pretool_src)
        # 决策**字段名**只许出现在 hook_io 里（否则又有一份协议实现）。
        # 注意判据是带引号的键 `"permissionDecision"`：docstring 里提到它是允许的。
        self.assertNotIn('"permissionDecision"', pretool_src,
                         "决策字段只能出现在 hook_io 里（否则又有一份协议实现）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
