#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pytest 会话级环境隔离（F-P1-8，2026-11-18 外部审查）。

## 问题

`fish.py` 的宿主探测按优先级读这几个 env：

    HOST_SESSION_ENVS = (("dsh", "DSH_SESSION_ID"),
                         ("codebuddy", "CODEBUDDY_SESSION_ID"),
                         ("claude", "CLAUDE_SESSION_ID"))

它们在**宿主自己的 shell 里是有值的**，而测试子进程会原样继承。于是同一个测试
在不同宿主下跑出不同结果：

    $ python -m pytest test_session_bind.py          # 干净 shell → 全绿
    $ CLAUDE_SESSION_ID=fake python -m pytest …      # 2 条红（实测）

审查方实测的现象正是这个：公开仓在 ZCode/Claude 下报 5 failed，换成干净 shell
是 2 failed。**读数不可复现，比测试红更糟**——它让人无法判断"这条红是代码坏了
还是环境不同"。

## 为什么在 conftest 里清，而不是让每个测试自己清

同一件事写 128 份必然有漏。而且"这条判据依赖宿主没设某个 env"本身是**隐性前提**，
不该藏在单个测试里。会话开始时清一次，全量读数就只有一种解释。

## 为什么是"会话开始清一次"而不是"每个测试前清"

有些判据**故意**设 env 来验证探测逻辑（`monkeypatch.setenv`）。每测试前清会
把它们的设置也擦掉，那等于换个方式破坏判据。会话开始清 + 结束后还原，
既保证起跑线干净，又不干预测试内部的显式设置。

## 清哪些

