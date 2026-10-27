#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线维护管道：任务台账 + 七个维护任务（34 号 §2.2/§2.3 · T04b）。

为什么要这一层
--------------
fish 的维护过去全是**被动**的：`links` 只在写入时建、`push_log` 只写不清、
`citizens_archive` 长期 0 行（ADR-009 的归档从未被调用）、FTS 只在读的时候
自愈。被动维护的典型症状是"系统看起来正常，但派生数据一点点变旧"。本模块把
这些动作收成一条**可调度、可观测、可跳过**的管道：

    daily_report（23:00 已有调度）
        └─→ run_maintenance(conn, root, deps=…)
              ├─ 台账 maintenance_task（间隔 / 上次状态 / 上次错误 / 上次影响行数）
              └─ 七个任务（顺序固定）

依赖方向（与 `fish_kb_index` 同一条纪律）
----------------------------------------
本模块 **不 import `fish`**：需要门面的六个能力全部由调用方以 `deps` 注入——
`archive_citizens` / `reindex_fts` / `build_links_on_write` / `get_config` /
`corpus_ingest` / `corpus_retag`。理由有两条：

  · `fish` 的调用方（`daily_report`）要 import 本模块，反向 import `fish` 就成环；
  · 台账与任务编排是**纯 SQL + 纯函数**，不该被门面的加载期副作用（MCP 注册、
    hook 安装等）绑架——判据要能在临时库里只跑一只任务。

可以静态 import 的两样东西：`fish_kb_index`（叶子模块，只依赖 `fish_kb` /
`fish_common`）与 `fish_corpus`（语料层门面，判据 J2 同样不 import `fish`）。
`fish_corpus` 只在需要 `DEFAULT_WINDOW_DAYS` 时**局部 import**——那是采集窗口
的唯一口径（3 天，6d7f2ac 定的），复制一个 `3` 出来就是下一个静默漂移点。

corpus 两项是**双层闸门**
--------------------------
台账 `enabled`（种子 0，人工显式开）是第一道，配置键（`corpus.daily_enabled` /
`corpus.daily_retag`，缺省 false）是第二道。两道都开才会真跑；只改配置仍是关着
——这不是遗漏，是"最贵的一句默认"（全量采集是分钟级动作）必须被明确表态两次。
日报里的语料步另有 CLI/环境变量闸门，两者职责不重叠（见 `daily_report.py` 的
`MAINTENANCE_DAILY_ONLY`）。

跳过与失败（三种"没执行"，语义必须分开）
----------------------------------------
  · `skipped(disabled)`           —— 台账里 `enabled=0`（人手动关的）
  · `skipped(interval)`           —— 距上次真实运行不足 `interval_hours`（幂等闸门）
  · `skipped(dep_missing:<name>)` —— 调用方没注入这个依赖（能力不在，不是故障）
  · `skipped(disabled_by_config)` —— 配置说"不自动做"（如 `session.archive_after_days=0`）
  · `error`                       —— 任务真的抛了异常；**只记台账，不阻塞后续任务**

