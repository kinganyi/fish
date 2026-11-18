#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可见性（`visibility=private`）的执行层：三个函数。

## 为什么单独一个模块

不是为了"更模块化"这种理由——是因为 `fish.py` 撞到了 12,700 行的窗口上限。
第二轮评审指出注入包的"最近三件事"仍在泄露 private，而那处要改就会超窗。
按棘轮纪律（只许降）不能抬窗，所以把这三个函数搬出来腾空间。

顺带它本身也说得通：可见性是**一条横切的判定**，被 search / list / neighbors /
inject 四处共用，独立成模块比埋在一个一万三千行的文件里清楚。

## 依赖方向

`fish_common`（取配置）← 本模块 ← `fish.py`。只依赖 `get_config`，不认识记忆。
"""
from fish_common import (  # noqa: F401 —— 搬去最底层后从这里 re-export
    _visibility_enforced, _visibility_sql, get_config)


def _filter_by_visibility(conn, found, viewer_citizen_id):
    """按 `visibility` 过滤检索结果（**可选**；用户 2026-10-29 拍板"做成开关、不强制"）。

    规则（**开关打开时**）：
      · `visibility='private'` 且 `owner == viewer` → 保留
      · `visibility='private'` 且无 viewer / owner 不匹配 → 剔除
      · 其它（shared / NULL / 未知值）→ 保留

    开关**关着**时原样返回（逐字节等价旧行为，这是默认状态）。
    语义选择说明：没给 viewer 时**一律剔除 private**（宁严勿松）——猜错方向
    是"泄露"，而猜另一个方向只是"少看见一条自己的东西"，代价不对称。

    批量查询（一次 SQL）而不是逐条查：`search_memories` 是每次 recall 都走的
    热路径，逐条查会变成 N+1（评审 🟡11 同款问题）。
    """
    if not found or not _visibility_enforced(conn):
        return found
    codes = [c for c in found if c]
    if not codes:
        return found
    marks = ",".join("?" * len(codes))
    hidden = {r[0] for r in conn.execute(
        "SELECT code FROM code_index WHERE code IN (" + marks + ") "
        "AND LOWER(TRIM(COALESCE(visibility,'shared')))='private' "
        "AND COALESCE(owner,'')<>?", codes + [str(viewer_citizen_id or "")]).fetchall()}
    return {c: r for c, r in found.items() if c not in hidden}