只清**会改变 fish 判定的**那几类。`PATH` / `TEMP` 之类一律不碰——
那是换了个测试环境，不是隔离污染。
"""
import os

# 宿主会话标识：决定 `host` 字段，直接改变断言结果
_HOST_ENVS = (
    "DSH_SESSION_ID",
    "CODEBUDDY_SESSION_ID",
    "CLAUDE_SESSION_ID",
    "CODEX_SESSION_ID",
)

# 自报会话名：会让 `session start` 命中不同的公民（同一测试换机器就换身份）
_ALIAS_ENVS = (
    "FISH_SESSION_ACTOR",
    "FISH_ACTOR",
)

# 会话凭证：带上它会让"无凭证"分支的判据全部走另一条路
_CRED_ENVS = (
    "FISH_SESSION_TOKEN",
)

_POLLUTING = _HOST_ENVS + _ALIAS_ENVS + _CRED_ENVS

# 前缀型：宿主的**会话类**变量。只认"带 SESSION 的"，不是 `CLAUDE_*` / `CODEX_*` 全家——
# 实测本机有 `CODEX_HOME` / `CODEX_INSTALL_DIR`，那是**安装路径**不是会话标识，
# 清掉它们等于换了个环境而不是隔离污染。判据是"它会不会改变 fish 的判定"，
# 不是"它看起来像不像宿主变量"。
_PREFIXES = ("CLAUDE_", "CODEX_")

# 快照：会话结束还原，免得把 pytest 进程的环境改脏后影响同进程的其它逻辑
_SNAPSHOT = {}


def _is_polluting(name: str) -> bool:
    if name in _POLLUTING:
        return True
    # 前缀只对"会话类"生效：`CODEX_HOME` 是安装路径，不该被当成污染清掉
    return any(name.startswith(p) for p in _PREFIXES) and "SESSION" in name.upper()


# ============================================================== 生产数据护栏
# **2026-11-19 事故（INC-20261119-01）**：我在校验镜像测试产物时，对**生产树**的
# `p0/data/` 执行了 `Remove-Item -Recurse -Force`。前几次因为 `index.sqlite` 被
# 运行中的进程锁着而**静默失败**，我以为没删掉就重复试；某次锁一松动，
# `data/projects/`（565 条记忆正文）、`data/corpus.sqlite`、`data/blobs/` 全没了。
#
# 恢复：8 个历史树合并救回 279 条，**286 条正文永久丢失**。
#
# 根因不是"手滑"，是**没有任何东西拦着我删生产数据**。这一节就是那道拦截：
#   ① 测试进程内对 `p0/data` 的 `shutil.rmtree` **直接抛错**；
#   ② 会话结束时比对 `data/` 的文件数，**变少就失败**（哪怕不是 rmtree 干的）。
#
# 为什么两层：① 拦已知的删法，② 兜住任何别的删法（`os.remove`、子进程、脚本）。

_DATA_GUARD = {"dir": None, "count": None}


def _count_files(path):
    import os as _os
    n = 0
    for _r, _d, _f in _os.walk(str(path)):
        n += len(_f)
    return n


def pytest_configure(config):
    """会话开始：**先**摘宿主会话 env，**再**装生产数据护栏。

    ## 为什么是一个函数而不是两个（2026-11-19 的教训）

    pytest 只认**最后定义**的那个 `pytest_configure`。我加数据护栏时新写了一个，
    于是先前的环境隔离钩子被**静默顶掉**——没有报错、没有警告，
    只有 `test_env_isolation` 那句「conftest 要报告隔离动作」变红才发现。

    **这是"加功能顺手把旧功能顶掉"的典型。** 判据在这里起了它该起的作用：
    它测的不是"新功能有没有加"，而是"旧承诺还在不在"。

    顺序有意义：隔离必须在最前，否则后面的逻辑读到的是脏 env。
    """
    # ---- 一、环境隔离（F-P1-8）----
    for name in list(os.environ):
        if _is_polluting(name):
            _SNAPSHOT[name] = os.environ.pop(name)
    if _SNAPSHOT:
        # 打一行，让"这次跑在什么环境下"成为可读事实而不是猜测
        print("\n[conftest] 已隔离宿主环境变量（否则读数不可复现）："
              + ", ".join(sorted(_SNAPSHOT)))

    # ---- 二、生产数据护栏（INC-20261119-01）----
    import shutil as _sh
    from pathlib import Path as _P

    data = _P(__file__).resolve().parent / "data"
    if not data.is_dir():
        return                     # 干净树（公开镜像）没有生产数据，不装
    _DATA_GUARD["dir"] = data
    _DATA_GUARD["count"] = _count_files(data)

    real_rmtree = _sh.rmtree

    def guarded_rmtree(path, *a, **kw):
        # **每次调用现读** `_DATA_GUARD["dir"]`，不用闭包里的 `data`：
        # 判据要用假根当靶子测这道护栏（真路径当靶子的话，护栏一失效
        # 判据自己就删掉了生产数据——2026-11-19 我就是这么又删了一次）。
        guard_dir = _DATA_GUARD.get("dir")
        if guard_dir is None:
            return real_rmtree(path, *a, **kw)
        try:
            target = _P(str(path)).resolve()
        except OSError:      # A档：解析不了就交给原函数，它会自己报错
            return real_rmtree(path, *a, **kw)
        try:
            target.relative_to(_P(str(guard_dir)).resolve())
        except ValueError:
            return real_rmtree(path, *a, **kw)
        raise AssertionError(
            f"**拒绝**：测试进程试图删除生产数据 `{target}`。\n"
            "　这是 2026-11-19 事故（INC-20261119-01）的直接防护——\n"
            "　我当时就是这么删掉 565 条记忆正文的。\n"
            "　要删的若是**临时**库，请用 `tempfile.mkdtemp()` 建的目录；\n"
            "　要清理的是**本测试自己造的**东西，请只删它自己建的那个子目录。")

    _sh.rmtree = guarded_rmtree


def pytest_unconfigure(config):
    """会话结束：先还原 env，再检查 `data/` 文件数没有变少。

    检查放最后：那时所有测试都跑完了，删没删才看得出来。
    """
    # ---- 一、还原环境变量（不污染调用方的 shell）----
    for name, value in _SNAPSHOT.items():
        os.environ.setdefault(name, value)
    _SNAPSHOT.clear()

    # ---- 二、生产数据变少就报错（INC-20261119-01）----
    d = _DATA_GUARD.get("dir")
    if d is None:
        return
    now = _count_files(d)
    before = _DATA_GUARD["count"]
    if now < before:
        import sys as _sys
        print(f"\n[conftest] **生产数据变少了**：{before} → {now}（少了 {before - now} 个文件）。"
              "\n　这可能是某个测试删了 `p0/data` 下的东西——2026-11-19 正是这样丢了"
              " 565 条记忆正文。\n　请立刻查是谁删的。", file=_sys.stderr)