**跳过不写台账的 `last_run_at`**：它是"上次真实运行时刻"，被跳过刷成 now 会让
间隔判断永远往后滚（任务静默饿死）。只有 `ok` / `error` 才更新运行时刻。
"""
import datetime
import json
import os
import sqlite3
import time
from pathlib import Path

import fish_common
import fish_kb_index

# ---------------------------------------------------------------- 常量（口径）

#: 注册表顺序 = 执行顺序 = 台账展示顺序。**不要按字典序重排**：
#: `kb_reindex` 必须在 `fts_verify` 之前（索引先更新，再验 FTS 是否跟上）。
TASK_ORDER = (
    "kb_reindex",
    "links_rebuild",
    "push_log_prune",
    "citizens_archive",
    "fts_verify",
    "corpus_ingest",
    "corpus_retag",
)

#: 任务规格（interval 的出处：34 号 §2.3 表格；corpus 两项见 55 号 §2.2.1）。
TASK_SPECS = {
    "kb_reindex": {
        "interval_hours": 6,
        "enabled": 1,
        "what": "知识库索引增量重建（kb_index + kb_fts）",
    },
    "links_rebuild": {
        "interval_hours": 24,
        "enabled": 1,
        "what": "重算 links：悬空边标 suppressed，最近 7 天新增编码补建边",
    },
    "push_log_prune": {
        "interval_hours": 24,
        "enabled": 1,
        "what": "清理已判定且超 90 天的推送日志 + activity:* 兜底清理",
    },
    "citizens_archive": {
        "interval_hours": 24,
        "enabled": 1,
        "what": "归档 N 天不活跃的非主人公民（可逆的搬行）",
    },
    "fts_verify": {
        "interval_hours": 24,
        "enabled": 1,
        "what": "校验 memory_fts 与 code_index 行数，不等则重建",
    },
    "corpus_ingest": {
        "interval_hours": 24,
        "enabled": 0,       # **默认关**：全量采集是分钟级动作（55 号 §2.2.1）
        "what": "语料采集（窗口 = fish_corpus.DEFAULT_WINDOW_DAYS）",
    },
    "corpus_retag": {
        "interval_hours": 24,
        "enabled": 0,       # **默认关**：只在映射表变更后按需跑
        "what": "按库里 cwd 重算语料会话标签（幂等）",
    },
}

#: 跳过原因（返回值与台账共用；判据按字符串断言，所以集中定义）。
REASON_DISABLED = "disabled"
REASON_INTERVAL = "interval"
REASON_CONFIG = "disabled_by_config"

PUSH_LOG_KEEP_DAYS = 90
ACTIVITY_KEEP_DAYS_DEFAULT = 14
LINK_BACKFILL_DAYS = 7

_TASK_TABLE_DDL = """CREATE TABLE IF NOT EXISTS maintenance_task (
  task_name      TEXT PRIMARY KEY,
  enabled        INTEGER NOT NULL DEFAULT 1,
  interval_hours INTEGER NOT NULL,
  last_run_at    TEXT,
  last_status    TEXT,
  last_error     TEXT,
  last_affected  INTEGER,
  last_seconds   REAL
)"""

#: "active 编码"的唯一口径：`status` 允许为 NULL（老库），NULL 语义就是默认 active。
_SQL_ACTIVE_CODES = "SELECT code FROM code_index WHERE COALESCE(status, 'active') = 'active'"

_LEDGER_COLS = ("task_name", "enabled", "interval_hours", "last_run_at",
                "last_status", "last_error", "last_affected", "last_seconds")


class _Skip(Exception):
    """任务主动跳过（不是故障）：携带原因是给返回值与台账读者看的。"""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = str(reason)


# ---------------------------------------------------------------- 时间工具


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_utc(value):
    """把库里的时间戳（ISO8601 / `Z` / 带偏移 / datetime）解析成 aware datetime。

    解析不了返回 `None` —— 调用方据此把"坏时间戳"当成"从未运行"（宁可多跑一次，
    也不要因为一条脏数据让维护任务永久停摆）。
    """
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z") or text.endswith("z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def _iso_utc(moment):
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _coerce_now(now):
    """`now` 参数：缺省 = 真此刻；显式值必须是 ISO8601 或 datetime（判据要钉时钟）。"""
    if now is None:
        return _utcnow()
    parsed = _parse_utc(now)
    if parsed is None:
        raise ValueError(
            "now 必须是 ISO8601（如 2026-10-27T12:00:00Z）或 datetime，"
            f"收到 {now!r}")
    return parsed


def _within_interval(last_run_at, interval_hours, now_dt):
    """上次运行 + 间隔 > now → 还在间隔内（= 这次跳过）。从未运行 → 立即到期。"""
    last = _parse_utc(last_run_at)
    if last is None:
        return False
    try:
        return last + datetime.timedelta(hours=float(interval_hours)) > now_dt
    except (OverflowError, ValueError):
        return False


def _next_run_at(last_run_at, interval_hours):
    last = _parse_utc(last_run_at)
    if last is None:
        return None
    try:
        return _iso_utc(last + datetime.timedelta(hours=float(interval_hours)))
    except (OverflowError, ValueError):
        return None


# ---------------------------------------------------------------- 台账


def ensure_maintenance_tables(conn):
    """建 `maintenance_task` 并 seed 七个任务（**幂等**）。

    seed 用 `INSERT OR IGNORE`：已存在的行**一个字段都不动**——运维手工调过的
    `interval_hours` / `enabled` 不该被下一次启动覆盖回默认值。

    返回 `{"created": bool, "seeded": n, "tasks": 7}`：`seeded` 只数本次真正
    新插入的行（判据据此证明"第一次 7、第二次 0"）。
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='maintenance_task'"
    ).fetchone()
    conn.execute(_TASK_TABLE_DDL)
    seeded = 0
    for name in TASK_ORDER:
        spec = TASK_SPECS[name]
        cur = conn.execute(
            "INSERT OR IGNORE INTO maintenance_task (task_name, enabled, interval_hours) "
            "VALUES (?, ?, ?)",
            (name, int(spec["enabled"]), int(spec["interval_hours"])))
        seeded += max(int(cur.rowcount or 0), 0)
    conn.commit()
    return {"created": not bool(exists), "seeded": seeded, "tasks": len(TASK_ORDER)}


