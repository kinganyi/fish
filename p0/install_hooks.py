#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fish · hooks 配置安装器（**两个宿主一次装好**：DSH + WorkBuddy）。

## 为什么要有安装器（而不是手改 JSON）

hooks 配置是**启动时读一次**的进程级配置：写错了不会报错，只会"某个事件不再触发"，
而症状是"某个功能悄悄没了"——最难发现的那类。手改两份 JSON（其中 `settings.json`
有 1400 行）意味着：漏一个事件、把大小写写错、把 WorkBuddy 的其它键动了一个字节，
都会在**下一次重启**才暴露。

所以配置在这里被声明一次、校验一次、带备份地写一次：

    python install_hooks.py --check      # 只比对，不写盘；不一致 exit 2
    python install_hooks.py --apply      # 先备份，再**合并**写入两份配置
    python install_hooks.py --apply --force    # 整层替换（会删其它事件，**会列出来**）
    python install_hooks.py --restore latest   # 按备份**字节还原**
    python install_hooks.py --file <path> --host dsh --check   # 测试/演练用

## 四条硬校验（`--check` 每次都跑，不通过就 exit 2）

1. **两宿主各 4 个事件**：`SessionStart` / `UserPromptSubmit` / `PreToolUse` / `PostToolUse`；
2. **每宿主只有一个采集器**：DSH 用 `fish_push_hook.py`、WorkBuddy 用
   `fish_posttool_hook.py`。两个都注册 = 每次工具调用跑两个进程、写两份行、
   心跳互相覆盖（实测每次多 157ms，且统计翻倍）；
3. **写类 matcher 大小写齐备**：DSH 的工具名是小写（`write`/`edit`），WorkBuddy 是
   首字母大写（`Write`/`Edit`），所以两种写法都要出现在 matcher 里；
4. **matcher 只认纯字面备选**（`matcher_dialect`）——F1 的根因修复、Round 5/N1 收紧，见下。

## matcher 的宿主方言契约（F1 + N1：一个字符就能让整份配置作废）

DSH 的 `hooks` 解析器（出厂 asar 里的 `parseClaudeCodeConfig`）对 matcher 有两种分支：
**纯字面备选**（只含 `[A-Za-z0-9_]` 与 `|`，例如 `Write|write|Edit|edit`）走**字面比较**；
其余形态一律当**正则编译**。于是旧写法 `(?i)^(write|edit|…)$` 在 JS 里抛
`SyntaxError: Invalid group` → 整个 `apply()` 被 catch 后 `return` ——
**四个事件一个都不注册**（不是"某个事件不生效"，是**整份配置被拒**），
而 `--check` 当时只查"子串齐备"，照样 exit 0。

**Round 5 · N1 又收了一道**：旧判据还认"锚定正则"档，而那一档只查**字符集**、不查
**可编译性**（括号平衡 / 锚定配平）。`^(Write|write|Edit|edit$` 与
`^(Write|write|Edit|edit))$` 的字符集完全合法，交给宿主 parser 却是 `SyntaxError`
→ 同样整份配置被拒，而 `--check` 照样 exit 0。所以现在**只认宿主根本不编译正则的那一档**：

| 档 | 形态 | 例 |
|---|---|---|
| `literal` | `^[A-Za-z0-9_]+(\|[A-Za-z0-9_]+)*$`（**唯一**合法形态） | `Write\|write\|Edit\|edit` |

锚定 / 括号 / 内联 flag 等**一切正则形态一律拒绝**：两个宿主的正则方言不可假设，
同一个串不可能对两边都安全——**如需正则请人工确认宿主方言后再手改配置**。
判据一律用**原串**（不先 `strip()`）：宿主按原串解释，`" Write|write"` 这种前导空格
会让字面比较永不命中，所以它也必须红（只对空值/纯空白做判空）。

⚠️ 区分两种"宿主不认"（Y16 更正）：`(?i)` 在宿主 JS RegExp 里确实抛 `Invalid group`；
但 `(?i:…)` 在宿主 Node 24 里**可以编译**——拒绝它是**策略性**的（两宿主语义不可假设），
不是"宿主不认"。

## 事件支持 vs 配置被拒（F3：两种失败完全不同）

- **不支持的事件键**：宿主忽略那个键，其余事件照常（"配置即忽略"）；
- **非法 matcher**：整份配置被拒，**一个事件都不注册**——连既有的 SessionStart
  凭证注入与 PostToolUse 采集一起消失（生产静默停摆）。

所以部署后第一件事不是看新功能，而是**验证既有 SessionStart/PostToolUse 心跳仍在更新**；
排查顺序第一条永远是「matcher 是否满足宿主方言」——本工具 `--check` 已把它做成契约判据。

