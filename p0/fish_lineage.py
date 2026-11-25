#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话树投影层：节点模型 + 唯一的写原语 + 深度重算 + 防环 + 回填/对账（113 §2.4/§10）。

> **W2 已填**：`recompute_depth` 落地（113 §3.3），DSH 适配器的真实现进了**叶子模块**
> `fish_lineage_dsh.py`（`DshLineageAdapter` / `read_header` / `classify` 六判据）。
> **W6 已填**：`detect_cycles`（walk 求反 → cycle/orphan/conflict 三清单 + 标环）落地（113 §10），
> 而 `scan` / `report`（历史回填 dry-run + 只读对账）进了**批量层** `fish_lineage_backfill`
> ——那里要装影子库、`_backups` 快照与报告渲染，塞进本文件会顶破 J4 的 700 行窗口
> （仓库规矩是「先抽层再抬窗口」，先例：W1b 把 DDL 抽成 `fish_lineage_schema`）。
> 方向是单向的：批量层 import 本模块，**本模块看不见批量层**。
> 到这里本模块**没有空壳**了：剩下的 `DshLineageAdapter`/`ADAPTERS` 从来就不是壳——
> 它们是 W2/W3 的**懒加载真实现**（`_LazyAdapter`），首次调用才 import 叶子模块。

## 这一波（W1）为什么几乎什么都不"做"

W1 只搭骨架：**一行数据都不写、一个既有查询都不改**（113 §2.1）。所以本模块里
只有 `upsert_node` 是真实现——它是全仓**唯一**的 `session_lineage` 写口，W2/W3/W6
都只经由它写投影。

为什么空壳要"响"而不是返回空结果：返回 `{}` 或 `[]` 会让调用方以为"算过了、没有
异常"，而 W2/W6 的判据（J-D1 深度重算、J-D3 环）正是靠"算出来的数不对"才会红。
**空壳返回假值 = 把判据变成恒绿**。

## 依赖方向：stdlib + 最底层的两个模块

本模块**只 import 标准库 + `fish_lineage_schema`（零 import 的 DDL 常量叶子）+
`fish_common`（全仓最底层的工具层，`test_lineage_schema.LineageLayeringTest` 的
白名单里只有这两个）**。理由不是洁癖：W2/W3 的宿主适配器（`fish_lineage_dsh` /
`fish_lineage_wb`）会 import `fish_corpus_*`，而那些模块最终 import `fish`；只要本模块
不反向依赖任何业务模块，`fish_db`/`fish_schema` 将来要引用它也不会成环（113 §2.5 的
叶子模块纪律，先例：`fish_git_schema` 同样是"只含字符串常量、谁都能安全依赖"的一层）。

## 三条不变式（W1 用库级 CHECK 钉住，W2 起才有行）

1. `(host, session_id)` 是主键；`session_id` **保持宿主原始形态**，不加 `host:` 前缀
   （113 §1-7）。语料里 742 行 WB 的 `session_id` 已经是 `agent-*`（= 文件名），
   改来源 = 主键整批漂移。
2. `parent_session` 是**裸父 id**；`origin='main'` 时它必须是 NULL（`_SESSION_LINEAGE_DDL`
   的末条 CHECK = 110 §6 J-D4 的库级兜底）。
3. `lineage_path` 随深度线性变长（深 100 层就上千字符）：**只许用于展示、不许用于判据**
   （113 §2.8）。判深度请读 `depth`（由链重算）而不是数 `lineage_path` 里的斜杠。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Protocol, runtime_checkable

import fish_common                               # 最底层工具层：时间口径只此一处

from fish_lineage_schema import _LINEAGE_SCHEMA   # 纯 DDL 叶子：建表/建索引的唯一来源


def ensure_tables(conn: sqlite3.Connection) -> None:
    """幂等：建会话树两张投影表 + 5 条索引（旧库升级路径；113 §2.2 / §2.5）。

    为什么它在 `migrate()` 里、而不是只靠 `SCHEMA`：`CREATE TABLE IF NOT EXISTS` 对
    **已存在**的旧库什么都不做，"升级"这件事只能由 migrate 负责——它是每条打开路径
    （CLI 各命令 / MCP server / 测试装配）都会跑的那一步。

    为什么一条 `executescript` 建表+建索引：DDL 与索引在同一个常量
    （`fish_lineage_schema._LINEAGE_SCHEMA`）里，全新库与旧库升级执行的是**同一个对象**，
    不会漂成两份拷贝；「先表后索引」的顺序纪律就写在那条常量的拼装顺序里。

    **只做结构：不写任何行、不改任何既有查询**（113 §2.1）。表里 0 行本身就是 W1 的
    判据（J-W1-1），而「只建表」这件事也必须能被变异证明（见 `test_lineage_schema`）。
    """
    conn.executescript(_LINEAGE_SCHEMA)