def _ledger_row(conn, name):
    """读一行台账；行被手工删了就地补回（自愈），保证任务永远有登记。"""
    fields = ", ".join(_LEDGER_COLS)
    row = conn.execute(
        f"SELECT {fields} FROM maintenance_task WHERE task_name=?", (name,)).fetchone()
    if row is None:
        spec = TASK_SPECS[name]
        conn.execute(
            "INSERT OR IGNORE INTO maintenance_task (task_name, enabled, interval_hours) "
            "VALUES (?, ?, ?)",
            (name, int(spec["enabled"]), int(spec["interval_hours"])))
        conn.commit()
        row = conn.execute(
            f"SELECT {fields} FROM maintenance_task WHERE task_name=?", (name,)).fetchone()
    return dict(zip(_LEDGER_COLS, row))


def _record_run(conn, name, *, status, error, affected, seconds, at):
    conn.execute(
        "UPDATE maintenance_task SET last_run_at=?, last_status=?, last_error=?, "
        "last_affected=?, last_seconds=? WHERE task_name=?",
        (_iso_utc(at), status, error, int(affected or 0), float(seconds or 0.0), name))
    conn.commit()


def maintenance_status(conn, *, now=None):
    """台账全貌（供 CLI/日报/判据读）：七个任务 + 到期判断 + 上次读数。

    `now`：判定到期的时间基准（缺省 = 真 UTC now）。**测试必须传固定 NOW**——
    否则"跑完立刻 due=False"的断言会在 NOW + interval 之后变成红（时间敏感）。
    """
    ensure_maintenance_tables(conn)
    now_dt = now if now is not None else _utcnow()
    tasks = []
    for name in TASK_ORDER:
        row = _ledger_row(conn, name)
        interval = int(row["interval_hours"])
        enabled = bool(row["enabled"])
        tasks.append({
            "name": name,
            "enabled": enabled,
            "interval_hours": interval,
            "last_run_at": row["last_run_at"],
            "last_status": row["last_status"],
            "last_error": row["last_error"],
            "last_affected": row["last_affected"],
            "last_seconds": row["last_seconds"],
            "due": enabled and not _within_interval(row["last_run_at"], interval, now_dt),
            "next_run_at": _next_run_at(row["last_run_at"], interval),
            "what": TASK_SPECS[name]["what"],
        })
    return {
        "tasks": tasks,
        "count": len(tasks),
        "enabled_count": sum(1 for t in tasks if t["enabled"]),
        "erroring": [t["name"] for t in tasks if t["last_status"] == "error"],
        "now": _iso_utc(now_dt),
    }


def set_task_enabled(conn, name, enabled):
    """开/关一个任务（`enabled=0` 时 `run_maintenance` 记 `skipped(disabled)`）。"""
    if name not in TASK_SPECS:
        raise ValueError(
            f"未知维护任务：{name!r}（可用：{', '.join(TASK_ORDER)}）")
    ensure_maintenance_tables(conn)
    value = 1 if _as_bool(enabled) else 0
    conn.execute("UPDATE maintenance_task SET enabled=? WHERE task_name=?", (value, name))
    conn.commit()
    return {"task_name": name, "enabled": value}


# ---------------------------------------------------------------- 依赖注入


def _dep(deps, name, *, required=True):
    """取一个注入的依赖；缺失/不可调用 → `_Skip(dep_missing:<name>)`。"""
    dep = deps.get(name)
    if callable(dep):
        return dep
    if required:
        raise _Skip(f"dep_missing:{name}")
    return None


