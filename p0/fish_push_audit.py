#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推送面板的数据面：**只读诊断** + **窄写配置** — p0/fish_push_audit.py

## 这个文件解决的问题

设置页的「推送」二级页要看两件事，而它们此前都没有可复用的实现：

  1. **推送到底有没有用**：总量/采纳率/盲区消除率/违规率（这四个率是 `fish.push_stats`
     的），加上**为什么没用**的诊断——收件人项目未知率、开放词表占比、artifact 占比、
     同一会话同一天同一编码的重复推送、以及被打扰最多的 Top offenders；
  2. **想调一下**：`push.*` 里有 7 个键是「口味」而不是「架构」（频率、开关、预算、
     两条下限），改它们不需要改代码；但写口必须**窄**：白名单 + 类型/范围 + 审计。

## 三条纪律（与 `fish_workbench_api.py` / `fish_ui.py` 同构，不另立一套）

1. **读**走 `fish_ui.open_readonly`（`mode=ro` + `PRAGMA query_only=1`）——物理上写不了，
   本模块的只读函数**不含任何写语句**。
2. **写**只开在 `write_config` / `apply_preset` 内部的一条**独立写连接**
   （`sqlite3.connect(timeout=15)` + `busy_timeout=15000`），且只允许
   `WRITABLE_KEYS` 里的 7 个键；未知/只读键一律 `BadKey`，值越界一律 `BadValue`。
   `push.enabled` 与 `push.factors/rrf/mmr/rerank/corpus_enabled` **不可写**，只显示
   （`push.enabled` 在 DSH 上设 false 会连 `tool_activity` 采集一起停，见
   `fish_push_hook.py:138` —— 那是「把仪表盘关掉」不是「调安静」）。
3. **审计**：每次成功写入追加一行到 `<data>/_audit/push-config.jsonl`：
   `{ts, actor, key, old, new}`（预设写入额外带 `preset`）。审计写失败**不阻断主写**
   （配置已经生效了，回滚更糟），但必须 `fish_common.warn_derived` 出声。
4. **串行化**：`write_config` / `apply_preset` 的「读 old → 写库 → 追加审计」整段在
   模块级 `_WRITE_LOCK` 里。审计链的 `old == 上一行 new` 是它唯一的可核性，而**读 old**
   与**追加审计**都在库外——不串行化，两个并发写就会各读同一个 old，链断（评审 🟡-1）。
   预设另外做到「先全部校验、再逐键写」：写到一半失败时，已应用的键仍按行落审计。

## 口径（要么复用，要么写在这里，不许第三处）

- `totals` **整段复用** `fish.push_stats(conn, days=days, now=now)`：采纳率的分母是
  「已判」（accepted+ignored），盲区消除率 7 天窗口、违规率 5 分钟窗口都写在那里面。
  这里**不重算**——重算就是第二份领域实现，两份迟早漂开。
- `channels/kinds/topCodes/diagnostics` 用 SQL。核对这些数时要注意：**推送行**是
  `push_log` 的行，`code` 可能没有 `code_index` 行（看板/知识库/工具动作的伪编码），
  所以 kinds 的判定顺序是「mem_type 优先 → 前缀兜底 → 其余归 memory」。
- `(none)` 归 `memory`：实测本库 `code_index.mem_type` 里**没有** `memory` 这个值
  （记忆条目一律是被推内容之外的默认类别），所以「认不出类型」的行就是普通记忆推送。

## 二级拆解（`breakdown`，2026-11-24 第二轮）

`overview.data.breakdown` 回答「这些推送到底是些什么东西」，按**两条腿**拆：

- **内容源** = 记忆（按类型 / 项目 / 通道 / 开放词表占比）+ 知识库（按顶层目录 / 通道 /
  top 文件）+ 语料（`push.corpus_enabled` 现场关闭；`total` 是窗口内 `conv:` 历史行数）；
- **态势源** = 看板（按 `push_log.project` / 看板 label）+ 动作流（按目录）+ git（按仓库）。

口径四条（写在这里，别处不许另算一份）：

1. **分组和必须等于组 total**：每组按 `total` 降序取前 8 行，其余合成一行
   `{"key":"__other__","label":"其他（N 项）"}` —— 合并行也带 accepted/ignored/pending，
   所以 `sum(rows.total) == 组 total` 在截断之后仍然成立（判据 V-31 查的就是这个）。
2. **记忆 = `kinds` 里 memory/artifact/episode/fact/rule/lesson**（即非 board/kb/activity/git）；
   `byType` 用 `ci.mem_type`（空/认不出 → `unknown`/未分类），`byProject` 用 `ci.project`
   （空 → 未归属），`byChannel` 三通道。`conv:` 前缀按既有契约归 memory（本库 18 条）。
3. **开放词表占比** 的 `pushes`/`codes` **复用** `diagnostics.openLoop`（同一个数不重算），
   只有分母换成 `memory.total`。
4. **语料是「现场关闭 + 历史行单列」**：`enabled` 读 `push.corpus_enabled`，`total` 是窗口内
   `conv:` 行数（关闭前推过多少就显示多少，**不拿 0 冒充「没有」**）。

`share` = 行 total / 组 total（0..1）；组 total 为 0 时没有行（空组返回 `[]`，不是 500）。

## 缓存

`overview` 30 天要跑一遍 `push_stats`（实测 0.24 s / 6 195 行）+ 若干聚合，同一屏里
「切时间窗 / 写完再读一遍 / 手动点刷新」会连着打同一个 `(库, days)`，所以按 `(库, days)`
缓存 15 s。

**这份缓存不是为了给轮询看**：推送页**没有自动轮询**（它是「看一眼、调一下」的面，
见 `docs/109` §六.5），缓存只为吃掉同一秒里连续的重复读。

**`asOf` 与缓存时间戳必须是同一个时刻（`built_ts`）**：构建前取一次 `time.time()`，
`_build_overview` 的 `now` 由它派生，`asOf` / `cachedAt` 都写成它、`cacheAgeSec=0`，
并用 `_cache_put(..., built_ts=built_ts)` 把它一起存进缓存；命中时 `asOf` / `cachedAt`
仍取它，只有 `cacheAgeSec` 随真实时间变大。

为什么强调「同一个时刻」：之前这里是两个时刻——`asOf` 取构建**开始**（`now`），缓存戳取
构建**结束**（`_cache_put` 里的 `time.time()`）。构建实测 0.32–0.78 s，跨整秒时同一个
进程里连读两次会看到 `asOf` 从 `12:16:22Z` 跳到 `12:16:23Z`（`cacheAgeSec` 0.0 → 1.3），
即「数据没变、时刻却变了」。这不是显示问题：V-30 的判据正是「命中缓存时 asOf 不变」，
两个时刻让它在 33 条验收里 5 次只绿 3 次（靠运气）。

命中缓存时 `asOf` **仍是缓存构建时刻**（另给 `cachedAt` 同值 + `cacheAgeSec` 表示这份
数据放了多久）。之前这里是 `payload["asOf"] = _iso(now)`——拿「此刻」冒充数据时刻，
一屏 15 秒前的旧数字会自称「刚刚」（评审 🟡-2）。
**写成功后整片失效**（否则面板会拿着旧 config 说「还是 10」，用户以为没保存）。

## 边界（如实写）

- 只读主库，不读语料库（`corpus.sqlite` 是另一份文件，推送面板不看语料内容）。
- `duplicates` 的「天」按 `pushed_at` 前 10 位（UTC 日期）切——与库里时间戳的存法一致；
  跨时区的「同一天」不重算（那需要逐行转本地时区，且会让读数随查看者的时区变）。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import fish_common
import fish_db
import fish_ui

# ── 口径常量 ────────────────────────────────────────────────────────────
DEFAULT_DAYS = 30
DAYS_MIN = 1
DAYS_MAX = 365
EVENTS_LIMIT_DEFAULT = 20
EVENTS_LIMIT_MAX = 100
TOP_CODES_LIMIT = 12
CACHE_TTL_S = 15.0

