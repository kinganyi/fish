#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fish · 动作采集 / 聚合 / 快照 / 撞车判定（stdlib only，**不 import fish**）。

## 位置与边界

`import fish` 约 400ms、Python 进程启动约 157ms，而 PostToolUse 是**每次工具
调用**都要跑的。所以采集层是一个独立小模块：只依赖 `sqlite3/json/hashlib/re/
datetime/os` + 两个纯函数模块（`fish_surface`/`fish_naming`），import ~5ms。

**共享核心**：分类、路径归一、聚合、快照、撞车全在这里，`fish.py`（看板/推送/
CLI）与两个 hook（Pre/Post）都调这里——不在任何一处再写第二份判定。

## 事实 vs 缓存（这条不许含糊）

  · **真相** = `tool_activity` 表（只记写类；只记路径/目录/摘要，**不记内容**）。
  · **缓存** = `presence.auto_surface/auto_seen_at/auto_source`（让外部读者与
    注入包能一次取到"最近在动哪"，减少回表）。渲染、推送、撞车判定一律以
    事实表读时聚合为准；缓存只给"顺手看一眼"的场景。
    否则「声明 vs 实际」又变成两份会漂的真相。

  · **聚合的判定列只来自事实**（评审 L1）：`auto_surfaces` 同时给 `dir`（展示，人的基准）
    与 `dir_abs`（判定，`dir_of(path_abs)`）；快照里的 `dir_abs` 只取后者。

  · **提醒通道**（评审 L3）：`warn` 模式**不输出决策**（不阻塞），靠
    `_audit/hooks/warn-pending-<会话哈希>.json` 让**下一次 PostToolUse** 递一句
    `additionalContext`——模型看得见、不阻塞、只一次、可审计。

## 隐私红线（用户 2026-10-26 拍板 + 设计 §10.3）

  · 落库字段只有：host / session_key / citizen_id / project / tool_name / action /
    path / path_dir / path_abs / args_digest / ok / seen_at；
  · `args_digest` 的输入**只有元数据**（工具名、路径、动作、参数长度、命中的
    Bash 特征名）——**不含任何内容片段**，所以它不能反推内容；
  · 心跳/审计只写：事件、工具名、session 哈希、长度、决策。绝不写
    content / old_string / command / prompt / token 原文。

## 失败策略

