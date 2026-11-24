#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话树投影层：节点模型 + 唯一的写原语 + 防环（113 §2.4 骨架，2026-11-25 · W1）。

## 这一波（W1）为什么几乎什么都不"做"

W1 只搭骨架：**一行数据都不写、一个既有查询都不改**（113 §2.1）。所以本模块里
只有 `upsert_node` 是真实现——它是全仓**唯一**的 `session_lineage` 写口，W2/W3/W6
都只经由它写投影；其余四项（`recompute_depth` / `detect_cycles` / 两个宿主适配器）
是**空壳**，调用即 `NotImplementedError`。

为什么空壳要"响"而不是返回空结果：返回 `{}` 或 `[]` 会让调用方以为"算过了、没有
异常"，而 W2/W6 的判据（J-D1 深度重算、J-D3 环）正是靠"算出来的数不对"才会红。
**空壳返回假值 = 把判据变成恒绿**。

## 依赖方向：只许 stdlib

本模块**只 import 标准库**（`json` / `sqlite3` / `typing`）。理由不是洁癖：
W2/W3 的宿主适配器（`fish_lineage_dsh` / `fish_lineage_wb`）会 import
`fish_corpus_*`，而那些模块最终 import `fish`；只要本模块不反向依赖任何 fish 模块，
`fish_db`/`fish_schema` 将来要引用它也不会成环（113 §2.5 的叶子模块纪律，
先例：`fish_git_schema` 同样是"只含字符串常量、谁都能安全依赖"的一层）。

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


def recompute_depth(conn: sqlite3.Connection, host: str) -> dict:
    """**W2 落地**（113 §3.3）：递归 CTE 重算 `depth`/`lineage_path`，宿主值落 `depth_host`。

    W1 只留签名。这里刻意**不**返回 `{"updated": 0, "conflicts": 0}`：那与"重算过、
    恰好 0 变动"无法区分，而 J-D1（四层链要算出 0/1/2/3）正是靠这个区分才会红。
    """
    raise NotImplementedError(
        "recompute_depth 在 W2 落地（113 §3.3）：W1 只有空壳，一行都不写")


def detect_cycles(conn: sqlite3.Connection, host: str) -> dict:
    """**W6 落地**（113 §10.1 步 4 / J-D3）：walk 求反 → `cycle` / `orphan` 两份清单。

    W1 明说不引入环风险（113 §2.8）：本波不写行，也就没有环可检。
    """
    raise NotImplementedError(
        "detect_cycles 在 W6 落地（113 §10.1）：W1 只有空壳，一行都不写")


class _AdapterShell:
    """宿主适配器空壳的公共部分：**所有方法都响亮失败**，不返回假结果。

    W2/W3 用真实现替换时，按 113 §2.5 拆成叶子模块（`fish_lineage_dsh` /
    `fish_lineage_wb`），本模块只留注册表与公共算法——两波同时改一个文件必然打架。
    """
    host = ""

    def resolve(self, session_id: str, *, hint: dict | None = None) -> SessionNode | None:
        raise NotImplementedError(
            f"{self.host or '未命名'}适配器的 resolve 在 W2/W3 落地（113 §3/§4）："
            "骨架返回 None 会被上层当成'这个会话不存在'，比报错危险得多")

    def scan(self, *, since: str | None = None,
             limit: int | None = None) -> Iterable[SessionNode]:
        raise NotImplementedError(
            f"{self.host or '未命名'}适配器的 scan 在 W2/W3 落地（113 §3/§4）："
            "空列表会被当成'扫描成功、宿主没有会话'")


class DshLineageAdapter(_AdapterShell):
    """DSH 适配器空壳 → W2 填 `fish_lineage_dsh.py`（读会话头，classify 六条判据）。

    DSH 是**唯一能判父**的宿主（`SessionStart`/`PostToolUse` payload 里没有 `agent_id`，
    110 §1.1 v3），所以判父只能读 `session.v4.jsonl.zstd` 的首帧。
    """
    host = "dsh"


class WbLineageAdapter(_AdapterShell):
    """WorkBuddy 适配器空壳 → W3 填 `fish_lineage_wb.py`（runtime → path → missing 四级）。

    注意 WB 的 `session_id` 只能取文件名（transcript 首行的 `sessionId` 不是
    `agent-<uuid>`，113 §4.5），所以本适配器的 `resolve` 在 W3 也必须按路径取 id。
    """
    host = "workbuddy"


#: 宿主 → 适配器。W2/W3 把这两个空壳换成叶子模块里的真实现（注册表本身不用改）。
ADAPTERS: dict[str, SessionLineageAdapter] = {
    "dsh": DshLineageAdapter(),
    "workbuddy": WbLineageAdapter(),
}
