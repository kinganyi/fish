#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看板与门面渲染层：把"当前谁在动哪块"变成人和 AI 都能读的文字。

## 为什么独立成模块

`fish.py` 是编排层——它决定"什么时候该渲染什么"。这里只回答"渲染成什么样的字"：
看板行怎么排、谁的名字怎么显示、未了事项怎么折、门面根目录落在哪里。

两者混在一个文件里时，渲染层每处文案微调都要在一万两千行里定位，评审时真正的改动
会淹没在上下文里；而渲染本身是**纯函数性**的——输入是行数据、输出是字符串，
与"什么时候调用"无关。整块拿出去之后，编排层只剩调用点。

## 依赖方向（J2）

`fish_board` **不 import `fish`**。它需要的一切都来自下层：`fish_common`（时间 /
配置 / 占用面原语）、`fish_surface`（路径与覆盖判定）、`fish_activity`（在场与动作
读数）、`fish_naming`（可读名）、`fish_db` / `fish_schema`（库形态探测）。这与其它
领域模块同一条规矩——拆出去的模块不许反向依赖编排层。

`fish.py` 把这里的名字 re-export 出去，所以 `fish.board_render` 这类旧名字仍然可用，
调用面一个字没变（判据 J1 逐个查对象同一性）。
"""

from __future__ import annotations

import datetime
import fish_activity
import fish_naming
import fish_surface
import os
import re
import sqlite3
import sys

from fish_common import (
    BOARD_SCOPES, _board_seen_columns, _exc_text, _presence_has_session, board_period, board_relpath,
    count_tokens, get_config, now_utc, parse_utc)
from fish_db import _presence_pk_cols
from fish_schema import _PRESENCE_PK_COLS
from fish_surface import _path_inside, normalize_surface, surface_hit
from pathlib import Path


ACTIVITY_COLUMN_NOTE = "活动列来自 PostToolUse hook；纯 MCP 宿主（无 hook）不会有活动数据。"


BOARD_DOING_MAX = 80               # "在做什么"≤80 字


BOARD_MAX_BYTES = 32 * 1024        # 单文件上限（`09` R5-5 的实证数字）


BOARD_MAX_POST_CHARS = 200         # 单条动态 ≤200 字（渲染时硬截断）


BOARD_POSTS_PER_FILE = 200         # 单文件动态条数上限


BOARD_PRESENCE_IDLE_SECONDS = 30 * 60   # 30 分钟没动 = idle（渲染时算，不写库）


HOME_DIRNAME = "fish-home"            # 门面根目录名（与 p0/ 同级；裁决 Q3）


# 「实际在动」列的两句文案（FB-06）。空值为什么不能写 `—`：`—` 会被读成
# "这个人没在动"，而纯 MCP 宿主**结构上**就没有活动数据（没有 PostToolUse hook
# 就没有 `tool_activity` 行）。空值必须自己解释自己，并且把依赖条件写在同一格里。
NO_ACTIVITY_TEXT = "无活动数据（依赖宿主 hook）"


PST_DASH_NO_BOARD = "—（今日无看板）"


UNCHECKED_EMPTY = "_（无）_"


# ---------------------------------------------------- 在动但未签到（FB-14 · 2026-10-26）
# 用户报的现象：「AI 明明在开发，看板却说离线」。生产库只读实证的根因：
# 会话 `dsh:b92c164c…` 有 64 条 `tool_activity`（一直在写 `fish-devA/p0/fish.py`），
# 但它的 presence 落在「未分类」板，项目板上没有它的行 —— 而旧渲染**只读 presence**。
#
# 这一节补的就是那个盲区：把 `tool_activity` 聚合成**会话级**的行印出来。
# 三条纪律（写死在文案与代码里，不是风格问题）：
#   1. **是推断，不是签到** —— 标题与行文必须标「推断」；不能让人误以为是声明；
#   2. **只读** —— 不写 presence / 不写 events（推断不许变成事实）；
#   3. **不重复** —— 已经在该板在场表里的会话（按 citizen_id 或 agent_id/会话键）排除掉。
UNCHECKED_HEADING = "### 在动但未在本板登记（推断：来自 tool_activity）"


UNCHECKED_NOTE = ("这一节是**推断**：来自 `tool_activity` 的写动作，**不是签到**——"
                  "会话确实在动，但它没有声明在做什么，所以不能据它判断会不会与你撞车。")


# 2026-10-26（评审 ⚪15）：标题从「在动但未签到」改成「在动但**未在本板登记**」。
# 旧措辞错一档：生产库实测 29 个会话都在「未分类」板自动签到过——它们**签过到**，
# 只是没签在**这块板**上。说成"未签到"就是把"签在别的板"误报成"漏签"，
# 于是读者去催一个已经按协议办过事的人。这一句负责把那一档说清楚。
UNCHECKED_SCOPE_NOTE = ("说明：**未在本板签到 ≠ 没签到**——可能签在别的板"
                        "（例如「未分类」板）；这里是按本板在场表做的差集推断。")


UNCHECKED_UNKNOWN_CITIZEN = "（未识别）"


_AUTO_DOING_MARK = "自动登记"


# 账号 = 跨会话聚合锚点。一个 AI 账号下可以有多个会话公民（citizens.account_id 指过来）。
# kind 词表（visitor | role | resident）**刻意不设 CHECK**：它是词表，随规格会长；
# links.rel 的既有决策就是"加点值是规格内动作，不该每次迁移表"。
# 下面的 session_tokens 性质不同——它的 CHECK 锁的是**权限升级路径**，不是词表。
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
# 归档表：**必须与 citizens 逐列一致**（名字、类型、顺序、默认值），否则 citizens_all
# 视图对不上位——UNION ALL 只按**列位置**拼，不按列名认。
# 两份 DDL 是手抄的，所以不靠注释提醒、靠判据：test_identity_p1a 的判据 A 拿
# PRAGMA table_info 逐列比对两张表，改一处忘另一处当场红。
# citizens 的 P1-a 补列清单（旧库升级路径）。声明必须是**能加到非空表上**的形态
# （SQLite 的 ALTER ADD COLUMN 不允许无默认值的 NOT NULL）——这 5 列全是可空 TEXT，
# 自然满足；将来若加 NOT NULL 列，必须带 DEFAULT。
# 顺序即 citizens 的末尾列序，**必须与 _CITIZENS_ARCHIVE_DDL 的末尾 5 列同序**。
# 合并视图的列契约。**顺序就是拼接口**：两份 SELECT 共用这一份列名，写两遍就会漂，
# 而这里漂了的后果是静默的（错列/少列都不报错，只是数据错位）。
# 开档期唯一索引的目标定义（2026-10-24：一列 → 两列）。
# **单独提成常量**，因为它有一个别的索引没有的问题：**名字没变、定义变了**。
# 旧库里 `ux_stint_open` 已经存在（`ON project_stints(citizen_id) WHERE to_ts IS NULL`），
# 而 `CREATE UNIQUE INDEX IF NOT EXISTS` 一见同名索引就直接跳过——旧的一列索引会带着
# 旧语义原地活下来，多项目并行当场被它拦住（而且报错发生在插入时，离原因很远）。
# 所以它的建法不能是"清单里一条幂等 CREATE"，必须走 _ensure_stint_open_index：
# **先按 PRAGMA 判断现有定义 → 不对就 DROP → 再 CREATE**。
# 身份层的索引清单（只放 migrate，不放 SCHEMA：SCHEMA 先于 migrate 执行）。
# 口径：这里只放**定义不会变**的索引。定义会变的那个（开档期唯一索引）由
# _ensure_stint_open_index 单独负责——两种生命周期不该塞进同一个循环里。
# 四张新表的 DDL，**按依赖顺序**（citizens_archive 必须早于 citizens_all 视图）。
# 提成一份元组是为了让两个消费者共用同一份：SCHEMA 拼进去（新库）与
# _ensure_identity_tables 循环建（旧库/幂等）。第三份拷贝就是漂移的起点。
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
# 表在**两个消费者**里各建一次：SCHEMA（新库）与 migrate（旧库升级），共用同一份 DDL。
# 沿用身份层/push 层的纪律：表 DDL 提成常量，索引与视图只放 migrate（这张表目前
# 不需要额外索引：行数是个位数，最长前缀匹配在 Python 里做，见 resolve_project_by_cwd）。
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
# ------------------------------------------------------ 看板（P1-b · 2026-10-25）
# 裁决依据 `16` §4.1：**数据库做真相 + 文件做渲染**。
# 为什么不用纯文件（`12` 最初的方案）：
#   并发写靠纪律会丢行、读到半写的文件、单文件必然膨胀（`active-sessions` v1
#   就是这样死的：10 天 114 条 → 396KB → 超过读取工具上限）。
#   表和文件不是"两份真相"，是"真相 + 投影"，与 fish 已有的事件→投影同构。
#
# 看板回答的是「**此刻谁在动哪块**」——不是"发生了什么"（那是 daily）。
# 这条边界一旦破了，看板就会退化成第二份 daily（`09` §4.6 的第三个坑）。
# 旧库补列清单（顺序纪律：先建表 → 再补列 → 最后建索引）。
# 声明必须是「可加到非空表上」的形态（SQLite 的 ALTER ADD COLUMN 不允许无默认值 NOT NULL）。
# `session_key` **刻意不在上面那份补列清单里**：它要连主键一起重建，走
# `_ensure_presence_session`（补列与重建是同一次事务的两步，拆开就会留下
# "有列、两列主键"的半迁移态——那正是 R3 要避免的形态）。
# 目标主键（**顺序是契约**：判据 S24 按元组全等比对，不看集合）。
# 回填规则（迁移只跑一次；之后不再推断）：只认「已知宿主前缀 + 冒号 + 至少 1 字符」。
# `?*` 里的 `?` 是必要的：GLOB 的 `*` 能匹配空串，少了 `?` 就会把 `dsh:` 这种空 id
# 也当成会话键（那等于伪造证据）。非标准形态（`codebuddy-主AI/ffd54d84`：带 `/`、
# 无 `:`）一律回填 ''，保留旧的"合并成一行"语义。
# 回滚用的**历史形态**（两列主键）。只给 `presence_session_rollback` 建临时表用，
# 不参与新建库——新库永远走 `_PRESENCE_DDL`（v3）。写死一份是刻意的：v2 从此冻结，
# 它不该跟着 v3 的列增删漂。
# 看板动态（"今日动态"区）——它比"在场"轻，但同样受容量纪律约束。
# 为什么不复用 `events`：events 是**不可变的事实流**，而看板动态是**可滚动的展示层**；
# 把展示层塞进事实流会让"重建投影"多一类要处理的事件，得不偿失。
# 看板"已看到"快照（EVAL-261025-20，2026-10-25 修）：记录**每个公民上次注入时**
# 看到的看板状态。推送候选拿它与当前状态比较——"你看完之后又有人动了"才推。
# 为什么单独建表而不是塞进 config：config 是全局键值，多公民会互相覆盖。
# 2026-10-27（P0-3）：主键升到 **(board_id, citizen_id, session_key)**。
# 旧表按公民记"上次看到哪"——同公民多会话会互相吞推送（A 会话看过，B 会话就收不到
# "板上有人动了"）。升三列后每个会话各有自己的快照；旧行迁成 session_key=''（无凭证会话）。
# ------------------------------------------------------ 动作流（2026-10-26 · T02）
# 独立审计表，**不进记忆库**（设计 §4.1）：
#   · 不进 `events`：事件流是不可变真相、要参与投影重建；动作流是低语义审计，
#     进事件会让 rebuild_projection 多处理一类无投影价值的事件；
#   · 不进 `memory_fts`/`recall`：会污染记忆检索（EVAL 建议 7）。
# 两条都写成判据（见 test_activity）。
# 旧库补列清单（与 `_PRESENCE_NEW_COLS` 同理；当前与 DDL 逐列一致，
# 留这份清单是为了"下次加列"时不用再想顺序，也为了让补列路径有单一入口）。
# 两张推送地基表的「补列 / 建索引」清单。
# 补列用的声明都必须是**可加到非空表上**的（SQLite 的 ALTER ADD COLUMN 不允许
# 无默认值的 NOT NULL），所以这里刻意比 _*_DDL 里的声明宽松——新建表仍按严格版走。
# push_log 的补列清单（旧库升级路径）。与上面两份同理：声明必须是**能加到非空表上**的形态
# （SQLite 的 ALTER ADD COLUMN 不允许无默认值的 NOT NULL），新建表仍按 _PUSH_LOG_DDL 的严格版走。
def _presence_write_shape(conn) -> tuple:
    """返回 `(INSERT 是否带 session_key, ON CONFLICT 目标)`——写侧的**唯一**分叉点。

    三列主键 → `(True, "board_id, citizen_id, session_key")`
    两列主键（迁移未跑 / 故障降级）→ `(False, "board_id, citizen_id")`

    为什么只按**主键**判、不按配置判：`ON CONFLICT` 的目标必须与某个唯一约束逐列
    一致，否则 SQLite 直接报 `ON CONFLICT clause does not match`，签到与自动行全崩
    （R2）。配置项 `board.presence_session_rows` 只影响读侧渲染（设计 §6.3）。
    """
    if _presence_pk_cols(conn) == _PRESENCE_PK_COLS:
        return True, "board_id, citizen_id, session_key"
    return False, "board_id, citizen_id"


def _board_seen_write_shape(conn) -> tuple:
    """`(INSERT 是否带 session_key, ON CONFLICT 目标)`——写侧唯一分叉点（P0-3）。

    与 `_presence_write_shape` 同一条纪律：迁移可能因"备份写不进"而故意不升级，
    此时库还是两列形态，写侧必须自动降级，否则"宁可这次不升级"会变成"这次直接崩"。
    """
    if "session_key" in _board_seen_columns(conn):
        return True, "board_id, citizen_id, session_key"
    return False, "board_id, citizen_id"


def _fold_presence_for_inject(pres, limit=3):
    """注入包看板段**按公民折叠**：同公民多会话只占一格（后附 `（+N 会话）`）。

    为什么必须折（设计 §4.4 / R4）：注入包看板段有 **3 格硬上限**。会话维度一进来，
    一个公民的 10 个会话会把别的公民整个挤出注入包——那等于把"看见更多行"做成
    "看不见别人"，比不修更坏。

    口径：同公民取 `effective_last` 最新的那行做主行（其余字段原样保留，**不编**）；
    `surface` 取并集前 2 项（读者只需要知道"这块被占了"）；`extra_sessions` 记被折掉的行数。
    """
    order, groups = [], {}
    for row in pres or ():
        cid = row.get("citizen_id") or ""
        if cid not in groups:
            groups[cid] = []
            order.append(cid)
        groups[cid].append(row)
    out = []
    for cid in order:
        group = groups[cid]
        main = max(group, key=lambda r: effective_last(r)) if group else None
        if main is None:
            continue
        folded = dict(main)
        merged = []
        for row in group:
            for item in normalize_surface(row.get("surface")):
                if item not in merged:
                    merged.append(item)
        folded["surface"] = ",".join(merged[:2])
        folded["extra_sessions"] = max(0, len(group) - 1)
        folded["sessions_of_citizen"] = len(group)
        out.append(folded)
    if limit is not None and int(limit) > 0:
        return out[:int(limit)]
    return out


def board_block(conn, project=None, limit=5, citizen_id=None, max_tokens=None, stats=None,
                session_key=None):
    """注入包里的看板段（约 150-200 token）。

    三条设计约束（都来自 `09` R5-6 与 §4.6 的三个坑）：

    1. **只给最近一级**：项目级优先，没有项目才给根看板。三级全推会重复——
       项目级看板已经包含"这个项目里谁在动"，再推根看板就是同一件事说两遍。
    2. **只给最近 5 行动态**：看板是"此刻"，不是"全程"。全给会让注入包变成第二份 daily。
    3. **空看板要明写**：不能留白——留白会让 AI 以为"看板这个功能坏了"，
       而实际上"没人签到"本身就是一条重要信息（未登记 = 未发生）。

    2026-10-26（FB-02）：`max_tokens` = 本段自己的预算上限（由
    `inject.board_max_ratio` × 整包预算算出）。旧实现**没有上限**：动态一多，
    整段就膨胀，而整包裁剪只能"整段丢"——于是"看板多了"会连带把别段挤掉，
    读者却看不出是看板挤的。现在超上限就**逐行截断**（从末尾往回丢），
    并在段头写 `（已按预算截断）`：**被砍过这件事必须可读**。
    `stats`（可选字典，出参）回填 `{"lines": 截断前总行数, "truncated": 丢掉的行数,
    "tokens": 截断后 token}`，供 `meta["truncated"]["board"]` 与包尾声明共用。
    """
    scope, key = ("project", project) if project else ("root", "")
    if stats is not None:
        stats.setdefault("lines", 0)
        stats.setdefault("truncated", 0)
        stats.setdefault("tokens", 0)
    try:
        # 读路径**只查不建**（2026-11-22 · 空看板治理 A+C）。
        # `id=None` 的虚拟板让下面 `not pres and not posts` 的空板分支自然成立，
        # 所以空板文案只此一份，不需要为"没有板行"再写一段。
        board = board_ensure(conn, scope, key, create=False)
        if board is None:
            board = _virtual_board(scope, key)
        pres = board_presence_rows(conn, board)
        posts = board_post_rows(conn, board, limit=limit)
    except Exception:        # noqa: BLE001 —— 看板是增强项，坏了不该拖垮整个注入包
        return None
    # EVAL-261025-20：把"这次注入看到的看板"记成快照，供推送候选比较。
    # 空板也要记（last_post_id=0）——否则"空板 → 第一条签到"永远推不出来。
    if citizen_id and board["id"] is not None:
        try:
            last_post = conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM board_posts WHERE board_id=?",
                (board["id"],)).fetchone()[0]
            last_pres = conn.execute(
                "SELECT COALESCE(MAX(last_seen_at),'') FROM presence WHERE board_id=?",
                (board["id"],)).fetchone()[0]
            # P0-3：按**会话**记快照（同公民多会话各记各的，互不吞推送）。
            # 旧库（未迁移）自动降级两列——写侧唯一分叉点 `_board_seen_write_shape`。
            has_sk, conflict = _board_seen_write_shape(conn)
            if has_sk:
                conn.execute(
                    "INSERT INTO board_seen (board_id,citizen_id,session_key,last_post_id,"
                    "last_presence_at,updated_at) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(board_id,citizen_id,session_key) DO UPDATE SET "
                    "last_post_id=excluded.last_post_id, "
                    "last_presence_at=excluded.last_presence_at, "
                    "updated_at=excluded.updated_at",
                    (board["id"], citizen_id, session_key or "", int(last_post or 0),
                     last_pres or "", now_utc()))
            else:
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
    proj_label = fish_naming.project_label(
        key if scope == "project" else "",
        _project_namespace(conn, key) if scope == "project" else None)
    if not pres and not posts:
        empty = (f"看板（{day}，{proj_label}）：**今天还没有人签到**——未登记 = 未发生。"
                 "你要开工就用 `fish board checkin --doing \"在做什么\"` 登记，"
                 "别人才能判断会不会跟你撞车。")
        if stats is not None:
            stats["lines"] = 1
            stats["truncated"] = 0
            stats["tokens"] = count_tokens(empty)
        return empty
    # 2026-10-25（`EVAL-261025-09`）：**必须把 surface 带出去**。
    # 旧实现只印 doing，于是"自动注入层恰好缺了唯一有用的字段"——
    # 每个会话都会读到的段落里没有 surface，AI 不主动 board show 就永远
    # 不知道谁占了哪块，又回到"靠自觉"。
    # 2026-10-26（T03）：改成**两栏**（声明 / 实际）+ 项目名 + 可读名。
    overlaps = board_overlaps(conn, board)
    # 人数按**公民**去重、会话数另计（"5 人在场"里的"人"不该因为开了两个窗口变成 6）
    people = len({p.get("citizen_id") for p in pres})
    head = f"看板（{day}，{proj_label}）：{people} 人在场"
    if pres:
        if len(pres) > people:
            head += f"（{len(pres)} 会话）"
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
        # 注入包每段都要为"AI 会不会读到"付预算：最多 3 格、每格一个**公民**
        # （同公民多会话在这里折叠，见 `_fold_presence_for_inject`）
        for p in _fold_presence_for_inject(ordered, limit=3):
            surf = normalize_surface(p["surface"])
            declared = ("｜声明 " + "、".join(surf[:2]) + ("…" if len(surf) > 2 else "")
                        ) if surf else ""
            actual = _actual_dirs_text(p, limit=1)
            actual_s = "" if actual == "—" else f"｜实际 {actual}"
            mark = ""
            if p["status"] == "left":
                mark = "（已下板）"
            elif p["state"] != "active":
                mark = f"（{_state_suffix(p['state'], p['state_inferred'])}）"
            auto = "（自动登记）" if _is_auto_doing(p["doing"]) else ""
            extra = (f"（+{int(p['extra_sessions'])} 会话）"
                     if int(p.get("extra_sessions") or 0) else "")
            shown.append(f"{who_label(p)}{auto}{extra}：{p['doing'] or '未写 doing'}"
                         f"{declared}{actual_s}{mark}")
        head += " —— " + "、".join(shown) + ("…" if people > 3 else "")
    lines = [head]
    # 声明 ≠ 实际：**只在真的不一致时**说一句（天天说等于没说）。
    # 2026-10-26 评审 F2：判定走 `dir_abs`（判定列），声明面按**看板所属项目根**
    # 解析成绝对再比——展示列 `dir` 是相对写入者基准算的，同名不同处，
    # 拿它比会让"绝对声明 vs 相对实际"听上去像不一致（假提示）。
    decl_base = fish_activity.project_base(conn, key) if scope == "project" else None
    mismatch = [p for p in pres
                if p["status"] != "left" and p["state"] == "active"
                and normalize_surface(p["surface"])
                and (p.get("auto_dirs") or [])
                and not _surfaces_touch(normalize_surface(p["surface"]),
                                        [d.get("dir_abs") for d in p["auto_dirs"]],
                                        base=decl_base)]
    if mismatch:
        names = "、".join(who_label(p) for p in mismatch[:2])
        lines.append(f"（声明≠实际：{names} 的 doing/surface 与实际写的目录不一致"
                     "——要协调先看「实际」那栏）")
    if overlaps:
        # 重叠是**最该被看见的一句话**，放在动态之前
        lines.append("⚠ 占用面重叠（开工前先协调）：")
        for ov in overlaps[:3]:
            lines.append(f"  · `{ov['at']}` ← {ov.get('a_label', ov['a'])} ↔ "
                         f"{ov.get('b_label', ov['b'])}")
        if len(overlaps) > 3:
            lines.append(f"  · …共 {len(overlaps)} 处")
    # 残留行**不默默丢掉**，单独一句轻提示（`EVAL-261025-13` 建议 3）：
    # 换个词、不叫"冲突"——它们既不是冲突，也不该被无痕删除。
    stale = board_stale_rows(conn, board)
    if stale:
        names = "、".join((s.get("label") or s["citizen_id"]) for s in stale[:3]) + (
            "…" if len(stale) > 3 else "")
        lines.append(f"（另有 {len(stale)} 行已闲置超时仍声明占用：{names}"
                     "——这些**不算**重叠，别当成冲突）")
    if posts:
        lines.append("最近动态：")
        post_labels = citizen_labels(conn, [p[0] for p in posts])
        for cid, kind, txt, ts in posts[:limit]:
            lines.append(f"  · {_hhmm(ts)} {post_labels.get(cid) or cid} {kind}：{txt}")
    lines.append("（完整看板：`fish board show`）")
    # 缺可读名：只对**当前会话**提示一次（不刷别人的屏）
    who = _unnamed_hint(conn, citizen_id)
    if who:
        lines.append(who)
    # 占用面提示（2026-11-22 · Wave 2）：只对**当前会话**、只在其**没填**时说一次。
    surf = _surface_hint(conn, board, citizen_id, session_key)
    if surf:
        lines.append(surf)
    # 跨午夜可见性（`EVAL-261025-11`）：注入包也带一句，否则后半夜开工的人
    # 会把"板上只有我一个人"当成事实。
    prev_note = board_prev_note(conn, board)
    if prev_note:
        lines.append(prev_note)
    if stats is not None:
        stats["lines"] = len(lines)
        stats["truncated"] = 0
        stats["tokens"] = count_tokens("\n".join(lines))
    if max_tokens is not None:
        dropped = 0
        # 从末尾往回丢，**段头（lines[0]）永不动**：整段的意义都在那一行
        # （几人、谁、有没有重叠）。丢掉它换回来的预算不抵损失。
        while len(lines) > 1 and count_tokens("\n".join(lines)) > int(max_tokens):
            lines.pop()
            dropped += 1
        if dropped:
            lines[0] = lines[0] + "（已按预算截断）"
            if stats is not None:
                stats["truncated"] = dropped
                stats["tokens"] = count_tokens("\n".join(lines))
    return "\n".join(lines)


def _project_namespace(conn, project):
    """项目 → `code_registry.namespace`（只用于展示；查不到返回 None）。"""
    if not project:
        return None
    try:
        row = conn.execute("SELECT namespace FROM code_registry WHERE prefix=?",
                           (project,)).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _surfaces_touch(declared, actual, *, base=None, min_segments=2) -> bool:
    """声明面与实际**目录**有没有交集（任一命中即算"一致"）。

    ## 判定列纪律（2026-10-26 评审 F2）

    两侧比的都是**路径**，不是展示串：

      · `actual` 必须是 `dir_abs`（判定列），不是 `dir`（展示列）。展示列是相对
        **写入者基准**算出来的——同名的 `p0` 在不同项目里长得一模一样，拿它比
        会把"根本没有交集"判成"一致"（跨项目同名目录的假阴性/假阳性都从这里来）；
      · `declared` 先按 `base`（看板所属项目根）解析成**绝对**，与 `dir_abs` 同基准，
        再走 `path_hit`（分量边界 + `min_segments` 门槛），绝不退化成字符串前缀；
      · **基准不可证**（根板/未映射项目：相对面 + 没有 base）时**当"一致"**：
        宁可少提一句，也不许因为"证明不了"就给一句「声明≠实际」的假话
        ——假提示一旦出现，这条提示就会被学会忽略。

    其余语义同 `path_hit`（见 `fish_surface.path_hit` 的四步）。
    """
    decl = []
    for item in declared or ():
        norm = fish_surface.normalize_path(item)
        if not norm:
            continue
        if fish_surface.is_absolute(norm):
            decl.append(norm)
            continue
        resolved = fish_surface.make_abs(norm, base)
        if resolved and fish_surface.is_absolute(resolved):
            decl.append(resolved)
        else:
            return True          # 有声明面判不了 → 不下结论（宁可不说）
    acts = [fish_surface.normalize_path(a) for a in (actual or ())]
    acts = [a for a in acts if a and fish_surface.is_absolute(a)]
    if not decl or not acts:
        return True              # 有一侧没有可判定路径 → 不下结论
    for a in decl:
        for b in acts:
            if fish_surface.path_hit(a, b, min_segments=min_segments):
                return True
    return False


def _surface_overlap_hit(a, b):
    """「观察到的重叠」的一对是否命中（同基准 → `surface_hit`；跨基准 → `path_hit`）。

    2026-10-26 评审 F2：实际面改用 `dir_abs` 之后，声明面（相对）与它**不同基准**，
    继续用 `surface_hit` 会让 mixed 组合永远不命中（看着有判定、其实哑掉）；
    而把相对面当绝对比又会误报。`path_hit` 正是为跨基准保守判定存在的
    （分量边界 + `min_segments`：1 段面到处同名，不判）。
    """
    hit = surface_hit(a, b)
    if hit:
        return hit
    if fish_surface.is_absolute(a) != fish_surface.is_absolute(b):
        return fish_surface.path_hit(a, b, min_segments=2)
    return None


def _unnamed_hint(conn, citizen_id):
    """当前会话缺可读名时给一段**可执行**提示（人话 + 具体调用 + 为什么值得做）。

    ## 2026-11-22：这条提示此前**从未触发过**

    判据是 `is_auto_name(name)`，而那个函数用正则 `[0-9a-z]{6,12}` 认名字，
    生成器产出的却是 `dsh-session-88f9` 这种**尾巴带 `-`** 的形状 —— 匹配不上。
    实测 173 个自动名 **100% 漏判**，于是这条提示一直是死代码：机制在、接线在，
    只有判定坏了。

    现在 `is_auto_name` 支持传入 `host` + `session_key` 做精确比对，这里把两个
    上下文都递进去（`citizens` 行上本来就有，不需要额外查询）。

    ## 为什么只对本会话

    看板上几十个历史公民大多没名字，逐行提示等于刷屏；而"我"这一行是唯一
    现在就能改的（`rename_citizen`）。

    ## 文案为什么这么写

    用户 2026-11-22 要求：起名提示要**简单诚恳地讲清它的意义与价值，让 AI 有收获感**。
    所以除了"怎么做"，还写了"改了对你有什么好处"——认不出你是谁，要协调你的
    人就得先翻会话；有名字之后成本从"翻"变成"看一眼"。
    """
    if not citizen_id:
        return ""
    row = conn.execute(
        "SELECT display_name, host, host_session_key FROM citizens WHERE citizen_id=?",
        (citizen_id,)).fetchone()
    if not row:
        return ""
    name = (row[0] or "").strip()
    host = (row[1] or "").strip()
    sk = (row[2] or "").strip()
    if name and not fish_naming.is_auto_name(name, host=host, session_key=sk):
        return ""
    current = name or f"{fish_naming.UNNAMED_LABEL}·{host or '未知宿主'}"
    return (f"你还没有可读名（当前占位 `{current}`）。\n"
            + fish_naming.rename_guidance())


def _surface_hint(conn, board, citizen_id, session_key=""):
    """当前会话**没声明占用面**时，给一句"为什么值得填"。

    ## 为什么要这条（用户 2026-11-22 要求）

    用户要求「让 surface 默认化，并诚恳地强调它的意义与价值，让 AI 有收获感」。
    但调研发现不能按原设想讲：**碰撞在这套工作流里 20 天只发生过 1 次（目录级）、
    文件级 0 次**。所以"填了能防别人覆盖你的改动"是**假承诺**——而假提示一旦被
    验证是假的，整条提示就会被永久学会忽略（`_surfaces_touch` 的注释也写着这句）。

    诚实的价值是**可观测**：填了，看板上那一行就多出 `｜声明 p0/fish.py`，
    别的 AI（和以后翻看板的你）看得见你在动哪块。这在 89.7% 的写入都还没有
    归属的环境里尤其值钱。

    ## 三个"不说"（避免变成噪音）

      · **只对当前会话说**：看板上几十行历史公民，逐行提示就是刷屏；
      · **已经有 surface 就不说**：说一次就够；
      · **连 doing 都没有就不说**：那是"还没登记"，交给 `checkin` 的引导去管。

    返回空串表示不需要提示。
    """
    if not citizen_id or board is None or not board.get("id"):
        return ""
    sk = (session_key or "").strip()
    sql = ("SELECT surface, doing FROM presence WHERE board_id=? AND citizen_id=?"
           + (" AND session_key=?" if sk else ""))
    params = (board["id"], citizen_id) + ((sk,) if sk else ())
    row = conn.execute(sql, params).fetchone()
    if not row:
        return ""
    surface = (row[0] or "").strip()
    doing = (row[1] or "").strip()
    if surface or not doing:
        return ""
    return ("你还没声明占用面（surface）。**填了，别人在看板上就看得见你在动哪几个"
            "文件。**\n不用填全 —— 写你最不希望被别人无意识改掉的那几个就够："
            "`fish board checkin --doing \"...\" --surface p0/fish.py,p0/mcp_server.py`")


def recent_memory_code_for_session(conn, session_key, *, hours=2):
    """本会话最近写入的记忆编码；没有返回 `""`。

    ## 为什么读 `events` 而不是 `code_index`

    实测：`memory.recorded` 事件的 payload 里带 `session_actor`（全库 747 条中
    564 条有值，75.5%）；而 `code_index` 只有 `owner`（公民），**区分不出同一公民
    的不同会话**。所以"本会话写了什么"只能走事件流。

    ## 为什么限定时间窗

    跨天的"本会话"没有意义——两天前写的记忆与今天的帖子没有因果关系，
    硬关联就是编造因果。
    """
    sk = (session_key or "").strip()
    if not sk:
        return ""
    since = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(hours=max(1, int(hours)))).strftime(
                 "%Y-%m-%dT%H:%M:%SZ")
    try:
        row = conn.execute(
            "SELECT json_extract(payload,'$.code') FROM events "
            "WHERE type='memory.recorded' AND ts>=? "
            "AND json_extract(payload,'$.session_actor')=? "
            "ORDER BY seq DESC LIMIT 1", (since, sk)).fetchone()
    except sqlite3.OperationalError:      # 极老库没有 JSON1：宁可不关联，也不猜
        return ""
    return (row[0] or "").strip() if row else ""


#: 帖子里出现这个形状就算"已经写了编码"，不再自动补
_POST_CODE_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*-\d{6}-\d+\b")


def autolink_memory_code(conn, text, session_key, *, enabled=None, hours=2):
    """帖子里没写记忆编码时，自动补上本会话最近那条（**标明是自动推断**）。

    ## 为什么这么做

    实测：412 条看板帖子里只有 43 条（10.4%）含记忆编码，其余 369 条是**孤立的**
    ——内容在，但连不回记忆链。用户 2026-11-22 要求把这条接上。

    ## 三条边界

      1. **只补"最近一条"**：一个会话里可能写 5 条记忆、发 3 条帖子，谁对应谁
         无法自动判断。补最近一条是**可辩护的猜测**，补多条就是编造。
      2. **标明是自动推断**：写成 `（自动关联 CODE）` 而不是裸编码——读者要能
         分清"作者自己指的"与"系统猜的"。
      3. **已经写了就不补**：正文已有编码形状就原样返回。
    """
    body = (text or "").strip()
    # `enabled=None` = 读配置（`board.post_auto_link`）。把这次读放进函数里，
    # 是为了让调用点保持一行——`fish.py` 有 J4 行数棘轮，接线代码越薄越好。
    if enabled is None:
        enabled = bool(get_config(conn, "board.post_auto_link"))
    if not enabled or not body:
        return body
    if _POST_CODE_RE.search(body):
        return body
    code = recent_memory_code_for_session(conn, session_key, hours=hours)
    if not code:
        return body
    return f"{body}（自动关联 {code}）"


def board_out_path(data_root, board, *, home_root=None) -> Path:
    """看板渲染的**唯一落点入口**（§6.2）：`<home_root>/<board['path']>`。

    为什么要有这个函数而不让调用方自己拼：`boards.path` 的语义是"相对 fish-home"，
    任何一处自己写 `root / path` 都会把旧的"相对 data_root"语义偷偷带回来
    （`test_board*` 的三处断言就是这么红的）。测试也用它定位产物——
    "钉落点"和"读产物"用同一个入口，才不会各钉各的。
    """
    home = Path(home_root) if home_root else resolve_home_root(Path(data_root))
    return home / board["path"]


def _virtual_board(scope, scope_key, period_key=None):
    """构造一块**不落库**的看板行（读路径在没有板时用）。

    为什么需要它：`create=False` 让读路径不再建板，但上层（`_render_one`、
    `board_block` 的空板分支）仍要一个 dict 形状的板。返回"半成品 dict"和返回
    `None` 都不行——前者会让 `id=None` 悄悄流进写库路径，后者会让每个调用方
    各写一套空板文案。

    `_render_one` 只读 `scope / scope_key / period_key / status / closed_at`，
    **不读 `id`**；`board_presence_rows` / `board_post_rows` 按 `id` 查，
    `id=None` 时自然返回空 —— 这正是"没有板"该有的读数。
    """
    pk = board_period(period_key)
    key = scope_key or ""
    return {"id": None, "scope": scope, "scope_key": key, "period_key": pk,
            "path": board_relpath(scope, key, pk), "status": "open",
            "created_at": None, "closed_at": None}


def board_ensure(conn, scope, scope_key="", period_key=None, *, create=True):
    """取或建一块板，返回 `boards` 行（sqlite3.Row 风格的 dict）。

    幂等：同一 `(scope, scope_key, period_key)` 只会有一行（UNIQUE 约束保证）。
    **不渲染**——渲染是独立一步（`board_render`），因为读路径要尽量轻。

    ## `create=False`：读路径**只查不建**（2026-11-22 · 空看板治理 A+C）

    实测证据：库里 146 块板有 **78 块空板**（无任何 `board_posts`），而 `board_ensure`
    有 10 个调用点，其中 **3 条是纯读路径**（`board_block` 注入渲染 / `board_show` 查板 /
    `board_render_root` 聚合）。读一次就建一块板 —— 那 78 块里的大部分是这么来的，
    不是"没人用"。

    不堵住读路径，任何清理策略（sweep 加空判、空板不渲染）都会退化成"清了又长"。

    `create=False` 且没有板时返回 `None`，调用方自行处理；**绝不返回半成品 dict**
    ——半成品会被上层当成"板存在但 id 是 None"，然后在写库时炸在很深的地方。
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
    if not create:
        return None
    now = now_utc()
    cur = conn.execute(
        "INSERT INTO boards (scope, scope_key, period_key, path, status, created_at) "
        "VALUES (?,?,?,?, 'open', ?)", (scope, scope_key, period_key, path, now))
    conn.commit()
    return {"id": cur.lastrowid, "scope": scope, "scope_key": scope_key,
            "period_key": period_key, "path": path, "status": "open",
            "created_at": now, "closed_at": None}


