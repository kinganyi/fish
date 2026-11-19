#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话 token 的维护：清点与清理（F-P2-6，2026-11-18 外部审查）。

## 审查方指出的问题

> 377 token / 227 活；**默认永不过期**；无清理

实测（2026-11-19，生产库）：**379 总数 / 229 未撤销 / 349 永不过期 / 30 已过期未清理**。

这不是"能不能"的问题，是**每天都在变大**的问题：每开一个会话就多一行，
而没有任何命令能让它变小。

## 为什么单开一个模块

`fish.py` 顶在行数窗口上、`fish_db.py` 只剩 5 行余量（795/800）。
token 维护是一个独立关注点，不该为了塞进去去压别的注释。

依赖方向：只依赖 `fish_common`（时间/配置），**不 import `fish`**。

## 两种清理，语义不同，不许混

  · **过期**（`expires_at` 已过）：**删**。它已经无效，删掉不改变任何权限。
  · **长期未用**（未过期但很久没动）：**撤销**，不删。留着审计痕迹，
    而且"撤销"可追溯，删了就看不出曾经有过。

`last_used` 这一列**现在没有**——所以第二种只能按 `issued_at` 估，
这会在输出里明说，不假装它是"最后使用时间"。
"""

import sqlite3

# 多久没"签发"算陈旧。默认 180 天——它不是"使用"，见模块开头那段。
STALE_DAYS_DEFAULT = 180


def token_stats(conn) -> dict:
    """清点：总数 / 未撤销 / 永不过期 / 已过期 / 陈旧。"""
    def _n(sql, params=()):
        try:
            return int(conn.execute(sql, params).fetchone()[0])
        except sqlite3.Error:
            return 0
    total = _n("SELECT COUNT(*) FROM session_tokens")
    active = _n("SELECT COUNT(*) FROM session_tokens WHERE revoked_at IS NULL")
    no_exp = _n("SELECT COUNT(*) FROM session_tokens "
                "WHERE expires_at IS NULL AND revoked_at IS NULL")
    expired = _n("SELECT COUNT(*) FROM session_tokens "
                 "WHERE revoked_at IS NULL "
                 "AND expires_at IS NOT NULL AND expires_at <= ?",
                 (_now_z(conn),))
    # 陈旧 = **未过期但很久没签发**。已过期的**不算陈旧**（它归 expired 那栏，
    # 否则同一行被数两次；且对已过期的人来说"陈旧"是多余信息）。
    # 陈旧 = **未撤销 + 很久没签发 + （没有过期时间 或 还没过期）**。
    # 第七轮评审指出第一版把「永不过期」也排除了——那正是增长主体
    # （生产 218/381 永不过期，命令对它们零作用）。但**已过期的不算陈旧**，
    # 它归 expired 那栏，否则同一行被数两次（四栏必须互斥）。
    stale = _n("SELECT COUNT(*) FROM session_tokens "
               "WHERE revoked_at IS NULL AND issued_at <= ? "
               "AND (expires_at IS NULL OR expires_at > ?)",
               (_days_ago_z(STALE_DAYS_DEFAULT), _now_z(conn)))
    return {"total": total, "active": active, "never_expires": no_exp,
            "expired": expired, "stale": stale}


def token_prune(conn, *, expired=True, stale=False, dry_run=True) -> dict:
    """清理。**默认 dry-run**，且**只删过期**（那是唯一不改变权限的删除）。"""
    now = _now_z(conn)
    out = {"dry_run": bool(dry_run), "deleted": 0, "revoked": 0,
           "stale_days": STALE_DAYS_DEFAULT}

    if expired:
        sql = ("SELECT COUNT(*) FROM session_tokens "
               "WHERE expires_at IS NOT NULL AND expires_at <= ?")
        n = int(conn.execute(sql, (now,)).fetchone()[0])
        out["would_delete"] = n
        if not dry_run and n:
            conn.execute("DELETE FROM session_tokens "
                         "WHERE expires_at IS NOT NULL AND expires_at <= ?",
                         (now,))
            out["deleted"] = n

    if stale:
        cutoff = _days_ago_z(STALE_DAYS_DEFAULT)
        sql = ("SELECT COUNT(*) FROM session_tokens "
               "WHERE revoked_at IS NULL AND issued_at <= ? "
               "AND (expires_at IS NULL OR expires_at > ?)")
        m = int(conn.execute(sql, (cutoff, now)).fetchone()[0])
        out["would_revoke"] = m
        if not dry_run and m:
            conn.execute("UPDATE session_tokens SET revoked_at = ? "
                         "WHERE revoked_at IS NULL AND issued_at <= ? "
                         "AND (expires_at IS NULL OR expires_at > ?)",
                         (now, cutoff, now))
            out["revoked"] = m

    if not dry_run:
        conn.commit()
    return out


def _now_z(conn) -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _days_ago_z(days: int) -> str:
    import datetime
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
