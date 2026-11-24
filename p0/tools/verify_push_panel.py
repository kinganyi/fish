#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设置页「推送」面板的可执行验收 — p0/tools/verify_push_panel.py

## 这份脚本回答什么

`docs/109-推送面板-设计与验收-20261124.md` 的判据是 33 条（V-01~V-33），覆盖六件事：

  A. **读口**（V-01~V-07）：`/api/meta.capabilities` 里有 `push`；`overview` 的
     `totals.total` 与库直查**相等**（口径只有一个来源）；`sum(channels.total)` 对得上；
     `config` 里 7 个可写键齐全且 `push.enabled` 只读；`topCodes` 非空且按次数降序；
     `events` 给 5 条且按 id 严格倒序；`GET /api/push/config|preset` → 405。
  B. **写口**（V-08~V-13）：未知键 / 已注册但不在白名单的键 → 400 `bad_key`；
     越界与类型错 → 400 `bad_value`；合法写 20 → 200 且**重新 GET 读回 20**（不信回执）；
     写回 10；审计逐行有 `ts/actor/key/old/new`；预设 `pause` → 9999/false/0、
     `resume` → 10/true/450（都从 GET 读回），预设审计行带 `preset` 字段。
  C. **变异自证 + 界面 + 数据安全**（V-14~V-20）：把白名单那一行换成「放行」的补丁版
     → 同一个请求必须从 400 变成 200（**判据必须有判别力**）；源码字节在跑完后**不变**；
     生产库 sha256 前后一致、**本次运行**没往生产目录写 `_audit/push-config.jsonl`
     （本来没有 → 跑完也不能有；本来就有 → 大小/行数/sha256 一个字节不长）；
     `?page=settings&sub=push` 的二级导航 2 项、推送页标题 + 工具栏 + 六块内容
     （`breakdown` 那一块由 V-32 单独判）+ 刷新 + 4 张指标卡 +
     7 个可写项 + 6 个只读项；1280×720 `scrollWidth == clientWidth`；截图落盘。
  D. **QA 第 1 轮打回的两条根因**（V-21~V-25）：**F1** —— 窗口内某通道一行都没有时
     `overview` 仍须 200（空库 + 单通道缺失两种坏输入，`sum(channels.total)` 要对得上
     `totals.total`），把修复还原成修前写法后**同一空库必须 500 `KeyError: 'accepted'`**；
     **F2** —— 同值 POST 必须 200 且 `changed=False`、审计不多一行、`config.changed` 不多
     一条、`config` 行不被插入/刷新；把同值短路关掉后同一请求必须重新写审计 +1。
  E. **独立评审打的 3 个 🟠 与 3 个关键 🟡**（V-26~V-30）：**V-26** Agent 接入 1280×720
     详情区可滚、刷新按钮 `bottom ≤ 720` 且 `elementFromPoint` 命中，把 dist 副本里本轮
     **两处布局修法一起**还原成修前写法必须红（只去 `h-full` 不够：容器那条单独也在撑高度，
     第一轮实测变异后布局仍正确 → V-26 假红）；**V-27** `reaction=pending` 只含 `reaction IS NULL`
     的行、数量与库直查相等、且**不等于**不过滤的 id 集合（判别力），空串的请求证明
     「空串=待判」这条路到不了后端；**V-28** 6 线程并发写同一键，审计链必须满足
     `old == 上一行 new`，去掉写口的串行闸必须断链；**V-29** `--host 0.0.0.0` 时两个
     写端点 403 `write_requires_loopback`（读端点 200、库一个字节不动、启动日志有警告），
     加 `--allow-remote-write` 才 200 且真写进库；**V-30** 命中缓存时 `asOf` 仍是数据时刻
     ——两次读必须是**同一个字符串**（`asOf` 与缓存戳共用一个 `built_ts`，构建跨整秒
     也不漂；修前是「asOf 取构建开始、缓存戳取构建结束」，5 次里只绿 3 次）、
     `cacheAgeSec` 变大、信封 `asOf` 也不冒充「此刻」。
  F. **二级拆解**（V-31~V-33）：**V-31** `breakdown` 的口径和——`memory.total` 同时等于
     四路（`byType` / `byProject` / `byChannel` / `kinds` 里的记忆类）之和，`kb` 与
     看板/动作流/git 的每组行和等于组 total，并且**每个 total 都用 SQL 直查对拍**
     （不信 API 自述）；**摘要相加关系**——界面那五个数（记忆/知识库/看板/动作流/git）
     相加必须等于窗口总数（两条独立分母：SQL 直查行数 + `totals.total`），而**语料不在
     和里**（`conv:` 已含在 `memory`，再加一遍就多 18 条）；空库（`push_log` 清空）与
     单通道缺失两种坏输入下同样 200 且全 0。
     **V-32** DOM——`data-push-block="breakdown"` 在、**真的画出来了**（不是 `display:none`
     / 高 0）、内容源/态势源两栏、记忆三组 + KB 两组 + 看板两组 + 动作流 + git 都有
     `data-breakdown-key` 行、1280×720 不横向溢出。**V-33** 变异自证——把 `memory.total`
     +1 → 同一把尺子（V-31）必须红；把产物副本里这一区加 `hidden` → V-32 必须红；
     恢复成基线后两边都绿。

## 数据安全（硬约束）

- **绝不写生产库**：整场跑在 `%TEMP%` 的副本上（`sqlite3` backup API 拷一份，含 `-wal` 一起），
  写测试、审计文件、预设全部落在副本目录里。
- 跑完对生产库取 sha256 与 config 行数**对拍**（V-19）；生产目录出现
  `_audit/push-config.jsonl` 直接判红。
- 变异会改**副本目录**里的一份 `fish_push_audit.py`（V-14/V-23/V-25/V-28/V-33）与**两份**
  `ui/dist` 副本（V-26 的布局变异、V-33 的 hidden 变异），绝不碰仓库源码与产物：
  跑前跑后对 `p0/fish_push_audit.py` 取 sha256（V-15）。

## 用法

    python -X utf8 p0\\tools\\verify_push_panel.py
    python -X utf8 p0\\tools\\verify_push_panel.py --keep        # 保留临时目录备查
    python -X utf8 p0\\tools\\verify_push_panel.py --skip-dom    # 只跑 API 面（DOM 记 SKIP）

退出码：全 PASS 0；有 FAIL 或 SKIP 1（**「没验」不等于「通过」**——SKIP 也返回 1）。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_P0 = os.path.dirname(_HERE)
_REPO = os.path.dirname(_P0)
for _path in (_P0, _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import accept_workbench as ACC  # noqa: E402  复用 CDP/WebSocket/端口/杀进程（不重写一套）

DEFAULT_DB = r"I:\app\fish-data\data\index.sqlite"
DEFAULT_PORT = 8791
DIST_DIR = os.path.join(_REPO, "ui", "dist")
SHOT_PATH = os.path.join(_REPO, "ui", "screenshots", "08-settings-push.png")
#: 设置页 · Agent 接入 的 1280×720 截图（V-26 写）。与 `shoot_workbench` 的
#: `06-settings-<stamp>.png`（1440×900）是**两张不同的图**：这张专门钉「720 高能不能够到刷新」。
SHOT_AGENT_PATH = os.path.join(_REPO, "ui", "screenshots", "06-settings-1280.png")

#: V-26 的坏布局变异：把本轮**两处**修法一起还原成修前写法，**改的是 dist 副本**，
#: 仓库源码与 `ui/dist` 一个字节都不动。锚点在打包产物里各自唯一命中（`_mutate_dist` 会验）。
#:
#: 为什么必须两处一起还原：本轮修了「容器 `flex flex-col`」与「根 `h-full`」两条，
#: 任一条单独都在起作用（这就是双保险的意义）——只去掉 `h-full` 时布局**仍然是对的**
#: （第一轮实测：变异后详情区照样可滚、刷新照样命中，V-26 假红）。要证明判据有判别力，
#: 变异就得还原成**修前那两行都在**的样子。
DIST_MUTATIONS = (
    # ① AgentAccess 根：去掉 `h-full`
    ("flex h-full min-h-0 min-w-0 flex-1 flex-col",
     "flex min-h-0 min-w-0 flex-1 flex-col"),
    # ② Settings 的子页容器：`flex flex-col` 退回块级（修前就是块级 + overflow-hidden）
    ("flex min-h-0 min-w-0 flex-col overflow-hidden",
     "min-h-0 min-w-0 overflow-hidden"),
)

#: 可写白名单（与 `fish_push_audit.WRITABLE_KEYS` 同一份口径；这里只用来判「7 项齐全」）。
EXPECTED_WRITABLE = (
    "push.after_tool_every_n_calls",
    "push.prompt_enabled",
    "push.prompt_max_items",
    "push.prompt_min_interval_sec",
    "push.open_budget_tokens",
    "push.score_floor",
    "push.value_floor",
)
EXPECTED_READONLY = (
    "push.enabled",
    "push.factors_enabled",
    "push.rrf_enabled",
    "push.mmr_enabled",
    "push.rerank_enabled",
    "push.corpus_enabled",
)
#: 已注册但**不在**白名单里的键（变异自证的靶子：它能被 set_config 接受，只是不许可写）。
REGISTERED_NOT_WRITABLE = "push.serendipity_ratio"
MUTATION_OLD = "    spec = WRITABLE_BY_KEY.get(key)"
MUTATION_NEW = ('    spec = WRITABLE_BY_KEY.get(key) or {"key": key, "label": key, '
                '"min": None, "max": None, "help": "", "writable": True}  '
                '# MUTATION: 白名单被绕过\n')

# ── F1/F2 的变异（V-23/V-25）：把本轮的两处修复**还原成修前写法** ─────────
#: F1 修复后（通道行不存在时给 0）。变异 = 换回 `(row or {})["accepted"]` → KeyError。
F1_ANCHOR = ('        accepted = int(row["accepted"] or 0) if row else 0\n'
             '        ignored = int(row["ignored"] or 0) if row else 0\n')
F1_MUTANT = ('        accepted = int((row or {})["accepted"] or 0)  '
             '# MUTATION: 还原 F1 修前写法\n'
             '        ignored = int((row or {})["ignored"] or 0)\n')
#: F2 修复后（值没变就 return）。变异 = 把那道短路关掉，于是照旧写库 + 记审计。
F2_ANCHOR = "        if old == new:\n"
F2_MUTANT = "        if False:  # MUTATION: 去掉 F2 的同值短路\n"

# ── V-28 的变异（🟡-1）：把写口的串行闸去掉 ─────────────────────────────
#: 锚点必须带上 `new = _check_range(...)` 那一行：`write_config` 与 `apply_preset`
#: 都以 `with _WRITE_LOCK:` + `conn = _write_conn(...)` + `try:` 开头，光用那三行会命中 2 处。
LOCK_ANCHOR = ("    new = _check_range(key, _coerce_config(key, value), spec)\n\n"
               "    with _WRITE_LOCK:\n")
LOCK_MUTANT = ("    new = _check_range(key, _coerce_config(key, value), spec)\n\n"
               "    if True:  # MUTATION: 去掉写口的串行闸（评审 🟡-1）\n")

#: V-28 并发写用到的键（白名单里的整数键，范围 1..9999）。
CONC_KEY = "push.after_tool_every_n_calls"
#: 并发线程数 / 每个线程写几次。6 线程是任务书要求的数；3 轮让「提交顺序 ≠ 追加顺序」
#: 有足够机会暴露（去掉锁时必红，见 `_concurrent_audit_chain` 的判据）。
CONC_THREADS = 6
CONC_ROUNDS = 3

# ── V-31~V-33 的尺子与变异 ─────────────────────────────────────────────
#: 判据侧的 kind 分组**独立写死**，不从 `fish_push_audit` import：判据要能判实现，
#: 不能跟实现共用同一份常量（那边改错时两边会一起错，判据就废了）。
MEMORY_KINDS = ("memory", "artifact", "episode", "fact", "rule", "lesson")
SITUATION_KINDS = ("board", "activity", "git")
#: 行纪律：每组最多 8 行 + 一行合并行（`__other__`，必须是最后一行）。
BREAKDOWN_ROWS_MAX = 9
#: V-32 必须「有 `data-breakdown-key` 行」的子组（记忆三组 / KB 两组 / 看板两组 / 动作流 / git）。
BREAKDOWN_REQUIRED_GROUPS = (
    "memory.byType", "memory.byProject", "memory.byChannel",
    "kb.byFolder", "kb.byChannel",
    "board.byProject", "board.byLabel",
    "activity.byDir", "git.byRepo",
)
#: V-32 的 DOM 必须是这两栏（内容源 / 态势源）。
BREAKDOWN_SIDES = ("content", "situation")
#: V-33 的数据面变异：`memory.total` +1（口径和立刻不成立；改的是**副本模块**）。
BREAKDOWN_DATA_MUTATION = (
    '        "memory": {\n            "total": memory_total,\n',
    '        "memory": {\n            "total": memory_total + 1,  '
    '# MUTATION: 口径和立刻不成立\n',
)
#: V-33 的 DOM 变异：给「二级拆解」区加 `hidden`（React 落成 `display:none`），
#: 让「区在**且真的画出来了**」这条判据必须红。锚点只在**产物副本**里唯一命中。
BREAKDOWN_DIST_MUTATION = (
    '"data-push-block":"breakdown"',
    '"data-push-block":"breakdown","hidden":true',
)

RESULTS: list[tuple[str, str, str]] = []


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record(cid: str, status, detail: str) -> None:
    """`status` 允许是 bool（判据返回值）或 `"SKIP"`。

    为什么不在每个调用点写 `"PASS" if ok else "FAIL"`：那行字要抄 20 遍，抄漏一处就会
    出现 `True`/`1` 这种状态词，而**状态词本身是读数**（`f"{True:<4}"` 在 Python 里会
    格式化成 `"1   "`——一个判据表里出现 `1` 和 `0`，人眼会把它当成计数）。
    """
    label = status if isinstance(status, str) else ("PASS" if status else "FAIL")
    RESULTS.append((cid, label, detail))
    print(f"{label:<4}  {cid}  {detail}")


def sha256(path: str) -> str | None:
    try:
        with open(path, "rb") as handle:
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
            return digest.hexdigest()
    except OSError:
        return None


def _file_snapshot(path: str) -> dict:
    """文件快照（是否存在 / 字节数 / 行数 / sha256 / mtime）——「跑完有没有变」的判据。

    为什么要行数：只看大小的话，「追加了等长的两行」看不出来；只看 sha256 又说不清
    「变了多少」。三样一起给，读数自己会说话。
    """
    if not os.path.exists(path):
        return {"exists": False, "bytes": None, "lines": None,
                "sha256": None, "mtime": None}
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = sum(1 for _ in handle)
    except OSError:
        lines = None
    return {"exists": True, "bytes": os.path.getsize(path), "lines": lines,
            "sha256": sha256(path),
            "mtime": datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ")}


# ── HTTP（带 status；写测试要看 400/405 的确切码）──────────────────────
def http_json(url: str, *, method: str = "GET", body=None,
              timeout: float = 60.0) -> tuple[int, dict]:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw[:400]}


# ── 临时库副本（绝不动生产库）─────────────────────────────────────────
def copy_db(source: str, target: str) -> None:
    """`sqlite3` backup API 拷一份一致性快照（比复制文件安全：WAL 里的内容也算进去）。"""
    uri = "file:" + source.replace("\\", "/") + "?mode=ro"
    src = sqlite3.connect(uri, uri=True, timeout=30.0)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


# ── 起读口（可带补丁目录 / 指定监听地址 / 抓启动日志）───────────────────
def start_api(db_path: str, port: int, *, boot_dir: str | None = None,
              dist: str | None = None, host: str | None = None,
              allow_remote_write: bool = False,
              log_path: str | None = None) -> subprocess.Popen:
    """起一个读口子进程；`host` / `allow_remote_write` / `log_path` 只给 V-26/V-29 用。"""
    argv = [sys.executable, "-X", "utf8"]
    if boot_dir:
        argv.append(os.path.join(boot_dir, "boot.py"))
    else:
        argv.append(os.path.join(_P0, "fish_workbench_api.py"))
    argv += ["--db", db_path, "--port", str(port)]
    if dist:
        argv += ["--dist", dist]
    if host:
        argv += ["--host", host]
    if allow_remote_write:
        argv += ["--allow-remote-write"]
    handle = None
    if log_path:
        # 启动日志要能被判据读到（V-29 要证明「非回环时大声警告」真的打出来了）。
        handle = open(log_path, "w", encoding="utf-8", newline="\n")
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                            stderr=handle if handle else subprocess.DEVNULL)
    if handle is not None:
        handle.close()          # 子进程已拿到自己的句柄，父进程这份可以关
    base = f"http://127.0.0.1:{port}"
    for _ in range(160):
        try:
            status, payload = http_json(f"{base}/api/meta", timeout=2)
            if status == 200:
                # **必须证明应答的就是我刚起的这个进程**：端口要是被别人占着
                # （实测踩过：另一个会话的静态服务绑在 127.0.0.1:8799，绑 0.0.0.0 的那个
                # 收不到请求），这里会安静地测上别人的服务，一路假绿。
                if payload.get("serverPid") != proc.pid:
                    ACC._kill_tree(proc)
                    raise RuntimeError(
                        f"端口 {port} 上应答的不是本次启动的读口"
                        f"（pid {payload.get('serverPid')} != {proc.pid}）：端口被占用，"
                        "换一个端口再跑")
                return proc
        except (urllib.error.URLError, OSError, ValueError):
            pass
        if proc.poll() is not None:
            raise RuntimeError(f"读口启动即退出（exit={proc.returncode}，端口 {port}）")
        time.sleep(0.25)
    ACC._kill_tree(proc)
    raise RuntimeError(f"读口没起来（db={db_path} port={port}）")