## 合并而不是整层替换（评审 L4）

`settings.json` 是**共用文件**：别的插件、别的人、别的工具都往同一个 `hooks` 层里挂
自己的事件（`Stop` / `Notification` / …）。上一版 `document["hooks"] = desired`
把整层换掉 → **静默删掉别人的事件**，症状是"某个功能悄悄没了"，正好是这套安装器
最该防的那类失败（它存在的理由就是"写错了不会报错"）。

现在缺省是**合并**：只覆盖/新增本工具负责的四个事件，其它事件与其它顶层键原样保留
（`merge_hooks`）。确实要整层替换时才用 `--force`，而且**必须把将被删除的事件列出来**
（`diff_hooks` / `cmd_apply` 都会打印），不允许"悄悄删"。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 宿主配置位置（可用环境变量覆盖，测试/演练必用 `--file`）。
DSH_HOME = Path(os.environ.get("DSH_HOME") or r"C:\Users\15596\.dsh")
WORKBUDDY_HOME = Path(os.environ.get("WORKBUDDY_HOME") or r"C:\Users\15596\.workbuddy")
TARGETS = {
    "dsh": lambda: DSH_HOME / "hooks.json",
    "workbuddy": lambda: WORKBUDDY_HOME / "settings.json",
}

# hooks 是"启动读一次"的：备份名带日期，便于 `--restore latest` 与人工排查。
BACKUP_SUFFIX = ".bak-20261026-hooks"
COLLECTOR_SCRIPTS = ("fish_push_hook.py", "fish_posttool_hook.py")
WRITE_TOOL_NAMES = ("write", "edit", "multiedit", "notebookedit", "apply_patch")
# 写类工具的**两种大小写写法**（DSH 小写、WorkBuddy 首字母大写）。
# 键是 DSH 的小写名，值是 WorkBuddy 的驼峰名——`apply_patch` 的驼峰不是
# `str.capitalize()`（那会得到 `Apply_patch`），所以这里**显式列表**，不靠推导。
WRITE_TOOL_CASED = (
    ("write", "Write"), ("edit", "Edit"), ("multiedit", "MultiEdit"),
    ("notebookedit", "NotebookEdit"), ("apply_patch", "ApplyPatch"),
)
# 字面备选串（两宿主的写法都写全）：**只含 `[A-Za-z0-9_]` 与 `|`** →
# DSH 走字面分支、根本不编译正则（评审 F1：这一形态就是修复本身）。
PRETOOL_MATCHER = "|".join(
    name for pair in WRITE_TOOL_CASED for name in (pair[1], pair[0]))
# 兼容别名：两个宿主现在用**同一串字面备选**（旧版 DSH 用 `(?i)` 正则，已判非法）。
DSH_PRETOOL_MATCHER = PRETOOL_MATCHER
WB_PRETOOL_MATCHER = PRETOOL_MATCHER
WB_POSTTOOL_MATCHER = ("Read|read|Bash|bash|Glob|glob|Grep|grep|"
                       "Write|write|Edit|edit|MultiEdit|multiedit|"
                       "NotebookEdit|notebookedit")
REQUIRED_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse")

# ── 宿主方言契约的两条判据（F1 + Round 5/N1）────────────────────────────────
# 纯字面备选：只允许字母数字下划线与 `|`——宿主按**字面比较**，不编译正则。
# ⚠️ 判据用 `fullmatch`（见 `matcher_dialect`）：`$` 会容忍结尾换行，`match` 会把
# `"write\n"` 判合法，而宿主按原串比较 → 永不命中。
LITERAL_ALTERNATIVES = re.compile(r"^[A-Za-z0-9_]+(?:\|[A-Za-z0-9_]+)*$")
# 内联 flag（`(?i)` / `(?i:…)` / `(?m)`…）：**策略性**拒绝，不是"宿主不认"。
# `(?i)` 在宿主 JS RegExp 里抛 Invalid group；`(?i:…)` 在 Node 24 里其实能编译（Y16）。
# 两个宿主的正则语义不可假设 → 本工具只认字面形态；正则请人工确认方言后再手改。
INLINE_FLAG = re.compile(r"\(\?[A-Za-z]+[:)]")
# 写类 matcher 必须覆盖的最小集合（大小写两种写法都要在）。
WRITE_MATCHER_REQUIRED = frozenset({"write", "Write", "edit", "Edit"})


def _p0(folder=None) -> Path:
    return Path(folder) if folder else HERE


def _fwd(path) -> str:
    """配置里的路径一律正斜杠（两个宿主都按字面执行，反斜杠要转义，正斜杠不用）。"""
    return str(path).replace("\\", "/")


