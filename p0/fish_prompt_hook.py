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
    except OSError:
        pass                     # 状态写不进去 → 只是少一层节流，不影响正确性
    return {"pass": True, "reason": "ok", "digest": digest}


def ask_fish(raw: str, host: str, data_dir=None, *, timeout: int = 10) -> str:
    """把 payload 交给 `fish prompt-push`，返回它吐出的纯文本（失败返回空串）。"""
    if not FISH.exists():
        _dbg("找不到 fish.py")
        return ""
    cmd = [sys.executable, str(FISH), "--root", str(Path(data_dir or DATA)),
           "prompt-push", "--host", host]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(cmd, input=raw, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env,
                              timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        _dbg("prompt-push 失败：%r" % (exc,))
        return ""
    if proc.returncode != 0:
        _dbg("prompt-push exit=%s stderr=%s" % (proc.returncode, (proc.stderr or "")[:200]))
        return ""
    return (proc.stdout or "").strip()


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
    text = ask_fish(raw, host, DATA)
    if not text:
        return 0
    _emit(text)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:      # noqa: BLE001 —— 兜底：hook 永远不能影响用户消息
        sys.exit(0)