#: 通道词的唯一真相在 `fish_common.PUSH_CHANNELS`（**同一个对象**，不是副本）。
#: 顺序即 UI 的显示顺序；历史词 `hook`/`situational` 只做写入兼容与读端识别，
#: 不进这份 UI 词表（聚合时追加成额外行并标 `legacy=True`，见 112 W-B · A1）。
CHANNELS = fish_common.PUSH_CHANNELS
#: `events` 允许的 reaction 过滤值。**`pending` = 待判**（库里 `reaction IS NULL`）。
#:
#: 为什么不是空串（评审 🟠-2）：`parse_qs` 默认把 `reaction=` 丢成「没有这个参数」，
#: 前端 `buildUrl` 也跳过空串——「空串=待判」这条契约在 HTTP 上根本到不了，UI 里也就
#: 永远没有「只看待判」这条路。显式哨兵 `pending` 不改任何一端的全局语义，且**能到**。
REACTIONS = ("pending", "accepted", "ignored")

#: kinds 的显示顺序。`memory` 同时是「认不出类型」的兜底桶（见文件头口径）。
KINDS = ("memory", "artifact", "episode", "fact", "rule", "lesson",
         "board", "kb", "activity", "git")
#: 没有 `code_index` 行时，按这些前缀归类（其余前缀一律归 memory）。
CODE_PREFIX_KINDS = ("board", "kb", "activity", "git")

#: 「二级拆解」每组最多显示几行；超过就取前 8 行、其余合成一行 `__other__`。
#: 为什么是 8：1280 宽下两列各约 480 px，8 行是一个子组不滚一屏看得完的上限。
BREAKDOWN_TOP = 8
#: 「态势源」三类（看板 / 动作流 / git）。其余 kind 都是「内容源」。
SITUATION_KINDS = ("board", "activity", "git")
#: 「内容源 · 记忆」的 kind（`memory` 是「认不出类型」的兜底桶；`conv:` 也落在这里）。
#: 从 `KINDS` 派生而不是另抄一遍：`KINDS` 加一类时这里自动跟着走。
MEMORY_KINDS = tuple(kind for kind in KINDS if kind not in ("kb",) + SITUATION_KINDS)

AUDIT_DIRNAME = "_audit"
AUDIT_FILENAME = "push-config.jsonl"
ACTOR_DEFAULT = "workbench"

#: 写口的**唯一串行闸**：`write_config` / `apply_preset` 的「读 old → 写库 → 追加审计」
#: 必须在同一临界区里。
#:
#: 为什么不能只靠 SQLite：库那一步（`set_config`）自己确实是原子的，但**读 old** 和
#: **写审计**都在库外。两个并发写会各自读到同一个 old，于是审计链里出现「两条 old 都等于
#: X」——而 `old == 上一行 new` 正是这份审计唯一的可核性（它要回答「谁把它改成现在这样」）。
#: 同理，审计是**追加**在提交之后：不加锁时追加顺序还可能和提交顺序相反。
#: 开销可忽略：配置写入是低频动作（人工点保存 / 点预设），串行化换的是可核的审计链。
_WRITE_LOCK = threading.Lock()

# ── 白名单（唯一真相：可写的 7 个键 + 范围 + 人话说明）──────────────────
#: `(key, label, min, max, help)`；类型取自 `fish_common.CONFIG_DEFAULTS` 的登记，
#: **不在这里另写一份**（写两份就会漂：注册成 float 的键在别处被当 int 夹）。
WRITABLE_SPECS = (
    ("push.after_tool_every_n_calls", "工具后推送频率", 1, 9999,
     "每 N 次工具调用最多推一次。调大 = 更安静；9999 等于事实停掉（预设「止血」用它）"),
    ("push.prompt_enabled", "用户消息边界也推（开场提示）", None, None,
     "关掉 = 用户发消息后不再主动提示（工具后推送不受影响）"),
    ("push.prompt_max_items", "一次提示最多几条", 1, 5,
     "提示块里最多塞几条候选。调大 = 更吵"),
    ("push.prompt_min_interval_sec", "两次提示的最短间隔（秒）", 0, 3600,
     "在这个窗口内不重复提示。0 = 不限"),
    ("push.open_budget_tokens", "开场推送预算（token）", 0, 2000,
     "开场注入包里留给推送的预算。0 = 不推（预设「止血」用它）"),
    ("push.score_floor", "分数下限", 0.0, 1.0,
     "低于这个分的候选不推。0 = 不设下限（现状 0.15 是手调出来的）"),
    ("push.value_floor", "价值下限（importance×relevance）", 0.0, 1.0,
     "按 重要度×相关度 卡一刀。0 = 关"),
)

WRITABLE_KEYS = tuple(spec[0] for spec in WRITABLE_SPECS)
WRITABLE_BY_KEY = {
    spec[0]: {"key": spec[0], "label": spec[1], "min": spec[2], "max": spec[3],
              "help": spec[4], "writable": True}
    for spec in WRITABLE_SPECS
}

#: 只显示、不可写的键（顺序即 UI 顺序）。`push.enabled` 单独给一句为什么。
READONLY_SPECS: tuple = (
    ("push.enabled", "总开关（本页只读）",
     "在 DSH 上设 false 会连 tool_activity 采集一起停（fish_push_hook.py:138），"
     "不要在这里一键关。要止血请用下面的「止血预设」。"),
    ("push.factors_enabled", "多因子打分（只读）", "只显示，不在本页写：改它要重新校准权重"),
    ("push.rrf_enabled", "RRF 融合（只读）", "只显示，不在本页写"),
    ("push.mmr_enabled", "MMR 去重（只读）", "只显示，不在本页写：会影响候选集的多样性"),
    ("push.rerank_enabled", "重排（只读）", "只显示，不在本页写"),
    ("push.corpus_enabled", "语料候选（只读）", "只显示，不在本页写：语料库是另一份文件"),
)
READONLY_KEYS = tuple(spec[0] for spec in READONLY_SPECS)
READONLY_KEY_SET = frozenset(READONLY_KEYS)

#: 预设（用户 2026-11-24 拍板的两个动作，见 docs/109-推送面板-设计与验收-20261124.md）。键必须都在白名单里。
PRESETS = {
    "pause": {
        "push.after_tool_every_n_calls": 9999,
        "push.prompt_enabled": False,
        "push.open_budget_tokens": 0,
    },
    "resume": {
        "push.after_tool_every_n_calls": 10,
        "push.prompt_enabled": True,
        "push.open_budget_tokens": 450,
    },
}
PRESET_NAMES = {"pause": "止血预设", "resume": "恢复默认"}


class BadKey(ValueError):
    """未知键、或键已登记但不允许本页写 → HTTP 400 `bad_key`。"""


class BadValue(ValueError):
    """值类型/范围不合法 → HTTP 400 `bad_value`。"""


# ── 时间戳（与库里一致：只到秒、UTC、`Z` 结尾）──────────────────────────
def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ── 小缓存（键含库 + days；写成功后整片失效）────────────────────────────
_CACHE: dict[tuple, tuple[float, dict]] = {}
_CACHE_LOCK = threading.Lock()


def invalidate_cache() -> None:
    """写成功后清空：否则面板拿着 15 秒前的 config 说「还是 10」。"""
    with _CACHE_LOCK:
        _CACHE.clear()


def _cache_get(key: tuple):
    """命中返回 `(构建时刻 epoch, 载荷)`；未命中/过期返回 `None`。

    为什么把构建时刻也交出去：`asOf` 必须是**数据时刻**。只回载荷的话，调用方手上只有
    「现在」，一不小心就会拿 `time.time()` 顶上去，缓存年龄就此消失（评审 🟡-2）。
    """
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if not hit:
        return None
    ts, value = hit
    if time.time() - ts > CACHE_TTL_S:
        return None
    return hit