#: `session_lineage` 的**列契约**：一行的全部字段（与 `_SESSION_LINEAGE_DDL` 逐列对应）。
#:
#: 刻意不含 `retracted_at`：撤回顾名思义是**另一个动作**（W6 对账用），不该被一次普通
#: upsert 顺手清掉或顺手写上。少了这一条，`upsert_node` 的整行覆盖会把"已撤回"的行
#: 重新变成"有效行"，而没有任何判据看得出来。
NODE_FIELDS = ("host", "session_id", "parent_session", "lineage_path", "depth", "depth_host",
               "origin", "relation", "agent_id", "agent_role", "agent_label", "agent_path",
               "cwd", "agent_preset", "basis", "source", "extra", "observed_at")

#: 省略时取**表的 DEFAULT** 的两列（不是"随便给个 0"：DDL 里写的就是这两个默认值）。
_NODE_DEFAULTS = {"lineage_path": "", "depth": 0}

#: NOT NULL **且无 DEFAULT** 的列：缺一个就让 `upsert_node` 当场 `ValueError`。
_NODE_REQUIRED = ("host", "session_id", "origin", "basis", "source", "observed_at")


class SessionNode(dict):
    """一个会话节点 = `NODE_FIELDS` 的 dict（dict 子类，便于 JSON 化与逐字段对拍）。

    刻意**不做字段校验**：校验只在 `upsert_node` 一处做。两处各校验一半时，
    任何一处漏掉的字段都会长驱直入写成一行"看起来合法"的投影。
    """


@runtime_checkable
class SessionLineageAdapter(Protocol):
    """一个宿主一个适配器：把宿主日志投影成 `SessionNode`（113 §2.4）。

    `host` 是类属性（`dsh` / `workbuddy`），与 `session_lineage.host` 同值；
    `resolve` 判单个会话（判不到就 `None`，**不许拿 main 冒充**）、`scan` 批量枚举。

    两条纪律写进协议而不是写在注释里：
      · 适配器**只读宿主日志**，不写投影表——写只走 `upsert_node`，一处可审计；
      · 适配器**不许写 `citizens` / `session_tokens`**：那是 W4 的事，投影层不签发凭证。
    """
    host: str

    def resolve(self, session_id: str, *, hint: dict | None = None) -> SessionNode | None: ...

    def scan(self, *, since: str | None = None,
             limit: int | None = None) -> Iterable[SessionNode]: ...


def identity_key_of(session_key: str) -> str:
    """`session_key` → 身份键：**全仓唯一**的 `#role` 剥离点（113 §1-5 / §2.3 取舍③）。

    为什么必须收敛成一个函数：`issue_agent_token` 签的子键是 `<父会话>#<role>`
    （`fish.py` 现读 4477；W4 **不执行** 110-⑥ 的去尾），而 `session_identity` 的键
    **允许带 `#role`**。两处各写一遍 `split("#")[0]`，第三处必然忘——忘掉的后果是
    "子代理查不到父身份"，表现是**静默新建公民**（J4 会红，但已经晚了）。
    """
    return str(session_key or "").split("#", 1)[0]


# ---------------------------------------------------------------- 判父（W4 热路径的唯一入口）
#
# 2026-11-25（113 W4 · §5.7）：子会话的 SessionStart payload **没有父**（110 §1.1 v3），
# 唯一能判父的是会话头。所以 `ensure` 的继承必须走这里：
#
#   投影表有行 → 直接用（W2/W6 已经算过，零 IO）
#   投影表没行 → 现读宿主会话头（**只读**：热路径不写投影，写只走 `upsert_node`）
#
# 为什么先查投影表：`session ensure` 每次 SessionStart 都跑，而一个 4 层链就是 4 次
# 向上查询；投影表的读是索引命中，读头是解压 + JSON 解析。但"投影表没填"（W6 回填
# 之前是常态）不能让继承失效——那正是 W4 必须在 W2 之后、却不能依赖 W6 的原因。

