#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fish · PostToolUse hook（DSH 侧）：**采集动作 + 每 N 次推送一条候选**。

## 2026-10-26（hooks 深度利用 T02）改了什么

  1. **加采集**（`--collect-activity`，默认真）：每次工具调用先把写类动作落进
     `tool_activity`（只记路径/目录/参数摘要，**内容不落盘**），并在节流内刷新
     `presence.auto_*` 缓存与快照。采集**先于**频率闸——频率闸是为"推送"设计的，
     不该让"事实"跟着每 10 次才记一次。
  2. **心跳脱敏**（H1 修复）：原实现写 `raw[:200]`（payload 原文），
     可能把 `token=fsh_...` 与写入内容写进磁盘。现在只写
     事件/工具名/session 哈希/payload 长度——**心跳文件里不可能出现原文**。
  3. **`--host`**：会话键前缀由参数决定（DSH=`dsh`，WorkBuddy=`codebuddy`），
     分类/路径/推送全部走共享核心，不在脚本里再写一份。
  4. **合并成一次连接**：采集与推送本来要各开一次库，现在共用一个连接。
  5. **撞车提醒的递送口**（评审 L3）：PreToolUse 在 `warn` 模式下**不能输出决策**
     （那会变成阻塞/批准），只能留一个一次性标记；本脚本在同一次调用的收尾把那一句
     `additionalContext` 递出去——模型看得见、不阻塞、只一次。位置在频率闸**之前**：
     提醒与"每 N 次推送"是两回事，被节流挡住就等于没提醒。

## 三条纪律（原样保留）

  1. **失败静默**：工具结果是主流程，推送只是锦上添花；任何异常都零输出。
  2. **频率闸前置**（对推送而言）：不到点就返回，**连 fish 都不 import**。
  3. **无身份不猜**：查不到 `session_key` 对应的公民就不推。

配置（`~/.dsh/hooks.json`，由 `install_hooks.py` 生成）：
```json
"PostToolUse": [{"hooks": [{"type": "command",
  "command": "python \"…/fish_push_hook.py\" --host dsh --collect-activity",
  "timeout": 10}]}]
```
⚠️ hook 配置在宿主**启动时**装配 ⇒ 改完必须重启桌面端才生效。
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hook_io  # noqa: E402 —— 统一 stdout 出口（编码只在一处决定）
import fish_activity  # noqa: E402 —— 采集/心跳/计数的共享核心（不 import fish）

# 数据目录：默认本目录下的 data；可用 FISH_DATA 覆盖（测试隔离用）。
DATA = Path(os.environ.get("FISH_DATA") or (HERE / "data"))


def _dbg(msg: str) -> None:
    """诊断输出（仅 FISH_HOOK_DEBUG=1）。默认静默——生产环境不许往 stderr 写噪音。"""
    if os.environ.get("FISH_HOOK_DEBUG"):
        try:
            sys.stderr.write("[fish_push_hook] " + msg + "\n")
            sys.stderr.flush()
        except Exception:      # noqa: BLE001
            pass


def _emit(text: str) -> None:
    """输出 additionalContext（追加，不替换工具结果）。委托统一出口。"""
    hook_io.emit("PostToolUse", text)


def _argv_value(argv, flag, default=None):
    for i, arg in enumerate(argv):
        if arg == flag and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith(flag + "="):
            return arg.split("=", 1)[1]
    return default


def _host_from_argv(argv) -> str:
    return (_argv_value(argv, "--host", "dsh") or "dsh").strip().lower()


def collect_activity(payload, host):
    """采集一次（独立 try 边界：采集失败不能影响推送，反之亦然）。"""
    conn = None
    try:
        conn = fish_activity.connect(DATA)
        return fish_activity.handle_post_tool_use(
            conn, host=host, payload=payload, data_dir=DATA,
            snapshot_path=DATA / fish_activity.SNAPSHOT_RELPATH)
    except Exception as exc:           # noqa: BLE001 —— 采集失败静默（下一次继续）
        _dbg("collect failed: %r" % (exc,))
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:          # noqa: BLE001
                pass