def _cache_put(key: tuple, value: dict, *, built_ts: float | None = None) -> None:
    """登记缓存。`built_ts` = 这份载荷的**构建时刻**（缺省取现在，兼容旧调用）。

    调用方必须把「构建开始前那一次 `time.time()`」传进来：`asOf` 与缓存时间戳要是同一个
    时刻，否则构建跨整秒时命中缓存会看到 `asOf` 自己往后跳 1 s（QA V-30 / 文件头「缓存」）。
    """
    with _CACHE_LOCK:
        _CACHE[key] = (time.time() if built_ts is None else float(built_ts), value)


# ── 连接 ────────────────────────────────────────────────────────────────
def _read_conn(db_path: str) -> sqlite3.Connection:
    """只读连接（`mode=ro` + `query_only=1`）。写语句在这里会被 SQLite 拒绝。"""
    conn = fish_ui.open_readonly(db_path)
    for pragma in ("PRAGMA cache_size=-32768", "PRAGMA temp_store=MEMORY"):
        try:
            conn.execute(pragma)
        except sqlite3.Error:
            pass
    return conn


def _write_conn(db_path: str) -> sqlite3.Connection:
    """写连接：**只**在 `write_config` / `apply_preset` 内开，用完就关。

    为什么要 `timeout=15` + `busy_timeout`：hook 进程也在写这个库（推送/工具动作），
    默认 5 秒拿到锁的概率不够高，而配置写入是低频动作，多等 10 秒不吃亏。
    """
    conn = sqlite3.connect(db_path, timeout=15.0)
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (name,)
    ).fetchone()
    return row is not None


def _guard_tables(conn: sqlite3.Connection, tables) -> str | None:
    missing = [name for name in tables if not _table_exists(conn, name)]
    return ("缺表：" + "、".join(missing)) if missing else None


# ── 参数校验（HTTP 层只做路由，校验全在这里）────────────────────────────
def check_days(days) -> int:
    try:
        out = int(days)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"days 要整数，收到 {days!r}") from exc
    if out < DAYS_MIN or out > DAYS_MAX:
        raise ValueError(f"days 的范围是 {DAYS_MIN}..{DAYS_MAX}，收到 {out}")
    return out


def check_limit(limit) -> int:
    try:
        out = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"limit 要整数，收到 {limit!r}") from exc
    if out < 1 or out > EVENTS_LIMIT_MAX:
        raise ValueError(f"limit 的范围是 1..{EVENTS_LIMIT_MAX}，收到 {out}")
    return out


def check_channel(channel) -> str | None:
    """筛选参数校验：**写端接受什么，这里就接受什么**（含历史词）。

    旧故障（112 §三 A1）：写端接受 5 个值、这里只认 3 个 → 历史词写进去了却筛不出来。
    实际三词仍是 UI 词表；历史词只用于"把旧数据筛出来"，聚合侧会标 `legacy`。
    """
    raw = "" if channel is None else str(channel).strip()
    if not raw:
        return None
    if raw not in fish_common.PUSH_CHANNELS_WRITE:
        raise ValueError(f"channel 只能是 {list(fish_common.PUSH_CHANNELS_WRITE)}，收到 {raw}")
    return raw


def check_reaction(reaction) -> str | None:
    """`None` = 不过滤；`pending` = 只要「待判」；`accepted` / `ignored` 原样。

    **空串只是兼容、不是契约**：经 HTTP 时它到不了这里（`parse_qs` 默认把空值丢掉，
    于是「没有参数」与「参数为空」在后端看起来一模一样）。所以契约写 `pending`。
    这里仍把空串映射成 `pending`，是为了让「进程内直调 / 别的调用方传了空串」不至于
    退化成「不过滤」——把「只看待判」悄悄放大成「全部」是最容易看错的假读数。
    """
    if reaction is None:
        return None
    raw = str(reaction).strip()
    if raw == "":
        return "pending"
    if raw not in REACTIONS:
        raise ValueError(
            f"reaction 只能是 {'/'.join(REACTIONS)}（pending=待判）；不传=不过滤，收到 {raw!r}")
    return raw


# ── 类型/范围（写口的第一道闸）──────────────────────────────────────────
_INT_RE = re.compile(r"[+-]?\d+")


def _coerce(key: str, value):
    """按 `CONFIG_DEFAULTS` 的登记类型转换；转不了就 `BadValue`。

    `bool` 必须先判：Python 里 `True` 是 `int` 的子类，`isinstance(True, int)` 为真，
    不先拦掉就会出现「JSON true 被写成 1」——配置表里留下一个 `1`，读侧 `get_config`
    按 bool 解析成 `"1" == "true"` → False。**它能编译、能跑、就是反的**。
    """
    _default, typ, _allowed = fish_common._key_type(key)
    if typ == "int":
        if isinstance(value, bool):
            raise BadValue(f"{key} 要整数，收到布尔值 {value!r}")
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if not value.is_integer():
                raise BadValue(f"{key} 要整数，收到小数 {value!r}")
            return int(value)
        if isinstance(value, str) and _INT_RE.fullmatch(value.strip()):
            return int(value.strip())
        raise BadValue(f"{key} 要整数，收到 {value!r}")
    if typ == "float":
        if isinstance(value, bool):
            raise BadValue(f"{key} 要小数，收到布尔值 {value!r}")
        if isinstance(value, (int, float)):
            out = float(value)
        elif isinstance(value, str):
            try:
                out = float(value.strip())
            except (TypeError, ValueError) as exc:
                raise BadValue(f"{key} 要小数，收到 {value!r}") from exc
        else:
            raise BadValue(f"{key} 要小数，收到 {value!r}")
        if out != out or out in (float("inf"), float("-inf")):
            raise BadValue(f"{key} 要有限小数，收到 {value!r}")
        return out
    if typ == "bool":
        if not isinstance(value, bool):
            raise BadValue(f"{key} 要 true/false（JSON 布尔），收到 {value!r}")
        return value
    raise BadKey(f"{key} 的类型 {typ!r} 不在本页支持的写入类型里")


def _check_range(key: str, value, spec: dict):
    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None and value < lo:
        raise BadValue(f"{key} 的范围是 {lo}..{hi}，收到 {value}")
    if hi is not None and value > hi:
        raise BadValue(f"{key} 的范围是 {lo}..{hi}，收到 {value}")
    return value


def _bad_key_message(key: str) -> str:
    if key in READONLY_KEY_SET:
        return (f"{key} 在推送页只读：它改的是架构/采集，不是口味"
                f"（push.enabled 直接关会把 tool_activity 采集一起停）")
    return f"未知或不可写的配置键：{key}（可写：{', '.join(WRITABLE_KEYS)}）"


# ── 只读：配置视图 ──────────────────────────────────────────────────────
def config_view(conn: sqlite3.Connection) -> list[dict]:
    """7 个可写键 + 6 个只读键的现场值（`source` = 现场有行还是吃了默认值）。"""
    seen_rows = {
        row["key"]: row["updated_at"]
        for row in conn.execute("SELECT key, updated_at FROM config")
    }
    out: list[dict] = []
    for key in WRITABLE_KEYS:
        spec = WRITABLE_BY_KEY[key]
        default, typ, _allowed = fish_common._key_type(key)
        seen = key in seen_rows
        out.append({
            "key": key,
            "label": spec["label"],
            "value": fish_common.get_config(conn, key),
            "default": default,
            "type": typ,
            "source": "db" if seen else "default",
            "writable": True,
            "min": spec["min"],
            "max": spec["max"],
            "help": spec["help"],
            "updatedAt": seen_rows.get(key) if seen else None,
        })
    for key, label, help_text in READONLY_SPECS:
        default, typ, _allowed = fish_common._key_type(key)
        seen = key in seen_rows
        out.append({
            "key": key,
            "label": label,
            "value": fish_common.get_config(conn, key),
            "default": default,
            "type": typ,
            "source": "db" if seen else "default",
            "writable": False,
            "min": None,
            "max": None,
            "help": help_text,
            "updatedAt": seen_rows.get(key) if seen else None,
        })
    return out


