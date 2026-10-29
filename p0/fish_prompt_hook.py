#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fish · UserPromptSubmit hook —— **按提示词递记忆**（DSH / WorkBuddy 共用，`--host` 区分）。

## 它做什么（三步，缺一不可）

  1. **快筛**（本进程，不 import fish）：prompt 太短 / 含 fish 注入标记 / 距上次推送
     不到 `push.prompt_min_interval_sec` / 与上次 prompt 完全相同 → **零输出返回**；
  2. **子进程**：`fish.py prompt-push --host <host>`，payload 从 stdin 原样透传
     （候选生成、相关性/陈旧性、24h 去重、`push_log` 记账全在 fish 里——
     **判定只有一份实现**，不在 hook 里再写一遍）；
  3. **出口**：子进程返回的非空纯文本走 `hook_io.emit("UserPromptSubmit", text)`。

## 为什么快筛放在 hook 而不是 fish 里

用户每条消息都会触发这个 hook。逐条付 400ms（`import fish`）+ 一次打分，
在"用户只是打个招呼"时是纯浪费；而 `push_log` 的 24h 去重与相关性门槛在
fish 侧仍然生效（双保险，不是二选一）。

## 失败静默（硬纪律）

子进程超时（10s）/ 非 0 / 空文本 / 任何异常 → **零输出、exit 0**。
对话永远不该因为"递记忆"这件事失败而受影响。

## 隐私