def _quote_exe(python: str) -> str:
    """解释器路径：裸命令（`python`）原样，绝对路径一律加引号（路径里可能有空格）。"""
    py = str(python)
    if py.lower().endswith(".exe") or " " in py:
        return f'"{_fwd(py)}"'
    return py


def _hook(script: str, args: str, timeout: int, folder=None, python: str = "python") -> dict:
    cmd = f'{_quote_exe(python)} "{_fwd(_p0(folder) / script)}"'
    if args:
        cmd += " " + args
    return {"type": "command", "command": cmd, "timeout": int(timeout)}


def desired_hooks(host: str, *, folder=None, python: str = None) -> dict:
    """**唯一一份**目标配置（DSH / WorkBuddy 的差异全部收敛在这里）。"""
    host = (host or "").strip().lower()
    py = python if python is not None else ("python" if host == "dsh" else sys.executable)
    if host == "dsh":
        return {
            "SessionStart": [{"hooks": [_hook("fish_dsh_hook.py", "", 25, folder, py)]}],
            "UserPromptSubmit": [{"hooks": [
                _hook("fish_prompt_hook.py", "--host dsh", 10, folder, py)]}],
            "PreToolUse": [{"matcher": DSH_PRETOOL_MATCHER,
                            "hooks": [_hook("fish_pretool_hook.py", "--host dsh", 3,
                                            folder, py)]}],
            "PostToolUse": [{"hooks": [
                _hook("fish_push_hook.py", "--host dsh --collect-activity", 3, folder, py)]}],
        }
    if host == "workbuddy":
        return {
            "SessionStart": [{"hooks": [_hook("fish_session_hook.py", "", 15, folder, py)]}],
            "UserPromptSubmit": [{"hooks": [
                _hook("fish_prompt_hook.py", "--host workbuddy", 10, folder, py)]}],
            "PreToolUse": [{"matcher": WB_PRETOOL_MATCHER,
                            "hooks": [_hook("fish_pretool_hook.py", "--host workbuddy", 3,
                                            folder, py)]}],
            "PostToolUse": [{"matcher": WB_POSTTOOL_MATCHER,
                             "hooks": [_hook("fish_posttool_hook.py", "--host workbuddy", 3,
                                             folder, py)]}],
        }
    raise ValueError(f"未知宿主 {host!r}（可用：{', '.join(TARGETS)}）")


# ------------------------------------------------------------------ 校验


def _iter_commands(hooks: dict):
    """遍历配置里所有命令串（容错：结构坏了就当没有）。"""
    for event, groups in (hooks or {}).items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            for hook in group.get("hooks") or ():
                if isinstance(hook, dict) and isinstance(hook.get("command"), str):
                    yield event, group, hook["command"]


def collectors(hooks: dict) -> list:
    """采集器清单：`[(event, script)]`（用于"每宿主只有一个采集器"这条校验）。"""
    found = []
    for event, _group, command in _iter_commands(hooks):
        for script in COLLECTOR_SCRIPTS:
            if script in command:
                found.append((event, script))
    return found


def matcher_dialect(matcher) -> tuple:
    """matcher 是否命中**纯字面备选**契约 → `(kind, reason)`；不合法时 `kind == ""`。

    这是评审 F1 的修复本体、Round 5/N1 又收紧了一道：**合法性是形态问题，不是内容问题**。
    旧判据"字符串里含 write 与 edit"对 `(?i)^(write|edit)$` 恒绿，而那一串在宿主的
    JS RegExp 里直接 `SyntaxError` → 整份 `hooks` 配置被拒（四个事件全部不注册）。
    N1 再收掉"锚定正则"档：它只查字符集、不查可编译性，`^(Write|write|Edit|edit$`
    这种括号不平衡的串照样"合法"，而宿主 parser 同样 `SyntaxError`。

    返回的 `kind` 只有一档非空值（模块头有同一张表）：

      · `"literal"`         —— 纯字面备选，宿主**不编译正则**（**唯一**合法形态）；
      · `""`                —— 非法（`reason` 说明为什么不合法）。

    ⚠️ 判据一律用**原串**（不做 `strip()`）：宿主按原串解释，先 strip 再判 = 判据与
    宿主的实际行为不是同一件事。只对空值/纯空白判空。
    ⚠️ 不在这里"顺手修一下再判合法"：配置是**启动读一次**的，判据若会自我纠正，
    磁盘上的坏串就永远得不到修——而"看着有校验、其实放过了"正是上一版的问题。
    """
    raw = "" if matcher is None else str(matcher)
    if not raw.strip():
        return "", "matcher 为空（`null`/空串都算没写，但 PreToolUse 必须有）"
    if raw != raw.strip():
        return "", (f"matcher 首尾含空白（{raw!r}）——宿主按**原串**解释、不会替你 strip，"
                    "字面比较会永不命中")
    if LITERAL_ALTERNATIVES.fullmatch(raw):
        return "literal", ""
    inline = INLINE_FLAG.search(raw)
    if inline:
        return "", (f"含内联 flag {inline.group(0)!r}：本工具只认**纯字面备选**、"
                    "**策略性**拒绝一切正则形态（`(?i:…)` 在宿主 Node 24 里其实可编译，"
                    "但两个宿主的语义不可假设，所以不是'宿主不认'而是不做假设）——"
                    "如需正则请人工确认宿主方言后再手改配置")
    return "", ("不是纯字面备选（只允许 `[A-Za-z0-9_]` 与 `|`，如 `Write|write|Edit|edit`）；"
                "锚定/括号等正则形态一律拒绝——两个宿主的解释不可假设；"
                "如需正则请人工确认宿主方言后再手改配置")


