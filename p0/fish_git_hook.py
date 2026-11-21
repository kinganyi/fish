#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PostToolUse 的 git 采集（共享核心 · 2026-11-21 下沉）。

## 为什么必须有这个模块（一次真实事故）

第一版把这段逻辑写在 `fish_push_hook.py` —— 那是 **DSH 专属脚本**。
结果是：同一个仓库、同一份采集规则，在 WorkBuddy 宿主上**根本不存在**。

实测症状（外部 AI 反馈报告的原话）：
> 同一台机器上 DSH 有 git 采集、WorkBuddy 没有；真提交完成后 fish 里依然
> 「没有这个仓库的记录」。

更刺眼的是 `is_git_commit()` 当时**已经**放在共享核心 `fish_activity` 里了——
**用它的代码却不在共享核心**。「共享逻辑只能写在共享核心里，宿主脚本只做触发」
这条纪律是我自己在 README 里反复写的，这次没做到。

## 依赖方向（重要）

`fish_git_hook → fish_git + fish_git_store + fish_surface`，
**不 import `fish_activity`** —— 那会形成
`fish_activity → fish_git_hook → fish_activity` 的循环。

所以会话键、工具名、payload 都由调用方（`fish_activity.handle_post_tool_use`）
准备好再传进来。这也让本模块能单独测：不需要造一个"活动采集"环境。

## 采集由 AI 的工具调用驱动（这是一条设计边界，不是缺陷）