本模块的函数**照常抛异常**（可测）；「静默」只在 hook 入口层做。唯一的例外是
几处"增强项"（读配置、读快照、写心跳），它们按设计必须在无库/无文件时给出
可用的默认值，而不是把 hook 打挂。
"""
from __future__ import annotations
from fish_common import warn_ignored

import datetime
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

import fish_naming
import fish_surface

# ── 宿主语汇（会话键前缀与现有 `fish_session_hook`/`mcp_server` 同口径）─────────
HOST_KEY_PREFIX = {"dsh": "dsh", "workbuddy": "codebuddy", "codebuddy": "codebuddy"}

# ── 动作词表：本批只落 write / exec ──────────────────────────────────────────
# read/other 保留在词表里只是**文档**（设计 §4.1）：读不改变任何东西，
# 落库只会撑爆看板与表（EVAL 建议 1）。所以本批不写这两个值——不许编数据。
ACTIONS = ("write", "exec", "read", "other")
RECORDED_ACTIONS = ("write", "exec")

# ── 高频判别阈值（与设计 §6.1 表一致）────────────────────────────────────────
NOISE_REDIRECT_TARGETS = ("/dev/null", "nul", "/dev/tty")

# ── 配置默认值（**必须与 fish.CONFIG_DEFAULTS 一致**，由 test_activity 逐键对账）──
# 为什么这里还要有一份：hook 快路径不许 import fish，但若各写一套默认值，
# 「库里没配」与「代码默认」就会漂（关闭开关时不生效最难查）。
# 判据把两份表钉在一起：任一侧改动而另一侧没跟上 → 测试红。
CONFIG_FALLBACK = {
    "activity.enabled": True,
    "activity.retention_days": 14,
    "activity.aggregate_window_min": 30,
    "activity.snapshot_min_interval_sec": 20,
    "activity.snapshot_max_age_sec": 3600,
    "activity.presence_cache_min_interval_sec": 20,
    "board.active_window_sec": 300,
    "board.collision_window_min": 30,
    "board.collision_ask": "warn",
    "board.collision_ask_cooldown_sec": 60,
    "board.collision_min_segments": 2,
    "board.overlap_window_min": 90,
    "push.prompt_min_interval_sec": 90,
}

# 撤回「提示词里含 fish 注入标记」的判据（与 fish.clean_prompt 共用同一份）。
# 放在这里而不是 fish.py：prompt hook 必须在**不 import fish** 的前提下先筛一遍，
# 否则每条用户消息都要付 400ms 去发现"这条是注入块"。
INJECTION_MARKERS = ("fsh_", "【当前状态", "[fish]", "登记身份")

SNAPSHOT_RELPATH = Path("_audit") / "hooks" / "active_surfaces.json"
# 快照条目硬上限（设计 45 §4.4 / R5）：PreToolUse 每次写之前都要读它，
# 行数从"公民数"涨到"会话数"之后必须有个闸；截断条数写进 `snapshot["truncated"]`。
SNAPSHOT_MAX_ENTRIES = 200
ASKS_RELPATH = Path("_audit") / "hooks" / "pretool-asks.jsonl"
HEARTBEAT_RELDIR = Path("_audit") / "hooks"
# 撞车提醒的「一次性 pending 标记」文件名前缀与最大年龄（评审 L3）。
# 15 分钟：再老的提醒只会是噪音——上下文早翻篇了，迟到的一句"别人在写这块"
# 只会让人怀疑系统在乱说。
WARN_PENDING_PREFIX = "warn-pending-"
WARN_PENDING_MAX_AGE_SEC = 900


# ------------------------------------------------------------------ 时间


def utc_now() -> str:
    """UTC ISO（`%Y-%m-%dT%H:%M:%SZ`）——与 fish 同口径，定长字符串可比大小。"""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(text):
    """解析 UTC ISO；失败返回 None（**不抛**：坏时间戳不该让采集整体失败）。"""
    if not text:
        return None
    try:
        return datetime.datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def _dt(text=None):
    return parse_utc(text) or datetime.datetime.now(datetime.timezone.utc)


def _age_sec(ts, now_text=None) -> float:
    """`ts` 距 `now_text`（缺省=现在）的秒数；解析不了返回一个很大的数。"""
    start = parse_utc(ts)
    if start is None:
        return float("inf")
    return ( _dt(now_text) - start).total_seconds()


def local_period_key(now_text=None) -> str:
    """今天是哪块看板（`YYMMDD`，**本地日期**）——与 `fish.board_period` 同口径。"""
    now_utc = parse_utc(now_text) if now_text else None
    if now_utc is None:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
    local = now_utc.astimezone(datetime.timezone(datetime.timedelta(hours=8)))
    return local.strftime("%y%m%d")


# ------------------------------------------------------------------ 配置


def _coerce(raw, default):
    """把 config 表里的 TEXT 值按默认值的类型转回来；转不了就用默认值。"""
    if raw is None:
        return default
    if isinstance(default, bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(str(raw).strip())
        except (TypeError, ValueError):
            return default
    return str(raw)


def get_cfg(conn, key, default=None):
    """从已开的连接读一个配置键（缺库/缺键/坏值 → 返回 `CONFIG_FALLBACK` 或 default）。"""
    fallback = CONFIG_FALLBACK.get(key) if default is None else default
    try:
        row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    except sqlite3.Error:
        return fallback
    if not row:
        return fallback
    return _coerce(row[0], fallback)


def read_cfg(data_dir, key, default=None):
    """**只读**打开 data 目录读一个配置键（hook 快路径用；无库 → 默认值）。

    只读连接是刻意的（`mode=ro`）：读配置不该有任何写副作用，而且这样在
    「库正被别的进程独占」时也不会卡住 hook。
    """
    fallback = CONFIG_FALLBACK.get(key) if default is None else default
    db = Path(data_dir) / "index.sqlite"
    if not db.exists():
        return fallback
    try:
        conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return fallback
    try:
        return get_cfg(conn, key, fallback)
    except sqlite3.Error:
        return fallback
    finally:
        conn.close()


# ------------------------------------------------------------------ 连接


def connect(data_dir):
    """采集用的连接：**WAL + 短 busy_timeout**（hook 不允许长时间等锁）。"""
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(root / "index.sqlite"), timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    return conn


def make_session_key(host, session_id) -> str:
    """`<host 前缀>:<session_id>`；session_id 为空返回空串（不编一个会话）。"""
    sid = str(session_id or "").strip()
    if not sid:
        return ""
    prefix = HOST_KEY_PREFIX.get((host or "").strip().lower(), (host or "").strip().lower() or "unknown")
    return f"{prefix}:{sid}"


# ------------------------------------------------------------------ 身份 / 项目
# ⚠️ 下面两条 SQL 是本模块**唯一**的 citizens 读点，由 `test_activity` 逐条冻结
#    （设计 T02 §7：新模块自己的 citizens 查询不许悄悄增删）。查不到就是 None，
#    「不知道是谁」不等于「谁都可以」。
SQL_SESSION_TOKEN_CITIZEN = (
    "SELECT citizen_id FROM session_tokens WHERE session_key=? AND revoked_at IS NULL "
    "AND (expires_at IS NULL OR expires_at > ?) ORDER BY issued_at DESC LIMIT 1")
SQL_HOST_SESSION_CITIZEN = (
    "SELECT citizen_id FROM citizens_all WHERE host_session_key=? AND status='active' "
    "ORDER BY created_at LIMIT 1")


def resolve_citizen(conn, session_key, now=None):
    """会话键 → 公民；查不到返回 None（**不猜**）。"""
    if not session_key:
        return None
    ts = now or utc_now()
    try:
        row = conn.execute(SQL_SESSION_TOKEN_CITIZEN, (session_key, ts)).fetchone()
        if row and row[0]:
            return row[0]
        row = conn.execute(SQL_HOST_SESSION_CITIZEN, (session_key,)).fetchone()
        return row[0] if row and row[0] else None
    except sqlite3.Error:
        return None


def project_pairs(conn) -> list:
    """`workspace_map` 的 `(前缀, 项目)` 列表（表不存在时给空列表）。"""
    try:
        return [(r[0], r[1]) for r in conn.execute(
            "SELECT path_prefix, project FROM workspace_map").fetchall()]
    except sqlite3.Error:
        return []


def resolve_project(conn, path_abs, cwd=None):
    """解析归属项目，返回 `(project, project_root)`；都没有 `(None, None)`。

    **先按写入目标路径、再按 cwd**（设计 §4.1）：DSH 会话的 cwd 常常是
    `workspace/learn` 这种父目录，而真正落笔的地方是某个已映射的项目里——
    只看 cwd 会得到 NULL，于是"动作没有项目归属"，聚合与统计全空。
    """
    pairs = project_pairs(conn)
    if not pairs:
        return (None, None)
    for candidate in (path_abs, cwd):
        if not candidate:
            continue
        hit = fish_surface.longest_prefix(candidate, pairs)
        if hit:
            return (hit[1], hit[0])
    return (None, None)


def project_base(conn, project):
    """项目 → 它的工作区根（`workspace_map` 里最短的那条前缀）；没有返回 None。

    为什么取最短而不是最长：一个项目可能映射多个子目录（主库/文档/输出），
    相对路径的基准应当是**最外层**那条，否则 `p0/fish.py` 会被算成 `fish.py`。
    """
    if not project:
        return None
    best = None
    for prefix, proj in project_pairs(conn):
        if proj != project:
            continue
        norm = fish_surface.normalize_path(prefix)
        if not norm:
            continue
        if best is None or (len(norm), norm) < (len(best), best):
            best = norm
    return best


# ------------------------------------------------------------------ 分类


_PATH_FIELDS = ("file_path", "notebook_path", "path", "target_file", "file")
_LENGTH_FIELDS = ("content", "new_string", "old_string", "new_source", "edits")

_REDIRECT_RE = re.compile(r"(?<![0-9&<>])>>?\s*([^\s|;&<>()]+)")
_TEE_RE = re.compile(r"\btee\b(?:\s+-[a-zA-Z]+)*\s+([^\s|;&<>()]+)")
_SED_INPLACE_RE = re.compile(r"\bsed\b[^|;&\n]*?\s-i[a-zA-Z]*\s+\S*\s*([^\s|;&<>()]+)")
_PERL_INPLACE_RE = re.compile(r"\bperl\b[^|;&\n]*?\s-[a-zA-Z]*i[a-zA-Z]*\s+\S*\s*([^\s|;&<>()]+)")
_PS_WRITE_RE = re.compile(
    r"\b(Set-Content|Out-File|Add-Content|New-Item|Remove-Item|Move-Item|Copy-Item)\b([^\n|;&]*)",
    re.I)
_GIT_WRITE_RE = re.compile(
    r"\bgit\s+(commit|checkout|switch|reset|merge|rebase|cherry-pick|apply|stash|clean)\b", re.I)
_PKG_WRITE_RE = re.compile(r"\b(npm|pnpm|yarn|pip3?|uv)\s+(install|add|remove|uninstall)\b", re.I)
_OPAQUE_RE = re.compile(
    r"\b(python[0-9.]*|py)\s+-c\b|\b(powershell|pwsh)\b[^\n]*\s-(command|c)\b", re.I)
_FS_CMDS = ("rm", "mv", "cp", "mkdir", "touch", "rmdir", "del", "copy", "move", "md")
_FS_CMD_RE = re.compile(r"(?:^|[|;&\n(])\s*(%s)\b([^|;&\n]*)" % "|".join(_FS_CMDS), re.I)


def _first_path(tool_input) -> str:
    """从工具入参里取**目标路径**（第一个非空字符串）；没有返回空串。"""
    if not isinstance(tool_input, dict):
        return ""
    for field in _PATH_FIELDS:
        value = tool_input.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _param_length(tool_input) -> int:
    """参数里**内容型字段的总长度**（只用来算摘要，内容本身绝不落库）。"""
    if not isinstance(tool_input, dict):
        return 0
    total = 0
    for field in _LENGTH_FIELDS:
        value = tool_input.get(field)
        if isinstance(value, str):
            total += len(value)
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, str):
                    total += len(item)
                elif isinstance(item, dict):
                    for sub in item.values():
                        if isinstance(sub, str):
                            total += len(sub)
    return total


def _digest(tool_name, path_abs, action, length, feature) -> str:
    """参数摘要：`sha256(工具|路径|动作|长度|特征)[:16]`。**输入里没有任何内容片段。**"""
    raw = "|".join([str(tool_name), str(path_abs), str(action),
                    str(int(length or 0)), str(feature or "")])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _first_non_option(text):
    """取命令串里第一个「非选项」参数（跳过 `-x` / `--long` / `-rf` 这类开关）。"""
    for token in (text or "").split():
        token = token.strip("'\"")
        if not token or token.startswith("-"):
            continue
        return token
    return ""


def bash_write_feature(command, *, cwd=None):
    """判断一条 Bash 命令是不是**写动作**，返回 `(特征名, 目标绝对路径|None, 长度)`。

    保守白名单（设计 §5.3 表）：宁可漏占，不可误占。
      · 目标可解析（重定向 / tee / rm 等 / sed -i / PowerShell 写类）→ 占面；
      · 目标不可解析（`git commit`、包管理器装包、`python -c`）→ 只记 `exec`、不占面。
        理由：`git commit` 的 cwd 可能跨项目，按 cwd 占面会制造**假撞车**，
        而假撞车比漏报更伤（警告一旦掺假就会被学会忽略）。
    """
    cmd = (command or "").strip()
    if not cmd:
        return (None, None, 0)
    if _OPAQUE_RE.search(cmd):
        # `python -c "..."` / `powershell -Command "..."`：整段是不可解析的代码，
        # 落 path 只会写进一堆 `"` 与括号。只记 exec。
        return ("opaque-command", None, len(cmd))

    candidates = []
    match = _REDIRECT_RE.search(cmd)
    if match:
        target = match.group(1).strip("'\"")
        if fish_surface.normalize_path(target).rsplit("/", 1)[-1] not in NOISE_REDIRECT_TARGETS:
            candidates.append(("redirect", target))
    match = _TEE_RE.search(cmd)
    if match:
        candidates.append(("tee", match.group(1).strip("'\"")))
    match = _SED_INPLACE_RE.search(cmd)
    if match:
        candidates.append(("sed-inplace", match.group(1).strip("'\"")))
    match = _PERL_INPLACE_RE.search(cmd)
    if match:
        candidates.append(("perl-inplace", match.group(1).strip("'\"")))
    match = _PS_WRITE_RE.search(cmd)
    if match:
        rest = match.group(2)
        target = ""
        path_opt = re.search(r"-Path\s+([^\s|;&]+)", rest, re.I)
        if path_opt:
            target = path_opt.group(1).strip("'\"")
        else:
            target = _first_non_option(rest)
        if target:
            candidates.append(("powershell-write", target))
    match = _FS_CMD_RE.search(cmd)
    if match:
        target = _first_non_option(match.group(2))
        if target:
            candidates.append(("fs-" + match.group(1).lower(), target))

    for feature, target in candidates:
        if not target:
            continue
        resolved = fish_surface.make_abs(target, cwd)
        name = fish_surface.normalize_path(resolved).rsplit("/", 1)[-1]
        if name in NOISE_REDIRECT_TARGETS:
            continue
        return (feature, resolved, len(cmd))

    for pattern, feature in ((_GIT_WRITE_RE, "git-write"), (_PKG_WRITE_RE, "package-manager")):
        if pattern.search(cmd):
            return (feature, None, len(cmd))
    return (None, None, 0)


def classify_tool_call(tool_name, tool_input, *, cwd=None, project_root=None,
                       project=None, ok=None):
    """把一次工具调用分类成一行 `tool_activity`（或 None = 不记录）。

    返回 `{action, tool_name, path, path_dir, path_abs, args_digest, project, ok}`：

      · 写类工具（`write/edit/multiedit/notebookedit/apply_patch`）→ `action='write'`；
      · Bash 命中写特征 → `action='exec'`；目标不可解析时三列路径为 NULL（只统计）；
      · 读类 / 其他工具 / `mcp__fish__*`（自我观测噪音）→ 返回 None（**不落库**）。
    """
    name = (tool_name or "").strip().lower()
    if not name:
        return None
    if name.startswith("mcp__fish__") or name.startswith("mcp__"):
        # fish 自己的 MCP 工具不参与：那是自我观测，只会把表填满自己的噪音。
        return None

    base = fish_surface.normalize_path(project_root) or fish_surface.normalize_path(cwd)

    if fish_surface.is_write_tool(name):
        raw = _first_path(tool_input)
        if not raw:
            # 没有目标路径的写类调用记不出"占了哪块"，但**不代表没发生**。
            # 设计只允许落"路径可解析"的行（占面三列为 NULL 的行在 write 上没有语义），
            # 所以这里不落库——它不是漏记，是不该占一行。
            return None
        path_abs = fish_surface.make_abs(raw, cwd)
        if not path_abs:
            return None
        display = fish_surface.relativize(path_abs, base) if base else path_abs
        length = _param_length(tool_input)
        return {"action": "write", "tool_name": name, "project": project,
                "path": display, "path_dir": fish_surface.dir_of(display),
                "path_abs": path_abs,
                "args_digest": _digest(name, path_abs, "write", length, ""),
                "ok": ok}

    if fish_surface.is_bash_tool(name):
        ti = tool_input if isinstance(tool_input, dict) else {}
        command = ti.get("command")
        if not isinstance(command, str):
            command = ti.get("cmd") if isinstance(ti.get("cmd"), str) else ""
        feature, target_abs, length = bash_write_feature(command, cwd=cwd)
        if not feature:
            return None
        display = ""
        path_dir = None
        if target_abs:
            display = fish_surface.relativize(target_abs, base) if base else target_abs
            path_dir = fish_surface.dir_of(display)
        return {"action": "exec", "tool_name": name, "project": project,
                "path": display or None, "path_dir": path_dir,
                "path_abs": target_abs or None,
                "args_digest": _digest(name, target_abs or "", "exec", length, feature),
                "ok": ok}

    return None


_OK_FAIL_MARKERS = ("error", "failed", "failure", "traceback", "permission denied",
                    "no such file", "not found", "exception")


def judge_ok(tool_response):
    """从 `tool_response` 猜一次调用的成败：1 成功 / 0 失败 / None **未知**。

    **NULL 不是失败**（设计 §4.1）：没有 response 的宿主（或工具本身无输出）
    只能是"不知道"，把它记成 0 会让成功率永远偏低。
    """
    if tool_response is None:
        return None
    if isinstance(tool_response, dict):
        for key in ("is_error", "isError", "error"):
            value = tool_response.get(key)
            if isinstance(value, bool):
                return 0 if value else 1
        text = " ".join(str(v) for v in tool_response.values()
                        if isinstance(v, (str, int, float)))[:4000]
    elif isinstance(tool_response, (list, tuple)):
        text = " ".join(str(v) for v in tool_response if isinstance(v, (str, int, float)))[:4000]
    else:
        text = str(tool_response)[:4000]
    if not text.strip():
        return None
    lowered = text.lower()
    return 0 if any(marker in lowered for marker in _OK_FAIL_MARKERS) else 1


# ------------------------------------------------------------------ 落库


def record(conn, *, host, session_key, citizen_id, classification, seen_at=None) -> int:
    """INSERT 一行动作事实，返回 rowid。"""
    ts = seen_at or utc_now()
    cur = conn.execute(
        "INSERT INTO tool_activity (host, session_key, citizen_id, project, tool_name, "
        "action, path, path_dir, path_abs, args_digest, ok, seen_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (host, session_key, citizen_id, classification.get("project"),
         classification.get("tool_name"), classification.get("action"),
         classification.get("path"), classification.get("path_dir"),
         classification.get("path_abs"), classification.get("args_digest"),
         classification.get("ok"), ts))
    conn.commit()
    return int(cur.lastrowid or 0)


# ------------------------------------------------------------------ 聚合 / 缓存


def _presence_has_session(conn) -> bool:
    """`presence.session_key` 列在不在（读侧兜底的总开关）。

    为什么要探而不是假定：迁移可能因"备份写不进"而**故意不升级**（设计 45 §2.2 步 5），
    此时库还是两列形态，`SELECT p.session_key` 会 `no such column`。
    hook 侧不许 import fish，所以这份探测必须在 `fish_activity` 里自己有一份。
    """
    try:
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(presence)")}
    except sqlite3.Error:
        return False
    return "session_key" in cols


def _surfaces_query(project, cutoff, *, by_session: bool):
    """窗口内写动作按「目录 / 文件」聚合的唯一 SQL（两条聚合共用同一形状）。

    `by_session=False` → 分组键是公民（看板「实际在动」列的历史口径）；
    `by_session=True`  → 分组键多一个 `session_key`（撞车判定要精确到会话）。

    为什么共用：设计 45 §3.4 要求两条聚合对"什么算在动"给**同一个答案**；
    两份 SQL 各写一次，窗口/过滤/文件面口径迟早漂开（本项目的老账）。
    """
    keys = "citizen_id, session_key, " if by_session else "citizen_id, "
    sql = ("SELECT " + keys + "path_dir, path_abs, COUNT(*), MAX(seen_at) FROM tool_activity "
           "WHERE action='write' AND path_dir IS NOT NULL AND citizen_id IS NOT NULL "
           "AND seen_at >= ?")
    params = [cutoff]
    if project is not None:
        sql += " AND project IS ?"
        params.append(project)
    sql += " GROUP BY " + keys + "path_dir, path_abs ORDER BY COUNT(*) DESC"
    return sql, params


def _surfaces_new_entry() -> dict:
    """聚合项的空壳（`_by`/`_files` 是内部索引，最终会被剥掉：读侧只认四个键）。"""
    return {"dirs": [], "files": [], "last": None, "count": 0, "_by": {}, "_files": {}}


def _surfaces_accumulate(out, key, path_dir, path_abs, count, last) -> None:
    """把一行聚合结果并进 `out[key]`（`key` 是公民，或 `(公民, 会话键)`）。

    目录按 `dir_abs` 合并（同名不同处不算一处）；文件面只收「绝对 + 形态像文件」
    的 `path_abs`——不猜、也不反推展示列（评审 L1 / F4 的纪律）。
    """
    entry = out.get(key)
    if entry is None:
        entry = out[key] = _surfaces_new_entry()
    dir_abs = fish_surface.dir_of(path_abs) if path_abs else ""
    if dir_abs and not fish_surface.is_absolute(dir_abs):
        dir_abs = ""          # 证明不了是绝对路径 → 判定列留空（**不编**）
    slot_key = dir_abs or ("?" + str(path_dir))
    slot = entry["_by"].get(slot_key)
    if slot is None:
        slot = {"dir": path_dir or "", "dir_abs": dir_abs, "count": 0, "last": None}
        entry["_by"][slot_key] = slot
        entry["dirs"].append(slot)
    slot["count"] += int(count or 0)
    if last and (slot["last"] is None or last > slot["last"]):
        slot["last"] = last
    entry["count"] += int(count or 0)
    if last and (entry["last"] is None or last > entry["last"]):
        entry["last"] = last
    # 文件面：只收「绝对 + 形态像文件」的 `path_abs`（不猜、不反推显示列）
    file_abs = fish_surface.normalize_path(path_abs) if path_abs else ""
    if (file_abs and fish_surface.is_absolute(file_abs)
            and fish_surface.looks_like_file(file_abs)):
        file_slot = entry["_files"].get(file_abs)
        if file_slot is None:
            file_slot = {"path": file_abs, "count": 0, "last": None}
            entry["_files"][file_abs] = file_slot
            entry["files"].append(file_slot)
        file_slot["count"] += int(count or 0)
        if last and (file_slot["last"] is None or last > file_slot["last"]):
            file_slot["last"] = last


def _surfaces_finalize(out) -> None:
    """收尾：剥内部索引、排序、文件面截断（**排序口径是契约**，两条聚合一致）。"""
    for entry in out.values():
        entry.pop("_by", None)          # 内部索引不外泄（读侧只认 dirs/files/last/count）
        entry.pop("_files", None)
        entry["dirs"].sort(key=lambda d: (-d["count"], d["dir_abs"] or d["dir"]))
        entry["files"].sort(key=lambda f: (-f["count"], f["path"]))
        del entry["files"][MAX_FILES_PER_CITIZEN:]     # 只留前 N（提示用，不是清单）


def auto_surfaces(conn, *, project=None, window_min=30, now=None) -> dict:
    r"""按公民聚合「最近在写哪些目录 / 哪些**文件**」，返回
    `{citizen_id: {"dirs": [{"dir","dir_abs","count","last"}...],
                   "files": [{"path","count","last"}...], "last": ts, "count": n}}`。

    只算 `action='write'` 且 `path_dir` 非空的行（Bash 里目标不可解析的 exec 不占面）。

    `window_min` 是「正在写」的窗口（默认 `board.collision_window_min`/30 分钟）：
    与 idle 的 30 分钟同量级，因为它的用途是"此刻谁在这块"。

    ## 两列目录，**不许互相冒充**（评审 L1 的修复点）

      · `dir`     —— **展示**用：事实表里的 `path_dir`（相对**写入者基准**算出来的）；
      · `dir_abs` —— **判定**用：`dir_of(path_abs)`，永远来自事实列、永远绝对。

    为什么不能用 `dir` 反推绝对路径：`path_dir` 是写入者那一刻的展示值（相对它的项目根），
    看板侧拿**看板所属项目**的根去 `make_abs` 只会造出一个不存在的路径
    （2026-10-26 评审 L1 实测：同一个人在 A 板、动作在 B 项目 → `dir_abs` 变 `i:/a/p0`，
    而事实是 `i:/b/p0`；未映射项目 base 为空时干脆留下相对串）。
    聚合键也按 `dir_abs`：`i:/a/p0` 与 `i:/b/p0` 是两块**不同**的地方，同名不等于同处。

    ## `files`：文件面也要能被撞车判定看见（评审 F4）

    只看目录会漏一种真实撞车：一个人声明 `p0/fish.py`（**文件面**），另一个人在同一个
    文件里写——`path_hit('p0/fish.py', 'i:/other/p0')` 是 `None`（文件面不做分量链包含，
    而对方给的是目录），于是"同一文件被两个人改"永远不报。这里额外给出**绝对文件路径**
    （同样只来自事实列 `path_abs`，只收形态像文件的路径），让调用方能拿"对方的实际文件"
    对"我的声明面"再判一次（双向判定）。上限 `MAX_FILES_PER_CITIZEN`：这是给提示用的，
    不是完整清单（按次数降序取前 N，结果稳定可复现）。
    """
    window = int(window_min or 0)
    now_dt = _dt(now)
    cutoff = (now_dt - datetime.timedelta(minutes=max(window, 0))).strftime("%Y-%m-%dT%H:%M:%SZ")
    sql, params = _surfaces_query(project, cutoff, by_session=False)
    out = {}
    for cid, path_dir, path_abs, count, last in conn.execute(sql, params).fetchall():
        _surfaces_accumulate(out, cid, path_dir, path_abs, count, last)
    _surfaces_finalize(out)
    return out


def auto_surfaces_by_session(conn, *, project=None, window_min=30, now=None) -> dict:
    """会话维度聚合：`{(citizen_id, session_key): {"dirs","files","last","count"}}`。

    与 `auto_surfaces` **共用同一段 SQL 形状与同一套收尾口径**（只多一个分组列），
    但**不替换**它：`auto_surfaces` 的返回结构/排序/上限被既有判据逐条钉着，
    本轮一个字都不动（设计 45 §3.4）。

    为什么必须有它（设计 P4/P5）：按公民压扁会让"同一个人两个会话"的写入面合并成
    一份，于是「我的声明 vs 我的实际」永远分不清是谁的——那正是"自己撞自己"的第三个病根。
    """
    window = int(window_min or 0)
    now_dt = _dt(now)
    cutoff = (now_dt - datetime.timedelta(minutes=max(window, 0))).strftime("%Y-%m-%dT%H:%M:%SZ")
    sql, params = _surfaces_query(project, cutoff, by_session=True)
    out = {}
    for cid, session_key, path_dir, path_abs, count, last in conn.execute(sql, params).fetchall():
        _surfaces_accumulate(out, (cid, str(session_key or "")), path_dir, path_abs,
                             count, last)
    _surfaces_finalize(out)
    return out


# 共享缓存/临时目录的**分量名**（评审 L5）：这些地方到处同名（`%TEMP%/p0`、
# `node_modules/x/p0`），拿它们判撞车只会制造噪音，而误报比漏报更伤。
# 判据是**路径分量**（`path_segments` 已小写），不做字符串包含（`templates` ≠ `temp`）。
NOISE_DIR_COMPONENTS = frozenset({
    "node_modules", "__pycache__", ".git", ".pytest_cache", ".mypy_cache",
    ".cache", "temp", "tmp", "appdata",
})

# 每个公民最多保留多少个「实际写过的文件」（评审 F4）：撞车提示只需要
# "最像正在改的那几个文件"，全量清单既占内存也没人看。
MAX_FILES_PER_CITIZEN = 20


def is_noise_dir(path) -> bool:
    """这条目录是不是「临时/缓存」类，**不该参与撞车候选**。

    为什么要有这条（评审 L5）：`%TEMP%/fish-smoke-xxx/p0` 这种路径里的 `p0`
    与仓库里的 `p0` 只是**同名**，而单段目录在机器上到处都是。设计给的止损是
    "调大 `min_segments` 或直接 `off`"——但更准确的止损是**按目录种类**排除：
    临时/缓存目录不是协作面，里面根本没有"两个人同改一块"这回事。
    """
    return any(part in NOISE_DIR_COMPONENTS for part in fish_surface.path_segments(path))

# 单会话最多列几个目录（与 `refresh_presence_cache` 的 `dirs[:3]` 同口径：
# 看板一格放得下 3 个目录，再多就得断行，反而更难读）。
ACTIVITY_SESSION_MAX_DIRS = 3


def activity_sessions(conn, *, project, window_min=None, now=None) -> list:
    r"""按**会话**聚合"正在动"——返回按最近活动倒序的列表（只读，`tool_activity`）。

    每一行的形状：`{"session_key", "citizen_id", "display_name", "dirs",
    "writes", "last_seen"}`

      · `dirs`：`[{"dir", "count", "last"}]`，最多 `ACTIVITY_SESSION_MAX_DIRS` 个，
        按写次数倒序（次数相同按目录名，保证顺序稳定、渲染不抖）；
      · `writes`：窗口内 `action='write'` 的条数——`exec`（bash）不计入"写"；
      · `display_name`：查 `citizens`；查不到就是 `None`（**不猜名字**）。

    ## 为什么按会话聚合，而不是像 `auto_surfaces` 那样按公民

    两个函数回答的是**不同的问题**，共用一份实现会立刻丢掉其中一边：

      · `auto_surfaces`：**哪个公民**在动哪块 → 看板在场表的「实际在动」列；
      · 本函数：**哪些会话**在动而没签到 → FB-14 的那一节。

    FB-14 的现场（生产库只读实测）：会话 `dsh:b92c164c…` 有 64 条 `tool_activity`
    （一直在写 `fish-devA/p0/fish.py`），但它的 presence 落在「未分类」板，
    项目板上根本没有它的行 → 项目板显示"没人"。会话键是那时**唯一**能抓到它的
    抓手，所以聚合键必须是 `session_key`。

    ## 三条边界（都不是风格问题）

      1. **只读**：不写 `presence`、不写 `events`——它是**推断**，不是签到；
      2. **窗口**：`window_min` 缺省读 `activity.aggregate_window_min`（30 分钟）；
         超窗的行不参与聚合（"此刻在动"不等于"曾经在动"）；
      3. **必须给 `project`**：不给（空/None）返回 `[]`，**不做全项目混算**——
         混算会把 A 项目的会话印到 B 项目的板上，那是比"看不到"更坏的错。
    """
    key = str(project or "").strip()
    if not key:
        return []
    window = int(get_cfg(conn, "activity.aggregate_window_min")) \
        if window_min is None else int(window_min or 0)
    cutoff = (_dt(now) - datetime.timedelta(minutes=max(window, 0))).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        "SELECT a.session_key, a.citizen_id, a.path_dir, a.action, a.seen_at, "
        "       c.display_name "
        "FROM tool_activity a LEFT JOIN citizens c ON c.citizen_id = a.citizen_id "
        "WHERE a.project = ? AND a.seen_at >= ? "
        "  AND a.session_key IS NOT NULL AND a.session_key != '' "
        "ORDER BY a.seen_at DESC, a.id DESC", (key, cutoff)).fetchall()
    out = {}
    for session_key, citizen_id, path_dir, action, seen_at, display_name in rows:
        entry = out.get(session_key)
        if entry is None:
            entry = out[session_key] = {
                "session_key": session_key, "citizen_id": citizen_id or None,
                "display_name": display_name or None, "dirs": [], "writes": 0,
                "last_seen": seen_at, "dirs_map": {}}
        # 行按 seen_at 倒序扫：先遇到的就是**最新**的归属，后来的（更早的）不覆盖。
        if entry["citizen_id"] is None and citizen_id:
            entry["citizen_id"] = citizen_id
            entry["display_name"] = display_name or None
        elif entry["display_name"] is None and citizen_id == entry["citizen_id"]:
            entry["display_name"] = display_name or None
        if action != "write":
            continue                    # exec 不算"写"，但它的会话仍算"在动"
        entry["writes"] += 1
        if not path_dir:
            continue                    # 写类但没有可解析目录（Bash 里 redirect）不占面
        slot = entry["dirs_map"].get(path_dir)
        if slot is None:
            entry["dirs_map"][path_dir] = {"dir": path_dir, "count": 1, "last": seen_at}
        else:
            slot["count"] += 1
            if seen_at and (not slot["last"] or seen_at > slot["last"]):
                slot["last"] = seen_at
    sessions = []
    for entry in out.values():
        dirs = sorted(entry.pop("dirs_map").values(),
                      key=lambda d: (-d["count"], d["dir"]))
        entry["dirs"] = dirs[:ACTIVITY_SESSION_MAX_DIRS]
        sessions.append(entry)
    return sorted(sessions, key=lambda s: s["last_seen"] or "", reverse=True)


def refresh_presence_cache(conn, *, citizen_id, project, dirs, seen_at,
                           min_interval_sec=20, session_key="") -> bool:
    """把「最近在动哪」写进今日 presence 行的 `auto_*` 三列（**节流**）。

    三条边界（设计 §4.2）：
      · 只 UPDATE **已存在**的行——不建板、不建行（自动行由 SessionStart 建）；
      · 不动 `doing` / `surface`（那是人的声明，缓存不许冒充意图）；
      · 节流到 `min_interval_sec`（默认 20s），否则每次写都 UPDATE 一次库。

    返回 True 表示这次真的写了。

    ## 2026-10-26（撞车自撞修复 T02）：会话维度

    `session_key` 默认 `''` → 命中**无会话归属**那一行（= 今天的行为，旧调用点零改动）。
    有会话键时只刷**自己那一行**的 `auto_*`；同公民另一个会话的缓存不再被覆盖（P5）。
    列不存在（迁移未跑）时自动退回只按 `(board, citizen)`——故障兜底，不是第二套语义。
    """
    if not citizen_id:
        return False
    sk = str(session_key or "").strip()
    with_session = _presence_has_session(conn)
    sk_filter = " AND p.session_key=?" if with_session else ""
    params = [("project", project) if project else ("root", ""), local_period_key(seen_at),
              citizen_id]
    scope, key = params[0]
    row = conn.execute(
        "SELECT b.id, p.auto_seen_at FROM boards b JOIN presence p ON p.board_id = b.id "
        "WHERE b.scope=? AND b.scope_key=? AND b.period_key=? AND p.citizen_id=?"
        + sk_filter,
        tuple([scope, key, params[1], citizen_id] + ([sk] if with_session else []))).fetchone()
    if not row:
        return False
    board_id, auto_seen = row[0], row[1]
    if auto_seen and min_interval_sec and _age_sec(auto_seen, seen_at) < float(min_interval_sec):
        return False
    surface = ",".join([d for d in (dirs or [])][:3])
    conn.execute(
        "UPDATE presence SET auto_surface=?, auto_seen_at=?, auto_source='activity' "
        "WHERE board_id=? AND citizen_id=?" + (" AND session_key=?" if with_session else ""),
        tuple([surface, seen_at, board_id, citizen_id] + ([sk] if with_session else [])))
    conn.commit()
    return True


def _now_epoch() -> float:
    return datetime.datetime.now(datetime.timezone.utc).timestamp()


def _read_state(state_path):
    try:
        return json.loads(Path(state_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(state_path, payload):
    """原子写一份小状态文件（tmp + os.replace）；失败静默返回 False。"""
    path = Path(state_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


# ------------------------------------------------------------------ 快照


def _presence_entries(conn, now_text=None):
    """今日看板里所有 `status != 'left'` 的在场行 + **会话维度**标签。

    2026-10-26（撞车自撞修复 T03）：SELECT 多取一列 `p.session_key`（列不在时读 `''`），
    `labels` 的键从 `citizen_id` 变成 `(citizen_id, session_key)`。

    返回的 `rows` 列序：`(session_key, citizen_id, agent_id, surface, status, last_seen_at,
    auto_surface, auto_seen_at, auto_source, scope, scope_key, display_name, host,
    created_at)` —— 调用方按位置解包，改列就是改契约。
    """
    sk_col = "p.session_key" if _presence_has_session(conn) else "''"
    # ⚠ 保持**一个字符串常量**（`.format` 而不是 `+`）：`test_activity` 的
    # `FrozenCitizensSqlTest` 按"常量以 SQL 关键字开头且提到 citizens"机械清点
    # 本模块的 citizens 读点，拆字符串会让 `_presence_entries` 从清单里消失。
    rows = conn.execute(
        "SELECT {sk}, p.citizen_id, p.agent_id, p.surface, p.status, "
        "p.last_seen_at, p.auto_surface, p.auto_seen_at, p.auto_source, b.scope, "
        "b.scope_key, c.display_name, c.host, c.created_at "
        "FROM presence p JOIN boards b ON b.id = p.board_id "
        "LEFT JOIN citizens c ON c.citizen_id = p.citizen_id "
        "WHERE b.period_key=? AND p.status != 'left'".format(sk=sk_col),
        (local_period_key(now_text),)).fetchall()
    # 标签的键必须是**解析后**的会话键：F1 会把「列为空、agent_id 形如会话键」的
    # 行解析成 `agent_id:legacy` 来源的键。只用列值建键的话，快照条目拿到的是解析后的
    # 键 → 查不到标签 → 人类面掉成 `未命名`（判据会在这里红，而不是在语义上）。
    labels = fish_naming.session_labels([
        {"citizen_id": r[1], "session_key": entry_session_key(r[0], r[2])[0],
         "display_name": r[11], "host": r[12], "created_at": r[13]} for r in rows])
    return rows, labels


def entry_session_key(session_key, agent_id):
    """快照条目的会话键：**列优先**，旧列（形如会话键的 `agent_id`）兜底（设计 F1）。

    为什么保留兜底：既有判据（`test_pretool_ask._declare`）是用
    `UPDATE presence SET agent_id=?` 注入会话键的——只读新列会让"排除自己"失效，
    于是旧判据红在一个与语义无关的地方。来源写进 `session_key_source`：
    读者能看出这条键是**读出来的**（column）还是**顺着旧列认出来的**（agent_id:legacy），
    不静默（设计 §2.1.1 F1 的最后一句）。
    """
    col = str(session_key or "").strip()
    legacy = str(agent_id or "").strip() if fish_naming.is_session_key(agent_id) else ""
    if col:
        return col, "column"
    if legacy:
        return legacy, "agent_id:legacy"
    return "", "none"


def presence_auto_info(citizen_id, session_key, by_session, by_citizen) -> dict:
    """一行 presence 的「实际在动」取数——**三档渐进匹配**（设计 §3.6 第一处）。

      ① `(cid, sk)` 有聚合 → 用它（精确）；
      ② `sk == ''` → 退公民级聚合（旧行没有会话键，= 今天的行为）；
      ③ `sk != ''` 但该会话没有活动 → **空**（绝不拿同公民别的会话的面冒充）。

    第 ③ 档是纪律档不是保守档：有会话键却拿别人的面，等于把别人的写入栽在这行头上，
    正是这一轮要根治的误报源（判据 S26 专门盯它）。
    """
    if session_key:
        return by_session.get((citizen_id, session_key)) or {}
    return by_citizen.get(citizen_id) or {}


def _abs_list(paths, base):
    """把一组面转成**绝对路径**列表（相对面按 `base` 解析；`base` 缺失时原样保留）。"""
    out = []
    for raw in paths or ():
        norm = fish_surface.normalize_path(raw)
        if not norm:
            continue
        out.append(norm if fish_surface.is_absolute(norm)
                   else fish_surface.make_abs(norm, base))
    return out


def _merge_entries_by_identity(entries):
    """同一身份键 `(citizen_id, session_key)` 跨板出现时合并成 1 条（QA F3 / 设计 S31）。

    为什么必须合并：`_presence_entries` 查的是**所有板**的行——同一会话今天在 3 块板
    签到就是 3 条 entry、label 相同。读者看到"同一个名字出现三次"（诊断 §八 里
    `#2/#3` 假象的另一半来源），撞车判定也会对同一目标重复报。

    合并语义（设计 D7）：
      · `projects` = 去重后的项目列表；`project` 保留**声明面最长**的那条所在项目
        （无声明时取第一个非空）；
      · `declared` / `declared_abs` / `auto` 取并集（`auto` 按 `dir_abs` 去重，
        `count` 取 max、`last` 取最近）；
      · `last_seen` 取最大、`auto_source` 取最近、`label` 取第一条。
    """
    merged = {}
    order = []
    for e in entries or ():
        key = (str(e.get("citizen_id") or ""), str(e.get("session_key") or ""))
        if key not in merged:
            item = dict(e)
            item["projects"] = []
            item["declared"] = []
            item["declared_abs"] = []
            item["auto"] = []
            merged[key] = item
            order.append(key)
        item = merged[key]
        proj = str(e.get("project") or "")
        if proj and proj not in item["projects"]:
            item["projects"].append(proj)
        # QA R2-S1：`project` 取**声明面最长**那条 entry 所在项目（不是"项目名最长"）。
        face_len = max((len(p) for p in (e.get("declared") or ())), default=0)
        if face_len > item.get("_face_len", -1):
            item["_face_len"] = face_len
            item["_project_longest"] = proj
        # QA R2-S2：合并后 `declared_at` 取 max——取首行会让**仍然有效的声明**被
        # 90 分钟窗口丢掉（漏报），且结果依赖建板顺序。
        if (e.get("declared_at") or "") > (item.get("declared_at") or ""):
            item["declared_at"] = e.get("declared_at") or ""
        for p in e.get("declared") or ():
            if p not in item["declared"]:
                item["declared"].append(p)
        for p in e.get("declared_abs") or ():
            if p not in item["declared_abs"]:
                item["declared_abs"].append(p)
        by_dir = {a.get("dir_abs"): a for a in item["auto"]}
        for a in e.get("auto") or ():
            cur = by_dir.get(a.get("dir_abs"))
            if cur is None:
                item["auto"].append(dict(a))
                by_dir[a.get("dir_abs")] = item["auto"][-1]
            else:
                if (a.get("last") or "") > (cur.get("last") or ""):
                    cur["last"] = a.get("last")
                    cur["dir"] = a.get("dir") or cur.get("dir")
                try:
                    cur["count"] = max(int(cur.get("count") or 0), int(a.get("count") or 0))
                except (TypeError, ValueError) as exc:
                    warn_ignored('fish_activity.py:1007 _merge_entries_by_identity', exc)
        if (e.get("last_seen") or "") > (item.get("last_seen") or ""):
            item["last_seen"] = e.get("last_seen")
        if (e.get("auto_source") or "") >= (item.get("auto_source") or ""):
            item["auto_source"] = e.get("auto_source") or item.get("auto_source")
    out = []
    for key in order:
        item = merged[key]
        item["project"] = (item.get("_project_longest")
                           or (max(item["projects"], key=len) if item["projects"] else ""))
        item.pop("_face_len", None)
        item.pop("_project_longest", None)
        out.append(item)
    return out


def build_snapshot(conn, *, out_path, window_min=90, now=None, project=None) -> dict:
    """写「谁声明/实际占了哪块」的快照（`data/_audit/hooks/active_surfaces.json`）。

    ## 为什么是文件而不是查库

    PreToolUse 在**每次写之前**跑，读 8 万行的库要先 import sqlite3 再查一次；
    而快照是一份已经聚合好的小 JSON（几十行）。慢 200ms × 每次写 = 可感知的卡顿，
    而"快照最多晚 20 秒"在人工协作的时间尺度上没有代价。

    ## 内容纪律

    只写**归一化路径**（相对 + 绝对两列），不写内容、不写 command、不写 prompt。
    绝对路径只进 `declared_abs`/`dir_abs`——看板正文永远不会印它们。

    ## `auto.dir_abs` 只能来自事实列（评审 L1）

    `declared_abs` 与 `dir_abs` 的来源**不一样**，不许混：

      · `declared_abs`：人的声明是相对**本项目根**写的（`surface='p0/fish.py'`），
        所以按看板所属项目的 `workspace_map` 根解析——那是唯一正确的基准；
      · `dir_abs`：动作事实自带 `path_abs`（写入者那一刻解析出来的绝对路径），
        **直接取它的目录**（`dir_of(path_abs)`）。用看板项目根去 `make_abs` 一个相对显示串
        只会造出不存在的路径（A 板 + B 项目的动作 → `i:/a/p0`；base 为空 → 留下相对串），
        而 `find_collisions` 拿它做判定，等于**拿伪造的路径判撞车**。

    因此：`auto` 里**只放证明得了绝对目录的条目**（`dir_abs` 非绝对的直接不进快照）。
    """
    now_dt = _dt(now)
    window = int(window_min or 0)
    rows, labels = _presence_entries(conn, now)
    auto, auto_by_session = {}, {}
    try:
        auto = auto_surfaces(conn, window_min=window, now=now)
        auto_by_session = auto_surfaces_by_session(conn, window_min=window, now=now)
    except sqlite3.Error:
        auto, auto_by_session = {}, {}
    bases = {}

    def base_of(project_name):
        if project_name not in bases:
            bases[project_name] = project_base(conn, project_name)
        return bases[project_name]

    cutoff = (now_dt - datetime.timedelta(minutes=max(window, 0))).strftime("%Y-%m-%dT%H:%M:%SZ")
    entries = []
    for (session_col, cid, agent, surface, status, last_seen, auto_surface, auto_seen,
         auto_source, scope, scope_key, _dname, _host, _created) in rows:
        proj = scope_key if scope == "project" else None
        base = base_of(proj)
        declared = fish_surface.normalize_surface(surface)
        # 会话键：列优先 + 旧列兜底（F1）；来源可观测
        sk, sk_src = entry_session_key(session_col, agent)
        entry = {
            "citizen_id": cid,
            "label": labels.get((cid, sk)) or fish_naming.UNNAMED_LABEL,
            "host": _host or "",
            "session_key": sk,
            "session_key_source": sk_src,
            "agent_id": agent or "",       # 旧字段保留（外部读者可能还在用）
            "cwd": "",                 # 采集表不含 cwd（设计 §4.1 无此列，不编）
            "project": proj or "",
            "declared": declared,
            "declared_abs": _abs_list(declared, base),
            "declared_at": last_seen or "",
            "auto": [],
            "last_seen": last_seen or "",
            "auto_source": auto_source or "",
        }
        # 实际面按会话取（三档渐进，F2）：有会话键就只认自己那个会话
        info = presence_auto_info(cid, sk, auto_by_session, auto)
        for item in info.get("dirs") or []:
            if not item.get("last") or item["last"] < cutoff:
                continue
            dir_abs = item.get("dir_abs") or ""
            if not fish_surface.is_absolute(dir_abs):
                # 没有可证明的绝对目录 → **不进快照**（`find_collisions` 用不了它，
                # 放进去只会给人一个"看着有、其实永远不命中"的 dir_abs）。
                continue
            entry["auto"].append({
                "dir": item["dir"],
                "dir_abs": dir_abs,      # 事实列 `path_abs` 的目录，不是反推的
                "count": item["count"],
                "last": item["last"],
            })
        entries.append(entry)

    # QA F3 / 设计 S31：同一身份键跨板合并成 1 条（否则同一名字重复、撞车重复报）
    entries = _merge_entries_by_identity(entries)
    entries.sort(key=lambda e: (e["project"], e["label"], e["session_key"]))
    # 硬上限（设计 §4.4 / R5）：PreToolUse **每次写之前**都要读这份文件，
    # 会话维度一进来行数会从"公民数"涨到"会话数"，不设上限就是把 hook 拖慢。
    # 截断按最后一次活跃倒序（最可能撞的先留），截断条数写进 `truncated`（不静默）。
    truncated = max(0, len(entries) - SNAPSHOT_MAX_ENTRIES)
    if truncated:
        entries.sort(key=lambda e: (e["last_seen"] or e["declared_at"] or ""), reverse=True)
        entries = entries[:SNAPSHOT_MAX_ENTRIES]
        entries.sort(key=lambda e: (e["project"], e["label"], e["session_key"]))
    snapshot = {
        "generated_at": now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_min": window,
        "entries": entries,
        "truncated": truncated,
    }
    path = Path(out_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)       # 原子替换：读侧永远看不到半写的文件
    except OSError as exc:
        warn_ignored('fish_activity.py:1133 module', exc)
    return snapshot


def read_snapshot(path, *, max_age_sec: int = 3600):
    """读快照；缺失/损坏/过旧 → None（**调用方据此放行**，绝不猜）。"""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        return None
    generated = parse_utc(payload.get("generated_at"))
    max_age = int(max_age_sec or 0)
    if generated is None:
        return None
    if max_age > 0 and (_dt() - generated).total_seconds() > max_age:
        return None
    return payload


# ------------------------------------------------------------------ 撞车


def find_collisions(snapshot, *, target_abs, own_session_key, min_segments: int = 2,
                    now=None, declared_window_min: int = 90,
                    auto_window_min: int = 30) -> list:
    """在快照里找「别人正在写、我也要写」的重叠，返回可读清单。

    五条边界（设计 §6.6）：
      · 排除**自己这个 session_key**；同一公民的**另一个会话不排除**
        （两个窗口同改一个文件是真撞车），label 标「（另一会话）」；
      · 声明面的有效窗口 90 分钟、实际动作 30 分钟（不对称的代价，见设计 §6.6）；
      · `path_hit(min_segments)`：只认分量边界与 ≥2 段后缀，1 段的面到处同名；
      · **每会话最多 1 条**（取最具体的那一条），避免一个人报三遍；
      · 绝不返回 ULID，绝不返回内容。
    """
    now_dt = _dt(now)
    entries = (snapshot or {}).get("entries") or []
    hits = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_session = str(entry.get("session_key") or "")
        key = entry_session or str(entry.get("citizen_id") or "")
        if not key:
            continue
        # **本次修复的核心一行**（设计 §3.4）：同一条会话绝不报——
        # `''` 不等于"我自己"（它等于"不知道是谁"），所以不能拿它来排除。
        if own_session_key and entry_session == own_session_key:
            continue
        # `''` 与 `''` 是**同一参与方**（设计 §1.2 第 3 条）：两边都没有会话归属时，
        # 结构上分不清是不是同一个窗口，按"同一个"处理——否则每一次"无会话"的判定
        # 都会给自己报一条撞车（那正是这一轮要根治的误报源）。
        if not own_session_key and not entry_session:
            continue
        # 会话未标注（`''`）：照报，但标注出来（设计 §3.3 的不对称代价原则：
        # 漏报 > 多一句"可能是你自己"）。文案由 `describe_hits` 负责。
        unlabeled = not entry_session
        best = None
        declared_at = entry.get("declared_at") or entry.get("last_seen")
        if _age_sec(declared_at, now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")) <= declared_window_min * 60:
            for surface in entry.get("declared_abs") or ():
                hit = fish_surface.path_hit(surface, target_abs, min_segments=min_segments)
                if hit and (best is None or len(hit) > len(best["surface"])):
                    best = {"label": entry.get("label") or fish_naming.UNNAMED_LABEL,
                            "project": entry.get("project") or "",
                            "projects": list(entry.get("projects") or ()),
                            "surface": hit, "session_key": entry_session,
                            "session_unlabeled": unlabeled,
                            "citizen_id": entry.get("citizen_id") or "",
                            "at": declared_at, "kind": "declared"}
        for item in entry.get("auto") or ():
            if not isinstance(item, dict):
                continue
            if _age_sec(item.get("last"), now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")) > auto_window_min * 60:
                continue
            hit = fish_surface.path_hit(item.get("dir_abs") or "", target_abs,
                                        min_segments=min_segments)
            if hit and (best is None or len(hit) > len(best["surface"])):
                best = {"label": entry.get("label") or fish_naming.UNNAMED_LABEL,
                        "project": entry.get("project") or "",
                        "projects": list(entry.get("projects") or ()),
                        "surface": hit, "session_key": entry_session,
                        "session_unlabeled": unlabeled,
                        "citizen_id": entry.get("citizen_id") or "",
                        "at": item.get("last"), "kind": "auto"}
        if not best:
            continue
        at_dt = parse_utc(best["at"])
        best["minutes_ago"] = (int((now_dt - at_dt).total_seconds() // 60)
                               if at_dt else None)
        holder = hits.get(key)
        if holder is None or len(best["surface"]) > len(holder["surface"]):
            hits[key] = best
    out = list(hits.values())
    out.sort(key=lambda h: (h.get("minutes_ago") if h.get("minutes_ago") is not None else 10 ** 6,
                            h["label"]))
    return out


# ------------------------------------------------------------------ 审计 / 心跳 / 计数


def log_ask(path, row) -> bool:
    """追加一行撞车审计（JSONL，**不含内容**）；失败静默返回 False。"""
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def recent_ask(path, *, session_key, target_abs, cooldown_sec: int = 60,
               now=None) -> bool:
    """同 `(session_key, target_abs)` 是否在冷却期内**已经问过**。

    只读文件尾部 200 行：审计文件会长期追加，全读会随天数变慢。
    冷却不清代表"用户已批准"——它只防"同一次调用重复弹窗"。
    """
    cooldown = int(cooldown_sec or 0)
    if cooldown <= 0:
        return False
    target = Path(path)
    if not target.exists():
        return False
    try:
        lines = target.read_text(encoding="utf-8").splitlines()[-200:]
    except OSError:
        return False
    now_dt = _dt(now)
    norm = fish_surface.normalize_path(target_abs)
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("session_key") != session_key:
            continue
        if fish_surface.normalize_path(row.get("target")) != norm:
            continue
        asked = parse_utc(row.get("ts"))
        if asked and (now_dt - asked).total_seconds() <= cooldown:
            return True
    return False


# ── 撞车提醒的「一次性 pending 标记」（评审 L3：warn 对模型不可见）─────────────
#
# 问题不是"没写审计"，而是**审计只有人看**：`warn` 模式按设计**不输出任何决策**
# （那会变成阻塞/批准），于是模型那一侧看到的与"根本没撞车"完全一样——
# 提醒落在 `pretool-asks.jsonl` 里，模型永远读不到。
#
# 修法不是把 warn 改成输出决策（那等于偷偷开 ask，用户拍板不允许），而是走
# **PostToolUse 的 additionalContext**：在同一次工具调用的收尾递一句话。
#   · 即时 —— 就贴在工具结果旁边（pre → 执行 → post，同一瞬间）；
#   · 不阻塞 —— additionalContext 是"追加"，不是批准；
#   · 可审计 —— 标记文件留在 `_audit/hooks/`，带 `delivered_at`，发过几次一目了然；
#   · 只一次 —— `take_warn_pending` 读走即标记已递，冷却窗内不重复。
#
# 存什么：**与审计同口径**（时间、摘要原因、命中来源的名字/项目/路径/分钟数）。
# 不存 doing、不存内容、不存 command（模块头的隐私红线）。


def warn_pending_path(data_dir, session_key):
    """本会话 pending 标记的路径；没有会话键/数据目录 → None（**不编文件名**）。"""
    digest = session_digest(session_key)
    if not data_dir or not digest:
        return None
    return Path(data_dir) / HEARTBEAT_RELDIR / f"{WARN_PENDING_PREFIX}{digest}.json"


def set_warn_pending(data_dir, *, session_key, reason, target_abs="", hits=None,
                     now=None) -> bool:
    """写「待递一次」的撞车提醒（同会话覆盖写）。返回 True 表示真的落了盘。

    ## 覆盖规则（评审 F10：写死口径，并防乱序）

    **后到者覆盖，保留最新一条**——一个会话同时只欠**一句**提醒，欠三句等于没提醒
    （人只会看第一句，剩下的全是噪音）。两个并行调用撞车时，保留的那条就是**最后
    写进来的那条**（不是"最先"、也不是"合并"）。

    但"最后写进来"不等于"最新发生"：PreToolUse 是各自独立进程，事件可能乱序到达。
    所以这里用 `ts` 兜一道——**已有未递的标记比这次的 `ts` 更新时，不覆盖**
    （返回 False，磁盘不动）。判据见 `test_pretool_ask.WarnPendingTest`。
    """
    path = warn_pending_path(data_dir, session_key)
    text = str(reason or "").strip()
    if path is None or not text:
        return False
    row = {
        "ts": now or utc_now(),
        "session": session_digest(session_key),
        "reason": text[:400],
        "target": fish_surface.normalize_path(target_abs),
        "hits": [{"label": h.get("label"), "project": h.get("project"),
                  "surface": h.get("surface"), "minutes_ago": h.get("minutes_ago"),
                  "kind": h.get("kind")} for h in (hits or [])][:5],
        "delivered_at": None,
    }
    existing = _read_state(path)
    if isinstance(existing, dict) and not existing.get("delivered_at"):
        old_ts = parse_utc(existing.get("ts"))
        new_ts = parse_utc(row["ts"])
        if old_ts is not None and new_ts is not None and old_ts > new_ts:
            return False        # 迟到的旧事件不许盖掉更新的那条（乱序保护）
    return _write_state(path, row)


def take_warn_pending(data_dir, session_key, *, now=None):
    """取走本会话欠的那一句提醒：**只成功一次**；已递/过旧/写盘失败 → None。

    过旧（> `WARN_PENDING_MAX_AGE_SEC`）时**不删文件**：留着当"这条提醒过期没递出去"
    的证据（审计价值），但不再打扰模型。

    ## 写盘失败时**不递**（评审 F9）

    `delivered_at` 是"只递一次"的唯一凭据。旧实现把 `_write_state` 的返回值丢了：
    标记里写不进 `delivered_at`，可这句话照样被递出去 → **下一次 PostToolUse 再递一遍**，
    盘一直坏就一直重复。现在的取舍是：**宁可这次不提醒，也不让同一句反复刷屏**——
    标记文件还在、`delivered_at` 仍为空，下一次 PostToolUse 会再试，盘恢复即恰好递一次
    （失败是静默的：这条提醒是增强项，不许拖垮 hook）。
    """
    path = warn_pending_path(data_dir, session_key)
    if path is None:
        return None
    payload = _read_state(path)
    if not isinstance(payload, dict) or not str(payload.get("reason") or "").strip():
        return None
    if payload.get("delivered_at"):
        return None
    ts = parse_utc(payload.get("ts"))
    if ts is not None and (_dt(now) - ts).total_seconds() > WARN_PENDING_MAX_AGE_SEC:
        return None
    payload["delivered_at"] = now or utc_now()
    if not _write_state(path, payload):
        payload["delivered_at"] = None      # 没落盘就不算已递（标记保持原样，下次再试）
        return None
    return payload


def warn_notice_text(payload) -> str:
    """把 pending 标记拼成**递给模型的那一句**（两个宿主共用这一份措辞）。

    为什么措辞也放在共享核心：DSH 与 WorkBuddy 各写一份字符串，迟早出现
    "一个宿主说人话、另一个宿主说得像机器"的漂移，而这条提醒的全部价值就在于
    人/模型能不能立刻看懂。空 reason 返回空串（调用方据此不输出）。
    """
    reason = str((payload or {}).get("reason") or "").strip()
    if not reason:
        return ""
    return ("（大肥鱼生命系统撞车提醒：" + reason
            + "刚才那次写已经执行，本条只提醒一次——要不要协调由你判断。）")


def bump_counter(data_dir, *, session_id, event, every_n: int = 10) -> int:
    """共享频率闸：按 `(event, session)` 计数并返回本次计数；失败返回 -1。

    BOM 容错是必须的：PowerShell 5.1 的 `Set-Content -Encoding utf8` 会写 BOM，
    而 `int("\\ufeff9")` 直接抛 ValueError → 计数永远归零 → 推送永远不触发
    （2026-10-25 本机实测踩到过）。
    """
    step = int(every_n or 0)
    if step < 1:
        raise ValueError(f"every_n 必须 >= 1，收到 {every_n!r}")
    if not session_id:
        return -1
    try:
        folder = Path(data_dir) / "_audit" / "counters"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{event}-{str(session_id).replace(':', '_')}.n"
        count = 0
        if path.exists():
            try:
                count = int(path.read_text(encoding="utf-8").strip().lstrip("\ufeff") or "0")
            except (ValueError, OSError):
                count = 0
        count += 1
        path.write_text(str(count), encoding="utf-8")
        return count
    except OSError:
        return -1


_SAFE_FACT_RE = re.compile(r"^[A-Za-z0-9_.:\-]{0,32}$")

# 这些 fact 键走**更强的口径**（只留短哈希，原文一律不落盘）。
# 为什么单独列一张表而不是靠长度兜：会话键 `codebuddy:<uuid>` 恰好 ≤32 字符，
# 会被 `_safe_fact` 原样保留——而设计 §5.3/§10.3 的口径是「session 哈希」。
_FACT_DIGEST_KEYS = ("session",)
# 心跳里 session 的固定形态：8 位十六进制（判据按它钉）。
_SESSION_DIGEST_LEN = 8


def session_digest(value) -> str:
    """会话键 → **8 位十六进制**短哈希（心跳只记它，绝不记会话键原文）。

    用途只有一个：把**同一个会话的多条心跳**串起来，回答"hook 到底跑没跑、
    跑在哪个会话上"。这个用途不需要原文，也不需要更长——32 bit 足够区分。

    空值返回空串（没有会话可哈希时**不编一个**）。
    """
    text = "" if value is None else str(value)
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:_SESSION_DIGEST_LEN]


def _safe_fact(value):
    """把一个 fact 值收敛成**可落盘的安全形态**：短标识原样，其余只留长度+哈希。

    通用规则（设计 §2.3 H1）：超过 32 字符的字符串（token、路径、payload 片段）
    **一律**变成 `len`+`sha8`，所以心跳文件里永远不可能出现原文。
    注意 `session` 不走这里——它由 `session_digest` 强制成 8 位哈希（见 `_FACT_DIGEST_KEYS`）。
    """
    if isinstance(value, bool) or isinstance(value, int) or isinstance(value, float):
        return value
    text = "" if value is None else str(value)
    if _SAFE_FACT_RE.match(text):
        return text
    return {"len": len(text), "sha8": hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]}


def heartbeat(data_dir, *, event, host, **facts) -> bool:
    """写一份心跳（每 `(host, event)` 一个文件，覆盖写）：证明 hook **真的被执行过**。

    为什么需要它：hook 的六处提前 return 都会让"没有输出"和"根本没跑"长得一模一样。
    心跳把这两种情况分开——这是诊断"hook 到底跑没跑"的唯一可靠办法。
    「**看内容，不看 mtime**」：只有 `{"bad_payload": true}` 的那一行是**手工演练**喂坏
    JSON 留下的，不算"事件真的触发过"；真实触发会带 `tool_name` / `payload_len` /
    `prompt_len` / `session` 等字段。
    ⚠️ 2026-10-26 评审 Y8 更正：旧文档把心跳当作"WorkBuddy 不支持某事件就 24h 移除"的
    判据来源（设计 R4）——**那条规则已作废**（用户级实测支持 PreToolUse/UserPromptSubmit）。
    心跳现在的用途只有两个：证明 hook 跑过、以及区分"没有输出"与"根本没跑"。

    **绝不写 payload 原文/内容/凭证**（H1 的唯一修复点）：所有字符串 fact 都过
    `_safe_fact`；`session` 更严——只留 8 位哈希（`session_digest`，QA Round 1 · P3）。
    """
    try:
        folder = Path(data_dir) / HEARTBEAT_RELDIR
        folder.mkdir(parents=True, exist_ok=True)
        now = datetime.datetime.now().astimezone()
        payload = {"ts": now.isoformat(timespec="seconds"),
                   "ts_utc": now.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "event": str(event), "host": str(host)}
        for key, value in sorted(facts.items()):
            if str(key) in _FACT_DIGEST_KEYS:
                payload[str(key)] = session_digest(value)
            else:
                payload[str(key)] = _safe_fact(value)
        name = f"heartbeat-{str(host)}-{str(event)}.json"
        return _write_state(folder / name, payload)
    except OSError:
        return False


# ------------------------------------------------------------------ 清理 / 统计


def prune(conn, *, days: int, now=None) -> int:
    """删掉超过保留期的动作行，返回删除条数。`days<=0` = 不清理（沿用 0=永不的约定）。"""
    keep = int(days or 0)
    if keep <= 0:
        return 0
    cutoff = (_dt(now) - datetime.timedelta(days=keep)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = conn.execute("DELETE FROM tool_activity WHERE seen_at < ?", (cutoff,))
    conn.commit()
    return int(cur.rowcount or 0)


def stats(conn, *, days: int = 1, now=None, asks_path=None) -> dict:
    """动作流的固定读数（`fish activity stats` 与日报共用一份口径）。"""
    window = max(int(days or 1), 1)
    now_dt = _dt(now)
    cutoff = (now_dt - datetime.timedelta(days=window)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute("SELECT COUNT(*) FROM tool_activity WHERE seen_at >= ?",
                        (cutoff,)).fetchone()[0]
    write_rows = conn.execute(
        "SELECT COUNT(*) FROM tool_activity WHERE seen_at >= ? AND action='write'",
        (cutoff,)).fetchone()[0]
    exec_rows = conn.execute(
        "SELECT COUNT(*) FROM tool_activity WHERE seen_at >= ? AND action='exec'",
        (cutoff,)).fetchone()[0]
    sessions = conn.execute(
        "SELECT COUNT(DISTINCT session_key) FROM tool_activity WHERE seen_at >= ?",
        (cutoff,)).fetchone()[0]
    hosts = [r[0] for r in conn.execute(
        "SELECT DISTINCT host FROM tool_activity WHERE seen_at >= ? ORDER BY host",
        (cutoff,)).fetchall()]
    top_dirs = [(r[0], int(r[1])) for r in conn.execute(
        "SELECT COALESCE(path_dir,'(无目录)'), COUNT(*) FROM tool_activity "
        "WHERE seen_at >= ? AND action='write' GROUP BY 1 ORDER BY 2 DESC LIMIT 8",
        (cutoff,)).fetchall()]
    by_hour = {}
    for (ts,) in conn.execute("SELECT seen_at FROM tool_activity WHERE seen_at >= ?",
                              (cutoff,)).fetchall():
        parsed = parse_utc(ts)
        if not parsed:
            continue
        local = parsed + datetime.timedelta(hours=8)
        by_hour[local.hour] = by_hour.get(local.hour, 0) + 1
    asked = 0
    if asks_path:
        try:
            for line in Path(asks_path).read_text(encoding="utf-8").splitlines():
                try:
                    if (json.loads(line).get("ts") or "") >= cutoff:
                        asked += 1
                except ValueError:
                    continue
        except OSError:
            asked = 0
    return {"days": window, "rows": int(rows), "write_rows": int(write_rows),
            "exec_rows": int(exec_rows), "sessions": int(sessions), "hosts": hosts,
            "top_dirs": top_dirs, "asked": int(asked),
            "by_hour": sorted(by_hour.items())}


# ------------------------------------------------------------------ hook 唯一入口


def handle_post_tool_use(conn, *, host, payload, now=None, data_dir=None,
                         snapshot_path=None) -> dict:
    """PostToolUse 的唯一入口：分类 → 落库 → 刷缓存（节流）→ 刷快照（节流）。

    返回诊断 dict（hook 用来打一行 debug）；异常向上抛，由 hook 捕获（失败静默）。

    节流的两层各有理由：
      · `presence` 缓存：写它要 UPDATE 一行，每次写动作都刷是纯浪费；
      · 快照：写一个文件 + os.replace，**每个工具调用都写会明显加重 IO**。
    """
    if not isinstance(payload, dict):
        return {"recorded": False, "reason": "payload 不是对象"}
    if not get_cfg(conn, "activity.enabled"):
        return {"recorded": False, "reason": "activity.enabled=false"}
    session_key = make_session_key(host, payload.get("session_id"))
    if not session_key:
        return {"recorded": False, "reason": "payload 里没有 session_id"}

    seen_at = now or utc_now()
    cwd = payload.get("cwd") or ""
    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    raw_path = _first_path(tool_input)
    path_hint = fish_surface.make_abs(raw_path, cwd) if raw_path else ""
    project, project_root = resolve_project(conn, path_hint, cwd)
    classification = classify_tool_call(
        tool_name, tool_input, cwd=cwd, project_root=project_root, project=project,
        ok=judge_ok(payload.get("tool_response")))
    if not classification:
        return {"recorded": False, "reason": "不是写类动作（读类/其他不落库）",
                "session_key": session_key}

    citizen_id = resolve_citizen(conn, session_key, now=seen_at)
    classification["project"] = project
    row_id = record(conn, host=host, session_key=session_key, citizen_id=citizen_id,
                    classification=classification, seen_at=seen_at)

    result = {"recorded": True, "row_id": row_id, "session_key": session_key,
              "citizen_id": citizen_id, "project": project,
              "action": classification["action"], "path": classification.get("path")}
    if data_dir is None or not citizen_id or not classification.get("path_dir"):
        return result

    folder = Path(data_dir)
    state_path = folder / HEARTBEAT_RELDIR / "state.json"
    state = _read_state(state_path)
    epoch = _now_epoch()

    min_presence = float(get_cfg(conn, "activity.presence_cache_min_interval_sec"))
    try:
        info = auto_surfaces(conn, window_min=int(get_cfg(conn, "activity.aggregate_window_min")),
                             now=seen_at)
        dirs = [d["dir"] for d in (info.get(citizen_id) or {}).get("dirs") or []]
        result["presence_cache"] = refresh_presence_cache(
            conn, citizen_id=citizen_id, project=project, dirs=dirs,
            seen_at=seen_at, min_interval_sec=min_presence)
    except sqlite3.Error as exc:
        result["presence_cache"] = False
        result["presence_error"] = type(exc).__name__

    min_snapshot = float(get_cfg(conn, "activity.snapshot_min_interval_sec"))
    last_snapshot = float(state.get("last_snapshot_epoch") or 0)
    if min_snapshot <= 0 or (epoch - last_snapshot) >= min_snapshot:
        target = Path(snapshot_path) if snapshot_path else folder / SNAPSHOT_RELPATH
        try:
            build_snapshot(conn, out_path=target,
                           window_min=int(get_cfg(conn, "board.overlap_window_min")),
                           now=seen_at)
            result["snapshot"] = True
            state["last_snapshot_epoch"] = epoch
            _write_state(state_path, state)
        except (sqlite3.Error, ValueError) as exc:
            result["snapshot"] = False
            result["snapshot_error"] = type(exc).__name__
    else:
        result["snapshot"] = False
        result["snapshot_throttled"] = True
    return result