def _read_config(deps, key, default):
    """读配置的**唯一入口**：`get_config` 依赖的缺失与未知键都要有明确语义。"""
    getter = _dep(deps, "get_config")
    try:
        value = getter(key)
    except Exception:                     # noqa: BLE001 —— 未知键/脏值按缺省（保守）
        return default
    return default if value is None else value


def _cfg_required(deps, key, default):
    """配置**就是**任务的闸门时用（citizens / corpus）：拿不到配置 = 能力缺失。"""
    return _read_config(deps, key, default)


def _cfg_optional(deps, key, default):
    """配置只影响任务的**一段**时用（push_log 的 activity 段）：拿不到就用缺省。"""
    getter = deps.get("get_config")
    if not callable(getter):
        return default
    return _read_config(deps, key, default)


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_COUNT_TABLES = ("code_index", "memory_fts")


def _count(conn, table):
    """表名白名单（安全审计 ⚪-13：表名走 f-string，当前无外部可达，但不给后来人留口子）。"""
    if table not in _COUNT_TABLES:
        raise ValueError(f"_count 只允许 {', '.join(_COUNT_TABLES)}：{table!r}")
    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0] if row else 0)


def _read_body(root, file_path):
    """读正文文件（供补建边用）。**读不到返回空串**：边是派生数据，不该因此报错。

    安全审计 🟡-5：加 containment——`file_path` 只允许落在 root 之内
    （realpath + commonpath）；越界按"读不到"处理（不报错，但也不读）。
    今天三个写入方都只写相对归档路径，这是一层几乎免费的纵深。
    """
    if not file_path or root is None:
        return ""
    path = Path(file_path)
    if not path.is_absolute():
        path = Path(root) / file_path
    try:
        real = Path(os.path.realpath(str(path)))
        root_real = Path(os.path.realpath(str(root)))
        if os.path.commonpath([str(real), str(root_real)]) != str(root_real):
            return ""
        return real.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return ""


# ---------------------------------------------------------------- 七个任务


def _task_kb_reindex(conn, root, deps, now_dt):
    """`kb_reindex`（6h）：调 `fish_kb_index.rebuild_kb_index(mode='incremental')`。

    直接 import 而不是走 deps：`fish_kb_index` 是叶子模块（不 import `fish`），
    把它做成可注入的依赖只会给调用方多一条"忘传就静默跳过"的路。
    """
    res = fish_kb_index.rebuild_kb_index(conn, root, mode="incremental")
    affected = (_as_int(res.get("added")) + _as_int(res.get("updated"))
                + _as_int(res.get("deleted")))
    detail = {key: res.get(key) for key in
              ("vaults", "scanned_files", "notes", "added", "updated", "deleted",
               "unchanged", "errors", "offline", "sensitive_skipped", "seconds")}
    return affected, detail