def matcher_covers_write_tools(matcher) -> bool:
    """matcher 是否**同时**满足宿主方言契约 + 覆盖写类工具的大小写两种写法。

    两道门缺一不可（F1 + 旧版"大小写齐备"）：

      · **形态门**：`matcher_dialect(matcher)` 必须判为 `literal`（唯一合法形态）；
      · **覆盖门**：字面量集合里必须同时有 `write`/`Write`/`edit`/`Edit`
        （宿主工具名 DSH 是小写、WorkBuddy 首字母大写；少一种 = 那个宿主一次都不触发）。

    ⚠️ 覆盖门仍必须是"**字面量集合**"而不是"字符串含 `write`"——
    `"Write|Edit"` 也"含 write"（大小写不敏感的子串），那会让这条校验恒绿；
    而 `(?i)…` 现在连形态门都过不了（它正是 F1 的肇事串）。
    """
    kind, _reason = matcher_dialect(matcher)
    if not kind:
        return False
    literals = set(re.findall(r"[A-Za-z0-9_]+", str(matcher or "")))
    return WRITE_MATCHER_REQUIRED <= literals


def matcher_problems(hooks: dict, *, skip=()) -> list:
    """**所有**挂了 matcher 的事件组都要满足宿主方言契约（F1）。

    为什么扫全表而不是只看 PreToolUse：WorkBuddy 的 `PostToolUse` 也挂了 matcher
    （`Read|read|Bash|bash|…`）。一个事件组的 matcher 非法 → 整份配置被拒，
    所以"只校验 PreToolUse"永远抓不到"PostToolUse 写了一串坏正则"这种事故。
    `skip` 用于避开调用点已经单独判过的事件（避免同一条问题报两遍）。
    """
    problems = []
    for event in REQUIRED_EVENTS:
        if event in skip:
            continue
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        for index, group in enumerate(groups):
            if not isinstance(group, dict) or "matcher" not in group:
                continue          # 不写 matcher = 该事件全部触发（合法形态）
            kind, reason = matcher_dialect(group.get("matcher"))
            if not kind:
                problems.append(
                    f"{event}[{index}] matcher 不满足宿主方言契约"
                    f"（{reason}）：{group.get('matcher')!r}")
    return problems



