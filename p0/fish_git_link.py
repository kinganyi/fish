#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fish 引入 git：记忆↔提交的关联（2026-11-20 · Phase 4/5）。

## 为什么从 `fish_git_store.py` 拆出来

`fish_git_store.py` 一度同时承担五组职责（仓库 / 工作副本 / 观测 / 提交 / 关联），
涨到 376 行、逼近它的 J4 窗口（150–380）。

**抬窗是最差的选项**（那是放松门禁）。正确做法是承认「关联」本来就是一个
独立领域：它管的是「记忆与提交之间有什么关系」，而另外四组管的是「仓库是什么样」。
两者只在 `repo_id` / `sha` 这两个外键上相遇。

分层方向：**link → （无依赖）**。本模块只用 `fish_common` 的时间函数与 SQL，
不 import `fish_git` / `fish_git_store`——它不需要知道仓库是怎么发现的。

## 为什么是独立表，不扩展 `links`

`links` 的两端必须是 `code_index` 里的编码：`gov_apply.py:292` 的健康检查（悬空边
计数）与 `fish_maintenance.py:423` 的 24h 维护都会强制这一点。把 commit SHA 塞进去
会立刻变成悬空边、24 小时后被 `suppressed=1` 压掉——那条链路**本来是给
code↔code 用的**，硬塞第三种两端会破坏它的不变量。

## 两个刻意的设计

**① 软删除（`removed_at`），不是物理删除。**
J10 的判据就在这一条：物理删除之后「撤销」无从谈起——追溯不到原来关联过什么。
撤销动作本身也是信息（「这条关联曾经建立又被否掉」和「从没建立过」不是一回事）。

**② 重复建立返回已有 id，不报错。**
调用方（hook / 手工工具）不需要「先查再建」——那会在并发下变成两行。
"""
import fish_common


def link_create(conn, *, src_kind, src_id, dst_kind, dst_id, rel,
                source="explicit", confidence=1.0, evidence=None,
                content_hash=None, now=None):
    """建立一条记忆↔提交（或 ↔仓库/工作副本）的关联，返回 `git_link.id`。

    重复建立同一条关联（同一 src/dst/rel）返回已有的 id，不报错。
    若那条曾**被软删**，这次建立视为「恢复」（清掉 `removed_at`）——
    因为「再建立一次」的语义就是「它又成立了」。
    """
    stamp = now or fish_common.now_utc()
    row = conn.execute(
        "SELECT id, removed_at FROM git_link WHERE src_kind=? AND src_id=? AND "
        "dst_kind=? AND dst_id=? AND rel=?",
        (src_kind, src_id, dst_kind, dst_id, rel)).fetchone()
    if row:
        if row[1]:
            conn.execute("UPDATE git_link SET removed_at=NULL WHERE id=?", (row[0],))
        return row[0]
    cur = conn.execute(
        "INSERT INTO git_link (src_kind, src_id, dst_kind, dst_id, rel, source, "
        "confidence, evidence, content_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (src_kind, src_id, dst_kind, dst_id, rel, source, confidence,
         evidence, content_hash, stamp))
    return cur.lastrowid


def link_remove(conn, link_id, *, now=None):
    """**软删除**一条关联（写 `removed_at`），返回是否命中。"""
    cur = conn.execute("UPDATE git_link SET removed_at=? WHERE id=? AND removed_at IS NULL",
                       (now or fish_common.now_utc(), link_id))
    return bool(cur.rowcount)


def link_restore(conn, link_id):
    """撤销软删除（回滚），返回是否命中。"""
    cur = conn.execute(
        "UPDATE git_link SET removed_at=NULL WHERE id=? AND removed_at IS NOT NULL",
        (link_id,))
    return bool(cur.rowcount)


def links_of(conn, *, src_kind=None, src_id=None, dst_kind=None, dst_id=None,
             include_removed=False):
    """按 src 或 dst 查关联；默认**不含**已软删的。

    两个维度都支持，因为真实问题两个方向都会问：「这条记忆改过哪些提交」
    和「这个提交实现了哪些记忆」。
    """
    where, params = [], []
    for col, val in (("src_kind", src_kind), ("src_id", src_id),
                     ("dst_kind", dst_kind), ("dst_id", dst_id)):
        if val is not None:
            where.append(f"{col}=?")
            params.append(val)
    if not include_removed:
        where.append("removed_at IS NULL")
    sql = ("SELECT id, src_kind, src_id, dst_kind, dst_id, rel, source, confidence, "
           "evidence, content_hash, created_at, removed_at FROM git_link")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id"
    return [dict(zip(("id", "src_kind", "src_id", "dst_kind", "dst_id", "rel",
                      "source", "confidence", "evidence", "content_hash",
                      "created_at", "removed_at"), r)) for r in conn.execute(sql, params)]


def _drop_shadowed(codes):
    """去掉被更长编码包含的那些（`A-261121-01` 被 `A-261121-011` 遮蔽）。

    为什么需要：编码是 `PREFIX-YYMMDD-NN`，序号可以到 4 位（`-261022-1001`）。
    正文里出现 `A-261121-011` 时，子串匹配也会命中 `A-261121-01`——
    **那是这条编码的前缀，不是同一个对象。** 不处理就会给错的记忆挂上关联。
    """
    out = []
    for code in sorted(codes, key=len, reverse=True):
        if not any(code != seen and code in seen for seen in out):
            out.append(code)
    return out


def link_commit_mentions(conn, *, sha, text, now=None):
    """把提交 message 里**提到的记忆编码**连成 `discussed_in` 关联。

    ## 为什么这条能自动建立，别的不能

    `rel` 的四种关系里，`implemented_by` / `produced` / `revised_in` 都需要
    **语义判断**（这条记忆描述的工作是不是被这个提交做完了？），机械推断必然错。
    只有 `discussed_in` 可以零猜测：**提交正文里写没写这个编码，是一个确定的事实。**

    ## 为什么不用正则去切文本

    `fish.py` 的 `CODE_MENTION_RE` 是为「正文里找像编码的串」设计的粗筛（中文没有
    空格，它还得靠 `code_mentions_in()` 回查裁决）。这里反过来了：**拿库里真实
    存在的编码去正文里做子串匹配**——编码是唯一标识符，匹配就是匹配，不需要裁决。

    两个好处：① 不会把 `见企业智能-261022-01` 的前半截汉字并进前缀；
    ② 不需要在本模块复制一份正则（那样两份口径迟早漂）。

    成本：库里 300 余条编码 × 一次 `in` 判断，毫秒级。

    ## 返回

    `{"linked": [code...], "checked": 编码总数}`——`linked` 是**本次新建或恢复**的
    （已存在的不重复计入，因为 `link_create` 幂等）。
    """
    stamp = now or fish_common.now_utc()
    if not sha or not text:
        return {"linked": [], "checked": 0}
    # **不过滤 status**（2026-11-21 判据抓到的）：`withdrawn` 的记忆也要能挂关联——
    # 「提交提到这个编码」与「这条记忆此刻是不是作废了」是两件无关的事。
    # 尤其相反的场景才常见：提交正是在**清理/修复**一条已作废的记忆。
    rows = conn.execute("SELECT code FROM code_index").fetchall()
    codes = [r[0] for r in rows if r[0]]
    hits = _drop_shadowed([c for c in codes if c in text])
    for code in hits:
        link_create(conn, src_kind="memory", src_id=code, dst_kind="commit",
                    dst_id=sha, rel="discussed_in", source="inferred",
                    confidence=1.0, evidence="commit message 提到该编码", now=stamp)
    return {"linked": hits, "checked": len(codes)}