def _task_links_rebuild(conn, root, deps, now_dt):
    """`links_rebuild`（24h）：悬空边标 `suppressed=1`，恢复两端都 active 的边，补建新边。

    **不物理删边**（34 号 §2.3.1 / Graphiti 的 `t_invalid` 同构）：被 supersede 的
    记忆留下的边是历史，删掉就再也解释不清"这条边当时为什么存在"。

    **恢复只按"两端都 active"判定**：当前代码里 `suppressed` 的唯一写入方就是本
    任务（写入路径只建边、不抑制），所以不存在"把人工抑制误放回"的路径；将来若
    新增别的抑制来源，必须在 `links` 上加来源列，否则这条 UPDATE 会越权。
    """
    suppressed = conn.execute(
        "UPDATE links SET suppressed = 1 "
        "WHERE COALESCE(suppressed, 0) = 0 "
        f"AND (src NOT IN ({_SQL_ACTIVE_CODES}) OR dst NOT IN ({_SQL_ACTIVE_CODES}))"
    ).rowcount
    restored = conn.execute(
        "UPDATE links SET suppressed = 0 "
        "WHERE suppressed = 1 "
        f"AND src IN ({_SQL_ACTIVE_CODES}) AND dst IN ({_SQL_ACTIVE_CODES})"
    ).rowcount
    conn.commit()

    detail = {
        "suppressed": int(suppressed or 0),
        "restored": int(restored or 0),
        "edge_backfill": "ran",
        "backfill_codes": 0,
        "edges_created": 0,
        "backfill_errors": 0,
        "backfill_error": None,
    }
    builder = _dep(deps, "build_links_on_write", required=False)
    edges = 0
    if builder is None:
        detail["edge_backfill"] = "skipped_dep"
    else:
        cutoff = _iso_utc(now_dt - datetime.timedelta(days=LINK_BACKFILL_DAYS))
        rows = conn.execute(
            "SELECT code, title, file_path FROM code_index "
            "WHERE COALESCE(status, 'active') = 'active' "
            "  AND COALESCE(NULLIF(recorded_at, ''), NULLIF(occurred_at, ''), '') >= ? "
            "ORDER BY code", (cutoff,)).fetchall()
        detail["backfill_codes"] = len(rows)
        for code, title, file_path in rows:
            body = _read_body(root, file_path)
            try:
                stats = builder(conn, code, title=title or "", body=body) or {}
            except Exception as exc:      # noqa: BLE001 —— 单条补建失败只计数
                detail["backfill_errors"] += 1
                if detail["backfill_error"] is None:
                    detail["backfill_error"] = f"{code}: {type(exc).__name__}: {exc}"
                continue
            edges += sum(int(value) for value in stats.values()
                         if isinstance(value, int) and not isinstance(value, bool))
    detail["edges_created"] = edges

    affected = int(suppressed or 0) + int(restored or 0) + edges
    return affected, detail


def _task_push_log_prune(conn, root, deps, now_dt):
    """`push_log_prune`（24h）：两段删除，规则各自独立。

      ① 已判定（`reaction IS NOT NULL`）且 `pushed_at < now-90d` 且**不是** `kb:%`
         —— 知识库推送留作度量，不能和普通推送一起清；
      ② `activity:*` 兜底清理（写入端垃圾面的最后一道），保留期取
         `activity.retention_days`（缺省 14；`<1` = 不清理该段）。
    """
    cutoff = _iso_utc(now_dt - datetime.timedelta(days=PUSH_LOG_KEEP_DAYS))
    judged = conn.execute(
        "DELETE FROM push_log "
        "WHERE reaction IS NOT NULL AND pushed_at < ? "
        "  AND COALESCE(code, '') NOT LIKE 'kb:%'", (cutoff,)).rowcount
    detail = {"reaction_rows": int(judged or 0), "reaction_cutoff": cutoff,
              "keep_days": PUSH_LOG_KEEP_DAYS}

    keep_days = _as_int(_cfg_optional(deps, "activity.retention_days",
                                      ACTIVITY_KEEP_DAYS_DEFAULT),
                        ACTIVITY_KEEP_DAYS_DEFAULT)
    detail["activity_keep_days"] = keep_days
    activity = 0
    if keep_days < 1:
        detail["activity_skipped"] = "retention_days<1"
    else:
        activity_cutoff = _iso_utc(now_dt - datetime.timedelta(days=keep_days))
        activity = conn.execute(
            "DELETE FROM push_log WHERE code LIKE 'activity:%' AND pushed_at < ?",
            (activity_cutoff,)).rowcount
        detail["activity_cutoff"] = activity_cutoff
    detail["activity_rows"] = int(activity or 0)
    # 安全审计 🟡-9（2026-10-27）：破坏性动作留痕（与 citizens_archive 同标准，
    # 也满足"抵赖"威胁面）。本模块按 J2 不 import fish，所以按 events 表的最小结构
    # 直接写；event_id 用已下沉的 `fish_common.ulid()`。
    try:
        conn.execute(
            "INSERT OR IGNORE INTO events (event_id, ts, type, actor, subject, payload, "
            "cause, refs, schema) VALUES (?,?,?,?,?,?,?,?,?)",
            (fish_common.ulid(), _iso_utc(now_dt), "maintenance.pruned",
             json.dumps({"system": "maintenance"}, ensure_ascii=False), "push_log",
             json.dumps(detail, ensure_ascii=False, sort_keys=True),
             None, None, "evt@1"))
    except sqlite3.Error:
        pass
    conn.commit()
    return int(judged or 0) + int(activity or 0), detail