# ── kind 归类（kinds / topCodes / events 共用一份）──────────────────────
def classify_kind(code: str | None, mem_type: str | None) -> str:
    """`mem_type` 优先 → 前缀兜底 → 其余归 `memory`（详见文件头「口径」）。"""
    if mem_type and mem_type in KINDS:
        return mem_type
    text = (code or "").strip()
    if ":" in text:
        prefix = text.split(":", 1)[0]
        if prefix in CODE_PREFIX_KINDS:
            return prefix
    return "memory"


def _prefix_sql(column: str) -> str:
    """取 `code` 冒号前的前缀；没有冒号给空串（SQLite 的 `instr` 没找到回 0）。"""
    return (f"CASE WHEN instr(COALESCE({column},''), ':') > 0 "
            f"THEN substr({column}, 1, instr(COALESCE({column},''), ':') - 1) ELSE '' END")


# ── 二级拆解的分组小工具（只有这一处实现）──────────────────────────────
def _blank_row(key: str, label: str) -> dict:
    return {"key": key, "label": label, "total": 0, "accepted": 0,
            "ignored": 0, "pending": 0}


def _add_row(bucket: dict, key: str, label: str, row) -> None:
    """把一条 `(total, accepted, ignored, pending)` 灌进桶里（桶 = `{key: row}`）。"""
    item = bucket.get(key)
    if item is None:
        item = bucket[key] = _blank_row(key, label)
    item["total"] += int(row["total"] or 0)
    item["accepted"] += int(row["accepted"] or 0)
    item["ignored"] += int(row["ignored"] or 0)
    item["pending"] += int(row["pending"] or 0)


def _rows_of(bucket: dict, *, group_total: int) -> list[dict]:
    """桶 → 降序行；超过 `BREAKDOWN_TOP` 行就取前 N 行 + 一行 `__other__`。

    **合并行也要带 accepted/ignored/pending**：只合成 total 的话
    `sum(rows.total)` 虽然还对，但 UI 里「其他（N 项）」那一行的采纳/待判会凭空变 0
    —— 那是把「没显示」写成「没有」。`share` 统一按**组** total 算（不是按最大行）。
    """
    rows = sorted(bucket.values(), key=lambda item: (-item["total"], item["key"]))
    if len(rows) > BREAKDOWN_TOP:
        head, rest = rows[:BREAKDOWN_TOP], rows[BREAKDOWN_TOP:]
        merged = {"key": "__other__", "label": f"其他（{len(rest)} 项）"}
        for field in ("total", "accepted", "ignored", "pending"):
            merged[field] = sum(int(item[field] or 0) for item in rest)
        rows = head + [merged]
    out: list[dict] = []
    for item in rows:
        row = dict(item)          # 副本：缓存里那份会被下一次读到，别让调用方改它
        row["share"] = (row["total"] / group_total) if group_total else None
        out.append(row)
    return out


def _kb_vault_rel(code: str) -> tuple[str, str]:
    """`kb:<vault>:<rel>` → `(vault, rel)`；不是这个形状时 `rel` 给空串（不猜）。"""
    parts = (code or "").split(":", 2)
    if len(parts) < 3:
        return ((parts[1] if len(parts) == 2 else ""), "")
    return parts[1], parts[2]


def _kb_folder(rel: str) -> tuple[str, str]:
    """`rel` 的**顶层目录**；没有斜杠 = 根（键给空串，label 给「（根）」）。"""
    text = (rel or "").strip()
    if not text:
        return ("", "（根）")
    head = text.split("/", 1)[0]
    if head == text:
        return ("", "（根）")
    return (head, head)


# ── 只读：概览 ──────────────────────────────────────────────────────────
def overview(db_path: str, days: int = DEFAULT_DAYS) -> dict:
    """推送面板的整页数据（契约见 `docs/109-推送面板-设计与验收-20261124.md`）。

    `asOf` = **这份数据算出来的时刻**；命中缓存时它仍是那个构建时刻，另给
    `cachedAt`（同值）与 `cacheAgeSec`（放了多久）。**不许**用当前时刻冒充数据时刻。

    `asOf` 与缓存时间戳共用一个 `built_ts`（构建**开始前**取一次）：构建本身要 0.3–0.8 s，
    拿「构建结束」当缓存戳会让命中缓存时的 `asOf` 比刚算出来的那份晚 1 s（跨整秒），
    同一份数据在两次读之间自己换时刻（QA V-30）。
    """
    days = check_days(days)
    cache_key = ("overview", os.path.abspath(db_path), days)
    cached = _cache_get(cache_key)
    if cached is not None:
        built_ts, cached_payload = cached
        payload = dict(cached_payload)
        built_dt = datetime.fromtimestamp(built_ts, timezone.utc)
        payload["asOf"] = _iso(built_dt)            # 数据时刻 = 缓存登记的那个构建时刻（不是 now）
        payload["cachedAt"] = payload["asOf"]
        payload["cacheAgeSec"] = round(max(0.0, time.time() - built_ts), 1)
        return payload

    # 这一个时刻同时是「查询窗口的锚」「asOf」「缓存戳」：三个只能有一个来源。
    built_ts = time.time()
    now = datetime.fromtimestamp(built_ts, timezone.utc)
    conn = _read_conn(db_path)
    try:
        problem = _guard_tables(conn, ("push_log", "code_index", "session_context",
                                       "tool_activity"))
        if problem:
            raise RuntimeError(problem)
        payload = _build_overview(conn, days=days, now=now)
    finally:
        conn.close()
    # 构建期间时间在走：`asOf` 只认 `built_ts`，不认「构建结束」，否则跨整秒就漂 1 s。
    payload["asOf"] = _iso(now)
    payload["cachedAt"] = payload["asOf"]
    payload["cacheAgeSec"] = 0.0
    _cache_put(cache_key, payload, built_ts=built_ts)
    # 返回**副本**：API 层会 `pop("sources")` 把它提到信封里，别把缓存里的那份挖空。
    return dict(payload)


