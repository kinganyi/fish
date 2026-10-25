#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fish · hook stdout 统一出口（DSH / WorkBuddy / 未来宿主共用）。

## 为什么必须收敛到一处（2026-10-25 根治轮）

**机制（2026-10-25 QA 实测更正）**：Python 在 **Windows 管道**下
`sys.stdout.encoding = gbk`。`ensure_ascii=False` 时中文以 **GBK 字节**写出
（`D6 D0 CE C4`），而消费方（DSH / WorkBuddy）按 **UTF-8** 读入 → 乱码
（英文部分正常）。此前记录成「DSH 按非 UTF-8 解码」只说了半边；
完整机制是 **GBK 写出 + UTF-8 读入**。

同一个坑先后踩了三次：

- 2026-10-24 `fish_dsh_hook.py`（SessionStart）：修成 `ensure_ascii=True`；
- 2026-10-25 `fish_push_hook.py`（PostToolUse 推送）：**又写成 False**，
  用户真实看到了乱码推送；
- 同日排查发现 `fish_posttool_hook.py` / `fish_session_hook.py` 仍是 False。

根因不是"谁忘了"，是**同一个决定被 4 个 hook 各实现一遍**（4 份
`json.dumps(...)`，各自决定 `ensure_ascii`）。四份实现 = 迟早漂移。
收敛成这一个出口后，"编码"只剩一处决定，并由 `test_hook_encoding.py`
用行为测试 + 静态扫描双重守护。

## 纪律

1. 所有 hook 的 stdout 一律走 `emit()`；hook 里禁止直接
   `print(json.dumps({...hookSpecificOutput...}))`。
2. `ensure_ascii` **硬编码 True**：中文变成 `\\uXXXX` 转义、输出纯 ASCII，
   任何解码方式读都不会坏（这是跨宿主唯一安全的形态）。
3. 写**文件**日志不在约束内（文件 UTF-8 写、UTF-8 读，可保留
   `ensure_ascii=False` 便于人读）；测试用 `False))` 形态区分 stdout 与文件。

## 协议依据

`hookSpecificOutput` 包装（`additionalContext` 在内、
`hookEventName == 当前事件名`）来自 `checkout/packages/hooks/hook-protocol/
src/codec.ts:115-130`；两条缺一，宿主静默丢弃整块且不报错（见
`19-BUG报告-fish-DSH会话凭证接入-20261024.md` §三）。
"""
from __future__ import annotations

import json
import sys


def emit(hook_event_name: str, text: str) -> None:
    """输出 hook 协议要求的 `hookSpecificOutput`（ASCII 安全，带换行）。"""
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": hook_event_name,
            "additionalContext": text,
        }
    }, ensure_ascii=True) + "\n")
    sys.stdout.flush()