def validate_hooks(host: str, hooks: dict, *, folder=None) -> list:
    """返回问题清单（空 = 通过）。`--check/--apply` 都跑同一份校验。

    ⚠️ 采集器计数只数**本工具负责的四个事件**（评审 L4 的连带修正）：
    合并语义下，别人的事件会被原样保留，而"别的插件也在 `Stop` 里挂一个 fish 脚本"
    并不是本安装器的错（那不会造成 PostToolUse 双采集）。把范围放大到全表会让
    `--check` 对**不该管的事**报错，进而诱导人去 `--force` 删别人的东西。
    """
    problems = []
    if not isinstance(hooks, dict):
        return ["hooks 不是对象"]
    for event in REQUIRED_EVENTS:
        if not isinstance(hooks.get(event), list) or not hooks.get(event):
            problems.append(f"缺事件 {event}")
    found = [item for item in collectors(hooks) if item[0] in REQUIRED_EVENTS]
    if len(found) != 1:
        detail = "、".join(f"{e}:{s}" for e, s in found) or "无"
        problems.append(f"采集器数量应为 1，实际 {len(found)}（{detail}）")
    pre = hooks.get("PreToolUse") or []
    if len(pre) != 1:
        problems.append(f"PreToolUse 应有 1 组，实际 {len(pre)}")
    else:
        matcher = pre[0].get("matcher") if isinstance(pre[0], dict) else None
        kind, reason = matcher_dialect(matcher)
        if not kind:
            # F1：形态判据**先于**覆盖判据——非法 matcher 会让整份配置被拒，
            # 而"覆盖了写类工具"在那种情况下毫无意义（一个事件都不会注册）。
            problems.append(
                f"PreToolUse matcher 不满足宿主方言契约（{reason}）：{matcher!r}"
                f"——宿主会当正则编译失败，**整份配置被拒**")
        elif not matcher_covers_write_tools(matcher):
            problems.append(f"PreToolUse matcher 未覆盖写类工具大小写：{matcher!r}")
        for _event, _group, command in _iter_commands({"PreToolUse": pre}):
            script = command.split('"')
            candidates = [part for part in script if part.endswith(".py")]
            for candidate in candidates:
                if not Path(candidate).exists():
                    problems.append(f"PreToolUse 脚本不存在：{candidate}")
            if not candidates:
                problems.append("PreToolUse 命令里没有 .py 脚本路径")
    # 其余事件组（WorkBuddy 的 PostToolUse 也挂 matcher）同样要过方言契约。
    problems.extend(matcher_problems(hooks, skip=("PreToolUse",)))
    base = _p0(folder)
    for script in ("fish_dsh_hook.py", "fish_session_hook.py", "fish_prompt_hook.py",
                   "fish_pretool_hook.py", "fish_push_hook.py", "fish_posttool_hook.py"):
        if not (base / script).exists():
            problems.append(f"缺脚本 {script}")
    return problems


def merge_hooks(document: dict, desired: dict, *, force: bool = False) -> dict:
    """把目标四个事件**合并**进现有文档，返回一份新文档（不改入参）。

    返回 `{document, preserved, dropped, shape}`：

      · `preserved` —— 本工具不负责、**原样保留**的事件名（排序后）；
      · `dropped`   —— `force=True` 时**真的会被删掉**的事件名（非 force 恒为空）；
      · `shape`     —— `"hooks"`（合并进 `hooks` 键）或 `"bare"`（顶层就是事件表）。

    为什么必须合并（评审 L4）：`hooks` 是共用层，整层替换会**静默删掉**别人的事件。
    为什么 `bare` 也要认：`hooks_of()` 说过 DSH 两种形态都认（`{hooks:{…}}` 与裸事件表），
    写侧只处理第一种，等于"读得懂、写坏了"。

    `force=True` 时才整层替换（`--force`），调用方**必须**把 `dropped` 打出来——
    删除其它事件是一次显式操作，允许，但不许悄悄发生。
    """
    doc = dict(document or {})
    if isinstance(doc.get("hooks"), dict):
        base, shape = dict(doc["hooks"]), "hooks"
    elif "hooks" not in doc and doc and all(isinstance(v, list) for v in doc.values()):
        # 裸事件表：顶层键就是事件名（`hooks_of()` 的读侧说过 DSH 两种都认）。
        # 判据是"**所有**顶层值都是列表"——那形状只可能是事件组；宽松到"有一个列表就算"
        # 会把 `settings.json` 里别的列表键（插件清单之类）误当成事件表。
        base, shape = {k: v for k, v in doc.items() if k not in REQUIRED_EVENTS}, "bare"
    else:
        base, shape = {}, "hooks"
    preserved = sorted(str(key) for key in base if key not in REQUIRED_EVENTS)
    dropped = list(preserved) if force else []
    if force:
        merged = dict(desired)
    else:
        merged = {k: v for k, v in base.items() if k not in REQUIRED_EVENTS}
        merged.update(desired)      # 只覆盖/新增四个事件，其它事件一个字节不动
    if shape == "hooks":
        doc["hooks"] = merged
    elif force:
        # 裸事件表下"整层替换"= 顶层只剩这四个事件（`dropped` 才与事实一致）
        doc = dict(merged)
    else:
        doc = merged
    return {"document": doc, "preserved": preserved, "dropped": dropped, "shape": shape}


def diff_hooks(desired: dict, actual: dict, *, force: bool = False) -> list:
    """把"目标 vs 现状"的差异说成人话（只比 `hooks` 这一层）。

    `force=False`（缺省）时其它事件是**保留**的，所以文案必须说"保持不变"；
    `force=True` 时它们会被删掉，文案必须改成"将被删除"并列出名字——文案与实际
    不一致就是这条评审意见本身（L4：`--check` 说保留、`--apply` 实际删了）。
    """
    lines = []
    actual = actual if isinstance(actual, dict) else {}
    for event in REQUIRED_EVENTS:
        want = desired.get(event)
        have = actual.get(event)
        if have is None:
            lines.append(f"✗ {event}：缺失")
        elif have != want:
            lines.append(f"✗ {event}：与目标不一致（会被改写）")
        else:
            lines.append(f"✓ {event}：一致")
    extra = [e for e in actual if e not in REQUIRED_EVENTS]
    if extra:
        if force:
            lines.append(f"✗ --force：其它事件将被**删除**：{', '.join(sorted(extra))}")
        else:
            lines.append(f"✓ 其它事件保持不变：{', '.join(sorted(extra))}")
    return lines