def _task_citizens_archive(conn, root, deps, now_dt):
    """`citizens_archive`（24h）：`0` = 不自动归档（36 号裁定的无限期语义）。

    `archive_citizens` 对 `days<1` 会**响亮拒绝**，所以"0 就跳过"的责任在这里：
    先把 0 判掉，再决定要不要取归档依赖。
    """
    days = _as_int(_cfg_required(deps, "session.archive_after_days", 0), 0)
    if days < 1:
        raise _Skip(REASON_CONFIG)
    archive = _dep(deps, "archive_citizens")
    res = archive(conn, days=days) or {}
    affected = _as_int(res.get("archived_count"))
    detail = {
        "days": days,
        "cutoff": res.get("cutoff"),
        "checked": _as_int(res.get("checked")),
        "planned": _as_int(res.get("planned")),
        "archived_count": affected,
        "skipped": len(res.get("skipped") or []),
    }
    return affected, detail


def _task_fts_verify(conn, root, deps, now_dt):
    """`fts_verify`（24h）：`memory_fts` 行数 vs `code_index` 行数，不等则全量重建。

    为什么不"每次都重建"：重建是 O(全库) 且要读全部正文文件；行数相等是**便宜的
    必要条件**，用它当闸门能把"每天一次全量重建"降到"只在真的对不上时"。
    """
    code_rows = _count(conn, "code_index")
    missing = False
    try:
        fts_rows = _count(conn, "memory_fts")
    except sqlite3.Error:
        fts_rows, missing = 0, True
    detail = {"code_rows": code_rows, "fts_rows": fts_rows,
              "memory_fts_missing": missing, "rebuilt": 0}
    if not missing and fts_rows == code_rows:
        return 0, detail
    reindex = _dep(deps, "reindex_fts")
    rebuilt = _as_int(reindex(conn, root))
    detail["rebuilt"] = rebuilt
    return rebuilt, detail


def _corpus_window_days():
    """采集窗口 = `fish_corpus.DEFAULT_WINDOW_DAYS`（不复制数字）。"""
    import fish_corpus                   # noqa: PLC0415 —— 只为读口径常量
    return _as_int(fish_corpus.DEFAULT_WINDOW_DAYS)


def _task_corpus_ingest(conn, root, deps, now_dt):
    """`corpus_ingest`（24h，**默认关**）：开关 `corpus.daily_enabled`。

    `days` **必须显式传**：`fish_corpus.ingest()` 的函数默认是 `None`（不限），
    漏传等于把几个月历史重新投影一遍（6d7f2ac 的口径）。
    """
    if not _as_bool(_cfg_required(deps, "corpus.daily_enabled", False)):
        raise _Skip(REASON_CONFIG)
    ingest = _dep(deps, "corpus_ingest")
    days = _corpus_window_days()
    res = ingest(root=root, days=days) or {}
    affected = _as_int(res.get("segments_new"))
    detail = {
        "days": days,
        "segments_new": affected,
        "sessions_seen": _as_int(res.get("sessions_seen")),
        "busy": bool(res.get("busy")),
        "degraded": len(res.get("degraded") or []),
        "orphan": len(res.get("orphan") or []),
        "lag_sec": res.get("lag_sec"),
        "stall": bool((res.get("stall") or {}).get("alarm")),
    }
    return affected, detail


def _task_corpus_retag(conn, root, deps, now_dt):
    """`corpus_retag`（24h，**默认关**）：开关 `corpus.daily_retag`。

    没有这个配置键时按默认关处理（`_read_config` 会把未知键归到缺省）——
    "键不存在"和"键=false"都表示**没人要求重算**，不是故障。
    """
    if not _as_bool(_cfg_required(deps, "corpus.daily_retag", False)):
        raise _Skip(REASON_CONFIG)
    retag = _dep(deps, "corpus_retag")
    res = retag(root=root) or {}
    affected = _as_int(res.get("changed"))
    detail = {
        "sessions": _as_int(res.get("sessions")),
        "changed": affected,
        "kept_mapped": _as_int(res.get("kept_mapped")),
        "kept_identity": _as_int(res.get("kept_identity")),
        "warnings": len(res.get("warnings") or []),
    }
    return affected, detail


