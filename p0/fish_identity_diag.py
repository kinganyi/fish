#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""身份诊断：`whoami all=true` 的取数与渲染（2026-11-21 从 `fish.py` 搬出）。

## 为什么独立成一个模块

`fish.py` 是 10600 行的满窗口（J4 只许降不许抬），而这两个函数约 110 行。
更重要的是：**它是"诊断"而不是"身份系统本身"**——它不发凭证、不裁决归属，
只回答"库里还有谁跟我像"。这类只读诊断本该有自己的位置。

搬出后 `fish.py` 只剩一个 3 行转发，净腾出约 110 行。

## 依赖方向

**零内部依赖**（只用 stdlib）。原来 `related_citizens_lines` 要 `load_citizen`，
搬出后改成**由调用方传 citizen dict**——那本来就是调用方（`fish.py`）手上现成的东西，
没必要为了拿它再制造一条 `identity_diag → fish` 的上行依赖。

## 四条锚（2026-11-21 新增第四条）

| 锚 | 列/来源 | 它治什么 |
|---|---|---|
| `session` | `citizens.host_session_key` | 同一个宿主会话建出了两个公民 |
| `name` | `citizens.display_name` | 同名（决策 14：同名就是两个公民，不合并） |
| `account` | `citizens.account_id` | 同一账号在不同宿主/设备上的身份 |
| `former_name` | `citizen.renamed` 事件的 `from` | **改过名的身份**（见下） |

### 为什么需要第四条

外部 AI 反馈报告的实测：

> `whoami(all=true)` 的三键匹配不到「**换会话键 + 换名字**」的重复身份。

而那恰恰是最常见的形态：**自动派生的名字 vs 人给的名字**。
一次 `rename_citizen` 之后，前三条锚可能一条都不剩。

**但旧名字是可查的**——`rename_citizen` 把 `{"from","to","actor","reason"}` 写进了
`citizen.renamed` 事件（生产库有 21 条）。第四条锚就是把它用起来。

