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
import os
from pathlib import Path
import sys

# 决策词表（`hook-protocol/src/codec.ts:97-133` 只认这三个值）。
# **fish 本批只用 `ask`**：凭证 deny 与危险命令 ask 由宿主机制负责（用户 2026-10-26
# 拍板不做重复造轮子）；`allow/deny` 留在词表里只是为了这个出口不撒谎——
# 若将来真要写，也走同一个出口，而不是又冒一份 json.dumps。
DECISIONS = ("allow", "deny", "ask")


def emit(hook_event_name: str, text: str) -> None:
    """输出 hook 协议要求的 `hookSpecificOutput`（ASCII 安全，带换行）。"""
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": hook_event_name,
            "additionalContext": text,
        }
    }, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def emit_decision(hook_event_name: str, decision: str, reason: str) -> None:
    """输出 PreToolUse 的**决策**块（`permissionDecision` + 可读原因）。

    三条硬要求（都来自实测，不是风格）：

      1. `hookEventName` 必须与当前事件一致，否则 `codec.ts` 会**整块丢弃且不报错**
         ——表现是"hook 明明输出了，宿主像没收到一样"；
      2. `permissionDecision` 只认 `allow/deny/ask`，别的值一律被忽略；
      3. `ensure_ascii=True`：跨宿主唯一安全形态（GBK 写出 + UTF-8 读入会乱码）。

    `reason` 会直接显示给人看，所以**不许含公民 ULID、不许含文件内容**。
    """
    if decision not in DECISIONS:
        raise ValueError(f"未知决策 decision={decision!r}（可用：{', '.join(DECISIONS)}）")
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": hook_event_name,
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }, ensure_ascii=True) + "\n")
    sys.stdout.flush()


# **DATA 必须在本模块定义**（第九轮审查 🟠，2026-11-20）。
#
# 我把三份逐字相同的 `log_child_stderr` 搬进来时，只搬了函数体，没搬它依赖的
# 模块级 `DATA`——于是这个函数**在 hook_io 里永远 NameError**，被内部的
# `except Exception` 吞掉，只写一行 stderr。三个 hook 都 import 它，
# 等于子进程 stderr 从此**静默丢失**。
#
# 口径与两个 hook 原来的写法一致：`FISH_DATA` 环境变量优先，否则本文件旁边的 `data`。
# hook_io.py 在 `p0/` 下，所以 `HERE/"data"` 与 hook 自己的 `DATA` 是同一个目录。
HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("FISH_DATA") or (HERE / "data"))


# ---------------------------------------------------------------- hook 共用原语
#
# 2026-11-24（112 W-A · B3/B4）：`--host` 解析与诊断输出原来是**四份副本**。
# 四份 `_host_from_argv` 逐行相同、只有缺省值不同；四份 `_dbg` 只有前缀不同。
# 它们和 2026-10-25 收掉的那四份 `json.dumps` 是同一类残留：同一个决定各实现一遍。
#
# 合并后机制只在这里，**缺省值由调用点显式传**（`host_from_argv(argv, DEFAULT_HOST)`）。
# 不在这里给隐式缺省：隐式缺省正是行为分叉最容易藏进来的地方——112 §七对 C-A2 的
# 自评就是"把四个默认值统一成 dsh，再用四个都等于 dsh 当断言"会全绿的假绿。


def host_from_argv(argv, default: str) -> str:
    """从 hook 命令行取 `--host` 的值（去空白 + 小写）；没有/空值 → 调用点给的 `default`。

    `default` **必须由调用点显式传**，而且要用该 hook 的**历史缺省值**：

      · `fish_posttool_hook` → `workbuddy`（WorkBuddy 的 PostToolUse 入口）；
      · `fish_pretool_hook` / `fish_prompt_hook` / `fish_push_hook` → `dsh`。

    四个宿主命令行都显式带 `--host`（`fish_hosts.HOSTS` 的 hooks 配置逐条写着），
    所以默认只是兜底。但兜底也要保历史值：同一事件（PostToolUse）两个 hook 在
    无 `--host` 时若记成两个宿主身份，会话键与在场记录会分叉成两份（112 §三 B3）。
    """
    args = list(argv or ())
    for i, arg in enumerate(args):
        if arg == "--host" and i + 1 < len(args):
            return (args[i + 1] or "").strip().lower() or default
        if arg.startswith("--host="):
            return (arg.split("=", 1)[1] or "").strip().lower() or default
    return default


def dbg(tag: str, msg: str) -> None:
    """诊断输出（仅 `FISH_HOOK_DEBUG` 有值时）；默认静默——生产不许往 stderr 写噪音。

    `tag` 是四个 hook 之间**唯一**的差异（旧的四份 `_dbg` 只有这个前缀不同）。
    调用点用 `functools.partial(hook_io.dbg, "<hook 名>")` 绑一次，
    tag 就只剩一处定义，而 23 个调用点的字节一个都不用动。
    """
    if not os.environ.get("FISH_HOOK_DEBUG"):
        return
    try:
        sys.stderr.write("[" + tag + "] " + msg + "\n")
        sys.stderr.flush()
    except Exception:      # noqa: BLE001 —— 诊断绝不能影响主流程
        pass


def log_child_stderr(tag, text):
    """子进程 stderr 落盘（hook 里 stdout 是契约面，不能污染；但也不能丢）。"""
    if not (text or "").strip():
        return
    try:
        import datetime
        d = DATA / "_audit"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "hook-stderr.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                "tag": tag,
                "stderr": text[-4000:],
            }, ensure_ascii=False) + "\n")
    except Exception as exc:       # noqa: BLE001
        sys.stderr.write(f"[hook] hook-stderr 落盘失败（已忽略）：{exc!r}\n")