_PARENT_OF_SQL = ("SELECT parent_session, relation FROM session_lineage "
                  "WHERE host=? AND session_id=? LIMIT 1")


def parent_of(conn, host: str, session_id: str):
    """会话 → 裸父 id（判不到 → `None`，**不许兜成 main**）。

    三种"判不到"都返回 `None`：会话不存在、宿主头读不出来、宿主没有适配器。

    **W6 起第四种**：这一行是 `relation='cycle'` 的环成员 → 返回 `None`（113 §10.2
    J-D3 "不参与继承/推送"）。为什么必须在这一处堵：`parent_of` 是继承
    （`_nearest_identity_ancestor`）、推送目标（`push_target_of`）与 WB hook 反解父
    三条路的**同一个入口**；只在某一条路上跳过环，另外两条仍会顺着环往下走，
    而 `ancestor_sessions` 的 `seen` 只能保证"不无限循环"，不能保证"不从环里继承"。

    返回值不做任何前缀归一（与 `session_lineage.parent_session` 同口径）。
    """
    sid = str(session_id or "")
    if not host or not sid:
        return None
    row = None
    try:
        row = conn.execute(_PARENT_OF_SQL, (host, sid)).fetchone()
    except sqlite3.Error:          # 老库还没建投影表：当成"投影没填"，去读头
        row = None
    if row is not None:
        if row[1] == "cycle":      # 环成员：对上层表现为"没有父"（不参与继承/推送）
            return None
        return row[0] or None
    try:
        node = get_adapter(host).resolve(sid)
    except (KeyError, RuntimeError, OSError):
        # KeyError = 宿主没登记；RuntimeError = HeaderError（头坏了/缺依赖）；
        # OSError = 目录不可读。三者都只能得出"判不到"，不是"它是根"。
        return None
    return (node or {}).get("parent_session") or None


def ancestor_sessions(conn, host: str, session_id: str, *, max_hops: int = 64) -> list:
    """沿 `parent_session` 向上，返回祖先 id 列表（**近 → 远**，不含自己）。

    停在三处：没有父 / 成环（`instr` 的等价物：`seen` 集合）/ 超过 `max_hops`。
    "不设深度上限"指的是**扫描不给 max_depth**（110 §3.6.1）；这里给跳数上限是为了
    防环与防病态数据把一次会话启动拖死，而不是对树深做业务限制——64 层远超任何真实链。
    """
    seen = {str(session_id or "")}
    out: list[str] = []
    cur = parent_of(conn, host, session_id)
    hops = 0
    while cur and hops < max_hops:
        if cur in seen:
            break                      # 环：到此为止（U 形回指不许无限展开）
        out.append(cur)
        seen.add(cur)
        cur = parent_of(conn, host, cur)
        hops += 1
    return out


def _node_params(node: SessionNode | dict) -> dict:
    """把节点整理成 SQL 参数：校验 → 补 DEFAULT → `extra` 规范化成 JSON 文本。"""
    row = dict(node)
    unknown = sorted(set(row) - set(NODE_FIELDS))
    if unknown:
        raise ValueError(
            f"upsert_node: 未知字段 {unknown}——`session_lineage` 只有 NODE_FIELDS 这些列。"
            "拼错的键若被静默忽略，写进去的行会缺值而没有任何报错。")
    missing = [f for f in _NODE_REQUIRED if row.get(f) in (None, "")]
    if missing:
        raise ValueError(
            f"upsert_node: 缺必填字段 {missing}——这几列 NOT NULL 且无 DEFAULT，"
            "编一个值等于伪造投影（`origin` 编成 main 还会撞 CHECK）。")
    params: dict[str, Any] = {}
    for field in NODE_FIELDS:
        value = row.get(field, _NODE_DEFAULTS.get(field))
        if value is None and field in _NODE_DEFAULTS:
            value = _NODE_DEFAULTS[field]
        if field == "extra" and value is not None and not isinstance(value, str):
            # `extra` 在库里是 TEXT：传 dict/list 时统一成规范 JSON（键序固定，便于对拍）
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        params[field] = value
    return params