def board_checkin(conn, citizen_id, doing="", surface="", scope="project",
                  scope_key="", period_key=None, agent_id=None, status="active",
                  *, session_key=""):
    """签到：登记"我来了、我在做什么、我占了什么面"，返回看板行。

    **只写自己行**——2026-10-26 起"自己"的精度是 `(board_id, citizen_id, session_key)`
    （撞车自撞修复 T01 · 设计 45 §2.1）：同一公民的两个会话各占一行，`doing` 不再互相
    覆盖。upsert 本身是原子的，`first_seen_at` 由 INSERT 一次定死、DO UPDATE 不碰它，
    所以重复签到保住首签时间。

    为什么 `doing` 必填（哪怕只给空字符串也记一行）：看板的核心价值就是
    **别人靠这一句判断"我会不会跟它撞车"**。没有 doing 的在场行等于没签到。

    ## `session_key`（仅关键字、默认 `''`）

    `''` = 无会话归属（CLI 手工签到 / 无凭证路径）。此时行为与今天**逐字节相同**：
    同一 `(board, citizen)` 反复签到仍只有一行——既有 70 处调用点一个都不用改。
    写侧形态由 `_presence_write_shape` 单点决定（三列主键 / 两列降级），本函数不猜。
    """
    board = board_ensure(conn, scope, scope_key, period_key)
    if board["status"] == "closed":
        raise ValueError(f"看板已归档（closed），不再接收签到：{board['path']}")
    now = now_utc()
    doing = (doing or "").strip()[:BOARD_DOING_MAX]
    surface = (surface or "").strip()
    sk = (session_key or "").strip()
    with_session, conflict = _presence_write_shape(conn)
    if with_session:
        conn.execute(
            "INSERT INTO presence (board_id, citizen_id, session_key, agent_id, status, "
            "doing, surface, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?) "
            f"ON CONFLICT({conflict}) DO UPDATE SET "
            "  agent_id=excluded.agent_id, status=excluded.status, doing=excluded.doing, "
            "  surface=excluded.surface, last_seen_at=excluded.last_seen_at, "
            # 2026-10-26（T03）：**手工 checkin = 意图行**，所以清掉 `auto_source`。
            # 自动刷新只写 auto_* 三列、绝不碰 doing——「手工行不被自动刷新覆盖」
            # 这条红线靠"doing 是否是自动占位"再兜一层（见 `_is_auto_doing`）。
            "  auto_source=NULL",
            (board["id"], citizen_id, sk, agent_id or sk or None, status, doing, surface,
             now, now))
    else:
        # 两列主键（迁移未跑 / 故障降级）：**与今天逐字节相同**，且不写 session_key 列
        # （那列可能压根不存在）。这是故障兜底，不是第二套语义。
        conn.execute(
            "INSERT INTO presence (board_id, citizen_id, agent_id, status, doing, surface, "
            "first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?) "
            f"ON CONFLICT({conflict}) DO UPDATE SET "
            "  agent_id=excluded.agent_id, status=excluded.status, doing=excluded.doing, "
            "  surface=excluded.surface, last_seen_at=excluded.last_seen_at, "
            "  auto_source=NULL",
            (board["id"], citizen_id, agent_id or sk or None, status, doing, surface,
             now, now))
    conn.commit()
    return board