⚠️ `citizen_id` 在 `subject` 里（`{"kind":"citizen","id":...}`），**不在 payload 里**
——用错字段会永远查不到，而且不报错。
"""
import sqlite3

#: 锚 → `citizens` 列。白名单而不是让调用方传列名：SQL 里的列名只能来自
#: 实现内部的常量表，传进来的永远是**值**（否则这个函数就是一个 WHERE 注入口）。
RELATED_ANCHORS = (("session", "host_session_key"),
                   ("name", "display_name"),
                   ("account", "account_id"))

#: 相关公民清单的硬上限（`whoami all=true`）。20 足够覆盖"同一 AI 的两三套身份"，
#: 又不至于让一次诊断变成全库扫描——这个清单的用途是**对人**，不是导出。
RELATED_CITIZENS_LIMIT = 20

_COLUMNS = ("citizen_id", "display_name", "kind", "status", "host",
            "last_seen_at", "created_at")


def _former_names(conn, citizen_id):
    """该公民的**曾用名**（从 `citizen.renamed` 事件反查）。

    `citizen_id` 在 `subject` 里、`from` 在 `payload` 里——两处都要对。
    事件坏了/表缺了返回空列表：**"查不到曾用名"不是致命错误**，
    它只是少一条关联锚。
    """
    if not citizen_id:
        return []
    try:
        rows = conn.execute(
            "SELECT json_extract(payload, '$.from') FROM events "
            "WHERE type='citizen.renamed' AND json_extract(subject, '$.id')=? "
            "ORDER BY seq DESC", (citizen_id,)).fetchall()
    except sqlite3.Error:
        return []
    return [r[0] for r in rows if r and r[0]]


def related_citizens(conn, *, display_name=None, host_session_key=None,
                     account_id=None, exclude_citizen_id=None,
                     limit=RELATED_CITIZENS_LIMIT) -> list:
    """与本会话上下文相关的公民（`whoami all=true` 的唯一取数口，FB-03）。

    四条锚**任一命中**即算相关，按 citizen_id 去重后回一行一条。

    这正是"身份双视图"的取证面：`register` 说 A、带 token 的 `whoami` 说 B，
    两个公民都活着、都不报错。系统**不合并**（那是人的决定），但必须能一眼看见
    "原来有两条身份"以及它们各自长什么样、手上还有几条有效凭证。

    几条实现纪律：
      · **不是全库 dump**：一个锚都没有就直接空转返回（`[]`），绝不"没锚就列全部"；
        有锚也受 `limit` 截断，调用方负责说明"被截断了"。
      · `status` 不过滤：退役的公民也要在清单里被看见（列上有 status 一栏）；
        但排序把 `active` 放前面——"谁现在还在用"才是诊断的第一问题。
      · `token_count` 只数**未吊销**的凭证：一条已吊销的 token 说明不了"它还活着"。
      · 读的是热表 `citizens`（归档公民不参与"当前的上下文"，它们已经搬去冷表）。
    """
    values = {"session": host_session_key, "name": display_name,
              "account": account_id}
    found = {}

    def _collect(rows, relation):
        for row in rows:
            item = found.get(row[0])
            if item is None:
                item = dict(zip(_COLUMNS, row))
                item["relations"] = []
                item["token_count"] = 0
                found[row[0]] = item
            if relation not in item["relations"]:
                item["relations"].append(relation)

    order_sql = ("ORDER BY CASE WHEN status='active' THEN 0 ELSE 1 END, "
                 "created_at, citizen_id")
    for relation, column in RELATED_ANCHORS:
        anchor = values[relation]
        anchor = anchor.strip() if isinstance(anchor, str) else anchor
        if not anchor:
            continue
        _collect(conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM citizens WHERE {column}=? " + order_sql,
            (anchor,)).fetchall(), relation)

    # 第四锚（2026-11-21）：曾用名。只在有 exclude_citizen_id（= 我自己）时有意义——
    # 没有"我"就没有"我曾用过的名字"。
    former = _former_names(conn, exclude_citizen_id)
    if former:
        ph = ",".join("?" * len(former))
        _collect(conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM citizens WHERE display_name IN ({ph}) "
            + order_sql, former).fetchall(), "former_name")

    if exclude_citizen_id:
        found.pop(exclude_citizen_id, None)
    if not found:
        return []
    ids = list(found.keys())
    placeholders = ",".join("?" * len(ids))
    counts = dict(conn.execute(
        "SELECT citizen_id,COUNT(*) FROM session_tokens WHERE revoked_at IS NULL "
        f"AND citizen_id IN ({placeholders}) GROUP BY citizen_id",
        ids).fetchall())
    # 关系名的稳定顺序（session → name → account → former_name），保证输出可判。
    order = ["session", "name", "account", "former_name"]
    items = sorted(
        found.values(),
        key=lambda it: (0 if it["status"] == "active" else 1,
                        min(order.index(r) for r in it["relations"]),
                        it["created_at"] or "", it["citizen_id"]))
    for item in items:
        item["relations"] = [r for r in order if r in item["relations"]]
        item["token_count"] = int(counts.get(item["citizen_id"], 0))
    cut = int(limit or 0)
    return items[:cut] if cut > 0 else []


def related_citizens_lines(conn, citizen=None) -> list:
    """`whoami --all`（CLI）与 `whoami all=true`（MCP）的**唯一渲染**。

    2026-10-26（评审 🟠3）：CLI 原来没有 `--all`，MCP 有——同一句"还有谁和我占着
    同一个上下文"，在一个入口能问、在另一个入口问不到。补 CLI 的那一刻必须
    **把渲染也搬下来**：两个入口各写一段文案，第二次改口径就会漂
    （本轮修的三处漂移里有两处正是这个来源）。

    `citizen` 是 `load_citizen()` 的返回值（**由调用方传**，本模块不依赖 `fish`）。
    为空 = 没有锚（没带有效凭证）：必须说清"不是库里没有人"，否则"没列"会被读成"没有"。
    """
    head = ("相关公民（**不含你自己**；按 会话/同名/账号/曾用名 关联，不是全库清单；"
            "token 数 = 未吊销凭证数，最多 "
            f"{RELATED_CITIZENS_LIMIT} 条）：")
    if not citizen or not citizen.get("citizen_id"):
        return [head,
                "- （未列出）这份清单需要一条**有效 token** 当锚：没凭证就没有"
                "会话键、公民名与账号可关联——这不等于\u201c库里没有人\u201d。"]
    me = citizen["citizen_id"]
    rows = related_citizens(
        conn,
        display_name=citizen.get("display_name"),
        host_session_key=citizen.get("host_session_key"),
        account_id=citizen.get("account_id"),
        exclude_citizen_id=me)
    if not rows:
        return [head, "- （没有匹配到相关公民：本会话、同名、同账号、曾用名都没有别人）"]
    lines = [head]
    for it in rows:
        lines.append(
            f"- {it['citizen_id']}｜{it['display_name'] or '（无名）'}｜"
            f"{it['kind'] or 'unknown'}｜{it['status'] or 'unknown'}｜"
            f"宿主 {it['host'] or '未记录'}｜最近活跃 {it['last_seen_at'] or '未记录'}｜"
            f"token {it['token_count']}｜关联：{'/'.join(it['relations'])}")
    if len(rows) >= RELATED_CITIZENS_LIMIT:
        lines.append(f"（已达上限 {RELATED_CITIZENS_LIMIT} 条，可能还有更多；"
                     "全库清单请用 `fish citizen list`）")
    return lines


def unattributed_sessions(conn, *, limit=12):
    """「有写入、但没有公民归属」的会话清单。`[(session_key, writes, last_seen)]`。

    ## 为什么只报不改

    实测 11,395 条写入没有 `citizen_id`（占全部写的 89.7%），只涉及 12 个会话键。
    它们是**接入侧缺口**——那些会话从没调用过 `fish session start`。

    **fish 不去替它们建公民。** 身份的唯一来源是凭证；从会话键推一段身份，
    正是本模块那句「不知道是谁 ≠ 谁都可以」禁止的事。所以这里只把缺口**说出来**，
    补账走 `fish activity attribute`（要人显式指定）。
    """
    rows = conn.execute(
        "SELECT session_key, COUNT(*), MAX(seen_at) FROM tool_activity "
        "WHERE action='write' AND (citizen_id IS NULL OR citizen_id='') "
        "AND session_key IS NOT NULL AND session_key!='' "
        "GROUP BY session_key ORDER BY 2 DESC LIMIT ?", (int(limit),)).fetchall()
    return [(str(sk), int(n), str(last or "")) for sk, n, last in rows]


def attribute_session(conn, session_key, citizen_id, *, apply=False):
    """把某会话**历史**的无归属写入显式归到某个公民名下。返回 `(matched, changed)`。

    ## 为什么必须是显式动作

    这是"补账"不是"推断"：谁该算谁的，只有人（或主代理）能决定。所以
    `apply=False` 是缺省（干跑），真写要显式 `--apply`。

    ## 三条校验

      1. 会话键必须真的有**无归属**行——否则返回 0，不做无意义的 UPDATE；
      2. `citizen_id` 必须是**已知**公民（含归档）——沿用 `citizen_ids_known` 的口径，
         避免把补账指向一个拼错的 id；
      3. 只改**无归属**行。已有归属的一行都不碰——那可能是别人显式写下的。
    """
    sk = (session_key or "").strip()
    cid = (citizen_id or "").strip()
    if not sk or not cid:
        raise ValueError("session 与 citizen 都必须给")
    matched = int(conn.execute(
        "SELECT COUNT(*) FROM tool_activity WHERE action='write' "
        "AND (citizen_id IS NULL OR citizen_id='') AND session_key=?",
        (sk,)).fetchone()[0] or 0)
    if not matched or not apply:
        return matched, 0
    changed = int(conn.execute(
        "UPDATE tool_activity SET citizen_id=? WHERE action='write' "
        "AND (citizen_id IS NULL OR citizen_id='') AND session_key=?",
        (cid, sk)).rowcount or 0)
    conn.commit()
    return matched, changed


def render_unattributed_line(conn, *, limit=6):
    """healthcheck 用的一行：有写入但无公民的会话。没有缺口返回空串。"""
    rows = unattributed_sessions(conn, limit=limit)
    if not rows:
        return ""
    total = sum(n for _sk, n, _l in rows)
    names = "、".join(f"`{sk}`（{n} 次）" for sk, n, _l in rows[:3])
    more = f" 等 {len(rows)} 个会话" if len(rows) > 3 else ""
    return (f"  ⚠ 归属缺口：{total} 条写入没有公民（{names}{more}）"
            "——它们没调用过 `fish session start`。补账："
            "`fish activity attribute --session <key> --citizen <id> [--apply]`\n")