def _build_overview(conn: sqlite3.Connection, *, days: int, now: datetime) -> dict:
    since = _iso(now - timedelta(days=days))
    started = time.perf_counter()

    # ── ① totals：**整段复用** push_stats（唯一算法，不重算）──────────────
    import fish  # 0.6 s 级 import：进程内只发生一次（与 collect_stats 同一手法）
    stats = fish.push_stats(conn, days=days, now=now)
    total = int(stats["total"] or 0)

    # ── ② 通道 ────────────────────────────────────────────────────────
    channel_rows = {
        row["channel"]: row for row in conn.execute(
            "SELECT COALESCE(channel,'') AS channel, COUNT(*) AS total, "
            "SUM(CASE WHEN reaction='accepted' THEN 1 ELSE 0 END) AS accepted, "
            "SUM(CASE WHEN reaction='ignored' THEN 1 ELSE 0 END) AS ignored, "
            "SUM(CASE WHEN reaction IS NULL THEN 1 ELSE 0 END) AS pending "
            "FROM push_log WHERE pushed_at >= ? GROUP BY channel", (since,))
    }
    channels = []
    for name in CHANNELS + tuple(sorted(k for k in channel_rows if k not in CHANNELS)):
        row = channel_rows.get(name)
        # `row is None` = 这个通道在窗口内一行都没有（空库 / 单通道缺失）：
        # 必须与 total/pending 一样走 `if row else 0`。旧写法 `(row or {})["accepted"]`
        # 在 row 为 None 时抛 `KeyError: 'accepted'`，读口回 500 `internal_error`。
        accepted = int(row["accepted"] or 0) if row else 0
        ignored = int(row["ignored"] or 0) if row else 0
        channels.append({
            "channel": name,
            "total": int(row["total"]) if row else 0,
            "accepted": accepted,
            "ignored": ignored,
            "pending": int(row["pending"] or 0) if row else 0,
            "acceptRate": fish_common.accept_rate(accepted, ignored),
            # 历史词（hook/situational）出现在这里说明库里有旧数据：显式标出来，
            # 免得它看起来像"当前还会写入的通道"（UI 词表只有实际三词）。
            "legacy": name in fish_common.LEGACY_PUSH_CHANNELS,
        })

    # ── ③ kinds（mem_type 优先 → 前缀兜底 → memory）────────────────────
    buckets = {kind: {"kind": kind, "total": 0, "accepted": 0, "ignored": 0,
                      "pending": 0} for kind in KINDS}
    for row in conn.execute(
            "SELECT COALESCE(ci.mem_type,'') AS mem_type, "
            f"{_prefix_sql('pl.code')} AS prefix, "
            "COUNT(*) AS total, "
            "SUM(CASE WHEN pl.reaction='accepted' THEN 1 ELSE 0 END) AS accepted, "
            "SUM(CASE WHEN pl.reaction='ignored' THEN 1 ELSE 0 END) AS ignored, "
            "SUM(CASE WHEN pl.reaction IS NULL THEN 1 ELSE 0 END) AS pending "
            "FROM push_log pl LEFT JOIN code_index ci ON ci.code = pl.code "
            "WHERE pl.pushed_at >= ? GROUP BY mem_type, prefix", (since,)):
        kind = classify_kind(f"{row['prefix']}:" if row["prefix"] else "",
                             row["mem_type"] or None)
        bucket = buckets[kind]
        bucket["total"] += int(row["total"] or 0)
        bucket["accepted"] += int(row["accepted"] or 0)
        bucket["ignored"] += int(row["ignored"] or 0)
        bucket["pending"] += int(row["pending"] or 0)
    kinds = [buckets[kind] for kind in KINDS]

    # ── ④ 诊断：收件人 ────────────────────────────────────────────────
    sess = conn.execute(
        "SELECT COUNT(*) AS n, "
        "SUM(CASE WHEN project IS NULL OR project='' THEN 1 ELSE 0 END) AS unknown "
        "FROM session_context WHERE updated_at >= ?", (since,)).fetchone()
    act = conn.execute(
        "SELECT COUNT(*) AS n, "
        "SUM(CASE WHEN project IS NULL OR project='' THEN 1 ELSE 0 END) AS unknown "
        "FROM tool_activity WHERE seen_at >= ?", (since,)).fetchone()
    sessions = int(sess["n"] or 0)
    unknown_sessions = int(sess["unknown"] or 0)
    activity_rows = int(act["n"] or 0)
    unknown_activity = int(act["unknown"] or 0)
    workspace_map_rows = (conn.execute("SELECT COUNT(*) FROM workspace_map").fetchone()[0]
                          if _table_exists(conn, "workspace_map") else None)
    receiver = {
        "sessions": sessions,
        "unknownSessions": unknown_sessions,
        "unknownRate": (unknown_sessions / sessions) if sessions else None,
        "activityUnknownRate": (unknown_activity / activity_rows) if activity_rows else None,
        "workspaceMapRows": workspace_map_rows,
    }

    # ── ⑤ 诊断：开放词表 / artifact ────────────────────────────────────
    keywords = tuple(fish_common.ATTENTION_OPEN_KEYWORDS)
    like = " OR ".join(["ci.title LIKE ?"] * len(keywords))
    open_row = conn.execute(
        "SELECT COUNT(*) AS pushes, COUNT(DISTINCT pl.code) AS codes FROM push_log pl "
        "JOIN code_index ci ON ci.code = pl.code "
        f"WHERE pl.pushed_at >= ? AND ({like})",
        (since, *[f"%{word}%" for word in keywords])).fetchone()
    open_pushes = int(open_row["pushes"] or 0)
    open_loop = {
        "pushes": open_pushes,
        "codes": int(open_row["codes"] or 0),
        "share": (open_pushes / total) if total else None,
        "keywords": list(keywords),
    }
    art_row = conn.execute(
        "SELECT COUNT(*) AS pushes, "
        "SUM(CASE WHEN pl.reaction='accepted' THEN 1 ELSE 0 END) AS accepted "
        "FROM push_log pl JOIN code_index ci ON ci.code = pl.code "
        "WHERE pl.pushed_at >= ? AND ci.mem_type='artifact'", (since,)).fetchone()
    artifact = {"pushes": int(art_row["pushes"] or 0),
                "accepted": int(art_row["accepted"] or 0)}

    # ── ⑥ 诊断：重复推送（同会话 + 同 code + 同一天）───────────────────
    dup_row = conn.execute(
        "SELECT COUNT(*) AS groups, MAX(n) AS max_n FROM ("
        "  SELECT COUNT(*) AS n FROM push_log "
        "  WHERE pushed_at >= ? AND code IS NOT NULL AND code <> '' "
        "  GROUP BY session_key, code, substr(pushed_at, 1, 10) HAVING n > 1)", (since,)
    ).fetchone()
    dup_groups = int(dup_row["groups"] or 0)
    max_per_day = int(dup_row["max_n"] or 0)
    max_code = None
    max_detail = None
    if max_per_day > 1:
        top = conn.execute(
            "SELECT session_key, code, substr(pushed_at, 1, 10) AS day, COUNT(*) AS n, "
            "MIN(pushed_at) AS first_at, MAX(pushed_at) AS last_at FROM push_log "
            "WHERE pushed_at >= ? AND code IS NOT NULL AND code <> '' "
            "GROUP BY session_key, code, substr(pushed_at, 1, 10) "
            "ORDER BY n DESC, last_at DESC LIMIT 1", (since,)).fetchone()
        if top:
            max_code = top["code"]
            max_detail = (f"{_session_tag(top['session_key'])} · {top['code']} · "
                          f"{top['day']} · {int(top['n'])} 次")
    duplicates = {
        "sessionCodeDayGroups": dup_groups,
        "maxPerSessionDay": max_per_day,
        "maxCode": max_code,
        "maxDetail": max_detail,
    }

    # ── ⑦ Top offenders ───────────────────────────────────────────────
    top_codes = []
    for row in conn.execute(
            "SELECT pl.code AS code, COUNT(*) AS n, "
            "SUM(CASE WHEN pl.reaction='accepted' THEN 1 ELSE 0 END) AS accepted, "
            "SUM(CASE WHEN pl.reaction='ignored' THEN 1 ELSE 0 END) AS ignored, "
            "MAX(pl.pushed_at) AS last_at, ci.title AS title, ci.project AS project, "
            "ci.mem_type AS mem_type "
            "FROM push_log pl LEFT JOIN code_index ci ON ci.code = pl.code "
            "WHERE pl.pushed_at >= ? GROUP BY pl.code "
            "ORDER BY n DESC, last_at DESC LIMIT ?", (since, TOP_CODES_LIMIT)):
        top_codes.append({
            "code": row["code"] or "",
            "title": row["title"] or "",
            "project": row["project"],
            "kind": classify_kind(row["code"], row["mem_type"]),
            "count": int(row["n"] or 0),
            "accepted": int(row["accepted"] or 0),
            "ignored": int(row["ignored"] or 0),
            "lastAt": row["last_at"],
        })

    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    # `code_index` 的 `rows` 是**这张表的行数**（原来这里写的是 `len(top_codes)`，
    # 那是「Top offenders 的条数」，不属于 code_index——两个数都真，但名字对不上就是假证据）。
    code_index_rows = int(conn.execute("SELECT COUNT(*) FROM code_index").fetchone()[0])

    # ── ⑧ 二级拆解（内容源 / 态势源两条腿；口径见文件头）─────────────────
    # 放在最后算：它复用 ⑤ 的 `open_loop`（开放词表的 pushes/codes 不重算），
    # 并且**不新增 sources 行**——它读的还是 push_log / code_index（已在 sources 里），
    # `git_repo` 只用来取仓库名（缺表时退回编码），那一列不是判据。
    breakdown = _build_breakdown(conn, since=since, open_loop=open_loop)

    return {
        "window": {"days": days, "since": since},
        "totals": {
            "total": total,
            "accepted": int(stats["accepted"] or 0),
            "ignored": int(stats["ignored"] or 0),
            "pending": int(stats["pending"] or 0),
            "decided": int(stats["decided"] or 0),
            "acceptRate": stats["accept_rate"],
            "gapClearRate": stats["gap_clear_rate"],
            "violationRate": stats["violation_rate"],
            "sessionStdev": stats["session_stdev"],
            # 下面两个是率的分母/分子，UI 写口径小字用（**不**给「总分」）
            "gapCleared": int(stats["gap_cleared"] or 0),
            "toolAfterTotal": int(stats["tool_after_total"] or 0),
        },
        "channels": channels,
        "kinds": kinds,
        "diagnostics": {
            "receiver": receiver,
            "openLoop": open_loop,
            "artifact": artifact,
            "duplicates": duplicates,
        },
        "topCodes": top_codes,
        # 「这些推送是什么东西」：内容源（记忆/知识库/语料）+ 态势源（看板/动作流/git）。
        # 每组的 `sum(rows.total) == 组 total`（截断成「前 8 行 + 其他（N 项）」之后也成立）。
        "breakdown": breakdown,
        "config": config_view(conn),
        "asOf": _iso(now),
        # 刚算出来的这份：`cachedAt` 与 `asOf` 同值、年龄 0；命中缓存时由 `overview()` 改写。
        # `now` 由 `overview()` 的 `built_ts` 派生（同一个时刻），所以这里与缓存戳天然同秒。
        "cachedAt": _iso(now),
        "cacheAgeSec": 0.0,
        "sources": [
            {"source": "push_log", "ok": True, "rows": total, "error": None,
             "ms": elapsed_ms},
            {"source": "code_index", "ok": True, "rows": code_index_rows, "error": None,
             "ms": elapsed_ms},
            {"source": "session_context", "ok": True, "rows": sessions, "error": None,
             "ms": elapsed_ms},
            {"source": "tool_activity", "ok": True, "rows": activity_rows, "error": None,
             "ms": elapsed_ms},
        ],
    }