def upsert_node(conn: sqlite3.Connection, node: SessionNode | dict) -> None:
    """写一个节点（幂等 UPSERT，冲突键 `(host, session_id)`）：**全仓唯一的投影写口**。

    冲突时**整行覆盖** `NODE_FIELDS` 全部字段，不做逐字段合并。理由：投影表是"可整体
    重建"的派生表（113 §2.3），后到的观测就是当前事实；逐字段合并会让"这一轮没给的值"
    悄悄保留上一轮的旧值，两轮之间就再也分不清哪一个是真的事实——而两种写法的**表内容
    长得一样**，坏数据查不出来。

    事务归调用方：本函数只 `execute`，不 `commit`。W2/W3 的批量投影要一次提交
    （半批落盘 = 树有洞），W6 回填还要包快照，提交点不能藏在这里。

    省略 `lineage_path`/`depth` 时取表的 DEFAULT（`''` / `0`）；`extra` 传 dict/list
    自动转 JSON 文本。
    """
    params = _node_params(node)
    columns = ", ".join(NODE_FIELDS)
    marks = ", ".join(f":{f}" for f in NODE_FIELDS)
    # 主键两列不进 SET：冲突时它们必然相等，写进去只是噪声
    updates = ", ".join(f"{f} = excluded.{f}" for f in NODE_FIELDS
                        if f not in ("host", "session_id"))
    conn.execute(
        f"INSERT INTO session_lineage ({columns}) VALUES ({marks}) "
        f"ON CONFLICT(host, session_id) DO UPDATE SET {updates}",
        params)


#: 深度重算的递归 CTE（113 §3.3 / 110 §3.6.1）：**从每个会话往上走**父链。
#:
#: 为什么不是 110 那段"从根往下走"的写法：往下走时**环上的行永远不会被走到**（一个会话
#: 有父就不是根，而环上没有根），于是那段 SQL 里的 `instr` 守卫是**死代码**——删掉它
#: 读数一个字都不变，"关掉守卫必红"的变异因此永远不成立。改成往上走之后守卫才是承重墙：
#: A→B→A 的第二步就会被 `instr` 挡住（实测：带守卫 4 行收工，去掉守卫 0.01 s 内被
#: SQLite 的进度回调掐断，否则是无限递归——110 §7-14 说的"可用性事故"）。
#:
#: **fork 边不是嵌套边**（110 §1.1 / §2.5：`parentSession` 在 DSH 里一列两义，"用父的历史
#: 播种"不是"被 spawn"）。所以走到 `relation='fork'` 的那一行就**停**：fork 自己是一个新的根
#: （宿主给的 `delegationDepth` 也恒为 0），它的子会话从 1 起算。不停的话本机 777 行里会有
#: **92 行**凭空多一层、全部标成 conflict（2026-11-25 实测），`basis='conflict'` 立刻从
#: "异常告警"退化成"常态噪音"。
#:
#: 路径口径：每步把**新的祖先**拼到前面（`'/B' || '/C/'` = `/B/C/`），所以最深一行的
#: `path` 正好是"根 → … → 自己"；写库前去掉尾斜杠（根写 `''`，113 §2.3 的列注释）。
#:
#: 三个退化形状都靠"最深那一行的 `cur`"认（停止点的三种理由，缺一种就会误报）：
#:   · `cur` 已不在表里 → **孤儿**（父指针指向不存在的会话，110 §3.6.3）；
#:   · `cur` 是 `relation='fork'` 的行 → **正常停止**（seed 边，见上）；
#:   · `cur` 有父且不是 fork → 被 `instr` 守卫挡住 ⇒ **环**（W6 才给它标
#:     `relation='cycle'`，本波只**列名报告**、**不写深度**——环上的行没有可用的深度）。
_WALK_SQL = """
WITH RECURSIVE up(sid, cur, d, path) AS (
  SELECT session_id, session_id, 0, '/' || session_id || '/'
    FROM session_lineage
   WHERE host = :host
  UNION ALL
  SELECT u.sid, p.parent_session, u.d + 1, '/' || p.parent_session || u.path
    FROM up u
    JOIN session_lineage p ON p.host = :host AND p.session_id = u.cur
   WHERE p.parent_session IS NOT NULL
     AND COALESCE(p.relation, '') <> 'fork'
     AND instr(u.path, '/' || p.parent_session || '/') = 0
)
SELECT sid, cur, d, path FROM up
"""