def _is_auto_doing(doing) -> bool:
    """这一行的 `doing` 是不是**自动占位**（而不是谁主动声明的一句话）。"""
    return _AUTO_DOING_MARK in (doing or "")


def _auto_doing_text(source) -> str:
    """自动登记的占位文案：**把来源写进去**，读者一眼看得出这不是谁的主张。"""
    return {
        # 注意：`自动登记` 这四个字是**标记**（`_is_auto_doing` 靠它识别自动行），
        # 不能去掉；这里改的只是"还没说在做什么"这个含糊描述。
        "session_start": "（自动登记：会话开始，尚未声明在做什么）",
        "session_inject": "（自动登记：会话开始，尚未声明在做什么）",
        "remember": "（自动登记：正在写记忆）",
    }.get(source or "", "（自动登记）")


def board_auto_checkin(conn, citizen_id, *, project=None, session_key=None,
                       source="session_start", period_key=None) -> dict:
    """**自动签到**：SessionStart / 写记忆时把这一行建出来或刷新（用户 2026-10-26 拍板）。

    ## 为什么要有它

    > 很多人不 checkin 不是因为不愿意，是**记不住**。如果 `remember` 或写入时
    > 能自动带一条极简 presence（甚至只是"某公民今天有活动"），看板就不会是 0 人。
    > —— `EVAL-261025-16` 建议 3

    用户 2026-10-26 进一步拍板：**「自动行」算登记**。所以它不只是兜底，
    而是"开工即可见"的默认路径；软门（`board_checkin_or_remind`）据它判"已登记"。

    ## 四条红线

    1. **不覆盖人的意图**：只有 `doing` 是空或**自动占位**时才改写它；
       手工写的一句话永不被自动刷新冲掉（覆盖 = 把"我在做 X"改成"自动登记" = 毁信息）。
    2. **不编造占用面**：`surface` 留空。编造的 surface 会制造假重叠，
       而重叠警告一旦掺假就会被学会忽略（`EVAL-261025-13` 的教训）。
    3. **来源可解释**：`auto_source` 记 `session_start` / `remember` / `activity`，
       第 3 条值由采集层写（`fish_activity.refresh_presence_cache`）。
    4. **失败不拖垮调用方**：会话启动不能被看板拖垮，所以调用方一律 try/except。

    返回 `{board, created, refreshed, skipped}`；`skipped` 非空表示这次没动（附原因）。
    """
    if not get_config(conn, "board.auto_presence"):
        return {"created": False, "refreshed": False, "skipped": "board.auto_presence=false"}
    scope, key = ("project", project) if project else ("root", "")
    try:
        board = board_ensure(conn, scope, key, period_key)
        if board["status"] == "closed":
            return {"board": board, "created": False, "refreshed": False,
                    "skipped": "看板已归档"}
        now = now_utc()
        sk = (session_key or "").strip()
        with_session, conflict = _presence_write_shape(conn)
        if with_session and not sk:
            # **无会话键的自动行不许制造影子行**：该公民在本板**只有一行**时落到那一行。
            # 现场：MCP 的 `board checkin` 现在会写会话键，而 `remember` 的兜底行若落到
            # `''`，同一公民就多出一条「自动登记」影子行——看板上看见两行，其中一行的
            # doing 是假的。有 ≥2 行时才写 `''`（那才是诚实的"不知道是哪个窗口"）。
            existing = conn.execute(
                "SELECT session_key FROM presence WHERE board_id=? AND citizen_id=?",
                (board["id"], citizen_id)).fetchall()
            if len(existing) == 1:
                sk = str(existing[0][0] or "")
        # 2026-10-26（撞车自撞修复 T02）：自动行也按**会话**定位。
        # 旧实现只按 `(board, citizen)` 找行，于是同公民第 2 个会话的 `doing`
        # 会直接改写第 1 个会话的自动占位——正是设计 P1/P5 那两个病根。
        where = "board_id=? AND citizen_id=?" + (" AND session_key=?" if with_session else "")
        key_params = (board["id"], citizen_id) + ((sk,) if with_session else ())
        row = conn.execute(
            f"SELECT doing, auto_source FROM presence WHERE {where}", key_params).fetchone()
        if row is None:
            cols = ["board_id", "citizen_id"]
            values = [board["id"], citizen_id]
            if with_session:
                cols.append("session_key")
                values.append(sk)
            cols += ["agent_id", "status", "doing", "surface", "first_seen_at",
                     "last_seen_at", "auto_surface", "auto_seen_at", "auto_source"]
            values += [sk or None, "active", _auto_doing_text(source), "", now, now, "",
                       now, source]
            conn.execute(
                f"INSERT INTO presence ({', '.join(cols)}) VALUES "
                f"({','.join('?' * len(cols))}) ON CONFLICT({conflict}) DO NOTHING",
                tuple(values))
            conn.commit()
            return {"board": board, "created": True, "refreshed": False, "skipped": None}
        doing, auto_source = row[0] or "", row[1] or ""
        if _is_auto_doing(doing) or not doing.strip():
            conn.execute(
                "UPDATE presence SET last_seen_at=?, auto_seen_at=?, auto_source=?, "
                f"doing=? WHERE {where}",
                (now, now, source, _auto_doing_text(source)) + key_params)
        else:
            # 手工行：只刷自动时间与来源，**不动 doing / surface / last_seen_at**
            conn.execute(
                "UPDATE presence SET auto_seen_at=?, auto_source=? "
                f"WHERE {where}", (now, source) + key_params)
        conn.execute(
            f"UPDATE presence SET agent_id=? WHERE {where} "
            "AND agent_id IS NULL AND ? IS NOT NULL",
            (sk or None,) + key_params + (sk or None,))
        conn.commit()
        return {"board": board, "created": False, "refreshed": True, "skipped": None}
    except (sqlite3.Error, ValueError) as exc:
        return {"created": False, "refreshed": False,
                "skipped": f"{type(exc).__name__}: {exc}"}