def _build_breakdown(conn: sqlite3.Connection, *, since: str, open_loop: dict) -> dict:
    """「内容源 / 态势源」两条腿的二级拆解（口径见文件头「二级拆解」）。

    四件事写清楚：

    1. **每条腿最多 5 条聚合 SQL**（记忆一条同时喂三个维度、知识库一条喂目录/通道/top 文件），
       都走 `since` 同一个时间窗、复用同一条只读连接——**不逐行查、不另开连接**。
    2. **组 total 与分组和是同一批行**：`memory.total = sum(byType)` 是构造出来的，
       所以截断成「前 8 行 + 其他（N 项）」之后仍然 `sum(rows.total) == total`。
       和 `kinds` 的相等关系是**另一条独立路径**，交给判据去对（V-31），不在这里靠赋值保证。
    3. 每一行都是同一形状 `{key,label,total,accepted,ignored,pending,share}`
       （`topFiles` 例外：它是「文件」不是「分组」，按 code/rel/title 给），
       前端不用为不同的组写不同的字段判空。
    4. **空库 / 单通道不抛**：所有桶为空时各组返回 `[]`、total 为 0；`git_repo` 缺表时
       仓库名退回编码本身（那一列不是判据，读不到就不猜）。
    """
    # ── 内容源 ① 记忆：一条 SQL 同时喂 type / project / channel ──────────
    by_type: dict = {}
    by_project: dict = {}
    by_channel: dict = {}
    for row in conn.execute(
            "SELECT COALESCE(ci.mem_type,'') AS mem_type, "
            f"{_prefix_sql('pl.code')} AS prefix, "
            "COALESCE(ci.project,'') AS proj, "
            "COALESCE(pl.channel,'') AS chan, "
            "COUNT(*) AS total, "
            "SUM(CASE WHEN pl.reaction='accepted' THEN 1 ELSE 0 END) AS accepted, "
            "SUM(CASE WHEN pl.reaction='ignored' THEN 1 ELSE 0 END) AS ignored, "
            "SUM(CASE WHEN pl.reaction IS NULL THEN 1 ELSE 0 END) AS pending "
            "FROM push_log pl LEFT JOIN code_index ci ON ci.code = pl.code "
            "WHERE pl.pushed_at >= ? "
            # 别名必须换个名字：`pl.project` 与 `ci.project` 同名，`GROUP BY project`
            # 在 SQLite 里是**歧义列**（实测 `ambiguous column name: project` → 500）。
            "GROUP BY mem_type, prefix, proj, chan", (since,)):
        # 与 `kinds` 走**同一个** `classify_kind`：两处的「记忆」才是同一批行。
        kind = classify_kind(f"{row['prefix']}:" if row["prefix"] else "",
                             row["mem_type"] or None)
        if kind not in MEMORY_KINDS:
            continue
        mem_type = (row["mem_type"] or "").strip()
        _add_row(by_type, mem_type or "unknown", mem_type or "未分类", row)
        project = (row["proj"] or "").strip()
        _add_row(by_project, project or "未归属", project or "未归属", row)
        channel = (row["chan"] or "").strip()
        _add_row(by_channel, channel or "（空）", channel or "（空）", row)
    memory_total = sum(int(item["total"]) for item in by_type.values())

    # ── 内容源 ② 知识库：目录 / 通道 / top 文件（一条 SQL）────────────────
    kb_by_folder: dict = {}
    kb_by_channel: dict = {}
    kb_files: dict = {}
    for row in conn.execute(
            "SELECT pl.code AS code, COALESCE(pl.channel,'') AS chan, "
            "COALESCE(ci.title,'') AS title, "
            "COUNT(*) AS total, "
            "SUM(CASE WHEN pl.reaction='accepted' THEN 1 ELSE 0 END) AS accepted, "
            "SUM(CASE WHEN pl.reaction='ignored' THEN 1 ELSE 0 END) AS ignored, "
            "SUM(CASE WHEN pl.reaction IS NULL THEN 1 ELSE 0 END) AS pending "
            "FROM push_log pl LEFT JOIN code_index ci ON ci.code = pl.code "
            "WHERE pl.pushed_at >= ? AND pl.code LIKE 'kb:%' "
            "GROUP BY pl.code, chan, ci.title", (since,)):
        code = row["code"] or ""
        _vault, rel = _kb_vault_rel(code)
        folder_key, folder_label = _kb_folder(rel)
        _add_row(kb_by_folder, folder_key, folder_label, row)
        channel = (row["chan"] or "").strip()
        _add_row(kb_by_channel, channel or "（空）", channel or "（空）", row)
        item = kb_files.get(code)
        if item is None:
            item = kb_files[code] = {"code": code, "rel": rel,
                                     "title": (row["title"] or "").strip(),
                                     "total": 0, "accepted": 0, "ignored": 0, "pending": 0}
        for field in ("total", "accepted", "ignored", "pending"):
            item[field] += int(row[field] or 0)
    kb_total = sum(int(item["total"]) for item in kb_files.values())
    top_files = sorted(kb_files.values(), key=lambda item: (-item["total"], item["code"]))[:5]
    for item in top_files:
        # `rel` 与 `title` **至少一个可读**：两个都空时退回编码本身（空白比编码更难查）。
        if not item["rel"] and not item["title"]:
            item["rel"] = item["code"]

    # ── 态势源 ① 看板：project / 看板 label ───────────────────────────────
    board_by_project: dict = {}
    board_by_label: dict = {}
    for row in conn.execute(
            "SELECT pl.code AS code, COALESCE(pl.project,'') AS project, "
            "COUNT(*) AS total, "
            "SUM(CASE WHEN pl.reaction='accepted' THEN 1 ELSE 0 END) AS accepted, "
            "SUM(CASE WHEN pl.reaction='ignored' THEN 1 ELSE 0 END) AS ignored, "
            "SUM(CASE WHEN pl.reaction IS NULL THEN 1 ELSE 0 END) AS pending "
            "FROM push_log pl WHERE pl.pushed_at >= ? AND pl.code LIKE 'board:%' "
            "GROUP BY pl.code, project", (since,)):
        code = row["code"] or ""
        label = code.split(":", 1)[1].strip() if ":" in code else code.strip()
        _add_row(board_by_label, code, label or "根看板", row)
        project = (row["project"] or "").strip()
        _add_row(board_by_project, project or "未归属", project or "未归属", row)
    board_total = sum(int(item["total"]) for item in board_by_label.values())

    # ── 态势源 ② 动作流：按目录 ─────────────────────────────────────────
    activity_by_dir: dict = {}
    for row in conn.execute(
            "SELECT pl.code AS code, COUNT(*) AS total, "
            "SUM(CASE WHEN pl.reaction='accepted' THEN 1 ELSE 0 END) AS accepted, "
            "SUM(CASE WHEN pl.reaction='ignored' THEN 1 ELSE 0 END) AS ignored, "
            "SUM(CASE WHEN pl.reaction IS NULL THEN 1 ELSE 0 END) AS pending "
            "FROM push_log pl WHERE pl.pushed_at >= ? AND pl.code LIKE 'activity:%' "
            "GROUP BY pl.code", (since,)):
        code = row["code"] or ""
        dirname = code.split(":", 1)[1].strip() if ":" in code else code.strip()
        _add_row(activity_by_dir, dirname or "（空）", dirname or "（空）", row)
    activity_total = sum(int(item["total"]) for item in activity_by_dir.values())

    # ── 态势源 ③ git：按仓库（名字取 git_repo 的 display_name/identity）────
    repo_names: dict[int, str] = {}
    if _table_exists(conn, "git_repo"):
        for row in conn.execute("SELECT repo_id, display_name, identity FROM git_repo"):
            repo_names[int(row["repo_id"])] = (
                (row["display_name"] or "").strip() or (row["identity"] or "").strip())
    git_by_repo: dict = {}
    for row in conn.execute(
            "SELECT pl.code AS code, COUNT(*) AS total, "
            "SUM(CASE WHEN pl.reaction='accepted' THEN 1 ELSE 0 END) AS accepted, "
            "SUM(CASE WHEN pl.reaction='ignored' THEN 1 ELSE 0 END) AS ignored, "
            "SUM(CASE WHEN pl.reaction IS NULL THEN 1 ELSE 0 END) AS pending "
            "FROM push_log pl WHERE pl.pushed_at >= ? AND pl.code LIKE 'git:%' "
            "GROUP BY pl.code", (since,)):
        code = row["code"] or ""
        repo_id = code.split(":", 1)[1].strip() if ":" in code else ""
        name = repo_names.get(int(repo_id)) if repo_id.isdigit() else None
        # 没有 display_name/identity 时 label 退回**编码本身**（含 repo_id）——不猜仓库名。
        _add_row(git_by_repo, code, name or code, row)
    git_total = sum(int(item["total"]) for item in git_by_repo.values())

    # ── 内容源 ③ 语料：现场关闭也要如实说「关着」+「历史行多少」──────────
    enabled = bool(fish_common.get_config(conn, "push.corpus_enabled"))
    corpus_total = int(conn.execute(
        "SELECT COUNT(*) FROM push_log WHERE pushed_at >= ? AND code LIKE 'conv:%'",
        (since,)).fetchone()[0] or 0)
    note = ("push.corpus_enabled=true（现场开着）" if enabled
            else "push.corpus_enabled=false（现场关闭）")
    if corpus_total:
        note += f"；窗口内 {corpus_total} 次是关闭前的历史行"

    open_pushes = int(open_loop.get("pushes") or 0)
    return {
        "memory": {
            "total": memory_total,
            "byType": _rows_of(by_type, group_total=memory_total),
            "byProject": _rows_of(by_project, group_total=memory_total),
            "byChannel": _rows_of(by_channel, group_total=memory_total),
            "openLoop": {
                # pushes/codes 是 `diagnostics.openLoop` 的**同一个数**，不重算；只有分母不同。
                "pushes": open_pushes,
                "codes": int(open_loop.get("codes") or 0),
                "share": (open_pushes / memory_total) if memory_total else None,
            },
        },
        "kb": {
            "total": kb_total,
            "byFolder": _rows_of(kb_by_folder, group_total=kb_total),
            "byChannel": _rows_of(kb_by_channel, group_total=kb_total),
            "topFiles": top_files,
        },
        "board": {
            "total": board_total,
            "byProject": _rows_of(board_by_project, group_total=board_total),
            "byLabel": _rows_of(board_by_label, group_total=board_total),
        },
        "activity": {
            "total": activity_total,
            "byDir": _rows_of(activity_by_dir, group_total=activity_total),
        },
        "git": {
            "total": git_total,
            "byRepo": _rows_of(git_by_repo, group_total=git_total),
        },
        "corpus": {"enabled": enabled, "total": corpus_total, "note": note},
    }