def _load_extra(text) -> dict:
    """`extra` 的 JSON → dict；坏值不丢（塞进 `extra_unparsable`，别让一次解析失败吞掉现场）。"""
    if not text:
        return {}
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {"extra_unparsable": str(text)[:500]}
    return value if isinstance(value, dict) else {"extra_value": value}


#: 读投影行的**唯一列序**：`recompute_depth` / `detect_cycles` 共用（下标写死在这里）。
#: 0=session_id 1=parent_session 2=relation 3=depth 4=lineage_path 5=depth_host
#: 6=basis 7=extra —— 两处各写一份 SELECT 时，加列迟早只改一处，另一处静默错位。
_NODE_COLS = ("session_id, parent_session, relation, depth, lineage_path, depth_host, "
              "basis, extra")


def read_rows(conn: sqlite3.Connection, host: str) -> dict:
    """`session_id → 行元组`（列序见 `_NODE_COLS`）。表不存在时**不吞**：调用方自己判。"""
    return {r[0]: r for r in conn.execute(
        f"SELECT {_NODE_COLS} FROM session_lineage WHERE host = ?", (host,))}


#: `_stop_kind` 的四个取值：`root`（走到没父）/ `seed`（fork 种子边，正常停止）/
#: `orphan`（父指针指向不存在的会话）/ `cycle`（被 `instr` 守卫挡住 = 环）。
_STOP_KINDS = ("root", "seed", "orphan", "cycle")


def _stop_kind(rows: dict, cur: str) -> str:
    """walk 走到 `cur` 时**为什么停**（113 §3.3 末尾列的三种退化形状 + 正常两种）。

    抽成一个函数的理由不是好看：`recompute_depth` 用"停在哪"决定写不写深度，
    `detect_cycles` 用"停在哪"把行分进 cycle / orphan 两份清单。两处各写一遍判断，
    改一处忘一处时，深度和清单会给出**互相矛盾**的结论，而两边单看都"有数据"。
    """
    row = rows.get(cur)
    if row is None:
        return "orphan"
    if row[1] is None:
        return "root"
    if row[2] == "fork":
        return "seed"
    return "cycle"


def _lineage_path(depth: int, path: str, session_id: str) -> str:
    """CTE 的 `/A/B/`（**含自己**，守卫要靠它认环）→ 列注释要的**祖先链** `/A`。

    为什么去掉自己：`lineage_path` 的语义是"从根到这里的祖先"（根 = `''`，113 §2.3 的列
    注释），自己不算祖先；留着的话每个节点的路径都多一节，J-D1 的"逐层累加"就整体错一位。
    """
    if depth <= 0 or not path:
        return ""
    full = path[:-1] if path.endswith("/") else path      # '/A/B'
    suffix = "/" + str(session_id)
    return full[:-len(suffix)] if full.endswith(suffix) else full