def config_matches(desired: dict, actual: dict, *, force: bool = False) -> bool:
    """现状是否**语义上**已等于目标（N3：`--check` 的退出码必须与 diff 一致）。

    为什么不能直接 `actual == desired`：`--check` 缺省是**合并**模式，`hooks` 层里
    别人的 `Stop`/`Notification` 会原样保留——它们的存在不是"不一致"，也不该让
    `--check` 变红（那会诱导人去 `--force` 删别人的东西）。

    `force=True` 时那些其它事件**会被删除**，所以它们的存在确实算不一致。
    判据与 `diff_hooks` 的 ✗/✓ 一一对应：凡是 diff 打了 ✗ 的，这里必须返回 False
    （旧版 `--check` 打印 ✗「会被改写」却 exit 0，结论还说"都与目标一致"——N3）。
    """
    actual = actual if isinstance(actual, dict) else {}
    if any(actual.get(event) != desired.get(event) for event in REQUIRED_EVENTS):
        return False
    if force and any(key not in REQUIRED_EVENTS for key in actual):
        return False
    return True


# ------------------------------------------------------------------ JSON 读写


def load_document(path: Path) -> dict:
    """读一份配置；不存在 → 空 dict（`--apply` 时按新文件创建）。

    BOM 容错：PowerShell 5.1 的 `Set-Content -Encoding utf8` 会写 BOM，
    而 `json.loads` 见到 BOM 直接报 `Unexpected UTF-8 BOM`。配置是给人维护的，
    不是所有编辑器都默认不带 BOM——读侧剥掉，写侧**原样保留**（见 `_dump_like`）。
    """
    if not path.exists():
        return {}
    text = _read_text(path).lstrip("\ufeff")
    if not text.strip():
        return {}
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} 的 JSON 顶层不是对象")
    return payload