def _session_tag(session_key: str | None) -> str:
    """人类面的会话短标识：正常路径与兜底路径是**同一个函数**（112 W-B · N-5）。

    旧实现是两条路径各一份取法（正常路径取 id 前 4–8 位、兜底路径取末 4 位），
    同一个会话能显示成两个都"看着对"的标签。现在只有 `fish_naming.session_short`
    一份；**空标签的展示策略由调用点决定**（这里是 `(未归属)`，面板侧是 `""`）。
    """
    if not session_key:
        return "(未归属)"
    try:
        import fish_naming
        return fish_naming.session_short(session_key)
    except Exception:  # noqa: BLE001  短标识不是判据：取不到就给空串，不编一个
        return ""


# ── 只读：最近推送事件 ──────────────────────────────────────────────────
def events(db_path: str, *, limit: int = EVENTS_LIMIT_DEFAULT,
           channel: str | None = None, reaction: str | None = None) -> dict:
    """按 id 倒序的最近推送行；`reaction='pending'` = 只要「待判」（库里 `reaction IS NULL`）。

    **不设时间窗**：这一块叫「最近推送」，问的是「刚发生了什么」，不是「30 天内」。
    加窗口会让「昨天刚停推」这种状态看起来像「什么都没有」（空态与窗口空态混淆）。
    """
    limit = check_limit(limit)
    channel = check_channel(channel)
    reaction = check_reaction(reaction)
    now = now_utc()
    conn = _read_conn(db_path)
    try:
        problem = _guard_tables(conn, ("push_log",))
        if problem:
            raise RuntimeError(problem)
        where = ["1=1"]
        args: list = []
        if channel:
            where.append("COALESCE(pl.channel,'') = ?")
            args.append(channel)
        if reaction is None:
            pass                                  # 不传 = 不过滤
        elif reaction == "pending":
            where.append("pl.reaction IS NULL")   # pending = 只要「待判」
        else:
            where.append("pl.reaction = ?")
            args.append(reaction)
        args.append(limit)
        rows = conn.execute(
            "SELECT pl.id AS id, pl.channel AS channel, pl.code AS code, "
            "pl.project AS project, pl.pushed_at AS pushed_at, pl.reaction AS reaction, "
            "pl.latency_sec AS latency_sec, ci.title AS title, ci.mem_type AS mem_type "
            "FROM push_log pl LEFT JOIN code_index ci ON ci.code = pl.code "
            f"WHERE {' AND '.join(where)} ORDER BY pl.id DESC LIMIT ?", tuple(args)).fetchall()
    finally:
        conn.close()
    items = [{
        "id": int(row["id"]),
        "channel": row["channel"] or "",
        "code": row["code"] or "",
        "title": row["title"] or "",
        "project": row["project"],
        "kind": classify_kind(row["code"], row["mem_type"]),
        "pushedAt": row["pushed_at"],
        "reaction": row["reaction"] or "",
        "latencySec": row["latency_sec"],
    } for row in rows]
    return {
        "items": items,
        "filter": {"limit": limit, "channel": channel, "reaction": reaction},
        "asOf": _iso(now),
    }