def maybe_warn_pending(payload, host) -> None:
    """把 PreToolUse 欠的**撞车提醒**（`warn` 模式）递一句给模型（评审 L3）。

    与 WorkBuddy 侧同一个共享核心（`fish_activity.take_warn_pending`），只是
    DSH 的 PostToolUse 只跑这一个脚本——两宿主的可见性必须一致，否则
    "默认 warn"在 DSH 上等于没提醒。
    只递一次（读走即写 `delivered_at`）；过旧（>15 分钟）不递。
    """
    session_key = fish_activity.make_session_key(host, payload.get("session_id"))
    pending = fish_activity.take_warn_pending(DATA, session_key)
    if not pending:
        return
    text = fish_activity.warn_notice_text(pending)   # 措辞只有一份（共享核心）
    if text:
        _emit(text)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    host = _host_from_argv(argv)
    collect = "--no-collect-activity" not in argv
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:      # noqa: BLE001
        pass
    try:
        raw = sys.stdin.read()
    except Exception:      # noqa: BLE001
        return
    try:
        req = json.loads(raw) if raw.strip() else {}
    except Exception as exc:           # noqa: BLE001
        # 心跳只记长度与错误类型：**绝不写原文**（H1）。
        fish_activity.heartbeat(DATA, event="PostToolUse", host=host,
                                payload_len=len(raw), bad_payload=True)
        _dbg("bad payload: %r" % (exc,))
        return
    if not isinstance(req, dict):
        return

    session_id = str(req.get("session_id") or "")
    tool_name = str(req.get("tool_name") or "")
    # 心跳在最开头：把"没有输出"与"根本没被执行"分开（诊断 hook 是否跑过）。
    fish_activity.heartbeat(
        DATA, event="PostToolUse", host=host, tool_name=tool_name,
        session=fish_activity.make_session_key(host, session_id),
        payload_len=len(raw))
    if not session_id:
        return

    # ── ① 采集（先于频率闸：事实不该跟着推送节流）────────────────────
    if collect:
        collect_activity(req, host)

    # ── ①b 撞车提醒（`warn` 模式唯一可见通道；欠一句就递一句）─────────
    # 必须在频率闸**之前**：这条提醒与"每 N 次推送"是两回事，
    # 被推送节流挡住等于 warn 又变回"模型不可见"（评审 L3 的原症状）。
    try:
        maybe_warn_pending(req, host)
    except Exception as exc:      # noqa: BLE001 —— 提醒失败不影响后续推送
        _dbg("warn pending failed: %r" % (exc,))

    # ── ② 频率闸（前置）：先算"到没到点"，不到就别去加载 fish ──────────
    # 默认 10，可被 config 覆盖；但读 config 要连库，所以这里用**保守默认**，
    # 真正的值在 import 之后复核。两个值不一致时以库为准。
    n = fish_activity.bump_counter(DATA, session_id=session_id, event="posttool-push",
                                   every_n=10)
    if n < 0 or n % 10 != 0:      # 粗闸：先按 10 判，真正的 every_n 在下面复核
        return

    try:
        import fish
    except Exception as exc:       # noqa: BLE001
        _dbg("import fish failed: %r" % (exc,))
        return

    try:
        root = DATA
        conn = fish.connect(root)
        try:
            every_n = int(fish.get_config(conn, "push.after_tool_every_n_calls") or 10)
            if every_n <= 0 or n % every_n != 0:
                return
            if not fish.get_config(conn, "push.enabled"):
                return

            key = fish_activity.make_session_key(host, session_id)
            citizen_id = fish_activity.resolve_citizen(conn, key)
            if not citizen_id:
                return                     # 无身份不猜

            project = None
            try:
                cwd = req.get("cwd") or ""
                if cwd:
                    project = fish.resolve_project_by_cwd(conn, cwd)
            except Exception:              # noqa: BLE001
                project = None

            cands = fish.build_push_candidates(
                conn, root, citizen_id=citizen_id, project=project,
                session_key=key, limit=6, timing="tool_after")
            picked = fish.select_push_candidates(conn, cands, limit=1)
            if not picked:
                return
            c = picked[0]
            _emit(f"（fish 提示：{c['code']} {c['title']}（{c['reason']}）"
                  f"——工具后推送，每 {every_n} 次调用最多一条。）")
            # 记一条 push_log：工具后推送也要进度量（否则这类永远算不出采纳率）。
            # channel 必须是 `append_push_log` 白名单里的值（2026-10-25 实测：
            # 写成 after_tool 会被明确拒绝——有这个校验才没静默写脏数据）。
            try:
                fish.append_push_log(
                    conn, [c["code"]], channel="tool_after",
                    citizen_id=citizen_id, session_key=key,
                    candidate_id=c["code"],
                    budget_tokens=int(fish.get_config(conn, "push.after_tool_budget_tokens")))
            except Exception as exc:       # noqa: BLE001
                _dbg("append_push_log failed: %r" % (exc,))
        finally:
            conn.close()
    except Exception as exc:       # noqa: BLE001
        _dbg("push hook failed: %r" % (exc,))
        return                             # 失败静默：绝不影响工具调用


if __name__ == "__main__":
    try:
        main()
    except Exception:          # noqa: BLE001 —— 兜底：hook 永远不能影响工具结果
        pass
