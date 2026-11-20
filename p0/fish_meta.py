#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""库的 schema 版本号（F-P2-5，2026-11-18 外部审查）。

## 审查方指出的问题

> SQLite 无 schema version；`PRAGMA user_version=0`；旧库/新环境：无法判断版本；
> 列级迁移无统一备份

实测（2026-11-19，生产库）：`PRAGMA user_version = 0`，而 `migrate()` 一直在
按"缺列就补"的方式改结构——**改了十年，版本号还是 0**。

## 为什么这不是"加个数字"那么简单

`migrate()` 是**幂等补列**：每次启动都查 `PRAGMA table_info`，缺什么补什么。
所以它过去不需要版本号，也就能一直跑。

但幂等只对"补列"成立。一旦以后出现**需要判断顺序**的迁移（改类型、拆表、
回填数据），"缺什么补什么"就答不了"这一步做了没有"。

版本号存在的意义不是好看，是**让"这一步做过没有"变成可判定**。

## 设计取舍（说清楚，免得下一个人以为是疏漏）

  · **只记版本，不做自动迁移**。`SCHEMA_VERSION` 是"当前代码期望的结构"，
    `stamp()` 在 `migrate()` 补完列之后写进去。它**不会**替你改数据。
  · **老库补写成当前版本**：升级到有版本号之前的库，`user_version` 是 0，
    `stamp()` 会把它写成当前值——这是**能确认**的（补列已经跑过、没抛异常），
    不是猜测。**如果以后要加真正的分步迁移，必须从 0 起步而不是从这个值起步**，
    这一条写在 `MIGRATIONS_NOTE` 里。
  · 不做"版本低了就拒绝启动"：那是**可用性**换**正确性**的取舍，
    而幂等补列目前是安全的。版本号先落地，拒绝启动留到真有分步迁移时再说。

依赖方向：只用标准库。不 import `fish`，也不 import `fish_common`。
"""

import sqlite3

# 当前代码期望的结构版本。**改结构时必须 +1 并写清改了什么。**
SCHEMA_VERSION = 2

# 版本 2 = 2026-11-20 新增 `session_context` 表（fish 引入 git 的 P1-3）：
#   存每个会话的 cwd，供撞车判定的路径绝对化使用。此前 cwd 被 hook 拿到后即丢弃，
#   `presence.surface` 因此常为空、相对面靠分量链兜底匹配绝对目标 → 假撞车。
#   为什么必须 +1：加表是结构变化，`stamp()` 会把 user_version 写成新值；
#   不 +1 就是版本号说谎（代码期望 1、实际结构已是 2）。

# 版本 1 = 2026-11-19 首次引入版本号（此前一直是 0，结构由幂等补列维护）。
# ⚠️ 如果将来要做"从 0 开始的真分步迁移"，**不要**把这个 1 当成"第一步已完成"，
# 它是"补列时代结束"的标记。真正的分步序列应当从一个显式的 baseline 快照起算。
MIGRATIONS_NOTE = "1 = 补列时代结束；分步迁移若要做，从显式 baseline 起算，不要从 1 续。"


def read_version(conn) -> int:
    """读当前库的版本号。读不到返回 -1（区别于 0 = 老库）。"""
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    except (sqlite3.Error, TypeError, IndexError):
        return -1


def stamp(conn, version=None) -> int:
    """把版本号写成 `version`（缺省 = SCHEMA_VERSION）。返回写入前的值。

    **调用点应该在 `migrate()` 补完列之后**——那一刻"结构已符合当前期望"
    是刚被验证过的事实，不是假设。
    """
    before = read_version(conn)
    want = int(SCHEMA_VERSION if version is None else version)
    # **不降级**：库比代码新的时候（before > want）**一个字都不写**。
    # 第七轮评审实测：造一个 user_version=5 的库，`describe()` 说 newer，
    # 而 `stamp()` 照样写 1 —— 版本号被静默往下盖，与"让这步做过没有可判定"冲突。
    # 这通常意味着代码比库旧（回滚过），那种情况下**库是对的、代码是旧的**。
    if before > want:
        return before
    if before == want:
        # **已经是这个版本就一个字节都不写**。`migrate()` 在每次 CLI 调用里都会跑，
        # 无条件盖章会让"只读命令"也写一次 WAL——实测把 `board render` 变成了
        # 会改 data_root 的写入（判据 test_board_home_root 抓到）。
        return before
    # **只读连接上这一行就会抛**（`attempt to write a readonly database`），
    # 根本走不到下面的 commit——所以第一版那句 `# A档：只读连接上没有事务可提交`
    # 描述的是一个**不会发生**的场景（第七轮评审实测）。真正被吞掉的是
    # commit 因别的原因失败（如锁超时），那会让"盖章失败"变成静默。
    conn.execute(f"PRAGMA user_version = {want}")
    # **必须提交**：`PRAGMA user_version = N` 开一个写事务，不提交就放手，
    # 下一个连接会拿到 `database is locked`——实测会让别的进程整条链失败。
    conn.commit()
    return before


def describe(conn) -> dict:
    """给人看的状态：库里是多少、代码期望多少、要不要升级。"""
    got = read_version(conn)
    return {
        "db_version": got,
        "code_version": SCHEMA_VERSION,
        "state": ("unversioned" if got == 0 else
                  "same" if got == SCHEMA_VERSION else
                  "older" if got < SCHEMA_VERSION else "newer"),
        "readable": got >= 0,
        "note": MIGRATIONS_NOTE,
    }
