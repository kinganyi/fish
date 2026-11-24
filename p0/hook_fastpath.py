#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hook 快通道（P0-⑥，外部审查 🔴3 / 2026-10-28 晚）——关闭态秒退 + 按 hook 身份隔离的冷却。

## 为什么独立成文件（不塞进 fish_common）

hook 每轮都要**起一个 Python 进程**，所以"启动成本"就是它的全部预算。
本机实测（Windows 11 / CPython 3.11，取 7 次最小值）：

    Python 解释器启动 + 常用 stdlib     ≈ 130–220 ms   ← 这是地板，任何 hook 都要付
    + `import hook_fastpath`             + 25 ms
    + `import fish_common`               + 24 ms
    + `import fish`                      + 130–430 ms（随负载波动）

所以本模块**只保留必要依赖**：`json` / `sqlite3` / `time` / `pathlib` 四个 stdlib，
外加 `fish_common` 的报警出口与 `fish_atomic` 的原子写（两者都是**纯叶子**：
零 fish import）。它**不是**纯 stdlib 模块——但它的两个调用方
（`fish_pretool_hook` / `fish_push_hook`）本来就都要 `fish_activity`，而那条链
一定先加载 `fish_common`，所以这里的 import 是**零边际成本**；为了"纯 stdlib"
再造一份 `warn_ignored`/原子写只会在本模块里多一份要同步维护的副本。

真正的预算节省来自**不 import `fish`**（差 130–430 ms，十倍以上），而不是省掉
一个 24 ms 的共用层。

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
from fish_common import warn_ignored
import fish_atomic          # 原子写正解（纯叶子：零 fish import，~1ms）
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


def _read_state_file(path: Path) -> dict:
    """读冷却状态；缺失/坏 JSON → `{}`（并 `warn_ignored`，不静默）。"""
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError) as exc:
        warn_ignored('hook_fastpath.py _read_state_file 冷却状态', exc)
    return {}


def _write_state_file(path: Path, state: dict) -> None:
    """原子写冷却状态（`fish_atomic`：同目录 tmp + fsync + replace）；失败只告警。

    2026-11-24（112 W-A · N-6）：原来是**直接 `write_text`**。对冷却状态来说，
    写到一半被杀会让"连续超时"计数归零 → 静默 fail-open（每轮都重走慢路径）。
    `fish_atomic` 是纯叶子（零 fish import），不违反本模块的性能契约。

    改名（112 W-D · C9）：旧名 `_write_state` 与 `fish_activity` 那份同名，
    但那份返回 bool、这份只告警——名字必须各自说清策略。
    """
    try:
        fish_atomic.write_text_atomic(path, json.dumps(state, ensure_ascii=False))
    except OSError as exc:
        warn_ignored('hook_fastpath.py _write_state_file 冷却状态', exc)


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
    state = _read_state_file(hook_state_path(data_dir, hook))
    try:
        until = float(state.get("cooldown_until") or 0)
    except (TypeError, ValueError):
        until = 0.0
    left = int(until - time.time())
    return left if left > 0 else 0


# 超过这个耗时就算"接近宿主超时"。宿主给 pretool/posttool 的 timeout 是 3 秒，
# 所以 1.5 秒已经是一半预算——连续 3 次（COOLDOWN_AFTER_TIMEOUTS）就该退避了。
SLOW_SECONDS = 1.5


def note_elapsed(data_dir, hook: str, started_at, *, slow_seconds=SLOW_SECONDS) -> int:
    """按**自测耗时**记一次尝试（`pretool` / `posttool` 专用）。

    为什么这里不能用"超时"这个事件：超时是**宿主杀进程**，hook 自己永远等不到那一刻，
    所以它唯一能提供的客观事实是"我这次跑了多久"。连续 N 次接近超时就冷却 N 分钟，
    下一轮直接秒退 —— 不让用户每次工具调用都等满宿主给的 3 秒。

    为什么 `prompt` 不走这条：prompt hook 自己起子进程、能直接拿到 `timeout` 状态，
    用 `record_attempt(..., status)` 更准（那边就是这么用的）。

    任何异常都返回 0（少一层冷却，不影响正确性）。
    """
    try:
        elapsed = time.time() - float(started_at)
    except (TypeError, ValueError):
        return 0
    return record_attempt(data_dir, hook,
                          "timeout" if elapsed >= float(slow_seconds) else "ok")


def record_attempt(data_dir, hook: str, status: str) -> int:
    """记一次尝试；返回**剩余冷却秒数**（>0 = 本次不要再走慢路径）。"""
    path = hook_state_path(data_dir, hook)
    state = _read_state_file(path)
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
        _write_state_file(path, {"timeout_streak": 0,
                                 "cooldown_until": now + COOLDOWN_SECONDS})
        return COOLDOWN_SECONDS
    _write_state_file(path, {"timeout_streak": streak, "cooldown_until": 0})
    return 0