下面三种提交**永远采不到**：人在终端里手动 `git commit`、用没挂 hook 的宿主提交、
在编辑器 GUI 里提交。消费方很容易误以为"提交了就会被记录"——所以写在这里。
"""
import sqlite3

import fish_git
import fish_git_link
import fish_git_store
import fish_surface
from fish_common import now_utc, warn_ignored

#: 同一个 checkout 两次 L1 采样之间的最小间隔（秒）。
#: 方案 §5.3 的节流要求：`status` 实测 P50 253ms / max 684ms，而 PostToolUse 是
#: **每次工具调用**都跑的。不节流的话，连续写 10 个文件就是 10 次 status，
#: 把 hook 的 3 秒预算吃光。
OBS_THROTTLE_SEC = 60


def _age_sec(stamp, now_text=None):
    """`stamp` 距 `now_text` 的秒数；解析不了返回 None（**不猜**）。

    为什么不复用 `fish_activity._age_sec`：那会形成循环依赖（见模块 docstring）。
    这里是十几行的时间差，复制一份比制造一个环形依赖划算得多。
    """
    from datetime import datetime, timezone
    if not stamp:
        return None
    try:
        a = stamp.replace("Z", "+00:00")
        b = (now_text or now_utc()).replace("Z", "+00:00")
        ta = datetime.fromisoformat(a)
        tb = datetime.fromisoformat(b)
        if ta.tzinfo is None:
            ta = ta.replace(tzinfo=timezone.utc)
        if tb.tzinfo is None:
            tb = tb.replace(tzinfo=timezone.utc)
        return (tb - ta).total_seconds()
    except (ValueError, TypeError):
        return None


def obs_throttled(conn, root, *, window_sec=OBS_THROTTLE_SEC, now=None):
    """同一个 checkout 在 `window_sec` 内采过就跳过。

    老库没有 git 表时返回 False（**照采**）：那是"还没建表"，不是"刚采过"——
    把它当成"刚采过"会让新库永远采不到。
    """
    norm = fish_surface.normalize_path(root)
    if not norm:
        return False
    try:
        row = conn.execute(
            "SELECT o.sampled_at FROM git_checkout c JOIN git_obs o "
            "ON o.checkout_id = c.checkout_id WHERE c.path_key=? "
            "ORDER BY o.sampled_at DESC LIMIT 1", (norm,)).fetchone()
    except sqlite3.Error as exc:
        warn_ignored("fish_git_hook.py obs_throttled 查不到 git 表", exc)
        return False
    if not row or not row[0]:
        return False
    age = _age_sec(row[0], now)
    if age is None:
        return False
    return 0 <= age < window_sec


def collect_git(conn, *, cwd, session_key, tool_name, tool_input,
                is_commit_cmd=None, first_path=None, dbg=None, now=None):
    """PostToolUse 的 git 采集：commit 事件（先）+ L1 观测（带节流）。

    返回诊断 dict（hook 用来打一行 debug）；**异常不向上抛**——旁路采集失败
    绝不能影响工具结果，也不能拖垮既有的活动采集。失败走 `dbg`，不静默。

    ## `is_commit_cmd` / `first_path` 为什么是参数而不是 import

    `fish_activity.is_git_commit` 与 `fish_activity._first_path` 是判断用的纯函数，
    但它们住在 `fish_activity` 里，而 `fish_activity` 要调本模块
    （`handle_post_tool_use` 是 PostToolUse 的唯一入口）。
    **本模块 import 它们就成环。**

    把判断函数当参数传进来，方向就干净了：`fish_activity → fish_git_hook`，单向。
    传空时用本模块的保守缺省（见下），所以本模块仍能单独测。

    ## 两件事的顺序是刻意的

    **commit 采集先做**：它是归因链唯一的确定性绑定（不靠时间窗口猜），
    不能被 60s 节流挡掉——否则「刚提交完立刻又提交」的第二次会丢。
    """
    say = dbg or (lambda _m: None)
    res = {"observed": None, "commit": None, "throttled": False, "error": None}
    if not cwd or not session_key:
        return res
    ti = tool_input if isinstance(tool_input, dict) else {}
    try:
        # ── ① commit 采集（归因链的锚，先于节流）─────────────────────────
        if is_commit_cmd is not None and is_commit_cmd(ti.get("command")):
            root = fish_git.discover_root(cwd)
            if root:
                # 先 observe 一次：确保 repo/checkout 存在，才能拿到 repo_id
                obs = fish_git_store.observe(conn, root)
                rid = obs.get("repo_id")
                if rid:
                    res["commit"] = fish_git_store.record_commit(
                        conn, root=root, repo_id=rid, session_key=session_key)
                    say("commit collect: %r" % (res["commit"],))
                    # ── ③ 提交 message 提到记忆编码 → `discussed_in` 关联 ──────
                    #
                    # 2026-11-21（用户裁定「接上」）：`fish_git_link` 原来有实现、
                    # 有 21 条判据、生产侧 0 个调用点——`git_link` 表永远 0 行。
                    # **判据绿着但能力不存在**，比 bug 更隐蔽。
                    #
                    # 只接「message 提到编码」这一种，因为它是 rel 四种关系里
                    # **唯一零猜测**的（implemented_by / produced / revised_in
                    # 都要语义判断）。详见 `fish_git_link.link_commit_mentions`。
                    c = res["commit"]
                    if c and c.get("recorded"):
                        res["linked"] = fish_git_link.link_commit_mentions(
                            conn, sha=c.get("sha"),
                            text="%s\n%s" % (c.get("subject") or "", c.get("body") or ""))
                        say("commit link: %r" % (res["linked"],))
                else:
                    say("commit collect: observe 没拿到 repo_id（%r）" % (obs,))

        # ── ② 写类动作 → L1 观测（带节流）────────────────────────────────
        if fish_surface.is_write_tool(tool_name) or fish_surface.is_bash_tool(tool_name):
            target = cwd
            raw_path = first_path(ti) if first_path is not None else ""
            if raw_path:
                target = fish_surface.make_abs(raw_path, cwd) or cwd
            root = fish_git.discover_root(target)
            if root:
                if obs_throttled(conn, root, now=now):
                    res["throttled"] = True
                    say("observe skipped (throttled): %s" % root)
                else:
                    res["observed"] = fish_git_store.observe(conn, root).get("state")
                    say("observe: %r" % (res["observed"],))
        # ⚠️ **必须提交**：`fish_git_store` 里一个 commit 都没有——它约定由调用方
        # 控制事务边界。而下沉时我把原实现里的这一行漏了，后果是：hook 里采集
        # "成功"（返回值 `observed: clean`），`conn.close()` 时全部回滚，
        # 库表里一条都没有。**这类错误最坏：日志说成功、数据不存在。**
        conn.commit()
        return res
    except Exception as exc:              # noqa: BLE001 —— 旁路失败绝不拖垮工具结果
        res["error"] = repr(exc)
        say("git collect failed: %r" % (exc,))
        return res