**原始 prompt 绝不落盘、绝不进日志**：状态文件里只有 prompt 的 sha256 前 16 位
与时间戳；心跳只有长度与 session 哈希（`fish_activity.heartbeat` 的 `_safe_fact`）。
"""
from fish_common import warn_ignored
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hook_io  # noqa: E402 —— 统一 stdout 出口（编码只在一处决定）
import fish_activity  # noqa: E402 —— 采集/配置/心跳的共享核心（不 import fish）

FISH = HERE / "fish.py"
# 数据目录：默认本目录下的 data；可用 FISH_DATA 覆盖（与其它 hook 同口径，测试可隔离）。
DATA = Path(os.environ.get("FISH_DATA") or (HERE / "data"))
MIN_PROMPT_CHARS = 4


def _dbg(msg: str) -> None:
    """诊断输出（仅 FISH_HOOK_DEBUG=1）。默认静默——生产不许往 stderr 写噪音。"""
    if os.environ.get("FISH_HOOK_DEBUG"):
        try:
            sys.stderr.write("[fish_prompt_hook] " + msg + "\n")
            sys.stderr.flush()
        except Exception:      # noqa: BLE001 —— 诊断绝不能影响主流程
            pass


def _emit(text: str) -> None:
    """输出 additionalContext（委托统一出口：编码与协议只在一处决定）。"""
    hook_io.emit("UserPromptSubmit", text)


def _host_from_argv(argv) -> str:
    for i, arg in enumerate(argv):
        if arg == "--host" and i + 1 < len(argv):
            return (argv[i + 1] or "").strip().lower() or "dsh"
        if arg.startswith("--host="):
            return (arg.split("=", 1)[1] or "").strip().lower() or "dsh"
    return "dsh"


def _session_hash(session_id: str) -> str:
    return hashlib.sha256((session_id or "").encode("utf-8")).hexdigest()[:12]


def prompt_gate(session_id: str, prompt: str, data_dir=None, *, now=None) -> dict:
    """频率闸 + 去重（**唯一**的 hook 侧门）。返回 `{"pass": bool, "reason": str}`。

    三重门：
      · 清洗后 < 4 字（"好"/"ok"/空）→ 不推：没有检索价值，只会白跑一次；
      · 含 fish 注入标记（会话开场注入块会被原样提交回来）→ 不推：那是**我们自己的话**；
      · 同一 prompt 的哈希重复，或距上次推送不到 `push.prompt_min_interval_sec`
        → 不推：连续两条消息之间塞两次记忆，读者只会跳过。
    """
    data_dir = Path(data_dir or DATA)
    text = (prompt or "").strip()
    if len(text) < MIN_PROMPT_CHARS:
        return {"pass": False, "reason": "prompt 太短"}
    lowered = text
    for marker in fish_activity.INJECTION_MARKERS:
        if marker in lowered:
            return {"pass": False, "reason": "prompt 是 fish 自己的注入块"}
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    interval = float(fish_activity.read_cfg(data_dir, "push.prompt_min_interval_sec"))
    stamp = float(now if now is not None else time.time())
    state_dir = data_dir / "_audit" / "hook-state"
    state_path = state_dir / f"prompt-{_session_hash(session_id) or 'anon'}.json"
    state = {}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if state.get("last_hash") == digest:
        return {"pass": False, "reason": "与上次 prompt 相同"}
    last = state.get("last_ts")
    if isinstance(last, (int, float)) and interval > 0 and (stamp - float(last)) < interval:
        return {"pass": False, "reason": f"距上次推送不到 {int(interval)}s"}
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        state.update({"last_hash": digest, "last_ts": stamp})
        tmp = state_path.with_name(state_path.name + ".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, state_path)
    except OSError as exc:
        warn_ignored('fish_prompt_hook.py:115 prompt_gate', exc)
    return {"pass": True, "reason": "ok", "digest": digest}


def ask_fish(raw: str, host: str, data_dir=None, *, timeout: int = 3):
    """把 payload 交给 `fish prompt-push`，返回 `(文本, 状态)`。

    状态：`ok` / `timeout` / `fail` / `skip`（找不到 fish.py）。

    **为什么 timeout 从 10 降到 3**（B 报告 BUG-1）：宿主对 UserPromptSubmit 的硬超时
    也是 10s，两者等值 → 子进程稍慢一步就被宿主杀掉，并把**用户整条 prompt** 判成
    block（记忆系统是旁路增强，不该有阻塞主流程的权力）。3s ≈ 稳态（1.5–2.1s）的
    1.5 倍余量；超时也只是"本次不推"（fail-open）。
    """
    if not FISH.exists():
        _dbg("找不到 fish.py")
        return "", "skip"
    cmd = [sys.executable, str(FISH), "--root", str(Path(data_dir or DATA)),
           "prompt-push", "--host", host]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(cmd, input=raw, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env,
                              timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _dbg("prompt-push 超时（%ss）：%r" % (timeout, exc))
        return "", "timeout"
    except OSError as exc:
        _dbg("prompt-push 失败：%r" % (exc,))
        return "", "fail"
    if proc.returncode != 0:
        _dbg("prompt-push exit=%s stderr=%s" % (proc.returncode, (proc.stderr or "")[:200]))
        return "", "fail"
    return (proc.stdout or "").strip(), "ok"


# ---------------------------------------------------- 连续超时降级（B 报告 BUG-1 的建议）
# 3 次连续超时 → 冷却 10 分钟（只心跳、不推送）。目的：在"磁盘忙/杀软扫描"的窗口里
# 不再反复撞超时（每次撞都可能把用户的一句话判成 block）。
COOLDOWN_AFTER_TIMEOUTS = 3
COOLDOWN_SECONDS = 600
_HOOK_STATE = "prompt_hook_state.json"


def _save_hook_state(path, state) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        warn_ignored('fish_prompt_hook.py:167 _save_hook_state', exc)


def _record_attempt(data_dir, status) -> int:
    """记一次尝试；返回剩余冷却秒数（>0 = 本次不推送，只心跳）。"""
    path = Path(data_dir or DATA) / "_audit" / _HOOK_STATE
    state = {}
    try:
        if path.exists():
            state = json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        state = {}
    now = time.time()
    until = float(state.get("cooldown_until") or 0)
    if now < until:
        return int(until - now)
    streak = int(state.get("timeout_streak") or 0)
    if status == "timeout":
        streak += 1
    elif status == "ok":
        streak = 0
    if streak >= COOLDOWN_AFTER_TIMEOUTS:
        _save_hook_state(path, {"timeout_streak": 0,
                                "cooldown_until": now + COOLDOWN_SECONDS})
        return COOLDOWN_SECONDS
    _save_hook_state(path, {"timeout_streak": streak, "cooldown_until": 0})
    return 0


def _prompt_enabled(data_dir=None) -> bool:
    """`push.prompt_enabled` 的**快速**检查（不 import fish、不起子进程）。

    为什么要有它（2026-10-28 生产事故）：关闭态下每次 hook 仍会起 `fish prompt-push`
    （第二个 Python + import fish ≈ 2.7s 起步）；高峰期（并发 / 杀软扫描 / 磁盘忙）
    这 2.7s 会被推到 10s 超时线——前端AI 在 anyi-studio 连续撞到的 `Hook timed out`
    就是这个。功能已关时必须**秒退**。

    fail-open：读不到库 / 读不到键 → 按"开启"处理（宁可多花一次子进程，也不因为
    读库失败而悄悄关掉用户要的功能）。
    """
    try:
        import sqlite3
        db = Path(data_dir or DATA) / "index.sqlite"
        if not db.exists():
            return True
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            row = conn.execute(
                "SELECT value FROM config WHERE key='push.prompt_enabled'").fetchone()
        finally:
            conn.close()
        if row is None:
            return True
        return str(row[0]).strip().lower() in ("true", "1", "yes", "on")
    except Exception:      # noqa: BLE001 —— 读不到就按开启（fail-open 到旧行为）
        return True


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    host = _host_from_argv(argv)
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:      # noqa: BLE001 —— 老 Python / 非标准流时保持默认
        pass
    try:
        raw = sys.stdin.read()
    except Exception:      # noqa: BLE001 —— 读不到 stdin 就当没发生
        return 0

    session_id = ""
    prompt = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
        if isinstance(payload, dict):
            session_id = str(payload.get("session_id") or "")
            prompt = str(payload.get("prompt") or "")
    except ValueError:
        fish_activity.heartbeat(DATA, event="UserPromptSubmit", host=host, bad_payload=True)
        return 0

    fish_activity.heartbeat(DATA, event="UserPromptSubmit", host=host,
                            session=_session_hash(session_id), prompt_len=len(prompt))

    gate = prompt_gate(session_id, prompt, DATA)
    if not gate["pass"]:
        _dbg("gate: %s" % gate["reason"])
        return 0
    # 关闭态秒退（2026-10-28 事故修复）：不 subprocess、不 import fish
    if not _prompt_enabled(DATA):
        _dbg("push.prompt_enabled=false → 秒退")
        return 0
    text, status = ask_fish(raw, host, DATA)
    cooldown = _record_attempt(DATA, status)
    if cooldown:
        _dbg("连续超时降级：冷却 %ds（只心跳，不推送）" % cooldown)
        return 0
    if not text:
        return 0
    _emit(text)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:      # noqa: BLE001 —— 兜底：hook 永远不能影响用户消息
        sys.exit(0)