# ── 变异补丁（只在临时目录里改副本，不碰仓库源码）─────────────────────
def _patch_module(src_path: str, dest_dir: str, db_path: str, port: int,
                  replacements: list[tuple[str, str]]) -> dict:
    """把 `src_path` 里的锚点换成新片段的**副本**写进 `dest_dir`，并写一个 boot 用它起服务。

    为什么要单独一个目录 + `sys.path` 顺序：`fish_workbench_api` 的
    `import fish_push_audit` 只认 `sys.path` 里**第一个**同名模块——boot 脚本所在目录
    （脚本目录天然是 `sys.path[0]`）放补丁版，`p0` 追加在后面，于是业务模块用仓库里的，
    数据面用补丁的。仓库源码一个字节都不动。

    **锚点必须唯一命中**（命中数 ≠ 1 直接抛）：补丁没打上时变异会「看起来没有判别力」，
    而实际是判据在测一棵没被改过的树——这个坑比判据红更危险。
    """
    with open(src_path, encoding="utf-8") as handle:
        patched = handle.read()
    hits: list[int] = []
    for old, new in replacements:
        count = patched.count(old)
        if count != 1:
            raise RuntimeError(f"变异锚点不唯一（找到 {count} 处 {old!r}），拒绝打补丁")
        hits.append(count)
        patched = patched.replace(old, new, 1)
    patched_path = os.path.join(dest_dir, "fish_push_audit.py")
    with open(patched_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(patched)
    boot_path = os.path.join(dest_dir, "boot.py")
    with open(boot_path, "w", encoding="utf-8", newline="\n") as handle:
        # 路径用 `!r`（**不加 r 前缀**）：加了 r 的话写进文件的是 `r'I:\\app\\...'`，
        # 反斜杠被原样保留成两个，`fish_workbench_api` 里的 `_HERE not in sys.path` 就会
        # 判成「p0 不在路径里」→ 它把 p0 *插到最前面*，补丁目录被挤到后面 → 变异不生效。
        # 这个坑实测踩过：变异后仍然 400，看起来像「判据没判别力」，其实是补丁没被加载。
        handle.write(
            "import sys\n"
            f"sys.path.append({_P0!r})   # 补丁目录在 sys.path[0]（脚本目录），p0 追加在后\n"
            "import fish_workbench_api as A\n"
            f"raise SystemExit(A.main(['--db', {db_path!r}, '--port', '{port}']))\n"
        )
    return {"patched": patched_path, "boot": boot_path, "hits": hits}


def build_mutation(src_path: str, dest_dir: str, db_path: str, port: int) -> dict:
    """把白名单那一行换成「放行」的补丁版（V-14 用）。`hits` 是锚点命中数（恒为 1）。"""
    out = _patch_module(src_path, dest_dir, db_path, port, [(MUTATION_OLD, MUTATION_NEW)])
    out["hits"] = out["hits"][0]
    return out


# ── V-26 的坏布局变异：改的是**打包产物副本**，不是源码 ────────────────
def _mutate_dist(src_dir: str, dest_dir: str, mutations=DIST_MUTATIONS) -> dict:
    """拷一份 `ui/dist` 到 `dest_dir`，把 `mutations` 里的每条锚点换成新片段。

    **默认是 V-26 的两处布局变异**（把本轮修法一起还原成修前写法）；V-33 另外传它自己的
    那一条（给二级拆解区加 `hidden`）。每条锚点必须**唯一命中**，命中数不是 1 直接抛：
    补丁没打上时变异会「看起来没有判别力」，而实际是判据在测一棵没被改过的树。
    """
    if not os.path.isdir(src_dir):
        raise RuntimeError(f"缺构建产物：{src_dir}（先 cd ui && npm run build）")
    shutil.copytree(src_dir, dest_dir)
    assets = os.path.join(dest_dir, "assets")
    js_files = [os.path.join(assets, name) for name in sorted(os.listdir(assets))
                if name.endswith(".js")] if os.path.isdir(assets) else []
    hits: dict[str, int] = {}
    patched_files: list[str] = []
    for old, new in mutations:
        found: list[str] = []
        for path in js_files:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            count = text.count(old)
            if not count:
                continue
            if count != 1:
                raise RuntimeError(f"dist 里锚点命中 {count} 处（应为 1）：{old!r} @ {path}")
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text.replace(old, new, 1))
            found.append(path)
            patched_files.append(path)
        hits[old] = len(found)
    missing = [old for old, count in hits.items() if count != 1]
    if missing:
        raise RuntimeError(
            "dist 里这些锚点命中数不是 1："
            + "；".join(f"{old!r} → {hits[old]}" for old in missing)
            + "。类名/属性改过就该同步改变异锚点。")
    return {"dir": dest_dir, "hits": hits, "files": patched_files}