def _read_text(path: Path) -> str:
    """按**原样**读文本（`newline=""` 保留 CRLF）。

    为什么必须保留行尾：Python 的文本模式默认做 universal-newline 转换（`\\r\\n` → `\\n`），
    `hooks.json` 是 CRLF——用默认模式读进来再写回去，整个文件的行尾就被换了，
    git/差异工具看到的是"整文件重写"。这是 2026-10-26 实测踩到的：apply 之后
    `hooks.json` 从 CRLF 变成 LF（内容对、风格变了），回滚时更难看清谁动了什么。

    用 `open(..., newline="")` 而不是 `Path.read_text(newline=)`：后者要 Python 3.13+。
    """
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def _write_text(path: Path, text: str) -> None:
    """按原样写文本（不动行尾），原子替换。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, path)


def hooks_of(document: dict) -> dict:
    """取 `hooks` 那一层（同时兼容 `{hooks: {...}}` 与"裸事件表"两种形态）。

    依据 `hook-protocol`/`hooks-claude-code/src/config.ts:82`：DSH 两种都认。
    """
    hooks = document.get("hooks")
    if isinstance(hooks, dict):
        return hooks
    return document


def _dump_like(payload: dict, original_text: str) -> str:
    """按**原文风格**序列化（缩进 2、不转义中文、保留行尾风格与 BOM）。

    为什么必须保留行尾：`hooks.json` 现在是 CRLF，`settings.json` 是 LF。
    统一成一种会让 diff 变成"整文件重写"，回滚时也分不清"谁动了什么"。
    BOM 同理：原文有就保留（否则 diff 首位会变），原文没有就不加。
    """
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if "\r\n" in (original_text or ""):
        text = text.replace("\n", "\r\n") + "\r\n"
    else:
        text += "\n"
    if (original_text or "").startswith("\ufeff"):
        text = "\ufeff" + text
    return text


def backup_path(path: Path) -> Path:
    """备份路径：`<file>.bak-20261026-hooks`；已存在则加 `.2` `.3`……**绝不覆盖旧备份**。"""
    first = path.with_name(path.name + BACKUP_SUFFIX)
    if not first.exists():
        return first
    index = 2
    while True:
        candidate = path.with_name(f"{path.name}{BACKUP_SUFFIX}.{index}")
        if not candidate.exists():
            return candidate
        index += 1


def find_backups(path: Path) -> list:
    """按后缀序号排序的备份清单（`latest` = 最后一个）。"""
    folder = path.parent
    if not folder.exists():
        return []
    stem = path.name + BACKUP_SUFFIX
    found = []
    for item in folder.iterdir():
        if not item.is_file():
            continue
        if item.name == stem:
            found.append((0, item))
        elif item.name.startswith(stem + "."):
            tail = item.name[len(stem) + 1:]
            if tail.isdigit():
                found.append((int(tail), item))
    found.sort(key=lambda pair: (pair[0], pair[1].name))
    return [item for _index, item in found]


def apply_host(host: str, path: Path, *, folder=None, dry_run=False,
               force=False) -> dict:
    """把目标配置写进一份文件（先备份；缺省**只合并**四个事件，见 `merge_hooks`）。"""
    original = _read_text(path) if path.exists() else ""
    document = load_document(path)
    desired = desired_hooks(host, folder=folder)
    merged = merge_hooks(document, desired, force=force)
    text = _dump_like(merged["document"], original)
    result = {"host": host, "path": str(path), "backup": None, "written": False,
              "changed": text != original,
              "preserved": merged["preserved"], "dropped": merged["dropped"],
              "shape": merged["shape"], "force": bool(force)}
    if dry_run:
        return result
    path.parent.mkdir(parents=True, exist_ok=True)
    if original and result["changed"]:
        # **只在真的要改时才备份**：内容没变还备份一份，会让 `--restore latest`
        # 指向"已经改过的那一版"，回滚点就失真了（2026-10-26 实测踩到）。
        target = backup_path(path)
        shutil.copy2(path, target)          # copy2：保留时间戳，便于事后核对
        result["backup"] = str(target)
        result["line_endings"] = "CRLF" if "\r\n" in original else "LF"
    _write_text(path, text)
    result["written"] = True
    return result


def restore_host(path: Path, which: str = "latest") -> dict:
    """按备份**字节还原**（`latest` 或显式文件名）。没有备份 → 报错，不猜。"""
    backups = find_backups(path)
    if not backups:
        raise FileNotFoundError(f"没有找到备份：{path.name}{BACKUP_SUFFIX}*")
    if which in ("latest", "", None):
        source = backups[-1]
    else:
        matches = [b for b in backups if b.name == which or b.name.endswith(which)]
        if not matches:
            raise FileNotFoundError(f"没有匹配的备份：{which}")
        source = matches[-1]
    payload = source.read_bytes()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, path)
    return {"path": str(path), "restored_from": str(source), "bytes": len(payload)}


# ------------------------------------------------------------------ CLI


def _target(host: str, file=None) -> Path:
    if file:
        return Path(file)
    return TARGETS[host]()


def _utf8_stdout() -> None:
    """把 stdout/stderr 切到 UTF-8（幂等、失败静默）。

    为什么 CLI 函数里也要做一遍：计划任务/无控制台环境下 stdout 是 GBK，
    `print("✓ …")` 会直接抛 `UnicodeEncodeError`（→ 退出码 1 的"假失败"）。
    `main()` 里做过一次，但 `cmd_check` 等函数也可能被测试/别的脚本直接调用。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:      # noqa: BLE001 —— 不可重配的流（如测试捕获对象）就保持原样
            pass


def cmd_check(hosts, file=None, folder=None, force=False) -> int:
    """只读比对；**校验失败或与目标不一致**都 exit 2（N3：退出码必须与 diff 一致）。

    「不一致」也算红，而不是只打印 ✗ 然后 exit 0：`--check` 是脚本/CI 的判据口，
    打印"会被改写"却返回 0 等于告诉调用方"没问题"——旧版就是这么自相矛盾的。
    """
    _utf8_stdout()
    bad = 0
    for host in hosts:
        path = _target(host, file)
        print(f"[{host}] {path}")
        try:
            document = load_document(path)
        except (ValueError, OSError) as exc:
            print(f"  ✗ 读不出来：{type(exc).__name__}: {exc}")
            bad += 1
            continue
        hooks = hooks_of(document)
        desired = desired_hooks(host, folder=folder)
        problems = validate_hooks(host, hooks, folder=folder)
        changed = not config_matches(desired, hooks, force=force)
        for line in diff_hooks(desired, hooks, force=force):
            print("  " + line)
        if problems:
            bad += 1
            for problem in problems:
                print(f"  ✗ {problem}")
        elif changed:
            bad += 1
            print("  ✗ 与目标不一致（会被改写）——形态合法，但 `--apply` 会写入这份差异")
        else:
            print("  ✓ 校验通过（4 事件 / 1 采集器 / matcher 方言契约 + 大小写齐备）")
    if bad:
        print(f"\n结论：{bad} 个宿主未通过 `--check`（校验失败，或与目标不一致）"
              "——跑 `python install_hooks.py --apply` 写入（**写完要重启宿主**）")
        return 2
    mode = "整层替换（--force：其它事件会被删除）" if force else "合并（其它事件保持不变）"
    print(f"\n结论：两个宿主都与目标一致（写入模式：{mode}；仍未重启则配置尚未生效）")
    return 0


