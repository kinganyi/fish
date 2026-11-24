#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fish · 时间差原语（叶子模块：零 fish import，无第三方依赖）。

## 为什么单独成文件（112 W-D · C7 的落点修正）

112 的原方案是"下沉到 `fish_common`"（它已有 `now_utc` / `parse_utc`）。机制上那
没问题、也没有环，但落地时撞上 `test_split_contract.FISH_COMMON_LINES_MAX = 970`
这条**硬契约**：`fish_common` 改动前 966 行，只余 4 行，装不下这个原语（+55 行）。
窗口是验收判据，不是可以顺手调大的数字。所以独立成叶子模块。

## 机制与策略分离（这才是 C7 的重点）

  · **机制**（解析 + 相减）只有本文件的 `age_sec` 一份；
  · **策略**留在调用点并各自命名，因为对调用方的含义完全不同：

        fish_activity._age_sec_or_inf   → 解析不了 = 无穷大（过期判据：当作早已过期）
        fish_git_hook._age_sec_or_none  → 解析不了 = None（节流判据：未知就不猜）

    强行合并会逼出一个二选一的语义——那不是去重，是制造语义债。

## 解析口径

取两份旧实现的**超集**：先认 `fish_common.parse_utc` 的定长形态
（`%Y-%m-%dT%H:%M:%SZ`），再试 `fromisoformat`（把 `Z` 换成 `+00:00`，naive 当 UTC）。
生产写入口径全是定长 UTC，两者对真实数据结果相同；超集只影响"原本会被判成解析失败"
的非常规字符串（更宽容，不改变正常路径）。
"""
from __future__ import annotations

import datetime


def _parse_iso_utc(value):
    """宽松解析一个 UTC 时间戳；空值/解析不了 → `None`（不抛）。"""
    if not value:
        return None
    try:
        text = str(value).strip()
    except Exception:            # noqa: BLE001 —— 连 str() 都失败的值当作解析不了
        return None
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def age_sec(stamp, now_text=None):
    """`stamp` 距 `now_text`（缺省=现在）的秒数；解析不了返回 `None`。

    只做机制：**怎么解析、怎么相减**。解析不了返回什么由调用点按自己的判据语义决定
    （见文件头「机制与策略分离」）。

    `now_text` 给了但解析不了时**退回当前时刻**（与旧 `fish_activity._age_sec` 一致：
    它用的是 `parse_utc(now_text) or now`；旧 `fish_git_hook._age_sec` 在这种情况下
    返回 None）。生产调用点传的都是库里的定长时间戳，所以这条差异只存在于非常规输入。
    """
    if not stamp:
        return None
    start = _parse_iso_utc(stamp)
    if start is None:
        return None
    end = _parse_iso_utc(now_text) if now_text else None
    if end is None:
        end = datetime.datetime.now(datetime.timezone.utc)
    return (end - start).total_seconds()