# ── 审计 ────────────────────────────────────────────────────────────────
def audit_path(db_path: str) -> str:
    """`<data>/_audit/push-config.jsonl`（`<data>` = 主库所在目录）。"""
    return os.path.join(os.path.dirname(os.path.abspath(db_path)),
                        AUDIT_DIRNAME, AUDIT_FILENAME)


def _append_audit(db_path: str, entries: list[dict], *, actor: str,
                  preset: str | None = None) -> str | None:
    """追加审计行；**写失败不阻断主写**，但必须出声（`warn_derived`）。"""
    if not entries:
        return None
    ts = _iso(now_utc())
    path = audit_path(db_path)
    lines = []
    for entry in entries:
        record = {"ts": ts, "actor": actor, "key": entry["key"],
                  "old": entry["old"], "new": entry["new"]}
        if preset:
            record["preset"] = preset
        lines.append(json.dumps(record, ensure_ascii=False))
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        # 审计失败 → 配置已经生效了（回滚更糟），但读日志的人必须知道证据缺了一行。
        fish_common.warn_derived(None, "push-config-audit", exc)
        return None
    return ts


# ── 写口（唯一两个）─────────────────────────────────────────────────────
def write_config(db_path: str, key, value, *, actor: str = ACTOR_DEFAULT) -> dict:
    """写**一个**白名单键。未知/只读键 `BadKey`；类型/范围不合法 `BadValue`。

    「读 old → 写库 → 追加审计」整段在 `_WRITE_LOCK` 里：并发写同一个键时，审计链里
    每一行的 `old` 必须等于上一行的 `new`（否则这份审计回答不了「谁把它改成现在这样」）。
    """
    if not isinstance(key, str) or not key.strip():
        raise BadKey("缺 key（要写哪个配置项？）")
    key = key.strip()
    spec = WRITABLE_BY_KEY.get(key)
    if spec is None:
        raise BadKey(_bad_key_message(key))
    new = _check_range(key, _coerce(key, value), spec)

    with _WRITE_LOCK:
        conn = _write_conn(db_path)
        try:
            old = fish_common.get_config(conn, key)
            if old == new:
                # 值没变 → **不写库、不记审计**（文档 §三：「没改」不该在审计里留下一条
                # 看起来改过的行）。与 `apply_preset` 的同值短路同一口径（`changed=False`）。
                return {"key": key, "old": old, "new": new, "source": "workbench",
                        "auditAt": None, "changed": False}
            fish_db.set_config(conn, key, new)  # 已 commit + 写 config.changed 事件
        finally:
            conn.close()

        # 审计也在锁里：不加锁时「提交顺序」与「追加顺序」可以相反，审计链就断了。
        audit_at = _append_audit(db_path, [{"key": key, "old": old, "new": new}], actor=actor)
        invalidate_cache()
    return {"key": key, "old": old, "new": new, "source": "workbench",
            "auditAt": audit_at, "changed": True}


def apply_preset(db_path: str, preset, *, actor: str = ACTOR_DEFAULT) -> dict:
    """应用「止血/恢复」预设。**一次连接、先全部校验、再逐键写**，值没变的键不写（也不记审计）。

    与评审 🟡-1 直接相关的三件事：

      1. **先校验完再动写**：三个键的 `_coerce`/`_check_range` 全跑过一遍才开写。半个预设
         生效（改了 2 个、第 3 个不合法）比整个不生效更难排查——而且用户看到的是
         「点了止血，回执是 400」却已经改了两个键。
      2. **失败也留审计**：写到一半库出错（锁被占满 / 磁盘满）时，**已应用**的键仍逐行落审计，
         然后原样抛出——不许出现「配置改了但审计零行」。
      3. 整段在 `_WRITE_LOCK` 里（与 `write_config` 同一把锁）：两个客户端同时点
         「止血」「恢复」时，两条审计链不会互相踩。
    """
    if not isinstance(preset, str) or preset.strip() not in PRESETS:
        raise BadValue(f"preset 只能是 {list(PRESETS)}，收到 {preset!r}")
    preset = preset.strip()
    plan = PRESETS[preset]

    # ① 全部校验（不碰库）：任何一个键不合法 → 一个字节都不写。
    checked: list[tuple[str, object]] = []
    for key, value in plan.items():
        spec = WRITABLE_BY_KEY[key]
        checked.append((key, _check_range(key, _coerce(key, value), spec)))

    changes: list[dict] = []
    applied: list[dict] = []
    audit_at = None
    with _WRITE_LOCK:
        conn = _write_conn(db_path)
        try:
            for key, new in checked:
                old = fish_common.get_config(conn, key)
                changed = old != new
                changes.append({"key": key, "old": old, "new": new, "changed": changed})
                if not changed:
                    continue
                fish_db.set_config(conn, key, new)
                applied.append({"key": key, "old": old, "new": new})
        except BaseException:
            # ② 写了一半失败：已应用的键先落审计，再原样抛（不让「改了但零审计」发生）。
            audit_at = _append_audit(db_path, applied, actor=actor, preset=preset)
            invalidate_cache()
            raise
        finally:
            conn.close()

        audit_at = _append_audit(db_path, applied, actor=actor, preset=preset)
        invalidate_cache()

    return {
        "preset": preset,
        "label": PRESET_NAMES[preset],
        "changes": changes,
        "changedCount": len(applied),
        "source": "workbench",
        "auditAt": audit_at,
    }


# ── 自检（不起服务；人工/脚本对拍用）───────────────────────────────────
def _selftest(db_path: str) -> int:
    out: dict = {}
    data = overview(db_path, DEFAULT_DAYS)
    out["window"] = data["window"]
    out["totals"] = data["totals"]
    out["channels"] = data["channels"]
    out["kinds"] = [k for k in data["kinds"] if k["total"]]
    out["diagnostics"] = data["diagnostics"]
    out["topCodes"] = [(c["code"], c["count"]) for c in data["topCodes"][:3]]
    # 二级拆解：只打各段 total 与行数（人眼对拍用；细节看 API）
    bd = data["breakdown"]
    out["breakdown"] = {
        "memory": {"total": bd["memory"]["total"],
                   "rows": {k: len(bd["memory"][k])
                            for k in ("byType", "byProject", "byChannel")},
                   "openLoop": bd["memory"]["openLoop"]},
        "kb": {"total": bd["kb"]["total"],
               "rows": {k: len(bd["kb"][k])
                        for k in ("byFolder", "byChannel", "topFiles")}},
        "board": {"total": bd["board"]["total"],
                  "rows": {k: len(bd["board"][k]) for k in ("byProject", "byLabel")}},
        "activity": {"total": bd["activity"]["total"],
                     "rows": {"byDir": len(bd["activity"]["byDir"])}},
        "git": {"total": bd["git"]["total"], "rows": {"byRepo": len(bd["git"]["byRepo"])}},
        "corpus": bd["corpus"],
    }
    out["config"] = [(c["key"], c["value"], c["writable"]) for c in data["config"]]
    out["events"] = len(events(db_path, limit=5)["items"])
    out["auditPath"] = audit_path(db_path)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法：python p0/fish_push_audit.py <主库路径>（自检，只读）", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(_selftest(sys.argv[1]))
