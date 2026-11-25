#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""身份开关判定叶子（113 §7 · W4.6c 抽层）。

为什么单独成模块（不是为了"看起来拆了"）
--------------------------------------
W4.6b 把两条判定加进了 `fish_common`：`wb_ensure_enabled`（WorkBuddy 身份要不要
ensure）与 `config_combo_warnings`（两个开关的**致命组合**体检）。判定本身是对的，
但 `fish_common` 有自己的 J4 窗口（970 行）——加完 1,055 行，整条 J4 判据从此红。

**抬窗不是解法**：本仓的历史里已经有一条被评审点名批评的抬窗记录（窗口只能往下
走，见 `test_split_contract.py` 的 J4 说明）。而这两条判定又不依赖 `fish_common`
的任何原语——一个环境变量 + 两个配置键，自足。所以整块下沉成叶子，`fish_common`
用 `from fish_config_guard import …` 再导出（**同一对象**，调用面零变化）：

    fish_config_guard（本模块：stdlib + sqlite3，零 fish 业务 import）
        ↑
    fish_common
        ↑
    其余领域模块（push / board / activity / session_hook …）

方向**单向**。本模块要是 import `fish_common`（哪怕写在函数体里"延迟导入"），
`fish_common` 那句再导出就成环——本仓已经为"隐藏的上行依赖"付过一次账
（F-P2-1，见 `fish_common.INJECTION_MARKERS` 上方的注释）。

## 为什么配置读取在这里自带一份

`fish_common.get_config` 要查 `CONFIG_DEFAULTS` 那张类型表，而那张表在本模块
**之上**——叶子不许依赖它。所以这里只保留这两条判定真正用到的两种读法
（bool / 原样字符串），语义与 `get_config` 逐条对齐：缺行 → 缺省值；
bool → `raw == "true"`；表不存在 → 抛 `sqlite3.Error`，由调用点兜（与原来一致）。

代价是**缺省值出现了两处**（这里与 `fish_common.CONFIG_DEFAULTS`）。所以
`test_split_contract.py` 里配了一条对拍判据（J-CG-2）：常量对不上、或读法在任一
档位上与 `get_config` 分叉 → 红。没有那条判据，这次抽层就只是制造第二个真相。

## 搬了什么