def recompute_depth(conn: sqlite3.Connection, host: str) -> dict:
    """**W2 落地**（113 §3.3）：递归 CTE 重算 `depth` / `lineage_path`，宿主值只做对拍。

    三件事，缺一件这条函数就等于没做：

    1. **以链为准**：`depth` 是"从自己往上数到根的步数"，不是宿主的 `delegationDepth`。
       J-D1 的变异（把四条会话头的 `delegationDepth` 全写 0）就是靠这一条才会红——
       省掉它，W6 的回填会把错的深度灌进 1,522 行历史，而"错的"与"对的"在库里长得一样。
    2. **对拍留痕**：宿主值仍在 `depth_host`（适配器写的），两值不等 → `basis='conflict'`；
       原 `basis` 存进 `extra.conflict_from`，所以这不是单向标记——下一轮若两值又对上了，
       `basis` 会被**还原**（不清账的标记等于把一次性冲突变成永久噪音）。
    3. **例外诚实**：环与孤儿逐条列名返回，**环上的行不写深度**（没有可用的深度可写）。

    事务归调用方（与 `upsert_node` 同一纪律：一批投影一次提交，提交点不藏在这里）。
    返回 `{"host","rows","updated","conflicts","conflict_ids","cycles","orphans",
    "max_depth","walk_rows","observed_at"}`——全是**真统计**，不是恒 0：

      · `updated` 本轮真的改了几行（第二次跑必然 0，这就是幂等）；
      · `conflicts` 当前"重算 ≠ 宿主"的行数（稳定值，不是"本轮新标记几行"）；
      · `cycles` / `orphans` 两个清单即使为空也要**存在**（"没检测"与"检测到 0"必须可分，
        113 §10.2 J-W6-C 的可观测性要求）。
    """
    rows = read_rows(conn, host)
    observed_at = fish_common.now_utc()
    if not rows:
        return {"host": host, "rows": 0, "updated": 0, "conflicts": 0, "conflict_ids": [],
                "cycles": [], "orphans": [], "max_depth": 0, "walk_rows": 0,
                "observed_at": observed_at}

    walk = list(conn.execute(_WALK_SQL, {"host": host}))
    # SQLite 不允许在一条语句里两次引用递归 CTE（"multiple references to recursive
    # table"），所以"每个会话的最深一行"在 Python 里归并——行数量级是 Σ(深度+1)，
    # 实测全量 DSH 也就一千多行，比拆成两条 SQL 换着法绕更省事也更好读。
    best: dict[str, tuple] = {}
    for sid, cur, depth, path in walk:
        if sid not in best or depth > best[sid][0]:
            best[sid] = (depth, path, cur)

    writes, conflict_ids, cycles, orphans = [], [], [], []
    max_depth = 0
    for sid in sorted(best):
        depth, path, cur = best[sid]
        (_parent, relation, old_depth, old_path, depth_host, basis,
         old_extra) = rows[sid][1:]
        kind = _stop_kind(rows, cur)
        if kind == "orphan":
            orphans.append(sid)              # 父指针指向一个不存在的会话（孤儿，不是根）
        elif kind == "cycle":
            # 停止点有父且不是 fork ⇒ 被 instr 守卫挡住 = 环。环上没有可用深度（W6 才给它标
            # `relation='cycle'`，本函数只**列名报告**、**不写深度**、也不碰 basis）
            cycles.append(sid)
            continue
        lineage_path = _lineage_path(depth, path, sid)
        max_depth = max(max_depth, depth)

        extra = _load_extra(old_extra)
        conflict = depth_host is not None and depth_host != depth
        if conflict:
            conflict_ids.append(sid)
        changed = (depth != old_depth) or (lineage_path != old_path)
        if conflict:
            if basis != "conflict":
                extra = dict(extra, conflict_from=basis)
                basis = "conflict"
                changed = True
        elif basis == "conflict" and extra.get("conflict_from"):
            basis = extra.pop("conflict_from")
            changed = True
        if not changed:
            continue
        writes.append({"host": host, "session_id": sid, "depth": depth,
                       "lineage_path": lineage_path, "basis": basis,
                       "extra": json.dumps(extra, ensure_ascii=False, sort_keys=True)
                       if extra else None})

    if writes:
        conn.executemany(
            "UPDATE session_lineage SET depth = :depth, lineage_path = :lineage_path, "
            "       basis = :basis, extra = :extra "
            " WHERE host = :host AND session_id = :session_id", writes)
    return {"host": host, "rows": len(rows), "updated": len(writes),
            "conflicts": len(conflict_ids), "conflict_ids": conflict_ids,
            "cycles": cycles, "orphans": orphans, "max_depth": max_depth,
            "walk_rows": len(walk), "observed_at": observed_at}


# ================================================================ W6 · 检测（三份清单）
#
# 113 §10.2 的三份清单（J-W6-C）：cycle / orphan / conflict。**清单本身就是判据**——
# 所以三件事必须同时成立：① 三份清单永远存在（空也是空列表）；② 交给 `format_report`
# 时**空清单也要打印"本次检测到 0 行"**（否则"没打印"与"没检测"不可分）；③ 环上的行
# 真的被标成 `relation='cycle'` 且**不参与继承/推送**（`parent_of` 在环上返回 None）。

#: 环标记的唯一写入点：`relation='cycle'` + `basis='conflict'`，原值存进 `extra`（可逆）。
_MARK_CYCLE_SQL = ("UPDATE session_lineage SET relation='cycle', basis='conflict', extra=:extra "
                   "WHERE host=:host AND session_id=:sid")

#: `extra` 里存"标记前原值"的两个键（与 `basis='conflict'` 的 `conflict_from` 同一条纪律）。
_CYCLE_FROM_KEYS = ("cycle_relation_from", "cycle_basis_from")