# ── V-28 的变异：把补丁过的数据面模块**当模块导入**（不用起服务）─────────
def _load_patched_module(path: str, name: str):
    """把一份补丁过的 `fish_push_audit.py` 当独立模块导入（不注册进 `sys.modules`）。

    它的 `import fish_db / fish_common / fish_ui` 仍然解析到仓库里的真模块（`sys.path`
    里已有 `p0`）——所以「库怎么读写」和线上一致，**只有被改的那一行**不一样。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"导入不了补丁模块：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _concurrent_audit_chain(module, db_path: str, *, threads: int, rounds: int) -> dict:
    """6 线程并发写同一个键，返回审计链读数（`chainOk` / 断点明细）。

    ## 为什么要在判据里**人为加宽竞争窗口**

    线上两次写之间只隔几十微秒，光靠线程起跑快慢，去掉锁也可能碰巧不交错——那样
    变异自证就成了抽奖。这里给 `fish_db.set_config` 套一层 20 ms 的 sleep：
    「读 old」在 sleep 之前、「写」在之后，于是**没有锁时**所有线程会读到同一个 old；
    **有锁时** sleep 在临界区里，链仍然严丝合缝。sleep 是**判据侧的竞争注入**，
    不写生产代码（`fish_db` 只在本次进程里被替换，跑完还原）。

    ## 判据

    审计链必须满足：`第 1 行.old == 初始值` 且 `第 i 行.old == 第 i-1 行.new`。
    这就是这份审计唯一能自证的东西——「谁把它改成现在这样」。
    """
    import threading

    import fish_common
    import fish_db

    init_conn = sqlite3.connect("file:" + db_path.replace("\\", "/") + "?mode=ro", uri=True)
    try:
        initial = fish_common.get_config(init_conn, CONC_KEY)
    finally:
        init_conn.close()

    barrier = threading.Barrier(threads)
    errors: list[str] = []
    original = fish_db.set_config

    def slow_set_config(conn, key, value, **kwargs):
        time.sleep(0.02)                      # 竞争窗口（见 docstring）
        return original(conn, key, value, **kwargs)

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            for round_no in range(rounds):
                # 每个线程的每个值都不同：这样「两条审计行的 old 相同」一定是错的。
                module.write_config(db_path, CONC_KEY, 1000 + index * 10 + round_no)
        except BaseException as exc:  # noqa: BLE001  线程里的异常要带回主线程
            errors.append(f"thread{index}: {type(exc).__name__}: {exc}")

    fish_db.set_config = slow_set_config
    try:
        pool = [threading.Thread(target=worker, args=(i,), name=f"w{i}") for i in range(threads)]
        for thread in pool:
            thread.start()
        for thread in pool:
            thread.join(timeout=120)
    finally:
        fish_db.set_config = original

    lines = [json.loads(line) for line in _audit_lines(db_path) if line.strip()]
    rows = [{"key": line.get("key"), "old": line.get("old"), "new": line.get("new")}
            for line in lines if line.get("key") == CONC_KEY]
    breaks: list[dict] = []
    previous_new = initial
    for index, row in enumerate(rows):
        if row["old"] != previous_new:
            breaks.append({"line": index + 1, "old": row["old"], "prevNew": previous_new})
        previous_new = row["new"]
    expected = threads * rounds
    return {
        "initial": initial,
        "written": len(rows),
        "expected": expected,
        "chainOk": not breaks and len(rows) == expected,
        "breaks": breaks[:5],
        "breakCount": len(breaks),
        "threadErrors": errors[:3],
        "first3": [(row["old"], row["new"]) for row in rows[:3]],
    }


# ── DOM 探针（复用 accept_workbench 的 CDP 客户端）─────────────────────
def find_browser() -> str | None:
    return ACC.find_browser()


def render(url: str, expressions: dict[str, str], *, width: int, height: int,
           wait_ms: int = 7000, shot_path: str | None = None,
           resize_to: tuple[int, int] | None = None) -> dict:
    """渲染一次页面，按 `expressions` 求值，可选改尺寸后再截图。起不来抛 RuntimeError。"""
    browser = find_browser()
    if not browser:
        raise RuntimeError("找不到无头浏览器（Edge/Chrome）")
    profile = tempfile.mkdtemp(prefix="fish-push-verify-")
    port = ACC._free_port()
    proc = subprocess.Popen(
        [browser, "--headless=new", "--disable-gpu", "--no-first-run",
         "--no-default-browser-check", f"--user-data-dir={profile}",
         f"--remote-debugging-port={port}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        target = None
        for _ in range(80):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
                    pages = [item for item in json.loads(response.read().decode())
                             if item.get("type") == "page"]
                    if pages:
                        target = pages[0]
                        break
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(0.25)
        if not target:
            raise RuntimeError("CDP 目标没起来（调试端口绑不上）")
        ws = ACC._Ws(target["webSocketDebuggerUrl"])
        try:
            ws.call(1, "Page.enable")
            ws.call(2, "Runtime.enable")
            ws.call(3, "Emulation.setDeviceMetricsOverride",
                    {"width": width, "height": height, "deviceScaleFactor": 1,
                     "mobile": False})
            ws.call(4, "Page.navigate", {"url": url})
            time.sleep(wait_ms / 1000.0)
            out: dict = {}
            for index, (name, expression) in enumerate(expressions.items(), start=10):
                result = ws.call(index, "Runtime.evaluate",
                                 {"expression": expression, "returnByValue": True})
                out[name] = result.get("result", {}).get("value")
            if resize_to:
                ws.call(80, "Emulation.setDeviceMetricsOverride",
                        {"width": resize_to[0], "height": resize_to[1],
                         "deviceScaleFactor": 1, "mobile": False})
                time.sleep(1.2)
                for index, (name, expression) in enumerate(expressions.items(), start=100):
                    result = ws.call(index, "Runtime.evaluate",
                                     {"expression": expression, "returnByValue": True})
                    out[f"wide_{name}"] = result.get("result", {}).get("value")
            if shot_path:
                shot = ws.call(90, "Page.captureScreenshot",
                               {"format": "png", "captureBeyondViewport": False})
                with open(shot_path, "wb") as handle:
                    handle.write(base64.b64decode(shot["data"]))
        finally:
            ws.close()
        return out
    finally:
        ACC._kill_tree(proc)
        shutil.rmtree(profile, ignore_errors=True)


#: 一次渲染要取的读数（键名 → JS 表达式）。
DOM_PROBES = {
    "navItems": 'document.querySelectorAll(\'nav[aria-label="设置二级导航"] '
                '[data-settings-page]\').length',
    "navLabels": "Array.from(document.querySelectorAll('nav[aria-label=\"设置二级导航\"] "
                 "[data-settings-page]')).map(function(n){return (n.innerText||'').replace("
                 "/\\s+/g,' ').trim()})",
    "pushPage": "document.querySelectorAll('[data-push-page]').length",
    "title": "document.querySelector('[data-push-page] h2') ? "
             "document.querySelector('[data-push-page] h2').innerText.trim() : ''",
    "blocks": "Array.from(document.querySelectorAll('[data-push-block]')).map("
              "function(n){return n.getAttribute('data-push-block')})",
    "refresh": 'document.querySelectorAll(\'[data-push-action="refresh"]\').length',
    "metrics": "document.querySelectorAll('[data-push-metric]').length",
    "configRows": "document.querySelectorAll('[data-push-config]').length",
    "readonlyRows": "document.querySelectorAll('[data-push-readonly]').length",
    "presetButtons": "document.querySelectorAll('[data-push-preset]').length",
    "eventRows": "document.querySelectorAll('[data-push-event]').length",
    "channelRows": "document.querySelectorAll('[data-push-channel]').length",
    # ── V-32 的读数：二级拆解区（存在 + **真的画出来了** + 两栏 + 每个子组几行）──
    "breakdownBlock": "document.querySelectorAll('[data-push-block=\"breakdown\"]').length",
    # 为什么要量 `getBoundingClientRect().height` 与 `display`：只数 DOM 节点的话，
    # 给这一区加个 `hidden` 也照样「存在」——V-33 的 DOM 变异就是为了证明这条判据有牙齿。
    "breakdownBox": "(function(){var b=document.querySelector("
                    "'[data-push-block=\"breakdown\"]');if(!b)return null;"
                    "var r=b.getBoundingClientRect();"
                    "return {w:Math.round(r.width),h:Math.round(r.height),"
                    "display:getComputedStyle(b).display,visible:b.offsetParent!==null}})()",
    "breakdownSides": "Array.from(document.querySelectorAll('[data-breakdown-side]'))"
                      ".map(function(n){return n.getAttribute('data-breakdown-side')})",
    "breakdownGroups": "Array.from(document.querySelectorAll('[data-breakdown-group]'))"
                       ".map(function(g){return {group:g.getAttribute('data-breakdown-group'),"
                       "rows:g.querySelectorAll('[data-breakdown-key]').length}})",
    "text": "document.body ? document.body.innerText : ''",
    "scrollWidth": "document.documentElement.scrollWidth",
    "clientWidth": "document.documentElement.clientWidth",
    "bodyScrollWidth": "document.body.scrollWidth",
}


#: V-26 的读数：设置页 · Agent 接入（`?page=settings`，缺省子页）在 1280×720 下
#: 「详情区可滚 + 刷新按钮够得着」。选择器只用**界面已有的稳定钩子**
#: （`main[aria-label="设置"]` / `section[aria-label="宿主详情"]` / `[data-host]` /
#: `[data-host-action="refresh"]`），不靠文案匹配。
_AGENT_DETAIL = ('document.querySelector(\'main[aria-label="设置"] '
                 'section[aria-label="宿主详情"]\')')


def _agent_js(body: str) -> str:
    """把 `__D__` 换成「详情容器」表达式，再包成 IIFE。

    为什么不用 f-string 直接拼：表达式里有 `{` `}`，f-string 会把它们当占位符，
    要么转义成 `{{`/`}}`（拼错一个就变成非法 JS，`Runtime.evaluate` 只回 null，
    读数静默消失——第一轮实测踩过 `scrollTopApplied: null`）。用占位符替换没有这个问题。
    """
    return "(function(){" + body.replace("__D__", _AGENT_DETAIL) + "})()"


AGENT_DOM_PROBES = {
    "hostRows": 'document.querySelectorAll(\'main[aria-label="设置"] [data-host]\').length',
    "hostsNav": ('document.querySelectorAll('
                 '\'[data-settings-page="hosts"][aria-current="page"]\').length'),
    "detail": _agent_js("var d=__D__;return d?1:0"),
    "scrollHeight": _agent_js("var d=__D__;return d?d.scrollHeight:-1"),
    "clientHeight": _agent_js("var d=__D__;return d?d.clientHeight:-1"),
    # 把 scrollTop 改成 120 再读回：非 0 就是「这个容器真的能滚」的第二个证据
    # （第一格是 scrollHeight > clientHeight）。
    "scrollTopApplied": _agent_js("var d=__D__;if(!d)return -1;"
                                  "d.scrollTop=120;return Math.round(d.scrollTop)"),
    "refresh": _agent_js("var d=__D__;if(!d)return null;"
                         "var b=d.querySelector('[data-host-action=\"refresh\"]');"
                         "if(!b)return null;"
                         "b.scrollIntoView({block:'center'});"
                         "var r=b.getBoundingClientRect();"
                         "var cx=Math.round(r.left+r.width/2),cy=Math.round(r.top+r.height/2);"
                         "var el=document.elementFromPoint(cx,cy);"
                         "return {top:Math.round(r.top),bottom:Math.round(r.bottom),"
                         "centerX:cx,centerY:cy,"
                         "inViewport:(cx>=0&&cx<=window.innerWidth"
                         "&&cy>=0&&cy<=window.innerHeight),"
                         "hit:Boolean(el&&(el===b||b.contains(el))),"
                         "hitTag:el?el.tagName:null}"),
    # 截图前把滚动位置还原（图要拍「进来时看到的那一屏」，不是探针滚到的位置）。
    "resetScroll": _agent_js("var d=__D__;if(d)d.scrollTop=0;return 0"),
}


def _agent_layout_ok(probe: dict | None) -> tuple[bool, dict]:
    """把 V-26 的读数聚成一个布尔 + 明细（基线与变异两侧共用同一把尺）。"""
    probe = probe or {}
    refresh = probe.get("refresh") or {}
    scroll_h = probe.get("scrollHeight")
    client_h = probe.get("clientHeight")
    applied = probe.get("scrollTopApplied")
    scrollable = (isinstance(scroll_h, int) and isinstance(client_h, int)
                  and scroll_h - client_h > 2)
    can_scroll = scrollable or (isinstance(applied, int) and applied > 0)
    reach = bool(
        refresh
        and isinstance(refresh.get("bottom"), int)
        and refresh["bottom"] <= 720
        and isinstance(refresh.get("top"), int)
        and refresh["top"] >= 0
        and refresh.get("inViewport") is True
        and refresh.get("hit") is True)
    ok = (probe.get("hostRows", 0) >= 1
          and probe.get("hostsNav") == 1
          and probe.get("detail") == 1
          and can_scroll
          and reach)
    return ok, {"hostRows": probe.get("hostRows"), "hostsNav": probe.get("hostsNav"),
                "detail": probe.get("detail"),
                "scrollHeight": scroll_h, "clientHeight": client_h,
                "scrollTopApplied": applied, "scrollable": scrollable,
                "canScroll": can_scroll, "reach": reach, "refresh": refresh}


def _config_flag(db_path: str, key: str):
    """库里那个配置位的真假；读不到给 `None`（判不到，**不是** `False`）。"""
    raw = _config_value(db_path, key)
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _breakdown_sql_counts(db_path: str, since: str) -> dict:
    """**直接 SQL 数一遍**（V-31 的独立尺子：不信 breakdown 自己的任何字段）。

    `memory` 用 `total - kb - board - activity - git` 反推：`conv:` 按既有契约归 memory，
    所以这条减法与实现里的「记忆 = 非态势类」是同一口径的**两条独立路径**——两路对不上
    就说明有一路错了（这正是 V-31 要的判别力）。
    """
    def count(extra: str = "") -> int:
        return count_rows(db_path,
                          "SELECT COUNT(*) FROM push_log WHERE pushed_at >= ?" + extra,
                          (since,))

    total = count()
    kb = count(" AND code LIKE 'kb:%'")
    board = count(" AND code LIKE 'board:%'")
    activity = count(" AND code LIKE 'activity:%'")
    git = count(" AND code LIKE 'git:%'")
    conv = count(" AND code LIKE 'conv:%'")
    return {"total": total, "kb": kb, "board": board, "activity": activity,
            "git": git, "conv": conv,
            "memory": total - kb - board - activity - git,
            "corpusEnabled": _config_flag(db_path, "push.corpus_enabled")}


def _breakdown_checks(data: dict | None, sql: dict) -> tuple[bool, dict]:
    """V-31 的尺子：口径和 + 每行纪律 + 每组与 SQL 直查相等（基线与变异共用）。

    `data` = `/api/push/overview` 的 `data`；`sql` = `_breakdown_sql_counts(...)`。
    空库也用它判：0 行时所有和都成立、所有组都是空列表——**不另写一把空库专用的尺子**，
    否则「空库通过」不能证明线上那条路径没坏。
    """
    data = data or {}
    bd = data.get("breakdown") or {}
    memory = bd.get("memory") or {}
    kb = bd.get("kb") or {}
    board = bd.get("board") or {}
    activity = bd.get("activity") or {}
    git = bd.get("git") or {}
    corpus = bd.get("corpus") or {}
    kinds = {item.get("kind"): int(item.get("total") or 0)
             for item in (data.get("kinds") or [])}

    def total_of(node) -> int:
        return int((node or {}).get("total") or 0)

    def sum_rows(rows) -> int:
        return sum(int(row.get("total") or 0) for row in (rows or []))

    groups = {
        "memory.byType": memory.get("byType") or [],
        "memory.byProject": memory.get("byProject") or [],
        "memory.byChannel": memory.get("byChannel") or [],
        "kb.byFolder": kb.get("byFolder") or [],
        "kb.byChannel": kb.get("byChannel") or [],
        "board.byProject": board.get("byProject") or [],
        "board.byLabel": board.get("byLabel") or [],
        "activity.byDir": activity.get("byDir") or [],
        "git.byRepo": git.get("byRepo") or [],
    }
    row_problems: list[str] = []
    for name, rows in groups.items():
        if len(rows) > BREAKDOWN_ROWS_MAX:
            row_problems.append(f"{name}: {len(rows)} 行 > {BREAKDOWN_ROWS_MAX}")
        keys = [row.get("key") for row in rows]
        if "__other__" in keys and keys[-1] != "__other__":
            row_problems.append(f"{name}: __other__ 不在最后一行")
        for row in rows:
            if int(row.get("total") or 0) < 0:
                row_problems.append(f"{name}: {row.get('key')} total<0")
            share = row.get("share")
            if not isinstance(share, (int, float)) or not (0.0 <= float(share) <= 1.0 + 1e-9):
                row_problems.append(f"{name}: {row.get('key')} share={share!r}")
            if not row.get("label"):
                row_problems.append(f"{name}: {row.get('key')} 没有 label")

    # 摘要五个数之和（不含 corpus：它已含在 memory 里）与窗口总数的两个来源。
    sum5 = (total_of(memory) + total_of(kb) + total_of(board)
            + total_of(activity) + total_of(git))
    total_api = int((data.get("totals") or {}).get("total") or 0)

    checks = {
        "breakdownPresent": bool(bd),
        "memorySql": total_of(memory) == sql.get("memory"),
        "memoryKinds": total_of(memory) == sum(kinds.get(kind, 0) for kind in MEMORY_KINDS),
        "memoryTypeSum": sum_rows(groups["memory.byType"]) == total_of(memory),
        "memoryProjectSum": sum_rows(groups["memory.byProject"]) == total_of(memory),
        "memoryChannelSum": sum_rows(groups["memory.byChannel"]) == total_of(memory),
        "kbSql": total_of(kb) == sql.get("kb"),
        "kbFolderSum": sum_rows(groups["kb.byFolder"]) == total_of(kb),
        "kbChannelSum": sum_rows(groups["kb.byChannel"]) == total_of(kb),
        "boardSql": total_of(board) == sql.get("board"),
        "boardProjectSum": sum_rows(groups["board.byProject"]) == total_of(board),
        "boardLabelSum": sum_rows(groups["board.byLabel"]) == total_of(board),
        "activitySql": total_of(activity) == sql.get("activity"),
        "activityDirSum": sum_rows(groups["activity.byDir"]) == total_of(activity),
        "gitSql": total_of(git) == sql.get("git"),
        "gitRepoSum": sum_rows(groups["git.byRepo"]) == total_of(git),
        "situationKinds": (total_of(board) + total_of(activity) + total_of(git)
                           == sum(kinds.get(kind, 0) for kind in SITUATION_KINDS)),
        "corpusSql": total_of(corpus) == sql.get("conv"),
        # 语料这块**不写死 `enabled=false`**：判的是「与库里的 push.corpus_enabled 一致」。
        # 写死 false 的判据会在有人打开语料时变红——那是数据变了，不是代码坏了。
        "corpusEnabledMatchesDb": (sql.get("corpusEnabled") is not None
                                   and corpus.get("enabled") == sql.get("corpusEnabled")),
        "corpusNote": "corpus_enabled" in str(corpus.get("note") or ""),
        # **摘要相加关系**（QA 🟡②）：界面上那五个数（记忆/知识库/看板/动作流/git）相加必须
        # 等于窗口总数——两条独立路径各给一次分母：① `push_log` SQL 直查的行数、
        # ② `totals.total`（`fish.push_stats` 算的另一个数）。
        # **语料不在和里**：`conv:` 按既有契约归 `memory`（本库 18 条，落在 byType「未分类」），
        # 再加一遍就会比总数多 18。所以 `sum5 + corpus` 只有在 corpus 为 0 时才等于总数。
        "summaryAddsUp": (sum5 == int(sql.get("total") or 0) == total_api),
        "corpusInsideMemory": total_of(corpus) <= total_of(memory),
        "rowsOk": not row_problems,
    }
    return all(checks.values()), {
        **checks,
        "rowProblems": row_problems,
        "summary": {"sum5": sum5, "totalApi": total_api, "totalSql": int(sql.get("total") or 0),
                    "corpusIncludedInMemory": total_of(corpus)},
        "totals": {"memory": total_of(memory), "kb": total_of(kb),
                   "board": total_of(board), "activity": total_of(activity),
                   "git": total_of(git), "corpus": total_of(corpus)},
        "groupCounts": {name: len(rows) for name, rows in groups.items()},
        "kinds": kinds,
        "sql": sql,
    }


def _breakdown_dom_ok(probe: dict | None) -> tuple[bool, dict]:
    """V-32 的尺子：区在 → **真的画出来了** → 两栏 → 9 个子组各有 `data-breakdown-key` 行。

    「存在」与「看得见」是两件事：`hidden` 的 section 照样能被 `querySelector` 选中，
    所以这里量 `getBoundingClientRect().height > 20`、`display != 'none'`、
    `offsetParent !== null` 三样（V-33 的 DOM 变异证明这一条有牙齿）。
    """
    probe = probe or {}
    box = probe.get("breakdownBox") or {}
    sides = sorted(probe.get("breakdownSides") or [])
    groups = {str(item.get("group")): int(item.get("rows") or 0)
              for item in (probe.get("breakdownGroups") or [])
              if isinstance(item, dict)}
    missing = [name for name in BREAKDOWN_REQUIRED_GROUPS if groups.get(name, 0) < 1]
    visible = (isinstance(box.get("h"), int) and box["h"] > 20
               and box.get("display") != "none" and box.get("visible") is True)
    ok = (probe.get("breakdownBlock") == 1
          and sides == sorted(BREAKDOWN_SIDES)
          and visible
          and not missing)
    return ok, {"block": probe.get("breakdownBlock"), "sides": sides, "box": box,
                "groups": groups, "missingGroups": missing, "visible": visible}


def _port_is_free(port: int) -> bool:
    """端口是不是真的**空**（不只看能不能 bind：Windows 上 0.0.0.0 与 127.0.0.1 会「共存」，
    请求却只发给 127.0.0.1 的那个 —— 实测踩过一次假绿，所以这里两头都试）。"""
    for host in ("127.0.0.1", "0.0.0.0"):
        with socket.socket() as probe:
            try:
                probe.bind((host, port))
            except OSError:
                return False
    return True


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# ── 判据 ────────────────────────────────────────────────────────────────
def _reaction_map(db_path: str, ids: list[int]) -> dict:
    """这些 id 在库里各自的 `reaction`（`None` = 待判）。判「过滤真的在按库里的值筛」。"""
    if not ids:
        return {}
    conn = sqlite3.connect("file:" + db_path.replace("\\", "/") + "?mode=ro", uri=True,
                           timeout=15.0)
    try:
        marks = ",".join("?" * len(ids))
        return {int(row[0]): row[1] for row in conn.execute(
            f"SELECT id, reaction FROM push_log WHERE id IN ({marks})", tuple(ids))}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _ids_of(envelope: dict | None) -> list:
    return [int(item.get("id")) for item in
            (((envelope or {}).get("data") or {}).get("items") or [])]
def main() -> int:
    run_started = time.time()          # V-20 用它判「截图是本次运行之后写的」
    parser = argparse.ArgumentParser(
        description="设置页「推送」面板验收（V-01~V-33；只写临时库副本）")
    parser.add_argument("--db", default=DEFAULT_DB, help="生产库（只读拷贝它，绝不写它）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--keep", action="store_true", help="保留临时目录备查")
    parser.add_argument("--skip-dom", action="store_true", help="只跑 API 面（DOM 记 SKIP）")
    args = parser.parse_args()

    prod_db = os.path.abspath(args.db)
    api_src = os.path.join(_P0, "fish_push_audit.py")
    if not os.path.exists(prod_db):
        print(f"FAIL  生产库不存在：{prod_db}")
        return 2
    if not os.path.exists(api_src):
        print(f"FAIL  缺数据面模块：{api_src}")
        return 2

    prod_sha = sha256(prod_db)
    prod_audit = os.path.join(os.path.dirname(prod_db), "_audit", "push-config.jsonl")
    # 生产目录里的审计文件**可能在本次运行之前就已经存在**（别的会话在推送页上点过保存/预设）。
    # 那种情况下「文件不存在」不是一个能成立的前提——判据要问的是「**这一次**有没有往生产写」，
    # 所以先拍一张快照（大小 + 行数 + sha256 + mtime），跑完对拍「一个字节都没长」。
    # 环境本来是干净的，就仍然判「跑完也不该出现这个文件」（原判据的字面口径不放宽）。
    prod_audit_before = _file_snapshot(prod_audit)
    src_sha = sha256(api_src)
    print(f"# 判定时刻 {iso_now()}")
    print(f"# 生产库 {prod_db}")
    print(f"#   sha256 {prod_sha} · mtime "
          f"{datetime.fromtimestamp(os.path.getmtime(prod_db), timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print(f"# 数据面 {api_src}\n#   sha256 {src_sha}")
    print(f"# 截图目标 {SHOT_PATH}")

    dist_index = os.path.join(DIST_DIR, "index.html")
    if os.path.exists(dist_index):
        dist_mtime = datetime.fromtimestamp(os.path.getmtime(dist_index), timezone.utc)
        print(f"# ui/dist/index.html mtime {dist_mtime.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    else:
        print("# ui/dist 不存在：DOM 类判据会记 SKIP（先 cd ui && npm run build）")

    work = tempfile.mkdtemp(prefix="fish-push-verify-")
    copy_path = os.path.join(work, "index.sqlite")
    copy_db(prod_db, copy_path)
    print(f"# 临时副本 {copy_path}（{os.path.getsize(copy_path)} 字节）")

    base = f"http://127.0.0.1:{args.port}"
    if not _port_is_free(args.port):
        print(f"FAIL  端口 {args.port} 已被占用（不是本脚本的实例）：换端口重跑，"
              f"例如 --port {_free_port()}")
        return 2
    server = None
    mutate_server = None
    mutate_copy = None
    #: F1/F2 的坏输入要自己的读口（各自的副本库），统一在这里登记、收尾一起杀。
    extra_servers: list = []
    try:
        server = start_api(copy_path, args.port, dist=DIST_DIR)
        print(f"# 读口已起 {base}（pid {server.pid}）\n")

        conn = sqlite3.connect("file:" + copy_path.replace("\\", "/") + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            # ── A. 读口 ────────────────────────────────────────────────
            status, meta = http_json(f"{base}/api/meta")
            caps = (meta or {}).get("capabilities") or []
            record("V-01 /api/meta.capabilities 含 push",
                   status == 200 and "push" in caps,
                   json.dumps({"status": status, "capabilities": caps,
                               "count": len(caps), "readOnly": meta.get("readOnly")},
                              ensure_ascii=False))

            status, overview_env = http_json(f"{base}/api/push/overview?days=30")
            overview = (overview_env or {}).get("data") or {}
            window = overview.get("window") or {}
            totals = overview.get("totals") or {}
            since = window.get("since")
            db_total = None
            if since:
                db_total = conn.execute(
                    "SELECT COUNT(*) FROM push_log WHERE pushed_at >= ?", (since,)).fetchone()[0]
            record("V-02 overview.totals.total == 库直查（同 since）",
                   status == 200 and since is not None and db_total == totals.get("total"),
                   json.dumps({"status": status, "since": since, "dbTotal": db_total,
                               "apiTotal": totals.get("total"), "envelopePid": overview_env.get("pid"),
                               "serverPid": meta.get("serverPid")}, ensure_ascii=False))

            channels = overview.get("channels") or []
            channel_sum = sum(int(item.get("total") or 0) for item in channels)
            record("V-03 sum(channels.total) == totals.total",
                   bool(channels) and channel_sum == totals.get("total"),
                   json.dumps({"channels": [(c.get("channel"), c.get("total")) for c in channels],
                               "sum": channel_sum, "total": totals.get("total")},
                              ensure_ascii=False))

            config = overview.get("config") or []
            writable = [item for item in config if item.get("writable")]
            readonly = [item for item in config if not item.get("writable")]
            writable_keys = sorted(item.get("key") for item in writable)
            readonly_keys = sorted(item.get("key") for item in readonly)
            enabled_row = next((item for item in config if item.get("key") == "push.enabled"), None)
            config_ok = (
                writable_keys == sorted(EXPECTED_WRITABLE)
                and readonly_keys == sorted(EXPECTED_READONLY)
                and enabled_row is not None and enabled_row.get("writable") is False
                and all("max" in item and "min" in item and item.get("help") for item in writable)
                and all(item.get("source") in ("db", "default") for item in config)
            )
            record("V-04 config：7 可写 + push.enabled 等 6 只读",
                   config_ok,
                   json.dumps({"writable": writable_keys, "readonly": readonly_keys,
                               "pushEnabled": {"value": (enabled_row or {}).get("value"),
                                               "writable": (enabled_row or {}).get("writable")},
                               "ranges": [(item.get("key"), item.get("min"), item.get("max"))
                                          for item in writable]}, ensure_ascii=False))

            top = overview.get("topCodes") or []
            counts = [int(item.get("count") or 0) for item in top]
            top_ok = (len(top) > 0 and counts == sorted(counts, reverse=True)
                      and all(item.get("code") for item in top)
                      and all(item.get("lastAt") for item in top)
                      and len(top) <= 12)
            record("V-05 topCodes 非空、按次数降序、≤12",
                   top_ok,
                   json.dumps({"returned": len(top), "counts": counts[:6],
                               "first": top[0] if top else None}, ensure_ascii=False))

            status, events_env = http_json(f"{base}/api/push/events?limit=5")
            items = (events_env.get("data") or {}).get("items") or []
            ids = [int(item.get("id") or 0) for item in items]
            events_ok = (status == 200 and len(items) == 5
                         and ids == sorted(ids, reverse=True) and len(set(ids)) == 5
                         and all(item.get("pushedAt") for item in items))
            record("V-06 events?limit=5 给 5 条、按 id 严格倒序",
                   events_ok,
                   json.dumps({"status": status, "ids": ids,
                               "reactions": [item.get("reaction") for item in items],
                               "channels": [item.get("channel") for item in items]},
                              ensure_ascii=False))

            status_config, body_config = http_json(f"{base}/api/push/config")
            status_preset, body_preset = http_json(f"{base}/api/push/preset")
            record("V-07 GET /api/push/config|preset → 405",
                   status_config == 405 and status_preset == 405
                   and body_config.get("code") == "method_not_allowed"
                   and body_preset.get("code") == "method_not_allowed",
                   json.dumps({"config": [status_config, body_config.get("code")],
                               "preset": [status_preset, body_preset.get("code")],
                               "hint": body_config.get("hint")}, ensure_ascii=False))

            # ── V-27（评审 🟠-2）：`reaction=pending` 显式哨兵 ──────────────
            # 先**造出**三种 reaction 都在最新几行里的坏输入（只改 `%TEMP%` 副本）：
            # 判据自己先证明「输入造出来了」（每条 UPDATE 都必须影响 1 行），否则可能是在
            # 一棵本来就没有 accepted/ignored 的树上空跑。
            injected = {
                "accepted": exec_write(
                    copy_path,
                    "UPDATE push_log SET reaction='accepted' WHERE id IN "
                    "(SELECT id FROM push_log ORDER BY id DESC LIMIT 1)"),
                "ignored": exec_write(
                    copy_path,
                    "UPDATE push_log SET reaction='ignored' WHERE id IN "
                    "(SELECT id FROM push_log ORDER BY id DESC LIMIT 1 OFFSET 1)"),
                "pending": exec_write(
                    copy_path,
                    "UPDATE push_log SET reaction=NULL WHERE id IN "
                    "(SELECT id FROM push_log ORDER BY id DESC LIMIT 1 OFFSET 2)"),
            }
            null_total = count_rows(copy_path,
                                    "SELECT COUNT(*) FROM push_log WHERE reaction IS NULL")
            accepted_total = count_rows(copy_path,
                                        "SELECT COUNT(*) FROM push_log WHERE reaction='accepted'")
            ignored_total = count_rows(copy_path,
                                       "SELECT COUNT(*) FROM push_log WHERE reaction='ignored'")
            st_pending, env_pending = http_json(
                f"{base}/api/push/events?limit=100&reaction=pending")
            st_accepted, env_accepted = http_json(
                f"{base}/api/push/events?limit=100&reaction=accepted")
            st_ignored, env_ignored = http_json(
                f"{base}/api/push/events?limit=100&reaction=ignored")
            st_all, env_all = http_json(f"{base}/api/push/events?limit=100")
            # **空串**：证明它到不了后端（`parse_qs` 丢空值）——这就是换哨兵的理由。
            st_blank, env_blank = http_json(f"{base}/api/push/events?limit=100&reaction=")
            pending_ids = _ids_of(env_pending)
            accepted_ids = _ids_of(env_accepted)
            ignored_ids = _ids_of(env_ignored)
            all_ids = _ids_of(env_all)
            blank_ids = _ids_of(env_blank)
            pending_db = _reaction_map(copy_path, pending_ids)
            accepted_db = _reaction_map(copy_path, accepted_ids)
            ignored_db = _reaction_map(copy_path, ignored_ids)
            filter_pending = ((env_pending or {}).get("data") or {}).get("filter") or {}
            filter_blank = ((env_blank or {}).get("data") or {}).get("filter") or {}
            reaction_ok = (
                all(injected[key] == 1 for key in injected)          # 坏输入真的造出来了
                and all(status == 200 for status in
                        (st_pending, st_accepted, st_ignored, st_all, st_blank))
                and len(pending_ids) == min(100, null_total)          # 数量精确
                and all(value is None for value in pending_db.values())  # 全是 NULL
                and len(accepted_ids) == min(100, accepted_total)
                and all(value == "accepted" for value in accepted_db.values())
                and len(ignored_ids) == min(100, ignored_total)
                and all(value == "ignored" for value in ignored_db.values())
                and filter_pending.get("reaction") == "pending"       # 回执里是规范值
                # **判别力**：只筛待判的 id 集合必须与「不过滤」不同（否则过滤等于没生效）
                and set(pending_ids) != set(all_ids)
                # 空串 = 不过滤（不是「待判」）：契约里不再有「空串=待判」这条路
                and filter_blank.get("reaction") is None
                and blank_ids == all_ids
            )
            record("V-27 reaction=pending 只含 NULL 且 ≠ 不过滤（空串到不了后端，故用哨兵）",
                   reaction_ok,
                   json.dumps({"injected": injected,
                               "totals": {"pending": null_total, "accepted": accepted_total,
                                          "ignored": ignored_total},
                               "returned": {"pending": len(pending_ids),
                                            "accepted": len(accepted_ids),
                                            "ignored": len(ignored_ids),
                                            "all": len(all_ids)},
                               "filterEcho": filter_pending.get("reaction"),
                               "blankFilter": filter_blank.get("reaction"),
                               "pendingEqualsAll": set(pending_ids) == set(all_ids),
                               "samplePending": pending_ids[:5],
                               "sampleAll": all_ids[:5]}, ensure_ascii=False))

            # ── B. 写口 ────────────────────────────────────────────────
            status_unknown, body_unknown = http_json(
                f"{base}/api/push/config", method="POST",
                body={"key": "not.a.real.key", "value": 1})
            status_registered, body_registered = http_json(
                f"{base}/api/push/config", method="POST",
                body={"key": REGISTERED_NOT_WRITABLE, "value": 0.2})
            status_enabled, body_enabled = http_json(
                f"{base}/api/push/config", method="POST",
                body={"key": "push.enabled", "value": False})
            record("V-08 未知键 / 白名单外已注册键 / push.enabled → 400 bad_key",
                   status_unknown == 400 and body_unknown.get("code") == "bad_key"
                   and status_registered == 400 and body_registered.get("code") == "bad_key"
                   and status_enabled == 400 and body_enabled.get("code") == "bad_key",
                   json.dumps({"unknown": [status_unknown, body_unknown.get("code")],
                               "registeredNotWritable": [status_registered,
                                                         body_registered.get("code")],
                               "pushEnabled": [status_enabled, body_enabled.get("code")],
                               "enabledHint": body_enabled.get("hint")}, ensure_ascii=False))

            status_range, body_range = http_json(
                f"{base}/api/push/config", method="POST",
                body={"key": "push.prompt_max_items", "value": 99})
            status_type, body_type = http_json(
                f"{base}/api/push/config", method="POST",
                body={"key": "push.after_tool_every_n_calls", "value": True})
            status_float, body_float = http_json(
                f"{base}/api/push/config", method="POST",
                body={"key": "push.score_floor", "value": "abc"})
            record("V-09 越界值 / 类型错（bool 给 int、字符串给 float）→ 400 bad_value",
                   status_range == 400 and body_range.get("code") == "bad_value"
                   and status_type == 400 and body_type.get("code") == "bad_value"
                   and status_float == 400 and body_float.get("code") == "bad_value",
                   json.dumps({"range": [status_range, body_range.get("hint")],
                               "type": [status_type, body_type.get("hint")],
                               "float": [status_float, body_float.get("hint")]},
                              ensure_ascii=False))

            before_audit = len(_audit_lines(copy_path))
            status_write, body_write = http_json(
                f"{base}/api/push/config", method="POST",
                body={"key": "push.after_tool_every_n_calls", "value": 20})
            data_write = body_write.get("data") or {}
            # 回执三态的第 ① 态（真写了）：`sources[push-config-audit]` 必须是
            # `{ok:true, rows:1, detail:null}`。§九 的三态判据当时只在 `%TEMP%` 探针里，
            # 这里并进 V-10（**不新增判据号**），让「三态」在仓库脚本里也有读数。
            audit_write = {item.get("source"): item
                           for item in (body_write.get("sources") or [])
                           }.get("push-config-audit") or {}
            _status_back, overview_after = http_json(f"{base}/api/push/overview?days=30")
            live_after = {item.get("key"): item for item in
                          (overview_after.get("data") or {}).get("config") or []}
            status_restore, body_restore = http_json(
                f"{base}/api/push/config", method="POST",
                body={"key": "push.after_tool_every_n_calls", "value": 10})
            _status_back2, overview_restored = http_json(f"{base}/api/push/overview?days=30")
            live_restored = {item.get("key"): item for item in
                             (overview_restored.get("data") or {}).get("config") or []}
            write_ok = (
                status_write == 200 and data_write.get("new") == 20
                and data_write.get("old") == 10
                and data_write.get("source") == "workbench"
                and data_write.get("auditAt")
                # 回执三态 ①：真写了 → 审计项 ok=true / rows=1 / detail=null
                and audit_write.get("ok") is True
                and audit_write.get("rows") == 1
                and audit_write.get("detail") is None
                # **重新 GET 读回**才算生效：不信写口自己的回执
                and (live_after.get("push.after_tool_every_n_calls") or {}).get("value") == 20
                and (live_after.get("push.after_tool_every_n_calls") or {}).get("source") == "db"
                and status_restore == 200
                and (live_restored.get("push.after_tool_every_n_calls") or {}).get("value") == 10
            )
            record("V-10 写 20 → 200，GET 读回 20，再写回 10（含回执审计项 ok/rows=1/detail=null）",
                   write_ok,
                   json.dumps({"write": [status_write, data_write.get("old"),
                                         data_write.get("new")],
                               "receiptAudit": audit_write,
                               "readBack": (live_after.get("push.after_tool_every_n_calls")
                                            or {}).get("value"),
                               "readBackSource": (live_after.get("push.after_tool_every_n_calls")
                                                  or {}).get("source"),
                               "restored": (live_restored.get("push.after_tool_every_n_calls")
                                            or {}).get("value")}, ensure_ascii=False))

            audit_lines = _audit_lines(copy_path)
            new_lines = audit_lines[before_audit:]
            parsed = []
            bad = []
            for line in new_lines:
                try:
                    parsed.append(json.loads(line))
                except ValueError:
                    bad.append(line[:120])
            audit_ok = (len(new_lines) >= 2 and not bad
                        and all(set(("ts", "actor", "key", "old", "new")) <= set(item)
                                for item in parsed)
                        and all(item.get("actor") == "workbench" for item in parsed)
                        and {item.get("new") for item in parsed} >= {20, 10})
            record("V-11 审计：每行 ts/actor/key/old/new，且两次写入都在（≥2 行）",
                   audit_ok,
                   json.dumps({"auditPath": _audit_path(copy_path),
                               "newLines": len(new_lines),
                               "parsed": [(item.get("key"), item.get("old"), item.get("new"))
                                          for item in parsed],
                               "bad": bad}, ensure_ascii=False))

            before_preset = len(_audit_lines(copy_path))
            status_pause, body_pause = http_json(
                f"{base}/api/push/preset", method="POST", body={"preset": "pause"})
            _st, ov_pause = http_json(f"{base}/api/push/overview?days=30")
            paused = {item.get("key"): item.get("value") for item in
                      (ov_pause.get("data") or {}).get("config") or []}
            status_resume, body_resume = http_json(
                f"{base}/api/push/preset", method="POST", body={"preset": "resume"})
            _st, ov_resume = http_json(f"{base}/api/push/overview?days=30")
            resumed = {item.get("key"): item.get("value") for item in
                       (ov_resume.get("data") or {}).get("config") or []}
            preset_ok = (
                status_pause == 200
                and paused.get("push.after_tool_every_n_calls") == 9999
                and paused.get("push.prompt_enabled") is False
                and paused.get("push.open_budget_tokens") == 0
                and status_resume == 200
                and resumed.get("push.after_tool_every_n_calls") == 10
                and resumed.get("push.prompt_enabled") is True
                and resumed.get("push.open_budget_tokens") == 450
            )
            record("V-12 预设 pause → 9999/false/0；resume → 10/true/450（GET 读回）",
                   preset_ok,
                   json.dumps({"pause": {"status": status_pause,
                                         "values": [paused.get("push.after_tool_every_n_calls"),
                                                    paused.get("push.prompt_enabled"),
                                                    paused.get("push.open_budget_tokens")],
                                         "changed": (body_pause.get("data") or {}).get("changedCount")},
                               "resume": {"status": status_resume,
                                          "values": [resumed.get("push.after_tool_every_n_calls"),
                                                     resumed.get("push.prompt_enabled"),
                                                     resumed.get("push.open_budget_tokens")],
                                          "changed": (body_resume.get("data") or {}).get("changedCount")}},
                              ensure_ascii=False))

            preset_lines = [json.loads(line) for line in _audit_lines(copy_path)[before_preset:]
                            if line.strip()]
            tagged = [item for item in preset_lines if item.get("preset")]
            record("V-13 预设审计：逐键一行且带 preset 字段（pause/resume 各 ≥1 行）",
                   len(tagged) >= 2
                   and {item.get("preset") for item in tagged} >= {"pause", "resume"}
                   and all(set(("ts", "actor", "key", "old", "new", "preset")) <= set(item)
                           for item in tagged),
                   json.dumps({"lines": len(preset_lines), "tagged": len(tagged),
                               "presets": sorted({item.get("preset") for item in tagged}),
                               "keys": [item.get("key") for item in tagged]},
                              ensure_ascii=False))

            # ── V-28（评审 🟡-1）：并发写同一个键 → 审计链 old == 上一行 new ──────
            try:
                import fish_push_audit as AUDIT           # 直调数据面：锁就在这一层

                conc_dir = os.path.join(work, "v28", "base")
                os.makedirs(conc_dir, exist_ok=True)
                conc_db = os.path.join(conc_dir, "conc.sqlite")
                copy_db(prod_db, conc_db)
                fixed = _concurrent_audit_chain(AUDIT, conc_db,
                                                threads=CONC_THREADS, rounds=CONC_ROUNDS)
                # 变异：把写口的串行闸去掉 → 同一场景**必须断链**（证明 V-28 的基线有判别力）
                lock_dir = os.path.join(work, "v28", "mut")
                os.makedirs(lock_dir, exist_ok=True)
                lock_db = os.path.join(lock_dir, "mut.sqlite")
                copy_db(prod_db, lock_db)
                lock_patch = os.path.join(work, "patch-lock")
                os.makedirs(lock_patch, exist_ok=True)
                lock_meta = _patch_module(api_src, lock_patch, lock_db, 0,
                                          [(LOCK_ANCHOR, LOCK_MUTANT)])
                mut_module = _load_patched_module(lock_meta["patched"],
                                                  "fish_push_audit_mut_lock")
                mutated = _concurrent_audit_chain(mut_module, lock_db,
                                                  threads=CONC_THREADS, rounds=CONC_ROUNDS)
                record("V-28 6 线程并发写同一键：审计链 old == 上一行 new"
                       "（去掉串行闸必断链）",
                       fixed["chainOk"] and not mutated["chainOk"],
                       json.dumps({"fixed": fixed, "mutated": mutated,
                                   "threads": CONC_THREADS, "rounds": CONC_ROUNDS,
                                   "anchorHits": lock_meta["hits"][0],
                                   "mutantDir": lock_patch}, ensure_ascii=False))
            except RuntimeError as exc:
                record("V-28 并发写审计链", "SKIP", f"起不来：{exc}")

            # ── V-29（评审 🟡-3）：非回环监听时写口 403，读口照常；加 flag 才 200 ──
            v29_dir = os.path.join(work, "v29")
            os.makedirs(v29_dir, exist_ok=True)
            gate_db = os.path.join(v29_dir, "gate.sqlite")
            copy_db(prod_db, gate_db)
            open_db = os.path.join(v29_dir, "open.sqlite")
            copy_db(prod_db, open_db)
            gate_log = os.path.join(v29_dir, "gate.stderr.log")
            open_log = os.path.join(v29_dir, "open.stderr.log")
            gate_before = _config_value(gate_db, "push.open_budget_tokens")
            gate_srv = None
            open_srv = None
            try:
                gate_port = _free_port()
                gate_srv = start_api(gate_db, gate_port, host="0.0.0.0", log_path=gate_log)
                gate_base = f"http://127.0.0.1:{gate_port}"
                st_gate_cfg, body_gate_cfg = http_json(
                    f"{gate_base}/api/push/config", method="POST",
                    body={"key": "push.open_budget_tokens", "value": 300})
                st_gate_preset, body_gate_preset = http_json(
                    f"{gate_base}/api/push/preset", method="POST", body={"preset": "pause"})
                st_gate_read, _body_gate_read = http_json(
                    f"{gate_base}/api/push/overview?days=7")
                gate_after = _config_value(gate_db, "push.open_budget_tokens")

                open_port = _free_port()
                open_srv = start_api(open_db, open_port, host="0.0.0.0",
                                     allow_remote_write=True, log_path=open_log)
                open_base = f"http://127.0.0.1:{open_port}"
                st_open_cfg, body_open_cfg = http_json(
                    f"{open_base}/api/push/config", method="POST",
                    body={"key": "push.open_budget_tokens", "value": 300})
                open_data = (body_open_cfg or {}).get("data") or {}
                open_value = _config_value(open_db, "push.open_budget_tokens")
                gate_text = _read_text(gate_log)
                open_text = _read_text(open_log)
                gate_ok = (
                    st_gate_cfg == 403
                    and (body_gate_cfg or {}).get("code") == "write_requires_loopback"
                    and st_gate_preset == 403
                    and (body_gate_preset or {}).get("code") == "write_requires_loopback"
                    and st_gate_read == 200                    # 读端点照常
                    and gate_after == gate_before              # 库一个字节没被写
                    and "write_requires_loopback" in gate_text  # 启动日志大声警告
                    and "非回环" in gate_text
                )
                open_ok = (
                    st_open_cfg == 200
                    and open_data.get("changed") is True
                    and open_value == 300                      # 加了 flag 就真的写进去了
                    and "--allow-remote-write" in open_text
                )
                record("V-29 非回环（--host 0.0.0.0）写口 403 write_requires_loopback、读口 200；"
                       "加 --allow-remote-write 才 200 且真写库",
                       gate_ok and open_ok,
                       json.dumps({"gate": {"config": [st_gate_cfg, (body_gate_cfg or {}).get("code")],
                                            "preset": [st_gate_preset,
                                                       (body_gate_preset or {}).get("code")],
                                            "read": st_gate_read,
                                            "dbUnchanged": gate_after == gate_before,
                                            "logHasWarning": "非回环" in gate_text},
                                   "open": {"config": st_open_cfg,
                                            "changed": open_data.get("changed"),
                                            "dbValue": open_value,
                                            "logHasFlag": "--allow-remote-write" in open_text},
                                   "hint": (body_gate_cfg or {}).get("hint")}, ensure_ascii=False))
            except RuntimeError as exc:
                record("V-29 非回环写口闸", "SKIP", f"起不来：{exc}")
            finally:
                for _proc in (gate_srv, open_srv):
                    if _proc is not None:
                        ACC._kill_tree(_proc)

            # ── V-30（评审 🟡-2）：命中缓存时 asOf 仍是「数据时刻」，年龄单列 ──
            st_c1, env_c1 = http_json(f"{base}/api/push/overview?days=7")
            data_c1 = (env_c1 or {}).get("data") or {}
            time.sleep(1.3)                      # 让缓存年龄真的过 1 秒（TTL 15 s，仍在缓存里）
            st_c2, env_c2 = http_json(f"{base}/api/push/overview?days=7")
            data_c2 = (env_c2 or {}).get("data") or {}
            cache_ok = (
                st_c1 == 200 and st_c2 == 200
                and (data_c1.get("cacheAgeSec") or 0) <= 0.5           # 第一份是刚算的
                and data_c1.get("asOf") == data_c1.get("cachedAt")
                and data_c2.get("asOf") == data_c1.get("asOf")         # 仍是那个数据时刻
                and data_c2.get("cachedAt") == data_c2.get("asOf")
                and (data_c2.get("cacheAgeSec") or 0) >= 1.0           # 但年龄长大了
                and (env_c2 or {}).get("asOf") == data_c2.get("asOf")   # 信封也不冒充此刻
            )
            record("V-30 overview 命中缓存：asOf 是数据时刻 + cacheAgeSec 变大（不拿此刻冒充）",
                   cache_ok,
                   json.dumps({"first": {"asOf": data_c1.get("asOf"),
                                         "cachedAt": data_c1.get("cachedAt"),
                                         "cacheAgeSec": data_c1.get("cacheAgeSec")},
                               "second": {"asOf": data_c2.get("asOf"),
                                          "cachedAt": data_c2.get("cachedAt"),
                                          "cacheAgeSec": data_c2.get("cacheAgeSec")},
                               "envelopeAsOf": (env_c2 or {}).get("asOf")},
                              ensure_ascii=False))

            # ── C. 变异自证（白名单必须真的在挡）──────────────────────
            # 每份副本放**自己的目录**：审计文件按 `<库所在目录>/_audit/` 定位，
            # 都塞在 `work/` 下的话几份副本会共享同一个审计文件，读数就分不清是谁写的了。
            mutate_copy = os.path.join(work, "v14-mut", "mutate.sqlite")
            os.makedirs(os.path.dirname(mutate_copy), exist_ok=True)
            copy_db(prod_db, mutate_copy)
            patch_dir = os.path.join(work, "patched")
            os.makedirs(patch_dir, exist_ok=True)
            mutate_port = _free_port()
            mutate_meta = build_mutation(os.path.join(_P0, "fish_push_audit.py"),
                                         patch_dir, mutate_copy, mutate_port)
            mutate_base = f"http://127.0.0.1:{mutate_port}"
            try:
                mutate_server = start_api(mutate_copy, mutate_port, boot_dir=patch_dir)
                status_mut, body_mut = http_json(
                    f"{mutate_base}/api/push/config", method="POST",
                    body={"key": REGISTERED_NOT_WRITABLE, "value": 0.2})
                data_mut = body_mut.get("data") or {}
                # **直接读变异服务那份库**证明它真写进去了（不是假 200）。
                # 为什么不用 /api/push/overview 读回：那个契约只列 7 个可写 + 6 个只读键，
                # 靶子 `push.serendipity_ratio` 不在其中，用 overview 读回会永远读成 None。
                live_mut = _config_value(mutate_copy, REGISTERED_NOT_WRITABLE)
                mutation_ok = (
                    status_registered == 400            # 未变异：白名单在挡
                    and status_mut == 200               # 变异后：同一请求被接受
                    and data_mut.get("new") == 0.2
                    and live_mut == 0.2
                )
                record("V-14 变异自证：白名单换成放行 → 同一请求 400 → 200（且真写进库）",
                       mutation_ok,
                       json.dumps({"anchorHits": mutate_meta["hits"],
                                   "baselineStatus": status_registered,
                                   "mutatedStatus": status_mut,
                                   "mutatedDbValue": live_mut,
                                   "patchedDir": patch_dir}, ensure_ascii=False))
            except RuntimeError as exc:
                record("V-14 变异自证", "SKIP", f"变异服务起不来：{exc}")
            finally:
                if mutate_server is not None:
                    ACC._kill_tree(mutate_server)
                    mutate_server = None

            # ── 生产库与源码：跑了一场写测试之后必须一个字节都没变 ────
            record("V-15 仓库源码未被改动（sha256 前后一致）",
                   sha256(api_src) == src_sha,
                   json.dumps({"before": src_sha, "after": sha256(api_src),
                               "mutationWroteTo": patch_dir}, ensure_ascii=False))
        finally:
            conn.close()

        # ── DOM 面（1280 不溢出 + 六块 + 截图）────────────────────────
        # `probe` 在这里先置 None：skip-dom / 没 dist / 浏览器起不来时，后面 V-32/V-33
        # 要用同一个变量判「没验界面」，不能让 `probe` 处在「没定义」的状态（NameError 会被
        # 外层当程序崩溃，而不是判 SKIP）。
        probe = None
        if args.skip_dom:
            for cid in ("V-16", "V-17", "V-18", "V-26"):
                record(cid, "SKIP", "--skip-dom：本次没验界面")
        elif not os.path.exists(dist_index):
            for cid in ("V-16", "V-17", "V-18", "V-26"):
                record(cid, "SKIP", "缺 ui/dist：先 cd ui && npm run build")
        else:
            url = f"{base}/?page=settings&sub=push"
            if not os.path.isdir(os.path.dirname(SHOT_PATH)):
                os.makedirs(os.path.dirname(SHOT_PATH), exist_ok=True)
            try:
                probe = render(url, DOM_PROBES, width=1280, height=720, wait_ms=8000,
                               shot_path=SHOT_PATH, resize_to=(1440, 900))
            except RuntimeError as exc:
                for cid in ("V-16", "V-17", "V-18", "V-26"):
                    record(cid, "SKIP", f"无头浏览器起不来：{exc}")
                probe = None
            if probe is not None:
                labels = probe.get("navLabels") or []
                nav_ok = (probe.get("navItems") == 2
                          and any("Agent 接入" in (label or "") for label in labels)
                          and any("推送" in (label or "") for label in labels)
                          and probe.get("pushPage") == 1)
                record("V-16 DOM：设置二级导航 2 项（Agent 接入 / 推送）+ 推送页已渲染",
                       nav_ok,
                       json.dumps({"navItems": probe.get("navItems"), "labels": labels,
                                   "pushPage": probe.get("pushPage"),
                                   "url": url}, ensure_ascii=False))

                blocks = probe.get("blocks") or []
                need = ["toolbar", "diag", "channels", "top", "adjust", "events", "notes"]
                text = probe.get("text") or ""
                texts = ["诊断提要", "通道", "Top offenders", "调整", "最近推送", "口径"]
                blocks_ok = (
                    all(name in blocks for name in need)
                    and probe.get("title") == "推送"
                    and probe.get("refresh") == 1
                    and probe.get("metrics") == 4
                    and probe.get("configRows") == 7
                    and probe.get("readonlyRows") == 6
                    and probe.get("presetButtons") == 2
                    and (probe.get("eventRows") or 0) >= 1
                    and (probe.get("channelRows") or 0) >= 3
                    and all(word in text for word in texts)
                )
                record("V-17 DOM：标题 + 六块 + 刷新 + 4 指标卡 + 7 可写 + 6 只读 + 预设 2 键",
                       blocks_ok,
                       json.dumps({"title": probe.get("title"), "blocks": blocks,
                                   "refresh": probe.get("refresh"),
                                   "metrics": probe.get("metrics"),
                                   "configRows": probe.get("configRows"),
                                   "readonlyRows": probe.get("readonlyRows"),
                                   "presetButtons": probe.get("presetButtons"),
                                   "eventRows": probe.get("eventRows"),
                                   "channelRows": probe.get("channelRows"),
                                   "missingText": [word for word in texts if word not in text]},
                                  ensure_ascii=False))

                sw, cw = probe.get("scrollWidth"), probe.get("clientWidth")
                bsw = probe.get("bodyScrollWidth")
                wide_sw, wide_cw = probe.get("wide_scrollWidth"), probe.get("wide_clientWidth")
                overflow_ok = (
                    isinstance(sw, int) and isinstance(cw, int) and sw == cw
                    and isinstance(bsw, int) and bsw <= cw
                    and isinstance(wide_sw, int) and isinstance(wide_cw, int)
                    and wide_sw == wide_cw
                )
                record("V-18 1280×720 不横向溢出（1440×900 同测）",
                       overflow_ok,
                       json.dumps({"1280": [sw, cw], "bodyScrollWidth": bsw,
                                   "1440": [wide_sw, wide_cw]},
                                  ensure_ascii=False))

            # ── V-26（评审 🟠-1）：Agent 接入 1280×720 —— 详情可滚 + 刷新够得着 ──────
            # 修前：`Settings.tsx` 的容器是块级 + `overflow-hidden`，`AgentAccess` 根用
            # `flex … flex-1`，在块级父级下高度退化成内容高度 → section 不滚、被裁，
            # 刷新按钮落在 720 之外。修法 = 容器 `flex flex-col` + 根 `h-full`。
            agent_url = f"{base}/?page=settings"
            agent_shot = SHOT_AGENT_PATH
            try:
                os.makedirs(os.path.dirname(agent_shot), exist_ok=True)
                agent_probe = render(agent_url, AGENT_DOM_PROBES, width=1280, height=720,
                                     wait_ms=8000, shot_path=agent_shot)
                fixed_ok, fixed_detail = _agent_layout_ok(agent_probe)
                # 变异自证：把 `ui/dist` **副本**里两处布局修法一起还原成修前写法
                # （子页容器退回块级 + 根去掉 `h-full`）→ 同一判据必须红。
                mut_dist_dir = os.path.join(work, "dist-mut")
                mut_meta = _mutate_dist(DIST_DIR, mut_dist_dir)
                mut_port = _free_port()
                mut_dist_srv = start_api(copy_path, mut_port, dist=mut_dist_dir)
                extra_servers.append(mut_dist_srv)
                mut_probe = render(f"http://127.0.0.1:{mut_port}/?page=settings",
                                   AGENT_DOM_PROBES, width=1280, height=720, wait_ms=8000)
                mut_ok, mut_detail = _agent_layout_ok(mut_probe)
                record("V-26 Agent 接入 1280×720：详情可滚 + 刷新按钮 bottom≤720 且命中"
                       "（还原成修前布局的产物副本必红）",
                       fixed_ok and not mut_ok,
                       json.dumps({"fixed": fixed_detail, "mutated": mut_detail,
                                   "anchorHits": mut_meta["hits"],
                                   "mutatedDist": mut_dist_dir,
                                   "shot": ({"path": agent_shot,
                                             "bytes": os.path.getsize(agent_shot)}
                                            if os.path.exists(agent_shot) else None)},
                                  ensure_ascii=False))
            except RuntimeError as exc:
                record("V-26 Agent 接入 1280×720", "SKIP", f"起不来：{exc}")

        # ── D. F1/F2 回归：QA 第 1 轮打回的两条根因（V-21~V-25）────────
        # F1 的坏输入有两种，都必须 200 且 `sum(channels.total) == totals.total`：
        #   ① 空库（`push_log` 清空）② 单通道缺失（删掉 `channel='inject'` 的行）。
        # 两次写入都落在 `%TEMP%` 的副本上，生产库不参与。
        f1_dir = os.path.join(work, "f1")
        os.makedirs(f1_dir, exist_ok=True)
        f1_empty = os.path.join(f1_dir, "empty.sqlite")
        copy_db(prod_db, f1_empty)
        f1_deleted_all = exec_write(f1_empty, "DELETE FROM push_log")
        f1_one = os.path.join(f1_dir, "one-channel.sqlite")
        copy_db(prod_db, f1_one)
        f1_deleted_inject = exec_write(f1_one, "DELETE FROM push_log WHERE channel='inject'")
        # 坏输入必须先证明「真的造出来了」：删除 0 行的话判据是在一颗好树上跑。
        f1_inputs_ok = (
            f1_deleted_all > 0
            and f1_deleted_inject > 0
            and count_rows(f1_empty, "SELECT COUNT(*) FROM push_log") == 0
            and count_rows(f1_one, "SELECT COUNT(*) FROM push_log WHERE channel='inject'") == 0)

        def _overview_windows(base_url: str) -> dict:
            """两个窗口各取一次（days=30 与 days=7 走的是同一条聚合路径）。"""
            out = {}
            for days in (30, 7):
                status, env = http_json(f"{base_url}/api/push/overview?days={days}")
                data = (env or {}).get("data") or {}
                channels = data.get("channels") or []
                out[days] = {
                    "status": status, "data": data, "channels": channels,
                    "sum": sum(int(item.get("total") or 0) for item in channels),
                    "total": (data.get("totals") or {}).get("total"),
                    "names": [item.get("channel") for item in channels],
                }
            return out

        empty_srv = one_srv = mut_srv = None
        # V-31 要用这两个读数（空库 / 单通道的 breakdown 和）：先给空 dict 兜底，
        # 起不来时它 `[30]` 会 ValueError/KeyError → V-31 记 SKIP，而不是 NameError 崩脚本。
        empty_windows: dict = {}
        one_windows: dict = {}
        try:
            empty_port = _free_port()
            empty_base = f"http://127.0.0.1:{empty_port}"
            empty_srv = start_api(f1_empty, empty_port)
            one_port = _free_port()
            one_base = f"http://127.0.0.1:{one_port}"
            one_srv = start_api(f1_one, one_port)

            empty_windows = _overview_windows(empty_base)
            empty_ok = (
                f1_inputs_ok
                and all(item["status"] == 200 for item in empty_windows.values())
                and all(item["total"] == 0 for item in empty_windows.values())
                and all(item["sum"] == 0 for item in empty_windows.values())
                and all(item["names"] == ["inject", "prompt", "tool_after"]
                        for item in empty_windows.values())
                and all(all(int(ch.get("total") or 0) == 0 for ch in item["channels"])
                        for item in empty_windows.values())
            )
            record("V-21 F1 空库：overview?days=30|7 都 200，total=0 且三通道都为 0",
                   empty_ok,
                   json.dumps({"inputs": {"deletedPushLog": f1_deleted_all,
                                          "pushLogRowsNow": count_rows(
                                              f1_empty, "SELECT COUNT(*) FROM push_log")},
                               "windows": {days: {"status": item["status"],
                                                  "total": item["total"],
                                                  "sumChannels": item["sum"],
                                                  "channels": [(ch.get("channel"),
                                                                ch.get("total"),
                                                                ch.get("accepted"),
                                                                ch.get("ignored"))
                                                               for ch in item["channels"]]}
                                           for days, item in empty_windows.items()}},
                              ensure_ascii=False))

            one_windows = _overview_windows(one_base)
            one_ok = (
                f1_inputs_ok
                and all(item["status"] == 200 for item in one_windows.values())
                and all(item["sum"] == item["total"] for item in one_windows.values())
                and all(item["total"] > 0 for item in one_windows.values())
                and all(item["names"] == ["inject", "prompt", "tool_after"]
                        for item in one_windows.values())
                and all(int(next(ch for ch in item["channels"]
                                 if ch.get("channel") == "inject").get("total") or 0) == 0
                        for item in one_windows.values())
            )
            record("V-22 F1 单通道缺失（删 inject）：overview 200 且 sum(channels)==totals.total",
                   one_ok,
                   json.dumps({"inputs": {"deletedInject": f1_deleted_inject,
                                          "injectRowsNow": count_rows(
                                              f1_one,
                                              "SELECT COUNT(*) FROM push_log "
                                              "WHERE channel='inject'")},
                               "windows": {days: {"status": item["status"],
                                                  "total": item["total"],
                                                  "sumChannels": item["sum"],
                                                  "channels": [(ch.get("channel"),
                                                                ch.get("total"),
                                                                ch.get("accepted"),
                                                                ch.get("ignored"))
                                                               for ch in item["channels"]]}
                                           for days, item in one_windows.items()}},
                              ensure_ascii=False))

            # V-23：判别力自证——把 F1 的两行还原成修前写法，**同一份空库必须 500**。
            # （补丁锚点没命中时 `_patch_module` 会抛；抛了记 SKIP，不装 PASS。）
            try:
                f1_patch = os.path.join(work, "patch-f1")
                os.makedirs(f1_patch, exist_ok=True)
                mut_port = _free_port()
                _patch_module(api_src, f1_patch, f1_empty, mut_port,
                              [(F1_ANCHOR, F1_MUTANT)])
                mut_srv = start_api(f1_empty, mut_port, boot_dir=f1_patch)
                mut_status, mut_body = http_json(
                    f"http://127.0.0.1:{mut_port}/api/push/overview?days=30")
                mut_hint = str(mut_body.get("hint") or "")
                record("V-23 F1 变异自证：还原 `(row or {})[...]` → 同一空库 500 KeyError"
                       "（证明 V-21/V-22 有判别力）",
                       mut_status == 500 and mut_body.get("code") == "internal_error"
                       and "KeyError" in mut_hint and "accepted" in mut_hint,
                       json.dumps({"sameDb": f1_empty,
                                   "fixedStatus": empty_windows[30]["status"],
                                   "mutatedStatus": mut_status,
                                   "mutatedCode": mut_body.get("code"),
                                   "mutatedHint": mut_hint,
                                   "patchedDir": f1_patch}, ensure_ascii=False))
            except RuntimeError as exc:
                record("V-23 F1 变异自证", "SKIP", f"变异服务起不来：{exc}")
        finally:
            # F1 的三只坏输入读口用完就杀（它们不参与后面的判据）。
            for _proc in (empty_srv, one_srv, mut_srv):
                if _proc is not None:
                    ACC._kill_tree(_proc)

        # ── F2：同值 POST 不写库、不记审计（文档 §三）──────────────────
        # 用**现场值**而不是写死的常量：值取自 GET，判据才不会因为默认值变了而空转。
        f2_key = "push.prompt_max_items"
        _st, ov_before = http_json(f"{base}/api/push/overview?days=30")
        live_before = {item.get("key"): item for item in
                       (ov_before.get("data") or {}).get("config") or []}
        f2_value = (live_before.get(f2_key) or {}).get("value")
        f2_audit_before = len(_audit_lines(copy_path))
        f2_events_before = count_rows(
            copy_path, "SELECT COUNT(*) FROM events WHERE type='config.changed'")
        f2_row_before = _config_row(copy_path, f2_key)
        status_noop, body_noop = (None, {})
        if f2_value is None:
            record("V-24 F2 同值 POST：200、changed=False、不写库不记审计", False,
                   f"判据自己失效：{f2_key} 不在 overview.config 里，拿不到现场值")
        else:
            status_noop, body_noop = http_json(
                f"{base}/api/push/config", method="POST",
                body={"key": f2_key, "value": f2_value})
            data_noop = body_noop.get("data") or {}
            # 回执三态的第 ② 态（没改）：审计项必须是
            # `{ok:true, rows:0, detail:"值未变化，未写库、未写审计"}`——「没变」不是失败。
            audit_noop = {item.get("source"): item
                          for item in (body_noop.get("sources") or [])
                          }.get("push-config-audit") or {}
            f2_audit_after = len(_audit_lines(copy_path))
            f2_events_after = count_rows(
                copy_path, "SELECT COUNT(*) FROM events WHERE type='config.changed'")
            f2_row_after = _config_row(copy_path, f2_key)
            _st, ov_after_noop = http_json(f"{base}/api/push/overview?days=30")
            live_after_noop = {item.get("key"): item for item in
                               (ov_after_noop.get("data") or {}).get("config") or []}
            noop_ok = (
                status_noop == 200
                and data_noop.get("key") == f2_key
                and data_noop.get("changed") is False
                and data_noop.get("old") == f2_value
                and data_noop.get("new") == f2_value
                and data_noop.get("auditAt") is None
                # 回执三态 ②：没变 → ok=true / rows=0 / detail 说明「没写库也没写审计」
                and audit_noop.get("ok") is True
                and audit_noop.get("rows") == 0
                and "未变化" in (audit_noop.get("detail") or "")
                and f2_audit_after == f2_audit_before          # 审计没多一行
                and f2_events_after == f2_events_before        # 没有 config.changed 事件
                and f2_row_after == f2_row_before              # config 那行没被碰过
                and (live_after_noop.get(f2_key) or {}).get("value") == f2_value
            )
            record("V-24 F2 同值 POST：200、changed=False、不写库不记审计"
                   "（回执审计项 ok=true/rows=0/detail）",
                   noop_ok,
                   json.dumps({"key": f2_key, "value": f2_value, "status": status_noop,
                               "changed": data_noop.get("changed"),
                               "auditAt": data_noop.get("auditAt"),
                               "receiptAudit": audit_noop,
                               "auditLines": [f2_audit_before, f2_audit_after],
                               "configChangedEvents": [f2_events_before, f2_events_after],
                               "configRow": [list(f2_row_before), list(f2_row_after)],
                               "readBack": (live_after_noop.get(f2_key) or {}).get("value")},
                              ensure_ascii=False))

            # V-25：判别力自证——关掉那道短路，同一请求必须重新写库 + 记审计。
            try:
                f2_copy = os.path.join(work, "f2-mut", "mut.sqlite")
                os.makedirs(os.path.dirname(f2_copy), exist_ok=True)
                copy_db(prod_db, f2_copy)
                f2_patch = os.path.join(work, "patch-f2")
                os.makedirs(f2_patch, exist_ok=True)
                f2_port = _free_port()
                _patch_module(api_src, f2_patch, f2_copy, f2_port, [(F2_ANCHOR, F2_MUTANT)])
                f2_srv = start_api(f2_copy, f2_port, boot_dir=f2_patch)
                extra_servers.append(f2_srv)
                f2_base = f"http://127.0.0.1:{f2_port}"
                # 现场值取**变异服务自己那份库**的读数：两份副本互相独立，拿主副本的值套
                # 上去的话，一旦两边值不同，变异版会因为「值真的变了」多写一行——V-25 会假绿。
                _mst, mut_ov = http_json(f"{f2_base}/api/push/overview?days=30")
                mut_live = {item.get("key"): item for item in
                            (mut_ov.get("data") or {}).get("config") or []}
                mut_value = (mut_live.get(f2_key) or {}).get("value")
                if mut_value is None:
                    raise RuntimeError(f"变异副本里拿不到 {f2_key} 的现场值，判不了「同值」")
                mut_audit_before = len(_audit_lines(f2_copy))
                mut_events_before = count_rows(
                    f2_copy, "SELECT COUNT(*) FROM events WHERE type='config.changed'")
                mut_w_status, mut_w_body = http_json(
                    f"{f2_base}/api/push/config", method="POST",
                    body={"key": f2_key, "value": mut_value})
                mut_audit_after = len(_audit_lines(f2_copy))
                mut_events_after = count_rows(
                    f2_copy, "SELECT COUNT(*) FROM events WHERE type='config.changed'")
                mut_w_data = mut_w_body.get("data") or {}
                record("V-25 F2 变异自证：关掉同值短路 → 同一请求写审计 +1 且"
                       " config.changed +1（证明 V-24 有判别力）",
                       mut_w_status == 200 and mut_w_data.get("changed") is True
                       and mut_audit_after == mut_audit_before + 1
                       and mut_events_after == mut_events_before + 1,
                       json.dumps({"key": f2_key, "value": mut_value,
                                   "status": mut_w_status,
                                   "changed": mut_w_data.get("changed"),
                                   "auditLines": [mut_audit_before, mut_audit_after],
                                   "configChangedEvents": [mut_events_before, mut_events_after],
                                   "patchedDir": f2_patch}, ensure_ascii=False))
            except RuntimeError as exc:
                record("V-25 F2 变异自证", "SKIP", f"变异服务起不来：{exc}")

        # ── F. 二级拆解（V-31~V-33）──────────────────────────────────────
        # V-31 三条独立路径对拍：① API 自述的 breakdown、② 组内行和、③ SQL 直查
        # （`_breakdown_sql_counts` 自己数一遍——任务书要求「不许只信 API 自述」）。
        v31_ok = False
        v31_detail: dict = {}
        try:
            st31, env31 = http_json(f"{base}/api/push/overview?days=30")
            v31_data = (env31 or {}).get("data") or {}
            since31 = (v31_data.get("window") or {}).get("since") or ""
            sql31 = _breakdown_sql_counts(copy_path, since31)
            bd_ok, v31_detail = _breakdown_checks(v31_data, sql31)

            empty_since = ((empty_windows[30]["data"].get("window") or {}).get("since") or "")
            empty_ok, empty_detail = _breakdown_checks(
                empty_windows[30]["data"], _breakdown_sql_counts(f1_empty, empty_since))
            one_since = ((one_windows[30]["data"].get("window") or {}).get("since") or "")
            one_ok, one_detail = _breakdown_checks(
                one_windows[30]["data"], _breakdown_sql_counts(f1_one, one_since))
            # 空库还要**显式**全 0：「checks 全过」在 0 行时是必然，不足以证明入口没坏掉。
            empty_zeros = (all(int(value) == 0 for value in empty_detail["totals"].values())
                           and all(count == 0
                                   for count in empty_detail["groupCounts"].values()))
            # 单通道也要显式「还有量」：否则在空库上跑也会绿（同一个道理的另一半）。
            one_nonzero = (one_detail["totals"]["memory"] > 0
                           or one_detail["totals"]["kb"] > 0)
            v31_ok = (st31 == 200
                      and empty_windows[30]["status"] == 200
                      and one_windows[30]["status"] == 200
                      and isinstance(v31_data.get("breakdown"), dict)
                      and bd_ok and empty_ok and empty_zeros and one_ok and one_nonzero)
            record("V-31 breakdown 口径和：memory==byType/byProject/byChannel/kinds 四路之和、"
                   "每组行和==组 total、每个 total 都 SQL 直查对拍、摘要五数相加==窗口总数"
                   "（语料已在记忆里，不重复相加）（空库/单通道同样 200 且全 0）",
                   v31_ok,
                   json.dumps({"status": st31, "since": since31,
                               "checks": {name: value for name, value in v31_detail.items()
                                          if isinstance(value, bool)},
                               "totals": v31_detail.get("totals"),
                               "summary": v31_detail.get("summary"),
                               "groupCounts": v31_detail.get("groupCounts"),
                               "rowProblems": v31_detail.get("rowProblems"),
                               "sql": sql31,
                               "empty": {"status": empty_windows[30]["status"],
                                         "checksPass": empty_ok, "zeros": empty_zeros,
                                         "totals": empty_detail["totals"],
                                         "groupCounts": empty_detail["groupCounts"]},
                               "oneChannel": {"status": one_windows[30]["status"],
                                              "checksPass": one_ok, "nonzero": one_nonzero,
                                              "totals": one_detail["totals"]}},
                              ensure_ascii=False))
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            record("V-31 breakdown 口径和", "SKIP", f"读数不完整，判不了：{exc!r}")

        # V-32 的 DOM 读数来自上面那次 1280×720 渲染（`probe`；`--skip-dom` 时为 None）。
        v32_ok = False
        if probe is None:
            record("V-32 DOM：二级拆解区可见 + 两栏 + 9 个子组都有 data-breakdown-key",
                   "SKIP", "--skip-dom / 缺 dist / 浏览器起不来：本次没验界面")
        else:
            dom_ok, dom_detail = _breakdown_dom_ok(probe)
            sw, cw = probe.get("scrollWidth"), probe.get("clientWidth")
            overflow_ok = isinstance(sw, int) and isinstance(cw, int) and sw == cw
            shot = ({"path": SHOT_PATH, "bytes": os.path.getsize(SHOT_PATH),
                     "mtime": datetime.fromtimestamp(os.path.getmtime(SHOT_PATH),
                                                     timezone.utc)
                     .strftime("%Y-%m-%dT%H:%M:%SZ")} if os.path.exists(SHOT_PATH) else None)
            v32_ok = dom_ok and overflow_ok
            record("V-32 DOM：二级拆解区在且不 hidden + 内容源/态势源两栏 + 记忆三组/KB 两组/"
                   "看板两组/动作流/git 都有 data-breakdown-key 行 + 1280×720 不横向溢出",
                   v32_ok,
                   json.dumps({"dom": dom_detail,
                               "overflow": {"scrollWidth": sw, "clientWidth": cw},
                               "shot": shot}, ensure_ascii=False))

        # V-33 变异自证：数据面（memory.total +1）→ V-31 的尺子必红；
        # DOM 面（给这一区加 hidden）→ V-32 的尺子必红。基线（v31_ok / v32_ok）是绿的，
        # 所以「恢复后绿」这一半由它们自己证明，不靠一句口头的「已经改回来了」。
        if probe is None or not v31_ok:
            record("V-33 变异自证：memory.total+1 → V-31 必红；DOM 加 hidden → V-32 必红",
                   "SKIP", "基线没绿 / 没验界面：变异没有对照组，判不了（没验 ≠ 通过）")
        else:
            v33_dir = os.path.join(work, "v33-data")
            v33_dist = os.path.join(work, "v33-dist")
            os.makedirs(v33_dir, exist_ok=True)
            data_srv = dom_srv = None
            try:
                # ① 数据面变异：`memory.total` +1 的**副本模块**（仓库源码一个字节不动）
                v33_port = _free_port()
                data_meta = _patch_module(api_src, v33_dir, copy_path, v33_port,
                                          [BREAKDOWN_DATA_MUTATION])
                data_srv = start_api(copy_path, v33_port, boot_dir=v33_dir)
                st33, env33 = http_json(
                    f"http://127.0.0.1:{v33_port}/api/push/overview?days=30")
                mut_data = (env33 or {}).get("data") or {}
                mut_since = (mut_data.get("window") or {}).get("since") or ""
                mut_ok, mut_detail = _breakdown_checks(
                    mut_data, _breakdown_sql_counts(copy_path, mut_since))
                # ② DOM 面变异：产物副本里给二级拆解区加 `hidden`（React → display:none）
                dist_meta = _mutate_dist(DIST_DIR, v33_dist,
                                         mutations=(BREAKDOWN_DIST_MUTATION,))
                dom_port = _free_port()
                dom_srv = start_api(copy_path, dom_port, dist=v33_dist)
                mut_probe = render(f"http://127.0.0.1:{dom_port}/?page=settings&sub=push",
                                   DOM_PROBES, width=1280, height=720, wait_ms=8000)
                mut_dom_ok, mut_dom_detail = _breakdown_dom_ok(mut_probe)
                v33_ok = (not mut_ok) and (not mut_dom_ok)
                record("V-33 变异自证：memory.total+1 → V-31 必红；DOM 加 hidden → V-32 必红"
                       "（基线与恢复后都是绿的）",
                       v33_ok,
                       json.dumps({"baseline": {"v31": v31_ok, "v32": v32_ok},
                                   "dataMutation": {
                                       "status": st33, "checksPass": mut_ok,
                                       "failing": [name for name, value in mut_detail.items()
                                                   if isinstance(value, bool) and not value],
                                       "totals": mut_detail.get("totals"),
                                       "anchorHits": data_meta["hits"][0]},
                                   "domMutation": {"checksPass": mut_dom_ok,
                                                   "detail": mut_dom_detail,
                                                   "anchorHits": dist_meta["hits"]},
                                   "patched": {"module": v33_dir, "dist": v33_dist}},
                                  ensure_ascii=False))
            except RuntimeError as exc:
                record("V-33 变异自证", "SKIP", f"变异服务起不来：{exc}")
            finally:
                for _proc in (data_srv, dom_srv):
                    if _proc is not None:
                        ACC._kill_tree(_proc)
    finally:
        for proc in [server, mutate_server, *extra_servers]:
            if proc is not None:
                ACC._kill_tree(proc)

    # ── 生产库对拍 + 截图（V-19/V-20）────────────────────────────────
    prod_sha_after = sha256(prod_db)
    prod_audit_after = _file_snapshot(prod_audit)
    # ① 库 sha256 前后一致；② 审计文件**这一次**没被动过：
    #    本来没有 → 跑完也不能有；本来就有（别的会话写过）→ 大小/行数/sha256 一个字节不变。
    audit_ok = (prod_audit_after == prod_audit_before
                if prod_audit_before["exists"]
                else prod_audit_after["exists"] is False)
    record("V-19 生产库未被写：sha256 前后一致 + 本次运行没往生产目录写 "
           "_audit/push-config.jsonl（本来没有 → 跑完也没有）",
           prod_sha_after == prod_sha and audit_ok,
           json.dumps({"shaBefore": prod_sha, "shaAfter": prod_sha_after,
                       "prodAuditBefore": prod_audit_before,
                       "prodAuditAfter": prod_audit_after,
                       "prodAuditPreExisting": prod_audit_before["exists"],
                       "auditPathInCopy": _audit_path(copy_path)}, ensure_ascii=False))
    # V-20 加一条「**本次运行之后**写的」：只看大小会把上一次跑留下的旧图当成证据
    # （S6 判的是「晚于 dist」，而 dist 可能是几分钟前建好的）。
    shot_exists = os.path.exists(SHOT_PATH)
    shot_mtime = os.path.getmtime(SHOT_PATH) if shot_exists else None
    shot_fresh = shot_mtime is not None and shot_mtime >= run_started
    dom_possible = (not args.skip_dom) and os.path.exists(dist_index)
    shot_ok = (shot_exists and os.path.getsize(SHOT_PATH) > 10_000
               and (shot_fresh if dom_possible else True))
    record("V-20 截图 08-settings-push.png 落盘且非占位（>10KB）、且在本次运行后写"
           "（非本次则判红）",
           shot_ok,
           json.dumps({"path": SHOT_PATH if shot_exists else None,
                       "bytes": os.path.getsize(SHOT_PATH) if shot_exists else None,
                       "mtime": (datetime.fromtimestamp(shot_mtime, timezone.utc)
                                 .strftime("%Y-%m-%dT%H:%M:%SZ") if shot_exists else None),
                       "runStarted": datetime.fromtimestamp(run_started, timezone.utc)
                       .strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "fresh": shot_fresh, "domPossible": dom_possible},
                      ensure_ascii=False))

    if args.keep:
        print(f"\n# 临时目录保留：{work}")
    else:
        shutil.rmtree(work, ignore_errors=True)

    passed = sum(1 for _cid, status, _detail in RESULTS if status == "PASS")
    skipped = sum(1 for _cid, status, _detail in RESULTS if status == "SKIP")
    failed = sum(1 for _cid, status, _detail in RESULTS if status == "FAIL")
    total = len(RESULTS)
    print(f"\n# 汇总 {passed}/{total} PASS · {failed} FAIL · {skipped} SKIP"
          f"（SKIP 也算不通过：没验 ≠ 通过）")
    for cid, status, detail in RESULTS:
        if status != "PASS":
            print(f"# {status}：{cid}  {detail[:200]}")
    return 0 if failed == 0 and skipped == 0 else 1


def _config_value(db_path: str, key: str):
    """直读库里的一个配置值（写测试的**铁证**；读不到给 None）。"""
    try:
        conn = sqlite3.connect("file:" + db_path.replace("\\", "/") + "?mode=ro", uri=True,
                               timeout=15.0)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not row:
        return None
    try:
        return float(row[0])
    except (TypeError, ValueError):
        return row[0]


def _audit_path(db_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "_audit",
                        "push-config.jsonl")


def _audit_lines(db_path: str) -> list[str]:
    path = _audit_path(db_path)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return [line for line in handle.read().splitlines() if line.strip()]


# ── 坏输入 / 直查（V-21~V-25 用；只碰 `%TEMP%` 的副本）───────────────────
def exec_write(db_path: str, sql: str) -> int:
    """在**副本**上执行一条写 SQL，返回受影响行数（F1 造「空库 / 单通道缺失」用）。"""
    conn = sqlite3.connect(db_path, timeout=15.0)
    try:
        affected = conn.execute(sql).rowcount
        conn.commit()
        return int(affected or 0)
    finally:
        conn.close()


def count_rows(db_path: str, sql: str, args: tuple = ()) -> int:
    """副本上只读计数（用来证明「坏输入真的造出来了」「事件真的没多一条」）。"""
    conn = sqlite3.connect("file:" + db_path.replace("\\", "/") + "?mode=ro", uri=True,
                           timeout=15.0)
    try:
        row = conn.execute(sql, args).fetchone()
        return int(row[0] or 0) if row else 0
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def _config_row(db_path: str, key: str) -> tuple[int, object]:
    """`config` 表里这个键的 `(行数, 原值)`：行数 0 = 只有默认值，从没被写过。

    同值短路必须做到**行都不插入**（否则 `updated_at` 会被刷新，等于悄悄写了一笔）。
    """
    conn = sqlite3.connect("file:" + db_path.replace("\\", "/") + "?mode=ro", uri=True,
                           timeout=15.0)
    try:
        rows = conn.execute("SELECT key, value FROM config WHERE key=?", (key,)).fetchall()
        return (len(rows), rows[0][1] if rows else None)
    except sqlite3.Error:
        return (0, None)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