`wb_ensure_enabled`、`config_combo_warnings` 与它们的私有辅助（`_warn_ignored` /
`_config_raw` / `_cfg_bool` / `_cfg_str`）。**判定逻辑一个字符都没改**：环境变量
只能关、持久配置默认开、读不到按开、组合体检只在读侧喊。
"""
import os
import sqlite3
import sys

#: 两个开关的键名与缺省值。**必须与 `fish_common.CONFIG_DEFAULTS` 对齐**（J-CG-2 对拍）。
#: 为什么不是 `from fish_common import CONFIG_DEFAULTS`：那会成环（见模块 docstring）。
WB_ENSURE_KEY = "identity.wb_ensure"
WB_ENSURE_DEFAULT = True
NO_IDENTITY_KEY = "push.no_identity"
NO_IDENTITY_DEFAULT = "allow"


def _warn_ignored(what, exc):
    """静默点的出口**叶子版**：与 `fish_common.warn_ignored` 同一格式。

    为什么不 import 它：那是上行依赖（会成环）。为什么格式必须一样：stderr 那
    一行是运维唯一看得到的东西，两个出口打两种格式，读到的人会以为"有两类错误"。
    `test_split_contract.py` 的 J-CG-3 逐字对拍两边的输出，格式漂了就红。
    """
    try:
        print(f"[fish] {what} 失败（已忽略）：{exc!r}", file=sys.stderr)
    except Exception:       # noqa: BLE001 —— 连报警都失败就放弃
        pass  # A档：报警器的最后兜底，静默是唯一选择


def _config_raw(conn, key):
    """`config` 表里那一行的原值；**行不存在**返回 `None`。

    表不存在 / 库只读等错误**照样抛**（与 `get_config` 一致）：由调用点决定
    "读不到开关"该怎么兜（`wb_ensure_enabled` 兜成开，`config_combo_warnings` 兜成静默）。
    """
    row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return None if row is None else row[0]


def _cfg_bool(conn, key, default) -> bool:
    """`get_config` 的 bool 分支：缺行 → `default`，否则 `raw == "true"`。"""
    raw = _config_raw(conn, key)
    return default if raw is None else (raw == "true")


def _cfg_str(conn, key, default):
    """`get_config` 的 string/enum 分支：缺行 → `default`，否则原样返回。"""
    raw = _config_raw(conn, key)
    return default if raw is None else raw


def wb_ensure_enabled(conn, environ=None) -> tuple:
    """WorkBuddy 身份 ensure 的**唯一判定点**（113 §7 · W4.6b）：`(enabled, why_off)`。

    优先级（方向**单一**）：
      · 环境变量 `FISH_WB_ENSURE` 只能**关**（`0` / `false` / `off`）——它是应急口；
      · 否则由配置键 `identity.wb_ensure` 决定（默认 `true` = W4.6 的行为）。

    为什么环境变量**不能**把配置关掉的开关再打开：那会让"配置文件里写着 false"
    变成一句谎话；而应急口的方向从来不需要"开"（默认就是开，要开就别写 false）。

    为什么读配置失败 = **开**：旧库 / 只读介质上读配置可能抛错（配置表还没建），
    这时退回 W4.6 的默认行为。把"读不到开关"解释说成"永久只查不签"，会让新会话
    **静默**失去身份——那正是 W4.6 要治的病，不能用另一种方式复现它。

    `environ` 显式可传，是为了判据能在**不改进程环境**的前提下跑两档
    （与本仓 `start_session(conn, environ=…)` 同一手法）；缺省读 `os.environ`。
    """
    env = os.environ if environ is None else environ
    raw = str((env or {}).get("FISH_WB_ENSURE") or "").strip().lower()
    if raw in ("0", "false", "off"):
        return False, "FISH_WB_ENSURE=0（环境变量应急关断）"
    try:
        enabled = bool(_cfg_bool(conn, WB_ENSURE_KEY, WB_ENSURE_DEFAULT))
    except Exception as exc:      # noqa: BLE001 —— 读不到开关按缺省（开），见上
        _warn_ignored('fish_config_guard.py:wb_ensure_enabled', exc)
        return True, ""
    if not enabled:
        return False, "identity.wb_ensure=false（配置关断）"
    return True, ""


def config_combo_warnings(conn) -> list:
    """危险**配置组合**体检（113 J-W46-3）：返回告警列表，空 = 没有危险格。

    ## 为什么单键校验不够

    `identity.wb_ensure` 与 `push.no_identity` 各自都是**合理的单独开关**：
    前者关掉是"只查不签"（应急），后者切 `skip` 是"无身份不推"（防串推）。
    但两者**同时生效**会让 WorkBuddy 的推送面**归零**——WB 会话永远拿不到身份
    （不 ensure），而"无身份不推"把它们全部挡掉：2026-11-25T01:42Z 实测，
    此刻切 `skip` 会挡掉 7d 内 **238/312** 行 WB prompt 推送（76.3%）。
    两个键都"对"，组合是事故；**组合事故只能靠组合体检抓住**，单键校验永远绿。

    ## 为什么是"告警"而不是"拒绝"

    写入口 `set_config` 住在 `fish_db`（本波禁碰），而且同一个库会被多进程共写：
    写入侧拦一次也拦不住第二次（`fish config set` 之外还有 MCP、UI、手工 SQL 三条路）。
    所以判定放在**读侧**——谁消费这两个键，谁喊。

    ## 只看持久配置，不看环境变量

    `FISH_WB_ENSURE=0` 是运维的**一次性应急口**（人已经在场，知道后果）；持久配置
    才是无人值守会一直生效的那个。对着应急动作喊告警 = 训练人忽略告警。

    返回每条是 `{"keys": [...], "why": "..."}`：`keys` 让人知道改哪个键能解，
    `why` 写清后果（不是"看起来不对"，是"会挡掉多少"）。
    """
    out = []
    try:
        ensure_on = bool(_cfg_bool(conn, WB_ENSURE_KEY, WB_ENSURE_DEFAULT))
        if not ensure_on and str(_cfg_str(conn, NO_IDENTITY_KEY,
                                          NO_IDENTITY_DEFAULT)) == "skip":
            out.append({
                "keys": [WB_ENSURE_KEY, NO_IDENTITY_KEY],
                "why": ("identity.wb_ensure=false × push.no_identity=skip："
                        "WorkBuddy 会话永远拿不到身份，"
                        "而'无身份不推'会把它们的 prompt 推送全部挡掉"
                        "（2026-11-25T01:42Z 实测 7d 会挡 238/312 行 = 76.3%）"
                        "——两个键各自合理，合起来是推送面归零"),
            })
    except Exception as exc:      # noqa: BLE001 —— 体检失败不该拖垮调用方
        _warn_ignored('fish_config_guard.py:config_combo_warnings', exc)
    return out