def display_text(text):
    """把一段文本截成**渲染用**的显示版本（B-1：库里存全文，显示才截）。

    为什么单独抽出来：`board_post` 曾自己截断（写入时），而 `board_post_rows`
    直接返回库里的值——两处关于"哪一步截断"的理解不一致，就是数据丢失的来源。
    现在只有这一处截断，写的和读的都调它（或者是它的逆：写不截、读截）。
    """
    t = text or ""
    if len(t) > BOARD_MAX_POST_CHARS:
        return t[:BOARD_MAX_POST_CHARS - 1] + "…"
    return t


def board_post(conn, board, citizen_id, kind, text, *, session_key=""):
    """往看板"今日动态"加一条。**存全文**，渲染时才截断。

    `kind`：checkin | progress | done | note
    为什么动态单独一张表而不复用 `events`：events 是**不可变事实流**，
    而看板动态是**可滚动的展示层**——把展示层塞进事实流，
    会让"重建投影"多一类要处理的事件（`rebuild_projection` 已经够复杂了）。

    ## 2026-11-21 改（系统级修复 B-1）：从「写入时截断」改成「渲染时截断」

    旧实现在这行就截：`text = text[:199] + "…"`，而**表定义与常量注释都写着
    「渲染时硬截断」——两处注释在说谎**。

    后果是**原文永久丢失**：czhy--开发-3 读到一条只剩 200 字的 note，作者写的
    「保留引用 / 开分支约定 / 沙箱构建坑」三项全没了，而且没有任何地方能查到全文
    （知识库路径当时又离线）。**截断本身没错，错的是截在一个不可恢复的位置。**

    老数据不需要区分：它们本来就只有 200 字，丢失的部分已经追不回来。
    新数据从这一刻起是完整的，`board_post_rows` 返回显示用的截断版本。
    """
    text = (text or "").strip()
    if not text:
        return None
    # `session_key`（2026-11-22 · Wave 2）：把帖子绑到会话上，才能回头做
    # 「本条帖子对应哪条记忆」的关联。空串合法（无会话归属，与 presence 同口径）。
    cur = conn.execute(
        "INSERT INTO board_posts (board_id, citizen_id, kind, text, created_at,"
        " session_key) VALUES (?,?,?,?,?,?)",
        (board["id"], citizen_id, kind, text, now_utc(),
         (session_key or "").strip()))
    conn.commit()
    return cur.lastrowid


def board_post_full(conn, post_id):
    """取一条动态的**全文**（B-1 的配套读口）。

    为什么要它：`board_post_rows` 返回的是显示用截断文本，所以"我想看全文"
    必须有另一条路——否则用户知道有全文却取不到，和之前没区别。
    """
    row = conn.execute(
        "SELECT id, citizen_id, kind, text, created_at FROM board_posts WHERE id=?",
        (post_id,)).fetchone()
    if not row:
        return None
    return {"id": row[0], "citizen_id": row[1], "kind": row[2],
            "text": row[3], "created_at": row[4],
            "chars": len(row[3] or ""),
            # 是否超过显示上限——调用方据此判断"要不要提示有全文"
            "truncated_for_display": len(row[3] or "") > BOARD_MAX_POST_CHARS}


