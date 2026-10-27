#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共享原语（`fish.py` 拆包第一轮 · T02 · 设计 §1.2）。

为什么单独成模块
----------------
这几个小工具被**两个**子系统要用：`fish.py`（记忆 / 看板 / 门面）与
`fish_kb.py`（知识库）。它们原来是 `fish.py` 里的函数，`fish_kb` 要用就得
`import fish` —— 而 `fish` 又 `import fish_kb`，那是循环导入。所以先下沉成
"零依赖"的一层：

    fish.py ─┐
             ├─→ fish_common
    fish_kb ─┘

依赖方向是单向的：**这里不许 import fish 或 fish_kb**（判据 J2 静态 + 运行时双查）。
`count_tokens` 里的 tiktoken 是**函数内懒加载**，所以本模块顶层只有 stdlib。

搬了什么（整段搬运，一个字符都没改）
------------------------------------
`ulid`（编码里的时间前缀）、`now_utc`（库里的 UTC 时间戳）、`iso_local`（输出层
本地时区）、`count_tokens` 与它的 `_ENC` 缓存、`_exc_text`（异常一行摘要）；
2026-10-27 起再加切词三函数 `index_tokens` / `fts_text` / `fts_match_expr`
（语料层要按同一套切法建 `corpus_fts`，见下方"中文切词"一节）。
搬走后 `fish.<name>` 仍然可用，且与 `fish_common.<name>` 是**同一个对象**
（`from fish_common import …` 再导出，不是复制实现）。
"""
import datetime
import re
import uuid
from pathlib import Path

def ulid() -> str:
    ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    return f"{ts:013d}{uuid.uuid4().hex[:13].upper()}"

def now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def iso_local(dt=None) -> str:
    """本地时区的 ISO8601（`2026-10-26T02:05:11+08:00`）：人读友好、带时区、可回读。

    为什么不直接用 `now_utc()`：数据库里的时间戳一律是 UTC + `Z`（事实层），
    而 as-of 是**输出层**的东西——门面是给人看的，"几点几分"必须是他自己的时区。
    `fromisoformat` 能原样读回带偏移的字符串，所以这个格式不会丢信息。

    为什么无时区的入参按本地时区补：`--as-of 2026-10-26T00:00:00` 是人手写的最常见形态，
    把它当 UTC 会让"今天"整体偏 8 小时（索引月份、最近 30 条都会跟着错）。
    """
    if dt is None:
        dt = datetime.datetime.now().astimezone()
    elif dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.isoformat(timespec="seconds")

_ENC = None

def count_tokens(text: str) -> int:
    """优先用 tiktoken 精确计数；不可用时按字符估算（中文≈1.2 token/字）。"""
    global _ENC
    if _ENC is None:
        try:
            import tiktoken
            _ENC = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _ENC = False
    if _ENC is False:
        return int(len(text) * 1.2)
    return len(_ENC.encode(text))

def _exc_text(exc) -> str:
    return f"{type(exc).__name__}: {exc}"

# ------------------------------------------------------------------ 中文切词（ADR-005）

# 为什么切词也下沉（2026-10-27 · 语料层 T01 · 设计 D09）
# ------------------------------------------------------
# 语料层要自建 FTS（`corpus_fts`），而"索引侧怎么切"与"查询侧怎么切"**必须是同一套**：
# 两套切法的典型症状是"存进去搜不到"——单测各自都绿，因为谁也不查对方建的索引。
# 这次下沉是把唯一一份实现放到两个子系统都能 import 的最低层：
#
#     fish.py ─┐                    ┌─→ memory_fts（记忆）
#              ├─→ fish_common ─────┤
#     fish.py ←┘                    └─→ corpus_fts（语料）
#
# 搬运动作与原拆包第一轮同法：**整段搬运**，`fish.index_tokens` 等旧名字仍然可用，
# 且与这里**是同一个对象**（`from … import` 再导出，不是复制实现）。
_CJK = re.compile(r"[\u4e00-\u9fff]+")
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_.]*")


def index_tokens(text: str) -> list:
    """把文本切成可入 FTS 的 token（ADR-005 的切分规则）。

    中文连续段 → **重叠 bigram**（两字滑窗）。为什么必须自己切：SQLite 内置的
    unicode61 不做中文分词，未切分时「协作约定」是一个 token，查「协作」永远 0 条
    （2026-10-22 实测，见 ADR-005 背景表第 2 条）。
    拉丁 / 数字 / 单号（含 - _ .）→ 原样小写，交给 FTS 再切。
    """
    text = text or ""
    out = [m.group(0).lower() for m in _WORD.finditer(text)]
    for m in _CJK.finditer(text):
        s = m.group(0)
        if len(s) == 1:
            out.append(s)
        else:
            out.extend(s[i:i + 2] for i in range(len(s) - 1))
    return out


def fts_text(text: str) -> str:
    return " ".join(index_tokens(text))


def fts_match_expr(query: str):
    """查询串 → FTS5 MATCH 表达式；没有可查 token 时返回 None。

    **每个 token 必须加双引号**：不加的话 `CR-12` 会被 FTS5 解析成「CR 列包含 12」，
    直接抛 sqlite3.OperationalError: no such column: 12（2026-10-22 实测，ADR-005 §背景 1）。
    """
    toks = [t for t in index_tokens(query) if t]
    if not toks:
        return None
    return " AND ".join('"' + t.replace('"', '""') + '"' for t in toks)


# ============================================================
# 基础层下沉（T02，2026-10-27）：配置/时间/衰减/别名/FTS/看板辅助
# 为什么下沉：fish_push 的闭包（1,048 行）依赖这些，而新模块不许 import fish（J2）。
# ============================================================

import json
import sqlite3
import datetime
import time

CONFIG_DEFAULTS = {
    "neighbors.strategy": ("mixed", "enum", ["code", "time", "tag", "type", "mixed"]),
    "neighbors.n": (3, "int", None),
    "neighbors.cross_project": (False, "bool", None),
    "neighbors.include_dormant": (False, "bool", None),
    "timezone": ("Asia/Shanghai", "string", None),
    # B1 门面根（2026-10-26）：空 = 按标准布局推导（<项目根>/fish-home）；非空 = 显式覆盖。
    # 为什么做成配置而不是只认常量：换布局（p0/fish-home）不必改代码；同时它是
    # "故意指错"的变异入口——指到 data_root 里面必须启动即报错，见 resolve_home_root。
    "layout.home_root": ("", "string", None),
    # 2026-10-25（用户要求 C）：看板按日切、**没有别的东西会关旧板**，
    # 所以 open 板会日复一日累积。sweep 把 N 天前的 open 板自动归档（不删）。
    # 为什么是 7 天：看板是"此刻"——一周前的板对"谁在动哪块"已经零价值，
    # 但它仍是历史（R5-7：不合并、不压缩、不删除），所以只把 status 置 closed。
    "board.sweep_days": (7, "int", None),
    # 2026-10-25（`EVAL-261025-13`）：重叠检测的"有效窗口"。
    # 为什么比 idle 阈值（30 分钟）宽：idle 是"渲染上标个灰"，而重叠警告是
    # "别去动这块"——**漏报一个刚走开的人，代价是撞车**；多报一个走开一小时的人，
    # 代价只是一句多余的话。所以宽到 90 分钟（"刚刚还在"的人不该被漏掉）。
    "board.overlap_window_min": (90, "int", None),
    # 2026-10-25 · P1-a：1500 → 1600。**这不是"不够用所以加"，是"留缓冲"**。
    # 实测（同一次注入包）：L2 精简前 1371/1500（只剩 129）；精简后 906/1500（剩 594）。
    # 594 已够装 16 §5.7 设计的 ~450 开场推送段。那为什么还加？
    #   · 加了候选段后约 1206–1356，1500 只剩 144–294 缓冲，任何一段波动都会触发裁剪，
    #     而裁剪第一位是 L2——L2 精简后只剩 139，被裁就完全没索引了；
    #   · 1600 给 244–394 缓冲，够吸收"某个项目 L1 条目变多"这类正常波动。
    # 为什么不是 1800：那会留 444–594 缓冲，超出必要，等于白占上下文。
    # **判据比数字重要**：每段都必须能被"这个会话里真的会用到"证明（见 `31` §3.2）。
    "inject.budget_tokens": (1600, "int", None),
    # 注入风格：gentle 温和（只标注这是状态）/ strong 强化（要求回答前先参考）
    # 用户反馈（2026-10-22）：呈现方式应可配，实际效果取决于宿主 AI 的服从度
    "inject.style": ("gentle", "enum", ["gentle", "strong"]),
    # 2026-10-26（FB-02）：规矩段每栏最多列几条。原来是写死的常量
    # （`RULES_PER_SOURCE_LIMIT = 3`）——写死意味着"想多看一条就得改代码"，
    # 而它恰恰是**因人而异**的口味：本库 rule 已有 39 条，3 条是默认，不是上限的真理。
    # 范围 1–10 在读取处夹紧（不新增校验分支：越界值应被纠正而不是让注入包报错）。
    "inject.rules_per_source": (3, "int", None),
    # 2026-10-26（FB-02）：看板段最多吃预算的多少比例。
    # 旧实现里看板**没有自己的上限**：看板一长就整段膨胀，而预算裁剪只能整段丢，
    # 于是"看板多了"会连带把别段挤掉。0.25 = 1/4，按 1600 预算约 400 token。
    "inject.board_max_ratio": (0.25, "float", None),
    # ── 身份：必须区分「主人」与「当前行动者」────────────────────────────
    # 教训（2026-10-22 实测）：只用一个 default_citizen 时，AI 调 whoami 会拿到
    # 主人的档案，然后自称"我是安逸"。违反"助手 ≠ 安逸本人"。
    "owner_citizen": ("", "string", None),   # 系统主人（resident）
    "default_citizen": ("", "string", None), # 已弃用：旧键，回退为 owner
    "actor_name": ("", "string", None),      # MCP 启动时由 FISH_ACTOR 覆盖

    # ── 衰减参数（全部可配置；天数 = 0 表示永不衰减）─────────────────────
    # 默认值按「工作节奏」推导，不按模型能力——它回答的是"信息多久失效"：
    #   1 个 sprint ≈ 2 周 / 1 个季度 ≈ 90 天 / 1 年 ≈ 365 天
    # 业界无公认参数（判断），因此全部暴露为配置项，P2 后用真实使用数据校准。
    "decay.episode_days": (21, "int", None),    # 流水：1.5 个 sprint，一个月后基本只用于审计
    "decay.fact_days": (60, "int", None),       # 事实：约 2 个月，覆盖一个迭代周期
    "decay.lesson_days": (365, "int", None),    # 教训：跨年仍有复用价值
    "decay.artifact_days": (180, "int", None),  # 产物：半年参考期
    "decay.rule_days": (0, "int", None),        # 铁律：规范权威，永不衰减
    "decay.identity_days": (0, "int", None),    # 身份：自我解释，永不衰减
    "decay.dormant_threshold": (0.5, "float", None),   # 低于此 → 休眠
    "decay.archive_threshold": (0.1, "float", None),   # 低于此 → 归档

    # ── 推送系统（13-推送系统设计；P0 只**登记**、不实现）──────────────────
    # 为什么现在就把旋钮写进来：`fish config list` 是"当前到底有哪些可调项"的唯一
    # 人可读出口；键名一旦进过文档/UI 再改就是破坏性变更。行为实现留给 P1/P2。
    # 判断依据仍然只是参数本身（不按模型能力调），P2 用真实使用数据校准。
    "push.enabled": (True, "bool", None),                 # 总开关（度量要做对照，得能关一天）
    "push.serendipity_ratio": (0.10, "float", None),      # 意外候选占比（防茧房闸 2）
    "push.open_budget_tokens": (450, "int", None),        # 开场推送预算（§5.1）
    "push.after_tool_budget_tokens": (100, "int", None),  # 工具后推送预算
    "push.situational_budget_tokens": (80, "int", None),  # 态势预警预算
    "push.after_tool_every_n_calls": (10, "int", None),   # 工具后推送频率：每 N 次最多 1 次
    "push.gap_window_days": (14, "int", None),            # 遗漏检测窗口（§3.3-B ②）
    "push.gap_others_read_min": (3, "int", None),         # 「别人常读」的次数下限
    "push.situational_window_min": (10, "int", None),     # 态势窗口（§3.3-C ②）
    "push.gap_early_phase_ratio": (0.30, "float", None),  # 「项目早期」分位（§3.3-B ④）
    # 推送结果判定窗口（16 §5.6）：超过这个窗口还没有任何反应 → 判 ignored。
    # 0 = 关闭判定（沿用 decay.*=0「永不」的既有约定，避免"0 小时窗口"被读成"立刻判死"）。
    "push.outcome_window_hours": (24, "int", None),

    # ── 会话凭证（18 §6/§9-1）：TTL ─────────────────────────────────────
    # 为什么默认 24h 而不是"永久"：token 明文会落进宿主会话日志（18 §9-1 已登记），
    # 它的暴露窗口必须短于"一条旧日志被人翻出来"的周期。0 = 永不过期（沿用
    # decay.*=0「永不」的既有约定）——留这个口子是为了本地调试，不是常规配置。
    # 2026-10-25（用户裁定）：**默认不过期**。
    #
    # 为什么改：公民与会话是 1:1（`find_session_citizen` 按 host_session_key 查），
    # 所以 **token 从设计上就是「会话的身份牌」**，而它的有效期却按墙钟算 24h——
    # 这两件事从来没对上过：
    #   · 短会话（1 小时）：token 多活 23 小时，纯浪费且制造僵尸行
    #   · 长会话（跨天）：第 25 小时 token 过期，**会话还好好的，身份却没了**，
    #     此后它写的记忆全部变成 unattributed，而它自己不知道
    #     （这正是 EVAL-261025-21「一次性失误=永久脏数据」的结构性来源）
    #
    # 而且 token **没有权限语义**（全库 permission/can_write 命中 0 行），
    # 一个只是"署名笔"的东西，按时间过期不带来任何安全收益。
    #
    # **新语义：token 的生命周期 = 会话的生命周期。**
    # 0 = 永不过期（与 decay.*=0 同一套约定）；>0 时保留给子代理等临时场景显式指定。
    "session.token_ttl_hours": (0, "int", None),
    # 2026-10-25（用户要求）：会话归档的活跃度阈值。**默认 0 = 不自动归档（无限期）**。
    # 未来的用户前端可以调这个值：超过 N 天没有任何活动 → 归档公民 + 吊销其 token。
    # 为什么不给个默认天数：用户明确"会话归档默认无限期，N 不取数"。
    # 而 DSH 没有 SessionEnd hook（实测只有 SessionStart/PreToolUse/PostToolUse/Stop，
    # Stop 是"这一轮生成结束"不是"会话结束"），所以"会话结束"没有事件可听，
    # 只能靠活跃度判据——那就更不能替用户拍一个数。
    "session.archive_after_days": (0, "int", None),

    # ── 语料层（2026-10-27 复审 🟠-1）──────────────────────────────────────
    # 日报里的语料采集**默认关闭**：全量采集是分钟级动作（实测 515 个 DSH 会话 +
    # 4GB WorkBuddy），挂上日报就等于每个日报用例/每次定时任务都去扫真实日志。
    # 开启：`fish config set corpus.daily_enabled 1`；临时关断：FISH_CORPUS_DISABLE=1。
    "corpus.daily_enabled": (False, "bool", None),
    # 评审 🟠-2（2026-10-27）：`corpus_retag` 任务读 `corpus.daily_retag`，但该键
    # 从未登记 → `_cfg_required` 抛 KeyError → 任务**永远 skipped**（测试用桩掩盖了它）。
    # 语义：缺省关；开启后维护管道会按需重算项目归属（`retag` 全量）。
    "corpus.daily_retag": (False, "bool", None),
    # 单帧解压上限（A①）：首采里 10+ 个帧超过旧的 8 MiB 被降级，实测最大帧 152 KB。
    "corpus.max_frame_bytes": (64 << 20, "int", None),

    # ── 动作采集（2026-10-26 · hooks 深度利用 T02）───────────────────────────
    # 事实表 `tool_activity` 的开关与保留策略。**只记写类**（读不改变任何东西，
    # 落库只会撑爆表与看板），且只记路径/目录/摘要（内容不落盘，见 §10.3 隐私纪律）。
    "activity.enabled": (True, "bool", None),
    # 14 天而不是 7：要能回答"一周分布"并留一天余量；表很小（单行 ~200B）。
    # 实测 >30MB 时优先降这个值，而不是砍字段。
    "activity.retention_days": (14, "int", None),
    # 同一 session+目录在窗口内合并成「N 次写」的聚合窗（也是"正在写"的窗口）。
    "activity.aggregate_window_min": (30, "int", None),
    # 快照写入节流：每次工具调用都写一个文件 + os.replace 会明显加重 IO。
    "activity.snapshot_min_interval_sec": (20, "int", None),
    # 快照的最大年龄：超过就当作"没有快照"（PreToolUse 最坏漏报 = 放行）。
    "activity.snapshot_max_age_sec": (3600, "int", None),
    # presence.auto_* 缓存的刷新节流（写它要 UPDATE 一行，不必每次写动作都刷）。
    "activity.presence_cache_min_interval_sec": (20, "int", None),

    # ── 自动在场 + 撞车（2026-10-26 · T03/T05）─────────────────────────────
    # 自动行算登记（用户 2026-10-26 拍板）：SessionStart 就建/刷自动行。
    "board.auto_presence": (True, "bool", None),
    # 会话维度**读侧**降级开关（2026-10-26 · 撞车自撞修复 §6.3）：
    #   true  = 会话行展开（看板 / board show 一行/会话，默认）；
    #   false = 读侧按公民折叠回「一行/公民」（事故止血，秒级可回，库里一条不删）。
    # 为什么**写侧**不跟着降级：`ON CONFLICT` 的冲突目标必须与某个唯一约束逐列一致，
    # 三列主键下写两列目标会直接报 `ON CONFLICT clause does not match`；写侧的回退手段
    # 是 `rollback_presence_session.py`（重建两列主键），不是"换一个冲突目标"。
    "board.presence_session_rows": (True, "bool", None),
    # 5 分钟 = active：比 idle（30 分钟）窄，因为"在动"要比"在不在"更严格。
    "board.active_window_sec": (300, "int", None),
    # 自动面的"正在写"窗口：与 aggregate_window_min 同量级（此刻谁在这块）。
    "board.collision_window_min": (30, "int", None),
    # **三态**（用户 2026-10-26 拍板 + 主理人决策）：off / warn（默认）/ ask。
    # 为什么默认 warn 而不是 True/ask：当前宿主 approvals disabled 时 `ask` 会被
    # **自动拒绝**——那会把"撞车提示"变成"撞车拒写"，正好违背它的目的。
    # warn = 不阻塞，靠 activity_overlap 推送 + 看板重叠提示提醒；ask 才真的弹批准。
    "board.collision_ask": ("warn", "enum", ["off", "warn", "ask"]),
    # 同 (session, target) 的冷却：只防"同一次调用重复弹窗"，不当作"用户已批准"。
    "board.collision_ask_cooldown_sec": (60, "int", None),
    # 路径判定的最小段数：1 段的面（`p0`/`src`/`docs`）到处同名，用它判撞车必然误报。
    "board.collision_min_segments": (2, "int", None),

    # ── 提示词递记忆（2026-10-26 · T04）────────────────────────────────────
    "push.prompt_enabled": (True, "bool", None),
    "push.prompt_budget_tokens": (120, "int", None),
    "push.prompt_max_items": (2, "int", None),
    # 90s：连续两条消息之间塞两次记忆，读者只会跳过。
    "push.prompt_min_interval_sec": (90, "int", None),
    "push.prompt_min_relevance": (0.6, "float", None),
    # 陈旧性硬门槛：与 band 的 stale 阈值同值但**可独立调**（见 §6.5）。
    "push.prompt_stale_floor": (0.35, "float", None),
    # 同会话 24h 内不重复推同一编码（与 _recent_pushed_codes 同一口径）。
    "push.prompt_exclude_hours": (24, "int", None),
}

_DEFAULT_DECAY = {k: v[0] for k, v in CONFIG_DEFAULTS.items() if k.startswith("decay.")}

def parse_utc(s):
    if not s:
        return None
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except ValueError:
        return None

def local_date() -> str:
    return datetime.datetime.now().strftime("%y%m%d")

def _presence_columns(conn) -> tuple:
    """`presence` 当前真实列名（按声明序）。表不存在 → `()`。"""
    try:
        return tuple(str(r[1]) for r in conn.execute("PRAGMA table_info(presence)"))
    except sqlite3.Error:
        return ()

def _presence_has_session(conn) -> bool:
    """`presence.session_key` 列在不在（读侧兜底的总开关，见设计 §2.1.1 的 F1/F2/F3）。

    为什么不直接假定它在：迁移可能因"备份写不进"而**故意不升级**（设计步 5/6），
    此时库还是两列形态，任何 `SELECT p.session_key` 都会 `no such column`。
    读侧必须能应付这种过渡态——否则"宁可这次不升级"就变成了"这次直接罢工"。
    """
    return "session_key" in _presence_columns(conn)

def _board_seen_columns(conn) -> tuple:
    """`board_seen` 当前真实列名（按声明序）。表不存在 → `()`。"""
    try:
        return tuple(str(r[1]) for r in conn.execute("PRAGMA table_info(board_seen)"))
    except sqlite3.Error:
        return ()

def _key_type(key):
    if key not in CONFIG_DEFAULTS:
        raise KeyError(f"未知配置键：{key}（可用：{', '.join(sorted(CONFIG_DEFAULTS))}）")
    return CONFIG_DEFAULTS[key]

def get_config(conn, key):
    default, typ, _ = _key_type(key)
    row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    raw = row[0]
    if typ == "int":
        return int(raw)
    if typ == "float":
        return float(raw)
    if typ == "bool":
        return raw == "true"
    return raw

def push_outcome_window_hours(conn) -> int:
    """结果判定窗口（小时）。<=0 = 关闭判定（沿用 decay.*=0「永不」的既有约定）。"""
    return int(get_config(conn, "push.outcome_window_hours"))

_ALIAS_KINDS = ("project", "citizen")
_ALIAS_MAX_HOPS = 8

def _alias_map(conn, kind):
    """一次读出该 kind 的全部别名（from→to）。行数是个位数，不做增量查询。"""
    return dict(conn.execute(
        "SELECT from_name, to_name FROM name_aliases WHERE kind=?", (kind,)).fetchall())

def _walk_alias(amap, start):
    """沿别名走到规范名，返回 `(canonical, chain)`。成环时停在环入口（不抛错）。"""
    chain, cur = [start], start
    for _ in range(_ALIAS_MAX_HOPS):
        nxt = amap.get(cur)
        if not nxt or nxt in chain:
            break
        chain.append(nxt)
        cur = nxt
    return cur, chain

def resolve_name(conn, kind, value) -> dict:
    """把一个名字解析成**规范名**（单向别名展开，最多 8 跳）。

    返回 `{"input","canonical","aliases","chain","status","found"}`：
      · `canonical` —— 最终规范名（没有别名时就是 input 本身）
      · `aliases`   —— **全部**指向 canonical 的别名（含传递别名，按字母序，便于断言）
      · `chain`     —— 展开路径 `[input, …, canonical]`
      · `status`    —— 规范名的状态（project: active/closed/retired；citizen: active/…）
      · `found`     —— 规范名是否真实存在于对应表

    成环**不抛错**：`chain` 里看得出停在哪儿。写入侧会拒绝成环（`link_alias`），
    这里只是不让一条坏数据把读路径打挂——**读要永远能出结果**。
    """
    if kind not in _ALIAS_KINDS:
        raise ValueError(f"kind 只能是 {_ALIAS_KINDS}，收到 {kind!r}")
    start = (value or "").strip()
    empty = {"input": start, "canonical": "", "aliases": [], "chain": [],
             "status": None, "found": False}
    if not start:
        return empty
    amap = _alias_map(conn, kind)
    canonical, chain = _walk_alias(amap, start)
    aliases = sorted(f for f in amap
                     if f != canonical and _walk_alias(amap, f)[0] == canonical)
    status, found = None, False
    if kind == "project":
        row = conn.execute("SELECT status FROM code_registry WHERE prefix=?",
                           (canonical,)).fetchone()
    else:
        row = conn.execute(
            "SELECT status FROM citizens WHERE display_name=? AND status='active' "
            "ORDER BY created_at, citizen_id LIMIT 1", (canonical,)).fetchone()
    if row:
        found, status = True, row[0]
    return {"input": start, "canonical": canonical, "aliases": aliases,
            "chain": chain, "status": status, "found": found}

def resolve_project_names(conn, project) -> list:
    """给定一个项目名，返回"同一个项目"的**全部名字**（规范名 + 别名）。用于 IN 查询。

    这是别名机制对检索的唯一承诺：`recall(project='cdql')` 也返回 `CDQL` 的记忆。
    **写入仍按原名归属**（用户 2026-10-25 拍板：别名只影响检索，不改已有数据的归属语义）。
    """
    p = (project or "").strip()
    if not p:
        return []
    info = resolve_name(conn, "project", p)
    out = []
    for n in [info["canonical"]] + info["aliases"]:
        if n and n not in out:
            out.append(n)
    return out or [p]

def _index_one_fts(conn, code, title, tags, body):
    """把一条记忆写进全文索引（覆盖式，幂等）。"""
    conn.execute("DELETE FROM memory_fts WHERE code=?", (code,))
    title_part = " ".join([code or "", title or "", " ".join(tags or [])])
    conn.execute("INSERT INTO memory_fts (code, title_idx, body_idx) VALUES (?,?,?)",
                 (code, fts_text(title_part), fts_text(body)))
    conn.commit()

def reindex_fts(conn, root) -> int:
    """全量重建全文索引；返回索引条数。可从 code_index + 正文文件完整重建。"""
    root = Path(root)
    conn.execute("DELETE FROM memory_fts")
    n = 0
    for code, title, tags_s, fp in conn.execute(
            "SELECT code,title,tags,file_path FROM code_index").fetchall():
        body = ""
        if fp:
            p = root / fp
            if p.exists():
                try:
                    body = p.read_text(encoding="utf-8")
                except OSError:
                    body = ""
        try:
            tags = json.loads(tags_s or "[]")
        except (ValueError, TypeError):
            tags = []
        _index_one_fts(conn, code, title, tags, body)
        n += 1
    return n

def ensure_fts(conn, root) -> int:
    """索引缺失时自动补建（自愈）。

    为什么要它：老库升级后 memory_fts 是空表，此时检索会静默变弱——
    那种"看起来正常但查不到"的状态最难发现。返回补建的条数（0 = 无需补）。
    """
    try:
        have = conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]
    except sqlite3.Error:
        return 0
    need = conn.execute("SELECT COUNT(*) FROM code_index").fetchone()[0]
    if need and not have:
        return reindex_fts(conn, root)
    return 0

def load_decay_cfg(conn):
    """从 config 表读出全部衰减参数（缺项回落默认值）。"""
    return {k: get_config(conn, k) for k in CONFIG_DEFAULTS if k.startswith("decay.")}

def compute_decay(mem_type, last_accessed_at, recorded_at, cfg=None):
    """返回 (decay, state)。decay ∈ (0,1]，1=全新。

    cfg 缺省时用内置默认值（供测试与无库场景使用）。
    天数为 0 或 None = 永不衰减（规范性记忆）。
    """
    cfg = cfg or _DEFAULT_DECAY
    hl = cfg.get(f"decay.{mem_type}_days", 90)
    if hl is None or int(hl) <= 0:
        return 1.0, "active"
    ref = parse_utc(last_accessed_at) or parse_utc(recorded_at)
    if not ref:
        return 1.0, "active"
    days = (datetime.datetime.now(datetime.timezone.utc) - ref).total_seconds() / 86400
    decay = 0.5 ** (max(0.0, days) / int(hl))
    dormant_t = float(cfg.get("decay.dormant_threshold", 0.5))
    archive_t = float(cfg.get("decay.archive_threshold", 0.1))
    if decay > dormant_t:
        return decay, "active"
    if decay > archive_t:
        return decay, "dormant"
    return decay, "archived"

def _utc_str(value) -> str:
    """把 datetime（或已是字符串的值）变成 `%Y-%m-%dT%H:%M:%SZ` 的 UTC 串。

    为什么要这个转换：`datetime.isoformat()` 产出 `2026-10-26T01:23:45.123+00:00`，
    而 fish 全库只认 `2026-10-26T01:23:45Z`（定长、可直接比大小）。
    两者混用会让 `parse_utc` 静默返回 None —— 那是"时间判断全部失效但不报错"的最坏形态。
    """
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.timezone.utc)
        return value.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value or "")

ATTENTION_TITLE_MAX_CHARS = 50      # ④ 标题截断长度（只截这里；③ 的标题不截）
ATTENTION_OPEN_WINDOW_DAYS = 30     # A 未闭合：近 30 天

ATTENTION_OPEN_KEYWORDS = ("未闭合", "待办", "下一步", "todo", "待确认",
                           "待定", "待验证", "待补")

def _clip_chars(text, limit):
    """按**字符**截断（不是 token），并把换行压成空格。

    为什么按字符：④ 段每行是给人看的一句话，字符数最直观；token 门槛留给整段预算。
    """
    s = (text or "").strip().replace("\r", " ").replace("\n", " ")
    if limit <= 0 or len(s) <= limit:
        return s
    return s[:max(0, limit - 1)] + "…"

def _open_loop_predicate():
    """A 类「未闭合事项」的**唯一谓词**（`_open_loop_rows` 与 `count_open_loop_rows` 共用）。

    分出来的理由与 `_list_where` 完全相同：段头要印「显示 N/M 条」，而 M 必须与
    真正的取数用**同一个谓词**。两处各拼一份，迟早出现"显示 1/3 条"但实际有 5 条
    这种无法自证的数字——那比不声明更坏（读者会相信那个 M）。
    """
    cond = " OR ".join("INSTR(LOWER(COALESCE(title,'')), ?) > 0"
                       for _ in ATTENTION_OPEN_KEYWORDS)
    return ("status='active' AND dormant=0 AND recorded_at >= ? AND (" + cond + ")",
            [k.lower() for k in ATTENTION_OPEN_KEYWORDS])

def _open_loop_rows(conn, since_z, limit, exclude=(), *, project=None):
    """A 类「未闭合事项」：标题命中开放词表的 active 记忆（窗口内，时间降序）。

    `project=None`（默认）= 旧行为，**跨项目**（注入包一直这么用，判据
    `test_push_p1a_candidates` 是它的守护者）。
    `project=<前缀>` = 只取该项目的未闭合事项（B2 状态表的「未了事项」列）。

    **合并裁决 2026-10-26**：谓词走 `_open_loop_predicate()`（与
    `count_open_loop_rows` 同源）——段头印「显示 N/M」，M 必须与取数用同一个
    谓词；`project` 仍是**仅关键字**参数（既有调用全是位置调用）。
    """
    where, keywords = _open_loop_predicate()
    sql = "SELECT code,title FROM code_index WHERE " + where
    params = [since_z] + list(keywords)
    if project is not None:
        sql += " AND project = ? "
        params.append(str(project))
    sql += " ORDER BY recorded_at DESC, code DESC LIMIT ?"
    params += [int(limit) + len(set(exclude))]
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for code, title in rows:
        if code in exclude:
            continue
        out.append((code, (title or "").strip()))
        if len(out) >= int(limit):
            break
    return out

def clean_prompt(text, *, max_len=4000) -> str:
    """把用户提示词**洗干净**：丢掉 fish 自己注入的行，压平换行。

    为什么必须洗（设计 §6.4）：SessionStart 注入块会被宿主原样带进下一轮 prompt，
    于是"按提示词递记忆"会把**我们自己的话**当成用户意图去检索——那是纯噪音，
    而且会持续自我强化（推的内容变成下一轮的 query）。

    洗法：按行丢弃含 `fsh_` / `【当前状态` / `[fish]` / `登记身份` 的行（
    `fish_activity.INJECTION_MARKERS` 是**同一份**标记表，prompt hook 也用它快筛），
    再压平空白。原始 prompt **绝不落盘**（这里只返回处理后的文本，不写日志）。
    """
    kept = []
    # 延迟导入：`fish_common` 是基础层，`fish_activity` 是领域层；顶层 import 会把
    # "基础→领域"写死，局部导入只在实际用到时建立依赖（T02 拆包后 clean_prompt 随之下沉）。
    import fish_activity
    for line in str(text or "").splitlines():
        if any(marker in line for marker in fish_activity.INJECTION_MARKERS):
            continue
        kept.append(line.strip())
    return " ".join(part for part in kept if part)[:max_len]

def board_period(period_key=None) -> str:
    """看板的周期键：按日（`YYYYMMDD`），与 `daily` 的切分口径一致。"""
    return period_key or local_date()
