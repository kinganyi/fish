#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fish v0.3 — 大肥鱼生命系统 · P1a
================================
在 P0 基础上增加：
  1. config 表 + 默认值（邻近策略、时区、注入预算）
  2. neighbors 四策略（code / time / tag / type / mixed）
  3. recall 记录访问（last_accessed_at / access_count / dormant）
  4. typed decay（按记忆类型分层的半衰期）
  5. citizens 启用，kind = resident / visitor / role / system
  6. forgetting / config / citizen / whoami 命令

P0 已验证且不变的部分：事件表 + 两个不可变触发器 + next_code 的
BEGIN IMMEDIATE 并发分配 + rebuild 逻辑。
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import uuid
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent / "data"

# ------------------------------------------------------------------ 配置

CONFIG_DEFAULTS = {
    "neighbors.strategy": ("mixed", "enum", ["code", "time", "tag", "type", "mixed"]),
    "neighbors.n": (3, "int", None),
    "neighbors.cross_project": (False, "bool", None),
    "neighbors.include_dormant": (False, "bool", None),
    "timezone": ("Asia/Shanghai", "string", None),
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
}

_DEFAULT_DECAY = {k: v[0] for k, v in CONFIG_DEFAULTS.items() if k.startswith("decay.")}

KINDS = ("resident", "visitor", "role", "system")


# ------------------------------------------------------------------ 基础

def ulid() -> str:
    ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    return f"{ts:013d}{uuid.uuid4().hex[:13].upper()}"


def now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_occurred_at(value):
    """`occurred_at` 校验：缺省 = 现在；给了就必须是可解析的 ISO8601 UTC。

    为什么要校验：它是"这件事什么时候发生"的事实（02 §2.3 双时态的起点）。
    收下一个解析不了的字符串，等于让时间线悄悄断掉。
    """
    if value in (None, ""):
        return now_utc()
    if parse_utc(value) is None:
        raise ValueError(
            f"occurred_at 必须是 ISO8601 UTC 格式（如 2026-10-22T04:00:00Z），收到 {value!r}")
    return value


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


# ------------------------------------------------------ 推送地基 DDL（单一定义来源）
# 依据：13-推送系统设计 §2 + 16 §5.6。这是推送系统的地基层，P0 必须先建：
#   没有 memory_access_log（谁读过什么）画像只能猜；没有 links（什么和什么有关）候选只能乱；
#   没有 push_log（我推过什么、用户怎么反应）时机策略永远无数据可学。
#
# 为什么提成常量而不是直接抄进 SCHEMA：同一段 DDL 有两个消费者——
#   `SCHEMA`（新库一次建好）与 `migrate()`（旧库补建）。复制两份是"改一处忘另一处"的标准起点。
#
# 为什么索引**不**写在这里（07 的 is_owner 事故，回归见 test_legacy_db_migration_owner）：
#   旧库里若已存在同名表而列不齐，`CREATE INDEX` 会报 no such column，
#   而 SCHEMA 是先于 migrate 执行的 → 整个升级路径直接崩。
#   索引统一放在 migrate() 里「先建表 → 再补列 → 最后建索引」。
_ACCESS_LOG_DDL = """CREATE TABLE IF NOT EXISTS memory_access_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  code        TEXT NOT NULL,
  citizen_id  TEXT,
  session_key TEXT,
  project     TEXT,
  via         TEXT NOT NULL,
  payload     TEXT,
  accessed_at TEXT NOT NULL
);"""

# 字段语义（与 §2.1 一一对应）：
#   citizen_id  谁读的（会话公民；空 = 未登记/未归属）
#   session_key 哪次会话（同一宿主下区分 anyi-dev / anyi-review 这一级）
#   project     读的时候在哪个项目（档期机制未落地时 = 编码所属项目，见 append_access_log）
#   via         recall | resolve | read_body | inject | push | manual（代码里校验）
#   payload     可选 JSON：推送候选 id 等，用于度量采纳率（§8.2）

_LINKS_DDL = """CREATE TABLE IF NOT EXISTS links (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  src                TEXT NOT NULL,
  dst                TEXT NOT NULL,
  rel                TEXT NOT NULL,
  source             TEXT NOT NULL,
  weight             REAL DEFAULT 1.0,
  evidence           TEXT,
  created_at         TEXT NOT NULL,
  removed_at         TEXT,
  last_reinforced_at TEXT,
  reinforce_count    INTEGER DEFAULT 0,
  suppressed         INTEGER DEFAULT 0,
  UNIQUE(src, dst, rel)
);"""

# 存储约定（§2.2）：**边单向存、查询双向做**。
#   rel    supersedes | related | derived | contradicts | precedes（见 LINK_RELS）
#   source explicit | structural | semantic | manual（见 LINK_SOURCES）
#   weight 0~1，可比；evidence 是"为什么建这条边"的人话，查不到理由的边等于噪音
# 表里不设 CHECK 约束：加关系类型是规格内动作（03 §F），不该每次都迁移表。
#
# 再巩固预留（14 §6 修正 1 / 15 §4，REALM arXiv 2609.16053）：
#   论文的结论是**边权重不是写入时算死的静态值**，而是每次被使用／被一次无效推送
#   之后都会被改写。要支持这一点，只靠 weight 一个数不够——还得知道"上次改成什么
#   时候、改过几次、这条边是不是已经被判定为噪音"。于是有这三列：
#     last_reinforced_at 最后一次被强化/调整的时间（NULL = 从没调过，不是 0 时刻）
#     reinforce_count    累计被调整次数（单调递增，用于抑制"反复抖动"的边）
#     suppressed         0=正常 1=已被抑制（反复引入无关结果 → 不再参与候选）
#   **本轮只加列，不实现任何再巩固逻辑**（那是 P1）：写入口一个都不动，三列全走默认值。
#   为什么不等到 P1 再加：现在建表就带上，将来不必为一个纯加列再做一次迁移——
#   而 07 的 is_owner 事故说明"后期补列"是这套库里最容易崩的一步。

# 推送批次日志（16 §5.6；论文《When Should the Agent Speak?》§6.3）。
#
# 论文的原话是：所有记忆系统只暴露一个查询——"检索当前请求相关的内容"，
# **没有一个暴露时机策略真正需要的那个**：用户对系统**过去干预**的响应历史。
# 补这个缺口不需要新架构，只需要把"我推了什么"和"用户怎么反应"写回存储。
#
# 一行 = 一次推送里的**一条记忆**：同一次 inject 展示 N 条 → N 行，共享 pushed_at /
# budget_tokens。为什么不把 N 条压成一行 JSON：采纳率要按 code 聚合（哪类记忆推了有用），
# 压在 JSON 里每次都得解包再算，ix_push_code 也会变成空转。
#
# 字段语义：
#   channel       inject | hook | tool_after | situational（代码里校验，见 PUSH_CHANNELS）
#   budget_tokens 这次推送**批次**占的预算（同批次 N 行同值，不是单条成本）
#   candidate_id  候选池 id —— 候选池没实现之前恒为 NULL（**不许编**）
#   reaction      NULL=待判 | accepted | ignored | expired
#                 accepted = 之后读了正文 / 校验了编码；ignored = 窗口内没人理（daily_report 判）
#                 expired 只占位：它需要独立的过期信号源，P0 不实现（P0 不写它的任何值）
#   reacted_at    真正**发生反应**的时间；ignored 是"判定"不是"反应" → 保持 NULL
#   latency_sec   reacted_at − pushed_at（只对 accepted 有值）
#
# 与 16 §5.6 的两表方案（push_log + push_outcome）的差异：合并成一张。
# 理由是两张表严格 1:1、都靠 push_id 连，拆开只多一次 join 与一次"两边写不齐"的
# 机会；而 07 的教训是"同一件事不要存两处"。字段一个没少，语义不变。
_PUSH_LOG_DDL = """CREATE TABLE IF NOT EXISTS push_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  session_key   TEXT,
  citizen_id    TEXT,
  project       TEXT,
  code          TEXT,
  candidate_id  TEXT,
  channel       TEXT NOT NULL,
  budget_tokens INTEGER,
  pushed_at     TEXT NOT NULL,
  reaction      TEXT,
  reacted_at    TEXT,
  latency_sec   INTEGER
);"""

_PUSH_SCHEMA = ("\n" + _ACCESS_LOG_DDL + "\n\n" + _LINKS_DDL
                + "\n\n" + _PUSH_LOG_DDL + "\n")


# ------------------------------------------------------ 身份层 P1-a DDL（纯结构）
# 依据：身份层设计（账号 → 会话 → 凭证 → 档期 → 归档 → 合并视图）。
#
# **本轮只建结构，一句既有查询都不改。** 这不是保守：`citizens` 表被 28 条 SQL 引用
# （fish.py 25 条 / mcp_server.py 2 条 / daily_report.py 1 条），其中 fish.py 的
# cmd_healthcheck 与 rebuild_projection 两处是「校验 owner 是不是有效公民」。
# 在这张表上顺手改查询，风险会顺着全部调用点扩散；结构先就位、逻辑下一轮单独切，
# 出问题时才能一刀切开「结构错」还是「逻辑错」。这 28 条由 test_identity_p1a 的
# 判据 G 逐条冻结——语句改一个字就红（空白归一后比对）。
#
# 本轮只做 6 件事：citizens 加 5 列 / 建 agent_accounts / 建 session_tokens /
# 建 project_stints / 建 citizens_archive / 建 citizens_all 视图。
# **不实现**归档、不实现 token 签发与校验、不改任何工具签名。
#
# 沿用推送地基的同一套纪律（见 _ACCESS_LOG_DDL 的说明）：
#   · 表 DDL 提成常量，`SCHEMA`（新库）与 `migrate()`（旧库）共用一份；
#   · **索引与视图只放 migrate()**：SCHEMA 先于 migrate 执行，此刻旧库的列还没补齐；
#   · 顺序硬纪律：先建表 → 再补列 → 最后建索引/视图。

# 账号 = 跨会话聚合锚点。一个 AI 账号下可以有多个会话公民（citizens.account_id 指过来）。
# kind 词表（visitor | role | resident）**刻意不设 CHECK**：它是词表，随规格会长；
# links.rel 的既有决策就是"加点值是规格内动作，不该每次迁移表"。
# 下面的 session_tokens 性质不同——它的 CHECK 锁的是**权限升级路径**，不是词表。
_AGENT_ACCOUNTS_DDL = """CREATE TABLE IF NOT EXISTS agent_accounts (
  account_id   TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  host         TEXT,
  kind         TEXT,
  created_at   TEXT NOT NULL,
  closed_at    TEXT,
  note         TEXT
);"""

# 会话凭证：库里只存 sha256(token)，明文只在签发那一刻回一次（表里没有明文列可存）。
#
# 三条 CHECK 锁的是**权限升级路径**（评审 R-2 的教训：MCP 面不得签发 machine 级）：
#   evidence  machine | inherited | self-reported —— 这个身份主张是谁背书的
#   channel   cli | hook | parent                 —— 这条凭证是走哪条路拿到的
# "MCP 面不得签发 machine 级"最终还得靠「签发入口不在 MCP」来保证——CHECK 挡不住一段
# 绕过入口的代码，但它能挡住手写的 bad row，而 bad row 恰恰是审计时最难解释的东西。
#
# 第 4 条 CHECK（channel='parent' → evidence='inherited'）是**本轮补的判断**，理由：
#   channel='parent' 的语义是"这条凭证不是本进程自己签的，是从父会话继承来的"。
#   继承来的身份没经过本进程的机器绑定校验，所以它的证据级别只能是 inherited。
#   若允许 channel='parent' + evidence='machine'，一条低可信的子会话凭证就能贴上
#   machine 标签，随后被任何"只看 evidence"的校验当成本机直接签发——这正好是 R-2
#   要堵的那条路，只是绕了一层（parent）而不是绕过入口。→ 加。
#   代价为零：合法路径（parent 必带 parent_token）本来就只有 inherited 一种取值。
# 反方向（evidence='inherited' → channel 必须是 'parent'）**刻意不加**：
#   SQLite 的 CHECK 一旦建上，去掉要重建整张表；而"cli 渠道但证据是继承"是一种
#   **低报**（少主张可信度），不构成越权；多锁一条却会给下一轮的真实签发路径留一个
#   必须停机迁移的坑。宁可少锁一条，不锁错的。
_SESSION_TOKENS_DDL = """CREATE TABLE IF NOT EXISTS session_tokens (
  token_hash   TEXT PRIMARY KEY,   -- sha256(token)，明文只在签发时回一次
  citizen_id   TEXT NOT NULL,
  session_key  TEXT NOT NULL,
  evidence     TEXT NOT NULL,      -- machine | inherited | self-reported
  channel      TEXT NOT NULL,      -- cli | hook | parent
  parent_token TEXT,               -- inherited 时指向父
  issued_at    TEXT NOT NULL,
  expires_at   TEXT,
  revoked_at   TEXT,
  CHECK (channel IN ('cli','hook','parent')),
  CHECK (evidence IN ('machine','inherited','self-reported')),
  CHECK (channel != 'parent' OR parent_token IS NOT NULL),
  CHECK (channel != 'parent' OR evidence = 'inherited')
);"""

# 项目档期：某公民在某项目上的时间区间，to_ts IS NULL = 当前开档期。
#
# 2026-10-24 语义变更（用户裁定）：**一个会话可以同时服务多个项目**。
# 依据是实际工作形态：AI 常常跨项目干活（既改 ANYI，又查企业智能），而旧不变量把
# "会话"和"项目"绑成一夫一妻，多项目只能靠反复切换来伪造，档期追溯反而是假的。
# 于是库层不变量从「一人一个开档期」改成「**一人一项目一个**开档期」：
#   · 同一 citizen + 同一 project 同时只能有一条开档期（挡住重复开档）
#   · 不同 project 可以并存（多项目并行是合法状态，不是重复）
# 注意：**每条记忆仍只归属一个项目**（code_index.project 是单值）——档期是"会话在
# 哪些项目上干过"的多值事实，记忆归属的单值事实没变，两者不是一回事。
# 为什么仍锁在库层：写入口会有多个（CLI / 迁移脚本 / 将来的 hook），不变量只能有一个位置。
# 用**部分**唯一索引而不是普通唯一索引：公民换过几个项目是正常事实，
# 唯一性只约束"当前开着的这条"，不约束"一共开过几个"。
# 入口语义见 open_project_stint（同项目复用、异项目新增、**不关旧档期**）。
_PROJECT_STINTS_DDL = """CREATE TABLE IF NOT EXISTS project_stints (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  citizen_id    TEXT NOT NULL,
  account_id    TEXT,
  project       TEXT NOT NULL,
  from_ts       TEXT NOT NULL,
  to_ts         TEXT,               -- NULL = 当前开档期
  set_by        TEXT,
  closed_reason TEXT
);"""

# 归档表：**必须与 citizens 逐列一致**（名字、类型、顺序、默认值），否则 citizens_all
# 视图对不上位——UNION ALL 只按**列位置**拼，不按列名认。
# 两份 DDL 是手抄的，所以不靠注释提醒、靠判据：test_identity_p1a 的判据 A 拿
# PRAGMA table_info 逐列比对两张表，改一处忘另一处当场红。
_CITIZENS_ARCHIVE_DDL = """CREATE TABLE IF NOT EXISTS citizens_archive (
  citizen_id       TEXT PRIMARY KEY,
  display_name     TEXT,
  kind             TEXT,
  role             TEXT,
  host             TEXT,
  status           TEXT DEFAULT 'active',
  created_at       TEXT,
  is_owner         INTEGER DEFAULT 0,
  account_id       TEXT,
  device_id        TEXT,
  host_session_key TEXT,
  last_seen_at     TEXT,
  archived_at      TEXT
);"""

# citizens 的 P1-a 补列清单（旧库升级路径）。声明必须是**能加到非空表上**的形态
# （SQLite 的 ALTER ADD COLUMN 不允许无默认值的 NOT NULL）——这 5 列全是可空 TEXT，
# 自然满足；将来若加 NOT NULL 列，必须带 DEFAULT。
# 顺序即 citizens 的末尾列序，**必须与 _CITIZENS_ARCHIVE_DDL 的末尾 5 列同序**。
_CITIZENS_NEW_COLS = (
    ("account_id", "TEXT"),        # 属于哪个 AI 账号（跨会话聚合的血缘）
    ("device_id", "TEXT"),         # 设备（本地阶段留空）
    ("host_session_key", "TEXT"),  # 宿主给的会话标识（如 DSH_SESSION_ID）
    ("last_seen_at", "TEXT"),      # 最后活跃
    ("archived_at", "TEXT"),       # 归档时间（NULL = 未归档）
)

# 合并视图的列契约。**顺序就是拼接口**：两份 SELECT 共用这一份列名，写两遍就会漂，
# 而这里漂了的后果是静默的（错列/少列都不报错，只是数据错位）。
_CITIZENS_ALL_COLUMNS = (
    "citizen_id", "display_name", "kind", "role", "host", "status", "created_at",
    "is_owner", "account_id", "device_id", "host_session_key", "last_seen_at",
    "archived_at",
)


def _citizens_all_view_ddl() -> str:
    """citizens_all = 热表 ∪ 归档表。**显式列名，不用 `SELECT *`。**

    为什么不用 `*`：两边列一不同步，`*` 会静默错位（多/少一列就整行右移），
    而显式列名在缺列时直接报 no such column——错得响，才修得动。

    为什么是 UNION ALL 不是 UNION：归档是"搬"不是"复制"，同一个 citizen_id 不该在
    两张表里同时存在；万一真的同时存在，那是搬迁逻辑的真错误，UNION 会把它去重吞掉，
    视图就成了坏数据的遮羞布。宁可让上层看见两条、去查搬迁。
    """
    cols = ", ".join(_CITIZENS_ALL_COLUMNS)
    return ("CREATE VIEW IF NOT EXISTS citizens_all AS "
            f"SELECT {cols} FROM citizens "
            f"UNION ALL SELECT {cols} FROM citizens_archive")


# 开档期唯一索引的目标定义（2026-10-24：一列 → 两列）。
# **单独提成常量**，因为它有一个别的索引没有的问题：**名字没变、定义变了**。
# 旧库里 `ux_stint_open` 已经存在（`ON project_stints(citizen_id) WHERE to_ts IS NULL`），
# 而 `CREATE UNIQUE INDEX IF NOT EXISTS` 一见同名索引就直接跳过——旧的一列索引会带着
# 旧语义原地活下来，多项目并行当场被它拦住（而且报错发生在插入时，离原因很远）。
# 所以它的建法不能是"清单里一条幂等 CREATE"，必须走 _ensure_stint_open_index：
# **先按 PRAGMA 判断现有定义 → 不对就 DROP → 再 CREATE**。
_STINT_OPEN_INDEX_NAME = "ux_stint_open"
_STINT_OPEN_INDEX_COLS = ("citizen_id", "project")
_STINT_OPEN_INDEX_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_stint_open "
    "ON project_stints(citizen_id, project) WHERE to_ts IS NULL")


# 身份层的索引清单（只放 migrate，不放 SCHEMA：SCHEMA 先于 migrate 执行）。
# 口径：这里只放**定义不会变**的索引。定义会变的那个（开档期唯一索引）由
# _ensure_stint_open_index 单独负责——两种生命周期不该塞进同一个循环里。
_IDENTITY_INDEXES = (
    # 凭证撤销的两条主查询：按公民批量吊销 / 按会话查还有哪些活的
    "CREATE INDEX IF NOT EXISTS ix_session_tokens_citizen ON session_tokens(citizen_id)",
    "CREATE INDEX IF NOT EXISTS ix_session_tokens_session ON session_tokens(session_key)",
    # account_id 存在的唯一理由就是"按账号聚合会话"，这条单列索引是它的取数口。
    # 刻意不给 device_id / last_seen_at 建索引：还没有查询的东西不加索引。
    "CREATE INDEX IF NOT EXISTS ix_citizens_account ON citizens(account_id)",
)

# 四张新表的 DDL，**按依赖顺序**（citizens_archive 必须早于 citizens_all 视图）。
# 提成一份元组是为了让两个消费者共用同一份：SCHEMA 拼进去（新库）与
# _ensure_identity_tables 循环建（旧库/幂等）。第三份拷贝就是漂移的起点。
_IDENTITY_TABLE_DDLS = (_CITIZENS_ARCHIVE_DDL, _AGENT_ACCOUNTS_DDL,
                        _SESSION_TOKENS_DDL, _PROJECT_STINTS_DDL)

_IDENTITY_SCHEMA = "\n" + "\n\n".join(_IDENTITY_TABLE_DDLS) + "\n"

# ------------------------------------------------------ 工作区 → 项目映射（R1 根治）
# 为什么需要这张表：注入包里 L1「当前项目」原先靠 `GROUP BY project ORDER BY MAX(
# recorded_at)` **猜**——谁最近更新过记忆，谁就是"当前项目"。外部评估 EVAL-261024-02
# 的 R1 就是拿这件事定的级：读完 EVAL 自己的两条记忆后，注入包立刻把 EVAL 报成当前
# 项目，而实际在干的是 ANYI。**猜错项目比不报项目危险得多**（AI 会基于错误前提工作），
# 所以项目归属必须有一个外部事实源，这张表就是那个源头：某个工作区路径**归属**某个项目。
#
# 语义：一行 = "以 path_prefix 为前缀的工作区都属于 project"。归属按**最长前缀**解析，
# 因此父子目录可以各有归属（如 /a → P1、/a/b → P2，/a/b/c 解析成 P2）。
#
# 归一化纪律（必须严格，否则最长前缀匹配会错）：正斜杠、无尾斜杠、**全小写**。
# 为什么要小写：Windows 的路径比较不区分大小写（`I:/APP/X` 与 `i:/app/x` 是同一个
# 目录），不统一大小写就会出现"明明映射了却匹配不上"的幽灵问题。代价是 POSIX 上
# 大小写敏感的路径会被当成同一个——本系统跑在 Windows 上，这个取舍写在
# normalize_workspace_path 的 docstring 里，不让它只活在注释里。
#
# 与 project_stints 的分工（别混）：档期是"这个会话现在在哪些项目上干活"（会话事实，
# 会随会话结束关闭）；workspace_map 是"这个目录属于哪个项目"（环境事实，长期有效）。
# 两者都不等于"最近更新"，这正是 R1 的两条新证据链。
_WORKSPACE_MAP_DDL = """CREATE TABLE IF NOT EXISTS workspace_map (
  path_prefix TEXT PRIMARY KEY,   -- 归一化后的路径前缀（正斜杠、无尾斜杠、全小写）
  project     TEXT NOT NULL,      -- 项目前缀（如 ANYI）
  note        TEXT,
  created_at  TEXT NOT NULL
);"""

# 表在**两个消费者**里各建一次：SCHEMA（新库）与 migrate（旧库升级），共用同一份 DDL。
# 沿用身份层/push 层的纪律：表 DDL 提成常量，索引与视图只放 migrate（这张表目前
# 不需要额外索引：行数是个位数，最长前缀匹配在 Python 里做，见 resolve_project_by_cwd）。
_WORKSPACE_SCHEMA = "\n" + _WORKSPACE_MAP_DDL + "\n"


# ------------------------------------------------------ 名字别名（阶段 D · 名字治理）
#
# 为什么单独一张表，而不是给 `code_registry` / `citizens` 各加一列：
#   · 别名要能同时表达**项目**与**公民**——两张表各加一列做不到统一口径；
#   · 单向存储（from → to）、查询时展开，**完全不碰 ADR-003 的编码**（编码永不可改）；
#   · 可逆：删一行即撤销，不动任何历史数据。
#
# 为什么要别名：生产库里 `cdql`(9 条) 与 `CDQL`(2 条) 是**同一个项目的两个入口**，
# 而 ADR-003 让"改 project"等价于"改编码"——被永久禁止。别名是唯一不动编码的解法。
#
# `kind` 目前只允许 'project' / 'citizen'（`_ALIAS_KINDS`）：这不是封闭枚举，是**校验白名单**
# ——加第三种名字类型时必须同时想清楚"它怎么解析"，而不是让任意字符串混进来。
_NAME_ALIASES_DDL = """CREATE TABLE IF NOT EXISTS name_aliases (
  kind       TEXT NOT NULL,       -- 'project' | 'citizen'
  from_name  TEXT NOT NULL,       -- 别名（非规范名）
  to_name    TEXT NOT NULL,       -- 规范名
  created_at TEXT NOT NULL,
  created_by TEXT,
  reason     TEXT,
  PRIMARY KEY (kind, from_name)
);"""

_NAME_SCHEMA = "\n" + _NAME_ALIASES_DDL + "\n"

# ------------------------------------------------------ 看板（P1-b · 2026-10-25）
# 裁决依据 `16` §4.1：**数据库做真相 + 文件做渲染**。
# 为什么不用纯文件（`12` 最初的方案）：
#   并发写靠纪律会丢行、读到半写的文件、单文件必然膨胀（`active-sessions` v1
#   就是这样死的：10 天 114 条 → 396KB → 超过读取工具上限）。
#   表和文件不是"两份真相"，是"真相 + 投影"，与 fish 已有的事件→投影同构。
#
# 看板回答的是「**此刻谁在动哪块**」——不是"发生了什么"（那是 daily）。
# 这条边界一旦破了，看板就会退化成第二份 daily（`09` §4.6 的第三个坑）。
_BOARDS_DDL = """CREATE TABLE IF NOT EXISTS boards (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  scope      TEXT NOT NULL,               -- root | project | branch
  scope_key  TEXT NOT NULL,               -- root='' / project='anyi-studio' / branch='<名字>'
  period_key TEXT NOT NULL,               -- YYYYMMDD（按日一块板）
  path       TEXT NOT NULL,               -- 渲染文件相对路径（派生，可重建）
  status     TEXT NOT NULL DEFAULT 'open',-- open | closed
  created_at TEXT NOT NULL,
  closed_at  TEXT,
  UNIQUE(scope, scope_key, period_key)
);"""

_PRESENCE_DDL = """CREATE TABLE IF NOT EXISTS presence (
  board_id      INTEGER NOT NULL REFERENCES boards(id),
  citizen_id    TEXT NOT NULL,
  agent_id      TEXT,                     -- 会话/宿主侧标识（可为空）
  status        TEXT NOT NULL DEFAULT 'active',  -- active | idle | left
  doing         TEXT,                     -- 一句话：在做什么（≤80 字，撞车判断靠它）
  surface       TEXT,                     -- 占用面（文件/目录前缀，逗号分隔）
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  PRIMARY KEY (board_id, citizen_id)
);"""

_BOARDS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_boards_scope ON boards(scope, scope_key, period_key)",
    "CREATE INDEX IF NOT EXISTS ix_boards_status ON boards(status, period_key)",
    "CREATE INDEX IF NOT EXISTS ix_presence_last ON presence(board_id, last_seen_at)",
)

# 看板动态（"今日动态"区）——它比"在场"轻，但同样受容量纪律约束。
# 为什么不复用 `events`：events 是**不可变的事实流**，而看板动态是**可滚动的展示层**；
# 把展示层塞进事实流会让"重建投影"多一类要处理的事件，得不偿失。
_BOARD_POSTS_DDL = """CREATE TABLE IF NOT EXISTS board_posts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  board_id   INTEGER NOT NULL REFERENCES boards(id),
  citizen_id TEXT NOT NULL,
  kind       TEXT NOT NULL,               -- checkin | progress | done | note
  text       TEXT NOT NULL,               -- ≤200 字（渲染时硬截断）
  created_at TEXT NOT NULL
);"""

_BOARD_POSTS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_posts_board ON board_posts(board_id, id DESC)",
)
# 看板"已看到"快照（EVAL-261025-20，2026-10-25 修）：记录**每个公民上次注入时**
# 看到的看板状态。推送候选拿它与当前状态比较——"你看完之后又有人动了"才推。
# 为什么单独建表而不是塞进 config：config 是全局键值，多公民会互相覆盖。
_BOARD_SEEN_DDL = """CREATE TABLE IF NOT EXISTS board_seen (
  board_id         INTEGER NOT NULL,
  citizen_id       TEXT NOT NULL,
  last_post_id     INTEGER NOT NULL DEFAULT 0,
  last_presence_at TEXT NOT NULL DEFAULT '',
  updated_at       TEXT NOT NULL,
  PRIMARY KEY (board_id, citizen_id)
);"""
_BOARD_TABLE_DDLS = (_BOARDS_DDL, _PRESENCE_DDL, _BOARD_POSTS_DDL, _BOARD_SEEN_DDL)
_BOARD_SCHEMA = "\n" + "\n\n".join(_BOARD_TABLE_DDLS) + "\n"


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  seq       INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id  TEXT NOT NULL UNIQUE,
  ts        TEXT NOT NULL,
  type      TEXT NOT NULL,
  actor     TEXT NOT NULL,
  subject   TEXT NOT NULL,
  payload   TEXT NOT NULL,
  cause     TEXT,
  refs      TEXT,
  schema    TEXT NOT NULL DEFAULT 'evt@1'
);

CREATE TRIGGER IF NOT EXISTS ev_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;

CREATE TRIGGER IF NOT EXISTS ev_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;

CREATE TABLE IF NOT EXISTS code_registry (
  prefix    TEXT PRIMARY KEY,
  namespace TEXT,
  last_date TEXT,
  last_seq  INTEGER DEFAULT 0,
  status    TEXT DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS code_index (
  code             TEXT PRIMARY KEY,
  title            TEXT,
  project          TEXT,
  owner            TEXT,
  mem_type         TEXT,
  visibility       TEXT,
  status           TEXT DEFAULT 'active',
  tags             TEXT,
  file_path        TEXT,
  occurred_at      TEXT,
  recorded_at      TEXT,
  last_accessed_at TEXT,
  access_count     INTEGER DEFAULT 0,
  dormant          INTEGER DEFAULT 0,
  session_actor    TEXT,
  attachments      TEXT,
  superseded_by    TEXT
);

CREATE TABLE IF NOT EXISTS citizens (
  citizen_id       TEXT PRIMARY KEY,
  display_name     TEXT,
  kind             TEXT,
  role             TEXT,
  host             TEXT,
  status           TEXT DEFAULT 'active',
  created_at       TEXT,
  is_owner         INTEGER DEFAULT 0,
  -- ↓ P1-a 追加的 5 列（旧库靠 migrate 的 ALTER 补齐，见 _CITIZENS_NEW_COLS）。
  --   这一段必须与 _CITIZENS_ARCHIVE_DDL 逐列一致：citizens_all 按**列位置**拼。
  account_id       TEXT,
  device_id        TEXT,
  host_session_key TEXT,
  last_seen_at     TEXT,
  archived_at      TEXT
);
-- 注意：owner 的唯一索引不在 SCHEMA 里创建。
-- 旧库的 citizens 表可能还没有 is_owner 列，此时建索引会报 no such column。
-- 索引统一在 migrate() 里「先 ALTER 加列、再建索引」，保证新旧库都能走通。

CREATE TABLE IF NOT EXISTS config (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  type       TEXT NOT NULL DEFAULT 'string',
  updated_at TEXT
);

-- 全文检索索引（ADR-005）。**派生表，不是真相源**：可由 code_index + 正文文件重建。
-- 为什么自己切 token 而不让 FTS 分：内置 unicode61 不做中文分词，「协作约定」是整 token，
-- 查「协作」永远 0 条（2026-10-22 实测）。详见 ADR-005 背景表。
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
  code UNINDEXED,
  title_idx,
  body_idx,
  tokenize='unicode61'
);
""" + _PUSH_SCHEMA + _IDENTITY_SCHEMA + _WORKSPACE_SCHEMA + _NAME_SCHEMA + _BOARD_SCHEMA


def connect(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    db = root / "index.sqlite"
    conn = sqlite3.connect(str(db), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# 两张推送地基表的「补列 / 建索引」清单。
# 补列用的声明都必须是**可加到非空表上**的（SQLite 的 ALTER ADD COLUMN 不允许
# 无默认值的 NOT NULL），所以这里刻意比 _*_DDL 里的声明宽松——新建表仍按严格版走。
_ACCESS_LOG_COLS = (
    ("code", "TEXT"),
    ("citizen_id", "TEXT"),
    ("session_key", "TEXT"),
    ("project", "TEXT"),
    ("via", "TEXT"),
    ("payload", "TEXT"),
    ("accessed_at", "TEXT"),
)

_LINKS_COLS = (
    ("src", "TEXT"),
    ("dst", "TEXT"),
    ("rel", "TEXT"),
    ("source", "TEXT"),
    ("weight", "REAL DEFAULT 1.0"),
    ("evidence", "TEXT"),
    ("created_at", "TEXT"),
    ("removed_at", "TEXT"),
    # 再巩固预留（14 §6）：旧库升级也要补上这 3 列。
    # 声明必须是「可加到非空表上」的形态（INTEGER 带 DEFAULT），否则 ALTER 会失败。
    ("last_reinforced_at", "TEXT"),
    ("reinforce_count", "INTEGER DEFAULT 0"),
    ("suppressed", "INTEGER DEFAULT 0"),
)

_ACCESS_LOG_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_mal_code ON memory_access_log(code, accessed_at)",
    "CREATE INDEX IF NOT EXISTS ix_mal_citizen ON memory_access_log(citizen_id, accessed_at)",
    "CREATE INDEX IF NOT EXISTS ix_mal_session ON memory_access_log(session_key, accessed_at)",
)

_LINKS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_links_src ON links(src) WHERE removed_at IS NULL",
    "CREATE INDEX IF NOT EXISTS ix_links_dst ON links(dst) WHERE removed_at IS NULL",
    "CREATE INDEX IF NOT EXISTS ix_links_rel ON links(rel) WHERE removed_at IS NULL",
    # 再巩固预留：候选池只捞 suppressed=0 且未删除的边，这条部分索引就是它的取数口。
    "CREATE INDEX IF NOT EXISTS ix_links_suppressed ON links(suppressed) "
    "WHERE removed_at IS NULL",
)

# push_log 的补列清单（旧库升级路径）。与上面两份同理：声明必须是**能加到非空表上**的形态
# （SQLite 的 ALTER ADD COLUMN 不允许无默认值的 NOT NULL），新建表仍按 _PUSH_LOG_DDL 的严格版走。
_PUSH_LOG_COLS = (
    ("session_key", "TEXT"),
    ("citizen_id", "TEXT"),
    ("project", "TEXT"),
    ("code", "TEXT"),
    ("candidate_id", "TEXT"),
    ("channel", "TEXT"),
    ("budget_tokens", "INTEGER"),
    ("pushed_at", "TEXT"),
    ("reaction", "TEXT"),
    ("reacted_at", "TEXT"),
    ("latency_sec", "INTEGER"),
)

_PUSH_LOG_INDEXES = (
    # 按会话回看"这个会话被推过什么"——时机策略的主查询就是它
    "CREATE INDEX IF NOT EXISTS ix_push_session ON push_log(session_key, pushed_at)",
    # 按记忆回看"这条被推过几次、反应如何"（采纳率按 code 聚合）
    "CREATE INDEX IF NOT EXISTS ix_push_code ON push_log(code, pushed_at)",
    # 待判行的取数口：read_body 的回填与 daily_report 的收口都只碰这些行
    "CREATE INDEX IF NOT EXISTS ix_push_reaction ON push_log(reaction) WHERE reaction IS NULL",
)


def _has_unique_index(conn, table, columns) -> bool:
    """表上是否已有覆盖这几列的唯一索引（**含 CREATE TABLE 里 UNIQUE 自动建的**）。

    为什么不用「表是不是本次新建的」来判断：真实路径都是先 executescript(SCHEMA)
    再 migrate()，所以到 migrate 时表**永远已存在**——那样判断会变成恒真，
    于是每张新库上都会多建一条与自动索引重复的唯一索引（白占空间与写入）。
    按语义问「去重约束在不在」才问得准：links 的 DDL 自带 UNIQUE(src,dst,rel)，
    自动索引已经满足去重，不必再加一条。
    """
    want = tuple(columns)
    for row in conn.execute(f"PRAGMA index_list({table})").fetchall():
        index_name, is_unique = row[1], row[2]
        if not is_unique:
            continue
        cols = tuple(r[2] for r in conn.execute(f"PRAGMA index_info({index_name})"))
        if cols == want:
            return True
    return False


def _ensure_push_tables(conn: sqlite3.Connection):
    """幂等：把推送地基层的表建好、补齐缺列、再建索引（新旧库都能反复跑）。

    顺序是硬纪律（就是 citizens.is_owner 那条教训）：
    **先建表 → 再补列 → 最后建索引**。反过来做，旧库里一张「同名但缺列」的表
    就会让 CREATE INDEX 报 no such column，整个升级路径崩在启动时。

    索引只放这里、不放 SCHEMA：SCHEMA 先于 migrate 执行，旧库的列还没补齐。
    """
    for table, ddl, cols, indexes in (
            ("memory_access_log", _ACCESS_LOG_DDL, _ACCESS_LOG_COLS, _ACCESS_LOG_INDEXES),
            ("links", _LINKS_DDL, _LINKS_COLS, _LINKS_INDEXES),
            ("push_log", _PUSH_LOG_DDL, _PUSH_LOG_COLS, _PUSH_LOG_INDEXES)):
        if not conn.execute(f"PRAGMA table_info({table})").fetchall():
            conn.execute(ddl)          # 旧库根本没这张表：按 SCHEMA 的严格版建
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        for stmt in indexes:
            conn.execute(stmt)
        if table == "links" and not _has_unique_index(conn, "links", ("src", "dst", "rel")):
            # 更早的实验版可能建了一张**没有** UNIQUE 的 links 表：补唯一索引把它锁住，
            # 否则 add_link 的 INSERT OR IGNORE 去重会静默失效（边越写越多）。
            # 旧表里若已有重复边，建唯一索引会失败——此时不阻断升级（去重留给后续 rebuild）。
            try:
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_links_edge ON links(src,dst,rel)")
            except sqlite3.DatabaseError:
                pass


def _ensure_board_tables(conn: sqlite3.Connection):
    """幂等：把看板三张表建好、建索引（新旧库都能反复跑）。

    顺序与 `_ensure_push_tables` 同一条纪律：**先建表 → 再补列 → 最后建索引**。
    看板是 2026-10-25 新加的，没有"旧版缺列"的历史包袱，但仍按同一形态写——
    下次加列时不用再想一遍顺序。
    """
    for ddl in _BOARD_TABLE_DDLS:
        # 表名从 DDL 里取，避免再维护一份名单（两份名单就是漂移的起点）
        m = re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", ddl)
        if not m:
            continue
        table = m.group(1)
        if not conn.execute(f"PRAGMA table_info({table})").fetchall():
            conn.execute(ddl)
    for stmt in _BOARDS_INDEXES + _BOARD_POSTS_INDEXES:
        conn.execute(stmt)


# ------------------------------------------------------ 身份层 P1-a：建表 / 补列 / 视图
def _ensure_citizens_all_view(conn: sqlite3.Connection):
    """幂等地建 citizens_all；**定义与列契约不符就重建**（自愈）。

    为什么需要"不符就重建"：`CREATE VIEW IF NOT EXISTS` 只在视图缺席时建，
    已存在的旧定义不会被更新；视图定义一旦与 _CITIZENS_ALL_COLUMNS 漂开，
    查询会静默少列/错位。所以这里拿 PRAGMA 读**视图真实暴露的列**与契约比对。

    为什么建完立刻 `SELECT * FROM citizens_all LIMIT 0`：SQLite 的视图是**惰性校验**的
    ——允许建一个引用了不存在列的视图，错误留到查询时才报（实测：CREATE 成功，
    PRAGMA table_info 才抛 no such column）。不在这里真查一次，坏视图会一路活到
    某个调用方手里。视图是派生对象，在这里重建/报错的代价都可控。

    若**归档表本身**列不对，上面这一查会直接抛错——刻意不自动重建 citizens_archive：
    那张表迟早装着被搬走的行，静默重建 = 静默删数据。宁可升级当场失败让人来看。
    """
    want = list(_CITIZENS_ALL_COLUMNS)
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='view' AND name='citizens_all'").fetchone()
    if exists:
        try:
            have = [r[1] for r in conn.execute("PRAGMA table_info(citizens_all)")]
        except sqlite3.DatabaseError:
            have = []          # 旧定义引用了还不存在的列（半升级态）→ 重建
        if have == want:
            return
        conn.execute("DROP VIEW citizens_all")
    conn.execute(_citizens_all_view_ddl())
    # 把"惰性校验"提前成"立刻校验"：列不齐 / 两表列数不同 → 就在这里响。
    # 这里用 `*` 是刻意的：要验的就是"13 列全拿得到"，不该挑列（视图定义里才禁止 `*`）。
    conn.execute("SELECT * FROM citizens_all LIMIT 0")


def _ensure_stint_open_index(conn: sqlite3.Connection):
    """幂等：把开档期唯一索引统一成**两列版**（citizen_id, project）。

    这是本轮唯一需要"先删再生"的对象，根因是**名字复用、定义变了**：
    旧库里 `ux_stint_open` 是 `ON project_stints(citizen_id) WHERE to_ts IS NULL`，
    而 `CREATE UNIQUE INDEX IF NOT EXISTS` 见同名索引就跳过——不先 DROP，旧的一列
    索引会带着旧语义原地活下来，多项目并行被它拦住（判据 G 就是拿这个场景验的）。

    **顺序硬纪律：先判 → 再 DROP → 最后 CREATE。** 反过来（先 CREATE）什么都不会发生，
    因为 IF NOT EXISTS 让那句成为空操作，随后"DROP 掉再建"才是唯一能改定义的路径。

    为什么 DROP 之后再建一定不会失败（这是本轮唯一被质疑过的一步）：
      · 旧索引（一列）比新索引（两列）**更严**——旧库最多允许同一公民 1 条开档期，
        这种数据在新索引下必然合法（更宽的约束不会拒绝原本更少的数据）；
      · 反过来"旧库里同一公民有多个开档期"在旧索引下**根本插不进去**（那正是旧不变
        量），所以不存在"DROP 之后重建失败"的正常路径；就算有人手工删过旧索引造出
        这种数据，新索引也容得下（多项目并存合法），不会因为"一人多档"而失败；
      · 唯一会让 CREATE 失败的是**同一 citizen + 同一 project 两条开档期**，那在旧索引
        下更不可能存在。→ 结论：DROP → CREATE 是安全的单向升级。

    出现真重复时**不吞异常**：那是数据病，要让人看见。静默跳过索引的后果是
    "以为有唯一约束、其实没有"，比升级当场失败危险得多。

    判据式实现（不拿正则去扒 sqlite_master.sql）：`PRAGMA index_info` 是库自己报的
    列名与列序，与 DDL 文本怎么换行、怎么加引号都无关。
    """
    have = tuple(r[2] for r in conn.execute(
        f"PRAGMA index_info({_STINT_OPEN_INDEX_NAME})"))
    if have == _STINT_OPEN_INDEX_COLS:
        return                              # 已经是两列版：一个字节都不动
    if have:
        # 存在但列不对（旧库一列版 / 更早的实验版）→ 必须先删掉旧定义
        conn.execute(f"DROP INDEX IF EXISTS {_STINT_OPEN_INDEX_NAME}")
    conn.execute(_STINT_OPEN_INDEX_DDL)


def _ensure_identity_tables(conn: sqlite3.Connection):
    """幂等：身份层结构一次到位（新表 → citizens 补列 → 视图 → 索引）。

    顺序仍是那条硬纪律（citizens.is_owner 事故的教训）：**先建表 → 再补列 →
    最后建索引/视图**。视图与 ix_citizens_account 都依赖 citizens 的最终列集，
    提前一步就会撞 `no such column`，整个升级路径崩在启动时。

    刻意**不**为 citizens 本身写"表不存在就建"的分支：migrate 前面几步（is_owner 补列、
    owner 回填）已经在用它，走到这里时它必然存在。建表归 SCHEMA，补列归这里。

    本轮只做结构：不写任何行、不改任何既有查询、不归档、不签发 token。
    """
    for ddl in _IDENTITY_TABLE_DDLS:
        conn.execute(ddl)                      # IF NOT EXISTS：已有的表原样不动

    ccols = {r[1] for r in conn.execute("PRAGMA table_info(citizens)")}
    for name, decl in _CITIZENS_NEW_COLS:
        if name not in ccols:
            conn.execute(f"ALTER TABLE citizens ADD COLUMN {name} {decl}")

    _ensure_citizens_all_view(conn)
    # 开档期唯一索引单独处理：它的**定义换过**（一列 → 两列），必须按 PRAGMA 判断后
    # DROP 重建，不能靠清单里的幂等 CREATE（同名旧索引会让那条成为空操作）。
    _ensure_stint_open_index(conn)
    for stmt in _IDENTITY_INDEXES:
        conn.execute(stmt)


def _ensure_workspace_table(conn: sqlite3.Connection):
    """幂等：建 workspace_map（旧库升级路径）。

    为什么必须走 migrate（而不是只在 SCHEMA 里建）：`CREATE TABLE IF NOT EXISTS` 对
    **已存在**的旧库什么都不做，所以"升级"这件事只能由 migrate 负责——它是每条
    打开路径（CLI 各命令 / MCP server / 测试装配）都会跑的那一步。SCHEMA 里也放一份
    是给全新库用的，两份共用 `_WORKSPACE_MAP_DDL` 这一个常量（第三份拷贝就是漂移起点）。

    这里刻意不做"把旧行里的尾斜杠/大写批量修一遍"的迁移：这张表是本轮新建的，
    **不存在旧行**；将来若真需要改已存数据，那是数据迁移，该单独一件事单独判据。
    """
    conn.execute(_WORKSPACE_MAP_DDL)


def _ensure_name_tables(conn):
    """幂等：建别名表（名字治理的第一层）。

    与 `_ensure_workspace_table` 同一纪律：表结构在两个消费者（SCHEMA 新库 /
    migrate 旧库）各建一次，共用同一份 DDL。索引刻意不加——别名是个位数级别，
    解析在 Python 里做（`_alias_map`），加索引只是给"看起来很专业"付利息。
    """
    conn.executescript(_NAME_ALIASES_DDL)
    conn.commit()


def migrate(conn: sqlite3.Connection):
    """旧库升级：缺列则补，不动已有数据。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(code_index)")}
    additions = [
        ("last_accessed_at", "TEXT"),
        ("access_count", "INTEGER DEFAULT 0"),
        ("dormant", "INTEGER DEFAULT 0"),
        # 会话级追溯：同一宿主身份下区分是哪个会话写的
        ("session_actor", "TEXT"),
        # 附件索引（JSON 数组）：正文里也写引用，这里是可查询的副本
        ("attachments", "TEXT"),
        # 替代关系：被哪条新记忆取代（status='superseded' 时有效）
        ("superseded_by", "TEXT"),
    ]
    for name, decl in additions:
        if name not in cols:
            conn.execute(f"ALTER TABLE code_index ADD COLUMN {name} {decl}")

    # citizens.is_owner（v0.3.1）：owner 从 config 迁回实体属性
    ccols = {r[1] for r in conn.execute("PRAGMA table_info(citizens)")}
    if "is_owner" not in ccols:
        conn.execute("ALTER TABLE citizens ADD COLUMN is_owner INTEGER DEFAULT 0")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_citizens_owner "
        "ON citizens(is_owner) WHERE is_owner = 1")
    # 回填：若 config 里已指定 owner 且该公民存在，则标记为 owner
    row = conn.execute("SELECT value FROM config WHERE key IN ('owner_citizen','default_citizen') "
                       "AND value<>'' ORDER BY key='owner_citizen' DESC LIMIT 1").fetchone()
    if row:
        has = conn.execute("SELECT 1 FROM citizens WHERE is_owner=1").fetchone()
        if not has:
            conn.execute("UPDATE citizens SET is_owner=1 WHERE citizen_id=?", (row[0],))

    # 推送系统 P0（13 §2）：两张地基表。顺序纪律见 _ensure_push_tables 的说明。
    _ensure_push_tables(conn)

    # 身份层 P1-a（纯结构）：citizens 补 5 列 + 4 张新表 + citizens_all 视图 + 索引。
    # 顺序纪律见 _ensure_identity_tables 的说明。**不动任何既有查询。**
    _ensure_identity_tables(conn)

    # 工作区 → 项目映射（R1 根治）：项目归属的权威来源，替掉"最近更新者即当前项目"的猜测。
    # 与身份层无依赖，放在后面是为了让"结构升级"的既有顺序一个字节都不动。
    _ensure_workspace_table(conn)
    # 名字治理（2026-10-25 · 阶段 D）：别名表。放最后，同样是为了让"既有结构升级
    # 顺序一个字节都不动"——新增结构永远加在链尾，不改前人的顺序。
    _ensure_name_tables(conn)
    # 看板（P1-b · 2026-10-25）：三张表 + 索引。同样加在链尾。
    _ensure_board_tables(conn)
    conn.commit()


def append_event(conn, etype, actor, subject, payload, cause=None) -> int:
    cur = conn.execute(
        "INSERT INTO events (event_id, ts, type, actor, subject, payload, cause, schema) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (ulid(), now_utc(), etype, json.dumps(actor, ensure_ascii=False),
         json.dumps(subject, ensure_ascii=False), json.dumps(payload, ensure_ascii=False),
         cause, "evt@1"),
    )
    conn.commit()
    return cur.lastrowid


# ------------------------------------------------------------------ 配置

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


def set_config(conn, key, value):
    default, typ, allowed = _key_type(key)
    if typ == "int":
        value = int(value)
    elif typ == "float":
        value = float(value)
    elif typ == "bool":
        value = bool(value) if not isinstance(value, str) else value.lower() in ("1", "true", "yes")
    if allowed and str(value) not in allowed:
        raise ValueError(f"{key} 只能是 {allowed}，收到 {value}")
    old = get_config(conn, key)
    conn.execute(
        "INSERT INTO config (key,value,type,updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, str(value).lower() if isinstance(value, bool) else str(value), typ, now_utc()),
    )
    conn.commit()
    append_event(conn, "config.changed",
                 {"citizen_id": "system", "evidence_level": "signed"},
                 {"kind": "config", "id": key},
                 {"key": key, "old_value": old, "new_value": value})
    return value


# ------------------------------------------------------------------ 编码

# ---------------------------------------------------------------- 正文文件名（2026-10-25）
# 正文文件名从「纯编码」改成「编码 + 标题缩写」：
#
#     projects/anyi-studio/anyi-studio-261022-05__协作铁律·行为层（CR-2-5-6-7-11_+.md
#
# 为什么（用户 2026-10-25 需求）：
#   AI 不看目录（实测 recall 78.6% / read_body 5.6%），但**人**会用文件管理器、
#   Everything 之类的工具按文件名搜。纯编码文件名对这个场景等于零信息。
#
# 三条纪律：
#   ① **纯函数**：同样的 (project, code, title) 必须永远算出同一个路径——
#      否则 `rebuild` 会算出和 `record_memory` 不同的文件名，重建即断链。
#   ② **标题是记录时的快照**：`memory.revised` 改标题后**文件名不变**
#      （用户 2026-10-25 拍板）。跟着变会让所有外部引用（人写的文档、别的 AI 的笔记）失效。
#   ③ **标题截 24 字符**（用户拍板）。实测标题平均 38.7 / 最长 81，
#      不截会让文件名到 100+ 字符，反而难用。
_SLUG_MAX = 24
# Windows 文件名非法字符 → 安全替换。**只换非法字符**，全角标点（（）·：）是中文字符，保留。
_SLUG_MAP = str.maketrans({
    "/": "-", "\\": "-", ":": "-", "*": "-", "?": "-",
    '"': "", "|": "-", "<": "(", ">": ")",
    "\n": " ", "\r": " ", "\t": " ",
})


def slug_title(title, limit=_SLUG_MAX):
    """标题 → 文件名安全片段（纯函数、稳定、可复算）。

    实测 76 条标题含 Windows 非法字符（`/` 出现 163 次最多），不处理就建不出文件。

    EVAL-261025-18（2026-10-25 修）：**截断必须显式标记**。旧实现 `t[:limit]`
    静默截断，文件名看起来像"数据损坏"（`title` 字段其实完好）。加 `...`
    让"这里被截了"一眼可见；总长仍 ≤ limit（省略号算在预算内）。
    """
    t = (title or "").strip()
    t = re.sub(r"\s+", "_", t)          # 空白 → 下划线（保留可读性，去掉歧义）
    t = t.translate(_SLUG_MAP)          # 非法字符替换
    t = t.strip("._-")                  # 首尾不能是点/下划线/连字符（Windows 规则）
    if not t:
        return "untitled"
    if len(t) > limit:
        if limit < 4:
            # QA 2026-10-25 边界：limit 太短放不下 `...`。退回纯截断，
            # 保住 docstring 的"总长 ≤ limit"承诺（默认 24 不受影响）。
            return t[:limit]
        head = t[:max(1, limit - 3)].rstrip("._-")
        return (head or "untitled") + "..."
    return t


def archive_relpath(file_path, year) -> str:
    """把 `projects/<项目>/<文件名>` → `projects/<项目>/archive/<年>/<文件名>`。

    ## 为什么**只换目录、不改文件名**

    文件名里的 slug 是**记录那一刻**由 title 算出来的。之后 `memory.revised` 可能改过
    `code_index.title`，此时重算 slug 会得到不同的名字——**那正是当年 rebuild 断链
    73 条的根因**（`rebuild_projection` 用事件里的旧 title 重算路径）。
    归档只该搬位置，不该重命名：移动是可逆的，改名不是。

    ## 为什么按年分目录

    用户的原话是「在很久很久以后」。平铺的问题是**给人看的**——AI 查询全走
    `code_index` 索引与 `memory_fts`，全仓零目录遍历（`os.walk`/`listdir`/`glob`
    命中数 = 0），所以文件放在哪一层对查询速度毫无影响。
    按年分是为了让 `archive/2026/` 这类目录的规模可控，而不是为了 AI。
    """
    p = Path(str(file_path).replace("\\", "/"))
    parts = p.parts
    if len(parts) == 3 and parts[0] == "projects":
        return f"projects/{parts[1]}/archive/{year}/{parts[2]}"
    raise ValueError(f"只支持 projects/<项目>/<文件> 形式，收到：{file_path!r}")


def archive_body(conn, root, code, *, year=None, reason=None, dry_run=False) -> dict:
    """把一条记忆的正文移进 `archive/<年>/`，并同步 `code_index.file_path`。

    ## 它不改什么（这是"不影响机制"的全部原因）

    - **不改 `status`**：归档是**位置**，不是**语义**。`superseded` 早已是语义，
      归档只是把文件挪走
    - **不改正文内容**：一个字节都不动
    - **不改事件**：只追加一条 `memory.archived`
    - **不删任何东西**

    ## 为什么所有工具都不用改

    全仓零目录遍历——`read_body` / `recall` / `neighbors` 全部走
    `code_index.file_path` 这个字段。**字段指到哪，就读到哪。**
    而 `rebuild_projection` 已有「快照优先」逻辑（2026-10-25 修）：
    只要 `file_path` 指向的文件存在，重建就保留它，不会算回平铺位置。
    """
    row = conn.execute(
        "SELECT code, project, title, file_path, status FROM code_index WHERE code=?",
        (code,)).fetchone()
    if not row:
        raise ValueError(f"编码不存在：{code}")
    code, project, title, old_fp, status = row
    if not old_fp:
        raise ValueError(f"{code} 没有 file_path，无法归档")
    old_abs = Path(root) / old_fp
    if not old_abs.exists():
        raise ValueError(f"{code} 的正文文件不存在：{old_fp}（先跑 rebuild 修复投影）")

    # **幂等**：已经在 archive/ 下的直接返回"已归档"，不报错。
    # 2026-10-25 副本实测抓到：先单条归档、再跑全量 sweep 时，这条会撞上
    # `archive_relpath` 的格式检查并报"只支持 projects/<项目>/<文件> 形式"——
    # 那是**把幂等当成错误**。归档本身就该可重复调用。
    if "/archive/" in old_fp.replace("\\", "/"):
        return {"code": code, "from": old_fp, "to": old_fp,
                "moved": False, "already": True}

    # 年份**从编码里取**：`<前缀>-<YYMMDD>-<NN>` → `20YY`。
    # 为什么不是从 file_path 取：路径里只有 `<编码>__<slug>.md`，前 4 位是编码前缀
    # （比如 `anyi`），不是年份——2026-10-25 dry-run 当场抓到这个 bug（算成了 `2610`）。
    if year:
        y = str(year)
    else:
        m = re.search(r"-(\d{2})\d{4}-\d+$", code)
        y = ("20" + m.group(1)) if m else local_date()[:4]
    new_fp = archive_relpath(old_fp, y)
    new_abs = Path(root) / new_fp
    if dry_run:
        return {"code": code, "from": old_fp, "to": new_fp, "moved": False}

    new_abs.parent.mkdir(parents=True, exist_ok=True)
    # 用 `os.replace` 之外的保守做法：先确认目标不存在，再 move
    # （Windows 上 os.rename 会静默覆盖同名文件——`exact_exists` 的教训）
    if new_abs.exists():
        raise ValueError(f"目标已存在，拒绝覆盖：{new_fp}")
    shutil.move(str(old_abs), str(new_abs))
    conn.execute("UPDATE code_index SET file_path=? WHERE code=?", (new_fp, code))
    append_event(conn, "memory.archived",
                 {"citizen_id": "system", "evidence_level": "signed"},
                 {"kind": "memory", "id": code},
                 {"code": code, "from": old_fp, "to": new_fp,
                  "reason": (reason or "").strip() or "superseded",
                  "status": status})
    return {"code": code, "from": old_fp, "to": new_fp, "moved": True}


def archive_sweep(conn, root, *, include_withdrawn=True, year=None,
                  dry_run=False, limit=None) -> dict:
    """批量归档：**只动 `superseded`（和可选 `withdrawn`）**，`active` 一律不碰。

    ## 为什么只动这两种状态（这是"不影响推送"的关键）

    全仓所有取数口都硬过滤 `status='active'`（`fish.py:1768 / 2542` 等多处）。
    所以只要不动 `active`，**推送、召回、注入包、邻近查询的行为一个字节都不会变**——
    它们本来就看不见 `superseded`/`withdrawn` 的那些。

    用户的原话是「不影响我们目前的记忆、推送等机制」——这条红线就是那样守住的：
    归档的**准入判据直接复用系统已有的 `status`**，不新造任何"归档状态"。
    """
    want = ["superseded"] + (["withdrawn"] if include_withdrawn else [])
    rows = conn.execute(
        "SELECT code FROM code_index WHERE status IN (%s) AND file_path IS NOT NULL "
        "ORDER BY recorded_at" % ",".join("?" * len(want)), want).fetchall()
    done, skipped, errors = [], [], []
    already = 0
    for (code,) in (rows[:limit] if limit else rows):
        try:
            r = archive_body(conn, root, code, year=year, dry_run=dry_run)
            # `already` 与 `moved` 分开计数：重复 sweep 时报"移动 38 条"是假读数
            # （文件一个都没动）——2026-10-25 幂等验证当场抓到。
            if r.get("already"):
                already += 1
            else:
                done.append(r)
        except ValueError as e:
            # 文件缺失 / 目标已存在 → 跳过并**如实记下**，不静默吞
            skipped.append({"code": code, "why": str(e)})
        except OSError as e:
            errors.append({"code": code, "why": f"{type(e).__name__}: {e}"})
    conn.commit()
    return {"total": len(rows), "moved": len(done), "already": already,
            "skipped": skipped, "errors": errors, "items": done}


def locate_body(root, code, file_path):
    """定位一条记忆的正文文件——**先按记录路径，再按编码兜底搜索**。

    ## 为什么需要它（2026-10-25 · 用户明确要求）

    用户的原话：

    > 人类会非常自由的修改项目文件夹内甚至包括项目文件夹本身，包括但不限于
    > 随意删除任意文件、调整里面的任意内容、修改项目文件名、修改项目文件夹名、
    > 改变文件顺序、改变文件位置……我们不接受系统因此而崩溃或无法运行。

    ## 三档返回

      `("exact", Path)`      —— 按 `code_index.file_path` 找到了（正常情况）
      `("relocated", Path)`  —— 原文没了，但**按编码在 projects/ 下找到了**
                               （人类改了文件名 / 改了项目文件夹名 / 挪了位置）
      `("missing", None)`    —— **真的找不到**（被删了，或挪到 projects/ 之外）

    ## 为什么按编码搜是有效的

    文件名格式是 `<编码>__<标题slug>.md`，**编码在文件名里**。
    所以只要人类没有连文件名里的编码一起改掉，改名/改目录/挪位置都能救回来。
    这也是本系统编码设计的意外红利：**编码是内容的稳定锚**。

    ## 为什么只搜 `projects/`

    人类的自由度是"项目文件夹内**甚至包括项目文件夹本身**"，不是在整块盘上漫游。
    限定 `projects/` 做**有界搜索**，避免在几万文件的工作区里 rglob 卡住。
    如果连 `projects/` 都整个挪走了——那已经不是"自由修改"，是搬家，
    此时返回 `missing` 并给出明确提示（**不崩溃**，见 `read_body_path`）。
    """
    p = (Path(root) / str(file_path)) if file_path else None
    if p is not None and p.exists() and p.is_file():
        return "exact", p

    base = Path(root) / "projects"
    if not base.is_dir():
        return "missing", None
    try:
        # ① `<编码>__<slug>.md`（标准形态）
        hits = sorted(base.rglob(f"{code}__*.md"))
        if not hits:
            # ② 人类可能把 `__标题` 那截删了，只剩 `<编码>.md`
            hits = sorted(base.rglob(f"{code}.md"))
        if not hits:
            # ③ 最宽松：文件名里含这个编码（人类加了前后缀，如 `-副本`、`（备份）`）
            hits = sorted(base.rglob(f"*{code}*.md"))
    except OSError:
        return "missing", None
    if hits:
        return "relocated", hits[0]
    return "missing", None


def read_body_path(conn, root, code):
    """读正文的**唯一入口**（CLI 与 MCP 共用）——永远返回 `(text, info)`，**不抛异常**。

    `info` 是给人看的状态：`{"state": "exact"|"relocated"|"missing",
    "path": 实际读到的路径或 None, "healed": 是否顺手修回了 file_path}`。

    ## 核心纪律：**文件不在 ≠ 系统崩**

    用户要求"不接受系统因此崩溃或无法运行"。所以这里的原则是：

      · `exact`     → 正常返回正文
      · `relocated` → **顺手自愈**：把 `code_index.file_path` 改成新位置，
                     然后返回正文 + 一句"文件被移动过，已自动修回"。
                     **人类改了名，系统自己找回来**——这才是"不崩溃"的正确含义
      · `missing`   → 返回一段**说明文字**（不是异常），讲清三种可能：
                     被删了 / 挪出 projects 了 / 文件名里的编码也被改了，
                     并给出修复命令

    为什么把"缺文件"做成**返回值而不是异常**：
    异常会让调用方（MCP 工具、CLI、批量脚本）整条链断掉。而"某一条正文不见了"
    是**局部数据问题**，不该升级成"整个系统不可用"。
    """
    row = conn.execute(
        "SELECT code, file_path, project, title, status FROM code_index WHERE code=?",
        (code,)).fetchone()
    if not row:
        raise ValueError(f"编码不存在：{code}")
    code, file_path, project, title, status = row
    state, p = locate_body(root, code, file_path)

    if state == "missing":
        return "", {"state": "missing", "path": None, "healed": False,
                    "hint": (
                        f"正文缺失：{code}\n"
                        f"　记录位置：{file_path or '（无）'}\n"
                        "　三种可能：① 文件被删了 ② 被挪到 projects/ 之外 ③ "
                        "文件名里的编码也被改掉了\n"
                        "　仓库里还有：标题 / 类型 / 标签 / 事件（`fish events --code "
                        f"{code}`），**只有正文内容不在库内**\n"
                        "　如果只是挪了位置又挪回来，本命令会自动认出来")}

    healed = False
    rel = str(Path(p).relative_to(Path(root))).replace("\\", "/")
    if state == "relocated" and rel != (file_path or ""):
        # **自愈**：人类改了名/挪了位置，系统自己修回记录
        conn.execute("UPDATE code_index SET file_path=? WHERE code=?", (rel, code))
        append_event(conn, "memory.relocated",
                     {"citizen_id": "system", "evidence_level": "signed"},
                     {"kind": "memory", "id": code},
                     {"code": code, "from": file_path, "to": rel})
        healed = True

    try:
        text = p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        # 文件在但读不出来（编码被改坏 / 权限）—— 同样不崩
        return "", {"state": state, "path": rel, "healed": healed,
                    "hint": (f"正文存在但读不出来：{rel}\n"
                             f"　原因：{type(e).__name__}: {e}\n"
                             "　人类可能改过编码或权限——文件是权威，系统不覆盖它")}
    return text, {"state": state, "path": rel, "healed": healed}


def body_relpath(project, code, title):
    """正文的相对路径：`projects/<项目>/<编码>__<标题缩写>.md`。

    **唯一入口**：`record_memory` 与 `rebuild_projection` 都必须调它，
    否则两处规则漂移 = 重建即断链（这类"两份实现"的亏本项目吃过多次）。
    """
    return f"projects/{project}/{code}__{slug_title(title)}.md"


def next_code(conn: sqlite3.Connection, prefix: str) -> str:
    """P0 已验证：BEGIN IMMEDIATE + 撞号重试，50 并发零重号。"""
    today = local_date()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT last_date, last_seq, status FROM code_registry WHERE prefix=?", (prefix,)
        ).fetchone()
        if not row:
            raise ValueError(f"未知项目前缀：{prefix}（先运行 project-add）")
        last_date, last_seq, status = row
        if status != "active":
            raise ValueError(f"项目 {prefix} 状态为 {status}，不能分配新编码")
        seq = 1 if last_date != today else int(last_seq or 0) + 1
        while True:
            code = f"{prefix}-{today}-{seq:02d}"
            if not conn.execute("SELECT 1 FROM code_index WHERE code=?", (code,)).fetchone():
                break
            seq += 1
        conn.execute("UPDATE code_registry SET last_date=?, last_seq=? WHERE prefix=?",
                     (today, seq, prefix))
        conn.commit()
        return code
    except Exception:
        conn.rollback()
        raise


# ------------------------------------------------------------------ 遗忘

def store_blob(root: Path, src: Path) -> dict:
    """把文件按内容寻址存进 blobs/，重复内容只存一份。返回附件元数据。"""
    data = src.read_bytes()
    h = hashlib.sha256(data).hexdigest()
    ext = src.suffix.lower()
    bdir = root / "blobs" / h[:2]
    bdir.mkdir(parents=True, exist_ok=True)
    bpath = bdir / f"{h}{ext}"
    if not bpath.exists():          # 去重：同一内容只写一次
        bpath.write_bytes(data)
    return {"name": src.name, "blob": f"blobs/{h[:2]}/{h}{ext}",
            "size": len(data), "sha256": h, "mime": ext.lstrip(".")}


def record_memory(conn, root: Path, *, project: str, title: str, body: str = "",
                  mem_type: str = "fact", tags=None, visibility: str = "shared",
                  owner=None, session_actor=None, attachments=None,
                  occurred_at=None, supersedes=None, cause=None, hints=None) -> tuple:
    """**唯一的记忆写入入口** —— CLI 与 MCP 都必须走这里。

    为什么要抽出来：2026-10-22 发现 cmd_remember 与 MCP tool_remember 是两份
    几乎相同但独立的实现，导致 inject 的身份 bug 在两条路径上不一致。
    两条路径必须共用一份写入逻辑。

    attachments: [路径字符串] 或 [{"path":..., "caption":...}]
    返回 (code, rel_path)
    """
    # 先校验时间参数：它若非法，必须在分配编码、落盘之前就失败，
    # 否则会留下一个空号 + 一个孤儿正文文件（编码一经分配不可回收）。
    occurred_at = normalize_occurred_at(occurred_at)
    # 项目可写性（2026-10-25 · 阶段 D）：closed / retired 拒绝写入。
    # 为什么拦在**这里**而不是 MCP 层：`record_memory` 是唯一写入口（CLI 与 MCP 共用），
    # 拦在别处必然有一条路径漏掉——那正是"两条路径必须共用一份写入逻辑"的原始教训。
    # 必须在 `next_code` **之前**：先拒绝，才不会留下空号（编码一经分配不可回收）。
    _ok, _why = project_writable(conn, project)
    if not _ok:
        raise ValueError(_why)
    code = next_code(conn, project)
    # 正文路径统一走 `body_relpath`（编码 + 标题缩写）。
    # 为什么不在这里直接拼：`rebuild_projection` 也要算同一个路径，两处各拼一次
    # 必然会漂移——这类"两份实现"的亏本项目吃过多次（D-10 的旧重建就是手抄一份逻辑）。
    rel = body_relpath(project, code, title)
    d = root / "projects" / project
    d.mkdir(parents=True, exist_ok=True)

    # ---- 附件：内容寻址 + 去重 ----
    atts = []
    for item in attachments or []:
        if isinstance(item, dict):
            src, cap = Path(item["path"]), item.get("caption", "")
        else:
            src, cap = Path(item), ""
        if not src.exists():
            raise FileNotFoundError(f"附件不存在：{src}")
        meta = store_blob(root, src)
        meta["caption"] = cap
        atts.append(meta)

    # ---- 正文 ----
    lines = [f"# {title}", "",
             f"> 编码：{code} ｜ 类型：{mem_type} ｜ 记录于 {now_utc()}", ""]
    if body:
        lines += [body, ""]
    if atts:
        lines.append("## 附件")
        for at in atts:
            cap = f" — {at['caption']}" if at["caption"] else ""
            lines.append(f"- [{at['name']}]({at['blob']}) "
                         f"({at['size']} B, sha256:{at['sha256'][:12]}…){cap}")
        lines.append("")
    (root / rel).write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---- 索引 + 事件 ----
    # 时间事实只取一次：occurred_at（这件事什么时候发生）进事件 payload，
    # recorded_at（我们什么时候知道）等于事件时间。两份事实各只有一处来源。
    # 2026-10-22 修 07 D-11：旧版 payload 不含 occurred_at，导致"忠实重建"在事件层面不可能。
    # 时间事实：occurred_at 已在函数开头校验归一；recorded_at 就是"现在"。
    recorded_at = now_utc()
    conn.execute(
        "INSERT INTO code_index (code,title,project,owner,mem_type,visibility,status,tags,"
        "file_path,occurred_at,recorded_at,access_count,dormant,session_actor,attachments) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,?,?)",
        (code, title, project, owner or "unknown", mem_type, visibility, "active",
         json.dumps(tags or [], ensure_ascii=False), rel,
         occurred_at, recorded_at, session_actor,
         json.dumps(atts, ensure_ascii=False) if atts else None))
    conn.commit()
    append_event(conn, "memory.recorded",
                 {"citizen_id": owner or "unknown", "evidence_level": "machine"},
                 {"kind": "memory", "id": code},
                 {"code": code, "title": title, "body_ref": rel,
                  "mem_type": mem_type, "tags": tags or [], "visibility": visibility,
                  "occurred_at": occurred_at,
                  "session_actor": session_actor,
                  # 存完整元数据（不只 name+sha256）：投影要能逐字段重建回来
                  "attachments": atts},
                 cause=cause)
    # 全文索引（ADR-005）：正文/标题入 FTS，让单号与中文子串可查
    _index_one_fts(conn, code, title, tags or [], body)

    # ---- 替代旧条目（写新的同时标记旧的）----
    if supersedes:
        olds = supersedes if isinstance(supersedes, (list, tuple)) else [supersedes]
        for old in olds:
            try:
                supersede_memory(conn, old, code, reason="被新记录替代", actor=owner)
            except ValueError:
                pass  # 旧条目不存在就不阻断写入，但会在返回值里体现

    # ---- 写入时建关联边（13 推送系统设计 §2.3 路 1）----
    # 兜底两层：build_links_on_write 内部逐条兜，这里再兜一次整体。
    # 建边是派生数据，绝不能让 remember 失败——写入才是主流程。
    try:
        build_links_on_write(conn, code, title=title, body=body or "", cause=cause)
    except Exception:
        pass

    # ---- 写前查重（2026-10-25 · 阶段 C）----
    # 名字叫"写前"，位置在写入**之后**，两者不矛盾：对照的是**下一次**写。
    # 这一次已经落盘（不阻塞是硬约束：每次写入都弹确认，AI 会绕过系统直接写文件），
    # 但把相似项交给调用方印进回执，AI 下一次写之前就会看见。
    # `hints=None`（不传）= 完全零开销，旧调用一个字都不变；传 `[]` 才计算。
    if hints is not None:
        try:
            hints.extend(find_similar_memories(
                conn, title=title, body=body or "", exclude={code}))
        except Exception:
            pass  # 查重是提示：绝不能让"提示失败"变成"写入失败"
    return code, rel


def supersede_memory(conn, old_code: str, new_code: str, reason: str = "",
                     actor=None) -> str:
    """把 old_code 标记为被 new_code 替代。

    为什么需要（2026-10-22 反馈 ANYI-261022-17）：同主题两条 rule 并存时，
    AI 无法判断哪条是当前的。superseded 状态让"当前的"唯一。
    """
    o = conn.execute("SELECT status,title,superseded_by FROM code_index WHERE code=?",
                     (old_code,)).fetchone()
    b = conn.execute("SELECT status,title FROM code_index WHERE code=?", (new_code,)).fetchone()
    if not o:
        raise ValueError(f"旧编码不存在：{old_code}")
    if not b:
        raise ValueError(f"新编码不存在：{new_code}")
    if old_code == new_code:
        raise ValueError("不能自己替代自己")
    if o[0] == "superseded":
        # 回显必须报库内实际替代者，不能回显入参（2026-10-22 反馈 ANYI-261022-34）：
        # 后到的会话看到「被 <我的入参> 替代过」会误以为自己的替代生效了，得到假成功。
        actual_by = o[2] or "（未记录）"
        if actual_by == new_code:
            return f"{old_code} 已是 superseded，实际替代者就是 {new_code}（本次未改动）"
        return (f"{old_code} 已经是 superseded，本次调用未生效：\n"
                f"实际替代者是 {actual_by}，不是传入的 {new_code}。\n"
                f"替代者锁定为首次替代者，不允许改写。\n"
                f"下一步：当前结论请看 {actual_by}；若要以 {new_code} 为权威版本，"
                f"请对 {actual_by} 再调一次 supersede（形成 {old_code}→{actual_by}→{new_code} 的链）。")

    conn.execute("UPDATE code_index SET status='superseded', superseded_by=? WHERE code=?",
                 (new_code, old_code))
    conn.commit()
    append_event(conn, "memory.superseded",
                 {"citizen_id": actor or "unknown", "evidence_level": "machine"},
                 {"kind": "memory", "id": old_code},
                 {"old": old_code, "new": new_code, "reason": reason})
    # 双写 links（13 §2.2「supersedes：已有 superseded_by 列 → 双写」）。
    # 方向与 code_index.superseded_by 同向：**被替代者 → 替代者**，同一事实两种存储。
    # 单独兜异常：状态已经写成功了，建边失败不能让人以为"替代没生效"。
    try:
        add_link(conn, old_code, new_code, "supersedes", "explicit", 1.0,
                 reason or "被新记录替代")
    except Exception:
        pass
    return f"{old_code} 已标记为被 {new_code} 替代"


def record_access(conn, codes, source, *, counted=True, actor=None):
    """记录一次「被想起」：刷新访问字段 + **写事件**（2026-10-22 修 07 D-12）。

    为什么要写事件：ADR-001 规定事件是唯一真相源，而访问记录此前只 UPDATE 投影、
    从未落事件——一次灾后重建就永久丢失。现在每次访问都留一笔，重建可重放。

    事件粒度：**一次调用一条事件**（payload 带 codes 列表），不是每条记忆一条。
    否则 recall 一次命中 10 条就写 10 条事件，事件量会被「看」这个动作吃掉。

    counted=False 用于「被看见但不算主动查询」的场景（inject 注入包 / read_body 读正文）：
    只刷新 last_accessed_at，不增加 access_count（保持既有语义）。
    """
    codes = [c for c in (codes or []) if c]
    if not codes:
        return 0
    ts = now_utc()
    if counted:
        conn.executemany(
            "UPDATE code_index SET last_accessed_at=?, access_count=COALESCE(access_count,0)+1, "
            "dormant=0 WHERE code=?", [(ts, c) for c in codes])
    else:
        conn.executemany(
            "UPDATE code_index SET last_accessed_at=?, dormant=0 WHERE code=?",
            [(ts, c) for c in codes])
    conn.commit()
    append_event(conn, "memory.accessed",
                 {"citizen_id": actor or "unknown", "evidence_level": "machine"},
                 {"kind": "memory", "id": codes[0] if len(codes) == 1 else "*"},
                 {"codes": codes, "source": source, "counted": bool(counted)})
    return len(codes)


# ------------------------------------------------------ 推送地基 · 访问日志（13 §2.1）
# 与 code_index 的分工（两者都要，不是二选一）：
#   access_count / last_accessed_at = **全局热度**：这条热不热、还该不该休眠
#   memory_access_log               = **按人事实**：谁读的、哪个会话、在哪个项目里读
# 为什么必须有后者：用户要判「其他 AI 经常读、但他不读」的遗漏（§3.3-B）。
# 只有全局计数时这个判据算不出来——这就是 P0 的第一块地基。
ACCESS_VIAS = ("recall", "resolve", "read_body", "inject", "push", "manual")


def append_access_log(conn, codes, *, via, citizen_id=None, session_key=None,
                      project=None, payload=None, accessed_at=None) -> int:
    """写「按人」访问记录，返回写入行数。

    容错边界（**故意的**）：本函数不做 try/except。它是唯一写入口，容错责任留在
    调用方（MCP 工具层）——这样"日志失败不影响工具调用"这条才能被单独验证；
    把 except 藏在最里层，测试就再也证明不了调用方真的兜住了。

    project 缺省时按编码回查 code_index.project：档期（stint）机制还没落地，
    这是当前能给出的最诚实的"当时在哪个项目"（不是猜，是已知事实）。
    """
    if isinstance(codes, str):
        codes = [codes]
    codes = [c for c in (codes or []) if c]
    if not codes:
        return 0
    if via not in ACCESS_VIAS:
        raise ValueError(f"未知访问来源 via={via!r}（可用：{', '.join(ACCESS_VIAS)}）")
    ts = accessed_at or now_utc()
    proj = {}
    if project is None:
        proj = dict(conn.execute(
            "SELECT code,project FROM code_index WHERE code IN ("
            + ",".join("?" * len(codes)) + ")", codes).fetchall())
    payload_s = json.dumps(payload, ensure_ascii=False) if payload is not None else None
    rows = [(c, citizen_id, session_key,
             project if project is not None else proj.get(c), via, payload_s, ts)
            for c in codes]
    conn.executemany(
        "INSERT INTO memory_access_log (code,citizen_id,session_key,project,via,payload,"
        "accessed_at) VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    return len(rows)


def access_log_rows(conn, code=None, citizen_id=None, session_key=None, via=None,
                    limit=200) -> list:
    """读访问记录（测试与后续画像/遗漏检测共用一份读法，避免两处各写一份过滤）。"""
    sql = ("SELECT code,citizen_id,session_key,project,via,payload,accessed_at "
           "FROM memory_access_log WHERE 1=1")
    params = []
    for col, val in (("code", code), ("citizen_id", citizen_id),
                     ("session_key", session_key), ("via", via)):
        if val is not None:
            sql += f" AND {col}=?"
            params.append(val)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    return conn.execute(sql, params).fetchall()


# ------------------------------------------------------ 推送批次与结果（16 §5.6）
# 与 memory_access_log 的分工（**两个维度，不是重复**）：
#   memory_access_log = **被看见**：谁、哪个会话、在哪个项目读了什么（用户行为）
#   push_log          = **这一次推送**：我们主动塞了什么、占多少预算、有没有反应
# inject 已经写过 access_log(via='push')——那记的是"这条被展示了"，记不出"这次推送批次"。
# 而批次恰恰是时机策略的计量单位：推 3 条读了 1 条 ≠ 推 1 条读了 1 条。
PUSH_CHANNELS = ("inject", "hook", "tool_after", "situational")
# 反应值是一个**封闭词表**（NULL=待判 / accepted / ignored / expired），但它没有写入校验点：
# reaction 只由 mark_push_accepted 与 close_stale_pushes 两个函数写，取值写死在 SQL 里。
# 刻意不设 PUSH_REACTIONS 常量：现在没有任何地方**校验**它，那只会变成一份会漂移的注释。
# 也刻意没有 dismissed：它需要"用户主动关掉推送"这个信号源，现在没有，不许编。

_UNSET = object()          # "不过滤"的哨兵；None 本身就是 reaction 的合法取值（待判）


def append_push_log(conn, codes, *, channel, session_key=None, citizen_id=None,
                    project=None, candidate_id=None, budget_tokens=None,
                    pushed_at=None) -> int:
    """写"这次推送"的日志：一条记忆一行，返回写入行数。

    容错边界与 append_access_log 完全一致（**故意的**）：本函数不做 try/except，
    容错责任留在调用方（MCP 工具层）——这样"推送日志失败不影响工具调用"这条
    才能被单独验证；把 except 藏在最里层，测试就再也证明不了调用方真的兜住了。

    project 缺省时按编码回查 code_index.project（与 append_access_log 同一取舍：
    档期机制没落地之前，编码所属项目是当前能给出的最诚实的"当时在哪个项目"）。
    """
    if isinstance(codes, str):
        codes = [codes]
    codes = [c for c in (codes or []) if c]
    if not codes:
        return 0
    if channel not in PUSH_CHANNELS:
        raise ValueError(
            f"未知推送渠道 channel={channel!r}（可用：{', '.join(PUSH_CHANNELS)}）")
    ts = pushed_at or now_utc()
    proj = {}
    if project is None:
        proj = dict(conn.execute(
            "SELECT code,project FROM code_index WHERE code IN ("
            + ",".join("?" * len(codes)) + ")", codes).fetchall())
    budget = int(budget_tokens) if budget_tokens is not None else None
    rows = [(session_key, citizen_id,
             project if project is not None else proj.get(c),
             c, candidate_id, channel, budget, ts) for c in codes]
    conn.executemany(
        "INSERT INTO push_log (session_key,citizen_id,project,code,candidate_id,channel,"
        "budget_tokens,pushed_at) VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return len(rows)


def push_log_rows(conn, code=None, session_key=None, channel=None, reaction=_UNSET,
                  limit=200) -> list:
    """读推送日志（测试与后续度量共用一份读法，避免两处各写一份过滤）。

    reaction 用哨兵而不是 None 缺省：**None 是一个有意义的取值（待判）**，
    "不过滤"必须能和它区分开，否则 `reaction=None` 会静默变成"全都不排队"。
    返回列顺序：code, session_key, citizen_id, project, channel, budget_tokens,
               pushed_at, reaction, reacted_at, latency_sec, candidate_id。
    """
    sql = ("SELECT code,session_key,citizen_id,project,channel,budget_tokens,pushed_at,"
           "reaction,reacted_at,latency_sec,candidate_id FROM push_log WHERE 1=1")
    params = []
    for col, val in (("code", code), ("session_key", session_key), ("channel", channel)):
        if val is not None:
            sql += f" AND {col}=?"
            params.append(val)
    if reaction is not _UNSET:
        sql += " AND reaction IS ?"          # IS 而不是 =：待判行必须用 NULL 查得到
        params.append(reaction)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    return conn.execute(sql, params).fetchall()


def push_outcome_window_hours(conn) -> int:
    """结果判定窗口（小时）。<=0 = 关闭判定（沿用 decay.*=0「永不」的既有约定）。"""
    return int(get_config(conn, "push.outcome_window_hours"))


def mark_push_accepted(conn, codes, *, session_key=None, window_hours=None,
                       reacted_at=None) -> int:
    """回填"这次推送被接受了"（读了正文 / 校验了编码），返回更新的行数。

    四个条件（13 §2.1 / 16 §5.6）：**同一会话**、**同一编码**、**窗口内**、**尚未判过**。
    前三条一条都不能省：
      · 编码 —— 推过 A 却读了 B，不能算 A 被接受
      · 会话 —— 另一个会话里读的，不证明"推给这个会话"有效
      · 窗口 —— 25 小时前推的、今天才读，中间隔了一天，算不到这次推送头上
    窗口内**所有**待判行一起置 accepted：同一编码被推过两次、只读了一次，
    两次推送都该算"被回应"，否则先那次永远悬着（而它确实是同一批里被接受的）。

    window_hours<=0 视为关闭回填（与 push.outcome_window_hours=0 的约定一致）。
    """
    if isinstance(codes, str):
        codes = [codes]
    codes = [c for c in (codes or []) if c]
    if not codes:
        return 0
    window = push_outcome_window_hours(conn) if window_hours is None else int(window_hours)
    if window <= 0:
        return 0                             # 关闭判定：不猜任何反应
    ts = reacted_at or now_utc()
    now_dt = parse_utc(ts)
    if now_dt is None:
        raise ValueError(f"reacted_at 必须是 ISO8601 UTC（如 2026-10-24T01:00:00Z），收到 {ts!r}")
    cutoff = (now_dt - datetime.timedelta(hours=window)).strftime("%Y-%m-%dT%H:%M:%SZ")
    updated = 0
    for code in codes:
        rows = conn.execute(
            "SELECT id,pushed_at FROM push_log WHERE code=? AND session_key IS ? "
            "AND reaction IS NULL AND pushed_at >= ?",
            (code, session_key, cutoff)).fetchall()
        for pid, pushed_at in rows:
            pdt = parse_utc(pushed_at)
            if pdt is None:                  # 历史坏行：不猜延迟，但反应照样记账
                conn.execute(
                    "UPDATE push_log SET reaction='accepted', reacted_at=? WHERE id=?",
                    (ts, pid))
            else:
                latency = max(int((now_dt - pdt).total_seconds()), 0)
                conn.execute(
                    "UPDATE push_log SET reaction='accepted', reacted_at=?, latency_sec=? "
                    "WHERE id=?", (ts, latency, pid))
            updated += 1
    conn.commit()
    return updated


def close_stale_pushes(conn, *, window_hours=None, now=None) -> int:
    """把超过窗口仍无反应的推送判为 ignored，返回判定行数。

    为什么"没反应"只能靠等：窗口内没读 ≠ 忽略（可能只是还没用到）。窗口是**唯一**
    能证明"这次推送白费了"的证据——正因如此，它只能由 daily_report 定时跑。

    ignored 只写 reaction：**不写 reacted_at / latency_sec**。那两列是"反应"的时间，
    这里发生的是"判定"；把判定时间冒充反应时间，画像就会以为用户在第 25 小时点了头。

    window_hours<=0 视为关闭判定（与 decay.*=0 的约定一致），返回 0。
    """
    window = push_outcome_window_hours(conn) if window_hours is None else int(window_hours)
    if window <= 0:
        return 0
    now_dt = parse_utc(now) if now else datetime.datetime.now(datetime.timezone.utc)
    if now_dt is None:
        raise ValueError(f"now 必须是 ISO8601 UTC（如 2026-10-24T01:00:00Z），收到 {now!r}")
    cutoff = (now_dt - datetime.timedelta(hours=window)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = conn.execute(
        "UPDATE push_log SET reaction='ignored' WHERE reaction IS NULL AND pushed_at < ?",
        (cutoff,))
    conn.commit()
    return cur.rowcount or 0


# ------------------------------------------------------ 推送地基 · 关联边（13 §2.2/§2.3）
# 五种关系沿用 03 §F 的既有设计，不新造。
# 存储约定：**边单向存（src→dst）、查询双向做**——同一条关系不写两条记录。
LINK_RELS = ("supersedes", "related", "derived", "contradicts", "precedes")
LINK_SOURCES = ("explicit", "structural", "semantic", "manual")


def add_link(conn, src, dst, rel, source="explicit", weight=1.0, evidence=None,
             created_at=None) -> bool:
    """建一条单向边 src→dst。返回 True=新建 / False=已存在（幂等）。

    幂等靠 UNIQUE(src,dst,rel) + INSERT OR IGNORE：同一对、同一关系重复生成不叠加。
    """
    if not src or not dst:
        raise ValueError("边的两端都必须是编码")
    if src == dst:
        raise ValueError("不能自己指向自己")
    if rel not in LINK_RELS:
        raise ValueError(f"未知关系 rel={rel!r}（可用：{', '.join(LINK_RELS)}）")
    if source not in LINK_SOURCES:
        raise ValueError(f"未知来源 source={source!r}（可用：{', '.join(LINK_SOURCES)}）")
    w = 1.0 if weight is None else float(weight)
    cur = conn.execute(
        "INSERT OR IGNORE INTO links (src,dst,rel,source,weight,evidence,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (src, dst, rel, source, w, evidence, created_at or now_utc()))
    conn.commit()
    return cur.rowcount > 0


def links_for(conn, code, rel=None, direction="both") -> list:
    """查一条记忆的关联邻居（**边单向存、这里双向查**）。

    返回 [{"code": 对方编码, "rel", "source", "weight", "evidence", "direction"}]，
    direction: out = 它是 src，in = 它是 dst。removed_at 非空的边不返回。
    """
    if direction not in ("both", "out", "in"):
        raise ValueError(f"direction 只能是 both/out/in，收到 {direction!r}")
    if direction == "both":
        where, params = "(src=? OR dst=?)", [code, code]
    elif direction == "out":
        where, params = "src=?", [code]
    else:
        where, params = "dst=?", [code]
    sql = ("SELECT src,dst,rel,source,weight,evidence FROM links "
           f"WHERE removed_at IS NULL AND {where}")
    if rel:
        sql += " AND rel=?"
        params.append(rel)
    sql += " ORDER BY id"
    out = []
    for src, dst, r, s, w, ev in conn.execute(sql, params):
        out.append({"code": dst if src == code else src, "rel": r, "source": s,
                    "weight": w, "evidence": ev,
                    "direction": "out" if src == code else "in"})
    return out


# 正文里「像编码」的串（13 §2.3 路 1 第 3 步）。
# 为什么不能只认 `[A-Z]{2,8}`：库里真实存在中文前缀（企业智能 / 言纹 / 常用会话，
# 见 07 D-14）。只认大写字母，那些记忆永远建不出「正文提及」边。
# 中文没有空格，所以这里只做**粗筛**——`见企业智能-261022-01` 前半截汉字会被并进前缀，
# 真伪交给 code_mentions_in() 用库里真实编码集合裁决。
#
# 2026-10-25 放宽（T2）：前缀字符集补上**数字与下划线**。
#   旧口径 `[A-Za-z\u4e00-\u9fff]{2,20}` 让 `A1` / `PROJ_2` / `3D` 这类前缀**永远**建不出
#   「正文提及」边——recall 查得到这条记忆，正文里写着它的编码却连不上，是静默的假阴性。
#   为什么敢放宽：本正则只负责粗筛（见上），真伪由 code_mentions_in() 拿库里真实编码集合
#   裁决；`tails()` 还会剥掉左侧误并进来的字符。误报的代价是几次空 IN 查询，漏报的代价是
#   一条永久的断链。
#   负向前瞻**保持原样**（不含中文）：把中文也挡掉会让 `见企业智能-…` 这类紧贴汉字的引用
#   全部失配，那正是本正则存在的理由。
#
# 2026-10-25 改为「右锚定」（R2，研判见 `33-编码方案研究与研判`）：
#   前缀从"写死的字符集"放开到**任意非空白字符**——因为 `anyi-studio` 里的 `-` 正是
#   编码的分隔符，旧口径对 `anyi-studio-261022-01` 直接返回 None（16 组用例实测）。
#   为什么这样仍无歧义：右边 `<6位日期>-<2~4位序号>` 是固定格式，天然是锚点；
#   解析规则是「取满足 -\d{6}-\d{2,4}$ 的**最右**一段，左边剩下的全是前缀」。
#   与 semver `1.2.3-alpha.1`（从右解析 prerelease，core 自由）同构。
#   向后兼容：204 条既有编码的解析结果逐条与改动前一致（判据 E2 钉住）。
#   仍不支持：空格（`[^\s]` 排除）与 Windows 非法字符 `<>:"/\|?*`——编码要当文件夹名。
#
# ⚠️ 必须是**非贪婪** `[^\s]+?`（2026-10-25 实测踩到）：
#   贪婪版在「先看 PROJA-261025-01，再看企业智能-261025-01」里会一路吃到句尾，
#   把第一个编码整个吞掉（因为中文逗号不是空白字符）——回归判据 M3 当场红。
#   非贪婪版会把最短的「能满足右侧锚点」的片段作为前缀，两个编码分别命中。
CODE_MENTION_RE = re.compile(
    r"(?<![0-9A-Za-z_\u4e00-\u9fff])([^\s]+?)-(\d{6})-(\d{2,4})(?!\d)")


def load_rename_map(conn) -> dict:
    """读所有 `code.renamed.batch` 事件，返回 `{旧编码: 新编码}`。

    2026-10-25 加（外部评估 `EVAL-261025-06` 报的读取侧死路）：
    `rebuild_projection` 早就用它还原编码，但**读取侧（resolve_code / read_body）没接**。
    于是改名之后，AI 拿旧编码来查会得到「编码不存在」——而正文明明在。

    后果比"查不到"更糟：**AI 会据此判定这条记忆已丢失，然后重记一条**，制造重复。
    而且它会**持续复发**——本地工作空间的 MEMORY.md / daily / artifacts 里
    还有大量 `ANYI-YYMMDD-NN` 写法，每个照旧编码查的 AI 都会踩一次。

    多次改名时**后写的覆盖先写的**（同一旧编码在时间上被复用过的情况，
    以最后一次映射为准——这与 `rebuild_projection` 的口径一致）。
    """
    m = {}
    for (payload_s,) in conn.execute(
            "SELECT payload FROM events WHERE type='code.renamed.batch' ORDER BY seq"):
        try:
            mp = json.loads(payload_s).get("mapping") or {}
        except (ValueError, TypeError):
            mp = {}
        for k, v in mp.items():
            if k and v:
                m[k] = v
    return m


def resolve_renamed(conn, code):
    """若 `code` 是**改名前**的旧编码，返回新编码；否则返回 None。

    只查一层（不做链式追踪）：改名映射是扁平的，链式追踪会让"旧→新→更新"
    这种历史噪音混进来，而实际只会有一层改名。
    """
    m = load_rename_map(conn)
    return m.get(code)


def split_code_prefix(code):
    """从编码里拆出项目前缀（**右锚定**：日期与序号格式固定，前缀任意）。

    为什么不能 `code.split("-")[0]`：前缀自 2026-10-25 起允许含连字符
    （`anyi-studio-261022-01`），按第一个 `-` 切会把 `anyi-studio` 切成 `anyi`，
    于是重建投影时项目归属全错、且**不会报错**——典型的静默数据损伤。

    口诀：**从右往左剥 `-<6位日期>-<2~4位序号>`，左边剩下的全是前缀。**
    与 `tails()` 用的是同一套规则（它用 `rsplit("-", 2)`，天然兼容）。
    """
    if not code:
        return None
    parts = code.rsplit("-", 2)
    if len(parts) != 3:
        return None
    prefix, date_part, seq = parts
    if not (date_part.isdigit() and len(date_part) == 6):
        return None
    if not (seq.isdigit() and 2 <= len(seq) <= 4):
        return None
    return prefix or None

# 语义边的两个门槛：
#   SIM_MIN_TOKENS —— 少于 3 个 token 时"重合"没有证据力（"备份"两个字就能让两条无关记忆
#                     相似度 1.0，那是噪音不是关联）
#   SIM_MIN_SIM    —— Jaccard 下限。太高会漏；太低会把"都写过 remember"这种弱关联写成边。
#                     0.15 是 P0 的起点，P2 拿真实使用数据校准。
SIM_MIN_SIM = 0.15
SIM_MIN_TOKENS = 3

# 正文最多看多少处「编码形态」的候选：粗筛会把左侧汉字并进前缀，每个候选还要展开
# 成「所有后缀前缀」，所以候选数直接决定 IN 查询的绑定参数个数（SQLite 有上限）。
_MENTION_MAX_RAWS = 20
_MENTION_MAX_PROBES = 400


def code_mentions_in(conn, text, exclude=None, limit=50) -> list:
    """文本里**真实存在**的编码（去重、按出现顺序）。

    真值来自 code_index：正则只负责粗筛，是否存在由库裁决——
    不凭正则把 typo 写成一条指向空号的边。
    """
    if not text:
        return []
    raws = []
    # 用 finditer + group(0) 而不是 findall：新正则（R2 右锚定）有**两个捕获组**，
    # `findall` 会返回元组列表（('261022','01')），`tails()` 直接崩。
    # 这里要的是**完整匹配串**（含前缀），所以取 group(0)。
    for m in CODE_MENTION_RE.finditer(text):
        raw = m.group(0)
        if raw not in raws:
            raws.append(raw)
        if len(raws) >= _MENTION_MAX_RAWS:
            break
    if not raws:
        return []

    def tails(raw):
        """粗筛串 → 所有「≥2 字符前缀」的候选编码（去掉左侧误并进来的汉字）。"""
        parts = raw.rsplit("-", 2)
        if len(parts) != 3:
            return []
        prefix, date_part, seq = parts
        cands = []
        for i in range(0, max(1, len(prefix) - 1)):
            if len(prefix) - i >= 2:
                cands.append(f"{prefix[i:]}-{date_part}-{seq}")
        return cands

    probes = set()
    for raw in raws:
        probes.update(tails(raw))
    if not probes:
        return []
    # 只认 active（2026-10-24 修）：正文提到一个**已作废 / 已被替代**的编码时不该建边——
    # 那会把新记忆指向死条目。候选集合仍来自 code_index（真值），只是把退役的排掉。
    found = {r[0] for r in conn.execute(
        "SELECT code FROM code_index WHERE code IN ("
        + ",".join("?" * min(len(probes), _MENTION_MAX_PROBES)) + ") AND status = 'active'",
        sorted(probes)[:_MENTION_MAX_PROBES])}
    if not found:
        return []
    out = []
    for raw in raws:
        hit = raw if raw in found else next((t for t in tails(raw) if t in found), None)
        if hit and hit != exclude and hit not in out:
            out.append(hit)
            if len(out) >= max(1, int(limit)):
                break
    return out


def similar_codes(conn, code, body, *, title="", limit=3, min_sim=SIM_MIN_SIM,
                  min_tokens=SIM_MIN_TOKENS) -> list:
    """FTS 粗召 + Jaccard 精排，返回 [(编码, 相似度)]（13 §2.3 路 1 第 4 步）。

    为什么两段：FTS5 的 bm25 给的是"命中强度"，不是可比的 0~1 相似度；
    links.weight 要的是 0~1（§2.2），所以相似度用 token 集合的 Jaccard 自己算。

    候选池只取 `code_index.status='active'` 的编码（2026-10-24 修）：`memory_fts` 是
    全局派生索引、不区分状态；不筛状态就会把**已作废 / 已被替代**的记忆当相似候选，
    `build_links_on_write` 随即建出指向死条目的语义边。索引本身不动，过滤只发生在读路径。
    """
    toks = {t for t in index_tokens(f"{title or ''} {body or ''}") if len(t) >= 2}
    if len(toks) < max(1, int(min_tokens)):
        return []
    expr = " OR ".join('"' + t.replace('"', '""') + '"' for t in sorted(toks)[:60])
    # 状态过滤必须**下推到 LIMIT 之前**（JOIN code_index）：先取 100 条再过滤，会被
    # 一批非 active 命中占满名额，active 候选被截断成假阴性——与 search_memories 同一个
    # 教训（ANYI-261024-05）。用 FTS 表名而非别名做 MATCH 左侧，与那边保持同一种写法。
    #
    # 2026-10-25 修（外部评估 `EVAL-261025-02` 的 N1）：**必须 ORDER BY bm25**。
    # FTS5 在没写 ORDER BY 时按**插入顺序**（rowid）返回，于是 LIMIT 100 截断的是
    # "最早入库的那 100 条命中"，不是"最相关的那 100 条"。记忆超过 200 条后，
    # 新增的那批永远进不了候选池 → **查重与写时建边静默失效**（不报错、只是不生效）。
    # 这是"没有判据就会静默"的典型：功能看着在，实际只对老数据有效。
    cands = [r[0] for r in conn.execute(
        "SELECT f.code FROM memory_fts f JOIN code_index ci ON ci.code = f.code "
        "WHERE memory_fts MATCH ? AND ci.status = 'active' "
        "ORDER BY bm25(memory_fts) LIMIT 100", (expr,))]
    cands = [c for c in cands if c and c != code]
    if not cands:
        return []
    scored = []
    for other, txt in conn.execute(
            "SELECT code, COALESCE(title_idx,'') || ' ' || COALESCE(body_idx,'') "
            "FROM memory_fts WHERE code IN (" + ",".join("?" * len(cands)) + ")",
            cands).fetchall():
        other_toks = set((txt or "").split())
        if not other_toks:
            continue
        union = len(toks | other_toks)
        if not union:
            continue
        sim = len(toks & other_toks) / union
        if sim >= min_sim:
            scored.append((sim, other))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [(c, s) for s, c in scored[:max(1, int(limit))]]


def find_similar_memories(conn, *, title="", body="", limit=3,
                          min_sim=SIM_MIN_SIM, min_tokens=SIM_MIN_TOKENS,
                          exclude=()) -> list:
    """写入**前**查重：给定标题/正文，找出库里已有的相似记忆。

    返回 `[{"code","title","similarity"}]`，按相似度降序。

    为什么单独一个函数、而不是让调用方自己拼：
      `similar_codes` 的入参是"**已经写进库**的一条记忆"（它要排除自己）；写入前
      没有 code 可给。这里用 `code=""`（空串不匹配任何真实编码）**复用它**——
      判定口径只有一份（FTS 粗召 + Jaccard 精排），不在调用方另写一套。

    **它只产提示，不拦写入**。用户 2026-10-25 的硬约束：如果每次写入都弹确认，
    AI 会绕过系统直接写文件——那比不查重糟糕得多。

    `exclude`：调用方已知的编码（例如 supersedes 的目标），不再重复提示。
    """
    pairs = similar_codes(conn, "", body or "", title=title or "", limit=limit,
                          min_sim=min_sim, min_tokens=min_tokens)
    skip = set(exclude or ())
    out = []
    for code, sim in pairs:
        if code in skip:
            continue
        row = conn.execute(
            "SELECT title, status FROM code_index WHERE code=?", (code,)).fetchone()
        if not row or row[1] != "active":
            continue
        out.append({"code": code, "title": row[0] or "",
                    "similarity": round(float(sim), 3)})
    return out


def find_similar_projects(conn, prefix, *, namespace="", limit=3) -> list:
    """创建前缀**前**查重：给出"可能是同一个项目"的候选。**只提示，不拦。**

    返回 `[{"prefix","namespace","reasons"}]`。

    三条判据（从强到弱），刻意**不用编辑距离**：
      1. **大小写归一后相同** —— 实测 `cdql` 与 `CDQL` 就是同一项目的两个入口；
      2. **前缀互为包含** —— `TESTP` / `TESTP2` 这类手滑；
      3. **命名空间描述高度重合**（token Jaccard ≥ 0.5）—— 名字完全不同但描述同义。

    为什么不用编辑距离：`TESTP` 与 `测试项目` 是两个**真实存在**的同义前缀，
    而它们的编辑距离毫无意义（一个英文一个中文）。用距离判，最该提示的那一对
    反而会被漏掉——这是"看起来更聪明的判据反而更差"的典型。
    """
    p = (prefix or "").strip()
    if not p:
        return []
    p_low = p.lower()
    ns_toks = {t for t in index_tokens(namespace or "") if len(t) >= 2}
    hits = []
    for other, other_ns in conn.execute(
            "SELECT prefix, namespace FROM code_registry WHERE status='active'"):
        if other == p:
            # 精确重名由 `add_project` 自己的查重负责（那边直接拒绝复用）；
            # 这里刻意不重复报，免得同一条信息说两遍。
            continue
        o_low = other.lower()
        reasons = []
        if o_low == p_low:
            reasons.append("大小写不同但同形")
        elif o_low.startswith(p_low) or p_low.startswith(o_low):
            reasons.append("前缀互为包含")
        if ns_toks:
            o_toks = {t for t in index_tokens(other_ns or "") if len(t) >= 2}
            if o_toks:
                union = len(ns_toks | o_toks)
                if union and len(ns_toks & o_toks) / union >= 0.5:
                    reasons.append("命名空间描述高度重合")
        if reasons:
            hits.append({"prefix": other, "namespace": other_ns or "",
                         "reasons": reasons})
    return hits[:max(1, int(limit))]


def format_dup_hint(mem_hints=None, project_hints=None) -> str:
    """把查重结果拼成回执尾巴。**全空返回空串**（不写"暂无相似"这种噪音）。

    文案纪律：先说"没拦你"，再给候选——否则 AI 会把提示当成拒绝信号，
    反而回去找绕路（那正是这条机制要避免的失败模式）。
    """
    lines = []
    if mem_hints:
        lines.append(f"查重提示（**没有拦你**，但建议先看一眼）：库里已有 "
                     f"{len(mem_hints)} 条相似记忆")
        for h in mem_hints:
            lines.append(f"  - {h['code']} {h['title']}（相似度 {h['similarity']}）")
    if project_hints:
        lines.append(f"查重提示（**没有拦你**）：已有 {len(project_hints)} 个相似前缀")
        for h in project_hints:
            lines.append(f"  - {h['prefix']}（{h['namespace']}）"
                         f" —— {'；'.join(h['reasons'])}")
    return ("\n".join(lines) + "\n") if lines else ""


# ------------------------------------------------------ 名字解析（阶段 D · 单一入口）
#
# 纪律：**所有**名字相关的查询都走 `resolve_name` / `resolve_project_names`。
# 为什么必须单一入口：`09` E-06 的教训是"顺手的 SQL 忘条件"；到了别名上，这个病的
# 后果更隐蔽——只要有一处不走这里，就会出现"**查得到但写不进**"的半截行为
# （recall 展开别名、写入却不展开，于是同一个项目看起来有两份记忆），而且**永远不会报错**。
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


def project_writable(conn, project) -> tuple:
    """项目现在能不能接收新记忆。返回 `(能不能写, 原因)`。

      · **active** → 可以写；
      · **closed** → 不能写，提示 reopen（用户拍板："close 是可逆的状态，要写就打开"）；
      · **retired** → 不能写（名字已废弃，前缀不再分配）；
      · **库里没有这个前缀** → **可以写**。这不是漏判：老库与测试库里
        `code_registry` 行可能缺失，而 `record_memory` 历来允许"先写后登记"。
        把"没登记"升级成拒绝，等于用新规则改写历史，会让所有既有数据当场不可写。
    """
    p = (project or "").strip()
    if not p:
        return True, ""
    row = conn.execute("SELECT status FROM code_registry WHERE prefix=?", (p,)).fetchone()
    if not row:
        return True, ""
    st = row[0]
    if st == "closed":
        return False, (f"项目 {p} 已闭合（closed）。要往里写，先 "
                       f"`fish project reopen {p}`（闭合是可逆的，不是锁死）")
    if st == "retired":
        return False, f"项目 {p} 已退役（retired），不再接收新记忆"
    return True, ""


def link_alias(conn, kind, from_name, to_name, *, actor=None, reason="") -> dict:
    """建立一条"`from_name` 就是 `to_name`"的别名。**单向**。

    拒绝四种情况（每一种都是真会踩的）：
      1. kind 不合法 / 名字为空 / `from == to`；
      2. **`to_name` 不存在** —— 别名必须指向一个**真实存在**的规范名，否则等于指向空号，
         查出来是"有别名、无实体"，比没有更糟；
      3. `from_name` **已有别名** —— 要改先 `unlink_alias`，不允许悄悄覆盖一条映射；
      4. **成环** —— 单向图成环之后谁都解析不出来（`A→B→A`），且不会报错。

    `to_name` 本身可以是另一个别名：会先解析到真正的规范名再落库（避免 A→B、B→C
    这种链式写入后 A 只走一跳就到 B 的歧义）。
    """
    if kind not in _ALIAS_KINDS:
        raise ValueError(f"kind 只能是 {_ALIAS_KINDS}，收到 {kind!r}")
    f = (from_name or "").strip()
    t = (to_name or "").strip()
    if not f or not t:
        raise ValueError("from_name / to_name 都不能为空")
    if f == t:
        raise ValueError("from_name 与 to_name 相同——那不是别名，是同一件事")

    info = resolve_name(conn, kind, t)
    if not info["found"]:
        raise ValueError(f"规范名不存在：{t}（别名必须指向一个真实存在的名字）")
    t = info["canonical"]
    if t == f:
        raise ValueError(f"{f} 本来就解析到 {t}，再建一条就是成环")

    row = conn.execute("SELECT to_name FROM name_aliases WHERE kind=? AND from_name=?",
                       (kind, f)).fetchone()
    if row:
        raise ValueError(f"{f} 已有别名 → {row[0]}；要改请先 unlink_alias")
    if _walk_alias(_alias_map(conn, kind), t)[0] == f:
        raise ValueError(f"会成环：{f} → {t} → … → {f}")

    conn.execute(
        "INSERT INTO name_aliases (kind,from_name,to_name,created_at,created_by,reason) "
        "VALUES (?,?,?,?,?,?)", (kind, f, t, now_utc(), actor, reason or ""))
    conn.commit()
    append_event(conn, "name.alias_linked",
                 {"citizen_id": actor, "evidence_level": "self-reported"},
                 {"kind": kind, "id": f},
                 {"from": f, "to": t, "reason": reason or ""})
    return {"kind": kind, "from": f, "to": t, "created": True}


def unlink_alias(conn, kind, from_name, *, actor=None, reason="") -> bool:
    """撤销一条别名。返回是否真的删了（**幂等**：不存在 → False，不报错）。

    为什么允许撤销：别名表达的是"我判断这两个名字是一个东西"，判断可能错。
    撤销只是删一行映射，**不碰任何记忆的 project 字段**——所以它天然可逆。
    """
    if kind not in _ALIAS_KINDS:
        raise ValueError(f"kind 只能是 {_ALIAS_KINDS}，收到 {kind!r}")
    f = (from_name or "").strip()
    if not f:
        return False
    cur = conn.execute("DELETE FROM name_aliases WHERE kind=? AND from_name=?",
                       (kind, f))
    conn.commit()
    if not cur.rowcount:
        return False
    append_event(conn, "name.alias_unlinked",
                 {"citizen_id": actor, "evidence_level": "self-reported"},
                 {"kind": kind, "id": f},
                 {"from": f, "reason": reason or ""})
    return True


def build_links_on_write(conn, code, *, title="", body="", cause=None) -> dict:
    """写入时建关联边（13 §2.3 路 1 的 explicit + semantic 两条）。

    `supersedes` 不在这里重复：它在 supersede_memory() 里双写——两条写入路径
    （remember 的 supersedes 参数、独立的 supersede 工具）共用同一处，不会各写一份。

    每条边各自兜异常：**建边失败绝不能让 remember 失败**（边是派生数据）。
    返回 {"derived": n, "mentioned": n, "similar": n}，供测试与调试读数。
    """
    stats = {"derived": 0, "mentioned": 0, "similar": 0}

    # ① derived：cause 指向的编码（B 由 A 得出）
    try:
        parents = code_mentions_in(conn, cause or "", exclude=code, limit=5)
    except Exception:
        parents = []
    for parent in parents:
        try:
            if add_link(conn, parent, code, "derived", "explicit", 1.0, "写入时 cause 指向"):
                stats["derived"] += 1
        except Exception:
            pass

    # ② related · explicit：正文里提到的编码（"这条接着那条"的显式线索）
    try:
        mentioned = code_mentions_in(conn, body or "", exclude=code)
    except Exception:
        mentioned = []
    for other in mentioned:
        try:
            if add_link(conn, code, other, "related", "explicit", 0.8, "正文提及"):
                stats["mentioned"] += 1
        except Exception:
            pass

    # ③ related · semantic：FTS 相似 top-3（排除自己）
    try:
        similars = similar_codes(conn, code, body or "", title=title, limit=3)
    except Exception:
        similars = []
    for other, sim in similars:
        try:
            if add_link(conn, code, other, "related", "semantic", round(float(sim), 4),
                        "正文相似"):
                stats["similar"] += 1
        except Exception:
            pass
    return stats


# ------------------------------------------------------ 推送地基 · 会话画像（S1，13 §3 / 16 §5.2）
# 分工：**别的步骤决定"推什么"，这一步只回答"这个会话缺什么"**。
# 本步**只读**：不写任何表（落库是 S3）、不改 build_injection_pack（S4）、
# 不加 MCP 工具（mcp_server 仍是 11 个）。
#
# 为什么画像必须分成「本会话」与「同类其他会话」两栏：
#   `tool_usage` / `knowledge_gaps` 里的"我"是**会话级事实**（这个会话做过什么）；
#   但要判断"某项算不算遗漏"，必须拿**背景**来比——一个会话只有 5 次调用时，
#   凭它自己看不出什么是常态。所以 peer 一栏取同账号（无账号则同项目）的
#   其他会话，且**必须排除自己**：不排除的话 my_calls 恒等于 peer_calls，
#   盲区永远是空的（判据 C 钉的就是这一条）。
#
# 为什么画像不落库：落库要回答"多久更新一次、旧画像怎么失效"，那是 S3 的事；
# 先让计算侧能被判据钉死，再谈持久化。这也是 20 §6「先做 S1+S2 干跑」的要求。

# 11 个官方 MCP 工具（20 §1.3）。**盲区只在这个集合里报**：
# 集合外的名字（`fish://me` 之类的资源读、CLI 命令名）不是"没用过这个工具"，
# 而是"这个概念不成立"——报出去就是噪音。
TOOL_UNIVERSE = ("remember", "recall", "add_project", "supersede", "read_body",
                 "neighbors", "resolve", "whoami", "register", "inject",
                 "healthcheck")

# 工具别名 → 规范名。实测（2026-10-24）：`resolve_code` 32 次、`resolve` 0 次；
# MCP 里叫 resolve，早期审计记的是 resolve_code——**同一个工具的两个名字**。
# 不归一就会出现两个键，于是"我用了 5 次"和"同类会话用了 8 次"各算各的，盲区判定失真。
TOOL_ALIASES = {"resolve_code": "resolve"}

# 判据门槛（13 §3.3 的算式）。提成常量是为了让判据里的"门槛 5 次"能被单独钉住
# （判据 D：把 5 改成 4，盲区就必须多出来一条）。
BLIND_SPOT_MIN_PEER_CALLS = 5        # 同类其他会话累计调用 ≥5 次才算"他们常用"
BLIND_SPOT_WINDOW_DAYS = 30          # "常用"的观察窗
KNOWLEDGE_GAP_MIN_PEER_READS = 3     # 同类其他会话累计读过 ≥3 次才算"别人常读"
KNOWLEDGE_GAP_WINDOW_DAYS = 14       # "常读"的观察窗
KNOWLEDGE_GAP_LIMIT = 10             # 遗漏最多报这么多条

AUDIT_DIR_NAME = "_audit"
AUDIT_TOOL_CALLS_FILE = "tool-calls.jsonl"

# 同类判定依据的人话（why 字段要让 AI 一眼看懂"跟谁比"）。
_BASIS_TEXT = {"actor": "同账号", "project": "同项目"}


def normalize_tool_name(name):
    """工具名归一化（`resolve_code` → `resolve`）。空/非字符串 → None。

    归一化是**读侧**的事：审计文件是历史事实，不改写它；两个名字在统计时合并。
    """
    if not isinstance(name, str):
        return None
    n = name.strip()
    if not n:
        return None
    return TOOL_ALIASES.get(n, n)


def _parse_iso_utc(raw):
    """ISO8601 → UTC aware datetime；解析不了返回 None。

    为什么不复用 `parse_utc`：它只吃 `...SZ` 一种形态，而审计文件里真实存在
    `2026-10-24T18:32:10+08:00` 这样的带偏移行（2026-10-24 实测）。窗口过滤若把
    这类行当"解析失败"丢掉，近 30 天的盲区统计会静默漏掉最新那批调用。
    """
    if not isinstance(raw, str):
        return None
    txt = raw.strip()
    if not txt:
        return None
    if txt.endswith(("Z", "z")):
        txt = txt[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(txt)
    except ValueError:
        # 3.10 及更早只认 3 位或 6 位小数秒；去掉小数部分再试一次（不猜时区）
        try:
            dt = datetime.datetime.fromisoformat(re.sub(r"\.\d+", "", txt))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def _audit_ts(rec):
    """审计行的时刻：优先 `ts_utc`（机器换算过的 UTC），回退 `ts`。"""
    for key in ("ts_utc", "ts"):
        dt = _parse_iso_utc(rec.get(key))
        if dt is not None:
            return dt
    return None


def _audit_in_window(rec, since):
    """是否落在观察窗内。

    缺时间戳的行**保留**：审计行是调用结束后写的，没有 ts 是记录方的缺陷，
    不能当成"旧行"——丢掉它就等于把"实测调用 6 次"静默改写成 3 次。
    """
    ts = _audit_ts(rec)
    return True if ts is None else ts >= since


def read_tool_call_audit(root):
    """读 `<root>/_audit/tool-calls.jsonl`，返回 dict 列表（坏行跳过）。

    **文件不存在 = 没有证据，不是错误**（判据 G）：新库、或宿主还没接审计工具时
    这里必然什么都没有。此时画像各项为空，但 `stats.computed_at` 仍然给出——
    "这一刻算过、结果是空"与"根本算过没有"必须能区分开。
    """
    if root is None:
        return []
    path = Path(root) / AUDIT_DIR_NAME / AUDIT_TOOL_CALLS_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # 读不到 = 没有证据：文件不存在、权限不够、编码坏了都走这里。
        # 画像只读且非关键路径，坏文件该由审计侧报警，不该让画像崩。
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _session_label(rec):
    """审计行的会话标签：`session_actor` 优先，回退 `actor`。

    两种形态在真实文件里同时存在：新行 `{actor: <公民>, session_actor: <会话名>}`，
    老行只有 `{actor: <名字>}`。只认一个会把自己另一半调用算到"未标注"里。
    """
    for key in ("session_actor", "actor"):
        val = rec.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _iter_tool_calls(audit):
    """遍历「成功记过的工具调用」，产出 (规范工具名, 原始行)。

    两条过滤都是铁的：
      · `kind == 'tool'` —— resource 读（`fish://me`）不是工具调用（20 §5.2）；
      · `ok is True`     —— 失败的调用不算"用过"（一次报错的 remember 什么都证明不了）。
    """
    for rec in audit:
        if not isinstance(rec, dict):
            continue
        if rec.get("kind") != "tool":
            continue
        if rec.get("ok") is not True:
            continue
        tool = normalize_tool_name(rec.get("name"))
        if tool:
            yield tool, rec


def _citizen_by_value(conn, value):
    """一个可能是 `display_name` 也可能是 `citizen_id` 的值 → (citizen_id, display_name)。

    **复用既有的两个读点**（`find_citizen_by_name` 按名字查活跃公民、`load_citizen`
    按 id 查冷热一致视图），不新开 citizens 查询面：P1-a 判据 G 冻结了全库
    citizens 引用函数的白名单，画像这一步没有理由再加一个口子（要加就得先声明，
    那是 P1-d 的事）。查不到 → (None, None)，绝不猜。
    """
    if not isinstance(value, str) or not value.strip():
        return None, None
    val = value.strip()
    try:
        row = find_citizen_by_name(conn, val)
    except sqlite3.Error:         # 稀有：老库缺表/缺视图。查不到 ≠ 画像可以崩
        row = None
    if row:
        return row[0], row[1]
    try:
        rec = load_citizen(conn, val)
    except sqlite3.Error:
        rec = None
    if rec:
        return rec["citizen_id"], rec["display_name"]
    return None, None


def _citizen_aliases(conn, values, *, must_exist=False):
    """把账号标识展开成「同一个人」的全部写法（display_name ↔ citizen_id）。

    `must_exist=True` 时只有库里真认得的标识才留下——会话名常常只是个纯标签
    （`anyi-studio-开发AI` 不是任何公民），把标签当账号会把不相干的会话当成"同账号"。
    另一个用途：审计里的 `actor` 有时写 display_name、有时写 citizen_id，而访问日志
    里存的是 citizen_id，展开后才能对上号。
    """
    aliases, todo = set(), [v.strip() for v in values if isinstance(v, str) and v.strip()]
    while todo:
        val = todo.pop()
        if val in aliases:
            continue
        cid, name = _citizen_by_value(conn, val)
        for other in (cid, name):
            if other and other not in aliases:
                todo.append(other)
        if (cid or name) or not must_exist:
            aliases.add(val)
    return aliases


def _display_name_of(conn, citizen_id):
    """citizen_id → display_name；查不到返回 None。"""
    return _citizen_by_value(conn, citizen_id)[1]


def _citizen_id_of(conn, value):
    """display_name / citizen_id → citizen_id；查不到返回 None。"""
    return _citizen_by_value(conn, value)[0]


def _audit_actor_values(audit, label):
    """本会话审计行里出现过的 `actor`（= 同账号的直接证据）。"""
    out = []
    for rec in audit:
        if _session_label(rec) != label:
            continue
        actor = rec.get("actor")
        if isinstance(actor, str) and actor.strip() and actor.strip() not in out:
            out.append(actor.strip())
    return out


def _resolve_profile_session(conn, audit, session_actor=None, citizen_id=None):
    """解析「这是哪个会话 / 哪个公民」，返回 {"label", "session"}。

    `label` 是后续一切匹配用的键——审计的 `session_actor` 与访问日志的 `session_key`
    用的就是这个字符串（2026-10-24 实测两处同名同形），所以它必须是**入参原样**。

    `session.citizen_id` 三级解析，都不猜：
      ① 入参 citizen_id；
      ② `citizens.display_name == 会话名`（老审计里 actor 写的就是账号名）；
      ③ 本会话审计行的 `actor` 反查公民（实测 `anyi-studio-开发AI` 的行里
         actor=`workbuddy-main`，而它确实是一个公民的 display_name）。
    """
    label = (session_actor or "").strip() or None
    if label:
        resolved_by = "session_actor"
    elif citizen_id:
        resolved_by = "citizen_id"
    else:
        resolved_by = "none"

    cid = (citizen_id or "").strip() or None
    if cid is None and label:
        cid = _citizen_id_of(conn, label)
    if cid is None and label:
        for actor in _audit_actor_values(audit, label):
            cid = _citizen_id_of(conn, actor)
            if cid:
                break
    if label is None and cid:
        label = _display_name_of(conn, cid)

    return {"label": label,
            "session": {"session_actor": label, "citizen_id": cid,
                        "resolved_by": resolved_by}}


def _project_scope(conn, label):
    """本会话可能归属的项目键集合（**只认库里已有的项目名**，不编新键）。

    两个证据源（3.2 的第 2 级）：
      ① 会话名前缀：`anyi-studio-开发AI` → `anyi-studio`。实测库里项目键是
         ANYI / 企业智能 / CDQL，所以这一条**常常什么都不给**——那正是"不许猜"
         的效果：给不出同项目就不给，而不是拿全库凑数；
      ② 本会话写入的记忆的 project（`code_index.session_actor = 会话名`）。
    """
    if not label:
        return set()
    projects = set()
    try:
        for (proj,) in conn.execute(
                "SELECT DISTINCT project FROM code_index WHERE session_actor=? "
                "AND project IS NOT NULL AND project != ''", (label,)):
            projects.add(proj)
        prefix = label.split("-")[0].strip()
        if prefix and prefix != label:
            for (proj,) in conn.execute(
                    "SELECT DISTINCT project FROM code_index WHERE LOWER(project)=LOWER(?)",
                    (prefix,)):
                projects.add(proj)
    except sqlite3.Error:
        return set()
    return projects


def _label_in_project(label, projects):
    """审计行的会话标签是否属于这些项目（`ANYI-开发AI` / `ANYI` 都算 ANYI）。"""
    if not label or not projects:
        return False
    head = label.split("-")[0].strip().lower()
    return any(label.lower() == p.lower() or head == p.lower() for p in projects)


def _candidate_scopes(conn, audit, label, citizen_id):
    """按 3.2 的优先级排出「同类」判定依据（有序，最多两个）。

    ① 同 actor（同一个 AI 账号）：账号标识来自入参 citizen_id、会话名（若它本身
       就是公民）、以及本会话审计行里的 `actor`——三条都是**已知事实**；
    ② 同 project：会话名前缀 / 本会话写过的记忆所属项目。
    两者都没有 → 返回空列表，调用方返回空结果（**绝不退回全库**）。

    为什么返回**列表**而不是一个 scope：当高优先级的依据在数据里一条同类记录
    都找不到时（例如账号在 `memory_access_log` 里从没写过行），继续用它只会
    得到"没有遗漏"这个**假阴性**——静默失败正是 16 §5.4 要防的。所以调用方
    按顺序取第一个"真的找得到同类"的依据，并把用了哪一档写进 `why`。
    """
    scopes = []
    accounts = _citizen_aliases(conn, [citizen_id, *_audit_actor_values(audit, label)])
    accounts |= _citizen_aliases(conn, [label], must_exist=True).difference({label})
    if accounts:
        scopes.append({"basis": "actor", "accounts": accounts, "projects": set()})
    projects = _project_scope(conn, label)
    if projects:
        scopes.append({"basis": "project", "accounts": set(), "projects": projects})
    return scopes


def _record_in_scope(rec, scope):
    """审计行是否属于这个"同类"范围（不含"排除自己"，那一步在调用方做）。"""
    basis = scope.get("basis")
    if basis == "actor":
        actor = rec.get("actor")
        return isinstance(actor, str) and actor.strip() in scope["accounts"]
    if basis == "project":
        return _label_in_project(_session_label(rec), scope["projects"])
    return False


def _tool_usage(audit, label):
    """本会话的工具调用次数（成功才算），按次数降序、同数按名字排。"""
    usage = {}
    for tool, rec in _iter_tool_calls(audit):
        if _session_label(rec) == label:
            usage[tool] = usage.get(tool, 0) + 1
    return {name: usage[name] for name in sorted(usage, key=lambda t: (-usage[t], t))}


def _peer_tool_counts(audit, scope, my_label, since):
    """同类**其他**会话在窗口内的工具调用次数（本会话的行一律不算）。"""
    counts = {}
    for tool, rec in _iter_tool_calls(audit):
        if _session_label(rec) == my_label:
            continue
        if not _record_in_scope(rec, scope):
            continue
        if not _audit_in_window(rec, since):
            continue
        counts[tool] = counts.get(tool, 0) + 1
    return counts


def _tool_blind_spots(audit, my_label, usage, scopes, since):
    """同类其他会话常用、本会话 0 次的工具（3.2：peer_calls ≥5 且 my_calls == 0）。"""
    for scope in scopes:
        peer = _peer_tool_counts(audit, scope, my_label, since)
        if not peer:
            continue                     # 这一档找不到同类证据 → 看下一档
        spots = []
        for tool in TOOL_UNIVERSE:       # 只报官方 11 个工具，集合外的一律不报
            peer_calls = peer.get(tool, 0)
            my_calls = usage.get(tool, 0)
            if peer_calls >= BLIND_SPOT_MIN_PEER_CALLS and my_calls == 0:
                spots.append({
                    "tool": tool,
                    "peer_calls": peer_calls,
                    "my_calls": my_calls,
                    "why": (f"{_BASIS_TEXT[scope['basis']]}其他会话近 "
                            f"{BLIND_SPOT_WINDOW_DAYS} 天调用 {peer_calls} 次，本会话 0 次"),
                })
        spots.sort(key=lambda s: (-s["peer_calls"], s["tool"]))
        return spots
    return []


def _access_scope_where(scope):
    """把 scope 翻成 `memory_access_log` 的 WHERE 片段与参数。

    访问日志与审计文件的"同类"键不同：这里只能按 `citizen_id`（谁读的）或
    `project`（在哪个项目读的）分——`session_key` 是会话，不是同类。
    """
    if scope.get("basis") == "actor" and scope["accounts"]:
        keys = sorted(scope["accounts"])
        return "m.citizen_id IN (" + ",".join("?" * len(keys)) + ")", list(keys)
    if scope.get("basis") == "project" and scope["projects"]:
        keys = sorted(scope["projects"])
        return "m.project IN (" + ",".join("?" * len(keys)) + ")", list(keys)
    return "1 = 0", []


def _peer_access_count(conn, scope, my_label, since_z):
    """同类其他会话在窗口内的访问行数（= 这一档有没有同类证据）。"""
    where, params = _access_scope_where(scope)
    sql = ("SELECT COUNT(*) FROM memory_access_log m WHERE m.accessed_at >= ? "
           "AND COALESCE(m.session_key, '') != ? AND " + where)
    try:
        return conn.execute(sql, [since_z, my_label or "", *params]).fetchone()[0]
    except sqlite3.Error:
        return 0


def _knowledge_gap_rows(conn, scope, my_label, since_z, limit):
    """遗漏候选（3.3 的四条判据全部落在 SQL 里，口径只有一份）。

    ```text
    ① 该条 status='active'
    ② 同类其他会话近 M 天读过 ≥3 次
    ③ 本会话 0 次（NOT EXISTS：本会话任何一次读过就不算遗漏）
    ④ 按"别人读的次数"降序，取前 limit
    ```
    为什么不用 `code_index.access_count`：那是**全局累计**，分不出"谁读的"——
    用它算遗漏会把本会话自己的阅读也算成"别人常读"，判据 ③ 立刻失效。
    """
    where, params = _access_scope_where(scope)
    sql = ("SELECT m.code, COUNT(*) AS n FROM memory_access_log m "
           "JOIN code_index ci ON ci.code = m.code AND ci.status = 'active' "
           "WHERE m.accessed_at >= ? AND COALESCE(m.session_key, '') != ? AND "
           + where +
           " AND NOT EXISTS (SELECT 1 FROM memory_access_log s "
           "                 WHERE s.code = m.code AND s.session_key = ?) "
           "GROUP BY m.code HAVING COUNT(*) >= ? "
           "ORDER BY n DESC, m.code ASC LIMIT ?")
    try:
        return conn.execute(sql, [since_z, my_label or "", *params, my_label or "",
                                 KNOWLEDGE_GAP_MIN_PEER_READS, int(limit)]).fetchall()
    except sqlite3.Error:
        return []


def _titles_of(conn, codes):
    """批量取标题（一条 SQL，不做 N 次查询）。"""
    codes = [c for c in codes if c]
    if not codes:
        return {}
    try:
        return {c: (t or "") for c, t in conn.execute(
            "SELECT code,COALESCE(title,'') FROM code_index WHERE code IN ("
            + ",".join("?" * len(codes)) + ")", codes)}
    except sqlite3.Error:
        return {}


def _knowledge_gaps(conn, scopes, my_label, since_z, limit):
    """别人常读、本会话没读过的记忆（取第一个找得到同类证据的那一档）。"""
    for scope in scopes:
        if _peer_access_count(conn, scope, my_label, since_z) == 0:
            continue
        rows = _knowledge_gap_rows(conn, scope, my_label, since_z, limit)
        titles = _titles_of(conn, [r[0] for r in rows])
        return [{
            "code": code,
            "title": titles.get(code, ""),
            "others_read": int(n),
            "my_reads": 0,
            "why": (f"{_BASIS_TEXT[scope['basis']]}其他会话近 "
                    f"{KNOWLEDGE_GAP_WINDOW_DAYS} 天读 {int(n)} 次，本会话 0 次"),
        } for code, n in rows]
    return []


def _access_log_total(conn, label):
    """本会话在 `memory_access_log` 里的行数（表不在 → 0，绝不崩）。"""
    if not label:
        return 0
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM memory_access_log WHERE session_key=?",
            (label,)).fetchone()[0]
    except sqlite3.Error:
        return 0


def compute_session_profile(conn, *, session_actor=None, citizen_id=None,
                            root=None) -> dict:
    """算一个会话的画像：**只读**，不写任何表、不碰注入包。

    返回结构（**字段固定，不多不少**——S3 落库与 S4 注入都按这个契约读）：

    ```python
    {
      "session": {"session_actor", "citizen_id", "resolved_by"},
      "tool_usage": {"remember": 128, ...},          # 本会话成功调用次数
      "tool_blind_spots": [{"tool", "peer_calls", "my_calls", "why"}],
      "knowledge_gaps": [{"code", "title", "others_read", "my_reads", "why"}],
      "stats": {"tool_calls_total", "access_log_total", "computed_at"},
    }
    ```

    三个子算法的口径（13 §3.3 / 16 §5.3）：
      · `tool_usage`   —— `data/_audit/tool-calls.jsonl` 里 `session_actor` 精确匹配、
                          `kind='tool'` 且 `ok=True`（`resolve_code` 归一成 `resolve`）；
      · `tool_blind_spots` —— 同类其他会话近 30 天调用 ≥5 次、本会话 0 次，且只在
                          11 个官方工具里报；
      · `knowledge_gaps`   —— `memory_access_log` 里同类其他会话近 14 天读过 ≥3 次、
                          本会话 0 次、该条 status='active'，按别人读的次数降序取前 10。

    `root` 是数据目录（`connect(root)` 的那个 root）；缺省时读不到审计文件，
    `tool_usage` / `tool_blind_spots` 自然为空——这**不是错误**，见
    `read_tool_call_audit` 的说明。
    """
    computed_at = now_utc()
    now_dt = parse_utc(computed_at) or datetime.datetime.now(datetime.timezone.utc)
    since_audit = now_dt - datetime.timedelta(days=BLIND_SPOT_WINDOW_DAYS)
    since_access = now_dt - datetime.timedelta(days=KNOWLEDGE_GAP_WINDOW_DAYS)

    audit = read_tool_call_audit(root)
    resolved = _resolve_profile_session(conn, audit, session_actor=session_actor,
                                       citizen_id=citizen_id)
    label = resolved["label"]

    profile = {
        "session": resolved["session"],
        "tool_usage": {},
        "tool_blind_spots": [],
        "knowledge_gaps": [],
        "stats": {"tool_calls_total": 0, "access_log_total": 0,
                  "computed_at": computed_at},
    }
    if not label:
        # 连"是哪个会话"都说不出来 → 不猜：返回空画像（统计口径仍是"这一刻算过"）。
        return profile

    usage = _tool_usage(audit, label)
    scopes = _candidate_scopes(conn, audit, label, resolved["session"]["citizen_id"])
    profile["tool_usage"] = usage
    profile["tool_blind_spots"] = _tool_blind_spots(audit, label, usage, scopes, since_audit)
    profile["knowledge_gaps"] = _knowledge_gaps(
        conn, scopes, label, since_access.strftime("%Y-%m-%dT%H:%M:%SZ"), KNOWLEDGE_GAP_LIMIT)
    profile["stats"]["tool_calls_total"] = sum(usage.values())
    profile["stats"]["access_log_total"] = _access_log_total(conn, label)
    return profile


# ------------------------------------------------------------------ 全文检索（ADR-005）

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


def search_memories(conn, *, query=None, code=None, project=None, tags=None,
                    scope="all", limit=10, include_superseded=False):
    """检索记忆——**CLI 与 MCP 共用的唯一读入口**（ADR-005）。

    排序优先级：精确编码 > 编码片段 > 标题命中 > 正文命中。
    扫描只发生在 SQL；切分与去重在这里，两条调用路径不许各写一份。

    返回 (rows, sources)：rows 每项是
      (code, title, project, mem_type, status, file_path, dormant, 匹配来源)
    """
    limit = max(1, int(limit or 10))
    order, src_of = [], {}

    def add(codes, source):
        for c in codes:
            if c and c not in src_of:
                src_of[c] = source
                order.append(c)

    q = (query or "").strip()

    # 过滤器在 FTS 命中同一次查询里完成（2026-10-24 修 · 反馈 ANYI-261024-05）：
    # 旧写法先 `LIMIT n` 取候选、再按 status 过滤，于是"最早那几条命中恰好都已 superseded"
    # 的查询会被整体滤空——记忆明明存在却查不到（实测 query="反馈"：命中 23 条、返回 0 条，
    # 其中 active 有 8 条）。假阴性比报错更难发现，所以状态/项目/标签必须和 MATCH 一起下推。
    # 阶段 D（2026-10-25）：项目过滤要展开别名——但它**只影响检索**，不改写入归属
    # （用户拍板）。同一份名字集合必须用在**两个地方**：FTS 命中的下推过滤，以及
    # 最后按 order 回取行时的过滤。两处用不同的集合 = 结果忽多忽少且不报错。
    _pnames = resolve_project_names(conn, project) if project else []
    _filters, _fparams = [], []
    if not include_superseded:
        _filters.append("ci.status = 'active'")
    if project:
        # 展开成 IN（含别名）：`recall(project='cdql')` 要能看到 `CDQL` 的记忆。
        _filters.append("ci.project IN (" + ",".join("?" * len(_pnames)) + ")")
        _fparams.extend(_pnames)
    for _t in ([tags] if isinstance(tags, str) else (tags or [])):
        if _t:
            _filters.append("ci.tags LIKE ?")
            _fparams.append(f'%"{_t}"%')
    _where = (" AND " + " AND ".join(_filters)) if _filters else ""
    _fts_sql = ("SELECT f.code FROM memory_fts f JOIN code_index ci ON ci.code = f.code "
                "WHERE memory_fts MATCH ?" + _where + " LIMIT ?")

    if code:
        add([r[0] for r in conn.execute(
            "SELECT code FROM code_index WHERE code=?", (code,))], "编码")

    if q:
        add([r[0] for r in conn.execute(
            "SELECT code FROM code_index WHERE code=?", (q,))], "编码")
        # 编码片段：FTS 对"部分编码"无能为力（token 化会把号码切碎），保留 LIKE 兜底
        add([r[0] for r in conn.execute(
            "SELECT code FROM code_index WHERE code LIKE ? ORDER BY code DESC LIMIT ?",
            (f"%{q}%", limit))], "编码")
        expr = fts_match_expr(q)
        if expr:
            if scope in ("all", "title"):
                add([r[0] for r in conn.execute(
                    _fts_sql, (f"title_idx:({expr})", *_fparams, limit))], "标题")
            if scope in ("all", "body"):
                add([r[0] for r in conn.execute(
                    _fts_sql, (f"body_idx:({expr})", *_fparams, limit))], "正文")

    if not order:
        return [], {}

    sql = ("SELECT code,title,project,mem_type,status,file_path,dormant FROM code_index "
           "WHERE code IN (" + ",".join("?" * len(order)) + ")")
    params = list(order)
    if not include_superseded:
        # 默认只给 active：superseded（被取代）与 withdrawn（作废）都不该出现在默认检索里
        sql += " AND status = 'active'"
    if project:
        sql += " AND project IN (" + ",".join("?" * len(_pnames)) + ")"
        params.extend(_pnames)
    for t in ([tags] if isinstance(tags, str) else (tags or [])):
        if t:
            sql += " AND tags LIKE ?"
            params.append(f'%"{t}"%')

    found = {r[0]: r for r in conn.execute(sql, params)}
    rows = []
    for c in order:
        if c in found:
            rows.append(tuple(found[c]) + (src_of[c],))
            if len(rows) >= limit:
                break
    return rows, src_of


def recent_fallback(conn, project=None, tags=None, limit=5, include_superseded=False):
    """未命中时给"最近的条目"作提示——**必须遵守与检索同样的过滤**。

    2026-10-22 缺陷（反馈 `ANYI-261022-114` 报的面）：旧实现直接
    `SELECT code,title FROM code_index ORDER BY code DESC LIMIT 5`，取全库最近 5 条，
    于是 `project=ANYI` 的查询未命中时，会捞出一串别的项目的条目（跨项目串台）。
    把它收进这里，CLI 与 MCP 共用一份——两边各写一份正是这个 bug 的来源。

    2026-10-24 补 status：上面那句"同样的过滤"长期只是**诺言**——实现只筛了
    project / tags，于是**作废 / 已被替代**的条目会出现在"最近的条目"里。默认只给
    active，与 `search_memories` 的默认口径一致；`include_superseded=True` 才放开
    （显式要看退役条目时用，例如排查"这条为什么查不到"），调用方按关键字传：
    `recent_fallback(conn, project=p, include_superseded=True)`。
    """
    limit = max(1, int(limit or 5))
    sql = "SELECT code,title FROM code_index"
    where, params = [], []
    if not include_superseded:
        where.append("status = 'active'")
    if project:
        # 2026-10-25：**必须展开别名**，与 `search_memories` 走同一口径。
        # 旧实现是等值 `project = ?`，于是「同一项目的两个入口」（cdql / CDQL、
        # 或 ANYI → anyi-studio）在**未命中的回退列表**里只给一半——而回退列表
        # 往往是用户唯一看到的东西（主检索没命中的时候）。
        # 这正是 2026-10-22 那个"回退列表不遵守过滤"的 bug 的同族：过滤条件只落实了一半。
        pnames = resolve_project_names(conn, project)
        if len(pnames) <= 1:
            where.append("project = ?")
            params.append(pnames[0] if pnames else project)
        else:
            where.append("project IN (" + ",".join("?" * len(pnames)) + ")")
            params.extend(pnames)
    for t in ([tags] if isinstance(tags, str) else (tags or [])):
        if t:
            where.append("tags LIKE ?")
            params.append(f'%"{t}"%')
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY code DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def withdraw_memory(conn, code: str, reason: str = "", actor=None) -> str:
    """作废一条记忆：`status → withdrawn`，**编码不回收**（ADR-003）。

    为什么用 withdrawn 而不是删除：
      - 事件永不删除（ADR-001，`ev_tables` 上有触发器拦 UPDATE/DELETE）；
      - 正文文件删掉会造成断链，`healthcheck` 会报（02 §8「断链数」是自检项之一）；
      - ADR-003 的编码状态机本来就写了 `active → superseded / withdrawn`，
        并明确「作废只标状态、号不回收」；02 §1.2 的事件 #7 `memory.retired`
        用 payload 的 `status` 区分 superseded / withdrawn。
    所以「弃用」是规格内的动作，不发明任何新东西（2026-10-22 关闭 07 D-6）。

    与 supersede 的区别：supersede 是「被谁取代」（有接替者、链能走），
    withdraw 是「作废」（没有接替者）。两者语义不同，不许混用。
    """
    row = conn.execute("SELECT status,title FROM code_index WHERE code=?", (code,)).fetchone()
    if not row:
        raise ValueError(f"编码不存在：{code}")
    if row[0] == "withdrawn":
        return f"{code} 已经是 withdrawn（本次未改动）"
    if row[0] == "superseded":
        raise ValueError(
            f"{code} 已被 supersede（有替代者）。取代与作废是两种语义："
            f"要作废请先确认那条替代链是否也要处理。")
    conn.execute("UPDATE code_index SET status='withdrawn' WHERE code=?", (code,))
    conn.commit()
    append_event(conn, "memory.retired",
                 {"citizen_id": actor or "unknown", "evidence_level": "machine"},
                 {"kind": "memory", "id": code},
                 {"code": code, "status": "withdrawn", "reason": reason})
    return f"{code} 已作废（withdrawn，编码不回收）"


def resolve_owner(conn) -> str:
    """系统主人：优先读实体属性 citizens.is_owner；回退旧配置（向后兼容）。"""
    row = conn.execute(
        "SELECT citizen_id FROM citizens WHERE is_owner=1 AND status='active'").fetchone()
    if row:
        return row[0]
    # 兼容 v0.3 之前的写法：owner 存在 config 里
    return get_config(conn, "owner_citizen") or get_config(conn, "default_citizen")


# 旧 CLI 的 --owner 默认值。它从来不是公民 ID，只是"人类"的占位说法。
# 2026-10-22 吃狗粮事故：这个默认值把 AI 的写入静默记到主人名下、且指向不存在的公民
# （见 06 §9.7-2）。保留常量只为识别并归一历史值，不再作为默认值使用。
LEGACY_OWNER_DEFAULT = "human-anyi"


def resolve_owner_arg(conn, owner):
    """把调用方传来的 owner 归一成**有效的 citizen_id**，或者响亮报错。

    语义（2026-10-22 定，见 07 D-13）：
      - 未指定（None / 空）→ 系统主人 `resolve_owner()`；再没有就 "unknown"
      - 旧默认值 "human-anyi" → 同上（它的原意就是"人类"，不是某个公民 ID）
      - 有效 citizen_id → 原样返回
      - 其他任何字符串（显示名、拼错的 ID、随手写的名字）→ 报错并给下一步

    为什么不再静默接受：`owner` 按 02 §2.3 是公民主键。收下不存在的 ID 会写出指向幽灵
    公民的归属，而且没有任何提示——这是 D-10 之外另一个静默数据缺陷。
    """
    if owner in (None, ""):
        return resolve_owner(conn) or "unknown"
    if conn.execute("SELECT 1 FROM citizens WHERE citizen_id=?", (owner,)).fetchone():
        return owner
    if owner == LEGACY_OWNER_DEFAULT:
        return resolve_owner(conn) or "unknown"
    cands = conn.execute(
        "SELECT citizen_id,display_name,kind FROM citizens WHERE status='active' "
        "ORDER BY created_at").fetchall()
    listing = "\n".join(f"    {c[0]}  {c[1]}（{c[2]}）" for c in cands) or "    （库里还没有公民）"
    raise ValueError(
        f"owner 必须是公民 ID，收到 {owner!r}。\n"
        f"  可用公民：\n{listing}\n"
        f"  下一步：`fish citizen-list` 查 ID；不给 --owner 则默认记到系统主人。")


def set_owner(conn, citizen_id):
    """把某个公民设为系统主人。唯一性由部分唯一索引 + 先清后设保证。"""
    row = conn.execute("SELECT kind,status,display_name FROM citizens WHERE citizen_id=?",
                       (citizen_id,)).fetchone()
    if not row:
        raise ValueError(f"公民不存在：{citizen_id}")
    kind, status, name = row
    if status != "active":
        raise ValueError(f"公民 {name} 状态为 {status}，不能设为主人")
    if kind != "resident":
        raise ValueError(
            f"主人必须是 resident 形态（{name} 当前是 {kind}）。"
            "若确需，请先把它改为 resident。")
    conn.execute("UPDATE citizens SET is_owner=0 WHERE is_owner=1")
    conn.execute("UPDATE citizens SET is_owner=1 WHERE citizen_id=?", (citizen_id,))
    conn.commit()
    append_event(conn, "config.changed",
                 {"citizen_id": "system", "evidence_level": "signed"},
                 {"kind": "citizen", "id": citizen_id},
                 {"key": "owner", "new_value": citizen_id})
    return name


def find_citizen_by_name(conn, name):
    """按 display_name 查活跃公民；查不到返回 None。

    2026-10-24 修（外部评估会话暴露的真问题）：原来是裸 `fetchone()` 且**没有
    ORDER BY**。当同一个 display_name 有多条活跃公民时（实测 `fish外部评估`
    就有 2 条），返回哪一条不确定 —— 后果是 `resolve_actor` 的归属会在多条
    重复公民之间漂移，两次调用可能给出不同结果，"谁写的"变得不可复现。

    修法：`ORDER BY created_at, citizen_id` —— 取**最早创建**的那条，再按 id
    兜底保证完全确定。为什么选最早：`register` 的语义是幂等复用，幂等失败时
    先建的那条才是"原始身份"，后建的是误建。

    ⚠️ 这只是让**结果确定**，不是去重。重复公民本身要靠人工/merge 清理 ——
    发现重复时该报出来，不该静默挑一条（`find_duplicate_display_names` 就是为此）。
    """
    if not name:
        return None
    return conn.execute(
        "SELECT citizen_id,display_name,kind,role,host FROM citizens "
        "WHERE display_name=? AND status='active' "
        "ORDER BY created_at, citizen_id LIMIT 1", (name,)).fetchone()


def find_duplicate_display_names(conn) -> list:
    """列出有重复的 display_name（同名多公民）。

    为什么单独一个函数：「结果确定」和「数据干净」是两件事。`find_citizen_by_name`
    修了前者（ORDER BY），但重复本身还在 —— 它会让 `whoami`、归属统计、
    推送画像全部带上噪声，而且**永远不会有报错**（只是数字不对）。
    所以要把"发现重复"做成一个可被查询、可被判据盯住的动作。
    """
    return [
        {"display_name": r[0], "count": r[1], "citizen_ids": r[2]}
        for r in conn.execute(
            "SELECT display_name, COUNT(*) AS n, GROUP_CONCAT(citizen_id) "
            "FROM citizens WHERE status='active' AND display_name IS NOT NULL "
            "GROUP BY display_name HAVING n > 1 ORDER BY n DESC, display_name")
    ]


def rename_citizen(conn, citizen_id, new_name, *, actor=None, reason="") -> dict:
    """把公民的 display_name 改成 new_name。**只改标签，不改身份。**

    返回 `{"citizen_id","old_name","new_name","changed","same_name_exists","event"}`。

    为什么需要它（2026-10-25 用户裁定：这是 BUG，不是增强）：
      用户开场说"你是 X"，系统里却没有任何机制把这个名字绑到当前公民上——
      `display_name` 只在**公民创建的那一瞬间**产生一次，此后全库 6 条
      `UPDATE citizens` 没有一条碰它（改的是 is_owner / last_seen_at /
      account_id / status）。直接后果：用户指定的名字在系统里**无处安放**，
      只能以"另一条孤儿公民"的形式存在（`register` 建行但不绑定），
      于是"可被检索的名字"对用户来说根本不存在。

    三条边界（都不许越）：
      1. **名字不是身份**：只改 `display_name`，不动 `citizen_id` / 凭证 / 证据级。
         历史记忆的 owner 是 `citizen_id`，所以改名**不会**让任何历史记录换主人——
         第一棒担心的"历史记忆指向陌生名字"是**显示层**的事，不是数据完整性。
      2. **事件留痕**：写 `citizen.renamed`（payload 带 from/to/actor/reason）。
         "它原来叫什么"必须可查，否则改名就是静默改写历史。
      3. **同名幂等**：新旧名相同 → 不写事件、`changed=False`（避免无意义噪声）。

    重名**允许**（决策 14：同名视作两个公民，不合并），但 `same_name_exists`
    会把同名的其它公民报出来——让调用方显示"已有同名公民 X"，而不是静默重复。

    `reason` 用于记录"谁在什么场景下改的名"（例如"用户在开场指定"）。
    """
    new_name = (new_name or "").strip()
    if not new_name:
        raise ValueError("display_name 不能为空")
    if len(new_name) > 64:
        # 与 `code_registry.prefix` 的 20 字符上限同理：名字是给人看的短标识，
        # 超长名字在注入包的一行里会挤掉别的信息。取 64 是因为要考虑角色后缀
        # （`host:role` 形态）与中文名，比前缀宽松但仍有界。
        raise ValueError(f"display_name 太长（{len(new_name)} 字符，上限 64）")

    row = conn.execute(
        "SELECT display_name, status FROM citizens WHERE citizen_id=?",
        (citizen_id,)).fetchone()
    if not row:
        raise ValueError(f"公民不存在：{citizen_id}")
    old_name, status = row
    if status != "active":
        raise ValueError(f"只能给活跃公民改名（{citizen_id} 当前状态 {status}）")

    if old_name == new_name:
        return {"citizen_id": citizen_id, "old_name": old_name, "new_name": new_name,
                "changed": False, "same_name_exists": [], "event": None}

    cur = conn.execute("UPDATE citizens SET display_name=? WHERE citizen_id=?",
                       (new_name, citizen_id))
    conn.commit()
    if cur.rowcount == 0:
        # 公民在归档表里（`citizens` 热表查不到）——`load_citizen` 走 citizens_all
        # 能读到它，但改名只该发生在活跃行上。响亮失败，不静默成功。
        raise ValueError(f"公民不在热表里（可能已归档），不能改名：{citizen_id}")

    same = []
    for d in find_duplicate_display_names(conn):
        if d["display_name"] != new_name:
            continue
        for cid in str(d["citizen_ids"] or "").split(","):
            cid = cid.strip()
            if cid and cid != citizen_id:
                same.append(cid)

    append_event(conn, "citizen.renamed",
                 {"citizen_id": citizen_id, "evidence_level": "self-reported"},
                 {"kind": "citizen", "id": citizen_id},
                 {"from": old_name, "to": new_name,
                  "actor": actor, "reason": reason or ""})
    return {"citizen_id": citizen_id, "old_name": old_name, "new_name": new_name,
            "changed": True, "same_name_exists": same, "event": "citizen.renamed"}


def resolve_actor(conn, actor_name=None):
    """当前行动者：按名字查公民；查不到返回 None（未登记访客）。

    与 resolve_owner 的区别（本轮修复的核心）：
      owner  = 系统主人（human/resident），一个系统只有一个
      actor  = 此刻在动手的公民（visitor/role），可能每次会话都不同
    whoami 必须返回 actor，否则 AI 会自称主人。
    """
    if actor_name is None:
        # 优先级：环境变量 FISH_ACTOR > config.actor_name
        # 环境变量优先，因为 MCP hook 是独立进程，配置不一定能传进来
        actor_name = os.environ.get("FISH_ACTOR") or get_config(conn, "actor_name")
    return find_citizen_by_name(conn, actor_name)


# ------------------------------------------------------ 身份层 P1-b：会话身份与凭证
# 依据：`18-身份层实现设计-P1b.md`（+ `16` §2 / `17` §2.4-2.5 / 评审 R-2）。
#
# 一句话理由：**MCP 面拿不到真实会话身份（宿主把 MCP 进程的 env scrub 掉了），
# 所以身份只能由"能读到 env 的一方"签发，然后交给 MCP 校验。**
#
# 铁律（R-2）：`session` 是 **CLI 专属命令族**，绝不注册成 MCP 工具
# （`mcp_server.TOOLS` 仍是 11 个，由 test_identity_p1b 判据 H 冻结）。
# MCP 侧将来只读 `session_tokens` 做校验，永远不签发 machine 级——它的环境里
# 没有任何东西可比对，声称 machine 就是自欺。
#
# 凭证三条纪律（铁律⑮）：
#   1. 明文只在 `session start` / `session agent` 的 **stdout** 出现一次：
#      不写文件、不进日志、不进事件 payload（事件里只写 sha256）；
#   2. 同一会话重跑 start = 轮换凭证（旧凭证立即吊销），公民不变（18 §10 的幂等承诺）；
#   3. `end` 只标 revoked_at，不删行——审计需要"谁什么时候拿过凭证、何时失效"。
#
# 已知边界（18 §9，实现层面必须记住的四条）：
#   ① 明文会落进宿主会话日志 → 缓解只有 TTL + 可吊销，做不到"日志里也没有"；
#   ② 没有 hook 时 AI 必须手动调 `session start`——它会忘（幂等就是为这个留的后路）；
#   ③ 宿主不给 session id 时身份退化为 self-reported（下面 build_session_identity 负责）；
#   ④ account_id 允许为空，表示"还没归属到某个 AI 账号"，后续可补。

SESSION_TOKEN_TTL_KEY = "session.token_ttl_hours"
SESSION_EVIDENCE_LEVELS = ("machine", "inherited", "self-reported")
SESSION_CHANNELS = ("cli", "hook", "parent")

# 宿主会话标识的 env（**按优先级**）。DSH 排第一是因为它是本机现役宿主；
# 另两个常作为"宿主配置里残留的值"被继承进子进程，谁真谁假以现役宿主为准。
HOST_SESSION_ENVS = (
    ("dsh", "DSH_SESSION_ID"),
    ("codebuddy", "CODEBUDDY_SESSION_ID"),
    ("claude", "CLAUDE_SESSION_ID"),
)

# 自报会话名的 env（**不是机器事实**：它只是宿主配置里写下的一个名字）。
# 为什么仍要读：无 hook 时这是唯一能让"重跑 start 命中同一个公民"的线索（18 §10）。
SESSION_ALIAS_ENVS = ("FISH_SESSION_ACTOR", "FISH_ACTOR")

# 会话标识的两条非机器前缀。`self:` = 自报名当幂等键；`anon:` = 什么都没有。
SELF_SESSION_PREFIX = "self:"
ANON_SESSION_PREFIX = "anon:"

# token 明文的前缀。存在理由见 `issue_session_token` 的 docstring：
# 主要是**别以 `-` 开头**（argparse 会把它当选项），顺带让 token 自解释、可被扫描器识别。
SESSION_TOKEN_PREFIX = "fsh_"

# 会话公民的读列契约（顺序就是 zip 的拼接口，改动必须同步 _citizen_row）。
_CITIZEN_READ_FIELDS = ("citizen_id", "display_name", "kind", "role", "host", "status",
                        "account_id", "host_session_key", "last_seen_at", "archived_at")

# 档期读列契约。
_STINT_READ_FIELDS = ("id", "citizen_id", "account_id", "project", "from_ts", "set_by")


def _citizen_row(row):
    return dict(zip(_CITIZEN_READ_FIELDS, row)) if row else None


def host_session_facts(environ=None) -> dict:
    """读进程环境里的**机器事实**：哪个宿主、会话 id 是什么。

    只认环境变量，**不接受任何调用方传入的会话名**——这就是 machine 级的定义：
    它来自进程环境，不来自模型文本。一个都没命中时返回 evidence=None，
    让上层去走自报/匿名分支，而不是在这里编一个（编出来的值就是自欺）。
    """
    environ = os.environ if environ is None else environ
    for host, var in HOST_SESSION_ENVS:
        raw = (environ.get(var) or "").strip()
        if raw:
            return {"host": host, "session_id": raw,
                    "session_key": f"{host}:{raw}", "evidence": "machine", "source": var}
    return {"host": None, "session_id": None, "session_key": None,
            "evidence": None, "source": None}


def env_session_alias(environ=None):
    """自报会话名（来自 env），没有就 None。"""
    environ = os.environ if environ is None else environ
    for var in SESSION_ALIAS_ENVS:
        raw = (environ.get(var) or "").strip()
        if raw:
            return raw
    return None


def env_actor_name(environ=None):
    """AI 账号的 env 来源（`FISH_ACTOR`）。与"会话名"不是一个概念：账号跨会话，会话不跨。"""
    environ = os.environ if environ is None else environ
    raw = (environ.get("FISH_ACTOR") or "").strip()
    return raw or None


def build_session_identity(alias=None, environ=None) -> dict:
    """把「进程环境 + 调用参数」翻译成会话身份（**不碰库**）。

    返回 {session_key, evidence, channel, host, alias, source, warning}。

    优先级 env（machine）> 自报名（self-reported）> 匿名：
      三者的可信度严格单调，所以顺序不能换——把 alias 放在 env 前面，
      一条自报就能覆盖机器事实，那正是 R-2 说的自欺。
    匿名（既无 env 也无别名）**每次都新建公民**：没有任何东西能证明"这是同一个会话"，
    硬编一个稳定 key 等于凭空宣布一个事实；宁可多建一个匿名公民，也不假装认识它。
    """
    facts = host_session_facts(environ)
    reported = (alias or "").strip() or env_session_alias(environ)
    if facts["evidence"] == "machine":
        return {"session_key": facts["session_key"], "evidence": "machine",
                "channel": "cli", "host": facts["host"], "alias": reported,
                "source": facts["source"], "warning": None}
    if reported:
        return {"session_key": f"{SELF_SESSION_PREFIX}{reported}",
                "evidence": "self-reported", "channel": "cli", "host": "cli",
                "alias": reported, "source": "alias", "warning": None}
    return {"session_key": f"{ANON_SESSION_PREFIX}{ulid()}", "evidence": "self-reported",
            "channel": "cli", "host": "cli", "alias": None, "source": "anonymous",
            "warning": ("没有任何会话标识（env 里没有宿主 session id，也没给 --alias）："
                        "本次建成一个匿名公民，重跑 start 会再建一个。"
                        "想复用同一个公民 → 带上 --alias 或设 FISH_SESSION_ACTOR。")}


def resolve_account_id(account=None, alias=None, environ=None):
    """账号锚点的来源：`--account` > `FISH_ACTOR` env > None。

    **2026-10-25 改**（外部评估 `EVAL-261025-03`）：**不再用 `--alias` 兜底**。

    为什么：alias 是"这个会话叫什么"（给人看的追溯标签），account 是"这个 AI 是哪个账号"
    （归属锚点）。把前者当后者，等于**用一个描述性字段推身份**——而两者语义不同：

      · 同一个 AI 开两个会话 → 两个 alias、同一个 account（应该）
      · 旧逻辑会得出两个不同的 account → 建出两个公民 → **归属分裂且无法合并**

    实测代价（`EVAL-261025-03` 亲历）：`session start --alias X` 建出的公民
    与 MCP `register` 给的公民**不是同一个**，而三条命令全都返回 OK，
    只有去比 `citizen_id` 两串十六进制才会发现不对。**最贵的是它静默。**

    空着仍然比编出来诚实（18 §9-4）：调用方要归属，就显式给 `--account`。
    """
    for cand in (account, env_actor_name(environ)):
        text = (cand or "").strip()
        if text:
            return text
    return None


def auto_session_display_name(host, session_key, role=None) -> str:
    """没给别名时的自动名：`<宿主>-<会话短码>`（子 agent 带 `:<role>`）。

    截断到 12 位是因为 display_name 是给人看的追溯标签、不是主键：
    全量 session id 会把 `citizen-list` 的表格撑破，而追溯靠的是 host_session_key。
    """
    tail = (session_key or "").split(":", 1)[-1].split("#", 1)[0]
    base = f"{host or 'cli'}-{tail[:12]}" if tail else f"{host or 'cli'}-{ulid()[:8]}"
    return f"{base}:{role}" if role else base


def hash_session_token(token: str) -> str:
    """库里只存 sha256(token)。**明文没有第二份副本**（表里没有明文列）。"""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def session_token_ttl_hours(conn) -> int:
    return int(get_config(conn, SESSION_TOKEN_TTL_KEY))


def _new_expires_at(conn, ttl_hours=None):
    """凭证到期时刻。0 或负数 = 永不过期（返回 None，与 decay.*=0 同一套约定）。"""
    ttl = session_token_ttl_hours(conn) if ttl_hours is None else int(ttl_hours)
    if ttl <= 0:
        return None
    moment = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=ttl)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_expired(expires_at, at=None) -> bool:
    """expires_at 为空 = 永不失效。字符串比较即可：定长 UTC ISO 的字典序就是时序。"""
    if not expires_at:
        return False
    return expires_at <= (at or now_utc())


def issue_session_token(conn, *, citizen_id, session_key, evidence, channel,
                        parent_hash=None, ttl_hours=None) -> dict:
    """签发一条凭证：生成明文 → 存 hash → 把明文交回调用方（**全流程唯一一次**）。

    明文只经手这一处，调用方唯一的合法动作是 print 到 stdout。想事后审计就查
    `token_hash`——明文离开 stdout 就没有了，这是设计而不是缺陷（铁律⑮）。
    词表校验放在这里：入口再多，bad row 都从这一道门过去（DDL 的 CHECK 是第二道）。

    **为什么 token 带 `fsh_` 前缀**（相对 18 §2 的 `secrets.token_urlsafe(32)` 是一处偏离，
    理由是实测出来的）：token_urlsafe 的字母表里有 `-`，约 1.3% 的 token 会以 `-` 开头；
    而 `fish session agent --parent <token>` 走 argparse，**以 `-` 开头的值会被当成选项**，
    报 `argument --parent: expected one argument`（2026-10-24 全量测试实测命中，
    400 次抽样中 5 次）。前缀顺手还解决了两件事：token 在日志里自解释、可被扫描器识别。
    熵不变（仍是 32 字节随机）。
    """
    if evidence not in SESSION_EVIDENCE_LEVELS:
        raise ValueError(f"evidence 只能是 {SESSION_EVIDENCE_LEVELS}，收到 {evidence!r}")
    if channel not in SESSION_CHANNELS:
        raise ValueError(f"channel 只能是 {SESSION_CHANNELS}，收到 {channel!r}")
    token = f"{SESSION_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    thash = hash_session_token(token)
    issued_at = now_utc()
    expires_at = _new_expires_at(conn, ttl_hours)
    conn.execute(
        "INSERT INTO session_tokens (token_hash,citizen_id,session_key,evidence,channel,"
        "parent_token,issued_at,expires_at) VALUES (?,?,?,?,?,?,?,?)",
        (thash, citizen_id, session_key, evidence, channel, parent_hash, issued_at,
         expires_at))
    return {"token": token, "token_hash": thash, "citizen_id": citizen_id,
            "session_key": session_key, "evidence": evidence, "channel": channel,
            "parent_token": parent_hash, "issued_at": issued_at,
            "expires_at": expires_at}


def revoke_session_tokens(conn, *, session_key=None, citizen_id=None, token_hash=None,
                          parent_hash=None) -> int:
    """吊销凭证（**只标 revoked_at，不删行**），返回吊销条数。

    四种口径互不重叠，全部走这一处——"什么算失效"只能有一份判断，多一份就会漂：
      session_key  同一会话的所有活凭证（start 轮换 / end 当前会话）
      citizen_id   某公民的所有活凭证（公民退役时用）
      token_hash   单条（`end --token`）
      parent_hash  某条凭证派生出的子凭证（父会话结束时级联，见 end_session）
    一个过滤条件都没给就报错：全表吊销是灾难，不能靠"忘了传参"触发。
    """
    sets, where, params = ["revoked_at=?"], [], [now_utc()]
    for column, value in (("session_key", session_key), ("citizen_id", citizen_id),
                          ("token_hash", token_hash), ("parent_token", parent_hash)):
        if value is not None:
            where.append(f"{column}=?")
            params.append(value)
    if not where:
        raise ValueError("吊销必须给至少一个条件（session_key/citizen_id/token_hash/父凭证）")
    where.append("revoked_at IS NULL")
    sql = f"UPDATE session_tokens SET {', '.join(sets)} WHERE {' AND '.join(where)}"
    return conn.execute(sql, params).rowcount


def lookup_session_token(conn, token):
    """按明文查凭证行（**不判有效性**：吊销/过期也返回），查不到返回 None。

    为什么留一个"能查出坏凭证"的口子：`session status` 的价值就在回答
    "我这条 token 为什么不能用"——只返回有效行，失效原因就永远看不见。
    有效性判断只有一处：`validate_session_token`。
    """
    if not token:
        return None
    row = conn.execute(
        "SELECT token_hash,citizen_id,session_key,evidence,channel,parent_token,"
        "issued_at,expires_at,revoked_at FROM session_tokens WHERE token_hash=?",
        (hash_session_token(token),)).fetchone()
    if not row:
        return None
    keys = ("token_hash", "citizen_id", "session_key", "evidence", "channel",
            "parent_token", "issued_at", "expires_at", "revoked_at")
    info = dict(zip(keys, row))
    info["expired"] = _is_expired(info["expires_at"])
    info["valid"] = (not info["revoked_at"]) and (not info["expired"])
    return info


def validate_session_token(conn, token, at=None):
    """**MCP 侧的校验契约**（18 §7）：有效 → citizen_id；无效（吊销/过期/不存在）→ None。

    这就是那条 SQL 的唯一实现：
      token_hash=? AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now)
    P1-c 的 MCP 工具只调这一处，不许各自再写一遍——两处实现必然漂，
    而"两处实现的失效判断"正是凭证体系里最不该漂的东西。
    """
    info = lookup_session_token(conn, token)
    if info is None:
        return None
    moment = at or now_utc()
    if info["revoked_at"] and info["revoked_at"] <= moment:
        return None
    if info["expires_at"] and info["expires_at"] <= moment:
        return None
    return info["citizen_id"]


def find_session_citizen(conn, session_key):
    """按宿主会话标识找**活跃**会话公民（走 citizens_all：归档后身份不能悬空）。

    只认 status='active'：退役/归档的公民不该被一条会话标识"复活"。
    """
    if not session_key:
        return None
    return _citizen_row(conn.execute(
        "SELECT citizen_id,display_name,kind,role,host,status,account_id,"
        "host_session_key,last_seen_at,archived_at FROM citizens_all "
        "WHERE host_session_key=? AND status='active' ORDER BY created_at LIMIT 1",
        (session_key,)).fetchone())


def load_citizen(conn, citizen_id):
    """读一个公民（走 citizens_all：冷热一致，owner 永不悬空）。"""
    if not citizen_id:
        return None
    return _citizen_row(conn.execute(
        "SELECT citizen_id,display_name,kind,role,host,status,account_id,"
        "host_session_key,last_seen_at,archived_at FROM citizens_all "
        "WHERE citizen_id=?", (citizen_id,)).fetchone())


def touch_citizen(conn, citizen_id, *, at=None) -> int:
    """刷新 last_seen_at。返回受影响行数（0 = 它不在热表里，通常意味着已归档）。"""
    return conn.execute("UPDATE citizens SET last_seen_at=? WHERE citizen_id=?",
                        (at or now_utc(), citizen_id)).rowcount


def find_or_create_session_citizen(conn, *, session_key, evidence, host, display_name,
                                   account_id=None, kind="visitor", role=None) -> dict:
    """按 host_session_key 找活跃会话公民；没有就建。返回 dict（含 created 标记）。

    **这是幂等的全部实现**（18 §10：token 丢了重跑 start → 公民不变、凭证换新）。
    主键选 host_session_key 而不是 display_name：库里已有 `dsh-main` vs
    `dsh-维护工程师` 这种不一致，拿名字当主键意味着改名就断血缘（17 §2.4）。

    复用只补两件事，**绝不改名**（改名让历史记忆指向一个陌生的名字）：
      · last_seen_at = now（活跃痕迹）
      · account_id 原来为空、这次能推断出来 → 补上；已有值不覆盖（那是别的会话的归属）
    """
    found = find_session_citizen(conn, session_key)
    if found:
        citizen_id = found["citizen_id"]
        touch_citizen(conn, citizen_id)
        if account_id and not found.get("account_id"):
            conn.execute("UPDATE citizens SET account_id=? WHERE citizen_id=?",
                         (account_id, citizen_id))
            found["account_id"] = account_id
        # 返回的是**库里生效的** account_id（没覆盖就仍是原来那个）：输出与库一致，
        # 否则"打印说 A、库里是 B"是审计时最难解释的那种不一致。
        return {"citizen_id": citizen_id, "created": False,
                "display_name": found["display_name"],
                "account_id": found.get("account_id"),
                "archived": bool(found.get("archived_at"))}
    citizen_id = ulid()
    stamp = now_utc()
    conn.execute(
        "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at,"
        "is_owner,account_id,device_id,host_session_key,last_seen_at,archived_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (citizen_id, display_name, kind, role, host, "active", stamp,
         0, account_id, None, session_key, stamp, None))
    append_event(conn, "citizen.registered",
                 {"citizen_id": citizen_id, "evidence_level": evidence},
                 {"kind": "citizen", "id": citizen_id},
                 {"display_name": display_name, "kind": kind, "role": role,
                  "host": host, "host_session_key": session_key,
                  "account_id": account_id, "via": "session.start"})
    return {"citizen_id": citizen_id, "created": True, "display_name": display_name,
            "account_id": account_id, "archived": False}


# ------------------------------------------------------ 工作区 → 项目映射（读写口）
# 依据与归一化纪律见 _WORKSPACE_MAP_DDL 的说明。这一节只做"路径 ↔ 项目"的映射，
# 不碰档期（那在下面）、也不碰注入包渲染（那在 build_injection_pack 附近）。

_WORKSPACE_MAP_FIELDS = ("path_prefix", "project", "note", "created_at")

# 正文里抓绝对路径：盘符 + 分隔符之后，遇到空白或常见中英文标点就截断。
# 刻意把反引号/引号/右括号列进"停止字符"：正文里的路径常被 `…`、"…"、（）包着，
# 那些标点是正文的，不是路径的一部分。
_ABS_PATH_RE = re.compile(r"[A-Za-z]:[/\\][^\s\"'`（）()，,；;：:｜|\[\]<>]+")

# 只在**后缀明确是文件**时才退到父目录。否则 `I:/app/cdql.cc` 会被当成文件 `cc`，
# 候选就变成了 `I:/app`——一个几乎必然跨项目的噪声前缀。
_PATH_FILE_EXTS = frozenset({
    ".md", ".txt", ".json", ".jsonl", ".py", ".js", ".ts", ".mjs", ".ps1", ".bat",
    ".yml", ".yaml", ".toml", ".ini", ".cfg", ".log", ".csv", ".xlsx", ".docx",
    ".pptx", ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".zip",
    ".sln", ".csproj", ".cs", ".html", ".css", ".exe", ".dll",
})

# seed 的降噪清单：这些目录不是"某个项目的工作区"。
# 刻意**不**整片跳过 c:/users —— 临时目录与部分真实工作区就在用户目录下；
# 只跳真正的系统目录与 AppData（那里全是工具自己的状态文件）。
# `.workbuddy` 同理：那是 agent 的状态/记忆目录，不是代码工作区
# （它常出现在路径里，如 `.../anyi-studio-AI工作空间/.workbuddy/memory`——
#  真正的候选是它的父目录，不是它自己）。
_SEED_SKIP_PREFIXES = ("c:/windows", "c:/program files", "c:/programdata")
_SEED_SKIP_PARTS = ("/appdata/", "/.workbuddy/")


def normalize_workspace_path(path) -> str:
    """把工作区路径归一成 workspace_map 的**存法**与**比法**：正斜杠 · 无尾斜杠 · 全小写。

    三条规则各自的理由：
      · 正斜杠：Windows 上 `\\` 与 `/` 混用是常态（手输、日志、JSON 各不同一套），
        不统一就会出现"明明映射了却匹配不上"；主键也只能有一个写法。
      · 无尾斜杠：`/a/b/` 与 `/a/b` 是同一个目录，而前缀匹配是字符串操作，差一个
        字符就漏。根路径（`/`、`c:/`）保留最小形态，不塌成空串。
      · 全小写：Windows 的路径比较不区分大小写。这是**本机平台的取舍**——在 POSIX
        大小写敏感的系统上，两个只差大小写的路径会被当成同一个（已在此声明）。

    UNC（`\\\\server\\share`）会被压成 `/server/share`：两侧用同一条归一化，
    匹配语义不受影响；显示上少一个斜杠是可接受的代价（比多一套分支可靠）。

    空值/纯空白返回空串：由调用方决定它是"写入错误"还是"这次没给"。
    """
    if path is None:
        return ""
    raw = str(path).strip()
    if not raw:
        return ""
    s = raw.replace("\\", "/")
    s = re.sub(r"/+", "/", s)
    # 2026-10-24 修（自查发现）：之前漏了 `.` / `..` 归一化，导致
    # `/a/b/../bc` 被按字面前缀判成 `/a/b` 的子路径（它实际是 `/a/bc`）。
    # 虽然 process.cwd() 返回的路径本就归一，但 `--cwd` 与 hook payload 都是
    # 可构造输入——按"判据要能挡住坏输入"的原则补上。
    # ⚠️ 两个坑，都是实测出来的：
    #   ① posixpath.normpath('c:/') == 'c:'（去掉根斜杠）→ 会破坏下面"根路径保留最小形态"
    #      的约定，所以要把盘符根补回 `c:/`；
    #   ② 必须用 posixpath 而不是 os.path：后者在 Windows 上会把 `/` 转回 `\`，
    #      让上面两行的正斜杠归一化白做。
    import posixpath
    s = posixpath.normpath(s)
    # ⚠️ 顺序不能反：先去尾斜杠，再补盘符根的斜杠。
    # 反过来的话，补上的 `/` 会被这条 while 立刻砍掉（实测踩过：C:\ 出来是 c:）。
    while len(s) > 1 and s.endswith("/"):
        s = s[:-1]
    # ⚠️ 正则必须容忍大写盘符：此时还没走到末尾的 s.lower()，
    # `C:\` 归一后是 `C:`，用 r"[a-z]:" 会匹配不上、盘符根就不补斜杠了（实测踩过）。
    if re.fullmatch(r"[A-Za-z]:", s):
        s += "/"
    return s.lower()


def resolve_project_by_cwd(conn, cwd) -> "str | None":
    """按**最长前缀 + 路径分量边界**把工作区路径解析成项目前缀；没有匹配返回 None。

    为什么不按"包含"匹配：`/a/b` 是 `/a/bc` 的字符串前缀，但**不是**它的父路径。
    必须按分量边界判：`/a/b` 覆盖 `/a/b` 与 `/a/b/...`，不覆盖 `/a/bc`。
    为什么最长优先：父子目录可以各有归属（`/a` → P1、`/a/b` → P2），
    `/a/b/c` 应该属于更具体的 `/a/b`。

    为什么在 Python 里全表扫、不用 SQL LIKE：前缀里的 `_` / `%` 是 LIKE 的通配符，
    要正确匹配就得转义（转义写错是**静默错配**，最难查的那类 bug）；而这张表是按
    "项目"维护的配置表，行数是个位数。全扫 + 内存里比长度更不容易错，真到几千行再换。

    读侧再归一一次：库里若有人手写了大写/尾斜杠的行，这里不让它变成幽灵路径
    （写侧 set_workspace_mapping 已经归一，读侧这次是防手写数据）。

    **没有匹配返回 None，绝不回退到"猜一个"**：R1 的病根就是猜（见
    _resolve_injection_project 的四档说明）。返回 None 让上层能直说"未声明"。
    """
    target = normalize_workspace_path(cwd)
    if not target:
        return None
    best = None                       # (prefix_len, project)
    for prefix, project in conn.execute(
            "SELECT path_prefix, project FROM workspace_map").fetchall():
        norm = normalize_workspace_path(prefix)
        if not norm:
            continue
        if target == norm or target.startswith(norm + "/"):
            key = (len(norm), project)
            # 同长度不可能有两条不同前缀（主键唯一）；仍给一个稳定次序，
            # 免得将来碰上脏数据时输出随查询顺序漂。
            if best is None or key[0] > best[0] or (key[0] == best[0] and key[1] < best[1]):
                best = key
    return best[1] if best else None


def list_workspace_candidates(conn, cwd) -> list:
    """cwd **之下**（严格子目录）已映射的项目。**只读查询，一行都不改。**

    为什么需要它（2026-10-24 真实踩坑）：注入包只写「项目 : （未声明）」时，
    人分不清是 ①hook 没传 cwd、②cwd 不在映射里、③解析算法坏了——三种处置完全不同，
    输出却一模一样。②里最常见的一种是"我就在某个已映射项目的**父目录**里干活"
    （典型：`workspace/learn` 下放着 8 个项目，每个都映射了，只有父目录没映射）。
    把 cwd 之下已有的映射列出来，人就能直接抄一条 `fish workspace set` 修好，
    不用去猜、也不用读库。

    为什么是"严格子目录"（不含 cwd 自己）：等值那条一旦存在，`resolve_project_by_cwd`
    就已经命中了，根本走不到"未声明"分支——把它算成候选是给一条不可达的分支写文案。

    为什么在 Python 里比前缀、不用 SQL LIKE：与 `resolve_project_by_cwd` 同一条理由
    （LIKE 的 `_`/`%` 是通配符，转义写错是静默错配）；这张表按项目维护，行数是个位数。

    返回：[{path_prefix, project, note}]，**由浅到深**排序（先看最外层的项目目录）；
    同长度按路径字典序，保证输出不随查询顺序漂。没有任何候选时返回空列表（调用方
    据此决定"别写一堆废话"）。
    """
    target = normalize_workspace_path(cwd)
    if not target:
        return []
    found = []
    for prefix, project, note in conn.execute(
            "SELECT path_prefix, project, note FROM workspace_map").fetchall():
        norm = normalize_workspace_path(prefix)      # 防手写数据：库里若有大写/尾斜杠
        if not norm or norm == target:
            continue
        if not norm.startswith(target + "/"):
            continue
        found.append({"path_prefix": norm, "project": project, "note": note})
    found.sort(key=lambda it: (len(it["path_prefix"]), it["path_prefix"]))
    return found


def set_workspace_mapping(conn, path, project, *, note=None) -> dict:
    """新增或更新一条映射（唯一写入口：CLI `fish workspace set`）。

    返回 {"path_prefix", "project", "note", "created"}；created=False 表示覆盖了旧行。

    为什么用 UPSERT 而不是"先查再插"：两句 SQL 之间没有并发保护，而"同一路径两条
    映射"在语义上就是错的（主键也会拒）。UPSERT 把判定交给库自己，一次往返。
    **created_at 只在第一次写**：它记的是"这条路什么时候被声明归属"，不是最后修改
    时间；要最后修改时间该另加一列，不在这里混成一个含义模糊的字段。
    **note 是声明式覆盖**：不传就清空（想保留旧备注就把旧值带上）。半覆盖（没给就
    保持原值）会让"这条到底是什么备注"取决于它被改过几次，比清空更难解释。
    """
    prefix = normalize_workspace_path(path)
    if not prefix:
        raise ValueError("路径不能为空")
    if not isinstance(project, str):
        raise TypeError(f"project 必须是字符串，收到 {type(project).__name__}")
    name = project.strip()
    if not name:
        raise ValueError("项目前缀不能为空")
    if note is not None and not isinstance(note, str):
        raise TypeError(f"note 必须是字符串，收到 {type(note).__name__}")
    clean_note = (note or "").strip() or None
    existed = conn.execute(
        "SELECT 1 FROM workspace_map WHERE path_prefix=?", (prefix,)).fetchone()
    conn.execute(
        "INSERT INTO workspace_map (path_prefix, project, note, created_at) "
        "VALUES (?,?,?,?) "
        "ON CONFLICT(path_prefix) DO UPDATE SET project=excluded.project, note=excluded.note",
        (prefix, name, clean_note, now_utc()))
    conn.commit()
    append_event(conn, "workspace.mapped",
                 {"citizen_id": "system", "evidence_level": "signed"},
                 {"kind": "workspace", "id": prefix},
                 {"project": name, "note": clean_note, "created": not bool(existed)})
    return {"path_prefix": prefix, "project": name, "note": clean_note,
            "created": not bool(existed)}


def list_workspace_mappings(conn) -> list:
    """全部映射（按路径升序）。字段见 _WORKSPACE_MAP_FIELDS。"""
    rows = conn.execute(
        "SELECT path_prefix, project, note, created_at FROM workspace_map "
        "ORDER BY path_prefix").fetchall()
    return [dict(zip(_WORKSPACE_MAP_FIELDS, r)) for r in rows]


def remove_workspace_mapping(conn, path) -> bool:
    """删掉一条映射；返回是否真的删了（幂等：不存在 → False，不报错）。"""
    prefix = normalize_workspace_path(path)
    if not prefix:
        raise ValueError("路径不能为空")
    cur = conn.execute("DELETE FROM workspace_map WHERE path_prefix=?", (prefix,))
    conn.commit()
    if cur.rowcount:
        append_event(conn, "workspace.unmapped",
                     {"citizen_id": "system", "evidence_level": "signed"},
                     {"kind": "workspace", "id": prefix}, {"removed": True})
    return bool(cur.rowcount)


def _workspace_path_from_text(raw) -> str:
    """把一个正文/路径片段裁成候选工作区路径（已归一化）；不可用返回空串。

    裁剪三件事：
      1. 去掉正文包裹字符与句末标点（反引号、引号、右括号、顿号/句号等）；
      2. 只在**后缀是已知文件扩展名**时退到父目录（`.../README.md` → 目录）；
      3. 归一化（小写/正斜杠/无尾斜杠）。
    最后挡掉盘符根（`c:`）、根路径 `/` 与系统目录：它们不是"某个项目的工作区"。
    """
    s = (raw or "").strip().rstrip("`'\"）)，,；;：:｜|]>.。")
    if not s:
        return ""
    s = normalize_workspace_path(s)
    if Path(s).suffix.lower() in _PATH_FILE_EXTS:
        s = normalize_workspace_path(str(Path(s).parent))
    if not s or s == "/" or re.fullmatch(r"[a-z]:", s):
        return ""
    if s.endswith("/.workbuddy"):
        return ""
    if any(s == p or s.startswith(p + "/") for p in _SEED_SKIP_PREFIXES):
        return ""
    if any(part in s for part in _SEED_SKIP_PARTS):
        return ""
    return s


def _covering_mapping(rows, path):
    """在已有映射里找**覆盖** path 的那一条（等值或祖先前缀，最长者优先）。"""
    best = None
    for row in rows:
        prefix = row["path_prefix"]
        if path == prefix or path.startswith(prefix + "/"):
            if best is None or len(prefix) > len(best["path_prefix"]):
                best = row
    return best


def workspace_seed_candidates(conn, root) -> dict:
    """从现有记忆反推"工作区 → 项目"候选。**只读：不写库、不改任何行。**

    数据来源（都是已经落库的事实，不靠猜）：
      1. `code_index.file_path` 本身是绝对路径 → 它（或其父目录）是候选；
      2. 记忆正文文件（`<root>/<file_path>`）里出现的绝对路径 → 候选。
    为什么必须有第 2 条：当前 `file_path` 全是 `projects/<项目>/<编码>.md`（记忆正文
    的存放位置），它并不携带"工作区"信息；真正的工作区路径只出现在正文叙述里。

    也正因为路径是"正文提到的"，它可能是**别人的**项目（实测：ANYI 的正文里提到
    `I:/app/codex`）——所以 seed 只打印建议，**不自动写入**。归属由人确认后走
    `fish workspace set`。

    置信规则：
      · 同一路径只出现在一个项目下 → suggest（可考虑 set）；
      · 同一路径出现在多个项目下 → conflict（**不建议**，人要先判断）；
      · 某路径是**另一个项目**路径的祖先 → conflict（共享父目录：整片归属会把
        别的项目也算进来）；
      · 同一路径的真子目录已被父路径建议覆盖 → 只留父路径（映射是前缀语义，
        列出子目录只会让建议表变长而不增加信息）；
      · 已被现有映射覆盖：归属一致 → mapped；不一致 → conflict（附 existing）。
      · 候选目录必须在本机**真实存在**：不存在的多半是笔误或文件引用，写进映射只会
        多一条永远匹配不上的幽灵记录。
      · 记忆根目录（`<root>` 及其下 `projects/`、`blobs/`、`_audit/`…）整片跳过：
        那是**数据仓库**，不是谁的工作区。正文里经常提到 `.../p0/data/index.sqlite`
        这类路径，把它们当候选只会污染建议表。
    """
    counts = {}

    def bump(path, project):
        if path and project:
            key = (path, project)
            counts[key] = counts.get(key, 0) + 1

    rows = conn.execute(
        "SELECT project, file_path FROM code_index "
        "WHERE file_path IS NOT NULL AND project IS NOT NULL AND project <> '' "
        "AND status='active'").fetchall()
    for project, rel in rows:
        p = Path(rel)
        if p.is_absolute():
            bump(_workspace_path_from_text(str(p if p.is_dir() else p.parent)), project)
            continue
        try:
            text = (Path(root) / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue                  # 正文文件缺失不算错：seed 只是建议器
        for m in _ABS_PATH_RE.finditer(text):
            bump(_workspace_path_from_text(m.group(0)), project)

    by_path = {}
    for (path, project), n in counts.items():
        by_path.setdefault(path, {})[project] = n

    # 只保留本机真实存在、且不在记忆根目录内部的工作区目录
    # （不存在的路径写进映射 = 幽灵记录；根目录内部是数据仓库，不是工作区）
    root_norm = normalize_workspace_path(root)
    paths = [p for p in sorted(by_path)
             if Path(p).is_dir()
             and not (root_norm and (p == root_norm or p.startswith(root_norm + "/")))]

    # 共享父目录：path 的某个子孙路径带有别的项目 → path 不能整片归属某个项目。
    ambiguous = set()
    for path in paths:
        mine = set(by_path[path])
        for other in paths:
            if other == path or not other.startswith(path + "/"):
                continue
            if set(by_path[other]) - mine:
                ambiguous.add(path)
                break

    existing = list_workspace_mappings(conn)
    suggest, conflicts, mapped = [], [], []
    for path in paths:
        projects = by_path[path]
        cover = _covering_mapping(existing, path)
        existing_project = cover["project"] if cover else None
        if len(projects) > 1:
            conflicts.append({
                "path": path, "project": None, "projects": sorted(projects),
                "count": sum(projects.values()),
                "existing": existing_project, "reason": "multi_project"})
            continue
        project = next(iter(projects))
        item = {"path": path, "project": project, "count": projects[project],
                "existing": existing_project}
        if path in ambiguous:
            conflicts.append({**item, "projects": [project], "reason": "shared_parent"})
            continue
        if cover:
            if existing_project == project:
                mapped.append(item)
            else:
                conflicts.append({**item, "reason": "mapping_mismatch"})
            continue
        suggest.append(item)

    # 降噪：父路径已被建议时，其子路径不再单列（前缀匹配本来就被父路径覆盖）。
    suggest = [it for it in suggest
               if not any(other["path"] != it["path"]
                          and it["path"].startswith(other["path"] + "/")
                          for other in suggest)]
    return {"suggest": suggest, "conflicts": conflicts, "mapped": mapped}


def current_open_stints(conn, citizen_id) -> list:
    """当前所有开档期（按 from_ts 升序）。没有返回空列表。

    2026-10-24 起"复数"才是真相：一个会话可以同时在多个项目上开档（AI 常跨项目干活）。
    排序键是 `(from_ts, id)` 而不是单独 from_ts：`now_utc()` 是**秒级**时间戳，同一秒内
    开两个档期完全可能（测试与批量脚本里常见），少了 id 兜底输出顺序就不稳定——
    而 status 的打印顺序、以及 current_open_stint 选"最近一个"的结果都会随之漂。
    """
    rows = conn.execute(
        "SELECT id,citizen_id,account_id,project,from_ts,set_by FROM project_stints "
        "WHERE citizen_id=? AND to_ts IS NULL ORDER BY from_ts ASC, id ASC",
        (citizen_id,)).fetchall()
    return [dict(zip(_STINT_READ_FIELDS, r)) for r in rows]


def current_open_stint(conn, citizen_id):
    """**兼容保留**：当前开档期里**最近开的**那一个（多项目时就是"主项目"）。没有返回 None。

    保留理由（不是历史包袱）：`session_status` 等调用方要一轮轮切到列表版；
    一次性把"档期语义变更"和"所有调用方改写"混在同一轮里，出问题时切不开是语义错
    还是调用方错。所以本轮把单数版保留为**列表版的最后一个**——它的返回值语义
    （"最近开的那条"）与旧实现一致，旧调用方的行为不变。

    注意它**不再是**"唯一开档期"：想知道全部，用 current_open_stints。
    """
    stints = current_open_stints(conn, citizen_id)
    return stints[-1] if stints else None


def close_open_stints(conn, citizen_id, *, reason, at=None) -> int:
    """关掉该公民**所有**开档期，返回关闭条数。`end_session` 用它。

    "关所有"是刻意的（与本轮"多项目并行"并不矛盾）：`session end` 的语义是**会话结束**，
    会话结束了，它在任何项目上的档期都该收尾——多项目并行只在会话活着时成立。
    """
    return conn.execute(
        "UPDATE project_stints SET to_ts=?, closed_reason=? "
        "WHERE citizen_id=? AND to_ts IS NULL",
        (at or now_utc(), reason, citizen_id)).rowcount


def open_project_stint(conn, citizen_id, project, *, account_id=None, set_by=None) -> dict:
    """把公民切到 project 档期。返回 {action, project, stint_id, from_ts, closed}。

    2026-10-24 改：**不再"先关后开"**。理由：多项目并行是合法状态，不是切换。
    新语义（三条，都在判据里钉住）：
      · 该项目**已有**开档期 → 复用（action='reused'，不写新行）
      · 该项目**没有**开档期 → 新建（action='opened'）
      · **不关任何其他项目的档期**（关自己的档期是 end_session 的事）
    为什么同项目要复用而不是重开：档期是一段时间，不是打卡记录。重开会在表里多切一条
    边界，"这个会话什么时候在哪个项目"被自己的重试切碎，追溯反而失真。

    `closed` 字段**保留但恒为 0**：语义是"本次调用关掉了几条档期"，本轮起本函数一条
    都不关。老调用方读它得到的仍是"本函数关了几条"，所以不用改。

    原来那句 `close_open_stints(..., reason="project_switch")` 已删（函数本身保留，
    end_session 还在用）。删它是本轮最核心的语义变更：判据 C 拿它当变异目标——
    把"先关后开"加回去，ANYI 的 to_ts 就不再是 NULL，判据当场红。
    """
    project = (project or "").strip()
    if not project:
        raise ValueError("project 不能为空（想只签发凭证不记档期，就别传 --project）")
    for st in current_open_stints(conn, citizen_id):
        if st["project"] == project:
            return {"action": "reused", "project": project, "stint_id": st["id"],
                    "from_ts": st["from_ts"], "closed": 0}
    from_ts = now_utc()
    cur = conn.execute(
        "INSERT INTO project_stints (citizen_id,account_id,project,from_ts,set_by) "
        "VALUES (?,?,?,?,?)", (citizen_id, account_id, project, from_ts, set_by))
    return {"action": "opened", "project": project, "stint_id": cur.lastrowid,
            "from_ts": from_ts, "closed": 0}


def _normalize_projects(projects=None, project=None) -> list:
    """把 projects（字符串 / 字符串列表）与兼容关键字 project 归一成**去重保序**的列表。

    为什么要归一：同一个事实有三个入口（`--project A --project B`、`projects=[...]`、
    旧关键字 `project=`），各写一套拼接逻辑必然漂。归一之后下面只有一条路要走。

    元素规则：
      · 必须是字符串；strip 后为空视为"没给"（跳过，不报错——空值多半来自计算出来的
        默认值，为它报错会把"本轮不开档期"这种合法情况变成立即失败）；
      · 非字符串（比如误传 dict）直接 TypeError：那是调用方写错，静默 str() 只会把错埋深；
      · 去重保序：同一个项目传两次不该开两条档期（第二次本就是 reused，但别让它先
        走一遍"没有→插入"的判断浪费一次往返）。
    """
    if projects is None:
        raw = []
    elif isinstance(projects, str):
        raw = [projects]
    elif isinstance(projects, (list, tuple)):
        raw = list(projects)
    else:
        raise TypeError(
            f"projects 必须是字符串或字符串列表，收到 {type(projects).__name__}")
    if project is not None:
        raw.append(project)              # 旧关键字与列表并用时按"追加"处理
    out = []
    for item in raw:
        if not isinstance(item, str):
            raise TypeError(
                f"projects 的元素必须是字符串，收到 {type(item).__name__}")
        name = item.strip()
        if name and name not in out:
            out.append(name)
    return out


def start_session(conn, *, projects=None, project=None, account=None, alias=None,
                  kind="visitor", role=None, environ=None, ttl_hours=None,
                  citizen=None) -> dict:
    """`session start` 的全部逻辑（CLI 只负责打印）。返回 dict，**含明文 token**。

    六步就是 18 §2 的六步，顺序不能换：
      环境事实 → 证据分级 → 找/建公民 → 轮换 + 签发凭证 → 开档期 → 事件

    2026-10-24：`projects` 支持**多个项目**（CLI 上 `--project` 可重复给）。逐个开档期，
    每个项目的档期互不干扰；一个都没给就只签发凭证、不记档期（合法用法）。
    兼容：旧关键字 `project=` 保留，等价于 `projects=[project]`——既有调用点与
    "单项目"这种最常见叫法都还走它，没必要为改名把调用面全改一遍。

    返回里 `stint` 是**第一条档期**（单项目时就是那一条，旧调用方不用改），
    `stints` 是完整列表（单项目也返回列表，口径统一，调用方不必写两套分支）。
    """
    project_list = _normalize_projects(projects, project)
    ident = build_session_identity(alias=alias, environ=environ)
    account_id = resolve_account_id(account=account, alias=ident["alias"], environ=environ)
    display_name = ident["alias"] or auto_session_display_name(
        ident["host"], ident["session_key"], role)
    # 2026-10-25 加（`EVAL-261025-05` 建议 3）：允许**显式指定公民**。
    # 场景：同一个 AI 在 MCP 侧已经登记成公民 A，但 CLI 这边的 host_session_key 不同，
    # 于是 `find_or_create` 会新建一个 B——两条通道的归属从此对不上，且无法合并。
    # `--citizen` 让调用方把这条会话**直接绑到已存在的公民**上，是那个问题的显式出口。
    forced = (citizen or "").strip() if isinstance(citizen, str) else citizen
    if forced:
        row = conn.execute(
            "SELECT citizen_id, display_name, account_id, archived_at FROM citizens "
            "WHERE citizen_id=?", (forced,)).fetchone()
        if not row:
            raise ValueError(f"指定的公民不存在：{forced}")
        if row[3]:
            raise ValueError(f"指定的公民已归档：{forced}（先 citizen-restore 再绑）")
        who = {"citizen_id": row[0], "created": False, "display_name": row[1],
               "account_id": row[2], "archived": False}
        if account_id and not row[2]:
            conn.execute("UPDATE citizens SET account_id=? WHERE citizen_id=?",
                         (account_id, row[0]))
            who["account_id"] = account_id
    else:
        who = find_or_create_session_citizen(
            conn, session_key=ident["session_key"], evidence=ident["evidence"],
            host=ident["host"], display_name=display_name, account_id=account_id,
            kind=kind, role=role)
    # 轮换：同一会话的新凭证生效时，旧的活凭证立即作废。
    # 为什么轮换而不是"留着"：重跑 start 的语义是"拿到一条新的、能用的凭证"，
    # 留着旧的意味着泄露面随时间只增不减；轮换后任一时刻一个会话只有一条活凭证。
    rotated = revoke_session_tokens(conn, session_key=ident["session_key"])
    issued = issue_session_token(
        conn, citizen_id=who["citizen_id"], session_key=ident["session_key"],
        evidence=ident["evidence"], channel=ident["channel"], ttl_hours=ttl_hours)
    stints = [
        open_project_stint(conn, who["citizen_id"], name, account_id=account_id,
                           set_by=f"cli:{ident['evidence']}")
        for name in project_list
    ]
    conn.commit()
    append_event(
        conn, "session.opened",
        {"citizen_id": who["citizen_id"], "evidence_level": ident["evidence"]},
        {"kind": "citizen", "id": who["citizen_id"]},
        # 事件里**只有 hash**，没有明文：不可变表会把任何写进去的东西永久留下。
        {"session_key": ident["session_key"], "evidence": ident["evidence"],
         "channel": ident["channel"], "host": ident["host"],
         "account_id": account_id,
         # project 保持单值口径（只开一个项目时它就是那个项目，多项目时为 None），
         # projects 记全量。两个都写：审计要能回答"这次 start 开了哪几个档期"，
         # 又不能给只看 project 的旧查询突然塞一个逗号串。
         "project": project_list[0] if len(project_list) == 1 else None,
         "projects": project_list,
         "token_hash": issued["token_hash"], "expires_at": issued["expires_at"],
         "citizen_created": who["created"], "rotated": rotated,
         "stint_actions": [{"project": s["project"], "action": s["action"]}
                           for s in stints]})
    return {"token": issued["token"], "token_hash": issued["token_hash"],
            "citizen_id": who["citizen_id"], "session_key": ident["session_key"],
            "evidence": ident["evidence"], "channel": ident["channel"],
            "host": ident["host"], "display_name": who["display_name"],
            "account_id": account_id, "created": who["created"],
            "rotated": rotated,
            "stint": stints[0] if stints else None, "stints": stints,
            "warning": ident["warning"],
            "issued_at": issued["issued_at"], "expires_at": issued["expires_at"],
            "project": project_list[0] if len(project_list) == 1 else None,
            "projects": project_list, "archived": who["archived"]}


def assert_agent_parent_allowed(parent) -> None:
    """子 agent 签发的守卫：父凭证不存在/已失效/证据不达标 → 抛 ValueError。

    **一跳限制的唯一实现**（18 §5）。父必须是 machine 级：
      machine        → 允许（父是机器事实，子继承它是有根的）
      inherited      → 拒绝（链式继承 = 无限漂白：父继承、孙再继承……最后无法追溯来源）
      self-reported  → 拒绝（**这条是本实现的加严**：父本来就是自报的，子再"继承"一次
                       会把自报值抬成 inherited 的可信度——证据升级比链式继承更糟）
    """
    if parent is None:
        raise ValueError("父 token 不存在：明文只在签发时输出一次，"
                         "丢了就重跑 `fish session start` 拿新的 machine token")
    if parent["revoked_at"]:
        raise ValueError(f"父 token 已吊销（{parent['revoked_at']}），不能为子 agent 背书")
    if parent["expired"]:
        raise ValueError(f"父 token 已过期（{parent['expires_at']}），不能为子 agent 背书")
    if parent["evidence"] != "machine":
        raise ValueError(
            f"不能链式继承：父 token 的 evidence={parent['evidence']}。"
            "只有 machine 级（能读宿主 env 的 CLI 签发）才能给子 agent 背书。"
            "→ 回主会话重跑 `fish session start` 拿一个新的 machine token。")


def issue_agent_token(conn, *, parent_token, role, ttl_hours=None) -> dict:
    """给子 agent 签 inherited 凭证（**只允许一跳**）。返回 dict，含明文 token。

    子 agent **不新建公民**（本实现的决定，记录在交付报告里）：
    `citizen_agents` 表还没建（P1-a 只建了 4 张表），现在给子 agent 造一个"独立公民"
    会把子 agent 的动作记成另一个人的历史——而 16 §2.1 要的是"共享父身份、各自可区分"。
    所以：citizen_id 复用父的，区分靠 session_key 的 `#<role>` 后缀 + 凭证上的
    evidence/channel。等 citizen_agents 建表（P1-d）再拆。
    """
    role = (role or "").strip()
    if not role:
        raise ValueError("--role 不能为空：子 agent 的身份就是「它为哪个角色干活」")
    parent = lookup_session_token(conn, parent_token)
    assert_agent_parent_allowed(parent)
    child_key = f"{parent['session_key']}#{role}"
    issued = issue_session_token(
        conn, citizen_id=parent["citizen_id"], session_key=child_key,
        evidence="inherited", channel="parent", parent_hash=parent["token_hash"],
        ttl_hours=ttl_hours)
    # 子凭证不能比父凭证活得久：父失效后它就没有根了，留着等于一条无源凭证。
    if parent["expires_at"] and issued["expires_at"] and \
            issued["expires_at"] > parent["expires_at"]:
        conn.execute("UPDATE session_tokens SET expires_at=? WHERE token_hash=?",
                     (parent["expires_at"], issued["token_hash"]))
        issued["expires_at"] = parent["expires_at"]
    conn.commit()
    append_event(
        conn, "session.agent_issued",
        {"citizen_id": parent["citizen_id"], "evidence_level": "inherited"},
        {"kind": "citizen", "id": parent["citizen_id"]},
        {"parent_token_hash": parent["token_hash"], "session_key": child_key,
         "role": role, "evidence": "inherited", "channel": "parent",
         "token_hash": issued["token_hash"], "expires_at": issued["expires_at"]})
    return {"token": issued["token"], "token_hash": issued["token_hash"],
            "citizen_id": parent["citizen_id"], "session_key": child_key,
            "role": role, "evidence": "inherited", "channel": "parent",
            "parent_token_hash": parent["token_hash"], "issued_at": issued["issued_at"],
            "expires_at": issued["expires_at"]}


def end_session(conn, *, token=None, environ=None, reason="session_end") -> dict:
    """`session end`：吊销凭证 + 关档期 + 刷新 last_seen_at。**幂等**。

    两种入口都必须支持：
      · `--token`       校验这条凭证，吊销它 + 它派生出的子凭证
      · 不带 `--token`  用 env 定位当前会话，把该会话所有活凭证一起吊销
    幂等的含义：重复调不报错、不产生新副作用（凭证已是吊销态就只补齐档期与活跃时间）。
    """
    children = 0
    if token:
        info = lookup_session_token(conn, token)
        if info is None:
            raise ValueError("token 不存在：明文只在签发时输出一次，"
                             "丢了就重跑 `fish session start`（公民不变，凭证换新）")
        citizen_id, session_key = info["citizen_id"], info["session_key"]
        already = bool(info["revoked_at"])
        revoked = revoke_session_tokens(conn, token_hash=info["token_hash"])
        # 父会话结束，派生出来的子凭证不该继续有效——它已经失去来源。
        children = revoke_session_tokens(conn, parent_hash=info["token_hash"])
    else:
        ident = build_session_identity(environ=environ)
        found = find_session_citizen(conn, ident["session_key"])
        if found is None:
            raise ValueError(
                f"当前会话没有登记过公民（session_key={ident['session_key']}）："
                "先跑 `fish session start`")
        citizen_id, session_key = found["citizen_id"], ident["session_key"]
        revoked = revoke_session_tokens(conn, session_key=session_key)
        # 一条活的都没吊销 = 这个会话早就收过工了。用"实际吊销数"判定，而不是靠另一个标志位，
        # 这样"重复 end"在两条入口（--token / env）上的语义才是同一个。
        already = revoked == 0
    stints = close_open_stints(conn, citizen_id, reason=reason)
    touch_citizen(conn, citizen_id)
    conn.commit()
    append_event(
        conn, "session.closed",
        {"citizen_id": citizen_id, "evidence_level": "machine"},
        {"kind": "citizen", "id": citizen_id},
        {"session_key": session_key, "revoked": revoked, "children_revoked": children,
         "stints_closed": stints, "reason": reason, "already_revoked": already})
    return {"citizen_id": citizen_id, "session_key": session_key,
            "revoked": revoked, "children_revoked": children,
            "stints_closed": stints, "already": already}


def session_status(conn, *, token=None, environ=None) -> dict:
    """`session status` 的事实层（CLI 只负责排版）。

    带 token：以**凭证**为准（它是这件事的真相），失效也照样报出原因。
    不带 token：用 env 定位（回答"我这个进程现在算谁"）。

    2026-10-24：档期从单值改**列表**（`stints`）。`stint` 保留为"最近开的那一条"，
    语义与旧版一致，所以只读 `stint` 的调用方不用改；要全部就取 `stints`。
    """
    if token:
        info = lookup_session_token(conn, token)
        if info is None:
            raise ValueError("token 不存在：明文只在签发时输出一次，"
                             "丢了就重跑 `fish session start`（公民不变，凭证换新）")
        citizen = load_citizen(conn, info["citizen_id"])
        stints = current_open_stints(conn, info["citizen_id"])
        return {"source": "token", "token": info, "citizen": citizen,
                "identity": None, "stints": stints,
                "stint": stints[-1] if stints else None}
    ident = build_session_identity(environ=environ)
    found = find_session_citizen(conn, ident["session_key"])
    citizen = load_citizen(conn, found["citizen_id"]) if found else None
    stints = current_open_stints(conn, citizen["citizen_id"]) if citizen else []
    return {"source": "env", "token": None, "citizen": citizen, "identity": ident,
            "stints": stints, "stint": stints[-1] if stints else None}


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


# ------------------------------------------------------------------ 邻近

def _row_dict(r):
    keys = ("code", "title", "project", "mem_type", "tags", "occurred_at", "status", "dormant")
    return dict(zip(keys, r))


def _day(s):
    d = parse_utc(s)
    return d.strftime("%y%m%d") if d else ""


def score_candidate(base, cand) -> float:
    """mixed 策略的打分。权重写死在代码里，可解释、可测试。"""
    s = 0.0
    if cand["project"] == base["project"]:
        s += 10
    bd, cd = _day(base["occurred_at"]), _day(cand["occurred_at"])
    if bd and cd:
        if bd == cd:
            s += 5
        else:
            try:
                diff = abs((datetime.datetime.strptime(cd, "%y%m%d") -
                            datetime.datetime.strptime(bd, "%y%m%d")).days)
            except ValueError:
                diff = 999
            if diff <= 7:
                s += 2
            s -= 2 / (1 + diff)
    if cand["mem_type"] == base["mem_type"]:
        s += 1
    bt = set(json.loads(base["tags"] or "[]"))
    ct = set(json.loads(cand["tags"] or "[]"))
    s += 3 * len(bt & ct)
    return s


def neighbors(conn, code, n=3, strategy="mixed", cross_project=False,
              include_dormant=False):
    row = conn.execute(
        "SELECT code,title,project,mem_type,tags,occurred_at,status,dormant "
        "FROM code_index WHERE code=?", (code,)
    ).fetchone()
    if not row:
        return None
    base = _row_dict(row)

    sql = "SELECT code,title,project,mem_type,tags,occurred_at,status,dormant FROM code_index WHERE code<>?"
    params = [code]
    if not cross_project:
        sql += " AND project=?"
        params.append(base["project"])
    if not include_dormant:
        sql += " AND dormant=0"
    cands = [_row_dict(r) for r in conn.execute(sql, params).fetchall()]

    if strategy == "code":
        before = sorted([c for c in cands if c["code"] < code], key=lambda x: x["code"], reverse=True)[:n]
        after = sorted([c for c in cands if c["code"] > code], key=lambda x: x["code"])[:n]
        return {"strategy": "code", "prev": before, "next": after}

    if strategy == "time":
        # 复合键 (occurred_at, code)：同一秒内按编码决胜负，保证排序稳定、不重叠
        def key(c):
            return (c["occurred_at"] or "", c["code"])

        bk = key(base)
        before = sorted([c for c in cands if key(c) < bk], key=key, reverse=True)[:n]
        after = sorted([c for c in cands if key(c) > bk], key=key)[:n]
        return {"strategy": "time", "prev": before, "next": after}

    if strategy == "tag":
        bt = set(json.loads(base["tags"] or "[]"))
        scored = []
        for c in cands:
            shared = bt & set(json.loads(c["tags"] or "[]"))
            if shared:
                scored.append((len(shared), c))
        scored.sort(key=lambda x: (-x[0], x[1]["code"]))
        return {"strategy": "tag", "hits": [c for _, c in scored[:n]]}

    if strategy == "type":
        hits = [c for c in cands if c["mem_type"] == base["mem_type"]]
        hits.sort(key=lambda x: x["code"], reverse=True)
        return {"strategy": "type", "hits": hits[:n]}

    # mixed
    scored = sorted(((score_candidate(base, c), c) for c in cands),
                    key=lambda x: (-x[0], x[1]["code"]))
    return {"strategy": "mixed", "hits": [{"score": round(s, 2), **c} for s, c in scored[:n]]}


# ------------------------------------------------------------------ 命令

def cmd_init(args):
    root = Path(args.root)
    conn = connect(root)
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.commit()
    (root / "projects").mkdir(exist_ok=True)
    (root / "citizens").mkdir(exist_ok=True)
    append_event(conn, "config.changed",
                 {"citizen_id": "system", "evidence_level": "signed"},
                 {"kind": "config", "id": "init"},
                 {"key": "schema_version", "new_value": "p1a"})
    print(f"已初始化：{root}")
    conn.close()


def cmd_project_add(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    # EVAL-261025-17：与 MCP 面同口径——**先查库、再校验字符**（2026-10-25 修）。
    # 已存在的存量前缀（可能含 `-`）必须先报"已存在"，不能被字符规则误报成"非法"。
    row = conn.execute("SELECT namespace FROM code_registry WHERE prefix=?",
                       (args.prefix,)).fetchone()
    if row:
        print(f"项目已存在：{args.prefix} → {row[0]}")
        conn.close()
        return
    if "-" in args.prefix:
        print("错误：prefix 不能含 '-'（编码用它分隔段，如 ANYI-261022-01；"
              "含 '-' 的存量前缀不适用本命令）", file=sys.stderr)
        conn.close()
        return
    if len(args.prefix) > 20:
        print("错误：prefix 太长（≤20 字符）", file=sys.stderr)
        conn.close()
        return
    # 写前查重（2026-10-25 · 阶段 C）：只提示不拦——精确重名上面已经先挡掉。
    # 放在 INSERT 之前算，才是"写前"；提示走 stderr，stdout 保持"一行结果"的契约。
    hints = find_similar_projects(conn, args.prefix, namespace=args.name)
    try:
        conn.execute(
            "INSERT INTO code_registry (prefix, namespace, last_date, last_seq, status) VALUES (?,?,?,?,?)",
            (args.prefix, args.name, "", 0, "active"))
        conn.commit()
        append_event(conn, "project.opened",
                     {"citizen_id": "system", "evidence_level": "signed"},
                     {"kind": "project", "id": args.prefix},
                     {"prefix": args.prefix, "name": args.name})
        # 2026-10-25（看板 C 批）：**新项目一开自动出看板**（`09` §8.3 C1 的判据）。
        # 为什么放在 cmd_project_add 而不是 append_event 里：事件层不该知道"看板"这个概念——
        # 事件是不可变事实流，看板是组织层的投影层。让事件层依赖投影层，会污染 ADR-001。
        # 为什么失败不阻断：出看板是"顺手做的事"，不该成为开项目的前提。
        try:
            _b = board_ensure(conn, "project", args.prefix)
            board_render(conn, Path(args.root), _b)
        except Exception as _e:      # noqa: BLE001 —— 见上：不阻断开项目
            print(f"（看板自动创建失败，可稍后 `fish board render --project "
                  f"{args.prefix}` 重建：{_e}）", file=sys.stderr)
        print(f"项目已开立：{args.prefix} ({args.name})")
        hint = format_dup_hint(project_hints=hints)
        if hint:
            print(hint, file=sys.stderr)
    except sqlite3.IntegrityError:
        print(f"项目已存在：{args.prefix}")
    conn.close()


# ------------------------------------------------------ 项目前缀生命周期（retire / restore）
#
# 背景（2026-10-24）：`code_registry` 里出现了一个空壳前缀「测试项目」（ns 同为
# 「测试项目」、last_date 空、last_seq 0、0 条记忆），与既有的 `TESTP` 同义重复。
# 前缀一边多一个，`fish workspace set` 与注入包就多一处能选错的地方。
#
# 纪律（ADR-003）：**前缀不可回收**——编码一经分配就永久指向一条记忆，删前缀等于让
# 历史编码失去解释。所以这里刻意**不提供删除**，只提供**退役**：把
# `code_registry.status` 从 active 改成 retired（`next_code` 早就在这一步拒绝分配新编码），
# 行与历史全部保留。
#
# 这一族命令的默认动作是**打印计划**，`--apply` 才落库。理由不是保守：改一个前缀的
# 状态会影响**所有后续编码分配**，它必须先让人看见"会改什么、不会改什么"再说同意。

# 项目三态（2026-10-25 · 阶段 D）：
#   active  —— 在用：可写入、可分配新编码
#   closed  —— **已闭合**：这件事做完了。不再接收新记忆，但**不删任何东西**，
#              且可 `reopen` 回来。用户拍板原话："close 其实从代码设计上是可逆的，
#              如果要写，那就打开"。闭合 ≠ 删除（决策 16）。
#   retired —— 名字废弃：停止分配新编码，前缀**永久不可再分配**（ADR-003）。
#
# 两态变三态的理由：`retired` 回答"这个名字还用不用"，`closed` 回答"这件事做完了没有"。
# 合成一个值会让"项目完成"看起来像"项目废弃"——那正是 T10 说的"系统缺已完成语义"。
PROJECT_STATUSES = ("active", "closed", "retired")

# 事件类型：retire / reopen 各一个（与 `project.opened` 同一族命名）。
_EVENT_BY_PROJECT_STATUS = {"retired": "project.retired",
                            "closed": "project.closed",
                            # close / retire 回到 active 都用同一个事件名：
                            # "重新可写"是同一件事，不该因为来路不同就分两种事件。
                            "active": "project.reopened"}

# CLI 动作 → 目标状态。`restore` 与 `reopen` 都回到 active：对使用者是两句话，
# 对状态机是一件事——所以映射到同一个值，事件也共用 `project.reopened`。
_PROJECT_ACTION_TARGET = {"retire": "retired", "restore": "active",
                          "close": "closed", "reopen": "active"}


def plan_project_status_change(conn, prefix, target, *, force=False) -> dict:
    """算一遍"把 prefix 改成 target 状态会发生什么"。**只读：不写库、不 begin。**

    返回 dict：
      found / already  前缀在不在、是不是已经是目标状态（幂等分支）
      current          现在的 status（可能是人手改的未知值，不猜）
      counts           名下记忆数 / workspace_map 引用数——决定"退役是否安全"的证据
      blockers         拦住执行的理由（人话）；空 = 可以执行
      changes          会做的每一件事（人话，含"**不会**做什么"）

    **只读是判据**（test_onboarding 判据 F）：连跑一百遍，库必须一行不变。
    所以这里没有 commit、没有 begin，只有 SELECT。
    """
    if target not in PROJECT_STATUSES:
        raise ValueError(f"目标状态只能是 {PROJECT_STATUSES}，收到 {target!r}")
    name = (prefix or "").strip()
    if not name:
        raise ValueError("项目前缀不能为空")
    row = conn.execute(
        "SELECT prefix, namespace, last_date, last_seq, status FROM code_registry "
        "WHERE prefix=?", (name,)).fetchone()
    if not row:
        return {"found": False, "already": False, "prefix": name, "target": target,
                "current": None, "namespace": None, "last_date": None, "last_seq": None,
                "counts": {"memories": 0, "mappings": 0},
                "blockers": [f"code_registry 里没有前缀 {name}"], "changes": []}
    _prefix, namespace, last_date, last_seq, current = row
    memories = conn.execute("SELECT COUNT(*) FROM code_index WHERE project=?",
                            (name,)).fetchone()[0]
    mappings = conn.execute("SELECT COUNT(*) FROM workspace_map WHERE project=?",
                            (name,)).fetchone()[0]
    blockers, changes = [], []
    plan = {"found": True, "already": current == target, "prefix": name, "target": target,
            "namespace": namespace, "last_date": last_date, "last_seq": last_seq,
            "current": current,
            "counts": {"memories": memories, "mappings": mappings},
            "blockers": blockers, "changes": changes}
    if plan["already"]:
        return plan
    if current not in PROJECT_STATUSES:
        # 不猜一个不认识的旧状态能不能动：可能是人手改的，交给执行者判断。
        blockers.append(f"当前状态 {current!r} 不在已知状态集 {PROJECT_STATUSES} 里")
    if target == "retired":
        changes.append(f"code_registry.status：{current} → retired（只改这一个字段）")
        changes.append("**不删行、不回收前缀**：历史编码仍可 resolve_code / read_body（ADR-003）")
        changes.append("last_date / last_seq 原样保留：恢复后序数接着走，旧编码不复用")
        if memories:
            changes.append(f"名下 {memories} 条记忆**不动**：退役只停分配，不搬归属")
            if not force:
                blockers.append(
                    f"名下还有 {memories} 条记忆。退役只停止分配新编码、不动这些记忆；"
                    "确认接受请加 --force（或先把记忆迁走）")
        if mappings:
            changes.append(f"workspace_map 里有 {mappings} 条映射指向它（映射本身不动）："
                           "退役后这些目录解析到的项目不再分配新编码")
    elif target == "closed":
        changes.append(f"code_registry.status：{current} → closed（这件事做完了）")
        changes.append("**不删行、不动记忆、不回收前缀**：只是不再接收新记忆"
                       "（决策 16：闭合 ≠ 删除）")
        changes.append("要再往里写，先 `fish project reopen <前缀>`"
                       "（用户拍板：闭合是可逆的状态，不是锁死）")
        if memories:
            changes.append(f"名下 {memories} 条记忆**照常可查可读**（闭合不冻结检索）")
        if mappings:
            changes.append(f"workspace_map 里有 {mappings} 条映射指向它"
                           "（映射不动，但那些目录解析出的项目在 reopen 之前不接受新记忆）")
    else:
        changes.append(f"code_registry.status：{current} → active（恢复分配）")
        changes.append("next_code 接着 last_date / last_seq 往下发号（旧编码不复用）")
    return plan


def apply_project_status(conn, prefix, target, *, reason=None, actor=None) -> dict:
    """真正落库（**只有 CLI 的 `--apply` 会走到这里**）。

    一个事务两件事：改字段 + 写事件。顺序有讲究——先 UPDATE（尚未提交）再调
    `append_event`，它的 commit 会把这条 UPDATE 一起提交；中途抛错则由调用方
    rollback，不会出现"状态改了但没留痕"的半截事实。
    """
    if target not in PROJECT_STATUSES:
        raise ValueError(f"目标状态只能是 {PROJECT_STATUSES}，收到 {target!r}")
    name = (prefix or "").strip()
    try:
        cur = conn.execute("UPDATE code_registry SET status=? WHERE prefix=?",
                           (target, name))
        if not cur.rowcount:
            raise ValueError(f"code_registry 里没有前缀 {name}")
        # 2026-10-25（用户要求 C）：**项目闭合 → 该项目所有 open 看板一起归档**。
        #
        # 为什么放在这里而不是 MCP 的 `_project_state_change`：
        # CLI 的 `fish project close --apply` 与 MCP 的 `close_project` **都走这个函数**。
        # 放在上层就一定会有一条路径漏掉——那是"两层各写一份"的老毛病。
        #
        # 为什么连看板一起关：看板按日切、**没有别的东西会关旧板**，
        # 于是 `boards` 里 open 板会日复一日累积。项目都闭合了，它的板还 open 着，
        # 语义上说不通。
        boards_closed = 0
        if target == "closed":
            boards_closed = conn.execute(
                "UPDATE boards SET status='closed', closed_at=? "
                "WHERE scope='project' AND scope_key=? AND status='open'",
                (now_utc(), name)).rowcount
        append_event(conn, _EVENT_BY_PROJECT_STATUS[target],
                     # actor：CLI 走 "system"（signed），MCP 走真实 citizen_id。
                     # 两种来源的证据级不同，不该混成一个值。
                     {"citizen_id": actor or "system",
                      "evidence_level": "signed" if not actor else "self-reported"},
                     {"kind": "project", "id": name},
                     {"prefix": name, "status": target,
                      "reason": (reason or "").strip() or "manual",
                      "boards_closed": boards_closed})
    except Exception:
        conn.rollback()
        raise
    return {"changed": True, "prefix": name, "status": target,
            "boards_closed": boards_closed}


def _render_project_plan(plan, *, apply, reason) -> int:
    """把计划打印出来；返回退出码（0 = 可以继续，2 = 拒绝执行）。"""
    if not plan["found"]:
        print(f"错误：{plan['blockers'][0]}")
        print("（前缀用 `fish project-add <前缀>` 开立；这里不会凭空建一个）")
        return 2
    verb = {"retired": "退役：停止分配新编码",
            "closed": "闭合：这件事做完了（不再接收新记忆，可 reopen）",
            "active": "恢复：改回 active（可写、可分配新编码）"}.get(
                plan["target"], plan["target"])
    print(f"项目前缀：{plan['prefix']}（{plan['namespace'] or '-'}）")
    print(f"当前状态：{plan['current']}  →  目标状态：{plan['target']}（{verb}）")
    print(f"名下记忆：{plan['counts']['memories']} 条"
          f"；workspace_map 引用：{plan['counts']['mappings']} 条")
    if plan["already"]:
        print(f"\n已经是 {plan['target']}：什么都不用做（幂等）。")
        return 0
    print("\n会做的事：")
    for line in plan["changes"]:
        print(f"  · {line}")
    if plan["blockers"]:
        print("\n现在不能执行：")
        for line in plan["blockers"]:
            print(f"  ✗ {line}")
    if not apply:
        print("\n（本次只是打印计划：库一个字节都没改。确认后加 --apply 才落库）")
        return 0
    if plan["blockers"]:
        print("\n已拒绝执行（未写库）。")
        return 2
    if not (reason or "").strip():
        print("\n⚠ 没给 --reason：事件里会记 reason=manual（少一条可追溯的说明，但不拦截）")
    return 0


def cmd_project(args):
    """`fish project retire|restore <prefix>` —— 前缀生命周期（默认只打印计划）。

    `project-add` 仍是顶层扁平命令（不改它的名字：那是已有脚本与文档在用的入口）；
    新增的这族只负责"状态"，两者不重叠。
    """
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    target = _PROJECT_ACTION_TARGET[args.project_action]
    try:
        plan = plan_project_status_change(conn, args.prefix, target,
                                          force=getattr(args, "force", False))
        code = _render_project_plan(plan, apply=args.apply, reason=args.reason)
        if code:
            sys.exit(code)
        if not args.apply or plan["already"]:
            return
        res = apply_project_status(conn, plan["prefix"], target, reason=args.reason)
        print(f"\n已执行：{res['prefix']} → {res['status']}"
              f"（事件 {_EVENT_BY_PROJECT_STATUS[target]}，可追溯）")
    finally:
        conn.close()


def cmd_alias(args):
    """`fish alias link|unlink|list` —— 名字别名（**单向**：from 就是 to）。

    为什么只给人一条命令、不给 AI 一个工具（决策 13/17 的落地）：
      别名改变的是**检索语义**——一旦建错，`recall` 的结果会静默变化（多出或少掉
      一批记忆），而且不会报错。按"AI 提议、人采纳"的口径，这条通道留给使用者。
      MCP 面刻意不提供：那是"可以远程改检索行为"的权限，不该随身份一起发出去。
    """
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    try:
        action = args.alias_action
        if action == "link":
            res = link_alias(conn, args.kind, args.from_name, args.to_name,
                             reason=args.reason or "")
            print(f"已建立别名：{res['from']} → {res['to']}（kind={res['kind']}）")
            print("（只影响检索：project 过滤会展开别名；**写入仍按原名归属**）")
        elif action == "unlink":
            ok = unlink_alias(conn, args.kind, args.from_name, reason=args.reason or "")
            print("已撤销别名。" if ok else "没有这条别名（幂等：什么都没做）")
        else:
            rows = list(conn.execute(
                "SELECT kind,from_name,to_name,created_at,reason FROM name_aliases "
                "ORDER BY kind,from_name"))
            if not rows:
                print("（还没有任何别名）")
            for kind, f, t, at, why in rows:
                print(f"{kind}\t{f} → {t}\t{at}\t{why or ''}")
            # 顺手把"哪些名字解析成同一个"报出来——人不用自己拼图。
            for kind in _ALIAS_KINDS:
                groups = {}
                for (f,) in conn.execute(
                        "SELECT from_name FROM name_aliases WHERE kind=?", (kind,)):
                    canon = resolve_name(conn, kind, f)["canonical"]
                    groups.setdefault(canon, []).append(f)
                for canon, fs in groups.items():
                    print(f"[{kind}] {canon} ← {', '.join(sorted(fs))}")
    except ValueError as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(2)
    finally:
        conn.close()


def cmd_remember(args):
    root = Path(args.root)
    conn = connect(root)
    conn.executescript(SCHEMA)
    migrate(conn)
    # 参数校验失败时不要吐 traceback：报错里已写清原因和下一步，直接读即可。
    # （getattr 兼容测试里手工构造的 args 命名空间）
    try:
        owner = resolve_owner_arg(conn, args.owner)
        occurred_at = normalize_occurred_at(getattr(args, "occurred_at", None))
    except ValueError as e:
        conn.close()
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(2)
    hints = []
    code, rel = record_memory(
        conn, root,
        project=args.project, title=args.title, body=args.body or "",
        mem_type=args.type, tags=args.tags or [], visibility=args.visibility,
        owner=owner, session_actor=args.session_actor,
        attachments=args.attach or [], occurred_at=occurred_at, cause=args.cause,
        hints=hints)
    try:
        print(f"{code}\t{args.title}\t{rel}")
    except UnicodeEncodeError:
        print(code)
    # 查重提示走 **stderr**：stdout 是 `编码\t标题\t路径` 的契约面（脚本会按 tab 切），
    # 往里塞散文就会污染解析。诊断句走 stderr 是 fish 的既有纪律（同 session start）。
    hint = format_dup_hint(hints)
    if hint:
        print(hint, file=sys.stderr)
    conn.close()


def cmd_recall(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    ensure_fts(conn, Path(args.root))
    rows, _ = search_memories(
        conn, query=args.query,
        code=getattr(args, "code", None),
        project=getattr(args, "project", None),
        tags=getattr(args, "tags", None),
        scope=getattr(args, "scope", "all"),
        limit=args.limit)
    if not rows:
        cand = recent_fallback(conn, project=getattr(args, "project", None),
                               tags=getattr(args, "tags", None))
        print(f"未找到「{args.query}」。最近的条目：")
        for c, t in cand:
            print(f"  {c}\t{t}")
        conn.close()
        return
    # 记录访问：刷新衰减，被想起即唤醒（并写 memory.accessed 事件，见 07 D-12）
    record_access(conn, [r[0] for r in rows], "recall")
    for code, title, proj, mtype, status, fp, dormant, src in rows:
        flag = " (休眠)" if dormant else ""
        print(f"{code}\t[{mtype}/{status}]{flag}\t{title}\t{fp}\t匹配:{src}")
    conn.close()


def cmd_neighbors(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    strategy = args.strategy or get_config(conn, "neighbors.strategy")
    n = args.n if args.n is not None else get_config(conn, "neighbors.n")
    cross = args.cross_project if args.cross_project is not None else get_config(conn, "neighbors.cross_project")
    inc_dormant = args.include_dormant if args.include_dormant is not None else get_config(conn, "neighbors.include_dormant")

    res = neighbors(conn, args.code, n=n, strategy=strategy,
                    cross_project=cross, include_dormant=inc_dormant)
    if res is None:
        print(f"编码不存在：{args.code}")
        conn.close()
        return
    print(f"策略={res['strategy']} n={n} 跨项目={cross} 含休眠={inc_dormant}")
    if "prev" in res:
        print("前：")
        for c in reversed(res["prev"]):
            print(f"  {c['code']}\t{c['title']}")
        print(f"← {args.code}")
        print("后：")
        for c in res["next"]:
            print(f"  {c['code']}\t{c['title']}")
    else:
        if not res["hits"]:
            print("  （没有符合条件的邻近项：可能该策略下无同类记忆，"
                  "或需要 --cross-project / --include-dormant）")
        for c in res["hits"]:
            sc = f"[{c['score']}] " if "score" in c else ""
            print(f"  {sc}{c['code']}\t{c['title']}")
    conn.close()


def cmd_list(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    rows = conn.execute(
        "SELECT code,mem_type,status,dormant,title FROM code_index ORDER BY code").fetchall()
    print(f"共 {len(rows)} 条")
    for code, mt, st, dm, title in rows:
        print(f"{code}\t{mt}\t{st}\t{'休眠' if dm else '活跃'}\t{title}")
    conn.close()


def cmd_forgetting(args):
    """列出衰减情况；--apply 时把 dormant 落库并写事件。"""
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    rows = conn.execute(
        "SELECT code,mem_type,last_accessed_at,recorded_at,dormant FROM code_index").fetchall()
    cfg = load_decay_cfg(conn)
    to_dormant, to_archive, stats = [], [], {}
    for code, mt, la, ra, dm in rows:
        decay, state = compute_decay(mt, la, ra, cfg)
        stats[state] = stats.get(state, 0) + 1
        if state == "dormant" and not dm:
            to_dormant.append(code)
        elif state == "archived":
            to_archive.append(code)
    print(f"记忆 {len(rows)} 条 ｜ 活跃 {stats.get('active', 0)} ｜ "
          f"休眠 {stats.get('dormant', 0)} ｜ 归档 {stats.get('archived', 0)}")
    if to_dormant:
        print(f"应转休眠 {len(to_dormant)} 条：{', '.join(to_dormant[:10])}"
              + (" ..." if len(to_dormant) > 10 else ""))
    if to_archive:
        print(f"已归档 {len(to_archive)} 条（仍可按编码召回）")
    if args.apply and to_dormant:
        conn.executemany("UPDATE code_index SET dormant=1 WHERE code=?", [(c,) for c in to_dormant])
        conn.commit()
        for c in to_dormant:
            append_event(conn, "memory.dormant",
                         {"citizen_id": "system", "evidence_level": "signed"},
                         {"kind": "memory", "id": c},
                         {"code": c, "reason": "decay_below_threshold"})
        print(f"已标记休眠 {len(to_dormant)} 条，并写入 memory.dormant 事件")
    conn.close()


def cmd_citizen_add(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    if args.kind not in KINDS:
        print(f"kind 只能是 {KINDS}")
        conn.close()
        return
    cid = ulid()
    conn.execute("INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at) "
                 "VALUES (?,?,?,?,?,?,?)",
                 (cid, args.name, args.kind, args.role, args.host, "active", now_utc()))
    conn.commit()
    append_event(conn, "citizen.registered",
                 {"citizen_id": cid, "evidence_level": "machine"},
                 {"kind": "citizen", "id": cid},
                 {"display_name": args.name, "kind": args.kind,
                  "role": args.role, "host": args.host})
    print(f"{cid}\n{args.name} / {args.kind}")
    conn.close()


def cmd_whoami(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)

    owner_id = resolve_owner(conn)
    if args.citizen:
        actor = conn.execute(
            "SELECT citizen_id,display_name,kind,role,host FROM citizens WHERE citizen_id=?",
            (args.citizen,)).fetchone()
        if not actor:
            print(f"公民不存在：{args.citizen}")
            conn.close()
            return
    else:
        actor = resolve_actor(conn)

    if actor:
        print(f"登记身份：{actor[1]}（公民 {actor[0]}，形态 {actor[2]}，"
              f"角色 {actor[3] or '-'}）")
        print("说明：这是你在 fish 的账户名（宿主级），与你的宿主身份不冲突。")
    else:
        an = get_config(conn, "actor_name")
        if an:
            print(f"你：未登记访客（actor_name={an} 未找到，请 register）")
        else:
            print("你：未登记访客（未配置 actor_name；设 FISH_ACTOR 或调用 register）")

    if owner_id:
        ro = conn.execute("SELECT display_name,kind FROM citizens WHERE citizen_id=?",
                          (owner_id,)).fetchone()
        if ro:
            print(f"主人：{ro[0]}（公民 {owner_id}，形态 {ro[1]}）")
    else:
        print("主人：未设置（fish config set owner_citizen <id>）")

    env = {k: v for k, v in
           ((k, os.environ.get(k)) for k in
            ("FISH_ACTOR", "DSH_SESSION_ID", "CODEBUDDY_SESSION_ID", "CLAUDE_SESSION_ID"))
           if v}
    if env:
        print(f"机器事实：{env}")
    conn.close()


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


# ------------------------------------------------------ L1「当前项目」的四档解析
# 2026-10-24（R1 根治）：项目归属必须有证据，按下面顺序取**第一个有证据的**档：
#   ① explicit      调用方显式给了 project 参数（最高优先：调用方知道自己在问什么）
#   ② stint         该公民当前开着的项目档期（`fish session start --project` 写的会话事实）
#   ③ workspace_map 调用方给了 cwd，且命中一条路径映射（环境事实）
#   ④ inferred      **兜底**：都没有 → 退回"最近更新"，但**必须在输出里标注这是推断**
# 第 ④ 档保留而不是删掉，是为了不让老调用方（与老判据）失去 L1 的项目行；但措辞必须
# 让人一眼看出"这是猜的"——猜错项目比不显示项目危险得多（EVAL-261024-02 R1 的定性）。
# 连"最近更新"都没有（空库）时，第 ④ 档也只能说"未声明"，不许编一个项目名。

_INFERRED_PROJECT_NOTE = "据最近更新推断 —— 建议用 `fish session start --project` 声明"
_UNDECLARED_PROJECT_LINE = (
    "当前项目：未声明（未传 project、无开档期、workspace_map 无匹配；系统不替你猜）")
_PROJECT_SOURCES = ("explicit", "stint", "workspace_map", "inferred", "undeclared")

# 兜底项目（2026-10-25 · T1）：cwd 解析不出具体项目时，会话落在哪个项目上。
#
# 为什么需要它：决策 12 定的是「生活 / 工作 / 未分类 都是**真实项目**」——用户可能
# 从不提项目名，那些对话必须有个容器接住，否则记忆无处落脚。实证代价就摆在这儿：
# `project_stints` 在生产库**恒为 0**，因为 cwd 未匹配 → 干脆不开档期，于是
# 「这个会话什么时候在哪个项目干活」这条最基础的事实，系统一条都没记过。
#
# ⚠️ 名字是**数据**不是代码常量：只有它在 code_registry 里真实登记过才用；没登记就
# 维持"不开档期"的老行为——代码里凭空造一个项目，等于在库里写一条假事实。
FALLBACK_PROJECT_NAME = "未分类"


def fallback_project(conn):
    """兜底项目前缀；库里没登记过就返回 None（只读，不建项目）。"""
    row = conn.execute(
        "SELECT prefix FROM code_registry WHERE prefix=? AND status='active'",
        (FALLBACK_PROJECT_NAME,)).fetchone()
    return row[0] if row else None


def _project_activity(conn, project) -> tuple:
    """该项目的活跃量：(活跃条数, 最近一次 recorded_at)。

    口径与 L1 一致：dormant=0 且 status='active'——已替代/已作废的不算活跃记忆。

    **2026-10-25：按别名展开统计**。同一个项目的两个入口（`cdql` 9 条 / `CDQL` 2 条）
    如果只算规范名，会显示「活跃 2 条」而实际有 11 条——数字偏小比偏大更危险，
    因为它会让 AI 以为"这个项目没什么东西"从而不去查。
    """
    pnames = resolve_project_names(conn, project)
    if not pnames:
        pnames = [project]
    ph = ",".join("?" * len(pnames))
    row = conn.execute(
        f"SELECT COUNT(*), MAX(recorded_at) FROM code_index "
        f"WHERE project IN ({ph}) AND dormant=0 AND status='active'", pnames).fetchone()
    return int(row[0] or 0), (row[1] or "")


def _resolve_injection_project(conn, citizen_id, project=None, cwd=None) -> tuple:
    """按四档优先级解析 L1 的"当前项目"，返回 (项目|None, 来源, 其他项目列表)。

    来源取值见 _PROJECT_SOURCES；"其他项目"只在 ② 档有值（多档期时**主项目以外**的
    那些，按开档顺序去重）。为什么只在 ② 档列：其余档的项目来源本身只有一个值，
    硬凑"同时在其他项目"会把"调用方只问了 ANYI"说成"你还在企业智能"。

    project/cwd 的类型校验放在这里：它们来自 CLI 或 MCP 入参，非字符串（误传 dict）
    应该响亮报错——静默 str() 会把调用方写错的 bug 埋进输出里。
    """
    if project is not None and not isinstance(project, str):
        raise TypeError(f"project 必须是字符串，收到 {type(project).__name__}")
    if cwd is not None and not isinstance(cwd, str):
        raise TypeError(f"cwd 必须是字符串，收到 {type(cwd).__name__}")

    explicit = (project or "").strip()
    if explicit:
        # 2026-10-25：**显式声明也要走别名归一**。
        # 不归一的后果实测过：`inject --project ANYI`（前身名）显示「活跃 0 条」，
        # 而记忆全在 `anyi-studio` 下（活跃 77 条）——**第一屏就给了个空项目**，
        # 正是 `08` §2.1 说的"注入包的第一屏经常是错的，而它不会说我不确定"。
        # 归一取规范名（`resolve_project_names` 的第一个元素），但**来源仍是 explicit**：
        # 调用方的声明优先级不变，变的只是名字被解析成了它当前的真名。
        canon = resolve_project_names(conn, explicit)
        return (canon[0] if canon else explicit), "explicit", []
    if citizen_id:
        stints = current_open_stints(conn, citizen_id)
        if stints:
            primary = stints[-1]["project"]          # 最近开的那个 = 主项目
            others = []
            for st in stints[:-1]:
                name = st["project"]
                if name != primary and name not in others:
                    others.append(name)
            return primary, "stint", others
    if (cwd or "").strip():
        by_map = resolve_project_by_cwd(conn, cwd)
        if by_map:
            return by_map, "workspace_map", []
    # 第 ④ 档：兜底。这个查询就是 R1 点名的"最近更新者即当前项目"——保留作兜底，
    # 但调用方（_render_project_head）会把 source='inferred' 标注进输出。
    #
    # 2026-10-25 加一道前置（用户实测抓到的）：**只对"来过的人"用兜底**。
    # 一个第一次接入的 AI 没有任何痕迹，"最近更新的项目"是**别人**的工作痕迹——
    # 拿它当这个 AI 的当前项目，等于给它一个错误前提（它会按别人的语境去工作）。
    # `12` 的原话是「猜错项目比不报项目危险得多」，而这里连"猜"的资格都不该有。
    #
    # "来过"的判据取**任一痕迹**：开过档期 / 写过记忆 / 读过东西。
    # 为什么不能只看档期：老数据里很多公民有记忆但从未开过档期（`project_stints`
    # 长期恒 0 就是那个 bug），只判档期会把它们也打成"没来过"（判据 F 当场抓到）。
    if citizen_id:
        touched = conn.execute(
            "SELECT 1 FROM project_stints WHERE citizen_id=? "
            "UNION ALL SELECT 1 FROM code_index WHERE owner=? "
            "UNION ALL SELECT 1 FROM memory_access_log WHERE citizen_id=? LIMIT 1",
            (citizen_id, citizen_id, citizen_id)).fetchone()
        if not touched:
            return None, "undeclared", []
    row = conn.execute(
        "SELECT project FROM code_index "
        "WHERE dormant=0 AND status='active' AND project IS NOT NULL AND project <> '' "
        "GROUP BY project ORDER BY MAX(recorded_at) DESC LIMIT 1").fetchone()
    if row:
        return row[0], "inferred", []
    return None, "undeclared", []


def _render_project_head(project, count, last_seen, source) -> str:
    """L1 的项目行。走 ④ 档（inferred）时**必须**带上"这是推断"的标注。

    2026-10-25（T1）：落兜底项目时也必须带标注。否则 AI 看到「当前项目：未分类」
    会把它当成一个正常项目名——而它真正的含义是"你没说在哪，系统替你接住了"。
    诚实信号不能因为"有了容器"就丢掉（21 号文档 §2.1 的保留疑问，这是它的答案）。
    """
    if source not in _PROJECT_SOURCES:
        # 来源词表是 meta 契约的一部分（调用方按它决定怎么显示/是否告警）。
        # 拼错一个词就应该当场响，而不是让上层把它当"某种有证据的来源"。
        raise ValueError(f"未知的项目来源：{source!r}（可用：{_PROJECT_SOURCES}）")
    if source == "inferred":
        return f"当前项目：{project}（活跃 {count} 条，{_INFERRED_PROJECT_NOTE}）"
    tail = f"，最近更新 {last_seen[:10]}" if last_seen else ""
    line = f"当前项目：{project}（活跃 {count} 条{tail}）"
    if project == FALLBACK_PROJECT_NAME:
        line += ("\n　（这是兜底项目：你给的工作目录没映射到具体项目，记忆暂时落在这里；"
                 "用 `fish workspace set` 声明后可归到真项目）")
    return line


# ------------------------------------------------------ ③ 规矩 / ④ 该注意什么
# 2026-10-24（全局复盘）：新 AI 接入时缺两段——「这里的规矩」与「我该注意什么」。
# 这两段**不是新架构**，是「让新来的人少踩坑」：
#   ③ 规矩      = 长期有效的偏好/硬规则（只给 code+标题，正文按需 resolve_code 读）；
#   ④ 该注意什么 = 临时信号（未闭合 / 最近的坑 / 别处常读而我没读）。
# 段的次序本身就是优先级：规矩在注意事项**之前**、也在裁剪顺序里更该保留——
# 规矩是长期的，注意事项是临时的；临时信息比长期规范更可弃。
#
# 两条贯穿两段的纪律（都是踩过的坑）：
#   · **空就不出现**：不写"暂无规矩"这种噪音。噪音会训练 AI 跳过这一段标题，
#     等于把"有信号"和"没信号"在观感上抹平。
#   · 只列标题不列正文：注入包是每会话开场都用的路径，正文会把预算吃光；
#     AI 真正需要的是"知道有这条"，读全文走 resolve_code（那是主动行为，可计量）。
#
# ③ 的分栏口径：**按适用范围，不按作者**（2026-10-25 修正，上一版是错的）。
#   · 上一版按 `owner` 分栏（「主人规矩」+「本项目规矩」）——语义错位：
#     `code_index.owner` 记的是"**谁写的**"，不是"这条规范**约束谁**"。
#     生产库实证：resolve_owner() = 安逸，而安逸名下 rule = 0 条 → 「主人」栏永远空；
#     全库 39 条 active rule 的作者是 workbuddy-main(37) / codex-main(1) / 无归属(1)。
#     所以「按作者筛规矩」筛的是写的人，不是适用范围——从语义上就不成立。
#   · 现行口径两栏，且两栏**谓词互斥**（同一条规矩不可能既绑项目又不绑项目）：
#       ① 本项目规矩 ← project = 当前项目
#       ② 全局规矩   ← project IS NULL（不绑定任何项目，才算"处处适用"）
#   · **全局栏在生产库目前是 0 条**（所有 rule 都带 project）→ 空就不出现，这是正常态，
#     不是 bug；也不许为了"让栏好看"把某个项目的规矩提成全局规矩。
#   · rule 标题里含「必须/不得/一律/所有」等通用词的实测只有 1 条：
#     现在的 rule 绝大多数是**项目特定**的，这也支持"按项目分栏、不硬造全局栏"。

RULES_PER_SOURCE_LIMIT = 3          # ③ 每栏最多列几条（本项目规矩 / 全局规矩 各自算）
RULES_BLOCK_MAX_TOKENS = 300        # ③ 段自身的硬上限（超了少列一条，**不截断标题**）
ATTENTION_PER_TYPE_LIMIT = 2        # ④ 每类信号各取 top N
ATTENTION_BLOCK_MAX_TOKENS = 400    # ④ 段自身的硬上限（超了从末尾丢行）
ATTENTION_TITLE_MAX_CHARS = 50      # ④ 标题截断长度（只截这里；③ 的标题不截）
ATTENTION_OPEN_WINDOW_DAYS = 30     # A 未闭合：近 30 天
ATTENTION_LESSON_WINDOW_DAYS = 7    # B 最近的坑：近 7 天
# A 类判据是**标题里的词**（不是正文）：注入包只看标题，正文里的"待办"看不见。
# 词表用**小写**存，SQL 侧对 LOWER(title) 做 INSTR —— 中文不受大小写影响，
# 但 `TODO` 在本库真实标题里既有大写也有小写，所以统一走小写匹配。
ATTENTION_OPEN_KEYWORDS = ("未闭合", "待办", "下一步", "todo", "待确认",
                           "待定", "待验证", "待补")

# ④ 段三类信号的行前缀。也是"这条为什么出现在这里"的可读说明。
_ATTENTION_LABELS = {"open": "未闭合", "lesson": "最近的坑", "gap": "别处读过但你没看"}


def _clip_chars(text, limit):
    """按**字符**截断（不是 token），并把换行压成空格。

    为什么按字符：④ 段每行是给人看的一句话，字符数最直观；token 门槛留给整段预算。
    """
    s = (text or "").strip().replace("\r", " ").replace("\n", " ")
    if limit <= 0 or len(s) <= limit:
        return s
    return s[:max(0, limit - 1)] + "…"


def _rule_rows(conn, *, project=None, limit=RULES_PER_SOURCE_LIMIT, exclude=()):
    """取某一栏规矩的 `(code, title)`：`mem_type='rule'`、active、未休眠，按时间降序。

    分栏口径见本节开头：**按适用范围，不按作者**（`owner` 是作者，不是适用范围）。
      · `project` 给定 → `project = ?`（本项目规矩）；
      · `project` 为 None → `project IS NULL`（全局规矩——不绑定任何项目才是全局）。
    空字符串/纯空白按"没有项目"处理、归到全局栏：那是"未绑定项目"，
    不是"项目名叫空"（`record_memory` 不给 project 时不会产出空串，但老数据/手工 SQL 可能）。
    **只取标题**——正文按需 `resolve_code` 读（见本节开头的取舍）。
    `exclude` 里的编码不列（现行两栏谓词互斥，理论上撞不上；这条是防御性去重）。
    多取 `len(exclude)` 条再在 Python 里剔，是为了剔除后仍凑满 `limit` 条。
    """
    key = (project or "").strip() or None
    if key is None:
        clause, where_params = "project IS NULL", []
    else:
        clause, where_params = "project = ?", [key]
    sql = ("SELECT code,title FROM code_index WHERE mem_type='rule' "
           "AND status='active' AND dormant=0 AND " + clause +
           " ORDER BY recorded_at DESC, code DESC LIMIT ?")
    params = where_params + [int(limit) + len(set(exclude))]
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        # 老库缺列/缺表：注入包不能因此崩（本段只是锦上添花，不是身份行）。
        return []
    out = []
    for code, title in rows:
        if code in exclude:
            continue
        out.append((code, (title or "").strip()))
        if len(out) >= int(limit):
            break
    return out


def build_short_rules_block(conn, *, project=None, per_source=RULES_PER_SOURCE_LIMIT,
                            max_tokens=RULES_BLOCK_MAX_TOKENS) -> dict:
    """段③「规矩」：本项目规矩 + 全局规矩，各取最近 N 条。

    返回 `{"text", "codes", "trimmed"}`：
      · `text`    —— 印进注入包的整段（**结尾带换行**；两栏都空时为 ""，调用方据此整段不出现）；
      · `codes`   —— 真正印进包里的编码（`meta["rules_codes"]` / push_log 用）；
      · `trimmed` —— 本段自己的裁剪标记（超 `max_tokens` 时**少列条目**，绝不截断标题）。

    为什么"最近 3 条"而不是全部：本库 rule 已有 39 条（生产库实测），全列会吃光预算；
    规矩的**新旧**代表当前在用的版本，老的还在库里（recall 查得到），不必占据开场那一屏。

    `project` 为空（当前项目未声明）时**只有全局栏**——不拿"某个项目"冒充你的项目。
    **不再接受 `owner_id`**：按作者筛规矩是 2026-10-25 证伪的口径（见本节开头）。
    两栏标题：本项目规矩（ANYI）：/ 全局规矩：；空栏整块不出现（不写"暂无"）。
    """
    proj_key = (project or "").strip() or None
    proj_items = _rule_rows(conn, project=proj_key, limit=per_source) if proj_key else []
    taken = {code for code, _ in proj_items}
    global_items = _rule_rows(conn, project=None, limit=per_source, exclude=taken)

    def render(proj_list, global_list):
        lines, codes = [], []
        if proj_list:
            lines.append(f"本项目规矩（{proj_key}）：")
            for code, title in proj_list:
                lines.append(f"- {code} {title}")
                codes.append(code)
        if global_list:
            lines.append("全局规矩：")
            for code, title in global_list:
                lines.append(f"- {code} {title}")
                codes.append(code)
        return ("\n".join(lines) + "\n") if lines else "", codes

    text, codes = render(proj_items, global_items)
    if not text:
        return {"text": "", "codes": [], "trimmed": []}

    proj_list, global_list = list(proj_items), list(global_items)
    trimmed = []
    # 自身硬上限：从**全局栏末尾**往回删（本项目规矩更针对手头工作，更该留）；
    # 全局栏空了才动本项目栏。只丢整条，不截断标题。
    while text and count_tokens(text) > max_tokens and (proj_list or global_list):
        if global_list:
            global_list.pop()
        else:
            proj_list.pop()
        text, codes = render(proj_list, global_list)
        if "规矩截断" not in trimmed:
            trimmed.append("规矩截断")
    return {"text": text, "codes": codes, "trimmed": trimmed}


def _open_loop_rows(conn, since_z, limit, exclude=()):
    """A 类「未闭合事项」：标题命中开放词表的 active 记忆（窗口内，时间降序）。"""
    cond = " OR ".join("INSTR(LOWER(COALESCE(title,'')), ?) > 0"
                       for _ in ATTENTION_OPEN_KEYWORDS)
    sql = ("SELECT code,title FROM code_index WHERE status='active' AND dormant=0 "
           "AND recorded_at >= ? AND (" + cond + ") "
           "ORDER BY recorded_at DESC, code DESC LIMIT ?")
    params = [since_z] + [k.lower() for k in ATTENTION_OPEN_KEYWORDS]
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


def _lesson_rows(conn, since_z, limit, exclude=(), same_project=None):
    """窗口内 lesson 的 `(code, title)`：时间降序；`same_project` 给定时只取该项目。

    `same_project=None` = 不限项目（用于跨项目补齐阶段，也用于"当前项目未声明"）。
    """
    where, params = "recorded_at >= ?", [since_z]
    if same_project is not None:
        where += " AND project = ?"
        params.append(same_project)
    sql = ("SELECT code,title FROM code_index WHERE mem_type='lesson' "
           "AND status='active' AND dormant=0 AND " + where +
           " ORDER BY recorded_at DESC, code DESC LIMIT ?")
    params.append(int(limit) + len(set(exclude)))
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


def _recent_lesson_rows(conn, since_z, limit, exclude=(), project=None):
    """B 类「最近的坑」：窗口内的 lesson，**同项目优先**，不足再用跨项目补齐。

    排序键 = (是否同项目 DESC, recorded_at DESC)（2026-10-25 改）。
    为什么不是"纯时间取最近 2 条"：生产库实测近 7 天有 39 条 lesson；纯按时间很可能
    取到**别的项目**的坑——给 A 项目工作的 AI，最该先知道 A 自己的坑。
    为什么还要跨项目：通用教训（如"判断谁在写"的证据顺序）跨项目有效，不该被排除，
    只是**排序靠后**——同项目满额时不占位，不足额时用它补满（不空着）。
    当前项目未声明（project 为空）时没有"同项目"可比，退化为纯时间序。

    实现用**两段查询**而不是一条 CASE 排序：两段各自 LIMIT，语义直白，
    也让判据能分别钉住"同项目优先"（B′）与"跨项目补齐"（B2）两件事。
    """
    limit = int(limit)
    exclude = set(exclude)
    key = (project or "").strip() or None
    out = _lesson_rows(conn, since_z, limit, exclude, same_project=key) if key else []
    if len(out) < limit:
        taken = exclude | {code for code, _ in out}
        out += _lesson_rows(conn, since_z, limit - len(out), taken, same_project=None)
    return out


def _attention_gaps(conn, root, citizen_id, session_actor, limit):
    """C 类「别处读过但你没看」：**直接复用 S1 的 `_knowledge_gaps`**，不另写一份。

    为什么必须复用而不是重写：这条判据有四个口径（`status='active'` / 别人读 ≥3 次 /
    本会话 0 次 / 14 天窗口）加一条"同类依据怎么选"。两处各写一份 SQL，改门槛时
    必有一处漏改——那正是"两份实现互相漂移"的 bug 来源。所以这里只做**参数准备**
    （会话标签、同类范围、窗口起点），判定与取数全部交给 S1。
    """
    audit = read_tool_call_audit(root)
    resolved = _resolve_profile_session(conn, audit, session_actor=session_actor,
                                        citizen_id=citizen_id)
    label = resolved["label"]
    if not label:
        # 连"我是哪个会话"都不知道 → C 类**没有**可说的（不拿全库凑数）。
        return []
    scopes = _candidate_scopes(conn, audit, label, resolved["session"]["citizen_id"])
    if not scopes:
        return []
    since_z = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(days=KNOWLEDGE_GAP_WINDOW_DAYS)).strftime(
                   "%Y-%m-%dT%H:%M:%SZ")
    try:
        return _knowledge_gaps(conn, scopes, label, since_z, int(limit))
    except sqlite3.Error:
        return []


def build_attention_block(conn, root, *, citizen_id=None, session_actor=None,
                          project=None,
                          per_type=ATTENTION_PER_TYPE_LIMIT,
                          max_tokens=ATTENTION_BLOCK_MAX_TOKENS) -> dict:
    """段④「该注意什么」：未闭合 / 别处读过但你没看，各取 top N。

    返回 `{"text", "codes", "trimmed"}`（与另两段同构）：
      · `text`    —— 两类**全空时为 ""**（整段不出现，不写"暂无"）；
      · `codes`   —— 印进包里的编码（`meta["attention_codes"]`）；
      · `trimmed` —— 本段自己的裁剪标记（超 `max_tokens` 时从末尾丢行）。

    2026-10-25（T9）：原本的第三类「最近的坑」（lesson）已独立成段⑤
    `build_lesson_block`（理由见那个函数——rule 与 lesson 是两种性质的约束）。
    因此段④**不再因 `project` 改变行为**：A 类未闭合跨项目都要看见（别人停在哪儿
    与你在哪个项目无关），C 类的高价值未读有自己的 S1 口径。
    `project` 参数保留**只为兼容既有调用方**，本段不读它。
    """
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    open_since = (now_dt - datetime.timedelta(days=ATTENTION_OPEN_WINDOW_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")

    bags = {"open": [], "gap": []}
    seen = set()
    for code, title in _open_loop_rows(conn, open_since, per_type):
        if not code or code in seen:
            continue
        seen.add(code)
        bags["open"].append((code, title))
    for item in _attention_gaps(conn, root, citizen_id, session_actor, per_type):
        code = (item or {}).get("code")
        if not code or code in seen:
            continue
        seen.add(code)
        bags["gap"].append((code, (item.get("title") or "").strip()))

    order = ("open", "gap")

    def render():
        lines, codes = [], []
        for key in order:
            for code, title in bags[key]:
                lines.append(f"- {_ATTENTION_LABELS[key]}：{code} "
                             + _clip_chars(title, ATTENTION_TITLE_MAX_CHARS))
                codes.append(code)
        return (("该注意什么：\n" + "\n".join(lines) + "\n") if lines else ""), codes

    text, codes = render()
    if not text:
        return {"text": "", "codes": [], "trimmed": []}

    trimmed = []
    # 自身硬上限：从**末尾**往回丢（末尾是 C 类；A 类「未闭合」最该留）。
    while text and count_tokens(text) > max_tokens and any(bags.values()):
        for key in reversed(order):
            if bags[key]:
                bags[key].pop()
                break
        text, codes = render()
        if "注意截断" not in trimmed:
            trimmed.append("注意截断")
    return {"text": text, "codes": codes, "trimmed": trimmed}


def build_lesson_block(conn, *, project=None, per_type=ATTENTION_PER_TYPE_LIMIT,
                       max_tokens=ATTENTION_BLOCK_MAX_TOKENS, exclude=()) -> dict:
    """段⑤「错题本」：窗口内的 lesson，**同项目优先**，不足用跨项目补齐。

    为什么从「该注意什么」里独立出来（2026-10-25 · T9）：
      `rule` 是"该怎么做"，`lesson` 是"别再这样"——两种约束的**性质**不同。
      混在段④里，AI 无法从段名判断"这条是通用教训还是本项目的事"，也让已有的
      「规矩（长期）/ 注意（临时）」分层被第三种东西糊住。拆开之后三段各归其位：
      ③规矩 = 规范，④该注意什么 = 临时信号，⑤错题本 = 已经踩过的坑。

    为什么仍按「同项目优先、跨项目补齐」：通用教训（例如"判断谁在写"的证据顺序）
    跨项目有效，不该被排除，只是**排序靠后**（口径在 `_recent_lesson_rows` 里，
    本函数不另写一份——那正是"两份实现互相漂移"的来源）。

    `exclude`：段④已印过的编码，由调用方传入。**一条记忆只在一段里出现**——
    同一件事占两段预算，等于用两倍的篇幅说同一句话。

    返回 `{"text", "codes", "trimmed"}`（与另两段同构；全空时 text=""，整段不出现）。
    """
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    lesson_since = (now_dt - datetime.timedelta(days=ATTENTION_LESSON_WINDOW_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    rows = _recent_lesson_rows(conn, lesson_since, per_type,
                               exclude=set(exclude or ()), project=project)
    items = [(code, title) for code, title in rows if code]

    def render():
        if not items:
            return "", []
        lines = [f"- {code} {_clip_chars(title, ATTENTION_TITLE_MAX_CHARS)}"
                 for code, title in items]
        return ("错题本：\n" + "\n".join(lines) + "\n"), [c for c, _ in items]

    text, codes = render()
    if not text:
        return {"text": "", "codes": [], "trimmed": []}

    trimmed = []
    # 自身硬上限：从**末尾**丢整条（与另两段同一纪律：不截断标题）。
    while text and count_tokens(text) > max_tokens and items:
        items.pop()
        text, codes = render()
        if "错题本截断" not in trimmed:
            trimmed.append("错题本截断")
    return {"text": text, "codes": codes, "trimmed": trimmed}


# ---------------------------------------------------------------- 推送候选（P1-a）
# 设计来源：`16` §5。要点四条——
#   ① 判据是「收益 − 成本」，不是"这条有没有用"（只有收益项的公式会退化成尽量不说话）
#   ② 四类候选 50/25/15/10，**意外类至少 1 条**（防茧房闸 2）
#   ③ **防静默失败**：降权可以，归零不行（归零 = 吸收态，`15` 调研的核心修正）
#   ④ 每条必须带 reason（可解释性是契约，不是可选装饰）
# 与 ③④⑤ 的分工：那三段"按类型取固定条数"，本段"按分数选、带理由"；
# 两者**必须不重复编码**——同一条记忆在包里出现两次等于白占预算（由 exclude 保证）。

_PUSH_KIND_RATIO = {"rule": 0.50, "association": 0.25,
                    "situational": 0.15, "serendipity": 0.10}
_SILENCE_FLOOR = 0.15      # 降权下限：**永不归零**
_SILENCE_CAP_DAYS = 90     # 封顶：超过这么久没推过 → 强制恢复 1.0
_PUSH_W = {                # `16` §5.1 的两组权重（收益侧 / 成本侧）
    "rel": 0.40, "gap": 0.40, "urgent": 0.20,
    "false": 0.50, "token": 0.30, "intr": 0.20,
}
_PUSH_TAG = "可能对你有用"


def _push_history(conn, code):
    """该编码的推送历史：(总次数, ignored 次数, 最近一次推送时间)。

    按 **code** 聚、不分会话：静默失败说的是"这条内容反复没人理"，
    跨会话才看得出——某个会话忽略它，不能证明它对所有人都没用。
    """
    rows = conn.execute(
        "SELECT reaction, pushed_at FROM push_log WHERE code=? ORDER BY id DESC",
        (code,)).fetchall()
    if not rows:
        return 0, 0, None
    ignored = sum(1 for r, _ in rows if r == "ignored")
    return len(rows), ignored, parse_utc(rows[0][1])


def _silence_weight(conn, code, *, now=None):
    """静默降权系数 ∈ [`_SILENCE_FLOOR`, 1.0]，**永不返回 0**。

    为什么不能归零（`15` 调研最重要的一条）：DAWN 论文的 silencing failure mode 就是
    「用户一直忽略 → 阈值一路上升 → 进入吸收态 → 永远不推」。
    用户原话是「等推送完善后会让 AI 意识到冷门工具的用途」——**归零就永远没有"以后"**。
    所以这里的下限是 0.15，且超过 `_SILENCE_CAP_DAYS` 天没推过就强制回到 1.0。
    """
    total, ignored, last = _push_history(conn, code)
    if total <= 0:
        return 1.0
    now_dt = now or datetime.datetime.now(datetime.timezone.utc)
    if last is not None and (now_dt - last).days >= _SILENCE_CAP_DAYS:
        return 1.0                      # 封顶：太久没推，给一次机会
    return max(_SILENCE_FLOOR, 1.0 - 0.85 * (ignored / float(total)))


def _project_of(conn, code):
    row = conn.execute("SELECT project FROM code_index WHERE code=?", (code,)).fetchone()
    return row[0] if row else None


def _seen_in_session(conn, code, session_key):
    """本会话是否读过这条（read_body / recall / push 展示都算"见过"）。"""
    if not session_key:
        return False
    row = conn.execute(
        "SELECT 1 FROM memory_access_log WHERE code=? AND session_key=? LIMIT 1",
        (code, session_key)).fetchone()
    return bool(row)


def _cand(conn, code, kind, *, title, reason, current_project,
          seen=False, key=False, now=None):
    """构造一条候选并打分（`16` §5.1：benefit − cost，再乘静默降权）。"""
    proj = _project_of(conn, code)
    # 2026-10-25（用户实测抓到的 #9）：**跨项目候选要降权到近乎不出现**。
    #
    # 实测场景：新 AI 看 EVAL 项目的注入包，拿到两条
    # 「anyi-studio-261022-75（跨项目的意外关联（经 EVAL-261025-08））」——
    # 它根本不知道 anyi-studio 是什么，也不知道 EVAL-261025-08 是哪条，**纯噪音**。
    #
    # 分三档：
    #   · 同项目        → rel=1.0（要的就是它）
    #   · 没有当前项目  → rel=0.0（**跨项目无从比较**：连"当前"都没有，谈什么"跨"）
    #   · 有别的项目    → rel=0.1（0.3 太高：它压得过本项目的边缘候选，占的还是同一份预算）
    if proj and current_project and proj == current_project:
        rel = 1.0
    elif not current_project:
        rel = 0.0
    else:
        rel = 0.1
    novelty = 0.0 if seen else 1.0
    urgent = 1.0 if key else 0.2
    benefit = (_PUSH_W["rel"] * rel + _PUSH_W["gap"] * novelty
               + _PUSH_W["urgent"] * urgent)
    total, ignored, _last = _push_history(conn, code)
    false_p = (ignored / float(total)) if total else 0.5   # 无历史 = 中性先验
    token_cost = min(len(title or "") / 100.0, 1.0)
    cost = (_PUSH_W["false"] * false_p + _PUSH_W["token"] * token_cost
            + _PUSH_W["intr"] * 0.1)                       # 开场打断成本最低
    weight = 1.0 if key else _silence_weight(conn, code, now=now)
    return {"code": code, "kind": kind, "title": title, "reason": reason,
            "project": proj, "benefit": round(benefit, 4), "cost": round(cost, 4),
            "weight": round(weight, 4), "key": bool(key),
            "score": round((benefit - cost) * weight, 4)}


def build_push_candidates(conn, root, *, citizen_id=None, session_actor=None,
                          project=None, exclude=(), limit=12, now=None,
                          session_key=None):
    """生成四类候选（`16` §5.2），每条带 reason / benefit / cost / score。

    **不过滤 score<=0**：是否真的推由 `select_push_candidates` 决定。
    但静默降权归零的会被剔除——那才是"放弃这条"的正式语义。

    返回列表，按 score 降序。
    """
    exclude = set(exclude or ())
    now_dt = now or datetime.datetime.now(datetime.timezone.utc)
    out = []

    # 锚点：当前项目最近 3 条 active。候选从它们的 links 邻居长出来，
    # 而不是"全库最近更新"——后者会让别的项目的更新把候选带偏。
    anchors = []
    if project:
        anchors = [c for (c,) in conn.execute(
            "SELECT code FROM code_index WHERE project=? AND status='active' "
            "AND dormant=0 ORDER BY code DESC LIMIT 3", (project,)).fetchall()]
    skip = exclude | set(anchors)

    def seen(code):
        if session_key:
            return _seen_in_session(conn, code, session_key)
        return conn.execute(
            "SELECT 1 FROM memory_access_log WHERE code=? AND citizen_id IS ? LIMIT 1",
            (code, citizen_id)).fetchone() is not None

    # ── A 规则（50%）：未闭合事项。key=True → 不受静默降权 ──────────────
    since = (now_dt - datetime.timedelta(days=ATTENTION_OPEN_WINDOW_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    for code, title in _open_loop_rows(conn, since, 3, exclude=skip):
        out.append(_cand(conn, code, "rule", title=title, current_project=project,
                         seen=seen(code), key=True, now=now_dt,
                         reason="未闭合：开了头还没有结尾"))
        skip.add(code)

    # ── B 联想（25%）/ D 意外（10%）：都从锚点的 links 邻居里取 ──────────
    assoc, serendip = [], []
    for anchor in anchors:
        for nb in links_for(conn, anchor, direction="both"):
            code = nb.get("code")
            if not code or code in skip:
                continue
            row = conn.execute(
                "SELECT title FROM code_index WHERE code=? AND status='active' "
                "AND dormant=0", (code,)).fetchone()
            if not row:
                continue
            title = row[0] or ""
            nproj = _project_of(conn, code)
            if nproj and project and nproj != project:
                serendip.append((code, title, anchor, nb.get("weight") or 0))
            else:
                assoc.append((code, title, anchor, nb.get("weight") or 0))
            skip.add(code)
    for code, title, anchor, _w in sorted(assoc, key=lambda x: -x[3])[:4]:
        out.append(_cand(conn, code, "association", title=title, current_project=project,
                         seen=seen(code), now=now_dt,
                         reason=f"与 {anchor} 有关联"))
    for code, title, anchor, _w in sorted(serendip)[:2]:
        out.append(_cand(conn, code, "serendipity", title=title, current_project=project,
                         seen=seen(code), now=now_dt,
                         reason=f"跨项目的意外关联（经 {anchor}）"))

    # ── C 态势（15%）：同项目、别的会话最近 10 分钟读过、本会话没读 ──────
    since10 = (now_dt - datetime.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        "SELECT DISTINCT m.code, c.title FROM memory_access_log m "
        "JOIN code_index c ON c.code = m.code "
        "WHERE m.accessed_at >= ? AND c.status='active' AND c.dormant=0 "
        "AND c.project IS ? ORDER BY m.accessed_at DESC LIMIT 8",
        (since10, project)).fetchall()
    for code, title in rows:
        if code in skip or seen(code):
            continue
        out.append(_cand(conn, code, "situational", title=title, current_project=project,
                         seen=False, now=now_dt,
                         reason="同项目其他会话刚读过"))
        skip.add(code)

    # ── E 看板变更（EVAL-261025-20，2026-10-25 修）─────────────────────
    # 背景：看板此前只在 SessionStart 注入一次——多 AI 陆续开工时，
    # A 看不到 B 后签的到（"看板只随开场注入一次，之后无任何变更推送"）。
    # 这里拿"上次注入看到的快照"（board_seen）与当前状态比：
    # 你看到之后又有人签到 / 有新动态 → 出一条候选，交给工具后推送。
    if citizen_id:
        try:
            for bid, bscope, bkey, seen_post, seen_pres in conn.execute(
                "SELECT b.id, b.scope, b.scope_key, s.last_post_id, s.last_presence_at "
                "FROM board_seen s JOIN boards b ON b.id = s.board_id "
                "WHERE s.citizen_id=? AND b.status='open' AND b.period_key=?",
                (citizen_id, board_period())).fetchall():
                cur_post = conn.execute(
                    "SELECT COALESCE(MAX(id),0) FROM board_posts WHERE board_id=?",
                    (bid,)).fetchone()[0]
                cur_pres = conn.execute(
                    "SELECT COALESCE(MAX(last_seen_at),'') FROM presence WHERE board_id=?",
                    (bid,)).fetchone()[0]
                new_posts = int(cur_post or 0) - int(seen_post or 0)
                new_pres = (cur_pres or "") > (seen_pres or "")
                if new_posts <= 0 and not new_pres:
                    continue
                bits = []
                if new_posts > 0:
                    bits.append(f"{new_posts} 条新动态")
                if new_pres:
                    bits.append("有人新签到/更新")
                label = bkey if bscope == "project" else "root"
                out.append({
                    "code": f"board:{label}",
                    "kind": "situational",
                    "title": f"看板 {label} 有更新",
                    "reason": "、".join(bits),
                    "project": (project if bscope == "project" and bkey == project
                                else None),
                    "benefit": 0.6, "cost": 0.05, "weight": 1.0, "key": False,
                    "score": 0.55,
                })
        except Exception:   # noqa: BLE001 —— 看板候选是增强项，不拖垮候选生成
            pass

    # 静默权重归零 = **正式放弃这条**（`_silence_weight` 正常永不返回 0，
    # 只有被显式改坏时才会——这里兜住它，让"归零"这个动作真的生效）。
    # 为什么过滤放在这里而不是 _cand 里：`_cand` 要如实报出 weight，
    # 让调用方看得见"这条被降了多少"，隐藏它等于让降权不可观测。
    out = [c for c in out if c["weight"] > 0]

    # ---- 2026-10-25（#9）：跨项目候选**只在本项目候选不足时才补**----
    #
    # 实测场景：在 EVAL 项目下，两条 `anyi-studio-*` 的「跨项目的意外关联」排进前四。
    # 而 AI 根本不知道 anyi-studio 是什么、也不知道引用的 `EVAL-261025-08` 是哪条——
    # 它不是"意外惊喜"，是**占预算的噪音**。
    #
    # 为什么不在 `_cand` 里把 rel 调到 0：那会让"跨项目关联"这个能力彻底死掉，
    # 而它在"本项目确实没东西可推"时是有价值的。所以改成**门槛**而不是**权重**：
    # 本项目候选够 3 条 → 跨项目一条都不出；不足 3 条 → 按分数补到 3 条。
    if project:
        same = [c for c in out if c.get("project") == project]
        other = [c for c in out if c.get("project") != project]
        if len(same) >= 3:
            out = same
        else:
            other.sort(key=lambda c: (-c["score"], c["code"]))
            out = same + other[:3 - len(same)]

    out.sort(key=lambda c: (-c["score"], c["code"]))
    return out[:int(limit)]


def select_push_candidates(conn, cands, *, limit=4):
    """按分数选，并保证**意外类至少 1 条**（防茧房闸 2）。

    只选 score > 0 的——`16` §5.1 的判据就是"收益要大于成本"。
    但注意：**这不等于"沉默最安全"**，因为 score 里已经含了静默降权项
    （被忽略多的候选分低，但不会归零，所以仍有被选中的机会）。
    """
    cands = [c for c in cands if c["score"] > 0]
    picked, seen_kinds = [], {}
    quota = int(limit)
    # 先给"意外"留一格——不靠排序运气（否则它在冷启动时永远排不上）
    ser = [c for c in cands if c["kind"] == "serendipity"]
    if ser:
        picked.append(ser[0])
        seen_kinds["serendipity"] = 1
    for c in cands:
        if len(picked) >= quota:
            break
        if c in picked:
            continue
        picked.append(c)
    picked.sort(key=lambda c: (-c["score"], c["code"]))
    return picked[:quota]


def build_push_block(conn, root, *, budget_tokens=None, **kw):
    """把候选渲染成注入包的一段（`16` §5.7 的"开场"档）。

    空就不出现（与 ③④⑤ 同一纪律：不写"暂无候选"这种噪音，噪音会训练 AI 跳过这段）。
    """
    if not get_config(conn, "push.enabled"):
        return {"text": "", "codes": [], "trimmed": [], "candidates": []}
    budget = int(budget_tokens or get_config(conn, "push.open_budget_tokens"))
    cands = build_push_candidates(conn, root, **kw)
    picked = select_push_candidates(conn, cands)
    line_budget = budget
    lines = []
    for c in picked:
        line = f"- {c['code']} {_clip_chars(c['title'], ATTENTION_TITLE_MAX_CHARS)}（{c['reason']}）"
        if count_tokens("\n".join(lines + [line])) > line_budget:
            break
        lines.append(line)
    if not lines:
        return {"text": "", "codes": [], "trimmed": [], "candidates": []}
    text = _PUSH_TAG + "：\n" + "\n".join(lines) + "\n"
    codes = [ln[2:].split(" ", 1)[0] for ln in lines]
    return {"text": text, "codes": codes, "trimmed": [],
            "candidates": [{"code": c["code"], "kind": c["kind"], "score": c["score"],
                            "reason": c["reason"]} for c in picked]}


def board_block(conn, project=None, limit=5, citizen_id=None):
    """注入包里的看板段（约 150-200 token）。

    三条设计约束（都来自 `09` R5-6 与 §4.6 的三个坑）：

    1. **只给最近一级**：项目级优先，没有项目才给根看板。三级全推会重复——
       项目级看板已经包含"这个项目里谁在动"，再推根看板就是同一件事说两遍。
    2. **只给最近 5 行动态**：看板是"此刻"，不是"全程"。全给会让注入包变成第二份 daily。
    3. **空看板要明写**：不能留白——留白会让 AI 以为"看板这个功能坏了"，
       而实际上"没人签到"本身就是一条重要信息（未登记 = 未发生）。
    """
    scope, key = ("project", project) if project else ("root", "")
    try:
        board = board_ensure(conn, scope, key)
        pres = board_presence_rows(conn, board)
        posts = board_post_rows(conn, board, limit=limit)
    except Exception:        # noqa: BLE001 —— 看板是增强项，坏了不该拖垮整个注入包
        return None
    # EVAL-261025-20：把"这次注入看到的看板"记成快照，供推送候选比较。
    # 空板也要记（last_post_id=0）——否则"空板 → 第一条签到"永远推不出来。
    if citizen_id:
        try:
            last_post = conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM board_posts WHERE board_id=?",
                (board["id"],)).fetchone()[0]
            last_pres = conn.execute(
                "SELECT COALESCE(MAX(last_seen_at),'') FROM presence WHERE board_id=?",
                (board["id"],)).fetchone()[0]
            conn.execute(
                "INSERT INTO board_seen (board_id,citizen_id,last_post_id,"
                "last_presence_at,updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(board_id,citizen_id) DO UPDATE SET "
                "last_post_id=excluded.last_post_id, "
                "last_presence_at=excluded.last_presence_at, "
                "updated_at=excluded.updated_at",
                (board["id"], citizen_id, int(last_post or 0), last_pres or "",
                 now_utc()))
            conn.commit()
        except Exception:       # noqa: BLE001 —— 记录快照是增强项，不拖垮注入
            pass
    day = _fmt_period(board["period_key"])
    if not pres and not posts:
        return (f"看板（{day}）：**今天还没有人签到**——未登记 = 未发生。"
                "你要开工就用 `fish board checkin --doing \"在做什么\"` 登记，"
                "别人才能判断会不会跟你撞车。")
    # 2026-10-25（`EVAL-261025-09`）：**必须把 surface 带出去**。
    # 旧实现只印 doing，于是"自动注入层恰好缺了唯一有用的字段"——
    # 每个会话都会读到的段落里没有 surface，AI 不主动 board show 就永远
    # 不知道谁占了哪块，又回到"靠自觉"。
    overlaps = board_overlaps(conn, board)
    head = f"看板（{day}）：{len(pres)} 人在场"
    if pres:
        # 排序纪律（2026-10-25 修 · `EVAL-261025-13` 建议 4）：**三级排序**
        #   ① 有效活跃（status != idle）  ② 有重叠  ③ 其余
        #
        # 旧实现只有两级（"有重叠的排前面"），于是**"已离开但有重叠"被抬到了最前面**：
        # 7 行场景实测——3 个活跃工 + 3 个 idle + 1 个 left，4 个残留行都声明 `backend`
        # → 产生 14 条重叠 → 注入包前 5 位坐进 3 个残留行，**一个真在干活的工
        # 被挤出第 6 位，注入包里看不见**。那等于把"看见更多行"变成了"看见错的行"。
        active_ids = {p["citizen_id"] for p in pres if p["status"] != "idle"}
        overlap_ids = {ov["a"] for ov in overlaps} | {ov["b"] for ov in overlaps}
        ordered = sorted(pres, key=lambda p: (
            0 if p["citizen_id"] in active_ids else 1,
            0 if p["citizen_id"] in overlap_ids else 1,
        ))
        shown = []
        for p in ordered[:5]:
            surf = normalize_surface(p["surface"])
            surf_s = ("｜占 " + "、".join(surf[:2]) + ("…" if len(surf) > 2 else "")
                      ) if surf else ""
            mark = "" if p["status"] != "idle" else "（已闲置）"
            shown.append(f"{who_label(p)}（{p['doing'] or '未写 doing'}{surf_s}）{mark}")
        head += " —— " + "、".join(shown) + ("…" if len(pres) > 5 else "")
    lines = [head]
    if overlaps:
        # 重叠是**最该被看见的一句话**，放在动态之前
        lines.append("⚠ 占用面重叠（开工前先协调）：")
        for ov in overlaps[:3]:
            lines.append(f"  · `{ov['at']}` ← {ov['a']} ↔ {ov['b']}")
        if len(overlaps) > 3:
            lines.append(f"  · …共 {len(overlaps)} 处")
    # 残留行**不默默丢掉**，单独一句轻提示（`EVAL-261025-13` 建议 3）：
    # 换个词、不叫"冲突"——它们既不是冲突，也不该被无痕删除。
    stale = board_stale_rows(conn, board)
    if stale:
        names = "、".join(s["citizen_id"] for s in stale[:3]) + (
            "…" if len(stale) > 3 else "")
        lines.append(f"（另有 {len(stale)} 行已闲置超时仍声明占用：{names}"
                     "——这些**不算**重叠，别当成冲突）")
    if posts:
        lines.append("最近动态：")
        for cid, kind, txt, ts in posts[:limit]:
            lines.append(f"  · {_hhmm(ts)} {cid} {kind}：{txt}")
    lines.append("（完整看板：`fish board show`）")
    # 跨午夜可见性（`EVAL-261025-11`）：注入包也带一句，否则后半夜开工的人
    # 会把"板上只有我一个人"当成事实。
    prev_note = board_prev_note(conn, board)
    if prev_note:
        lines.append(prev_note)
    return "\n".join(lines)


def build_injection_pack(conn, root, citizen_id, budget=None, project=None, cwd=None,
                         session_actor=None):
    """生成「权威当下」：L0 身份 + L1 状态 + ③规矩 + ④该注意什么 + L2 索引，硬预算裁剪。

    返回 (pack_text, meta)。
    裁剪顺序（2026-10-24 起）：L2 → ④该注意什么 → ③规矩 → L1 条目 → L0 铁律 → L1。
    为什么这个顺序：索引（L2）最可弃（recall/索引命令随时补回）；注意事项是**临时的**，
    规矩是**长期的**——临时的比长期的更该让位；身份铁律与身份行最后才动（身份永不舍弃）。

    `session_actor` 只影响 ④ 段的 C 类（"别处读过但你没看"要拿**本会话**去比）。
    缺省时按自报名环境变量（`FISH_SESSION_ACTOR`，其次 `FISH_ACTOR`）兜底，再不行
    由 S1 退回公民名——这是 P1-b 定过的会话标识优先级，本函数不发明第四种。
    它不是"账号"：拿账号当会话会把「同账号别的会话读过」算成「我读过了」，
    所以只在**没有更细证据**时才用它（将来 MCP 侧透传真实 `session_key` 后自然更准）。

    project / cwd 都是**可选**证据（R1 根治）：
      · project 显式声明当前项目（最高优先，如 MCP `inject` 的 project 入参）；
      · cwd 用于 workspace_map 的最长前缀匹配（调用方知道自己的工作目录时才给）；
      · 都不给时才按会话档期 → 最近更新兜底，且 meta 与 L1 都会标明来源。
    meta 里的 project / project_source / other_projects 就是这次判定的事实，
    调用方（MCP / CLI / 测试）不必再猜"这句话是怎么来的"。
    """
    budget = int(budget or get_config(conn, "inject.budget_tokens"))
    root_s = str(root)

    # ---- L0 身份（主人 / 行动者 必须分开）----
    owner_id = resolve_owner(conn)
    actor = None
    if citizen_id:
        actor = conn.execute(
            "SELECT display_name,kind,role,host FROM citizens WHERE citizen_id=?",
            (citizen_id,)).fetchone()

    if actor:
        name, kind, role, host = actor
        # 措辞纪律：说「登记身份」而不是「你是」。
        # 「你是 X」会被 AI 理解成本体身份，与宿主身份（如 WorkBuddy）打架。
        #
        # 2026-10-25 补（T4）：把 host 印出来。
        #   `citizens.host` 本来就在上面那条 SELECT 里（一直是查出来的），只是没渲染，
        #   于是「同名两公民」在注入包里长得一模一样——生产库实测 `fish外部评估` 有两条，
        #   分别来自 ZCode 与 cli，谁是本会话只能去库里翻 host_session_key 才分得出。
        #   host 是身份的固有属性（"我从哪个宿主来"），不是新增字段，也不抬高证据级。
        host_s = (host or "").strip() or "未记录"
        ident = (f"登记身份：{name}（公民 {citizen_id}，形态 {kind}，"
                 f"角色 {role or '-'}，宿主 {host_s}）。\n"
                 "　这是你在 fish 的账户名（宿主级），与你的宿主身份不冲突。")
    elif citizen_id:
        ident = (f"身份待核：凭证解出的公民 ID 是 {citizen_id}，但库里查不到这个公民"
                 "（可能是历史遗留或凭证已失效）。先跑 `fish whoami` 核实。")
    else:
        # 2026-10-25 修（外部评估 `EVAL-261025-04`）：**绝不能说"你尚未登记身份"**。
        #
        # 无 token ≠ 没登记。最常见的真相是"这次调用漏了凭证"（会话刚重启、还没 start）。
        # 说成"没登记"会把 AI 引向重跑 `register` —— 然后反复拿到"已存在"，
        # 于是它得出"登记逻辑坏了"的错误结论。实测代价：白跑一轮 + 错误归因。
        #
        # 一条原则（`EVAL-261025-04` 的原话）：**提示语必须是可执行的下一步，
        # 而不是一个可能错误的归因。**
        ident = ("本次调用未带会话凭证，**无法确定归属**。"
                 "（这不等于你没登记——很可能只是漏了 token。）\n"
                 "　· 已登记过 → 跑 `fish session start --citizen <你的 citizen_id>` 拿 token；\n"
                 "　· 还没登记 → 先调 register，它会回执你的 citizen_id。")

    owner_line = ""
    if owner_id and owner_id != citizen_id:
        ro = conn.execute("SELECT display_name,kind FROM citizens WHERE citizen_id=?",
                          (owner_id,)).fetchone()
        if ro:
            # 注意：这里的 actor 是 (display_name, kind, role, host) 四列，
            # kind 在索引 1（不是 2——2 是 role）。索引错位会静默降级成"行动者"。
            actor_kind = actor[1] if actor else "unknown"
            word = {"visitor": "访客", "role": "角色成员",
                    "resident": "居民", "system": "系统进程"}.get(actor_kind, "行动者")
            owner_line = (f"你的主人是 {ro[0]}（公民 {owner_id}，形态 {ro[1]}）。"
                          f"你以{word}身份行动，不要自称主人。\n")

    l0_full = (ident + "\n" + owner_line
               + f"记忆根目录：{root_s}。可用 remember / recall / neighbors 操作它。\n"
               "三条铁律：\n"
               "1. 不确定的先查，不要凭记忆下结论。\n"
               "2. 编码由系统分配，不要自己编。\n"
               "3. 你的身份是系统签发的，不要根据记忆叙事推断自己是谁。\n")
    l0_min = (ident + "\n" + owner_line + f"记忆根目录：{root_s}。\n"
              "铁律：先查再答；编码由系统分配；身份由系统签发。\n")

    # ---- L1 状态 ----
    # 2026-10-24 修（EVAL 外部评估 R1 + 自查），同一处两个问题：
    #   ① 当前项目不再靠"谁最近更新"猜：改走四档证据链（_resolve_injection_project），
    #      没有证据时明说"未声明"，只剩兜底时标注"据最近更新推断"；
    #   ② 状态口径：L1/L2 的四处计数与取数都必须只算 status='active'——原本没筛，导致
    #      「活跃 N 条」虚高（实测 134 vs 真实 104）、「最近三件事」推出已替代/已作废的
    #      条目（实测 TESTP/VIDEO 各中 1 条）、「今日编码」列出已作废编码、「休眠 N 条」
    #      把作废且休眠的也算进去。
    # 注入包是**每个会话开场**都用的路径，错误信息的杠杆最大。
    proj_name, proj_source, other_projects = _resolve_injection_project(
        conn, citizen_id, project=project, cwd=cwd)
    recent = []
    if proj_name:
        active_n, last_seen = _project_activity(conn, proj_name)
        l1_head = _render_project_head(proj_name, active_n, last_seen, proj_source)
        recent = conn.execute(
            "SELECT code,title FROM code_index WHERE project=? AND dormant=0 "
            "AND status='active' ORDER BY code DESC LIMIT 3", (proj_name,)).fetchall()
    else:
        l1_head = _UNDECLARED_PROJECT_LINE
    dormant_n = conn.execute(
        "SELECT COUNT(*) FROM code_index WHERE dormant=1 AND status='active'").fetchone()[0]

    def render_l1():
        lines = [l1_head] if l1_head else []
        if other_projects:
            lines.append("同时在其他项目：" + " / ".join(other_projects))
        if recent:
            lines.append("最近三件事：")
            lines += [f"- {c} {t}" for c, t in recent]
        if dormant_n:
            lines.append(f"另有 {dormant_n} 条已休眠（可 recall 唤醒）")
        return ("\n".join(lines) + "\n") if lines else ""

    # ---- L2 索引 ----
    # 2026-10-25 · P1-a·A 批（研判见 `31-P1a-推送系统-开发方案` §2）。原 L2 占整包
    # 29%（400/1371 tokens），但三项里有两项是**坐标不是信息**：
    #   「今日编码」只印编码不印标题——AI 看到 ANYI-261025-01 必须再查一次才知道是什么；
    #                而它要查就会查，不查则列了也没用（实测当日 19 条只印前 6 条）。
    #   「常用标签」给了名字却不能用（查要 recall --tags），等于给了个用不了的入口。
    # 故两项删除。项目列表改为**只列有 active 记忆的**（原样列 14 个，其中 3 个 0 条），
    # 且只给计数不给 namespace——namespace 是给人看的，AI 靠计数判断"有没有东西可查"。
    # 保留「衰减参数」与「操作指引」：两者都有真实事故依据
    # （`04` §3.3 文档抄错天数导致 AI 报错半衰期 / `12` §1.1 实测绕路率 10.5%）。
    proj_rows = conn.execute(
        "SELECT r.prefix, COUNT(c.code) AS n FROM code_registry r "
        "JOIN code_index c ON c.project = r.prefix AND c.status = 'active' "
        "WHERE r.status = 'active' GROUP BY r.prefix "
        "ORDER BY n DESC, r.prefix").fetchall()
    l2_lines = []
    if proj_rows:
        l2_lines.append("可查项目：" + " ".join(f"{p}({n})" for p, n in proj_rows))
    # 生效衰减参数：让 AI 不必去读可能过期的文档
    dcfg = load_decay_cfg(conn)
    l2_lines.append("衰减(天)：" + " / ".join(
        f"{t}={int(dcfg.get('decay.' + t + '_days', 0))}"
        for t in ("episode", "fact", "lesson", "artifact")))
    # 入口指引（第 1 层引导）：把 AI 引到 MCP 工具，而不是读文件/查 SQLite
    l2_lines.append("【操作指引】记→remember ｜ 查→recall/resolve_code ｜ "
                    "上下文→neighbors ｜ 状态→inject（勿直接读写 data/ 或 sqlite）")
    l2 = ("\n".join(l2_lines) + "\n") if l2_lines else ""

    # ---- ③ 规矩 / ④ 该注意什么 / ⑤ 错题本 ----
    # 三段都可能为空——**空就不出现**，不写"暂无规矩"这种噪音（噪音会训练 AI 跳过这一段）。
    # session_actor 的兜底顺序与 S1 一致：显式入参 > 自报 env > 公民名（由 S1 解析）。
    rules = build_short_rules_block(conn, project=proj_name)
    attention = build_attention_block(
        conn, root, citizen_id=citizen_id, project=proj_name,
        session_actor=(session_actor or "").strip() or env_session_alias())
    # 段⑤ 错题本（2026-10-25 · T9）：从段④拆出，理由是 rule（该怎么做）与
    # lesson（别再这样）性质不同，混在一段里 AI 分不清"通用教训"还是"本项目的事"。
    # `exclude` 传段④已印过的编码——一条记忆只在一段里出现，否则同一件事占两段预算。
    #
    # ⚠️ 2026-10-25 试过再加一条：把 L1「最近三件事」也排除掉，以消除用户看到的重复
    # （`EVAL-261025-08` 同时在 L1 与错题本）。**试了两次都退回来了**，原因是结构性的：
    #   · 全排除 → 错题本被挖空（判据 B2/B'/H/L2 五条红）；
    #   · 空了再兜底补 → 跨项目 lesson 顶替同项目 lesson（判据 B' 红，破坏"同项目优先"）。
    # 根本原因：**L1 的"最近三件事"与⑤错题本同源**（都是"最近窗口内的 lesson/fact"），
    # 而 L1 在 ③④⑤ 之前算，⑤ 想排除 L1 就得回头改已渲染的 L1 —— 顺序上不允许。
    # 结论：这个重复**保留**（约 60 token），因为去掉它要动"同项目优先"或"错题本完整性"，
    # 而那两条比 60 token 硬得多。如实记在这里，不留在代码里当"看起来修好了"。
    lesson = build_lesson_block(conn, project=proj_name,
                                exclude=set(attention["codes"]))
    # 段⑥ 候选（2026-10-25 · P1-a，`16` §5）：四类候选 + 打分 + 防静默。
    # exclude 传 ③④⑤ 已印的编码——一条记忆只在一段里出现（与上面同一纪律）。
    # 为什么排在这三段之后：候选要"补它们没覆盖的"，不是重复它们。
    push = build_push_block(
        conn, root, project=proj_name, citizen_id=citizen_id,
        session_actor=(session_actor or "").strip() or env_session_alias(),
        exclude=set(rules["codes"]) | set(attention["codes"]) | set(lesson["codes"]))
    rules_text, attention_text, lesson_text = (
        rules["text"], attention["text"], lesson["text"])
    push_text = push["text"]

    # ---- 组装 + 裁剪 ----
    # 裁剪顺序（2026-10-25 起）：**候选** → L2 → ④该注意什么 → ⑤错题本 → ③规矩
    #   → L1 条目 → L0 铁律 → L1。
    # 候选排在最前：它是"可能对你有用"，不是"你必须知道"——预算紧张时它第一个让位。
    # 其次才是索引（recall 随时补回）；④临时信号 < ⑤长期教训 < ③长期规范。
    # 身份铁律与身份行最后才动（身份永不舍弃）。
    trimmed = (list(rules["trimmed"]) + list(attention["trimmed"])
               + list(lesson["trimmed"]))

    # ---- 看板段（2026-10-25 · P1-b D 批）----
    # 位置纪律：紧跟 L1（坐标）之后、③规矩之前——**它是"当前态势"，与坐标同属"此刻"**。
    # 只给最近一级看板（项目级优先）：三级全推会重复（`09` R5-6）。
    board_text = board_block(conn, proj_name, citizen_id=citizen_id) or ""

    def total():
        return (count_tokens(l0) + count_tokens(l1) + count_tokens(rules_text)
                + count_tokens(attention_text) + count_tokens(lesson_text)
                + count_tokens(push_text) + count_tokens(l2) + count_tokens(board_text))

    # 注入风格决定开头（可配置：gentle / strong）
    style = get_config(conn, "inject.style")
    if style == "strong":
        header = "【必读 · 当前状态】回答任何问题前，先参考以下状态。\n"
    else:
        header = "【当前状态 · 大肥鱼生命系统注入】\n"

    l0, l1 = header + l0_full, render_l1()
    while total() > budget and push_text:
        push_text = ""
        trimmed.append("候选")
    while total() > budget and l2:
        l2 = ""
        trimmed.append("L2")
    # 看板在裁剪链里**排在注意事项之后**（2026-10-25 改 · `EVAL-261025-11`）。
    #
    # 原文把它排在第 3 位（push → L2 → 看板），理由是"没有它 AI 照样能工作"。
    # 但那个理由与工具描述**自相矛盾**——描述里写的是「开工前先 checkin，
    # 是协作协议，不是礼貌」。一边说它是协议，一边在预算紧时第一个裁掉它，
    # 等于承认它不是协议。
    #
    # 更硬的理由：**它只值 150-200 token**（`board_block` 的体积），
    # 裁它省不下多少，却裁掉了**时效性最强**的那块（"此刻谁在动哪块"）。
    # 而注意事项/错题本/规矩都可以离线查回来（recall / 注入包下一次就有）。
    #
    # 2026-10-25 再修（`EVAL-261025-15`）：上一版把注释改成了上面这段话，
    # 但代码顺序仍是 `注意事项 → 看板 → 错题本 → 规矩`——**注释说"看板该比这三者
    # 都活得久"，代码却只比注意事项久**。同一份文件里两种依据，那是新功能在注释里
    # 先欠一笔。现在按注释调齐，**最终顺序**：
    #   候选 → L2 → 注意事项 → 错题本 → 规矩 → 看板 → L1条目 → L0铁律
    # 依据：**可离线重建的让位于不可重建的**。看板是"此刻"，过了就查不回来；
    # 错题本/规矩 recall 一下就有。
    while total() > budget and attention_text:
        attention_text = ""
        trimmed.append("注意事项")
    while total() > budget and lesson_text:
        lesson_text = ""
        trimmed.append("错题本")
    while total() > budget and rules_text:
        rules_text = ""
        trimmed.append("规矩")
    # 看板放在这三者**之后**（最后被裁）——见上
    while total() > budget and board_text:
        board_text = ""
        trimmed.append("看板")
    while total() > budget and len(recent) > 1:
        recent = recent[:-1]
        l1 = render_l1()
        trimmed.append("L1条目")
    if total() > budget and l0 == header + l0_full:
        # 裁剪 L0 时保留 header（它是风格的载体）
        l0 = header + l0_min
        trimmed.append("L0铁律")
    if total() > budget and l1:
        l1 = ""
        trimmed.append("L1")
    final = total()
    over_budget = final > budget
    if over_budget:
        # 响亮失败：最小注入包仍超预算时，不假装成功
        trimmed.append(f"仍超预算({final}>{budget})")

    pack = (l0 + ("\n" + l1 if l1 else "")
            + ("\n" + board_text if board_text else "")
            + ("\n" + rules_text if rules_text else "")
            + ("\n" + attention_text if attention_text else "")
            + ("\n" + lesson_text if lesson_text else "")
            + ("\n" + push_text if push_text else "")
            + ("\n" + l2 if l2 else "")).strip() + "\n"
    meta = {
        "tokens": count_tokens(pack),
        "budget": budget,
        "over_budget": over_budget,
        "layers": {"L0": count_tokens(l0), "L1": count_tokens(l1),
                   "board": count_tokens(board_text),
                   "rules": count_tokens(rules_text),
                   "attention": count_tokens(attention_text),
                   "lesson": count_tokens(lesson_text),
                   "push": count_tokens(push_text),
                   "L2": count_tokens(l2)},
        "trimmed": trimmed,
        # L1「当前项目」的判定事实（R1 根治）：名字 / 来源 / 同时服务的其他项目。
        # source='inferred' = 走了"最近更新"兜底（输出里已标注这是推断）；
        # source='undeclared' = 没有任何证据（输出里明说未声明）。调用方不得再"补成"
        # 某个项目——那等于把本轮刚拆掉的猜测重新装回去。
        "project": proj_name,
        "project_source": proj_source,
        "other_projects": list(other_projects),
        # 供调用方刷新衰减：注入包展示过的记忆 = 被看见
        "recent_codes": [c for c, _ in recent],
        # L2 自 2026-10-25 起**不再展示具体编码**（研判见 `31` §2：编码是坐标不是信息），
        # 故这里恒为空。保留这个键是 meta 契约的一部分（调用方按固定形状读）。
        # 这不是"少记了一层"：原来 L2 印的只是编码、没印标题，AI 并未真看见内容；
        # 把它们算进 push_log 会让"采纳率"的分母虚高。
        "index_codes": [],
        # 段⑥ 候选（2026-10-25 · P1-a）：每条带 kind / score / reason。
        # 为什么 meta 要报候选而不只是编码：度量要看"推了什么类、打了多少分"，
        # 只报编码就算不出「哪类候选真被用上」——那是 `16` §5.5 的门控指标。
        "push_candidates": list(push["candidates"]) if push_text else [],
        # 段③/④ 里**真正印进包里**的编码（该段被预算裁掉时为空）。
        # 与 index_codes 同一纪律：这两段展示过的记忆也算"被看见"，push_log 要能记到。
        "rules_codes": list(rules["codes"]) if rules_text else [],
        "attention_codes": list(attention["codes"]) if attention_text else [],
        # 段⑤ 错题本里**真正印进包里**的编码（段被预算裁掉时为空）。与上面同一纪律：
        # 展示过就算"被看见"，push_log 要能记到；否则新拆出的这段永远算不出采纳率。
        "lesson_codes": list(lesson["codes"]) if lesson_text else [],
    }
    return pack, meta


def cmd_inject(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    # 与 MCP 的 tool_inject 保持一致：优先 actor（FISH_ACTOR / --citizen），
    # 而不是 default_citizen。否则 CLI 注入会把主人的身份当成行动者。
    if args.citizen:
        cid = args.citizen
    else:
        actor = resolve_actor(conn)
        cid = actor[0] if actor else None
    if not cid:
        # 2026-10-25 改（`EVAL-261025-04`）：原文「未登记行动者；设 FISH_ACTOR」
        # 把「没有行动者」说成了「没登记」——它是个可能错误的归因，
        # 而且指的修法（设 FISH_ACTOR）对 MCP 面根本无效（那是进程级变量）。
        # 改成中性描述 + **两条可执行路径**。
        print("本次未确定行动者（未给 --citizen，且 FISH_ACTOR 与 config.actor_name 都没配）。\n"
              "　· 已登记过 → `fish inject --citizen <你的 citizen_id>`\n"
              "　· 还没登记 → 先 `fish citizen-add <名字>` 或调 MCP 的 register")
        conn.close()
        return
    pack, meta = build_injection_pack(conn, Path(args.root), cid, args.budget,
                                      project=args.project, cwd=args.cwd)
    if args.json:
        print(json.dumps({"pack": pack, **meta}, ensure_ascii=False, indent=1))
    else:
        print("──── 注入包（权威当下） ────")
        print(pack)
        tail = (f" 裁剪={meta['trimmed']}" if meta["trimmed"] else "")
        flag = " ⚠ 超预算" if meta["over_budget"] else ""
        src = f" 项目来源={meta['project_source']}"
        print(f"──── {meta['tokens']} / {meta['budget']} token "
              f"(L0={meta['layers']['L0']} L1={meta['layers']['L1']} "
              f"看板={meta['layers'].get('board', 0)} "
              f"规矩={meta['layers']['rules']} 注意={meta['layers']['attention']} "
              f"L2={meta['layers']['L2']}){src}{tail}{flag} ────")
    conn.close()


def fix_owner(conn, code, citizen_id, reason=""):
    """修正一条记忆的归属（EVAL-261025-21：归属错了必须有修正通道）。

    ## 为什么是"追加事件 + 更新投影"，不是改事件

    `code_index.owner` 是投影；`events` 不可变（触发器锁死）。修正的正确形态：
    追加一条 `memory.owner_fixed`（记录 old/new/reason），再更新投影。
    这样"谁在何时把归属从 A 改到 B"永远可追溯。

    ## 边界

    · 只改 `code_index.owner`；**不改任何其他字段**，也不动正文文件。
    · 编码与公民都必须存在，且公民必须活跃——不给"猜一个"的余地。
    """
    row = conn.execute("SELECT owner FROM code_index WHERE code=?", (code,)).fetchone()
    if not row:
        raise ValueError(f"编码不存在：{code}")
    old = row[0]
    c = conn.execute(
        "SELECT display_name, status, archived_at FROM citizens_all WHERE citizen_id=?",
        (citizen_id,)).fetchone()
    if not c:
        raise ValueError(f"公民不存在：{citizen_id}")
    if (c[1] or "").lower() != "active" or c[2]:
        raise ValueError(
            f"公民不是活跃态：{citizen_id}（status={c[1]} archived={bool(c[2])}）")
    conn.execute("UPDATE code_index SET owner=? WHERE code=?", (citizen_id, code))
    conn.commit()
    append_event(conn, "memory.owner_fixed",
                 {"citizen_id": "system", "evidence_level": "signed"},
                 {"kind": "memory", "id": code},
                 {"code": code, "old_owner": old, "new_owner": citizen_id,
                  "reason": reason or ""})
    return {"code": code, "old_owner": old, "new_owner": citizen_id,
            "display_name": c[0]}


def cmd_owner_fix(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    try:
        res = fix_owner(conn, args.code, args.citizen_id, reason=args.reason or "")
    except ValueError as e:
        print(f"错误：{e}", file=sys.stderr)
        conn.close()
        sys.exit(2)
    print(f"归属已修正：{res['code']}  {res['old_owner']} → {res['new_owner']}"
          f"（{res['display_name']}）")
    if args.reason:
        print(f"　理由：{args.reason}")
    conn.close()


def cmd_owner(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    if args.action == "show":
        oid = resolve_owner(conn)
        if not oid:
            print("未设置主人（fish owner set <citizen_id>）")
        else:
            row = conn.execute(
                "SELECT display_name,kind,is_owner FROM citizens WHERE citizen_id=?",
                (oid,)).fetchone()
            if row:
                src = "citizens.is_owner" if row[2] else "旧配置回退"
                print(f"{oid}\t{row[0]}\t{row[1]}\t来源={src}")
            else:
                print(f"⚠ 悬空：config 指向 {oid}，但 citizens 表里没有这个公民")
    else:
        try:
            name = set_owner(conn, args.citizen_id)
            print(f"主人已设为：{name}（{args.citizen_id}）")
        except (ValueError, KeyError) as e:
            print(f"错误：{e}")
    conn.close()


def cmd_citizen_retire(args):
    """退役一个公民。若有记忆，必须显式 --migrate-to 或 --force，避免静默丢归属。"""
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)

    row = conn.execute("SELECT display_name,kind,status,is_owner FROM citizens WHERE citizen_id=?",
                       (args.citizen_id,)).fetchone()
    if not row:
        print(f"公民不存在：{args.citizen_id}")
        conn.close()
        return
    name, kind, status, is_owner = row
    if status == "retired":
        print(f"{name} 已经是 retired 状态")
        conn.close()
        return
    if is_owner:
        print(f"拒绝：{name} 是系统主人（is_owner=1），不能退役。先设新主人。")
        conn.close()
        return

    n_mem = conn.execute("SELECT COUNT(*) FROM code_index WHERE owner=?",
                         (args.citizen_id,)).fetchone()[0]
    if n_mem and not args.migrate_to and not args.force:
        print(f"拒绝：{name} 名下有 {n_mem} 条记忆。"
              f"请指定 --migrate-to <citizen_id> 或 --force（保持归属不变）")
        conn.close()
        return

    if args.migrate_to:
        tgt = conn.execute("SELECT display_name,status FROM citizens WHERE citizen_id=?",
                           (args.migrate_to,)).fetchone()
        if not tgt or tgt[1] != "active":
            print(f"目标公民无效或非 active：{args.migrate_to}")
            conn.close()
            return
        conn.execute("UPDATE code_index SET owner=? WHERE owner=?",
                     (args.migrate_to, args.citizen_id))
        print(f"已迁移 {n_mem} 条记忆 → {tgt[0]}")

    conn.execute("UPDATE citizens SET status='retired' WHERE citizen_id=?", (args.citizen_id,))
    conn.commit()
    append_event(conn, "citizen.retired",
                 {"citizen_id": "system", "evidence_level": "signed"},
                 {"kind": "citizen", "id": args.citizen_id},
                 {"display_name": name, "kind": kind,
                  "reason": args.reason or "manual",
                  "migrated_to": args.migrate_to,
                  "memories": n_mem})
    print(f"已退役：{name}（{args.citizen_id}）— {args.reason or 'manual'}")
    conn.close()


def cmd_citizen_list(args):
    """列出所有公民：状态、形态、宿主、记忆数、是否主人。"""
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)

    rows = conn.execute(
        "SELECT citizen_id,display_name,kind,host,status,is_owner,created_at "
        "FROM citizens ORDER BY status='retired', created_at").fetchall()
    if not rows:
        print("还没有任何公民。用 citizen-add 或让 AI 调 register 登记。")
        conn.close()
        return

    print(f"{'名字':<18}{'形态':<10}{'宿主':<10}{'状态':<10}{'记忆':>4}  创建时间")
    print("─" * 76)
    for cid, name, kind, host, status, is_owner, created in rows:
        n = conn.execute("SELECT COUNT(*) FROM code_index WHERE owner=?", (cid,)).fetchone()[0]
        mark = "★" if is_owner else " "
        print(f"{name:<18}{kind or '-':<10}{host or '-':<10}{status or '-':<10}{n:>4}  {created} {mark}")
    retired = sum(1 for r in rows if r[4] == "retired")
    print(f"\n共 {len(rows)} 个（活跃 {len(rows) - retired} / 退役 {retired}），★=系统主人")
    conn.close()


# ------------------------------------------------------ 工作区映射命令族（R1 根治）

def cmd_workspace(args):
    """`fish workspace set|list|remove|seed`。

    为什么必须有 CLI：workspace_map 是"项目归属"的唯一权威来源；没有一个可维护的
    入口，用户就只能手改 SQLite——那等于把这张表交给运气，下次升级照样能写错。

    动作分级（与全局纪律一致）：
      · set / remove 是**显式**数据变更（写 events 留痕，可追溯谁改了哪条映射）；
      · list / seed 只读：seed 打印建议但**绝不自动写入**——正文里提到的路径可能
        属于别人的项目，归属必须由人确认。
    """
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    try:
        if args.workspace_action == "set":
            _workspace_cmd_set(conn, args)
        elif args.workspace_action == "list":
            _workspace_cmd_list(conn)
        elif args.workspace_action == "remove":
            _workspace_cmd_remove(conn, args)
        elif args.workspace_action == "seed":
            _workspace_cmd_seed(conn, args)
        else:
            raise ValueError(f"未知的 workspace 子命令：{args.workspace_action}")
    finally:
        conn.close()


def _workspace_cmd_set(conn, args):
    try:
        row = set_workspace_mapping(conn, args.path, args.project, note=args.note)
    except ValueError as e:
        print(f"错误：{e}")
        return
    print(f"已{'新增' if row['created'] else '更新'}映射：{row['path_prefix']} → {row['project']}")
    if row["note"]:
        print(f"  备注：{row['note']}")
    # 只警告不阻断：映射可以先于项目登记存在（例如先把目录归属定下来，再建项目）。
    known = conn.execute("SELECT 1 FROM code_registry WHERE prefix=?",
                         (row["project"],)).fetchone()
    if not known:
        print(f"⚠ 项目前缀 {row['project']} 还没在 code_registry 登记"
              f"（`fish project-add {row['project']}`）——注入包按它取数会是 0 条")


def _workspace_cmd_list(conn):
    rows = list_workspace_mappings(conn)
    if not rows:
        print("还没有任何映射。用 `fish workspace set <路径> <项目>` 添加，"
              "或 `fish workspace seed` 看看现有记忆里提到哪些工作区。")
        return
    # 不用固定列宽：路径里有中文时，按字符数补空格在终端里必然对不齐（CJK 占两列）。
    # 一行一条 + 缩进的备注，比一张"看起来是表格但列是歪的"表更好读。
    for r in rows:
        note = f"（{r['note']}）" if r["note"] else ""
        print(f"{r['path_prefix']}  →  {r['project']}{note}")
    print(f"\n共 {len(rows)} 条映射（路径一律小写/正斜杠/无尾斜杠，匹配不区分大小写）")


def _workspace_cmd_remove(conn, args):
    try:
        removed = remove_workspace_mapping(conn, args.path)
    except ValueError as e:
        print(f"错误：{e}")
        return
    prefix = normalize_workspace_path(args.path)
    print(f"已删除映射：{prefix}" if removed
          else f"没有这条映射：{prefix}（幂等：什么都不做）")


def _workspace_cmd_seed(conn, args):
    res = workspace_seed_candidates(conn, Path(args.root))
    reasons = {
        "multi_project": "多个项目都提到它",
        "shared_parent": "是别的项目路径的父目录（整片归属会误伤）",
        "mapping_mismatch": "与现有映射的归属不一致",
    }

    def show(items):
        for it in items:
            print(f"  {it['path']}  →  {it['project']}（出现 {it['count']} 次）")
            print(f"      fish workspace set \"{it['path']}\" {it['project']}")

    print("工作区 → 项目候选（**只打印，不写入**；确认后用 `fish workspace set` 落库）")
    strong = [it for it in res["suggest"] if it["count"] >= 2]
    weak = [it for it in res["suggest"] if it["count"] < 2]
    if strong:
        print("\n建议映射（正文里出现 ≥2 次，可信度较高）：")
        show(strong)
    if weak:
        print("\n弱候选（只出现 1 次：可能只是在讲别人的目录，先确认再 set）：")
        show(weak)
    if res["conflicts"]:
        print("\n需要人工判断（不给建议）：")
        for it in res["conflicts"]:
            who = " / ".join(it["projects"]) if it.get("projects") else it["project"]
            existing = f"，现有映射属于 {it['existing']}" if it.get("existing") else ""
            why = reasons.get(it.get("reason"), "")
            print(f"  {it['path']}  ← {who}{existing}"
                  f"（出现 {it['count']} 次；{why}）")
    if res["mapped"]:
        print("\n已被现有映射覆盖（无需处理）：")
        for it in res["mapped"]:
            print(f"  {it['path']}  →  {it['project']}（与现有映射一致，出现 {it['count']} 次）")
    if not (res["suggest"] or res["conflicts"] or res["mapped"]):
        print("\n没有找到候选：现有记忆正文里没有出现（且本机真实存在的）绝对路径。"
              "可以直接用 `fish workspace set` 手工声明。")
        return
    print("\n提示：一个目录下放着多个项目（共用工作区）时**不要整片映射**——"
          "拿不准就先只映射最具体的那个项目目录。")


# ------------------------------------------------------ 身份层 P1-b：session 命令族
#
# 为什么是 CLI 而不是 MCP 工具（评审 R-2）：MCP 进程的 env 被宿主 scrub 掉，
# 它拿不到任何"机器事实"，所以它签不出 machine 级。签发权只属于能读到 env 的 CLI；
# MCP 侧将来只调 `validate_session_token` 做校验。判据 H 把这条冻结成断言。

def _print_session_start(res):
    """`session start` 的输出（**契约**：AI 要能从这几行抓到 token）。

    前五行严格按 18 §2 第 6 步的顺序。纪律：全流程只有这一处 print 明文，且只到
    stdout——不 print 到 stderr（stderr 经常被重定向进日志文件），不写任何文件。

    多项目（2026-10-24）：`stint:` 一行一个档期（复用/新开都列出来），`project:` 仍是
    单值口径——只开一个项目时就是那个项目（旧解析不变），多项目时给逗号串。
    """
    print(f"token: {res['token']}")
    print(f"citizen_id: {res['citizen_id']}")
    print(f"session_key: {res['session_key']}")
    print(f"evidence: {res['evidence']}")
    print(f"project: {', '.join(res.get('projects') or []) or '-'}")
    print(f"channel: {res['channel']}")
    print(f"host: {res['host']}")
    print(f"display_name: {res['display_name']}")
    print(f"account_id: {res['account_id'] or '-'}")
    print(f"issued_at: {res['issued_at']}")
    print(f"expires_at: {res['expires_at'] or '永久'}")
    print(f"citizen: {'新建' if res['created'] else '复用'}")
    if res["rotated"]:
        # 字段行保持"可直接解析"（只有数字），解释另起一行——输出是契约，别让 prose 混进字段。
        print(f"rotated: {res['rotated']}")
        print(f"（同会话的旧凭证已吊销 {res['rotated']} 条）")
    for st in res.get("stints") or []:
        print(f"stint: {st['action']} {st['project']}")
        print(f"（档期 id={st['stint_id']}，自 {st['from_ts']} 起）")
    if res.get("archived"):
        print("注意：该会话公民在归档表里——本轮只复用身份，搬行归 P1-d")
    if res.get("warning"):
        print(f"⚠ {res['warning']}")
    print("（token 明文只此一次；丢了就重跑 session start——公民不变，凭证换新）")


def _print_session_status(res):
    info, citizen, stints = res["token"], res["citizen"], res.get("stints") or []
    ident = res["identity"] or {}
    print("会话身份")
    if info:
        print(f"  session_key  {info['session_key']}")
        print(f"  evidence     {info['evidence']}")
        print(f"  channel      {info['channel']}")
        print("  来源         --token")
    else:
        print(f"  session_key  {ident.get('session_key') or '-'}")
        print(f"  evidence     {ident.get('evidence') or '-'}")
        print(f"  channel      {ident.get('channel') or '-'}")
        print(f"  来源         env（{ident.get('source') or '-'}）")
    if citizen:
        print(f"  citizen_id   {citizen['citizen_id']}")
        print(f"  display_name {citizen['display_name']}")
        print(f"  kind/role    {citizen['kind']}/{citizen['role'] or '-'}")
        print(f"  host         {citizen['host'] or '-'}")
        print(f"  account_id   {citizen['account_id'] or '-'}")
        print(f"  last_seen_at {citizen['last_seen_at'] or '-'}")
        if citizen.get("archived_at"):
            print(f"  归档时间     {citizen['archived_at']}（身份在冷表里）")
    else:
        print("  公民         未登记（先跑 `fish session start`）")
    # 一行一个档期，续行对齐到"项目档期："之后——多项目时一眼能看出开了几条。
    # 单档期也走这个格式：两套排版只会让"多了一个档期"这种关键变化不显眼。
    if stints:
        for i, st in enumerate(stints):
            head = "项目档期：" if i == 0 else " " * 10
            print(f"{head}{st['project']}（自 {st['from_ts']}）")
    else:
        print("项目档期：无")
    if info:
        print("凭证")
        # 只露 hash 前 12 位：hash 不是凭证，但它能让人对着 session_tokens 找到那一行。
        print(f"  token_hash   {info['token_hash'][:12]}…（sha256 前 12 位；明文不落库）")
        print(f"  issued_at    {info['issued_at']}")
        print(f"  expires_at   {info['expires_at'] or '永久'}")
        if info["revoked_at"]:
            print(f"  状态         已吊销（{info['revoked_at']}）")
        elif info["expired"]:
            print(f"  状态         已过期（{info['expires_at']}）")
        else:
            print("  状态         有效")
        if info.get("parent_token"):
            print(f"  父凭证       {info['parent_token'][:12]}…")
    if ident.get("warning"):
        print(f"⚠ {ident['warning']}")


def cmd_session_start(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    # 2026-10-24 加：`--cwd` 走 workspace_map 解析项目（R1 的落地点之一）。
    # 优先级：显式 --project > --cwd 解析结果 > 不开档期（宁可没有，不猜）。
    # 为什么不让 hook 自己先查再传：多一次进程启动、多一处可能忘传；
    # 放这里，任何调用方（hook / AI / 脚本）只要给 cwd 就能拿到正确项目。
    projects = getattr(args, "project", None)
    cwd = getattr(args, "cwd", None)
    resolved_from_cwd = None
    fell_back = None
    if not projects and cwd:
        try:
            resolved_from_cwd = resolve_project_by_cwd(conn, cwd)
        except Exception:
            resolved_from_cwd = None
        if resolved_from_cwd:
            projects = [resolved_from_cwd]
        else:
            # T1（2026-10-25）：cwd 没匹配到具体项目 → 落**兜底项目**，而不是"不开档期"。
            # 这是决策 12 的落地点（生活/工作/未分类是真实项目，不是引导层），
            # 也是 `project_stints` 恒 0 的修复：会话不再因为"认不出目录"就一条档期都不留。
            # 仍然"不猜"——兜底项目自身的语义就是"我不知道"，且 L1 会把它标注出来。
            fell_back = fallback_project(conn)
            if fell_back:
                projects = [fell_back]
    try:
        res = start_session(
            conn, projects=projects,
            account=getattr(args, "account", None), alias=getattr(args, "alias", None),
            citizen=getattr(args, "citizen", None))
    except ValueError as e:
        conn.close()
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(2)
    # ⚠️ 这两行必须走 stderr：stdout 是 `key: value` 的**契约面**（hook 按冒号
    # 切分）。诊断句里带冒号，走 stdout 会污染出一个无意义的 key。
    if resolved_from_cwd:
        print(f"（项目由 --cwd 经 workspace_map 解析：{resolved_from_cwd}）",
              file=sys.stderr)
    elif fell_back:
        print(f"（--cwd 未匹配任何 workspace_map 条目，落到兜底项目「{fell_back}」；"
              "用 `fish workspace set` 声明后即可归到真项目）", file=sys.stderr)
    elif cwd and not getattr(args, "project", None):
        print(f"（--cwd 未匹配任何 workspace_map 条目，未开档期：{cwd}）",
              file=sys.stderr)

    # 2026-10-25 加（外部评估 `EVAL-261025-07`）：**归属证据必须与参数回显分开标注**。
    #
    # 旧输出只有一行 `project: anyi-studio`——那只是 `--project` 的**回显**，
    # 不是"归属已确定"的证据。实测时 AI 把这行当成了"已命中映射"，
    # 而它实际工作的三个目录在 workspace_map 里一条都没有（判据是空的，但看起来成功）。
    # 这正是 fish 自己总结过的「宣称不代表建成」的同款形态。
    if getattr(args, "project", None) and not resolved_from_cwd:
        print("（注意：上面的 project 是 --project 的**回显**，不是 workspace 映射命中的证据。"
              "写归属的权威源是 workspace_map）", file=sys.stderr)
    try:
        n_map = conn.execute("SELECT COUNT(*) FROM workspace_map").fetchone()[0]
        if n_map < 2:
            print(f"（workspace_map 只有 {n_map} 条映射——多目录工作时请先 "
                  f"`fish workspace add <路径> --project <前缀>`，否则写入归属没有依据）",
                  file=sys.stderr)
    except sqlite3.Error:
        pass          # 映射表还没建（极旧库）时不阻断签发

    _print_session_start(res)
    conn.close()


def cmd_session_status(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    try:
        res = session_status(conn, token=getattr(args, "token", None))
    except ValueError as e:
        conn.close()
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(2)
    _print_session_status(res)
    conn.close()


def cmd_session_end(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    try:
        res = end_session(conn, token=getattr(args, "token", None))
    except ValueError as e:
        conn.close()
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(2)
    if res["already"]:
        print(f"凭证本来已是吊销态：{res['session_key']}（公民 {res['citizen_id']}）"
              "——只补齐档期与活跃时间")
        if res["children_revoked"]:
            print(f"级联吊销子凭证：{res['children_revoked']} 条")
    else:
        print(f"已结束会话：{res['session_key']}（公民 {res['citizen_id']}）")
        print(f"吊销凭证：{res['revoked']} 条；级联吊销子凭证：{res['children_revoked']} 条")
    print(f"关闭档期：{res['stints_closed']} 个")
    conn.close()


def cmd_session_agent(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    try:
        res = issue_agent_token(conn, parent_token=getattr(args, "parent", None),
                                role=getattr(args, "role", None))
    except ValueError as e:
        conn.close()
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(2)
    # 输出字段与 start 对齐（前 4 行同构），AI 不需要两套解析逻辑。
    print(f"token: {res['token']}")
    print(f"citizen_id: {res['citizen_id']}")
    print(f"session_key: {res['session_key']}")
    print(f"evidence: {res['evidence']}")
    print(f"channel: {res['channel']}")
    print(f"role: {res['role']}")
    print(f"parent_token_hash: {res['parent_token_hash'][:12]}…")
    print(f"issued_at: {res['issued_at']}")
    print(f"expires_at: {res['expires_at'] or '永久'}")
    print("（子 agent 凭证只允许一跳：不能拿它再去 `session agent --parent`）")
    conn.close()


def cmd_report(args):
    """使用日报/周报。设计原则：一周后要回答的问题，现在就要有数据。"""
    root = Path(args.root)
    conn = connect(root)
    conn.executescript(SCHEMA)
    migrate(conn)

    days = args.days
    now = datetime.datetime.now(datetime.timezone.utc)
    since = (now - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    label = f"近 {days} 天" if days > 1 else "今日"

    # ---------- 读审计 ----------
    def read_jsonl(name):
        p = root / "_audit" / name
        out = []
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    d = json.loads(line)
                    if (d.get("ts_utc") or d.get("ts") or "") >= since:
                        out.append(d)
                except Exception:
                    pass
        return out

    calls = read_jsonl("tool-calls.jsonl")
    hooks = read_jsonl("hook-calls.jsonl")
    detours = read_jsonl("detours.jsonl")

    from collections import Counter
    by_tool = Counter(c.get("name") for c in calls if c.get("kind") == "tool")
    by_session = Counter(c.get("session_actor") or "(未标注)" for c in calls)
    fails = [c for c in calls if c.get("ok") is False]
    durs = [c["duration_ms"] for c in calls if isinstance(c.get("duration_ms"), int)]
    unreg = [c for c in calls if c.get("actor_registered") is False]

    # ---------- 读数据库 ----------
    new_mem = conn.execute("SELECT COUNT(*) FROM code_index WHERE recorded_at >= ?",
                           (since,)).fetchone()[0]
    total_mem = conn.execute("SELECT COUNT(*) FROM code_index").fetchone()[0]
    never = conn.execute("SELECT COUNT(*) FROM code_index WHERE access_count=0").fetchone()[0]
    by_type = conn.execute("SELECT mem_type, COUNT(*) FROM code_index WHERE recorded_at >= ? "
                           "GROUP BY mem_type ORDER BY 2 DESC", (since,)).fetchall()
    by_owner = conn.execute("SELECT owner, COUNT(*) FROM code_index WHERE recorded_at >= ? "
                            "GROUP BY owner ORDER BY 2 DESC", (since,)).fetchall()
    by_session_mem = conn.execute(
        "SELECT COALESCE(session_actor,'(未标注)'), COUNT(*) FROM code_index "
        "WHERE recorded_at >= ? GROUP BY 1 ORDER BY 2 DESC", (since,)).fetchall()
    hot = conn.execute("SELECT code,title,access_count FROM code_index "
                       "WHERE access_count>0 ORDER BY access_count DESC LIMIT 5").fetchall()
    feedback = conn.execute("SELECT code,title FROM code_index "
                            "WHERE tags LIKE '%fish-feedback%' ORDER BY recorded_at DESC").fetchall()
    citizens = conn.execute("SELECT display_name,kind,status FROM citizens "
                            "WHERE status='active' ORDER BY created_at").fetchall()

    # ---------- 组装 ----------
    L = [f"# fish 使用报告 · {label}",
         f"> 生成于 {datetime.datetime.now().astimezone().isoformat(timespec='seconds')}",
         f"> 统计起点 {since}（UTC）", ""]

    L += ["## 一、行为量", "",
          f"- 工具调用：**{len([c for c in calls if c.get('kind')=='tool'])}** 次"
          f"（失败 {len(fails)}）",
          f"- 资源读取：{len([c for c in calls if c.get('kind')=='resource'])} 次",
          f"- hook 执行：{len(hooks)} 次",
          f"- **绕路（直接读文件/查库）：{len(detours)} 次**"]
    total_ops = len(calls) + len(detours)
    if total_ops:
        L.append(f"- 绕路率：**{len(detours)/total_ops*100:.1f}%**"
                 f"（越低越好，说明 AI 走正门）")
    if durs:
        L.append(f"- 工具耗时：中位 {sorted(durs)[len(durs)//2]} ms，"
                 f"最慢 {max(durs)} ms")
    if unreg:
        L.append(f"- ⚠ 未登记就调用：{len(unreg)} 次（说明有会话没走 register）")
    L.append("")

    L += ["## 二、工具使用分布", ""]
    if by_tool:
        L += ["| 工具 | 次数 |", "|---|---|"]
        L += [f"| {k} | {v} |" for k, v in by_tool.most_common()]
        dead = {"remember", "recall", "neighbors", "resolve_code", "whoami",
                "register", "inject", "healthcheck"} - set(by_tool)
        if dead:
            L += ["", f"⚠ **零调用工具**：{', '.join(sorted(dead))}（可能是死代码或太难被发现）"]
    else:
        L.append("（无调用）")
    L.append("")

    L += ["## 三、会话分布", ""]
    if by_session:
        L += ["| 会话 | 调用次数 |", "|---|---|"]
        L += [f"| {k} | {v} |" for k, v in by_session.most_common()]
    else:
        L.append("（无）")
    L.append("")

    L += ["## 四、记忆", "",
          f"- 新增：**{new_mem}** 条（全库 {total_mem} 条）",
          f"- **从未被召回：{never} 条**（占 {never/total_mem*100:.0f}%；高的离谱说明只写不读）"
          if total_mem else "- （空库）"]
    if by_type:
        L.append("- 类型分布：" + " / ".join(f"{t}={n}" for t, n in by_type))
    if by_owner:
        L.append("- 归属分布：" + " / ".join(f"{o or '空'}={n}" for o, n in by_owner))
    if by_session_mem:
        L.append("- 会话分布：" + " / ".join(f"{s}={n}" for s, n in by_session_mem))
    if hot:
        L += ["", "**最常被召回**："]
        L += [f"- {c} {t}（{n} 次）" for c, t, n in hot]
    L.append("")

    L += ["## 五、AI 反馈", ""]
    if feedback:
        L += [f"- {c} {t}" for c, t in feedback]
    else:
        L.append("（这一周还没有 AI 提反馈——如果没有，说明提示词里的反馈机制没生效）")
    L.append("")

    L += ["## 六、公民", ""]
    L += [f"- {n}（{k}）" for n, k, _ in citizens]

    text = "\n".join(L)
    print(text)

    # 落盘
    if args.save:
        rd = root / "_reports"
        rd.mkdir(exist_ok=True)
        fn = rd / f"report-{datetime.date.today().isoformat()}.md"
        fn.write_text(text, encoding="utf-8")
        print(f"\n[已保存] {fn}")
    conn.close()


def cmd_config(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    if args.action == "list":
        for k in sorted(CONFIG_DEFAULTS):
            print(f"{k}\t=\t{get_config(conn, k)}")
    elif args.action == "get":
        print(f"{args.key}\t=\t{get_config(conn, args.key)}")
    else:
        try:
            v = set_config(conn, args.key, args.value)
            print(f"{args.key} = {v}")
        except (KeyError, ValueError) as e:
            print(f"错误：{e}")
    conn.close()


# ---------------------------------------------------------------- 推送度量（P1-a · E 批）
# `16` §5.5 的硬要求：**四个数字永不合并**。论文原话是 "evaluation is the gating layer"——
# 干预质量不可测之前，主动性的奖励函数无从定义。而"不能只报一个综合分"的理由更直接：
# **只报综合分，"不说话"看起来最安全**（沉默永远没有误报）。
#
# 所以四个数字各自独立，任何"加权总分"都是对这条纪律的违反：
#   ① 采纳率 acceptance —— 收益侧：推的东西有没有被用
#   ② 盲区消除率 gap_cleared —— 收益侧：推完是否真的去读了
#   ③ 违规率 violation —— **成本侧，必须单独报**：推了但打扰了工作
#   ④ 每会话方差 session_stdev —— 聚合值必须和方差一起看
#      （强模型可能靠"放弃一部分会话"刷高精度，只看均值看不出来）

GAP_CLEAR_WINDOW_DAYS = 7      # 被推后多少天内读了正文，算"盲区消除了"
VIOLATION_WINDOW_MINUTES = 5   # 工具后推送后多久内毫无动静，算"白打扰"


def push_stats(conn, days=30, now=None):
    """推送效果度量（只读）。返回四个独立指标 + 分会话明细。

    **不返回"总分"**：那会把"多推"和"推得准"混成一个数，而它们方向相反。
    空数据时各率为 `None`（不是 0）——0 表示"推了但没人理"，None 表示"没推过"，
    这两件事完全不同；混用会让"还没开始推"看起来像"推了没用"。
    """
    now_dt = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = (now_dt - datetime.timedelta(days=int(days))).strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = conn.execute(
        "SELECT session_key, channel, code, pushed_at, reaction, latency_sec "
        "FROM push_log WHERE pushed_at >= ?", (cutoff,)).fetchall()

    total = len(rows)
    accepted = sum(1 for r in rows if r[4] == "accepted")
    ignored = sum(1 for r in rows if r[4] == "ignored")
    pending = sum(1 for r in rows if r[4] is None)
    decided = accepted + ignored                    # 只拿"已判"做分母

    lat = sorted(r[5] for r in rows if r[4] == "accepted" and isinstance(r[5], int))
    median_lat = lat[len(lat) // 2] if lat else None

    # ① 采纳率
    accept_rate = (accepted / decided) if decided else None

    # ② 盲区消除率：推过之后真的读了正文
    cleared = 0
    for _sess, _ch, code, pushed_at, _re, _la in rows:
        p_dt = parse_utc(pushed_at)
        if p_dt is None or not code:
            continue
        end = (p_dt + datetime.timedelta(days=GAP_CLEAR_WINDOW_DAYS)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        hit = conn.execute(
            "SELECT 1 FROM memory_access_log WHERE code=? AND via='read_body' "
            "AND accessed_at >= ? AND accessed_at <= ? LIMIT 1",
            (code, pushed_at, end)).fetchone()
        if hit:
            cleared += 1
    gap_rate = (cleared / total) if total else None

    # ③ 违规率（成本侧）：工具后推送出去，5 分钟内该会话一次访问都没有 = 白打扰
    ta = [r for r in rows if r[1] == "tool_after"]
    violated = 0
    for sess, _ch, _code, pushed_at, _re, _la in ta:
        p_dt = parse_utc(pushed_at)
        if p_dt is None:
            continue
        end = (p_dt + datetime.timedelta(minutes=VIOLATION_WINDOW_MINUTES)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        hit = conn.execute(
            "SELECT 1 FROM memory_access_log WHERE session_key IS ? "
            "AND accessed_at >= ? AND accessed_at <= ? LIMIT 1",
            (sess, pushed_at, end)).fetchone()
        if not hit:
            violated += 1
    violation_rate = (violated / len(ta)) if ta else None

    # ④ 分会话明细 + 方差
    by_session = {}
    for sess, _ch, _code, _pushed, reaction, _la in rows:
        key = sess or "(未归属)"
        s = by_session.setdefault(key, {"total": 0, "accepted": 0, "ignored": 0,
                                        "pending": 0, "accept_rate": None})
        s["total"] += 1
        if reaction == "accepted":
            s["accepted"] += 1
        elif reaction == "ignored":
            s["ignored"] += 1
        elif reaction is None:
            s["pending"] += 1
    rates = []
    for s in by_session.values():
        d = s["accepted"] + s["ignored"]
        s["accept_rate"] = (s["accepted"] / d) if d else None
        if d:
            rates.append(s["accept_rate"])
    if len(rates) >= 2:
        mean = sum(rates) / len(rates)
        session_stdev = (sum((r - mean) ** 2 for r in rates) / len(rates)) ** 0.5
    else:
        session_stdev = None      # 样本不足 2 个会话：算不出（不是 0）

    return {
        "window_days": int(days),
        "total": total,
        "accepted": accepted,
        "ignored": ignored,
        "pending": pending,
        "decided": decided,
        "accept_rate": accept_rate,              # ① 收益侧
        "gap_cleared": cleared,
        "gap_clear_rate": gap_rate,              # ② 收益侧
        "violation": violated,
        "violation_rate": violation_rate,        # ③ 成本侧（单独一栏）
        "tool_after_total": len(ta),
        "median_latency_sec": median_lat,
        "by_session": by_session,
        "session_stdev": session_stdev,          # ④ 聚合的方差
    }


def format_push_stats(st) -> str:
    """把度量渲染成人读文本。四栏分开，**不给总分**。"""
    def pct(v):
        return "—" if v is None else f"{v * 100:.1f}%"

    sdev = "—" if st["session_stdev"] is None else f"{st['session_stdev']:.3f}"
    lat = st["median_latency_sec"]
    lines = [f"推送效果（近 {st['window_days']} 天）",
             f"  推送 {st['total']} 次：accepted {st['accepted']} / "
             f"ignored {st['ignored']} / 待判 {st['pending']}",
             "",
             "【收益侧】",
             f"  ① 采纳率      {pct(st['accept_rate'])}"
             f"（分母=已判 {st['decided']}；待判不算，避免窗口未到就拉低）",
             f"  ② 盲区消除率  {pct(st['gap_clear_rate'])}"
             f"（{st['gap_cleared']}/{st['total']} 在推后 {GAP_CLEAR_WINDOW_DAYS} 天内读了正文）",
             f"     读后延迟中位数 {'—' if lat is None else str(lat) + ' 秒'}",
             "",
             "【成本侧】（单独一栏，不与收益合并）",
             f"  ③ 违规率      {pct(st['violation_rate'])}"
             f"（工具后推送 {st['tool_after_total']} 次里，{st['violation']} 次"
             f" {VIOLATION_WINDOW_MINUTES} 分钟内毫无动静）",
             "",
             "【分布】",
             f"  ④ 每会话方差  {sdev}",
             ]
    for k, v in sorted(st["by_session"].items(), key=lambda kv: -kv[1]["total"]):
        lines.append(f"    {k}: 推 {v['total']} / 采纳 {pct(v['accept_rate'])}")
    lines.append("")
    lines.append("  ⚠ 不报总分：只报综合分会让「不说话」看起来最安全（沉默永远没有误报）。")
    return "\n".join(lines)


def cmd_push(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    try:
        if args.action == "stats":
            print(format_push_stats(push_stats(conn, days=args.days)))
        else:                                   # status
            print("push.enabled = %s" % bool(get_config(conn, "push.enabled")))
            for k in ("push.serendipity_ratio", "push.open_budget_tokens",
                      "push.after_tool_budget_tokens", "push.situational_budget_tokens",
                      "push.after_tool_every_n_calls", "push.gap_window_days"):
                print("  %-34s %s" % (k, get_config(conn, k)))
    finally:
        conn.close()


def cmd_healthcheck(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    memories = conn.execute("SELECT COUNT(*) FROM code_index WHERE status='active'").fetchone()[0]
    dormant = conn.execute("SELECT COUNT(*) FROM code_index WHERE dormant=1").fetchone()[0]
    withdrawn = conn.execute(
        "SELECT COUNT(*) FROM code_index WHERE status='withdrawn'").fetchone()[0]
    broken = []
    root = Path(args.root)
    for code, fp in conn.execute("SELECT code,file_path FROM code_index").fetchall():
        if fp and not (root / fp).exists():
            broken.append(code)
    print(f"事件 {total} 条 / 活跃记忆 {memories} 条 / 休眠 {dormant} 条 / 作废 {withdrawn} 条"
          f" / 断链 {len(broken)} 条")
    for b in broken:
        print(f"  断链：{b}")
    # 全文索引哨兵（2026-10-24 实测教训：索引落后 1 条时，只有人工比对计数才发现——
    # 那种"存进去但搜不到"是最难发现的失真，必须在自检层报出来）
    try:
        fts = conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]
    except sqlite3.Error:
        fts = -1
    if fts < 0:
        print("  ⚠ 全文索引不可用（memory_fts 不存在）——跑 fish reindex 重建")
    else:
        gap = conn.execute("SELECT COUNT(*) FROM code_index").fetchone()[0] - fts
        print(f"  全文索引 {fts} 条" + (f"  ⚠ 落后 {gap} 条（跑 fish reindex 补齐）"
                                      if gap else "（与记忆同步）"))
    # owner 必须是有效公民（07 D-13）。库里有公民知识时才检查，空库不误报。
    ids = {r[0] for r in conn.execute("SELECT citizen_id FROM citizens")}
    bad_owner = [c for c, o in conn.execute("SELECT code,owner FROM code_index")
                 if ids and o != "unknown" and o not in ids] if ids else []
    if bad_owner:
        print(f"  ⚠ owner 不是有效公民 {len(bad_owner)} 条：{', '.join(bad_owner[:5])}"
              f"（跑 fish rebuild 可归一到系统主人）")
    conn.close()


def rebuild_projection(conn, root: Path) -> dict:
    """从事件重放重建 `code_index` 投影，返回对账报告。

    为什么不是"教科书式纯重放"（三条都是取证出来的，不是偷懒）：

    1. **历史事件缺字段**：2026-10-22 之前写入的 `memory.recorded` 不含 `occurred_at`
       （见 07 D-11）。事件不可改（ADR-001），所以时间只能用重建前的投影值兜底，
       再退到正文文件 mtime，最后退到事件时间；三种兜底各记一个计数，不静默。
    2. **访问记录的来源分两段**：2026-10-22 之前，访问只 UPDATE 投影、没有事件；之后每次访问
       都写 `memory.accessed`（见 07 D-12）。所以重建策略是「**快照优先**（它是累积态，已含
       历史与事件两者的效果），快照没覆盖到的条目再从事件补」。既没有快照、又早于该日期的
       访问，仍然无从恢复——不假装能恢复。
    3. **只重建 `code_index`**：`code_registry.last_seq` 是编码分配状态（重放会把 last_seq
       归零 → `next_code` 撞号），`citizens.is_owner` 有唯一性约束且不在事件里。
       所以这两张表不参与重建。

    重建后逐条对账 owner / occurred_at / status / superseded_by / session_actor，
    不一致写进报告（对账判据见 03 §G「重建前后一致」）。
    """
    root = Path(root)
    rep = {
        "projected": 0, "superseded_events": 0, "retired_events": 0,
        "from_payload": 0, "from_projection": 0, "from_file": 0, "from_event": 0,
        "access_preserved": 0, "owner_missing": 0,
        "owner_normalized": 0, "owner_normalized_list": [], "normalized_codes": set(),
        "occurred_corrected": 0, "occurred_corrected_list": [], "corrected_codes": set(),
        "access_from_events": 0, "dormant_from_events": 0,
        "atts_from_projection": 0, "atts_degraded": 0,
        "recorded_at_normalized": 0, "recovered": [], "mismatch": [], "snapshot_size": 0,
    }

    # ---- 0. 编码改名映射（2026-10-25 数据治理）----
    # events 是 append-only（ADR-001），历史 `memory.recorded` 里存的是**旧编码**
    # （ANYI-*）；而 2026-10-25 的治理把 153 条编码改成了 anyi-studio-* / 大肥鱼生命系统-*。
    # **不应用映射 = 重建把治理结果一键撤销**，所以必须在重放前把映射建好。
    # 映射本体在 `code.renamed.batch` 事件里（`project.renamed` 只存 sha256 指纹，不够用）。
    rename_map = {}
    renamed_events = 0
    for (payload_s,) in conn.execute(
            "SELECT payload FROM events WHERE type='code.renamed.batch' ORDER BY seq"):
        try:
            m = json.loads(payload_s).get("mapping") or {}
        except (ValueError, TypeError):
            m = {}
        for _old, _new in m.items():
            if _old and _new:
                rename_map[_old] = _new
        renamed_events += 1
    rep["renamed_map_size"] = len(rename_map)
    rep["renamed_events"] = renamed_events
    rep["renamed_applied"] = 0

    # ---- 1. 重建前快照：历史事实兜底 + 访问记录的唯一来源 ----
    # 2026-10-25：快照要存**足够补回一整行**的字段。原先只存了 owner/时间/状态这些
    # "对账用"字段，于是当某条在重放时丢了（见下面 §4b 的原因），连补都补不回来。
    snap = {}
    for r in conn.execute(
            "SELECT code,owner,occurred_at,recorded_at,status,superseded_by,session_actor,"
            "attachments,last_accessed_at,access_count,dormant,"
            "title,project,mem_type,visibility,tags,file_path FROM code_index"):
        snap[r[0]] = {"owner": r[1], "occurred_at": r[2], "recorded_at": r[3],
                      "status": r[4], "superseded_by": r[5], "session_actor": r[6],
                      "attachments": r[7], "last_accessed_at": r[8],
                      "access_count": r[9] or 0, "dormant": r[10] or 0,
                      "title": r[11], "project": r[12], "mem_type": r[13],
                      "visibility": r[14], "tags": r[15], "file_path": r[16]}
    rep["snapshot_size"] = len(snap)

    conn.execute("DELETE FROM code_index")
    conn.commit()

    # owner 归一所需的知识：哪些才是真公民（见 07 D-13）。
    # 库里没有公民知识时（空库 / 测试）不做归一——不凭猜测改写归属。
    valid_citizen_ids = {r[0] for r in conn.execute("SELECT citizen_id FROM citizens")}
    sys_owner = resolve_owner(conn)

    # ---- 2. 重放 memory.recorded ----
    rows = conn.execute(
        "SELECT ts,actor,payload FROM events WHERE type='memory.recorded' ORDER BY seq"
    ).fetchall()
    for ts, actor_s, payload_s in rows:
        p = json.loads(payload_s)
        try:
            actor = json.loads(actor_s or "{}")
        except Exception:
            actor = {}
        code = p["code"]
        # 应用改名映射（2026-10-25 治理）。**必须在 `snap.get` 之前**：
        # 快照的键是新编码，用旧编码去查会查不到，于是 owner/occurred_at 的兜底链全断。
        if code in rename_map:
            code = rename_map[code]
            rep["renamed_applied"] += 1
        old = snap.get(code, {})

        # 时间事实的兜底链（2026-10-22 调整顺序）：
        #   payload → 重建前投影（**仅当它不晚于记录时间**）→ 事件时间 → 正文文件 mtime
        # 为什么要「不晚于」：occurred_at 晚于 recorded_at 在逻辑上不可能；出现就说明投影值
        # 被历史那版坏重建压平过（实证 3 条被写成重建时刻）。这种值不可信，以事件时间为准。
        occurred = p.get("occurred_at")
        if occurred:
            rep["from_payload"] += 1
        elif old.get("occurred_at") and old["occurred_at"] <= ts:
            occurred = old["occurred_at"]
            rep["from_projection"] += 1
        elif old.get("occurred_at"):
            occurred = ts
            rep["from_event"] += 1
            rep["occurred_corrected"] += 1
            rep["corrected_codes"].add(code)
            rep["occurred_corrected_list"].append(f"{code}: {old['occurred_at']} → {ts}")
        else:
            ref = p.get("body_ref") or ""
            f = (root / ref) if ref else None
            if f is not None and f.exists():
                occurred = datetime.datetime.fromtimestamp(
                    f.stat().st_mtime, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                rep["from_file"] += 1
            else:
                occurred = ts
                rep["from_event"] += 1

        owner = actor.get("citizen_id") or old.get("owner") or "unknown"
        # owner 必须是有效公民（07 D-13）。有公民知识时把无效值归一到系统主人并逐条记录，
        # 而不是把它写回投影（否则会留下指向幽灵公民的归属）。
        if valid_citizen_ids and owner not in valid_citizen_ids and owner != "unknown":
            rep["owner_normalized"] += 1
            rep["owner_normalized_list"].append(f"{code}: {owner!r} → {sys_owner!r}")
            rep["normalized_codes"].add(code)
            owner = sys_owner or "unknown"
        if owner in ("", "unknown", None):
            rep["owner_missing"] += 1

        # 项目：**编码前缀优先**；`body_ref` 只在 code 没被改名映射过时才拿来兜底。
        # 2026-10-25 修：原文是「body_ref 优先（重命名场景比按前缀切更忠实）」，但改名后
        # body_ref 是**旧路径**（`projects/ANYI/...`），而 code 已映射成 `anyi-studio-*`——
        # 于是 body_ref 把正确的 project 覆盖回 `ANYI`。副本实测：rebuild 后
        # anyi-studio 108→8、大肥鱼 55→0（几乎全部被打回旧项目），212 条挂在 ANYI 下。
        project = split_code_prefix(code)
        if code == p.get("code"):
            # code 没被映射：沿用老逻辑（历史重命名的场景 body_ref 更忠实）
            ref = p.get("body_ref") or ""
            if ref.startswith("projects/") and ref.count("/") >= 2:
                project = ref.split("/")[1]
        if not project:
            project = code.split("-")[0]

        # file_path 的取法（2026-10-25 三次迭代后的最终版）：
        #   ① **优先用快照**——它是"文件此刻真的在哪"这个唯一事实。
        #      `body_relpath` 用 title 算，而事件里的 title 是**记录时**的；
        #      重命名脚本用的是 `code_index.title`（可能被 memory.revised 改过）。
        #      两者不一致时重算会算出不同路径 → 重建即断链（副本实测 73 条）。
        #   ② 快照没有（新库/首建）时，code 没被映射就信 body_ref；
        #   ③ 都不行才用 `body_relpath` 重算。
        snap_fp = (snap.get(code) or {}).get("file_path") or ""
        if snap_fp and (root / snap_fp).exists():
            file_path = snap_fp
        elif code == p.get("code") and p.get("body_ref") and (root / p["body_ref"]).exists():
            file_path = p["body_ref"]
        else:
            file_path = body_relpath(project, code, p.get("title"))

        session_actor = p.get("session_actor", old.get("session_actor"))
        # 附件：新事件 payload 存完整元数据（含 blob 路径），旧事件只有 name+sha256。
        # 旧事件不完整时用重建前投影兜底；连投影都没有，就降级保留 name+sha256 并计数。
        atts = p.get("attachments")
        full = bool(atts) and all(isinstance(a, dict) and a.get("blob") for a in atts)
        if full:
            atts_s = json.dumps(atts, ensure_ascii=False)
        elif old.get("attachments"):
            atts_s = old["attachments"]
            rep["atts_from_projection"] += 1
        else:
            atts_s = json.dumps(atts, ensure_ascii=False) if atts else None
            if atts:
                rep["atts_degraded"] += 1

        conn.execute(
            "INSERT OR IGNORE INTO code_index (code,title,project,owner,mem_type,visibility,"
            "status,tags,file_path,occurred_at,recorded_at,session_actor,attachments) "
            "VALUES (?,?,?,?,?,?,'active',?,?,?,?,?,?)",
            (code, p.get("title"), project, owner,
             p.get("mem_type", "fact"), p.get("visibility", "shared"),
             json.dumps(p.get("tags", []), ensure_ascii=False), file_path,
             occurred, ts, session_actor, atts_s))
        rep["projected"] += 1

    # ---- 3. 重放 memory.superseded（替代链靠事件恢复，不靠猜）----
    # 2026-10-25：old/new **都要过改名映射**，否则事件里的旧编码会把映射后的条目
    # "悬空"——UPDATE 找不到行，静默失效（重建后 status 全变回 active）。
    for (payload_s,) in conn.execute(
            "SELECT payload FROM events WHERE type='memory.superseded' ORDER BY seq").fetchall():
        p = json.loads(payload_s)
        old_code, new_code = p.get("old"), p.get("new")
        if not old_code:
            continue
        old_code = rename_map.get(old_code, old_code)
        if new_code:
            new_code = rename_map.get(new_code, new_code)
        conn.execute(
            "UPDATE code_index SET status='superseded', superseded_by=? WHERE code=?",
            (new_code, old_code))
        rep["superseded_events"] += 1

    # ---- 3b. 重放 memory.retired（作废 / withdrawn，ADR-003 状态机）----
    for (payload_s,) in conn.execute(
            "SELECT payload FROM events WHERE type='memory.retired' ORDER BY seq").fetchall():
        p = json.loads(payload_s)
        code = p.get("code")
        if not code:
            continue
        code = rename_map.get(code, code)      # 2026-10-25：作废事件也要过改名映射
        if p.get("status", "withdrawn") == "withdrawn":
            conn.execute("UPDATE code_index SET status='withdrawn' WHERE code=?", (code,))
            rep["retired_events"] += 1

    # ---- 4a. 访问记录：快照优先（它是累积态，已含历史与事件两者的效果）----
    restored = set()
    for code, s in snap.items():
        if not (s["last_accessed_at"] or s["access_count"] or s["dormant"]):
            continue
        cur = conn.execute(
            "UPDATE code_index SET last_accessed_at=?, access_count=?, dormant=? WHERE code=?",
            (s["last_accessed_at"], s["access_count"], s["dormant"], code))
        if cur.rowcount:
            rep["access_preserved"] += 1
            restored.add(code)

    # ---- 4b. 快照里没有访问信息的条目：从事件补齐 ----
    # 2026-10-22 起访问与休眠都有事件来源（memory.accessed / memory.dormant），
    # 所以「投影整个被毁、没有快照」时这两类状态不再必然丢失（修 07 D-12）。
    # 只对未被快照覆盖的条目重放，避免与累积值重复计数。
    for ts, payload_s in conn.execute(
            "SELECT ts,payload FROM events WHERE type IN ('memory.accessed','memory.dormant') "
            "ORDER BY seq"):
        p = json.loads(payload_s)
        if p.get("codes"):                       # memory.accessed
            counted = bool(p.get("counted", True))
            for c in p["codes"]:
                if c in restored:
                    continue
                if counted:
                    conn.execute(
                        "UPDATE code_index SET last_accessed_at=?, dormant=0, "
                        "access_count=COALESCE(access_count,0)+1 WHERE code=?", (ts, c))
                else:
                    conn.execute(
                        "UPDATE code_index SET last_accessed_at=?, dormant=0 WHERE code=?", (ts, c))
                rep["access_from_events"] += 1
        elif p.get("code"):                      # memory.dormant
            if p["code"] in restored:
                continue
            conn.execute("UPDATE code_index SET dormant=1 WHERE code=?", (p["code"],))
            rep["dormant_from_events"] += 1
    conn.commit()

    # ---- 4b. 快照补回：事件里找不到来源的条目（2026-10-25）----
    # 为什么会有这种条目：**编码被复用过**。典型案例——
    #   seq=391  `ANYI-261025-01` 是第一棒的原始记忆
    #   seq=458  `ANYI-261025-01` 又被 ZCode 重写（同编码、不同内容）
    # 改名映射只能把 `ANYI-261025-01` 映射到**一个**新编码，于是原始那条在重放时
    # 撞上已被占用的编码、`INSERT OR IGNORE` 丢弃 → 凭空少一条。
    #
    # 处理原则：**快照是累积态**（rebuild 的 docstring 早就这么写了，owner/occurred_at
    # 的兜底也走它）。事件里还原不出来的，就用快照补回整行并计数——**不静默丢**。
    restored_from_snap = 0
    for code, s in snap.items():
        if conn.execute("SELECT 1 FROM code_index WHERE code=?", (code,)).fetchone():
            continue
        # 补回也要做 owner 归一——否则"从快照补回的条目"会绕过 07 D-13 的规则，
        # 变成 healthcheck 里永远消不掉的最后一条（2026-10-25 实测：归一 10→1，
        # 剩下的那一条正是从快照补回的）。
        r_owner = s["owner"]
        if valid_citizen_ids and r_owner not in valid_citizen_ids and r_owner != "unknown":
            rep["owner_normalized"] += 1
            rep["owner_normalized_list"].append(f"{code}: {r_owner!r} → {sys_owner!r}")
            rep["normalized_codes"].add(code)
            r_owner = sys_owner or "unknown"
        conn.execute(
            "INSERT INTO code_index (code,title,project,owner,mem_type,visibility,status,"
            "tags,file_path,occurred_at,recorded_at,session_actor,attachments,"
            "last_accessed_at,access_count,dormant,superseded_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (code, s["title"], s["project"], r_owner, s["mem_type"], s["visibility"],
             s["status"], s["tags"], s["file_path"], s["occurred_at"], s["recorded_at"],
             s["session_actor"], s["attachments"], s["last_accessed_at"],
             s["access_count"], s["dormant"], s["superseded_by"]))
        restored_from_snap += 1
        rep["recovered"].append(f"{code}: 事件里无来源，从快照补回")
    conn.commit()
    rep["restored_from_snapshot"] = restored_from_snap
    if restored_from_snap:
        print(f"  从快照补回 {restored_from_snap} 条（事件里找不到来源：编码被复用过的）")

    # ---- 4c. 重放 memory.owner_fixed（EVAL-261025-21 修复 · 2026-10-25 QA 查出）----
    # 为什么必须在 §4b（快照补回）**之后**：修正事件是对"最终归属"的权威声明，
    # 快照补回的行带的是旧 owner——先补回再覆盖，才不会让修正被快照盖掉。
    # 为什么必须在 §5 对账**之前**：对账要看到修正后的值，否则每次 rebuild
    # 都会把这条修正报成 mismatch（而且下一次还会回退）。
    # 为什么这不算"改历史"：events 一个字节不动，修正本身就是追加进来的事件；
    # 重放只是让投影忠实反映"事件流的最终状态"。
    owner_fixed_codes = set()
    owner_fixed_skipped = []
    for (payload_s,) in conn.execute(
            "SELECT payload FROM events WHERE type='memory.owner_fixed' ORDER BY seq"):
        try:
            p = json.loads(payload_s)
        except (ValueError, TypeError):
            continue
        code = p.get("code")
        new_owner = p.get("new_owner")
        if not code or not new_owner:
            continue
        code = rename_map.get(code, code)
        if valid_citizen_ids and new_owner not in valid_citizen_ids:
            # 目标公民已被归档/删除：不把归属写回投影（会成为幽灵引用），
            # 跳过并计数——不静默。
            owner_fixed_skipped.append(f"{code}: 目标公民 {new_owner!r} 不在库中，跳过")
            continue
        cur = conn.execute("UPDATE code_index SET owner=? WHERE code=?",
                           (new_owner, code))
        if cur.rowcount:
            owner_fixed_codes.add(code)
            rep["owner_fixed_applied"] = rep.get("owner_fixed_applied", 0) + 1
        else:
            owner_fixed_skipped.append(f"{code}: 重建后不存在该编码，跳过")
    conn.commit()
    rep["owner_fixed_skipped"] = owner_fixed_skipped

    # ---- 5. 对账：重建前后必须一致的五件事 ----
    for code, s in snap.items():
        row = conn.execute(
            "SELECT owner,occurred_at,status,superseded_by,session_actor,recorded_at "
            "FROM code_index WHERE code=?", (code,)).fetchone()
        if not row:
            rep["mismatch"].append(f"{code}: 重建后消失了")
            continue
        owner, occurred, status, sup, sess, rec = row
        for field, before, after in (
                ("owner", s["owner"], owner),
                ("occurred_at", s["occurred_at"], occurred),
                ("status", s["status"], status),
                ("superseded_by", s["superseded_by"], sup),
                ("session_actor", s["session_actor"], sess)):
            if field == "owner" and code in rep["normalized_codes"]:
                continue      # 已在 owner_normalized 里单独报告，不算不一致
            if field == "owner" and code in owner_fixed_codes:
                continue      # 归属被 memory.owner_fixed 显式修正过：新旧不同是预期
            if field == "occurred_at" and code in rep["corrected_codes"]:
                continue      # 已在 occurred_corrected 里单独报告，不算不一致
            # 空 → 有值 = 从事件里**恢复**了投影丢掉的字段（2026-10-22 实测发生过：
            # 旧重建把 3 条记忆的 owner 清成空串，事件里仍留着真实 actor）。这不算不一致。
            if before in (None, ""):
                if after not in (None, ""):
                    rep["recovered"].append(f"{code}: {field} 空 → {after!r}")
                continue
            if before != after:
                rep["mismatch"].append(f"{code}: {field} {before!r} → {after!r}")
        if s["recorded_at"] and s["recorded_at"] != rec:
            rep["recorded_at_normalized"] += 1

    rep["total"] = conn.execute("SELECT COUNT(*) FROM code_index").fetchone()[0]
    rep["superseded_total"] = conn.execute(
        "SELECT COUNT(*) FROM code_index WHERE status='superseded'").fetchone()[0]

    # ---- 6. 全文索引一并重建（ADR-005）：它同样是"可从原料重建"的派生物 ----
    rep["fts_indexed"] = reindex_fts(conn, root)
    return rep


def cmd_rebuild(args):
    """投影可重建：从事件重放 code_index（2026-10-22 起保真，见 07 D-10/D-11）。"""
    root = Path(args.root)
    conn = connect(root)
    conn.executescript(SCHEMA)
    migrate(conn)
    before = conn.execute("SELECT COUNT(*) FROM code_index").fetchone()[0]
    rep = rebuild_projection(conn, root)
    print(f"重建完成：{before} → {rep['total']} 条"
          f"（重放 {rep['projected']} 条记录事件、{rep['superseded_events']} 条替代事件、"
          f"{rep['retired_events']} 条作废事件）")
    print(f"  occurred_at 来源：payload {rep['from_payload']} / 投影兜底 {rep['from_projection']}"
          f" / 文件 mtime {rep['from_file']} / 事件时间 {rep['from_event']}")
    print(f"  访问记录：快照保留 {rep['access_preserved']} 条 ｜ 事件补齐 "
          f"{rep['access_from_events']} 次 / 休眠 {rep['dormant_from_events']} 条")
    if rep["occurred_corrected"]:
        print(f"  occurred_at 纠正 {rep['occurred_corrected']} 条"
              f"（投影值晚于记录时间，逻辑上不可能，改用事件时间）：")
        for m in rep["occurred_corrected_list"][:10]:
            print(f"    - {m}")
    if rep["atts_from_projection"]:
        print(f"  附件元数据从投影兜底 {rep['atts_from_projection']} 条（旧事件只存 name+sha256）")
    if rep["atts_degraded"]:
        print(f"  ⚠ 附件元数据降级 {rep['atts_degraded']} 条（无投影可兜底）")
    if rep["recorded_at_normalized"]:
        print(f"  recorded_at 归一到事件时间：{rep['recorded_at_normalized']} 条（预期内）")
    if rep["recovered"]:
        print(f"  ✓ 从事件恢复投影丢失字段 {len(rep['recovered'])} 处：")
        for m in rep["recovered"][:10]:
            print(f"    - {m}")
    if rep["owner_normalized"]:
        print(f"  owner 归一为系统主人 {rep['owner_normalized']} 条"
              f"（事件里的 actor 不是有效公民，见 07 D-13）：")
        for m in rep["owner_normalized_list"][:10]:
            print(f"    - {m}")
    if rep.get("owner_fixed_applied"):
        print(f"  ✓ 归属修正重放 {rep['owner_fixed_applied']} 条（memory.owner_fixed，EVAL-261025-21）")
    if rep.get("owner_fixed_skipped"):
        print(f"  ⚠ 归属修正跳过 {len(rep['owner_fixed_skipped'])} 条（目标公民已归档/不存在）：")
        for m in rep["owner_fixed_skipped"][:10]:
            print(f"    - {m}")
    if rep["owner_missing"]:
        print(f"  ⚠ owner 缺失 {rep['owner_missing']} 条")
    print(f"  全文索引已重建 {rep['fts_indexed']} 条（memory_fts，ADR-005）")
    if rep["mismatch"]:
        print(f"  ✗ 对账不一致 {len(rep['mismatch'])} 处：")
        for m in rep["mismatch"][:20]:
            print(f"    - {m}")
    else:
        print("  ✓ 对账一致（owner / occurred_at / status / superseded_by / session_actor）")
    conn.close()
    if rep["mismatch"]:
        raise SystemExit(1)


def cmd_reindex(args):
    """重建全文索引（ADR-005）。派生表，随时可重建，不影响事件与正文。"""
    root = Path(args.root)
    conn = connect(root)
    conn.executescript(SCHEMA)
    migrate(conn)
    before = conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]
    n = reindex_fts(conn, root)
    print(f"全文索引已重建：{before} → {n} 条（memory_fts）")
    conn.close()


def cmd_withdraw(args):
    """作废一条记忆（ADR-003 的 withdrawn：只标状态，编码不回收，文件保留）。"""
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    try:
        msg = withdraw_memory(conn, args.code, reason=args.reason or "",
                              actor=resolve_owner(conn))
    except ValueError as e:
        conn.close()
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(2)
    print(msg)
    conn.close()


def cmd_events(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    rows = conn.execute("SELECT seq,ts,type,subject FROM events ORDER BY seq DESC LIMIT ?",
                        (args.limit,)).fetchall()
    for r in rows:
        print("\t".join(str(x) for x in r))
    conn.close()


# ------------------------------------------------------------------ 看板（P1-b · 2026-10-25）
# 意义：**看板回答「此刻谁在动哪块」**——不是"发生了什么"（那是 daily）。
# 这条边界一旦破了，看板会退化成第二份 daily（`09` §4.6 的第三个已知坑）。
#
# 三条硬纪律（全部来自参照物的真实死亡）：
#   1. **无限追加必死** —— `active-sessions` v1 十天 114 条 → 396KB → 超读取上限而亡。
#      所以 `BOARD_MAX_BYTES` 是硬约束，不是建议。
#   2. **写入只走这里** —— AI 不许直接 write 渲染文件；文件是投影，可随时重建。
#   3. **只写自己行** —— 签到按 `(board_id, citizen_id)` 主键 upsert，不覆盖别人。

BOARD_SCOPES = ("root", "project", "branch")
BOARD_MAX_BYTES = 32 * 1024        # 单文件上限（`09` R5-5 的实证数字）
BOARD_MAX_POST_CHARS = 200         # 单条动态 ≤200 字（渲染时硬截断）
BOARD_POSTS_PER_FILE = 200         # 单文件动态条数上限
BOARD_DOING_MAX = 80               # "在做什么"≤80 字
BOARD_PRESENCE_IDLE_SECONDS = 30 * 60   # 30 分钟没动 = idle（渲染时算，不写库）


def board_period(period_key=None) -> str:
    """看板的周期键：按日（`YYYYMMDD`），与 `daily` 的切分口径一致。"""
    return period_key or local_date()


def board_relpath(scope, scope_key, period_key) -> str:
    """渲染文件的相对路径。**唯一入口**（同 body_relpath 的纪律：两处各拼一次必然漂移）。"""
    name = f"看板-{period_key}.md"
    if scope == "root":
        return name
    if scope == "project":
        return f"projects/{scope_key}/{name}"
    if scope == "branch":
        return f"boards/branch/{scope_key}/{name}"
    raise ValueError(f"未知 scope：{scope!r}（可用：{', '.join(BOARD_SCOPES)}）")


def board_ensure(conn, scope, scope_key="", period_key=None):
    """取或建一块板，返回 `boards` 行（sqlite3.Row 风格的 dict）。

    幂等：同一 `(scope, scope_key, period_key)` 只会有一行（UNIQUE 约束保证）。
    **不渲染**——渲染是独立一步（`board_render`），因为读路径要尽量轻。
    """
    if scope not in BOARD_SCOPES:
        raise ValueError(f"未知 scope：{scope!r}（可用：{', '.join(BOARD_SCOPES)}）")
    scope_key = scope_key or ""
    period_key = board_period(period_key)
    path = board_relpath(scope, scope_key, period_key)

    row = conn.execute(
        "SELECT id, scope, scope_key, period_key, path, status, created_at, closed_at "
        "FROM boards WHERE scope=? AND scope_key=? AND period_key=?",
        (scope, scope_key, period_key)).fetchone()
    if row:
        return dict(zip(("id", "scope", "scope_key", "period_key", "path", "status",
                         "created_at", "closed_at"), row))
    now = now_utc()
    cur = conn.execute(
        "INSERT INTO boards (scope, scope_key, period_key, path, status, created_at) "
        "VALUES (?,?,?,?, 'open', ?)", (scope, scope_key, period_key, path, now))
    conn.commit()
    return {"id": cur.lastrowid, "scope": scope, "scope_key": scope_key,
            "period_key": period_key, "path": path, "status": "open",
            "created_at": now, "closed_at": None}


def board_checkin(conn, citizen_id, doing="", surface="", scope="project",
                  scope_key="", period_key=None, agent_id=None, status="active"):
    """签到：登记"我来了、我在做什么、我占了什么面"，返回看板行。

    **只写自己行**（`(board_id, citizen_id)` 主键 upsert）——这是并发协议，不是礼貌。
    写前不需要读整块板：upsert 本身是原子的，`first_seen_at` 用 COALESCE 保住首签时间。

    为什么 `doing` 必填（哪怕只给空字符串也记一行）：看板的核心价值就是
    **别人靠这一句判断"我会不会跟它撞车"**。没有 doing 的在场行等于没签到。
    """
    board = board_ensure(conn, scope, scope_key, period_key)
    if board["status"] == "closed":
        raise ValueError(f"看板已归档（closed），不再接收签到：{board['path']}")
    now = now_utc()
    doing = (doing or "").strip()[:BOARD_DOING_MAX]
    surface = (surface or "").strip()
    conn.execute(
        "INSERT INTO presence (board_id, citizen_id, agent_id, status, doing, surface, "
        "first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(board_id, citizen_id) DO UPDATE SET "
        "  agent_id=excluded.agent_id, status=excluded.status, doing=excluded.doing, "
        "  surface=excluded.surface, last_seen_at=excluded.last_seen_at",
        (board["id"], citizen_id, agent_id, status, doing, surface, now, now))
    conn.commit()
    return board


def board_post(conn, board, citizen_id, kind, text):
    """往看板"今日动态"加一条（≤200 字，超长硬截断并标省略号）。

    `kind`：checkin | progress | done | note
    为什么动态单独一张表而不复用 `events`：events 是**不可变事实流**，
    而看板动态是**可滚动的展示层**——把展示层塞进事实流，
    会让"重建投影"多一类要处理的事件（`rebuild_projection` 已经够复杂了）。
    """
    text = (text or "").strip()
    if len(text) > BOARD_MAX_POST_CHARS:
        text = text[:BOARD_MAX_POST_CHARS - 1] + "…"
    if not text:
        return None
    cur = conn.execute(
        "INSERT INTO board_posts (board_id, citizen_id, kind, text, created_at) "
        "VALUES (?,?,?,?,?)", (board["id"], citizen_id, kind, text, now_utc()))
    conn.commit()
    return cur.lastrowid


def board_checkin_or_remind(conn, citizen_id, project=None):
    """写操作前的软门：返回 `(已签到: bool, 提示语: str|None)`。

    **软门不硬拦**（用户 2026-10-25 拍板）：
    硬拦会让 AI 为了写记忆先签到——仪式感对了，但更容易被"先关掉"绕过。
    软门的做法是**照写不误，但在返回里带一句提示**，让它在下一轮注意到。

    ## 2026-10-25 修两处判定错位（`EVAL-261025-14`）

    **① 跨项目误报。** 旧实现只看 `project`（**写记忆的项目**）。
    外部评估的现场：19:16 在 `anyi-studio` 签到 → 19:17 写 `EVAL` 的记忆 →
    回执说「你还没签到」。而"在 A 项目开工、往 B 项目写反馈"是**常态**，
    于是每次跨项目写都必误报一次。
    → 改成**今天任一板上有活跃行就算已签到**。跨项目写记忆不该被当成异常。

    **② 签到一次顶一天。** 旧实现 `if row: return True, None`，**完全不看时间**，
    而本函数的 docstring 写的是"30 分钟内活跃"——实现比规格宽。
    后果：09:00 签一次，到 21:00 都不再提醒，而看板上 `doing` 已经 12 小时没更新，
    这与看板"回答**此刻**"的定位直接矛盾。
    → 真正实现时间窗，并把两种语义**分开说**（后者比前者更有用、也不像训话）：
      · 没行       → "你还没签到"
      · 有行但超窗 → "你的 doing 已 N 分钟没更新"
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    rows = conn.execute(
        "SELECT p.last_seen_at, p.doing, b.scope, b.scope_key "
        "FROM presence p JOIN boards b ON b.id = p.board_id "
        "WHERE p.citizen_id=? AND b.period_key=? AND p.status != 'left'",
        (citizen_id, board_period())).fetchall()
    if not rows:
        return False, ("你还没签到——看板上没有你的行。"
                       f"用 `fish board checkin --doing \"在做什么\" --project {project or '(根)'}` 登记，"
                       "别人才能判断会不会跟你撞车。")

    best = None                       # (minutes, doing, scope, scope_key)
    for last, doing, sc, sk in rows:
        mins = 0
        if last:
            try:
                t = datetime.datetime.strptime(
                    last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
                mins = int((now - t).total_seconds() // 60)
            except ValueError:
                mins = 0              # 时间戳坏了 → 当刚签过（宁可少提醒）
        if best is None or mins < best[0]:
            best = (mins, doing or "", sc, sk)
    mins, doing, sc, sk = best
    if mins <= BOARD_PRESENCE_IDLE_SECONDS // 60:
        return True, None
    where = ("根看板" if sc == "root" else f"{sc}:{sk}")
    return False, (
        f"你的 doing 已 {mins} 分钟没更新（{where} 上写的是「{doing or '未写'}」）。"
        "如果还在做同一件事，重跑一次 checkin 刷新一下；"
        "做完了就用 `board(action=\"post\", kind=\"done\", text=\"…\")` 标掉——"
        "否则看板会一直显示一件早就做完的事。")


def board_auto_presence(conn, citizen_id, project=None):
    """`remember` 时的**兜底 presence**：让看板不再"0 人"。

    ## 为什么要有它（`EVAL-261025-16` 建议 3）

    > 很多人不 checkin 不是因为不愿意，是**记不住**。如果 `remember` 或写入时
    > 能自动带一条极简 presence（甚至只是"某公民今天有活动"），看板就不会是 0 人。

    ## 三条红线（把它做对，而不是做成"幽灵行"）

    1. **只在无行时写**（`ON CONFLICT DO NOTHING`）——
       **绝不覆盖别人声明的 `doing`**。覆盖等于把"我刚在做 X"改成"自动登记"，那是毁信息
    2. **`doing` 明确标注来源**：`（自动登记：正在写记忆）`。
       让人一眼看出这不是谁主动声明的，避免把自动行当成真实意图
    3. **`surface` 留空**——**不编造占用面**。
       编造的 surface 会制造假重叠，而重叠警告一旦掺假就会被学会忽略
       （那正是 `EVAL-261025-13` 刚教过我们的）

    ## 返回值

    `True` = 这次真的插了一条；`False` = 已有人在（没动）。
    """
    scope, key = ("project", project) if project else ("root", "")
    try:
        board = board_ensure(conn, scope, key)
        if board["status"] == "closed":
            return False
        cur = conn.execute(
            "INSERT INTO presence (board_id, citizen_id, agent_id, status, doing, "
            "surface, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(board_id, citizen_id) DO NOTHING",
            (board["id"], citizen_id, None, "active",
             "（自动登记：正在写记忆）", "", now_utc(), now_utc()))
        conn.commit()
        return bool(cur.rowcount)
    except (sqlite3.Error, ValueError):
        return False          # 兜底是增强项，失败不该把 remember 打挂


def _fmt_period(pk) -> str:
    """周期键 `261025` → 人能读的 `2026-10-25`（看板标题上要让人一眼看懂是哪天）。"""
    pk = str(pk or "")
    if len(pk) == 6 and pk.isdigit():
        return f"20{pk[:2]}-{pk[2:4]}-{pk[4:6]}"
    return pk


def _hhmm(ts) -> str:
    """UTC 时间戳 → 本地 HH:MM（看板只显示到分钟，人看得懂就够）。"""
    if not ts:
        return "—"
    try:
        dt = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return str(ts)[11:16] or "—"
    return (dt + datetime.timedelta(hours=8)).strftime("%H:%M")


# ---------------------------------------------------------- 占用面：归一化与交集（2026-10-25）
# 外部评估 `EVAL-261025-09`：「surface 只声明不消费」——
# 两行 presence 都声明了 `p0/fish.py`，而 show 输出**无任何提示**。
# 全文件搜 `撞车|冲突|overlap` 只命中文案，**没有任何一处做交集比对**。
# 而「防撞车是这个功能存在的唯一理由」（工具描述原话），所以这是功能核心缺失，
# 不是体验瑕疵——它给人一个**假的安全感**，比没有更糟。

def who_label(row) -> str:
    """把一个在场行渲染成**人能认的标签**：`名字（短 id）`。

    2026-10-25（`EVAL-261025-16`）：入场表原来只有 ULID，人认不出是谁。
    规则：
      · 有 `display_name` → `codebuddy-8a83（17929164）`
        （名字优先，但**保留短 id**——同名时仍能区分，且不妨碍照它去查库）
      · 没有名字（老数据 / 匿名）→ 只用短 id，不编造
    为什么保留短 id 而不是只留名字：看板的核心动作是"找到那个人去协调"，
    而 ULID 是唯一不会撞的键；只留名字会在重名时失效。
    """
    try:
        cid = row["citizen_id"] if isinstance(row, dict) else row[0]
        name = (row.get("display_name") if isinstance(row, dict) else None) or ""
    except (KeyError, IndexError, TypeError):
        return str(row)
    if cid == "ASSUMED":
        return "（simulate 假设的人）"
    short = cid[:8] if cid else "?"
    return f"{name}（{short}）" if name else short


_SURFACE_SEP = re.compile(r"[,，;；\n]")


def normalize_surface(surface) -> list:
    """把 `surface` 归一化成**排序去重的前缀列表**。

    规则（`EVAL-261025-09` 建议 3）：小写、正斜杠、去首尾空白、去尾斜杠、去重排序。
    为什么必须归一化：`p0/fish.py` 与 `p0/fish.py `（尾空格）现在算两回事，
    而两侧一旦漂了无人发现——那正是"声明了但防不住"的最隐蔽形态。
    """
    out = []
    for part in _SURFACE_SEP.split(surface or ""):
        s = (part or "").strip().replace("\\", "/").lower().rstrip("/")
        if s and s not in out:
            out.append(s)
    return sorted(out)


def surface_hit(a, b):
    """两个归一化前缀是否**命中**（含目录前缀语义），命中返回共同前缀。

    为什么不是简单相等（`EVAL-261025-09` 建议 4）：
    声明 `backend/routes` 应能命中 `backend/routes/tasks.py`。
    要求调用方列全所有文件是做不到的——**列全本身很容易漏**，
    而漏掉的恰好是"我以为没人在动"的那块。

    边界纪律：按**路径分量**判前缀，不做字符串前缀——
    `a/b` 不该命中 `a/bc`（那是两条不同的路，字符串前缀会误报）。
    """
    if not a or not b:
        return None
    if a == b:
        return a
    if b.startswith(a + "/"):
        return a
    if a.startswith(b + "/"):
        return b
    return None


def board_overlaps(conn, board, *, window_min=None) -> list:
    """在场者之间的占用面**重叠清单**（两两比对，**只算有效行**）。

    返回 `[{"a": 公民A, "b": 公民B, "at": 共同前缀}]`。

    ## 2026-10-25 修（`EVAL-261025-13`）：必须过滤"已离开/已闲置"的行

    外部评估抓到的现场：两行 presence 都是 idle（16:13 / 18:22，当时 19:11），
    看板仍报「⚠ 占用面重叠（**开工前先协调**）」——而"开工前先协调"这句话是错的，
    两人都已走开数十分钟到数小时。

    旧实现这里**完全没有 last_seen / status 过滤**，而 `board_presence_rows()`
    会算 idle（30 分钟）——**两处对"有效行"的理解不一致**，于是警告掺假。

    ## 为什么"警告掺假"比"漏报"更严重

    与"未登记 = 未发生"是同一个病：**警告一旦掺假就会被学会忽略**。
    真实冲突只有 1 处时，满屏历史冲突会把它淡化——而这个功能的全部价值
    就是让那 1 处被看见。

    ## 窗口口径

    **90 分钟**（`board.overlap_window_min`），比 idle 阈值（30 分钟）宽：
      · 漏掉一个刚走开的人 → 代价是**撞车**
      · 多报一个走开一小时的人 → 代价只是一句多余的话
    不对称的代价，就该用不对称的阈值。
    """
    window = int(window_min if window_min is not None
                 else get_config(conn, "board.overlap_window_min"))
    now = datetime.datetime.now(datetime.timezone.utc)
    people = []
    for p in board_presence_rows(conn, board):
        if p["status"] == "left":
            continue                      # 明确下板的人：不算重叠
        fresh = True
        if p["last_seen_at"]:
            try:
                t = datetime.datetime.strptime(
                    p["last_seen_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=datetime.timezone.utc)
                fresh = (now - t).total_seconds() <= window * 60
            except ValueError:
                fresh = True              # 时间戳坏了 → 宁可算它有效（不静默漏报）
        if not fresh:
            continue
        people.append({"citizen_id": p["citizen_id"], "doing": p["doing"],
                       "surface": normalize_surface(p["surface"])})

    out = []
    seen = set()
    for i in range(len(people)):
        for j in range(i + 1, len(people)):
            a, b = people[i], people[j]
            for pa in a["surface"]:
                for pb in b["surface"]:
                    hit = surface_hit(pa, pb)
                    if hit:
                        # **按 (a, b, hit) 去重**：B 声明目录 `p0` 时，它会同时命中
                        # A 的 `p0/fish.py` 与 `p0/test_board.py`，而两次的共同前缀
                        # 都是 `p0`——不去重就会把同一条重叠打印两遍
                        # （2026-10-25 端到端实测抓到）。对读者来说"说了两遍"
                        # 等于"有两处冲突"，那是错误信息。
                        key = (a["citizen_id"], b["citizen_id"], hit)
                        if key in seen:
                            continue
                        seen.add(key)
                        out.append({"a": a["citizen_id"], "b": b["citizen_id"],
                                    "at": hit})
    return out


def board_simulate(conn, board, surface, *, exclude_citizen=None,
                   assume_surface=None, assume_label="（假设的人）"):
    """**模拟**"如果我占用这些面，会跟谁撞"——让撞车检测可自助验证。

    ## 为什么需要（`EVAL-261025-16` 建议 2）

    > 只有一个会话时，怎么 checkin 都不会触发撞车警告。**没有第二个会话就无法
    > 验证这个核心卖点是否真的工作**。这类功能最危险的状态就是"以为有保护"。

    这就是它存在的理由：**让一个 AI 在没有第二个人在场时，也能当场看到
    "撞车警告长什么样"，从而确认这条防线是真的**。

    ## 与真 checkin 的区别

      · **不写库**（不插 presence、不写动态、不渲染文件）——纯计算
      · 可以传 `exclude_citizen` 把自己排除掉（否则自己跟自己撞）

    ## `assume_surface`：单人也能验证（这是关键）

    第一版只对比**真实在场行**，结果一个人在场时"对比了 0 个有效在场行"——
    **还是验证不了**，而 `EVAL-261025-16` 的原话就是"没有第二会话就无法验证"。
    所以补一个假设档：`assume_surface` 直接注入一个**虚拟占用者**，
    走的是**同一套 `surface_hit`**——所以它证明的是真判定逻辑，不是另一条假路径。
    """
    mine = normalize_surface(surface)
    if not mine:
        return {"hits": [], "stale": [], "checked": 0}
    window = int(get_config(conn, "board.overlap_window_min"))
    now = datetime.datetime.now(datetime.timezone.utc)
    others = []
    for p in board_presence_rows(conn, board):
        if p["status"] == "left":
            continue
        if exclude_citizen and p["citizen_id"] == exclude_citizen:
            continue
        fresh = True
        if p["last_seen_at"]:
            try:
                t = datetime.datetime.strptime(
                    p["last_seen_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=datetime.timezone.utc)
                fresh = (now - t).total_seconds() <= window * 60
            except ValueError:
                fresh = True
        if fresh:
            others.append({**p, "fresh": True})
    if assume_surface:
        # 虚拟占用者：走**同一套** surface_hit，所以验证的是真逻辑
        others.append({"citizen_id": "ASSUMED", "display_name": assume_label,
                       "doing": "假设的占用者",
                       "surface": (assume_surface or "").strip()})
    hits = []
    for p in others:
        for their in normalize_surface(p["surface"]):
            for mine_one in mine:
                hit = surface_hit(mine_one, their)
                if hit:
                    hits.append({"at": hit, "who": who_label(p),
                                 "doing": p["doing"], "surface": their})
                    break
    return {"hits": hits, "checked": len(others), "my_surface": mine}


def board_stale_rows(conn, board, *, window_min=None) -> list:
    """**仍在声明占用、但已经超出有效窗口**的残留行（单独提示，不叫"冲突"）。

    外部评估 `EVAL-261025-13` 的建议 3：

    > 残留行不要默默丢掉，单独一行轻提示（换个词，不叫"冲突"）：
    > `另有 N 行已闲置（>90min）仍声明占用：…`
    > 这样既不误报、又不丢信息。
    """
    window = int(window_min if window_min is not None
                 else get_config(conn, "board.overlap_window_min"))
    now = datetime.datetime.now(datetime.timezone.utc)
    stale = []
    for p in board_presence_rows(conn, board):
        if p["status"] == "left" or not p["last_seen_at"]:
            continue
        try:
            t = datetime.datetime.strptime(
                p["last_seen_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
        mins = int((now - t).total_seconds() // 60)
        if mins > window:
            stale.append({"citizen_id": p["citizen_id"], "doing": p["doing"],
                          "surface": normalize_surface(p["surface"]),
                          "minutes": mins})
    return stale


def board_prev_note(conn, board, limit=3) -> str:
    """**昨日未归档板的最后 N 条动态**（跨午夜可见性）。

    外部评估 `EVAL-261025-11` 的原话：

    > 我们经常 20:00 干到 03:00，一条线跨两块板。后半夜开工的人看不到前半夜
    > 谁在动什么；前一天的行要等 `sweep` 归档后更难找。

    这是"按日切板"这个设计的固有代价。**不改成滑动窗口**的理由：按日切让
    "今天"这个语义干净（`daily`/根板/归档都依赖它），改成 12 小时滑窗会把
    "看板-YYYYMMDD" 这个文件名契约弄乱。**代价用一个提示补上**就够了——
    只要后半夜的人知道"去看看昨天那块板"，断片就不成立。

    只在**昨天那块板还没归档**时才提（sweep 之后它是历史，不该再冒出来）。
    """
    try:
        d = datetime.datetime.strptime("20" + str(board["period_key"]), "%Y%m%d").date()
    except (ValueError, TypeError):
        return ""
    prev = (d - datetime.timedelta(days=1)).strftime("%y%m%d")
    row = conn.execute(
        "SELECT id FROM boards WHERE scope=? AND scope_key=? AND period_key=? "
        "AND status='open'", (board["scope"], board["scope_key"], prev)).fetchone()
    if not row:
        return ""
    posts = conn.execute(
        "SELECT citizen_id, kind, text, created_at FROM board_posts "
        "WHERE board_id=? ORDER BY id DESC LIMIT ?", (row[0], limit)).fetchall()
    if not posts:
        return ""
    lines = [f"（昨日板 {_fmt_period(prev)} 仍开着，最后 {len(posts)} 条——"
             "跨午夜开工的话先看这里：）"]
    for cid, kind, txt, ts in reversed(posts):
        lines.append(f"  · {_hhmm(ts)} {cid} {kind}：{txt[:60]}")
    return "\n".join(lines)


def board_presence_rows(conn, board):
    """在场表：谁在这块板上、在做什么。

    `status` 的 **idle 是渲染时算的**，不写库：写库需要定时任务，
    而"30 分钟没动"这种判断随时可以重算——把可重算的派生态写进库，
    只会制造"库里的值和真实情况不一致"的新问题。
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    out = []
    # 2026-10-25（`EVAL-261025-16`）：**joins citizens 拿可读名**。
    # 之前只显示 ULID（如 `17929164334384A67C1B1F95C4`），而 `whoami` 里它叫
    # `codebuddy-8a83d218-8b2`——人每次都要拿 ULID 去对照才知道是谁。
    for r in conn.execute(
            "SELECT p.citizen_id, p.agent_id, p.status, p.doing, p.surface, "
            "p.last_seen_at, c.display_name, c.kind "
            "FROM presence p LEFT JOIN citizens c ON c.citizen_id = p.citizen_id "
            "WHERE p.board_id=? ORDER BY p.last_seen_at DESC", (board["id"],)):
        cid, agent, st, doing, surface, last, dname, ckind = r
        idle = False
        if last:
            try:
                t = datetime.datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=datetime.timezone.utc)
                idle = (now - t).total_seconds() > BOARD_PRESENCE_IDLE_SECONDS
            except ValueError:
                pass
        out.append({"citizen_id": cid, "agent_id": agent,
                    "display_name": dname or "", "kind": ckind or "",
                    "status": "idle" if (idle and st == "active") else st,
                    "doing": doing or "", "surface": surface or "", "last_seen_at": last})
    return out


def board_post_rows(conn, board, limit=BOARD_POSTS_PER_FILE):
    """今日动态：按时间**倒序**（最新的在最上面，符合"此刻"的语义）。"""
    return conn.execute(
        "SELECT citizen_id, kind, text, created_at FROM board_posts "
        "WHERE board_id=? ORDER BY id DESC LIMIT ?", (board["id"], limit)).fetchall()


def _render_one(conn, board, posts, heading_suffix=""):
    """渲染一块板的 markdown 正文（不含容量判断）。"""
    scope_label = {"root": "fish 全局", "project": board["scope_key"] or "?",
                   "branch": f"开发树 {board['scope_key']}"}[board["scope"]]
    pres = board_presence_rows(conn, board)
    lines = [
        f"# {scope_label} · 看板 {_fmt_period(board['period_key'])}{heading_suffix}",
        "",
        "> 本文件由 `fish board render` 生成，**请勿手工编辑**（改会被下一次渲染覆盖）。",
        "> 真相在 `boards` / `presence` / `board_posts` 三张表；删了本文件不影响正确性，重跑即重建。",
        f"> 状态：{board['status']}" + (f" ｜ 归档于 {board['closed_at']}" if board["closed_at"] else ""),
        "",
        f"## 在场（{len(pres)} 人）",
        "",
    ]
    if pres:
        lines += ["| 会话 | 状态 | 在做什么 | 占用面 | 最后活跃 |", "|---|---|---|---|---|"]
        for p in pres:
            doing = (p["doing"] or "—").replace("|", "\\|")
            surf = (p["surface"] or "—").replace("|", "\\|")
            lines.append(f"| {who_label(p)} | {p['status']} | {doing} | `{surf}` "
                         f"| {_hhmm(p['last_seen_at'])} |")
    else:
        lines.append("_（还没有人签到——**未登记 = 未发生**）_")

    # 占用面重叠警告（2026-10-25 · `EVAL-261025-09`）：
    # 这是把"声明"变成"防线"的那一步。在此之前 surface 只是被存起来，
    # 防撞车全靠人肉比字符串——而那一步我们历史上死过一次（EXP-17）。
    overlaps = board_overlaps(conn, board)
    if overlaps:
        lines += ["", "### ⚠ 占用面重叠（开工前先协调）", ""]
        for ov in overlaps[:10]:
            lines.append(f"- `{ov['at']}` ← {ov['a']} ↔ {ov['b']}")
        if len(overlaps) > 10:
            lines.append(f"- …共 {len(overlaps)} 处重叠")
    # 残留行（已闲置超时但仍声明占用）：不叫"冲突"，也不无痕删除
    stale = board_stale_rows(conn, board)
    if stale:
        lines += ["", f"### 已闲置但仍声明占用（{len(stale)} 行，**不算重叠**）", ""]
        for s in stale[:10]:
            lines.append(f"- {s['citizen_id']}（{s['minutes']} 分钟未动，"
                         f"doing「{s['doing']}」）")

    lines += ["", f"## 今日动态（{len(posts)} 条，最新在上）", ""]
    if posts:
        for cid, kind, text, ts in posts:
            lines.append(f"- **{_hhmm(ts)}** {cid} · {kind}：{text}")
    else:
        lines.append("_（今天还没有动态）_")
    # 跨午夜可见性（`EVAL-261025-11`）：昨天那块板还开着就提一句
    prev_note = board_prev_note(conn, board)
    if prev_note:
        lines += ["", prev_note]
    lines.append("")
    return "\n".join(lines)


def _pack_by_bytes(conn, board, posts, budget):
    """按**字节预算**装动态（从最新开始），返回 `(装得下的, 装不下的)`。

    为什么不能按条数或按小时切：单条最长 200 字（中文 3 字节 ≈ 600 B），
    100 条一小时就 42KB——**按小时切根本救不了**（2026-10-25 的 B5 判据实测抓到）。
    只有按字节装才是真的守住了 32KB 这条线。
    """
    kept, used = [], 0
    for i, p in enumerate(posts):
        est = len((p[2] or "").encode("utf-8")) + 80    # 80 ≈ 时间戳+公民+kind 的余量
        if used + est > budget and kept:
            return kept, posts[i:]
        kept.append(p)
        used += est
    return kept, []


def board_render_root(conn, root, period_key=None):
    """把今天所有**有效**子板聚合成根板（全局视图）。

    ## 三条设计约束（用户 2026-10-25 明确要求）

    1. **随子板刷新而刷新**：由 `board_render()` 在末尾调用——那是子板刷新的
       **唯一出口**（签到 / 发动态 / 手动 render 都经过它）。挂在别处就一定会漏。
    2. **只显示"有效"看板**：`period_key == 今天` **且** `status == 'open'`。
       为什么必须带日期条件：看板按日切，而**没有任何东西会关旧板**——
       不带日期的话，根板会一天比一天长，最后变成一张"历史板列表"。
    3. **根板自己刷新不递归**：`scope='root'` 不参与聚合（否则自己把自己装进去）。

    ## 它不推进给 AI 的原因（`09` R5-6）

    `board_block()` 只给**最近一级**（项目级优先），三级全推会重复。
    根板的价值是**给人看的运维视角**：所有项目今天谁在动。
    """
    pk = board_period(period_key)
    rows = conn.execute(
        "SELECT id, scope, scope_key, status FROM boards "
        "WHERE period_key=? AND scope!='root' AND status='open' "
        "ORDER BY scope, scope_key", (pk,)).fetchall()

    today = _fmt_period(pk)
    lines = [
        f"# fish 全局 · 看板 {today}",
        "",
        "> 本文件由 `fish board render` 生成，**请勿手工编辑**（改会被下一次渲染覆盖）。",
        "> 真相在 `boards` / `presence` / `board_posts` 三张表；删了本文件不影响正确性。",
        f"> 只列**今天（{today}）且未归档**的看板——过期的旧板不会出现在这里。",
        "",
        f"## 今日有效看板（{len(rows)} 块）",
        "",
    ]
    total_people = 0
    if rows:
        lines += ["| 看板 | 在场 | 在做什么 | 最新动态 |", "|---|---|---|---|"]
        for bid, scope, key, _st in rows:
            board = {"id": bid, "scope": scope, "scope_key": key,
                     "period_key": pk, "status": "open", "created_at": None,
                     "closed_at": None, "path": ""}
            pres = board_presence_rows(conn, board)
            posts = conn.execute(
                "SELECT kind, text, created_at FROM board_posts "
                "WHERE board_id=? ORDER BY id DESC LIMIT 1", (bid,)).fetchone()
            total_people += len(pres)
            who = "、".join(p["citizen_id"] for p in pres[:2]) + (
                "…" if len(pres) > 2 else "") if pres else "—"
            label = key if scope == "project" else f"{scope}:{key}"
            doing = (pres[0]["doing"] if pres else "") or "—"
            last = f"{_hhmm(posts[2])} {posts[0]}：{posts[1][:24]}" if posts else "—"
            lines.append(f"| `{label}` | {len(pres)} 人（{who}） | {doing} | {last} |")
    else:
        lines.append("_（今天还没有任何项目开工——**未登记 = 未发生**）_")
    lines += ["", f"合计：{len(rows)} 块有效看板，{total_people} 人次在场。", ""]

    board = board_ensure(conn, "root", "", pk)
    text = "\n".join(lines)
    p = Path(root) / board["path"]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return board["path"], len(text.encode("utf-8")), len(rows)


def board_sweep(conn, root=None, days=None):
    """把 N 天前的 open 看板自动归档（**只改 status，不删任何东西**）。

    ## 为什么需要它（用户 2026-10-25 要求 C）

    看板按日切 `period_key`，而**没有任何东西会关旧板**——`boards` 表里 open 板会
    日复一日累积。根板虽然只列今天（过滤掉了），但那是**掩盖**，不是**解决**。

    ## 三条纪律

    1. **只关 open**：已 closed 的不动（`closed_at` 是第一次归档的时刻，不该被刷新）
    2. **只按日期**：`period_key < cutoff`。`period_key` 是 `YYMMDD` 定长字符串，
       字典序 == 时间序，可以直接比（这是选 `YYMMDD` 而不是 `YYYY-MM-DD` 的额外好处）
    3. **不删**：`R5-7` 的"不合并、不压缩、不删除"。归档之后照样能 `board show` 读到
    """
    days = int(days if days is not None else get_config(conn, "board.sweep_days"))
    if days < 1:
        return {"swept": 0, "cutoff": None, "closed": []}
    today = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=8))).date()
    # **语义：`--days N` = "保留最近 N 天"**（含今天）。
    # 所以 7 天 = 10/19..10/25，**10/18 及更早归档** → cutoff 就是今天-N，用 `<=`。
    # 为什么不是 `<`：那样 10/18 会留在边界外（"保留 8 天"），
    # 而 README 级的预期是"保留 7 天"。2026-10-25 实测抓到：`--days 7` 归档 0 块。
    cutoff = (today - datetime.timedelta(days=days)).strftime("%y%m%d")
    now = now_utc()
    rows = conn.execute(
        "SELECT id, scope, scope_key, period_key FROM boards "
        "WHERE status='open' AND period_key <= ?", (cutoff,)).fetchall()
    closed = []
    for bid, scope, key, pk in rows:
        conn.execute("UPDATE boards SET status='closed', closed_at=? WHERE id=?",
                     (now, bid))
        closed.append({"id": bid, "scope": scope, "scope_key": key, "period_key": pk})
    conn.commit()
    # 归档会改变"有效看板"集合 → 根板跟着刷
    if root is not None and closed:
        try:
            board_render_root(conn, root)
        except Exception:      # noqa: BLE001 —— 根板是聚合视图，失败不该阻断 sweep
            pass
    return {"swept": len(closed), "cutoff": cutoff, "closed": closed}


def board_render(conn, root, board):
    """把看板渲染成文件，返回 `(相对路径, 字节数, 是否切分)`。

    **容量纪律（硬）**：单文件 32KB。超了按字节预算切开——
    最新的那份写主文件 `看板-YYYYMMDD.md`，更早的写 `看板-YYYYMMDD-<n>.md`，
    主文件顶部留指针。`active-sessions` v1 就是死在"永远追加不切分"上
    （10 天 114 条 → 396KB → 超读取上限），这个判据不许放松。
    """
    root = Path(root)
    posts = board_post_rows(conn, board, limit=BOARD_POSTS_PER_FILE)
    presence_only = len(_render_one(conn, board, []).encode("utf-8"))
    # 主文件预算 = 上限 - 在场表基线 - 指针句余量
    budget = max(1024, BOARD_MAX_BYTES - presence_only - 400)
    kept, rest = _pack_by_bytes(conn, board, posts, budget)

    shard_paths = []
    if rest:
        # 分片：同样按字节装，每片一个 `-<n>` 后缀
        shard_no, cur = 1, rest
        while cur:
            part, cur = _pack_by_bytes(conn, board, cur, budget)
            if not part:          # 单条就超预算：硬放一条，避免死循环
                part, cur = cur[:1], cur[1:]
            shard = dict(board)
            shard["path"] = board["path"][:-3] + f"-{shard_no}.md"
            p = root / shard["path"]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(_render_one(conn, shard, part, f"（更早第 {shard_no} 片）"),
                         encoding="utf-8")
            shard_paths.append(shard["path"])
            shard_no += 1

    suffix = ""
    if shard_paths:
        suffix = (f"（只列最新；更早有 {len(shard_paths)} 片："
                  + "、".join(os.path.basename(x) for x in shard_paths) + "）")
    text = _render_one(conn, board, kept, suffix)
    p = root / board["path"]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")

    # 2026-10-25（用户要求）：**随子板刷新而刷新根板**。
    # 挂在这里是刻意的——`board_render` 是子板刷新的**唯一出口**
    # （签到 / 发动态 / 手动 render 三条路都经过它），挂别处就一定会漏掉几条。
    # 用 try 兜住：根板是**聚合视图**，它失败不该拖垮子板那次渲染。
    if board.get("scope") != "root":
        try:
            board_render_root(conn, root, board["period_key"])
        except Exception:       # noqa: BLE001 —— 见上
            pass

    return board["path"], len(text.encode("utf-8")), bool(shard_paths)


def board_show(conn, scope, scope_key="", period_key=None):
    """读一块板（不渲染）：返回板信息 + 在场 + 动态。"""
    board = board_ensure(conn, scope, scope_key, period_key)
    return {"board": board, "presence": board_presence_rows(conn, board),
            "posts": board_post_rows(conn, board, limit=20)}


def board_close(conn, root, scope, scope_key="", period_key=None):
    """归档一块板（`closed` 后不再接收签到，但**不删**）。

    依据 `09` R5-7「直到项目、开发分支被标记为结束」+ 参照规则 6「不合并、不压缩、不删除」。
    """
    board = board_ensure(conn, scope, scope_key, period_key)
    if board["status"] == "closed":
        return board
    conn.execute("UPDATE boards SET status='closed', closed_at=? WHERE id=?",
                 (now_utc(), board["id"]))
    conn.commit()
    board = board_ensure(conn, scope, scope_key, period_key)
    board_render(conn, root, board)
    return board


def cmd_board(args):
    """看板 CLI：show / checkin / post / render / close。

    `--project` 是 `--scope project --key <项目>` 的便捷写法（日常最常用）。
    """
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    root = Path(args.root)
    try:
        act = args.board_action

        # sweep 不走 scope/key（它是**全局批量**动作），所以必须放在读 args.scope 之前——
        # 它的 parser 没有那些参数，先读会 AttributeError（2026-10-25 写完当场发现的）。
        if act == "sweep":
            sw = board_sweep(conn, root, getattr(args, "days", None))
            if not sw.get("cutoff"):
                print("跳过：board.sweep_days < 1")
                return 0
            print(f"已归档 {sw['swept']} 块（截止 {sw['cutoff']}）")
            for b in sw["closed"][:10]:
                print(f"  {b['scope']}:{b['scope_key']} {b['period_key']}")
            if len(sw["closed"]) > 10:
                print(f"  … 共 {len(sw['closed'])} 块")
            return 0

        scope, key = args.scope, args.key
        if getattr(args, "project", None):
            scope, key = "project", args.project

        if act == "checkin":
            # 2026-10-25 修（真实事故：anyi-studio 主 AI 签到被记成「安逸」）：
            # **不带 --citizen 时不再默认落到系统主人头上**。
            #
            # 为什么这必须改：签到是「声明我是谁」。默认成主人 = **冒充**。
            # 而且它是静默的——看板上出现一行"安逸在改 fish.py"，而安逸本人在睡觉。
            # 出事之后要靠人肉发现并清理，那正是"默认值把行为记到别人头上"的代价。
            citizen = (getattr(args, "citizen", None) or "").strip()
            if not citizen:
                print("错误：board checkin 必须带 --citizen。", file=sys.stderr)
                print("  签到是「声明我是谁」——不指定就默认记到系统主人头上等于冒充。",
                      file=sys.stderr)
                print("  不知道自己的 citizen_id？→ 先跑 `fish whoami`，或 "
                      "`fish session start --project X` 签发凭证后用回执里的 citizen_id。",
                      file=sys.stderr)
                return 2
            doing = (args.doing or "").strip()
            if not doing:
                print("错误：--doing 必填——看板的核心价值就是让别人靠这一句"
                      "判断会不会跟你撞车；没有 doing 的在场行等于没签到。",
                      file=sys.stderr)
                return 2
            st = getattr(args, "status", "active") or "active"
            board = board_checkin(conn, citizen, doing=doing,
                                  surface=getattr(args, "surface", "") or "",
                                  scope=scope, scope_key=key,
                                  agent_id=getattr(args, "agent", None),
                                  status=st)
            board_post(conn, board, citizen, "checkin" if st == "active" else "left",
                       doing)
            path, size, split = board_render(conn, root, board)
            verb = "已签到" if st == "active" else "已下板"
            print(f"{verb}\t{board['path']}\t{size} B" + ("（已切分）" if split else ""))
            return 0

        if act == "post":
            citizen = (getattr(args, "citizen", None) or resolve_owner(conn) or "unknown")
            board = board_ensure(conn, scope, key)
            pid = board_post(conn, board, citizen, args.kind, args.text)
            if pid is None:
                print("错误：动态内容为空", file=sys.stderr)
                return 2
            board_render(conn, root, board)
            print(f"已记录\t{board['path']}\t#{pid}")
            return 0

        if act == "render":
            board = board_ensure(conn, scope, key)
            path, size, split = board_render(conn, root, board)
            print(f"{path}\t{size} B" + ("（已切分）" if split else ""))
            return 0

        if act == "close":
            board = board_close(conn, root, scope, key)
            print(f"已归档\t{board['path']}")
            return 0

        # show（默认）
        info = board_show(conn, scope, key)
        print(_render_one(conn, info["board"], info["posts"]))
        return 0
    finally:
        conn.close()


# ------------------------------------------------------------------ CLI

def cmd_archive(args):
    """归档 CLI：list / one / sweep。

    **它不改语义**：`status` 一个字不动，只搬文件 + 更新 `file_path`。
    所以推送、召回、注入包、邻近查询的行为完全不变（它们本来就看不见这些状态）。
    """
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    root = Path(args.root)
    try:
        act = args.action or "sweep"

        if act == "list":
            rows = conn.execute(
                "SELECT code, project, status, file_path FROM code_index "
                "WHERE status IN ('superseded','withdrawn') AND file_path IS NOT NULL "
                "ORDER BY project, code").fetchall()
            print(f"可归档 {len(rows)} 条（status=superseded/withdrawn，active 一条都不在其中）：")
            for code, proj, st, fp in rows:
                print(f"  {code:<26} {proj:<14} {st:<11} {fp}")
            return 0

        if act == "one":
            if not args.code:
                print("错误：one 需要给编码", file=sys.stderr)
                return 2
            r = archive_body(conn, root, args.code, year=args.year,
                             dry_run=args.dry_run)
            conn.commit()
            tag = "（演练）" if args.dry_run else "已归档"
            print(f"{tag}\t{r['code']}\n  {r['from']}\n→ {r['to']}")
            return 0

        res = archive_sweep(conn, root, include_withdrawn=args.withdrawn,
                            year=args.year, dry_run=args.dry_run,
                            limit=args.limit)
        tag = "（演练，未真移）" if args.dry_run else ""
        print(f"{tag}可归档 {res['total']} 条，移动 {res['moved']} 条"
              + (f"，已在归档区 {res['already']} 条" if res.get("already") else ""))
        for it in res["items"][:20]:
            print(f"  {it['code']:<26} → {it['to']}")
        if len(res["items"]) > 20:
            print(f"  … 共 {len(res['items'])} 条")
        for s in res["skipped"][:10]:
            print(f"  跳过 {s['code']}：{s['why']}")
        for e in res["errors"][:10]:
            print(f"  错误 {e['code']}：{e['why']}")
        return 0 if not res["errors"] else 1
    finally:
        conn.close()


def main():
    # 计划任务/无控制台环境下 stdout 是 GBK，中文与 ⚠ 等符号会直接崩（实测 ExitCode=1）。
    # 这里强制 UTF-8，避免"功能正常但退出码 1"的假失败。
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(prog="fish", description="大肥鱼生命系统 v0.3 (P1a)")
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)

    p = sub.add_parser("project-add"); p.add_argument("prefix"); p.add_argument("--name", default="")
    p.set_defaults(func=cmd_project_add)

    # 项目前缀生命周期（retire / restore）。**默认只打印计划**：--apply 才落库。
    # 为什么与 project-add 不同族：add 是"开一个新前缀"（幂等、无副作用面），
    # 而 retire 改的是"以后还能不能发新码"——它必须能先看计划再执行。
    p = sub.add_parser("project", help="项目前缀生命周期（retire / restore；默认只打印计划）")
    psub = p.add_subparsers(dest="project_action", required=True)

    pp = psub.add_parser("retire", help="退役一个前缀：停止分配新编码（不删行、不回收前缀）")
    pp.add_argument("prefix", help="项目前缀（如 测试项目）")
    pp.add_argument("--reason", default="", help="为什么退役（写进事件，可追溯）")
    pp.add_argument("--apply", action="store_true",
                    help="真的落库；不加只打印计划（默认行为）")
    pp.add_argument("--force", action="store_true",
                    help="名下有记忆也退役（只停分配，不动记忆归属）")
    pp.set_defaults(func=cmd_project)

    pp = psub.add_parser("restore", help="把 retired 改回 active（retire 的撤销）")
    pp.add_argument("prefix")
    pp.add_argument("--reason", default="")
    pp.add_argument("--apply", action="store_true", help="真的落库；不加只打印计划")
    pp.set_defaults(func=cmd_project)

    # 2026-10-25（阶段 D · 项目三态）：闭合 / 重开。
    # 与 retire 的区别：retire 回答"这个名字还用不用"（终态，前缀不可再分配）；
    # close 回答"这件事做完了没有"（可逆，随时 reopen 回来继续写）。
    pp = psub.add_parser("close", help="闭合一个项目：这件事做完了（可 reopen，不删任何东西）")
    pp.add_argument("prefix")
    pp.add_argument("--reason", default="")
    pp.add_argument("--apply", action="store_true", help="真的落库；不加只打印计划")
    pp.set_defaults(func=cmd_project)

    pp = psub.add_parser("reopen", help="把 closed 改回 active（闭合的撤销，之后可继续写）")
    pp.add_argument("prefix")
    pp.add_argument("--reason", default="")
    pp.add_argument("--apply", action="store_true", help="真的落库；不加只打印计划")
    pp.set_defaults(func=cmd_project)

    # 名字别名（阶段 D）：只给 CLI，不给 MCP 工具——见 cmd_alias 的 docstring。
    p = sub.add_parser("alias", help="名字别名（单向 from→to；只影响检索，不改写入归属）")
    asub = p.add_subparsers(dest="alias_action", required=True)

    # ⚠️ 变量名纪律：**不要用 `ap`**。`main()` 里 `ap` 是**根 parser**
    # （`ap = argparse.ArgumentParser(...)`，结尾 `ap.parse_args()` 用的就是它）。
    # 2026-10-25 我真踩了一次：把这里的循环变量写成 `ap`，三行之后根 parser 被
    # 覆盖成 `alias list` 子命令，于是 `fish.py --help` 显示的是 list 的用法、
    # 所有子命令都报 "unrecognized arguments"。22 条 CLI 判据当场全红才暴露。
    alp = asub.add_parser("link", help="建立别名：from 就是 to（要求 to 真实存在且不成环）")
    alp.add_argument("kind", choices=list(_ALIAS_KINDS))
    alp.add_argument("from_name")
    alp.add_argument("to_name")
    alp.add_argument("--reason", default="")
    alp.set_defaults(func=cmd_alias)

    alp = asub.add_parser("unlink", help="撤销别名（幂等：不存在也算成功）")
    alp.add_argument("kind", choices=list(_ALIAS_KINDS))
    alp.add_argument("from_name")
    alp.add_argument("--reason", default="")
    alp.set_defaults(func=cmd_alias)

    alp = asub.add_parser("list", help="列出全部别名")
    alp.set_defaults(func=cmd_alias)

    # 看板（P1-b · 2026-10-25）。裁决 `16` §4.1：**数据库做真相 + 文件做渲染**，
    # 所以写入只走这里（AI 不许直接 write 渲染文件），文件删了可随时重建。
    p = sub.add_parser("board", help="看板（show / checkin / post / render / close）")
    bsub = p.add_subparsers(dest="board_action", required=True)

    def _board_common(bp, need_doing=False):
        """看板子命令的公共参数。`--project` 是 `--scope project --key <项目>` 的便捷写法。"""
        bp.add_argument("--scope", default="project", choices=list(BOARD_SCOPES))
        bp.add_argument("--key", default="")
        bp.add_argument("--project", default=None)
        bp.add_argument("--period", default=None)
        return bp

    # show：读看板（默认动作）
    bsh = _board_common(bsub.add_parser("show", help="读看板（不渲染）"))
    bsh.set_defaults(func=cmd_board)

    # checkin：签到（doing 必填——它是看板的核心价值）
    bck = _board_common(bsub.add_parser("checkin", help="签到：登记你在做什么"))
    bck.add_argument("--doing", required=True, help="一句话：在做什么（≤80 字）")
    bck.add_argument("--surface", default="", help="占用面（文件/目录前缀，逗号分隔）")
    bck.add_argument("--citizen", default=None, help="公民 id（默认取系统主人）")
    bck.add_argument("--agent", default=None, help="会话/宿主侧标识")
    # 2026-10-25（`EVAL-261025-14`）：`status='left'` 在 DDL 里写着，但**没有任何入口**
    # ——想「收工下板」的人做不到，行只能自然变 idle、或等 sweep 归档。
    # 补上之后，`left` 才从一个词汇变成一个动作。
    bck.add_argument("--status", default="active", choices=["active", "left"],
                     help="active=在场（默认）/ left=收工下板（之后不再计入重叠）")
    bck.set_defaults(func=cmd_board)

    # post：往今日动态加一条
    bpo = _board_common(bsub.add_parser("post", help="往今日动态加一条（≤200 字）"))
    bpo.add_argument("text", help="动态内容")
    bpo.add_argument("--kind", default="note", choices=["checkin", "progress", "done", "note"])
    bpo.add_argument("--citizen", default=None)
    bpo.set_defaults(func=cmd_board)

    # render：重新渲染（自愈）
    brd = _board_common(bsub.add_parser("render", help="重新渲染看板文件（可自愈）"))
    brd.set_defaults(func=cmd_board)

    # close：归档（不删）
    bcl = _board_common(bsub.add_parser("close", help="归档看板（closed 后不再收签到，但不删）"))
    bcl.set_defaults(func=cmd_board)

    # sweep：批量归档 N 天前的 open 板（2026-10-25 · 要求 C）
    bsw = bsub.add_parser("sweep", help="把 N 天前的 open 看板自动归档（只改状态，不删）")
    bsw.add_argument("--days", type=int, default=None,
                     help="保留最近 N 天（缺省取 config board.sweep_days，默认 7）")
    bsw.set_defaults(func=cmd_board)

    p = sub.add_parser("remember")
    p.add_argument("project"); p.add_argument("title")
    p.add_argument("--body", default="")
    p.add_argument("--type", default="fact",
                   choices=["identity", "rule", "fact", "episode", "lesson", "artifact"])
    p.add_argument("--visibility", default="shared", choices=["private", "shared", "public"])
    p.add_argument("--tags", nargs="*", default=[])
    p.add_argument("--owner", default=None,
                   help="公民 ID（不是显示名）。缺省=系统主人（citizens.is_owner）")
    p.add_argument("--cause", default=None)
    p.add_argument("--occurred-at", dest="occurred_at", default=None,
                   help="这件事什么时候发生（ISO8601 UTC，如 2026-10-22T04:00:00Z）；"
                        "缺省=现在。用于补记过去的事，与 recorded_at 区分")
    p.add_argument("--attach", action="append", default=[],
                   help="附件路径，可重复（图片/文档等，按内容寻址存入 blobs/）")
    p.add_argument("--session-actor", dest="session_actor", default=None,
                   help="会话级身份（如 anyi-dev），用于区分同一宿主下的不同会话")
    p.set_defaults(func=cmd_remember)

    p = sub.add_parser("recall")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--scope", choices=["all", "title", "body"], default="all",
                   help="检索范围：all（默认）/ title / body")
    p.add_argument("--code", default=None, help="精确编码（与 query 二选一或并用）")
    p.add_argument("--project", default=None, help="只查该项目前缀")
    p.add_argument("--tags", nargs="*", default=[], help="标签过滤（需同时满足）")
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("neighbors")
    p.add_argument("code")
    p.add_argument("--n", type=int, default=None)
    p.add_argument("--strategy", default=None, choices=["code", "time", "tag", "type", "mixed"])
    p.add_argument("--cross-project", dest="cross_project", action="store_true", default=None)
    p.add_argument("--include-dormant", dest="include_dormant", action="store_true", default=None)
    p.set_defaults(func=cmd_neighbors)

    sub.add_parser("list").set_defaults(func=cmd_list)

    p = sub.add_parser("forgetting"); p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_forgetting)

    p = sub.add_parser("citizen-add")
    p.add_argument("name"); p.add_argument("--kind", default="visitor", choices=list(KINDS))
    p.add_argument("--role", default=None); p.add_argument("--host", default=None)
    p.set_defaults(func=cmd_citizen_add)

    p = sub.add_parser("citizen-list")
    p.set_defaults(func=cmd_citizen_list)

    p = sub.add_parser("citizen-retire")
    p.add_argument("citizen_id")
    p.add_argument("--reason", default=None)
    p.add_argument("--migrate-to", dest="migrate_to", default=None,
                   help="把该公民名下的记忆迁给另一个公民")
    p.add_argument("--force", action="store_true",
                   help="有记忆但不迁移（保持原归属，仅退役身份）")
    p.set_defaults(func=cmd_citizen_retire)

    # 身份层 P1-b：session 命令族。**CLI 专属**——不注册成 MCP 工具（18 §4 / 评审 R-2）：
    # MCP 面签不出 machine 级，能签的只有"读得到宿主 env"的 CLI。
    p = sub.add_parser("session", help="会话身份（CLI 专属命令族；MCP 面不签发凭证）")
    sess = p.add_subparsers(dest="session_action", required=True)

    sp = sess.add_parser("start", help="签发会话凭证（幂等：同会话重跑 → 同一个公民）")
    sp.add_argument("--project", action="append", default=None, metavar="PREFIX",
                    help="顺带开一个项目档期（如 ANYI）。**可重复给**："
                         "一个会话可以同时在多个项目上开档（--project A --project B）")
    sp.add_argument("--account", default=None,
                    help="AI 账号（跨会话聚合锚点）。缺省取 FISH_ACTOR；**不再退到 --alias**"
                         "（2026-10-25 修：alias 是会话标签，不是账号，"
                         "把前者当后者会静默建出第二个公民）")
    sp.add_argument("--citizen", default=None, metavar="CITIZEN_ID",
                    help="把这条会话**直接绑到已存在的公民**上（跳过 find_or_create）。"
                         "用于「MCP 侧已登记、CLI 侧 host_session_key 不同」导致的归属分裂")
    sp.add_argument("--alias", default=None,
                    help="会话别名（没有宿主 session id 时，它就是幂等键；**只是标签**）")
    sp.add_argument("--cwd", default=None, metavar="PATH",
                    help="当前工作目录：按 workspace_map 最长前缀解析成项目并自动开档期。"
                         "**显式给了 --project 时以 --project 为准**（这里只做兜底）")
    sp.set_defaults(func=cmd_session_start)

    sp = sess.add_parser("status", help="当前会话的身份 / 档期 / 凭证状态")
    sp.add_argument("--token", default=None, help="查某条凭证（缺省=用 env 定位当前会话）")
    sp.set_defaults(func=cmd_session_status)

    sp = sess.add_parser("end", help="吊销凭证 + 关档期 + 刷新活跃时间（幂等）")
    sp.add_argument("--token", default=None, help="吊销这条凭证（缺省=吊销当前会话的全部）")
    sp.set_defaults(func=cmd_session_end)

    sp = sess.add_parser("agent", help="为子 agent 签 inherited 凭证（只允许一跳）")
    sp.add_argument("--parent", required=True, help="父会话的 token 明文")
    sp.add_argument("--role", required=True, help="子 agent 的角色（会拼进 session_key）")
    sp.set_defaults(func=cmd_session_agent)

    p = sub.add_parser("whoami"); p.add_argument("--citizen", default=None)
    p.set_defaults(func=cmd_whoami)

    p = sub.add_parser("owner")
    p.add_argument("action", choices=["set", "show"])
    p.add_argument("citizen_id", nargs="?", default=None)
    p.set_defaults(func=cmd_owner)

    p = sub.add_parser("owner-fix", help="修正一条记忆的归属（追加事件 + 更新投影）")
    p.add_argument("--code", required=True, help="要修正的编码，如 ANYI-261022-01")
    p.add_argument("--citizen", dest="citizen_id", required=True,
                   help="新的归属公民 ID")
    p.add_argument("--reason", default=None, help="修正理由（写进 memory.owner_fixed 事件）")
    p.set_defaults(func=cmd_owner_fix)

    # 工作区 → 项目映射（R1 根治）：项目归属的权威来源。
    # **CLI 专属**，不注册成 MCP 工具：维护映射是人的决定，不是 AI 每次调用顺手改的东西。
    p = sub.add_parser("workspace", help="工作区 → 项目映射（项目归属的权威来源）")
    wsub = p.add_subparsers(dest="workspace_action", required=True)

    wp = wsub.add_parser("set", help="新增/更新一条映射")
    wp.add_argument("path", help="工作区路径（如 I:/app/anyi-studio）；存库前会归一化")
    wp.add_argument("project", help="项目前缀（如 ANYI）")
    wp.add_argument("--note", default=None, help="备注（给人看的，如“主代码库”）")
    wp.set_defaults(func=cmd_workspace)

    wsub.add_parser("list", help="列出全部映射").set_defaults(func=cmd_workspace)

    wp = wsub.add_parser("remove", help="删除一条映射（幂等）")
    wp.add_argument("path")
    wp.set_defaults(func=cmd_workspace)

    wp = wsub.add_parser("seed", help="从现有记忆反推候选（只打印，不写入）")
    wp.set_defaults(func=cmd_workspace)

    p = sub.add_parser("inject")
    p.add_argument("--citizen", default=None)
    p.add_argument("--budget", type=int, default=None)
    p.add_argument("--project", default=None,
                   help="显式声明当前项目前缀（最高优先）；不传则按"
                        "会话档期 → workspace_map(--cwd) → 最近更新推断")
    p.add_argument("--cwd", default=None,
                   help="工作目录：用于 workspace_map 最长前缀匹配（不传则不查映射）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_inject)

    p = sub.add_parser("report")
    p.add_argument("--days", type=int, default=1, help="统计最近 N 天（默认 1=今日）")
    p.add_argument("--save", action="store_true", help="同时写入 data/_reports/")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("config")
    p.add_argument("action", choices=["get", "set", "list"])
    p.add_argument("key", nargs="?", default=None)
    p.add_argument("value", nargs="?", default=None)
    p.set_defaults(func=cmd_config)

    sub.add_parser("healthcheck").set_defaults(func=cmd_healthcheck)
    sub.add_parser("rebuild").set_defaults(func=cmd_rebuild)
    sub.add_parser("reindex").set_defaults(func=cmd_reindex)

    # 归档（2026-10-25 · 用户要求）：把已失效的正文移进 archive/<年>/，**只改位置不改语义**。
    # 判据直接复用已有的 status（superseded / withdrawn）——不新造"归档状态"，
    # 所以推送/召回（它们硬过滤 status='active'）的行为一个字节都不会变。
    p = sub.add_parser("archive",
                       help="归档：把 superseded/withdrawn 的正文移进 archive/<年>/（不动 active）")
    p.add_argument("action", nargs="?", default="sweep",
                   choices=["sweep", "list", "one"],
                   help="list=只看清单 / one=单条 / sweep=批量（默认）")
    p.add_argument("code", nargs="?", default=None, help="one：要归档的编码")
    p.add_argument("--dry-run", action="store_true", help="只看要动哪些，不真移")
    p.add_argument("--year", default=None, help="归档到哪一年（缺省取记录年份）")
    p.add_argument("--limit", type=int, default=None, help="最多处理几条")
    p.add_argument("--no-withdrawn", dest="withdrawn", action="store_false",
                   default=True, help="只归档 superseded，不含 withdrawn")
    p.set_defaults(func=cmd_archive)

    # 推送（P1-a · E 批）。变量名刻意避开 `ap` / `parser`：
    # 2026-10-25 踩过的坑是"子命令循环变量覆盖了 main() 的根 parser ap"，
    # 症状是整个 CLI 崩掉（22 条判据红）。这里的名字与根 parser 无重合。
    pushp = sub.add_parser("push", help="推送：stats 效果度量 / status 当前配置")
    pushsub = pushp.add_subparsers(dest="action", required=True)
    push_stats_p = pushsub.add_parser("stats", help="四个指标分开报（采纳/盲区/违规/方差）")
    push_stats_p.add_argument("--days", type=int, default=30)
    push_stats_p.set_defaults(func=cmd_push)
    pushsub.add_parser("status", help="看 push.* 配置现值").set_defaults(func=cmd_push)

    p = sub.add_parser("withdraw")
    p.add_argument("code")
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_withdraw)

    p = sub.add_parser("events"); p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_events)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