def _dump_extra(extra: dict) -> str | None:
    """`extra` dict → 列的 JSON 文本（空 dict 写 NULL：空 `{}` 与"没额外证据"不是一回事）。"""
    if not extra:
        return None
    return json.dumps(extra, ensure_ascii=False, sort_keys=True)


def _mark_cycles(conn: sqlite3.Connection, host: str, cycle_ids: list,
                 rows: dict) -> dict:
    """把环成员标成 `relation='cycle'` / `basis='conflict'`，并把**原值**存进 `extra`。

    两个方向都要写，缺一半就是"一去不回"的标记：

      · 本轮**在环上**且还没标过 → 标，并把原 `relation`/`basis` 存成
        `extra.cycle_relation_from` / `cycle_basis_from`（下一轮还原要用）；
      · 上一轮标过、本轮**已不在环上**（数据被修正）→ 还原并清掉那两个键。

    为什么必须可逆：标记一旦永久，环被拆开之后这些行会永远停在 `cycle/conflict`——
    "一次性异常"退化成"永久噪音"，而 `basis='conflict'` 的告警价值就被稀释掉了
    （与 113 §3.3 取舍② 同一条纪律）。

    幂等：已在 `cycle/conflict` 的行不重复写（第二次跑 `marked=[]`）。
    """
    marked, restored = [], []
    wanted = set(cycle_ids)
    for sid, row in rows.items():
        relation, basis, extra_text = row[2], row[6], row[7]
        extra = _load_extra(extra_text)
        if sid in wanted:
            if relation == "cycle" and basis == "conflict":
                continue                                     # 已标记：幂等
            extra = dict(extra)
            extra.setdefault(_CYCLE_FROM_KEYS[0], relation)
            extra.setdefault(_CYCLE_FROM_KEYS[1], basis)
            conn.execute(_MARK_CYCLE_SQL, {"host": host, "sid": sid,
                                           "extra": _dump_extra(extra)})
            marked.append(sid)
            continue
        if relation == "cycle" or extra.get(_CYCLE_FROM_KEYS[0]) is not None:
            extra = dict(extra)
            relation_from = extra.pop(_CYCLE_FROM_KEYS[0], None)
            basis_from = extra.pop(_CYCLE_FROM_KEYS[1], None)
            sets = ["relation = :relation"]
            params = {"host": host, "sid": sid, "relation": relation_from,
                      "extra": _dump_extra(extra)}
            if basis_from is not None:
                sets.append("basis = :basis")
                params["basis"] = basis_from
            sets.append("extra = :extra")
            conn.execute("UPDATE session_lineage SET " + ", ".join(sets)
                         + " WHERE host=:host AND session_id=:sid", params)
            restored.append(sid)
    return {"marked": marked, "restored": restored}