_TASKS = {
    "kb_reindex": _task_kb_reindex,
    "links_rebuild": _task_links_rebuild,
    "push_log_prune": _task_push_log_prune,
    "citizens_archive": _task_citizens_archive,
    "fts_verify": _task_fts_verify,
    "corpus_ingest": _task_corpus_ingest,
    "corpus_retag": _task_corpus_retag,
}


# ---------------------------------------------------------------- 驱动


def _selected_tasks(only):
    """`only` 过滤：支持单个名字或列表；**顺序永远按注册表**（传反了也不影响）。"""
    if only is None:
        return list(TASK_ORDER)
    if isinstance(only, str):
        only = [only]
    wanted = [str(name) for name in only]
    unknown = [name for name in wanted if name not in TASK_SPECS]
    if unknown:
        raise ValueError(
            f"未知维护任务：{', '.join(unknown)}（可用：{', '.join(TASK_ORDER)}）")
    seen = set(wanted)
    return [name for name in TASK_ORDER if name in seen]


def _skipped(name, reason, interval_hours):
    return {
        "name": name,
        "status": "skipped",
        "affected": 0,
        "seconds": 0.0,
        "error": None,
        "reason": reason,
        "detail": {"reason": reason, "interval_hours": int(interval_hours)},
    }


def run_maintenance(conn, root, *, only=None, now=None, deps=None, force=False):
    """跑一遍维护管道，返回 `{tasks, ran, skipped, errors, now}`。

    ## 判定顺序（每个任务）

      1. `enabled=0` → `skipped(disabled)`（`force` 也不越过它：关掉就是关掉）
      2. `last_run_at + interval_hours > now` 且 `not force` → `skipped(interval)`
      3. 执行；任务内部可以说"跳过"（`_Skip`，如配置关闭 / 依赖缺失）
      4. 异常 → `status='error'`、写台账，**继续下一个任务**

    ## 台账写入

      只有 `ok` / `error` 更新 `last_run_at`（跳过不推进时钟，否则任务会被自己的
      skipped 永久饿死）。`skipped` 的原因只出现在返回值里。

    ## `deps` 键

      `archive_citizens(conn, days=N)` / `reindex_fts(conn, root)` /
      `build_links_on_write(conn, code, title=…, body=…)` / `get_config(key)` /
      `corpus_ingest(root=…, days=…)` / `corpus_retag(root=…)`。

    ## 返回

      `tasks[*] = {name, status, affected, seconds, error, reason?, detail}`
      `ran` = 真正执行过的任务数（ok + error）；`skipped` = 跳过数；`errors` = error 数。
    """
    ensure_maintenance_tables(conn)
    now_dt = _coerce_now(now)
    dep_map = dict(deps or {})
    names = _selected_tasks(only)
    root_path = Path(root) if root is not None else None

    records = []
    for name in names:
        row = _ledger_row(conn, name)
        interval = _as_int(row["interval_hours"], 0)
        if not int(row["enabled"]):
            records.append(_skipped(name, REASON_DISABLED, interval))
            continue
        if not force and _within_interval(row["last_run_at"], interval, now_dt):
            records.append(_skipped(name, REASON_INTERVAL, interval))
            continue

        started = time.perf_counter()
        try:
            affected, detail = _TASKS[name](conn, root_path, dep_map, now_dt)
            status, error = "ok", None
        except _Skip as exc:
            records.append(_skipped(name, exc.reason, interval))
            continue
        except Exception as exc:          # noqa: BLE001 —— 单任务异常不阻塞后续
            status, error = "error", f"{type(exc).__name__}: {exc}"
            affected, detail = 0, {}
        seconds = round(time.perf_counter() - started, 3)
        _record_run(conn, name, status=status, error=error,
                    affected=affected, seconds=seconds, at=now_dt)
        records.append({
            "name": name,
            "status": status,
            "affected": _as_int(affected),
            "seconds": seconds,
            "error": error,
            "detail": detail,
        })

    ran = sum(1 for item in records if item["status"] in ("ok", "error"))
    errors = sum(1 for item in records if item["status"] == "error")
    return {
        "tasks": records,
        "ran": ran,
        "skipped": len(records) - ran,
        "errors": errors,
        "now": _iso_utc(now_dt),
        "only": [item["name"] for item in records],
    }