def _was_truncated_at_write(text):
    """这条是**旧实现留下的**截断文本吗（长度刚好 200 且以 `…` 结尾）。

    只用于提示，不用于任何判定：老数据与"刚好 200 字且以省略号收尾的新数据"
    在这个判据下无法区分。**宁可少提示一次，也不假装能区分**——所以措辞是
    「可能是历史数据」，不是「这是历史数据」。
    """
    t = text or ""
    return len(t) == BOARD_MAX_POST_CHARS and t.endswith("…")



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

    ## 2026-10-26（T03）③ 自动行算登记

    用户拍板「看板自动行算登记」，所以：
      · 有自动行（SessionStart/写记忆建的）且**新鲜** → `(True, None)`，不再说"你还没签到"；
      · 自动行但**过旧** → 仍返回 `False` 且提示里保留「分钟没更新」字样（兼容既有判据），
        再补一句「这是自动登记，补一句 doing 别人才能判断撞车」——
        它不是否定"算登记"，只是让 `doing` 有价值（占位文案判断不了撞车）。

    判定基准用 `effective_last = max(last_seen_at, auto_seen_at)`（见 `effective_last`）：
    一个正在连续写代码的会话，不该因为"没重跑 checkin"被判成没更新。
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    rows = conn.execute(
        "SELECT p.last_seen_at, p.auto_seen_at, p.doing, b.scope, b.scope_key "
        "FROM presence p JOIN boards b ON b.id = p.board_id "
        "WHERE p.citizen_id=? AND b.period_key=? AND p.status != 'left'",
        (citizen_id, board_period())).fetchall()
    if not rows:
        return False, ("你还没签到——看板上没有你的行（也还没有自动登记）。"
                       f"用 `fish board checkin --doing \"在做什么\" --project {project or '(根)'}` 登记，"
                       "别人才能判断会不会跟你撞车。")

    best = None                       # (minutes, doing, scope, scope_key)
    for last, auto, doing, sc, sk in rows:
        eff = max(last or "", auto or "")
        t = parse_utc(eff)
        mins = 0 if t is None else max(int((now - t).total_seconds() // 60), 0)
        if best is None or mins < best[0]:
            best = (mins, doing or "", sc, sk)
    mins, doing, sc, sk = best
    if mins <= BOARD_PRESENCE_IDLE_SECONDS // 60:
        return True, None
    where = ("根看板" if sc == "root" else f"{sc}:{sk}")
    tail = ("如果还在做同一件事，重跑一次 checkin 刷新一下；"
            "做完了就用 `board(action=\"post\", kind=\"done\", text=\"…\")` 标掉——"
            "否则看板会一直显示一件早就做完的事。")
    if _is_auto_doing(doing):
        return False, (
            f"你的 doing 已 {mins} 分钟没更新（{where} 上只有自动登记「{doing}」）。"
            "**这是自动登记，不算你的声明**——补一句 doing，别人才能判断会不会撞车。"
            + tail)
    return False, (
        f"你的 doing 已 {mins} 分钟没更新（{where} 上写的是「{doing or '未写'}」）。" + tail)


def board_auto_presence(conn, citizen_id, project=None, *, session_key=""):
    """`remember` 时的**兜底 presence**：委托 `board_auto_checkin(source='remember')`。

    2026-10-26（T03）：本函数原来是"只在无行时 INSERT"的独立实现；现在自动签到
    与它是同一件事（区别只有 `doing` 的来源标注与 `auto_source` 记账），
    所以**合流**——两条路径各写一份必然漂（这正是本项目反复付学费的那类问题）。

    返回语义保持旧契约：`True` = 这次新建了一行；`False` = 已有行（只刷新或没动）。
    """
    res = board_auto_checkin(conn, citizen_id, project=project,
                             session_key=(session_key or None), source="remember")
    return bool(res.get("created"))


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


def citizen_labels(conn, citizen_ids, *, scope_key=None) -> dict:
    """一批公民 → **可读标签**（`{citizen_id: label}`）。人类面的唯一取名入口。

    ## 为什么必须集中（设计 §6.3）

    看板渲染、动态行、重叠警告、残留行、根板汇总、注入包、MCP `board show` —— 7 处
    都在印名字。以前它们各印各的（多数直接印 26 位 ULID），于是"看板上认不出是谁"。

    ## 三条规则（`fish_naming.disambiguate` 是唯一实现）

      · 有 `display_name` → 原样；
      · 没有 → `未命名·<host>`（**绝不返回 citizen_id 片段**）；
      · 重名 → `名字·<host>`；host 也重 → `名字·<host>#2`（按 created_at 稳定排序）。

    `scope_key` 目前不参与取名（项目名在标题/注入头上出现一次即可）。
    参数留着是有意的：将来若要做"同名不同项目"的消歧，入口已经在这里了，
    不必再去改 7 个渲染点。
    """
    _ = scope_key                       # 见 docstring：项目名不参与取名
    ids = [str(c) for c in (citizen_ids or ()) if c]
    if not ids:
        return {}
    out = {}
    # SQLite 的变量上限是 999：按 500 个一批查，够用且不必担心边界。
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        rows = conn.execute(
            "SELECT citizen_id, display_name, host, created_at FROM citizens "
            "WHERE citizen_id IN (" + ",".join("?" * len(chunk)) + ")", chunk).fetchall()
        out.update(fish_naming.disambiguate(
            [{"citizen_id": r[0], "display_name": r[1], "host": r[2], "created_at": r[3]}
             for r in rows]))
    for cid in ids:
        out.setdefault(cid, fish_naming.base_label("", "", cid))
    return out


def who_label(row, *, with_id: bool = False) -> str:
    """把一个在场/动态行渲染成**人能认的标签**（2026-10-26 契约变更）。

    ## 契约（用户 2026-10-26 拍板：不要 ULID）

      · 有名字 → `anyi-studio-主AI`
      · 没名字 → `未命名·dsh`（host 也空 → `未命名`）
      · 重名 → 由 `citizen_labels` 消歧成 `名字·host` / `名字·host#2`
      · **任何人类可读面都不出现 26 位公民 ID**；要 ID 请传 `with_id=True`
        （机器视图/调试专用，看板/注入包一律不传）。

    ## 这是**有意的破坏性变更**

    旧实现是 `名字（短 id）`、无名字时只给短 id，`test_board_feedback3` U1 三条
    钉着那个行为。用户拍板后改契约 → 那三条判据**一起改**（不是放宽），
    并用变异自证证明"改回短 id 就红"（设计 §5.7 契约变更清单）。
    """
    try:
        cid = row["citizen_id"] if isinstance(row, dict) else row[0]
        name = (row.get("display_name") if isinstance(row, dict) else None) or ""
        host = (row.get("host") if isinstance(row, dict) else None) or ""
        precomputed = (row.get("label") if isinstance(row, dict) else None) or ""
    except (KeyError, IndexError, TypeError):
        return str(row)
    if cid == "ASSUMED":
        return "（simulate 假设的人）"
    # 行里已经带了**消歧后的标签**时直接用（`board_presence_rows` 会批量算好）。
    # 为什么要走这条路：重名消歧需要**整行集合**（谁和谁同名），单行内部算不出来；
    # 批量算一次比每个渲染点各算一次更准，也不会出现"看板消歧了、注入包没消歧"。
    if precomputed:
        return precomputed if not with_id else f"{precomputed}（{(cid or '?')[:8]}）"
    label = fish_naming.base_label(name, host, cid)
    if with_id:
        short = (cid or "?")[:8]
        return f"{label}（{short}）"
    return label


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
        eff = effective_last(p)            # 2026-10-26：基准改为「声明 ∪ 实际」
        if eff:
            t = parse_utc(eff)
            if t is not None:
                fresh = (now - t).total_seconds() <= window * 60
            # parse 失败 → 保持 fresh=True（宁可算它有效，不静默漏报）
        if not fresh:
            continue
        people.append({"citizen_id": p["citizen_id"],
                       "session_key": p.get("session_key") or "",
                       "doing": p["doing"],
                       "label": who_label(p), "surface": normalize_surface(p["surface"])})

    out = []
    seen = set()
    for i in range(len(people)):
        for j in range(i + 1, len(people)):
            a, b = people[i], people[j]
            # 同一行**不会**出现两次（presence 主键是 `(board, citizen, session)`），
            # 但读侧降级/手工数据都可能造出重复键——先挡住，免得报"自己跟自己撞"。
            if (a["citizen_id"], a["session_key"]) == (b["citizen_id"], b["session_key"]):
                continue
            for pa in a["surface"]:
                for pb in b["surface"]:
                    hit = surface_hit(pa, pb)
                    if hit:
                        # **按 (a, b, hit) 去重**：B 声明目录 `p0` 时，它会同时命中
                        # A 的 `p0/fish.py` 与 `p0/test_board.py`，而两次的共同前缀
                        # 都是 `p0`——不去重就会把同一条重叠打印两遍
                        # （2026-10-25 端到端实测抓到）。对读者来说"说了两遍"
                        # 等于"有两处冲突"，那是错误信息。
                        # 2026-10-26：去重键加**会话键**——同公民两个会话各算一条参与者
                        # （它们的声明面可能相同，但"该找谁"是两个不同的窗口）。
                        key = (a["citizen_id"], a["session_key"],
                               b["citizen_id"], b["session_key"], hit)
                        if key in seen:
                            continue
                        seen.add(key)
                        # `a`/`b` 保留**公民 ID**：机器调用与旧测试按 ID 比对，
                        # 追加 `a_label`/`b_label` 给人类面（设计 §5.7 契约变更）；
                        # `a_session`/`b_session`/`same_citizen` 是 2026-10-26 的加法。
                        out.append({"a": a["citizen_id"], "b": b["citizen_id"],
                                    "a_label": a["label"], "b_label": b["label"],
                                    "at": hit,
                                    "a_session": a["session_key"],
                                    "b_session": b["session_key"],
                                    "same_citizen": a["citizen_id"] == b["citizen_id"]})
    return out


def board_observed_overlaps(conn, board, *, window_min=None) -> list:
    """**观察到的**重叠（声明面 ∪ 实际面）——推断，与"声明重叠"分开展示。

    为什么必须分开（设计 §6.2 / T03 要点 5）：声明是人的意图，实际是发生的事实。
    把两者混成一句"占用面重叠"，读者就分不清"该去找谁协调"还是"要不要提醒某人更新 doing"。
    这一节的名字里就有「推断」两个字，正是为了不把事实当声明。

    判定用 `fish_surface.surface_hit` / `path_hit`（同一套前缀语义），**不引入第二套规则**。
    返回 `[{"a","b","a_label","b_label","at","kind"}]`，`kind`∈{declared,actual,mixed}。

    ## 2026-10-26 评审 F2：实际面取 `dir_abs`（判定列），不取 `dir`（展示列）

    展示列 `dir` 是**相对写入者基准**算出来的：两个人在不同项目里各写一个 `p0`，
    `dir` 都是 `'p0'` → 这一节会报一条「推断重叠」，而事实是两块不相干的地方。
    判定统一用 `dir_abs`（`dir_of(path_abs)`，永远绝对）。

    两侧基准不同时（声明相对、实际绝对）走 `_surface_overlap_hit` → `path_hit`：
    mixed 组合靠它维持判定能力，同时 1 段面仍被 `min_segments` 挡住。
    """
    window = int(window_min if window_min is not None
                 else get_config(conn, "board.collision_window_min"))
    now = datetime.datetime.now(datetime.timezone.utc)
    people = []
    for p in board_presence_rows(conn, board):
        if p["status"] == "left":
            continue
        declared = normalize_surface(p["surface"])
        # 只收证明得了绝对路径的目录（判定列）；证明不了的不进判定（不编）
        actual = [d["dir_abs"] for d in (p.get("auto_dirs") or []) if d.get("dir_abs")]
        fresh = False
        for ts in (p.get("last_seen_at"), p.get("auto_seen_at")):
            t = parse_utc(ts)
            if t is not None and (now - t).total_seconds() <= window * 60:
                fresh = True
        if not fresh and not actual:
            continue
        people.append({"citizen_id": p["citizen_id"],
                       "session_key": p.get("session_key") or "",
                       "label": who_label(p),
                       "declared": declared, "actual": normalize_surface(",".join(actual))})

    out, seen = [], set()
    for i in range(len(people)):
        for j in range(i + 1, len(people)):
            a, b = people[i], people[j]
            # 防御式跳过（设计 §3.5 的第 3 条）：`(cid, sk)` 相同就是同一个参与方。
            # 现在结构上不可能发生（一行/会话），但把"声明/实际"拆成两行的那天就会，
            # 成本 2 行，防的是回归。
            if (a["citizen_id"], a["session_key"]) == (b["citizen_id"], b["session_key"]):
                continue
            for kind, pa_list, pb_list in (
                    ("declared", a["declared"], b["declared"]),
                    ("actual", a["actual"], b["actual"]),
                    ("mixed", a["declared"], b["actual"]),
                    ("mixed", a["actual"], b["declared"])):
                for pa in pa_list:
                    for pb in pb_list:
                        hit = _surface_overlap_hit(pa, pb)
                        if not hit:
                            continue
                        key = (a["citizen_id"], a["session_key"],
                               b["citizen_id"], b["session_key"], hit, kind)
                        if key in seen:
                            continue
                        seen.add(key)
                        out.append({"a": a["citizen_id"], "b": b["citizen_id"],
                                    "a_label": a["label"], "b_label": b["label"],
                                    "at": hit, "kind": kind,
                                    "a_session": a["session_key"],
                                    "b_session": b["session_key"],
                                    "same_citizen": a["citizen_id"] == b["citizen_id"]})
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
        eff = effective_last(p)
        if eff:
            t = parse_utc(eff)
            if t is not None:
                fresh = (now - t).total_seconds() <= window * 60
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
        if p["status"] == "left":
            continue
        eff = effective_last(p)
        t = parse_utc(eff)
        if t is None:
            continue
        mins = int((now - t).total_seconds() // 60)
        if mins > window:
            stale.append({"citizen_id": p["citizen_id"], "label": who_label(p),
                          "doing": p["doing"],
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
    labels = citizen_labels(conn, [p[0] for p in posts])
    lines = [f"（昨日板 {_fmt_period(prev)} 仍开着，最后 {len(posts)} 条——"
             "跨午夜开工的话先看这里：）"]
    for cid, kind, txt, ts in reversed(posts):
        lines.append(f"  · {_hhmm(ts)} {labels.get(cid) or cid} {kind}：{txt[:60]}")
    return "\n".join(lines)


def effective_last(row) -> str:
    """一行的**有效最后活跃时间** = `max(last_seen_at, auto_seen_at, last_action_at)`。

    为什么要它（设计 §6.2）：手工签到的 `last_seen_at` 只在人 checkin 时刷新，
    而写动作会持续刷新 `auto_seen_at`（缓存，节流 20s）与 `last_action_at`（事实）。
    同一个"30 分钟没动 = idle"的判定，用哪个时间当基准，结论完全不同——
    一个在连续写代码的会话不该被标成 offline。

    为什么把**事实表**的 `last_action_at` 也算进来：缓存是节流的（可能落后 20s）
    且可能被清空；**真相在 `tool_activity`**。无自动数据（旧行）时它就是
    `last_seen_at`，与旧行为逐字一致。
    """
    if not isinstance(row, dict):
        return ""
    return max(row.get("last_seen_at") or "", row.get("auto_seen_at") or "",
               row.get("last_action_at") or "")


def _age_minutes(ts) -> "float | None":
    """UTC ISO → 距今分钟数（负数截 0）；解析不了返回 None（**不假装刚签过**）。"""
    parsed = parse_utc(ts)
    if parsed is None:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    return max(int((now - parsed).total_seconds() // 60), 0)


def _collapse_presence_raw(rows):
    """把同公民的多个会话行折成一行（`board.presence_session_rows=false` 的止血档）。

    返回 `(折叠后行, {citizen_id: 折叠前会话数})`。

    口径：`max(last_seen_at, auto_seen_at)` 最新的那行做主行——**只用于展示/判定读侧**，
    库里的会话行一条都不删（把配置改回 true 就恢复，秒级可回）。

    为什么在**取名字之前**折：折叠后再算标签，`session_labels` 看到"该公民只有 1 个会话"
    → 自动不追加 `·dsh#xxxx`，于是渲染/`board_overlaps`/`board show` 一起回到今天的样子
    ——一个开关管三处，不会出现"看板折了、show 没折"的分裂。
    """
    counts, order, best = {}, [], {}
    for row in rows:
        cid = row[1]
        counts[cid] = counts.get(cid, 0) + 1
        if cid not in best:
            order.append(cid)
            best[cid] = row
            continue
        if max(row[6] or "", row[11] or "") > max(best[cid][6] or "", best[cid][11] or ""):
            best[cid] = row
    return [best[cid] for cid in order], counts


def board_presence_rows(conn, board):
    """在场表：谁在这块板上、声明在做什么、**实际在动什么**。

    `status` 的 **idle 是渲染时算的**，不写库：写库需要定时任务，
    而"30 分钟没动"这种判断随时可以重算——把可重算的派生态写进库，
    只会制造"库里的值和真实情况不一致"的新问题。

    ## 2026-10-26（T03）新增字段，一条都不改旧语义

      · `host` / `auto_surface` / `auto_seen_at` / `auto_source`（自动占用的缓存）；
      · `auto_dirs`：自动面的读时聚合（`[{dir,dir_abs,count,last}]`，来自 `tool_activity`；
        `dir` 是**展示**列（相对写入者基准）、`dir_abs` 是**判定**列（`dir_of(path_abs)`，
        评审 L1 修复：判定只认它）；
      · `action_count` / `last_action_at`：本板周期内该公民的写动作总数与最近一次；
      · `state`：`active | idle | offline | left` 四档（`status` 仍是旧三值）；
      · `state_inferred`：state 是**推断**出来的（超时/自动时间），不是人的声明。

    为什么 `status` 不动：`board_overlaps`/`board_stale_rows`/看板渲染/既有测试
    全部按 active|idle|left 判断；把四档塞进 status 会一夜之间改掉所有语义。
    四档放在 `state` 里，是**新增**而不是替换。
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    active_window = int(get_config(conn, "board.active_window_sec"))
    window_min = int(get_config(conn, "activity.aggregate_window_min"))
    out = []
    # 自动面聚合只算一次（不要每行查一次库）。两份：公民级（兼容档）+ 会话级（精确档）
    try:
        auto_map = fish_activity.auto_surfaces(conn, window_min=window_min)
    except sqlite3.Error:
        auto_map = {}
    try:
        auto_by_session = fish_activity.auto_surfaces_by_session(
            conn, window_min=window_min)
    except sqlite3.Error:
        auto_by_session = {}
    # 2026-10-25（`EVAL-261025-16`）：**joins citizens 拿可读名**。
    # 2026-10-26 追加 `host`（重名消歧要用）与 `created_at`（消歧顺序要稳定）。
    # 2026-10-26（撞车自撞修复）：多取 `p.session_key`——列不在时读 `''`，
    # 于是"迁移未跑"的库仍然可读（设计 §2.1.1 的读侧兜底）。
    sk_col = "p.session_key" if _presence_has_session(conn) else "''"
    # ⚠ 这条 SQL 必须保持**一个字符串常量**（用 `.format` 而不是 `+` 拼）：仓库里有
    # 三处判据按"常量以 SQL 关键字开头且提到 citizens"机械清点 citizens 读点
    # （`test_identity_p1a::citizens_sql_by_function` / `test_activity` 的同款扫描）。
    # 拆成拼接后这个读点会从清单里消失——护栏看不见它，才是最坏的"改绿"。
    raw = conn.execute(
        "SELECT {sk}, p.citizen_id, p.agent_id, p.status, p.doing, p.surface, "
        "p.last_seen_at, c.display_name, c.kind, c.host, "
        "p.auto_surface, p.auto_seen_at, p.auto_source, c.created_at "
        "FROM presence p LEFT JOIN citizens c ON c.citizen_id = p.citizen_id "
        "WHERE p.board_id=? ORDER BY p.last_seen_at DESC".format(sk=sk_col),
        (board["id"],)).fetchall()
    # 读侧降级（§6.3）：false = 按公民折叠回"一行/公民"（止血档；库里一条不删）
    folded_sessions = {}
    if not get_config(conn, "board.presence_session_rows"):
        raw, folded_sessions = _collapse_presence_raw(raw)
    # 会话键先**解析**再取名：F1（列优先 + 旧列兜底）会改键，标签集合必须用同一个键，
    # 否则"列为空、agent_id 形如会话键"的行会查不到标签，人类面掉成 `未命名`。
    resolved = [fish_activity.entry_session_key(r[0], r[2]) for r in raw]
    labels = fish_naming.session_labels(
        [{"citizen_id": r[1], "session_key": resolved[i][0], "display_name": r[7],
          "host": r[9], "created_at": r[13]} for i, r in enumerate(raw)])
    for r, (sk, sk_src) in zip(raw, resolved):
        (session_col, cid, agent, st, doing, surface, last, dname, ckind, chost,
         auto_surface, auto_seen, auto_source, _created) = r
        # 实际面按会话取（三档渐进，F2）：有会话键就只认自己那个会话
        info = fish_activity.presence_auto_info(cid, sk, auto_by_session, auto_map)
        auto_dirs = list(info.get("dirs") or [])
        action_count = int(info.get("count") or 0)
        last_action = info.get("last") or auto_seen or ""
        # 有效最后活跃 = 声明 ∪ 缓存 ∪ **事实**（真相在 tool_activity，见 effective_last）
        eff = max(last or "", auto_seen or "", last_action or "")
        idle = False
        if eff:
            t = parse_utc(eff)
            if t is not None:
                idle = (now - t).total_seconds() > BOARD_PRESENCE_IDLE_SECONDS
        status = "idle" if (idle and st == "active") else st
        out.append({
            "citizen_id": cid,
            "session_key": sk,
            "session_key_source": sk_src,
            "session_short": fish_naming.session_short(sk),
            "agent_id": agent,
            "display_name": dname or "", "kind": ckind or "", "host": chost or "",
            "label": labels.get((cid, sk)) or fish_naming.base_label(dname, chost, cid),
            "status": status,
            "doing": doing or "", "surface": surface or "", "last_seen_at": last,
            "auto_surface": auto_surface or "", "auto_seen_at": auto_seen or "",
            "auto_source": auto_source or "",
            "auto_dirs": auto_dirs, "action_count": action_count,
            "last_action_at": last_action,
            "state": _presence_state(status, eff, now, active_window),
            "state_inferred": status != "left",
        })
    # 同公民在本板的会话数（机器字段；渲染侧据它决定"要不要出会话后缀"）
    counts = {}
    for row in out:
        counts[row["citizen_id"]] = counts.get(row["citizen_id"], 0) + 1
    for row in out:
        row["sessions_of_citizen"] = max(counts.get(row["citizen_id"], 1),
                                        int(folded_sessions.get(row["citizen_id"]) or 0))
    return out


def _presence_state(status, eff_last, now, active_window_sec=300) -> str:
    """四档推断：`left` / `active`（≤5 分钟）/ `idle`（≤30 分钟）/ `offline`。

    为什么 active 的窗口（5 分钟）比 idle（30 分钟）窄：**在动**比**在不在**更严格。
    为什么 `offline` 只是推断：没有 SessionEnd 事件可听（设计 §7），
    所以"人走了"只能由时间推出来——**推断必须标注**，不能把超时当事实。
    """
    if status == "left":
        return "left"
    minutes = _age_minutes(eff_last)
    if minutes is None:
        return "active"                    # 时间戳坏了 → 宁可当在（不静默隐藏）
    if minutes * 60 <= max(int(active_window_sec), 1):
        return "active"
    if minutes * 60 <= BOARD_PRESENCE_IDLE_SECONDS:
        return "idle"
    return "offline"


def _state_suffix(state, inferred) -> str:
    """看板/注入包里的状态文案：**推断必须标注**（`idle（推断）`）。"""
    if state in ("left", "active"):
        return state
    return f"{state}（推断）" if inferred else state


def board_post_rows(conn, board, limit=BOARD_POSTS_PER_FILE):
    """今日动态：按时间**倒序**（最新的在最上面，符合"此刻"的语义）。

    **返回显示用文本**（B-1：库里是全文，这里才截到 `BOARD_MAX_POST_CHARS`）。
    要全文用 `board_post_full(conn, post_id)`。
    """
    rows = conn.execute(
        "SELECT citizen_id, kind, text, created_at FROM board_posts "
        "WHERE board_id=? ORDER BY id DESC LIMIT ?", (board["id"], limit)).fetchall()
    return [(c, k, display_text(t), ts) for c, k, t, ts in rows]


def _actual_dirs_text(p, limit=2) -> str:
    """「实际在动」一格的文案：`p0（12 次写，21:03）`；多目录最多 2 个 + `…`。

    无数据 → `NO_ACTIVITY_TEXT`（FB-06）：这一格是**事实**，没数据就说没数据，
    不许用一个破折号让人自己猜是"没动"还是"没观测"。
    """
    dirs = p.get("auto_dirs") or []
    if not dirs:
        return NO_ACTIVITY_TEXT
    parts = []
    for item in dirs[:limit]:
        parts.append(f"{item['dir']}（{item['count']} 次写，{_hhmm(item.get('last'))}）")
    text = "、".join(parts)
    if len(dirs) > limit:
        text += "…"
    return text


def board_unchecked_sessions(conn, board, presence=None) -> dict:
    """「在动但未签到」：会话级读时聚合，并排除已签到的会话。

    返回 `{"window_min", "project", "sessions", "raw", "checked", "error"}`：

      · `sessions`：过滤后的会话行（`fish_activity.activity_sessions` 的原样结构）；
      · `raw`：窗口内该项目的**会话总数**（过滤前）——用来区分
        "观测不到（没有活动数据）"与"都已签到（在场表里）"，这两种"空"的
        含义完全不同，混成一句"无"就是在骗读者；
      · `checked`：被排除掉的会话数。

    ## 排除为什么同时看 citizen_id 与 agent_id

    `presence` 的主键是 `(board_id, citizen_id)`，签到行的 `agent_id` 可能存会话键
    （`board_auto_checkin(session_key=...)` 就是），也可能为空（手工 checkin 不传）。
    只看其中一边都会漏：只看 citizen 会漏"agent 已登记但 citizen 记录不同步"，
    只看 agent 会把手工签到行的会话又列一遍。

    ## 只对**项目板**生效

    根板/开发树板的 `scope_key` 不是项目名（根板是空串、开发树是分支名），
    拿它去查 `tool_activity.project` 只会混算出别的项目的会话。所以非 project
    scope 一律返回空结果——**宁可不显示，也不显示错的**。
    """
    project = board.get("scope_key") if board.get("scope") == "project" else ""
    window_min = int(get_config(conn, "activity.aggregate_window_min"))
    result = {"window_min": window_min, "project": project or None,
              "sessions": [], "raw": 0, "checked": 0, "error": None}
    if not project:
        return result
    try:
        raw = fish_activity.activity_sessions(conn, project=project,
                                              window_min=window_min)
    except (sqlite3.Error, ValueError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    result["raw"] = len(raw)
    pres = board_presence_rows(conn, board) if presence is None else presence
    # 2026-10-26（撞车自撞修复 T03）：排除口径从"公民或 agent_id"改成**两级**：
    #
    #   ① **会话级**（主口径）：这个 `session_key` 已经在场表里出现过 → 它签过了。
    #      判的是"会话"，不是"主权"：同一会话键即使被记在另一个公民名下，
    #      它仍然是同一个窗口（D2 的第二种形态就是"会话键记在 agent_id 上"）。
    #   ② **公民级**（只对 `session_key=''` 的行）：这类行**没有会话归属**
    #      （"这个人签过了，但不知道是哪个窗口签的"），保守地不把它名下的会话列出来。
    #      有会话键的行**绝不**这样覆盖别人——那正是旧实现漏列同公民另一会话的病根（S22）。
    checked_sessions = set()
    citizen_level = set()
    for p in pres:
        cid = p.get("citizen_id")
        sk = p.get("session_key") or ""
        if sk:
            checked_sessions.add(sk)
        elif cid:
            citizen_level.add(cid)
    kept = [s for s in raw
            if (s.get("session_key") or "") not in checked_sessions
            and s.get("citizen_id") not in citizen_level]
    result["sessions"] = kept
    result["checked"] = len(raw) - len(kept)
    return result


def _unchecked_dirs_text(session, limit=3) -> str:
    """「在动目录」一格：`p0（12 次）、fish-devA/p0（3 次）`；无目录 → `—`。"""
    dirs = session.get("dirs") or []
    if not dirs:
        return "—"
    parts = [f"{d['dir']}（{int(d['count'])} 次）" for d in dirs[:limit]]
    if len(dirs) > limit:
        parts.append("…")
    return "、".join(parts)


def render_unchecked_section(conn, board, pres) -> list:
    """渲染「在动但未签到」一节，返回 markdown 行（不出现时返回 `[]`）。

    ## 什么时候出这一节（三条，缺一条就会重新变成 FB-14）

      1. 本板在场表**非空** → 出（哪怕结果是 `_（无）_`：读者需要知道"看过、没有"）；
      2. 在场表为空，但项目里有窗口内活动 → 出（**这正是最该被看见的那一屏**）；
      3. 两者皆空（空看板）→ **什么都不加**（用户明确要求：空看板保持原样）。

    ## 为什么"空"也要写清楚原因

    `_（无）_` 后面必须跟一句括号说明：是"没有活动数据（纯 MCP 宿主没有 hook）"、
    "都已签到"、还是"读失败"。只印一个"无"，读者会当成"没人在动"——
    而这三件事的处置完全不同。
    """
    data = board_unchecked_sessions(conn, board, pres)
    sessions = data["sessions"]
    if not pres and not data["raw"] and not data.get("error"):
        return []                                  # 空看板：保持原样
    lines = ["", f"{UNCHECKED_HEADING} · 窗口 {data['window_min']} 分钟", "",
             UNCHECKED_SCOPE_NOTE, ""]
    if sessions:
        labels = citizen_labels(conn, [s["citizen_id"] for s in sessions
                                       if s.get("citizen_id")])
        lines += ["| 会话 | 公民 | 在动目录 | 写次数 | 最近 |", "|---|---|---|---|---|"]
        for s in sessions:
            cid = s.get("citizen_id")
            who = (labels.get(cid) if cid else "") or UNCHECKED_UNKNOWN_CITIZEN
            # 2026-10-26 QA F2：人类面**不出现完整会话键**（设计 §4.1 判据 S18 后半）。
            # 用与看板/注入包同一个 `session_short`（单点）；完整键只在机器面
            # （`board show --json` / `board_unchecked_sessions` 的返回里）保留。
            sk_text = (fish_naming.session_short(s.get("session_key"))
                       or fish_naming.NO_SESSION_SHORT)
            lines.append(
                f"| {sk_text} | {who} | {_unchecked_dirs_text(s)} "
                f"| {int(s.get('writes') or 0)} | {_hhmm(s.get('last_seen'))} |")
        lines += ["", UNCHECKED_NOTE]
        return lines
    if data.get("error"):
        why = f"活动数据读取失败（{data['error']}）"
    elif data["raw"]:
        why = "窗口内的会话都已签过到（见上面的在场表）"
    else:
        why = "窗口内没有观测到写动作（纯 MCP 宿主没有 hook 就不会有活动数据）"
    lines.append(f"{UNCHECKED_EMPTY}（{why}；窗口 {data['window_min']} 分钟）")
    return lines


def _presence_heading(pres) -> str:
    """「在场」标题：单会话公民 → `在场（N 人）`（与今天逐字节相同）；
    有公民多会话 → `在场（N 人 · M 会话）`（一列一行，M 才是有意义的规模读数）。
    """
    people = len({p.get("citizen_id") for p in pres})
    if len(pres) > people:
        return f"## 在场（{people} 人 · {len(pres)} 会话）"
    return f"## 在场（{people} 人）"


def _render_one(conn, board, posts, heading_suffix=""):
    """渲染一块板的 markdown 正文（不含容量判断）。

    ## 2026-10-26（T03）列结构变更（`09` 契约）

    `会话 | 状态 | 声明在做 | 实际在动 | 最后活跃`

      · 「声明在做」= 手工 doing（自动行显示 `（自动登记：…）` 并标 `自动`）；
      · 「实际在动」= `tool_activity` 读时聚合（**事实**），没数据就是 `—`；
      · 「状态」四档 `active/idle/offline/left`，非 left/active 一律带 `（推断）`；
      · **会话列只出可读名**（`who_label`），26 位 ULID 一个都不出现。
    """
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
        _presence_heading(pres),
        "",
    ]
    if pres:
        lines += ["| 会话 | 状态 | 声明在做 | 实际在动 | 最后活跃 |",
                  "|---|---|---|---|---|"]
        for p in pres:
            doing = (p["doing"] or "—").replace("|", "\\|")
            if _is_auto_doing(p["doing"]):
                doing += " `自动`"
            # 声明面放进「声明在做」这一格（**不丢信息**）：它回答"声明要动哪块"，
            # 而「实际在动」是事实。设计给的 5 列里没有单独的 surface 列，
            # 但把 surface 从看板上抹掉等于让"谁占了哪块"只能去重叠段里找——
            # 而重叠段只在对撞时出现（只有一个人时它永远是空的）。
            surf = normalize_surface(p["surface"])
            if surf:
                doing += "（占 " + "、".join(surf[:2]) + ("…" if len(surf) > 2 else "") + "）"
            actual = _actual_dirs_text(p).replace("|", "\\|")
            state = _state_suffix(p["state"], p["state_inferred"])
            lines.append(f"| {who_label(p)} | {state} | {doing} | {actual} "
                         f"| {_hhmm(effective_last(p))} |")
    else:
        lines.append("_（还没有人签到——**未登记 = 未发生**）_")
    # 表下方的一行说明（FB-06）：列头里放不下"这一列依赖什么"，而那句话正是
    # "为什么这里全是「无活动数据」"的答案。有表才写——没有表时说明也没有对象。
    if pres:
        lines += ["", ACTIVITY_COLUMN_NOTE]

    # 在动但未签到（FB-14 · 2026-10-26）：**推断**，只读 `tool_activity`；空看板不加空节。
    lines += render_unchecked_section(conn, board, pres)

    # 占用面重叠警告（2026-10-25 · `EVAL-261025-09`）：
    # 这是把"声明"变成"防线"的那一步。在此之前 surface 只是被存起来，
    # 防撞车全靠人肉比字符串——而那一步我们历史上死过一次（EXP-17）。
    overlaps = board_overlaps(conn, board)
    if overlaps:
        lines += ["", "### ⚠ 占用面重叠（开工前先协调）", ""]
        for ov in overlaps[:10]:
            lines.append(f"- `{ov['at']}` ← {ov.get('a_label', ov['a'])} ↔ "
                         f"{ov.get('b_label', ov['b'])}")
        if len(overlaps) > 10:
            lines.append(f"- …共 {len(overlaps)} 处重叠")
    # 观察到的重叠（声明 ∪ 实际）：推断，单独一节，避免把事实当声明
    observed = board_observed_overlaps(conn, board)
    if observed:
        lines += ["", "### 观察到的重叠（推断：声明面 ∪ 实际在动）", ""]
        for ov in observed[:6]:
            kind = {"declared": "双方声明", "actual": "双方实际在写",
                    "mixed": "一方声明/一方实际"}.get(ov["kind"], ov["kind"])
            lines.append(f"- `{ov['at']}` ← {ov['a_label']} ↔ {ov['b_label']}（{kind}）")
        if len(observed) > 6:
            lines.append(f"- …共 {len(observed)} 处（含推断）")
    # 残留行（已闲置超时但仍声明占用）：不叫"冲突"，也不无痕删除
    stale = board_stale_rows(conn, board)
    if stale:
        lines += ["", f"### 已闲置但仍声明占用（{len(stale)} 行，**不算重叠**）", ""]
        for s in stale[:10]:
            lines.append(f"- {s.get('label') or s['citizen_id']}"
                         f"（{s['minutes']} 分钟未动，doing「{s['doing']}」）")

    lines += ["", f"## 今日动态（{len(posts)} 条，最新在上）", ""]
    if posts:
        # 动态行的名字也走 citizen_labels（一次批量取名，避免逐条查库）
        post_labels = citizen_labels(conn, [p[0] for p in posts])
        for cid, kind, text, ts in posts:
            lines.append(f"- **{_hhmm(ts)}** {post_labels.get(cid) or cid} · {kind}：{text}")
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


def board_render_root(conn, root, period_key=None, *, home_root=None):
    """把今天所有**有效**子板聚合成根板（全局视图）。

    ## 落点（2026-10-26 · 裁决 C3）

    `root` 仍是 **data_root**（调用方一个字不用改，§6.2）；文件写到
    `board_out_path(root, board)` = `<home_root>/<board['path']>`。
    新增的 `home_root` 是**仅关键字**参数，缺省自动解析（`resolve_home_root`）。

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
    total_people = 0        # 按**公民**去重（"几个人在动"）
    total_sessions = 0      # 人次（"有几个窗口在动"）——会话维度之后这两个数不一样了
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
            people = len({p.get("citizen_id") for p in pres})
            total_people += people
            total_sessions += len(pres)
            who = "、".join(who_label(p) for p in pres[:2]) + (
                "…" if len(pres) > 2 else "") if pres else "—"
            label = key if scope == "project" else f"{scope}:{key}"
            doing = (pres[0]["doing"] if pres else "") or "—"
            last = f"{_hhmm(posts[2])} {posts[0]}：{posts[1][:24]}" if posts else "—"
            # 会话数只在**真的多于人数**时出现：单会话场景的文本与今天逐字节相同
            bulk = f"（{len(pres)} 会话）" if len(pres) > people else ""
            lines.append(f"| `{label}` | {people} 人{bulk}（{who}） | {doing} | {last} |")
    else:
        lines.append("_（今天还没有任何项目开工——**未登记 = 未发生**）_")
    lines += ["", f"合计：{len(rows)} 块有效看板，{total_people} 人在场"
                  f"（{total_sessions} 人次）。", ""]

    # 2026-11-22：这里**保留建板**（不打 `create=False`）。
    #
    # 我一开始把 `board_render_root` 归成"读路径"，理由是它只聚合不产生内容。
    # 但它是**渲染器**：产物是一份写到磁盘的门面文件。`test_root_board.py::R5`
    # 钉着"根板空状态必须明写"——今天一个项目都没开工时，根板文件仍要存在，
    # 里面写"今天还没有任何项目开工"。所以它是**写路径**，该建就建。
    board = board_ensure(conn, "root", "", pk)
    text = "\n".join(lines)
    home = Path(home_root) if home_root else resolve_home_root(Path(root), conn=conn)
    p = board_out_path(root, board, home_root=home)
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

    ## 空板提前归档（2026-11-22 · 空看板治理 A）

    实测：146 块板里 **78 块没有任何 `board_posts`**。只按 7 天日期归档等于让一块
    从没被用过的板占 7 天——它和留 1 天对读者没有区别，但对 `boards` 表是 7 倍。

    所以加第二条判据：**没有动态且超过 `board.empty_sweep_days`（默认 1）就归档**。
    两道判据是 `OR` 关系：老板按日期走，空板按空走，各自独立。

    为什么空板也要留 1 天而不是当场归档：**今天的板随时可能收到第一条动态**。
    `empty_sweep_days=0` 会把当天刚签到的板立刻关掉，那是错的。

    ## 显式 `--days` 优先级更高

    `--days N` 有判据钉着"保留最近 N 天"的语义（`test_board_archive.py::C3/C4`）。
    空板规则**只在 `days is None`（默认策略路径）时生效**——显式参数不被默认策略
    劫持。维护台账调用的是 `board_sweep(conn, root)`，所以它走默认策略、空板规则生效。
    """
    # **显式 `--days N` 时只按日期走，空板规则不参与**（2026-11-22 实测修正）。
    #
    # 为什么：`--days N` 的语义是"保留最近 N 天"，它有判据钉着
    # （`test_board_archive.py::C3/C4`）。第一版让空板规则与它 OR，结果
    # `--days 7` 把 3 天前的空板也归档了 —— **一个显式参数被默认策略劫持**。
    #
    # 分工：`--days` 是人的显式意图，字面执行；空板提前归档是**默认策略**，
    # 只走 `board_sweep(conn, root)` 这条（维护台账用的就是它）。
    explicit = days is not None
    days = int(days if days is not None else get_config(conn, "board.sweep_days"))
    empty_days = 0 if explicit else int(get_config(conn, "board.empty_sweep_days") or 0)
    if days < 1 and empty_days < 1:
        return {"swept": 0, "cutoff": None, "empty_cutoff": None,
                "empty_swept": 0, "closed": []}
    today = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=8))).date()
    # **语义：`--days N` = "保留最近 N 天"**（含今天）。
    # 所以 7 天 = 10/19..10/25，**10/18 及更早归档** → cutoff 就是今天-N，用 `<=`。
    # 为什么不是 `<`：那样 10/18 会留在边界外（"保留 8 天"），
    # 而 README 级的预期是"保留 7 天"。2026-10-25 实测抓到：`--days 7` 归档 0 块。
    # `days < 1` 时用一个永远匹配不到的值（`YYMMDD` 字典序里没有比 "000000" 更小的）
    cutoff = ((today - datetime.timedelta(days=days)).strftime("%y%m%d")
              if days >= 1 else "000000")
    # 空板那条判据的截止日；`empty_days < 1` 时用 None（SQL 里 `? IS NOT NULL` 为假）
    empty_cutoff = ((today - datetime.timedelta(days=empty_days)).strftime("%y%m%d")
                    if empty_days >= 1 else None)
    now = now_utc()
    rows = conn.execute(
        "SELECT id, scope, scope_key, period_key, "
        "  EXISTS(SELECT 1 FROM board_posts p WHERE p.board_id = boards.id) AS has_post "
        "FROM boards "
        "WHERE status='open' AND ("
        "    period_key <= ?"
        "    OR (? IS NOT NULL AND period_key <= ?"
        "        AND NOT EXISTS(SELECT 1 FROM board_posts p WHERE p.board_id = boards.id))"
        ") ORDER BY period_key, id", (cutoff, empty_cutoff, empty_cutoff)).fetchall()
    closed = []
    empty_swept = 0
    for bid, scope, key, pk, has_post in rows:
        conn.execute("UPDATE boards SET status='closed', closed_at=? WHERE id=?",
                     (now, bid))
        closed.append({"id": bid, "scope": scope, "scope_key": key, "period_key": pk,
                       "empty": not has_post})
        if not has_post:
            empty_swept += 1
    conn.commit()
    # 归档会改变"有效看板"集合 → 根板跟着刷
    # S9（第四轮 · 评审 🟡-N8）：与 `board_render` 同款——失败**不打断 sweep**（根板是聚合
    # 视图），但必须对调用方可见（stderr）。原来这里是 `except Exception: pass`，
    # "根板停在昨天"这件事对渲染者完全不可见，属于上轮 R19 漏掉的同类。
    if root is not None and closed:
        try:
            board_render_root(conn, root)
        except Exception as exc:            # noqa: BLE001 —— 见上：不阻断 sweep，但不许静默
            print(f"警告：根板刷新失败（归档已完成 {len(closed)} 块）：{_exc_text(exc)}",
                  file=sys.stderr)
    return {"swept": len(closed), "cutoff": cutoff, "empty_cutoff": empty_cutoff,
            "empty_swept": empty_swept, "closed": closed}


def board_render(conn, root, board, *, home_root=None):
    """把看板渲染成文件，返回 `(相对路径, 字节数, 是否切分)`。

    ## 落点（2026-10-26 · 裁决 C3）

    `root` 仍是 **data_root**（7 个位置调用 + mcp_server 的 2 处一个字不用改）；
    文件写到 `board_out_path(root, board)` —— 即 `<home_root>/<board['path']>`。
    为什么签名"不改、语义改内部"：467 个既有测试与 `mcp_server.py` 都是位置调用，
    改参数顺序会把一次"落点迁移"变成"全库调用面迁移"（§6.2）。

    **容量纪律（硬）**：单文件 32KB。超了按字节预算切开——
    最新的那份写主文件 `看板-YYYYMMDD.md`，更早的写 `看板-YYYYMMDD-<n>.md`，
    主文件顶部留指针。`active-sessions` v1 就是死在"永远追加不切分"上
    （10 天 114 条 → 396KB → 超读取上限），这个判据不许放松。
    """
    root = Path(root)
    home = Path(home_root) if home_root else resolve_home_root(root, conn=conn)
    posts = board_post_rows(conn, board, limit=BOARD_POSTS_PER_FILE)
    # 2026-11-22：**设计稿里的"C 空板不产文件"已撤回**，理由写在下方。
    #
    # 原设想：空板渲染出来只有标题 + 空表，没有信息量，省掉一份门面文件。
    # 实测推翻：`test_board.py::B4` 钉着一条更早的契约 ——
    #   「渲染必须有两区：在场表 + 今日动态；**空看板要明写而不是留白**」
    # 以及 `board_block` 的设计约束 3：「留白会让 AI 以为看板这个功能坏了」。
    # 那两条讲的不只是注入包，**文件本身也是给人读的门面**：
    # 文件不存在与"今天没人开工"是两件事，删掉文件就把后者变成前者。
    #
    # 所以 C 的真实收益（少 16 个文件）**小于**"读者分不清没开工与坏了"的代价。
    # 空板治理只保留 A（提前归档）+ 读路径不建板（堵住源头）。
    presence_only = len(_render_one(conn, board, []).encode("utf-8"))
    # 主文件预算 = 上限 - 在场表基线 - 指针句余量
    budget = max(1024, BOARD_MAX_BYTES - presence_only - 400)
    kept, rest = _pack_by_bytes(conn, board, posts, budget)

    shard_paths = []
    if rest:
        # 分片：同样按字节装，每片一个 `-<n>` 后缀（相对路径拼接，与 home 无关）
        shard_no, cur = 1, rest
        while cur:
            part, cur = _pack_by_bytes(conn, board, cur, budget)
            if not part:          # 单条就超预算：硬放一条，避免死循环
                part, cur = cur[:1], cur[1:]
            shard = dict(board)
            shard["path"] = board["path"][:-3] + f"-{shard_no}.md"
            p = board_out_path(root, shard, home_root=home)
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
    p = board_out_path(root, board, home_root=home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")

    # 2026-10-25（用户要求）：**随子板刷新而刷新根板**。
    # 挂在这里是刻意的——`board_render` 是子板刷新的**唯一出口**
    # （签到 / 发动态 / 手动 render 三条路都经过它），挂别处就一定会漏掉几条。
    #
    # 2026-10-26 第二轮（R19 · 评审 🟡-4）：根板失败**不再被 `except: pass` 吞掉**。
    # 根板是聚合视图，失败不该拖垮子板那次渲染（所以仍不抛），但"根板停在昨天"
    # 这件事必须对渲染者可见——否则渲染看起来成功、看板其实是旧的。
    if board.get("scope") != "root":
        try:
            board_render_root(conn, root, board["period_key"], home_root=home)
        except Exception as exc:            # noqa: BLE001 —— 见上：不拖垮子板，但不许静默
            print(f"警告：根板刷新失败（子板已写）：{_exc_text(exc)}",
                  file=sys.stderr)

    return board["path"], len(text.encode("utf-8")), bool(shard_paths)


def board_show(conn, scope, scope_key="", period_key=None):
    """读一块板（不渲染）：返回板信息 + 在场 + 动态。

    ## 读路径**只查不建**（2026-11-22 · 空看板治理 A+C）

    旧实现走 `board_ensure` 的建板分支：`fish board show` 查一块不存在的板，
    会**顺手把它建出来**。于是"看一眼"与"发生了一件事"在库里长得一样。

    现在没有板时返回一块**不落库的虚拟板**（`_virtual_board`，`id=None`）：
    接口形状不变（`_render_one` 只读 scope/scope_key/period_key/status/closed_at），
    但库里不多一行。
    """
    board = board_ensure(conn, scope, scope_key, period_key, create=False)
    if board is None:
        board = _virtual_board(scope, scope_key or "", period_key)
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


def _home_guard_dirs():
    """R14 的"绝不允许"清单：盘根 + 系统目录 + 用户家目录根（按环境变量取，取不到就跳过）。

    `mode`：系统目录用 `inside`（写在 `C:\\Windows\\…` 下面同样是事故），
    用户家目录只用 `equals`——家目录**根**不许用，但"家目录下的某个子目录"属于
    "非标准但可授权"（那是人自己的目录，不该被硬禁）。
    """
    guards = []
    for key in ("WINDIR", "SystemRoot", "ProgramFiles", "ProgramFiles(x86)",
                "ProgramW6432"):
        raw = os.environ.get(key)
        if raw:
            guards.append((f"系统目录 {key}", Path(raw), "inside"))
    try:
        guards.append(("用户家目录根", Path.home(), "equals"))
    except (KeyError, RuntimeError):        # 无 HOME/USERPROFILE 的环境
        pass
    return guards


def _guard_home_location(home, *, project_zone, allow_external=False):
    """R14（安全 F13）：显式 `--home` / `layout.home_root` 的落点校验。

    两道：
      ① **硬禁**：盘根（`C:\\` 这种）、`%WINDIR%` / `%ProgramFiles%`、用户家目录根。
         这些位置"写进去看起来也成功"，坏起来是系统级事故。
      ② **非标准位置需显式开关**：不在项目根那一片（`project_zone`）的路径要
         `--allow-external-home`——指错一个字符不该悄悄换掉整座门面的落点。
    """
    home = Path(home)
    anchor = Path(home.anchor) if home.anchor else None
    if anchor is not None and home == anchor:
        raise RuntimeError(
            f"门面根不能是盘根：home={home}。请给一个具体目录"
            f"（标准位置：{Path(project_zone) / HOME_DIRNAME}）。")
    for label, path, mode in _home_guard_dirs():
        hit = (home == path) if mode == "equals" else _path_inside(home, path)
        if hit:
            raise RuntimeError(
                f"门面根不能是{label}：home={home}（{path}）。"
                f"请换 --home 到项目根附近（标准位置：{Path(project_zone) / HOME_DIRNAME}）。")
    if not _path_inside(home, project_zone):
        if not allow_external:
            raise RuntimeError(
                f"门面根不在项目目录内：home={home}，项目目录={project_zone}。"
                "确需写到别处请显式加 `--allow-external-home`。")


def resolve_home_root(data_root, *, override=None, conn=None,
                      allow_external_home=False):
    r"""门面根解析（§1.4）。四档，顺序固定，每档都可解释（档位越高优先）。

    ⓪ `override`（CLI `--home`）——绝对路径；相对路径按 `data_root` 解析
    ① `config layout.home_root`——同上（需要传 `conn`；给"换布局"与"故意指错"两个用途）
    ② 标准布局：`data_root.name == 'data'` 且 `data_root.parent.name == 'p0'`
       → `data_root.parent.parent / 'fish-home'`（= <项目根>/fish-home，裁决 Q3）
    ③ 兜底：`data_root.parent / 'fish-home'`（= data_root 的**兄弟**目录）

    ## 为什么规则 ② 不是唯一规则

    真实布局是 `<项目根>/p0/data`：`data_root.parent` 是 `p0`，直接用它会得到
    `p0/fish-home`——违反裁决 Q3（门面必须与 `p0/` 同级）。

    ## 为什么规则 ③ 是 `parent` 而不是 `parent.parent`

    实测 26 个测试文件的临时根都是 `Path(mkdtemp()) / "data"`（如 test_board / test_root_board），
    `parent.parent` 会写到系统临时目录根上，直接毁掉隔离性；而 `parent` 是
    `<tmp>/fish-home`，既有 teardown（`rmtree(self.root.parent)`）会把它一起清掉。
    **"home 落在会被清理的那一层"是规则 ③ 的唯一理由**——下一批改 teardown 时别误改。

    ## 四条断言（写在代码里，不是写在文档里）

    - `home_root` 解析后**不得等于或位于 `data_root` 内**（HOME-6 的第一道闸）；
    - `home_root` 不得位于任何已登记 vault 内（否则门面会污染知识库）；
      ↑ 2026-10-26 第二轮追加两条（R14 · 安全 F13）：
    - **硬禁**：盘根、`%WINDIR%`、`%ProgramFiles%`（含 x86/ProgramW6432）、用户家目录根
      ——`--home C:\Windows` 会往系统目录写 20 个 md，这是误操作面，不是能力；
    - **非标准位置需显式开关**：不在项目根那一片的路径必须 `--allow-external-home`。
      否则"指错一个字符"就换了一个落点，而渲染看起来一切正常。
    """
    data_root = Path(data_root)
    # 先归一成绝对路径再套规则：`Path("data").parent.parent` 还是 `"."`，
    # 相对路径会让规则②算出 `p0/fish-home`（而不是与 p0 同级的 `fish-home`）。
    # 这不是"顺手 resolve"，是规则本身要求"看名字"必须在绝对路径上看。
    base = Path(os.path.realpath(str(data_root)))
    explicit = None                          # "是否显式指定了 home"（R14 的分支依据）
    raw = None
    if override not in (None, ""):
        raw = Path(override)
        explicit = "override"
    elif conn is not None:
        try:
            cfg = (get_config(conn, "layout.home_root") or "").strip()
        except (KeyError, sqlite3.Error):
            cfg = ""
        if cfg:
            raw = Path(cfg)
            explicit = "config"

    # 规则②/③的"标准落点"始终先算出来：它是 R14 判断"这个位置算不算标准"的基准。
    if base.name == "data" and base.parent.name == "p0":
        default_home = base.parent.parent / HOME_DIRNAME
        project_zone = base.parent.parent
    else:
        default_home = base.parent / HOME_DIRNAME
        project_zone = base.parent

    if raw is None:
        home = default_home
    else:
        # 相对 `--home` 按 **data_root.parent** 解析。
        # 设计原文写的是"按 data_root 解析"，但那条与本函数末尾"home 不得位于 data_root 内"
        # 的断言自相矛盾（相对路径必然落进 data_root，于是这个开关永远不可用）。
        # 以断言为准（HOME-6 是硬约束）；相对路径取项目根侧，得到的仍是"p0 的邻居"那一层。
        home = raw if raw.is_absolute() else base.parent / raw

    try:
        home = home.resolve()
    except OSError:                     # 3.11 的非严格 resolve 仍可能撞 OSError（环/权限）
        home = Path(os.path.abspath(str(home)))
    real_data = base

    if home == real_data or _path_inside(home, real_data):
        raise RuntimeError(
            f"门面根不能等于或位于 data_root 内：home={home}，data_root={real_data}。"
            "门面是投影，写进真相根就不再是投影（HOME-6）。")

    if explicit:
        _guard_home_location(home, project_zone=project_zone,
                             allow_external=allow_external_home)

    if conn is not None:
        try:
            vaults = conn.execute("SELECT name, path FROM kb_vault").fetchall()
        except sqlite3.Error:
            vaults = []
        for vname, vpath in vaults:
            if vpath and _path_inside(home, Path(vpath)):
                raise RuntimeError(
                    f"门面根不能位于已登记的知识库内：home={home}，"
                    f"vault={vname}（{vpath}）。请换 --home 或调整 layout.home_root。")
    return home


def _board_entry_of(conn, prefix, as_of, *, home_root=None):
    """看板入口：优先"as-of 当日的项目板"，否则"最近一条 open 板" → `(相对链接, 来源)`。

    返回的是**相对状态表所在目录**的 markdown 链接（状态表在 `10-项目库/项目状态表.md`），
    所以链接形如 `P1/看板-261026.md`。`layout verify` 对这类链接只记 warning
    （`board_entry_missing`）：今天没人开工是合法状态，不该把整份体检升级成 error。

    为什么不给"当日闭板"也算：闭板意味着这一天已经收工，链接仍指它没有坏处，
    但"今日无看板"的三种 `—` 之一必须在**真的没有板**时出现（PST-5）。所以只有
    当日板（open/closed 都算）+ 最近 open 板两条来源。
    """
    pk = as_of.strftime("%y%m%d")
    row = conn.execute(
        "SELECT path, period_key FROM boards WHERE scope='project' AND scope_key=? "
        "AND period_key=? ORDER BY id DESC LIMIT 1", (prefix, pk)).fetchone()
    source = f"boards.path(period={pk})"
    if row is None:
        row = conn.execute(
            "SELECT path, period_key FROM boards WHERE scope='project' AND scope_key=? "
            "AND status='open' ORDER BY period_key DESC, id DESC LIMIT 1",
            (prefix,)).fetchone()
        source = "boards.path(最近 open 板)"
    if row is None:
        return PST_DASH_NO_BOARD, "boards(无板)"
    rel_path = str(row[0] or "")
    name = Path(rel_path).name
    if home_root is not None:
        target = Path(home_root) / rel_path
        base = Path(home_root) / "10-项目库"
        try:
            rel = os.path.relpath(str(target), str(base)).replace(os.sep, "/")
        except ValueError:                 # 不同盘符（理论上不可能，但别让渲染炸）
            rel = name
    else:
        rel = rel_path[len("10-项目库/"):] if rel_path.startswith("10-项目库/") else rel_path
    return f"[{name}]({rel})", source