def cmd_apply(hosts, file=None, folder=None, force=False) -> int:
    bad = 0
    for host in hosts:
        path = _target(host, file)
        try:
            result = apply_host(host, path, folder=folder, force=force)
        except (ValueError, OSError) as exc:
            print(f"[{host}] ✗ 写入失败：{type(exc).__name__}: {exc}")
            bad += 1
            continue
        action = "已写入" if result["written"] else "无需改动"
        print(f"[{host}] {action}：{result['path']}")
        if result["backup"]:
            print(f"  备份：{result['backup']}（回滚：--restore latest）")
        if result.get("dropped"):
            print(f"  ⚠ --force：已删除其它事件：{', '.join(result['dropped'])}")
        elif result.get("preserved"):
            print(f"  其它事件保持不变：{', '.join(result['preserved'])}")
        if not result["changed"]:
            print("  内容与目标一致（本次未备份：没有改动就没有新的回滚点）")
    if not bad:
        print("\n⚠ 配置是**启动读一次**：DSH 桌面端与 WorkBuddy 都需要重启才生效。"
              "\n  重启后先验证既有 SessionStart/PostToolUse 心跳仍在更新，再看新事件。")
    return 1 if bad else 0


def cmd_restore(hosts, which, file=None) -> int:
    """按备份字节还原，**还原后立刻校验目标文件**（N2：latest 可能是坏版本）。

    `latest` 只是"最近一次备份"，可能早于某次修复、甚至就是那次坏配置本身。
    字节还原是用户要的确定性动作，所以照做；但**必须响亮告知**"这份配置会被宿主
    整份拒绝"，并让退出码非 0——回滚不该静默地把系统退回到不工作状态。
    """
    _utf8_stdout()
    bad = 0
    for host in hosts:
        path = _target(host, file)
        try:
            result = restore_host(path, which)
        except (FileNotFoundError, OSError) as exc:
            print(f"[{host}] ✗ {exc}")
            bad += 1
            continue
        print(f"[{host}] 已还原：{result['path']} ← {result['restored_from']}"
              f"（{result['bytes']} B）")
        try:
            problems = validate_hooks(host, hooks_of(load_document(path)))
        except (ValueError, OSError) as exc:
            problems = [f"还原后的文件读不出来：{type(exc).__name__}: {exc}"]
        if not problems:
            print("  校验通过：四个事件与 matcher 契约成立")
            continue
        bad += 1
        for problem in problems:
            print(f"  ✗ {problem}")
        if any("方言契约" in problem for problem in problems):
            print("  ⚠ 还原后的配置会被宿主整份拒绝（matcher 不合契约 = 一个事件都不注册），"
                  "请手工确认（先跑 --check 再重启宿主）")
        else:
            print("  ⚠ 还原后的配置未通过校验，请手工确认（先跑 --check 再重启宿主）")
    if not bad:
        print("\n⚠ 回滚同样要在**重启宿主**之后才对运行时生效。")
    return 1 if bad else 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:      # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description="fish hooks 配置安装器（DSH + WorkBuddy）")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true",
                      help="只比对，不写盘；校验失败或与目标不一致 exit 2")
    mode.add_argument("--apply", action="store_true", help="备份后写入两份配置")
    mode.add_argument("--restore", nargs="?", const="latest", default=None,
                      metavar="WHICH",
                      help="按备份字节还原（缺省 latest；还原后会校验，坏版本非 0）")
    ap.add_argument("--host", default="all", choices=["all", "dsh", "workbuddy"],
                    help="只处理某个宿主（缺省 all）")
    ap.add_argument("--file", default=None, help="显式指定配置文件（测试/演练用）")
    ap.add_argument("--force", action="store_true",
                    help="整层替换 hooks（会删除其它事件；缺省为合并保留）")
    args = ap.parse_args(argv)
    hosts = ["dsh", "workbuddy"] if args.host == "all" else [args.host]
    if args.file and len(hosts) > 1:
        ap.error("--file 只能配一个宿主（配 --host dsh|workbuddy）")
    if args.check:
        return cmd_check(hosts, args.file, force=args.force)
    if args.apply:
        return cmd_apply(hosts, args.file, force=args.force)
    return cmd_restore(hosts, args.restore, args.file)


if __name__ == "__main__":
    sys.exit(main())