def detect_cycles(conn: sqlite3.Connection, host: str, *, apply: bool = True) -> dict:
    """**W6 落地**（113 §10.1 步 4 / J-D3）：walk 求反 → cycle / orphan / conflict 三份清单。

    与 `recompute_depth` 共用同一条 walk（`_WALK_SQL`）与同一个停止点判断
    （`_stop_kind`）——两处各判一遍时，"深度说没事、清单说有环"这种自相矛盾的结论
    会同时出现在一张报告里，而单看哪一半都"有数据"。

    分类（按停止点，四类只取三类进清单）：

      · 停止点是 `cur not in rows` → **orphan**（父指针指向不存在的会话）。宿主写了
        这条边，缺的是父那一行；它**不是 main**，W2 的适配器已把它落成
        `origin='unknown'` / `basis='missing'`（J-D4 的数据面）；
      · 停止点有父且不是 fork → **cycle**（`instr` 守卫挡住）。环上的行没有可用深度，
        所以它同时进 conflict 的兄弟清单，但**不写深度**；
      · 停止点不在上面两类里，而 `depth_host` 与重算深度不等 → **conflict**。

    `apply=True`（缺省）时写环标记（`relation='cycle'` / `basis='conflict'`，见
    `_mark_cycles`）；`apply=False` 时**一个字节都不写**——`fish lineage report` 与
    对账工具都走这条路（只读连接上也能安全调用）。

    返回 `{"host","rows","walk_rows","apply","cycles","orphans","conflicts","marked",
    "restored","max_depth","observed_at"}`。**四份清单即使为空也存在**——这是 J-W6-C
    的可观测性要求："没检测"与"检测到 0 行"必须可分。
    """
    observed_at = fish_common.now_utc()
    empty = {"host": host, "rows": 0, "walk_rows": 0, "apply": bool(apply),
             "cycles": [], "orphans": [], "conflicts": [], "marked": [], "restored": [],
             "max_depth": 0, "observed_at": observed_at}
    try:
        rows = read_rows(conn, host)
    except sqlite3.Error:
        return empty                        # 老库 / 只读介质：没有投影表 → 检测到 0 行
    if not rows:
        return empty

    walk = list(conn.execute(_WALK_SQL, {"host": host}))
    best: dict[str, tuple] = {}
    for sid, cur, depth, path in walk:
        if sid not in best or depth > best[sid][0]:
            best[sid] = (depth, path, cur)

    cycles, orphans, conflicts = [], [], []
    max_depth = 0
    for sid in sorted(best):
        depth, _path, cur = best[sid]
        kind = _stop_kind(rows, cur)
        if kind == "cycle":
            cycles.append(sid)
            continue                        # 环上没有可用深度（113 §3.3 末）
        if kind == "orphan":
            orphans.append(sid)
        max_depth = max(max_depth, depth)
        depth_host = rows[sid][5]
        if depth_host is not None and depth_host != depth:
            conflicts.append(sid)

    marks = ({"marked": [], "restored": []} if not apply
             else _mark_cycles(conn, host, cycles, rows))
    return {"host": host, "rows": len(rows), "walk_rows": len(walk),
            "apply": bool(apply), "cycles": cycles, "orphans": orphans,
            "conflicts": conflicts, "marked": marks["marked"],
            "restored": marks["restored"], "max_depth": max_depth,
            "observed_at": observed_at}


class _LazyAdapter:
    """按需加载宿主适配器：导入 `fish_lineage` 时**不**把 `fish_corpus_*`/`fish` 拖进来。

    为什么不在模块顶层 import 叶子（113 §2.4 的分层护栏）：
    `fish_lineage_dsh`/`fish_lineage_wb` 会 import `fish_corpus_*`，后者最终 import `fish`；
    而 `fish_lineage` 可能被 `fish_db` 或 hook 路径引用。顶层导入 = 把整条业务链拖进 hook。
    """

    def __init__(self, host: str, module: str, class_name: str) -> None:
        self.host = host
        self._module = module
        self._class_name = class_name
        self._impl = None

    def _get(self):
        if self._impl is None:
            import importlib
            module = importlib.import_module(self._module)
            self._impl = getattr(module, self._class_name)()
        return self._impl

    def resolve(self, session_id: str, *, hint: dict | None = None) -> "SessionNode | None":
        return self._get().resolve(session_id, hint=hint)

    def scan(self, *, since: str | None = None,
             limit: int | None = None) -> Iterable[SessionNode]:
        return self._get().scan(since=since, limit=limit)

    @property
    def last_report(self):
        """最近一次 `scan` 的对账面（DSH 适配器有、WB 没有 → `None`）。

        为什么不复制一份到包装层：对账面是**适配器实例**的状态，复制就会有两份，
        而 `scan` 每次都会刷新——两份一旦不一致，报告里就会出现"文件数与失败清单对不上"。
        """
        try:
            return getattr(self._get(), "last_report", None)
        except (ImportError, AttributeError):     # 还没加载 / 该适配器没有对账面
            return None

    def __repr__(self) -> str:      # pragma: no cover
        return f"<LazyLineageAdapter {self.host}>"


#: 宿主 → 适配器（**懒加载**：首次 `resolve`/`scan` 才 import 叶子模块）。
ADAPTERS: dict[str, SessionLineageAdapter] = {
    "dsh": _LazyAdapter("dsh", "fish_lineage_dsh", "DshLineageAdapter"),
    "workbuddy": _LazyAdapter("workbuddy", "fish_lineage_wb", "WbLineageAdapter"),
}


def get_adapter(host: str) -> SessionLineageAdapter:
    """按宿主取适配器；未登记 → `KeyError`（**不猜、不回退到 dsh**）。"""
    try:
        return ADAPTERS[host]
    except KeyError:
        raise KeyError(
            f"没有 {host!r} 的会话树适配器；已登记：{sorted(ADAPTERS)}") from None
