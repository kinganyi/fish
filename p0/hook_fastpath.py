#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hook 快通道（P0-⑥，外部审查 🔴3 / 2026-10-28 晚）——关闭态秒退 + 按 hook 身份隔离的冷却。

## 为什么独立成文件（不塞进 fish_common）

hook 每轮都要**起一个 Python 进程**，所以"启动成本"就是它的全部预算：
`import fish` 实测 1.3s，`fish_activity` 约 0.1s。本模块**只依赖 stdlib**，
在"功能已关"或"冷却中"时把 hook 的总耗时压到**十几毫秒**，慢路径（import fish +
真正干活）只在确实要做时才走。

## 三个函数

· `enabled(data_dir, key, default=True)` —— 读 config 单个键（sqlite3 `mode=ro`，~5ms）。
  **fail-open**：读不到库/键 → 返回 default（宁可多走一次慢路径，也不因读库失败
  悄悄关掉用户要的功能）。
· `record_attempt(data_dir, hook, status) -> int` —— 记一次尝试；返回剩余冷却秒数。
  **按 hook 名隔离**（`prompt` / `pretool` / `posttool` 各有自己的状态文件）——
  外部审查 🟠-H 指出旧实现"任意一个 hook 迟到就冷却全局 600s"，那是错的。
· `hook_state_path(data_dir, hook)` —— 状态文件路径（可诊断/可清理）。

## 失败范式

状态文件写不进去 → 只是少一层冷却，**不影响正确性**（不抛错、不打印）。
"""
import json
import sqlite3
import time
from pathlib import Path

COOLDOWN_AFTER_TIMEOUTS = 3      # 连续 N 次超时 → 冷却
COOLDOWN_SECONDS = 600           # 冷却时长（10 分钟）


def hook_state_path(data_dir, hook: str) -> Path:
    """每个 hook 自己的状态文件（互不拖累）。"""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(hook or "hook"))
    return Path(data_dir) / "_audit" / ("hook-fastpath-%s.json" % safe)


def _read_state(path: Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        pass
    return {}


def _write_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass                     # 写不进去 = 少一层冷却，不影响正确性


def enabled(data_dir, key: str, default: bool = True) -> bool:
    """`config` 里某个开关的**快速**判定（不 import fish、不起子进程）。"""
    try:
        db = Path(data_dir) / "index.sqlite"
        if not db.exists():
            return default
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return default
        return str(row[0]).strip().lower() in ("true", "1", "yes", "on")
    except Exception:            # noqa: BLE001 —— 读不到就走默认（fail-open）
        return default


def cooldown_remaining(data_dir, hook: str) -> int:
    """还剩多少秒冷却；0 = 不在冷却。"""
    state = _read_state(hook_state_path(data_dir, hook))
    try:
        until = float(state.get("cooldown_until") or 0)
    except (TypeError, ValueError):
        until = 0.0
    left = int(until - time.time())
    return left if left > 0 else 0


def record_attempt(data_dir, hook: str, status: str) -> int:
    """记一次尝试；返回**剩余冷却秒数**（>0 = 本次不要再走慢路径）。"""
    path = hook_state_path(data_dir, hook)
    state = _read_state(path)
    now = time.time()
    try:
        until = float(state.get("cooldown_until") or 0)
    except (TypeError, ValueError):
        until = 0.0
    if now < until:
        return int(until - now)
    try:
        streak = int(state.get("timeout_streak") or 0)
    except (TypeError, ValueError):
        streak = 0
    if status == "timeout":
        streak += 1
    elif status == "ok":
        streak = 0
    if streak >= COOLDOWN_AFTER_TIMEOUTS:
        _write_state(path, {"timeout_streak": 0,
                            "cooldown_until": now + COOLDOWN_SECONDS})
        return COOLDOWN_SECONDS
    _write_state(path, {"timeout_streak": streak, "cooldown_until": 0})
    return 0
