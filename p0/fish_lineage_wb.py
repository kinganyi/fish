#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 会话树适配器（113 §4 · 2026-11-25 · W3）。

## 这一波做什么

把 WorkBuddy 的子代理 transcript 投影成 `SessionNode`（113 §2.3 的 18 列），
**只读宿主、只产节点**：本模块不 `import fish`、不连投影库、不写任何一行。
写口只有一个（`fish_lineage.upsert_node`），由 W2/W3 之后的接线与 W6 回填负责。

## WB 与 DSH 的根本差别：父写在路径里

DSH 的父只能从会话头 `parentSession` 读（110 §1.1 v3）；WB 的父**写在路径里**：

    projects/<ws>/<父 sid>/subagents/agent-<uuid>.jsonl

所以本适配器不做"读父日志"这一级——transcript 的首行只用来取 `cwd` 与
`providerData.agent`（角色），**不用来判父**。

## 四条来源优先级（113 §4.2，顺序即优先级）

1. `runtime`：`teams/<team>/runtime.json` 的 `members[].transcriptPath` 命中本文件
   → 父 = `runtime.leadSessionId`、`relation='team_member'`、`agent_role` 取 `agentId`
   的 `@` 前缀、`agent_label` 取 `taskDescription`；
2. `path`：只有路径、runtime 里没有这个成员（实况 1,281 / 2,099）；
3. `missing`：**判不到父**时落 `unknown/missing`——**不许兜回 main**（`fish_corpus_tag.py:240`
   的默认 main 就是这条的反面教材）；
4. runtime 与 path **冲突时以 runtime 为准**（runtime 有 `updatedAt`，路径会被搬）：
   路径父进 `extra.path_parent`，`basis` 仍是 `runtime`。

## 三条退化（113 §4.2 末，本轮实测的形态）

① `resumeSessionId` 实测有**三种**形态（`agent-<16hex>` / `agent-<uuid>` / `agent-<8hex>`），
   所以 `agent_id` 一律取 `transcriptPath` 的文件名（stem），`resumeSessionId` 只作退路；
② 3 个 runtime 成员**没有 `transcriptPath`**（czhy-p5 的三个 `unresumable`）→
   `agent_id=NULL`，**不许**拿 `agentId`（`role@team` 标签）顶上；连 `resumeSessionId`
   都没有时**没有会话键**，`resolve()` 返回 `None` 而不是造一行；
③ `runtime.json` 是缓存不是真相：读失败退 path，行上记 `source='wb:runtime(miss)'`。

## `session_id` 只能取文件名（113 §1-7 的护栏）

实测 2,099 份 transcript 的首行 `sessionId` **全部**是 `01a1e955-…` 这种 UUID，
**没有一份**是 `agent-<uuid>`；而语料里 742 行 `session_id` 已经是 `agent-*`（= `path.stem`，
`fish_corpus_wb.ingest_file` 就是这么写的）。所以 `session_id = path.stem` **一个字节都不许改**
——改了就是这 742 行主键整批漂移。首行那个 UUID 只写进 `extra.transcript_session_id` 备查。

## 会话键与 `agent_id` 为什么常常相等

DSh 侧 `agent_id` 恒 NULL（`agentPreset` 落 `agent_role`），而 WB 侧的"代理 id"就是
transcript 的文件名（`fish_lineage_schema._SESSION_LINEAGE_DDL` 第 47 行的注释），
与 `session_id` 同形。这不是冗余：`agent_id` 记的是**哪个代理会话**（可用于
`origin='main' AND agent_id LIKE 'agent-%'` 这类判据），`session_id` 记的是**树节点**。

## 只读三条线

  · `projects/**/agent-*.jsonl`（首行 + mtime）——**不写、不挪、不改**
  · `teams/*/runtime.json`
  · `workbuddy.db`（`mode=ro` URI 打开：宿主正在写它，只读是硬约束）

`workbuddy.db` 在判据里干两件事：① 给 `path` 父做"它真的是一个会话"的印证（J-D4 的孤儿
规则从这里落地）；② `resolve()` 把 lead/顶层会话解析成 `origin='main'` 的根节点
（`basis='db'`）。没有 DB 时这两件事都退化成"判不到"，**不假装**（见 `WbSessions.available`）。

## 语料侧复用（W3a · 113 §4.1 · 2026-11-25）

语料适配器 `fish_corpus_wb` 与树投影**必须用同一份判定**，否则同一条 transcript 会在
`corpus_session` 与 `session_lineage` 里落两个来源。所以分类被劈成两层：

  · `_classify_source(path, …)`：四条来源优先级的实现（只有一份）；
  · `classify_transcript(path, first_row, …)`：上面那层 + **首行证据**，多算
    `role`/`role_from`（`role_evidence`：runtime 的 `agentId` 前缀 > 首行 `providerData.agent`）；
  · `classify(path, …)`：`classify_transcript(path, None, …)` 的薄封装（兼容既有调用）。

`root_of_path()` 是给语料侧用的：它只拿到文件路径，而 runtime 在 `<root>/teams/` 下，
所以按路径里最后一级 `projects` 反推根；**推不出来就 `None`**（绝不回落到 `~/.workbuddy`）。

## 边界（113 §4.5，如实写进交付）

本波只做**离线投影**：hook 侧看不见成员级动作，撞车视图仍是"树上有行、实时看不到"。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Iterator

if TYPE_CHECKING:   # 只在类型检查时 import：本模块顶层不碰 fish（113 §2.5 的叶子纪律）
    from fish_lineage import SessionNode

#: 与 `session_lineage.host` 同值（113 §2.3 的 CHECK 值域里没有 'wb' 这种缩写）
HOST = "workbuddy"

#: 换一棵 WB 树（测试 / 别的机器）：**只有这一处**读环境变量，其余一律收 `root` 参数
ENV_WB_HOME = "FISH_WB_HOME"

SUBAGENTS_DIR = "subagents"
TRANSCRIPT_PREFIX = "agent-"
TRANSCRIPT_SUFFIX = ".jsonl"

#: WB 的 `projects` 这一级：语料侧只能靠它从路径反推根（`root_of_path`）
PROJECTS_DIR_NAME = "projects"

#: `role_from`（角色证据来自哪）：取值与 `extra.role_from` 既有口径逐字一致
ROLE_FROM_RUNTIME = "runtime"
ROLE_FROM_HEADER = "header"

#: `source` 串（`session_lineage.source` 是 TEXT，约定 `<host>:<来源>`）
SOURCE_RUNTIME = "wb:runtime"
SOURCE_RUNTIME_MISS = "wb:runtime(miss)"        # runtime 读失败 → 退 path（退化③）
SOURCE_RUNTIME_NOFILE = "wb:runtime(no-file)"   # runtime 说它在、transcript 已不在
SOURCE_PATH = "wb:path"
SOURCE_MISSING = "wb:missing"
SOURCE_DB = "wb:db"

#: `basis` 串（与 DDL 的 CHECK 值域逐字一致：header|catalog|db|path|runtime|depth|inferred|missing|conflict）
BASIS_RUNTIME = "runtime"
BASIS_PATH = "path"
BASIS_MISSING = "missing"
BASIS_DB = "db"

#: transcript 首行上限：第一行是普通 message，实测最大 ~12 KB，1 MiB 是"够用且不炸内存"的口径
MAX_HEADER_BYTES = 1 << 20


# ============================================================ 路径与时间的小工具


def default_root(root: str | os.PathLike | None = None) -> Path:
    """WB 根目录：显式 `root` > `FISH_WB_HOME` > `~/.workbuddy`。

    为什么允许覆盖：判据要造临时树（真宿主是只读的），而"造树"与"跑真树"必须是
    **同一份代码**——否则测的是另一个实现。
    """
    if root is not None:
        return Path(root)
    env = os.environ.get(ENV_WB_HOME)
    if env:
        return Path(env).expanduser()
    return Path.home() / ".workbuddy"


def projects_dir(root: str | os.PathLike | None = None) -> Path:
    return default_root(root) / PROJECTS_DIR_NAME


def teams_dir(root: str | os.PathLike | None = None) -> Path:
    return default_root(root) / "teams"


def db_file(root: str | os.PathLike | None = None) -> Path:
    return default_root(root) / "workbuddy.db"


def _iso(value: Any) -> str | None:
    """epoch 秒/毫秒 → `%Y-%m-%dT%H:%M:%SZ`（与 `fish_corpus_wb._to_utc:200` 同口径）。

    毫秒判定用 `> 1e11`（1e11 秒 = 5138 年）：WB 的 `updatedAt`/`created_at` 是毫秒，
    而 `st_mtime` 是秒，两者走同一个函数也不会互相误解。
    """
    if value is None or value == "":
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num > 1e11:
        num = num / 1000.0
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(num))


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _mtime_iso(path: Path, mtime: float | None = None) -> str:
    """transcript 的 `observed_at`：文件 mtime。

    为什么不用首行时间戳：mtime 是"这一行是什么时候被观测到的"的物证；首行时间戳是
    **会话开始时刻**，拿它当观测时刻会把老会话排到新扫描的末尾（排序就错了）。

    `mtime` 能从 `os.scandir` 的 `DirEntry.stat()` 免费拿到（Windows 上由目录枚举
    一次带回），传进来就不必再 `stat` 一次——实测 2,099 份 transcript 上多一次
    `stat` 要 1.3 秒，而 `scan` 是 W6 回填要反复跑的东西。
    """
    if mtime is not None:
        return _iso(mtime) or _now_iso()
    try:
        return _iso(path.stat().st_mtime) or _now_iso()
    except OSError:
        return _now_iso()


def _text(value: Any) -> str | None:
    """非空字符串或 `None`（空串/空白统一成 None，别让 `''` 混进 NOT NULL 列）。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _inner(data: dict | None, *keys: str) -> Any:
    """安全多层取值：任何一层不是 dict 就返回 None（transcript 首行是外部输入）。"""
    cur: Any = data
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def path_parent(path: str | os.PathLike) -> str | None:
    """`…/<父 sid>/subagents/agent-x.jsonl` → `<父 sid>`；不在 `subagents/` 下就 `None`。

    **唯一一处**判"路径即父"。返回裸父 id（不带 `host:` 前缀，113 §2.3 第 2 条不变式）。
    """
    parts = Path(path).parts
    if len(parts) >= 3 and parts[-2].lower() == SUBAGENTS_DIR:
        return parts[-3]
    return None


def root_of_path(path: str | os.PathLike) -> Path | None:
    """从 transcript 路径反推 WB 根：`<root>/projects/<ws>/…` → `<root>`。

    为什么需要它：`fish_corpus_wb.ingest_file` 只拿到**一个文件路径**，没有 `root`
    参数，而 runtime 与 `workbuddy.db` 都在根下。这里按路径里最后出现的 `projects`
    一级反推（WB 的树结构就是 `projects/<ws>/…`）。

    **推不出来就返回 `None`**，绝不回落到 `default_root()`（= `~/.workbuddy`）：
    临时树/测试树一旦踩空，读到的会是**生产 runtime** 并给出一个与本文件无关的父
    ——那比"只有路径证据"糟得多。
    """
    parts = Path(path).parts
    for index in range(len(parts) - 1, 0, -1):
        if parts[index].lower() == PROJECTS_DIR_NAME:
            return Path(*parts[:index])
    return None


def is_transcript_name(name: str) -> bool:
    """文件名形态：`agent-*.jsonl`（WB 子代理 transcript 的命名约定）。"""
    return (name.startswith(TRANSCRIPT_PREFIX) and name.endswith(TRANSCRIPT_SUFFIX)
            and not name.endswith(TRANSCRIPT_SUFFIX + ".zstd"))


# ============================================================ runtime（缓存不是真相）


class WbRuntime:
    """`teams/*/runtime.json` 的内存索引。

    三个取数口各有用途，**不合并**：
      · `by_transcript[文件名]` → 判"这个 transcript 是哪个团队的成员"（scan 用）
      · `by_agent_key['role@team']` → 让人能按 runtime 的标签反查（resolve 用）
      · `members` → 全量成员（含 3 个没有 transcriptPath 的），供判据/诊断用

    `failed` 非空 = 至少一份 runtime 读坏了。这时 path 行会记
    `source='wb:runtime(miss)'`（退化③）——**不是**说这一行读坏了，而是"本该用它、
    它没读出来"，落到行上才能被对账。
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = default_root(root)
        self.teams: dict[str, dict] = {}
        self.members: list[dict] = []
        self.by_transcript: dict[str, dict] = {}
        self.by_agent_key: dict[str, dict] = {}
        self.failed: list[str] = []
        self.notes: list[str] = []

    @property
    def loaded(self) -> bool:
        """有没有读到**至少一份** runtime（空树与"读了但全坏"必须区分）。"""
        return bool(self.teams)

    def member_of_path(self, path: str | os.PathLike) -> dict | None:
        """按文件名找 runtime 成员（大小写不敏感：Windows 主机上文件名不区分大小写）。"""
        return self.by_transcript.get(Path(path).name.lower())

    def __repr__(self) -> str:      # pragma: no cover - 只为读数好看
        return (f"<WbRuntime teams={len(self.teams)} members={len(self.members)} "
                f"transcripts={len(self.by_transcript)} failed={len(self.failed)}>")


def _member_of(team: str, lead: str | None, updated_at: str | None, raw: dict) -> dict:
    """`runtime.json` 的一个 `members[]` 条目 → 统一形状（**不猜、不丢**）。

    `agent_id` 只认 `transcriptPath` 的文件名：`agentId` 是 `role@team` 标签、
    `resumeSessionId` 有三种形态，**都不是** `agent-<uuid>` 的稳定来源（退化①②）。
    """
    agent_key = _text(raw.get("agentId"))
    transcript = _text(raw.get("transcriptPath"))
    stem = Path(transcript).stem if transcript else None
    missing_file = bool(transcript) and not Path(transcript).is_file()
    return {
        "team": team,
        "lead": lead,
        "agent_key": agent_key,                                   # 'role@team'：标签，不是 id
        "agent_id": stem,                                         # ← 只有文件名能当 agent_id
        "agent_role": agent_key.split("@", 1)[0] if agent_key else None,
        "agent_label": _text(raw.get("taskDescription")),
        "transcript": transcript,
        "resume": _text(raw.get("resumeSessionId")),              # 退路：没有 transcript 时才用
        "status": _text(raw.get("status")),
        "no_transcript": not transcript,                          # 退化②：3 个 unresumable 走这条
        "transcript_missing": missing_file,                       # runtime 说在、磁盘上没了
        "updated_at": updated_at,
    }


def load_runtime(root: str | os.PathLike | None = None) -> WbRuntime:
    """读 `teams/*/runtime.json`（10 份 / 828 成员 / 3 个无 transcript）。

    读失败**只记不抛**：runtime 是缓存，一份坏了不该让整棵树扫不出来——退化成 path
    并留痕（`failed` + `notes`）才是诚实的处理方式。
    """
    rt = WbRuntime(Path(root) if root is not None else None)
    base = teams_dir(rt.root)
    if not base.is_dir():
        rt.notes.append(f"teams 目录不存在：{base}")
        return rt
    for entry in sorted(base.iterdir(), key=lambda p: p.name):
        if not entry.is_dir():
            continue
        runtime_file = entry / "runtime.json"
        if not runtime_file.is_file():
            continue
        try:
            data = json.loads(runtime_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            rt.failed.append(f"{entry.name}: {exc.__class__.__name__}: {exc}")
            continue
        if not isinstance(data, dict):
            rt.failed.append(f"{entry.name}: runtime.json 不是对象")
            continue
        team = _text(data.get("teamName")) or entry.name
        lead = _text(data.get("leadSessionId"))
        updated = _iso(data.get("updatedAt"))
        slot = {"team": team, "dir": entry.name, "lead": lead, "updated_at": updated,
                "status": _text(data.get("status")), "members": 0,
                "lead_agent_key": _text(data.get("leadAgentId"))}
        rt.teams[team] = slot
        for raw in data.get("members") or ():
            if not isinstance(raw, dict):
                continue
            member = _member_of(team, lead, updated, raw)
            rt.members.append(member)
            slot["members"] += 1
            if member["agent_id"]:
                rt.by_transcript[member["agent_id"].lower() + TRANSCRIPT_SUFFIX] = member
            if member["agent_key"]:
                rt.by_agent_key[member["agent_key"]] = member
    return rt


# ============================================================ sessions（只读 DB）


class WbSessions:
    """`workbuddy.db` 的 `sessions` 表只读视图。

    两个属性把"判不到"和"没有"分开：
      · `available=False` → DB 打不开/表读不出，是本轮**判不到**（path 父不做孤儿降级）；
      · `available=True` 且 `has(id)=False` → 这是**真的没有**这一行（孤儿，J-D4）。
    把两者混成"没有"，一次锁库就会让整棵树掉成 missing。
    """

    def __init__(self) -> None:
        self.available = False
        self.rows: dict[str, dict] = {}
        self.notes: list[str] = []

    def has(self, session_id: str | None) -> bool:
        return bool(session_id) and session_id in self.rows

    def row(self, session_id: str | None) -> dict | None:
        return self.rows.get(session_id or "")

    def __repr__(self) -> str:      # pragma: no cover
        return f"<WbSessions available={self.available} rows={len(self.rows)}>"


def load_sessions(root: str | os.PathLike | None = None) -> WbSessions:
    """只读打开 `workbuddy.db`，取 `sessions(id, cwd, updated_at)`。

    **必须 `mode=ro`**：宿主正在写这个库（实测 mtime 每分钟级），以可写方式打开一个
    正在被另一个进程写的 SQLite 会拿到锁冲突，甚至把它锁死；而本模块的承诺是
    "只读宿主"（113 §4.1）。`as_uri()` 保证 Windows 路径（含中文目录）能被 URI 正确转义。
    """
    out = WbSessions()
    path = db_file(root)
    if not path.is_file():
        out.notes.append(f"库不存在：{path}")
        return out
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except (sqlite3.Error, ValueError, OSError) as exc:
        out.notes.append(f"只读打开失败：{exc.__class__.__name__}: {exc}")
        return out
    try:
        for sid, cwd, updated in conn.execute("SELECT id, cwd, updated_at FROM sessions"):
            out.rows[str(sid)] = {"cwd": _text(cwd), "updated_at": _iso(updated)}
        out.available = True
    except sqlite3.Error as exc:
        out.notes.append(f"sessions 表读取失败：{exc.__class__.__name__}: {exc}")
    finally:
        conn.close()
    return out


# ============================================================ transcript 首行


def read_header(path: str | os.PathLike,
                max_bytes: int = MAX_HEADER_BYTES) -> dict | None:
    """只读 transcript 的**第一行**（JSON）。

    为什么要"只读一行"：一个父会话的 `subagents/` 目录实测最大有 457 份 transcript
    （c48fd810），全读要几 GB；而本模块从首行只需要 `cwd` 与 `providerData.agent`。

    读不出（IO 错 / JSON 坏 / 不是对象）返回 `None`——**不抛**：一行读不出来不该让
    整棵树少一个节点（节点还有路径证据），坏在哪记在 `extra.header`。
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.readline(max_bytes)
    except OSError:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8-sig", "replace"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# ============================================================ 判来源（唯一一处）


def role_evidence(member: dict | None, first_row: dict | None) -> tuple[str | None, str | None]:
    """`(role, role_from)`：runtime 的 `agentId` 前缀 > 首行 `providerData.agent`。

    **唯一一处**判角色：树节点的 `agent_role` 与语料 `corpus_session.role`
    （113 §4.1 的四列之一）都从这里取。两处各写一份必然漂开——一处把 runtime 当权威、
    另一处当噪声，同一条 transcript 在两个投影里会有两个角色。

    `member=None` 且首行没有 `providerData.agent` → `(None, None)`，**不猜**。
    """
    if member is not None and member["agent_role"]:
        return member["agent_role"], ROLE_FROM_RUNTIME
    role = _text(_inner(first_row, "providerData", "agent"))
    if role:
        return role, ROLE_FROM_HEADER
    return None, None


def classify_transcript(path: str | os.PathLike | None, first_row: dict | None = None, *,
                        runtime: WbRuntime | None = None,
                        sessions: WbSessions | None = None) -> dict:
    """按「transcript 路径 + 首行 row」判来源（`classify` 的超集，树与语料共用）。

    与 `classify()` 的差别只有一个：多看一眼**首行**，把 `role`/`role_from` 一起算出来
    （语料的第四列就是 `role = providerData.agent`，113 §4.1）。判定本身仍走
    `_classify_source`（runtime → path → missing 的顺序一个字没改）。

    `path=None` = 只有首行、没有路径（判不到父，`reason='no_path'`）；`first_row=None`
    = 只有路径（`role` 为 `None`）——**两种"缺一半证据"都不许拿 main 冒充**。
    """
    decision = _classify_source(path, runtime=runtime, sessions=sessions)
    role, role_from = role_evidence(decision.get("member"), first_row)
    decision["role"] = role
    decision["role_from"] = role_from
    return decision


def classify(path: str | os.PathLike, *, runtime: WbRuntime | None = None,
             sessions: WbSessions | None = None) -> dict:
    """**唯一一处**判 `origin`/`relation`/`basis`/`parent_session`/`source`（113 §4.2）。

    顺序即优先级（runtime → path → missing；冲突以 runtime 为准）。返回 dict 而不是
    `SessionNode`：这里是"判定"，不是"节点"——判据可以单独喂一个路径来验这一层，
    不必先去造一整个文件树。

    返回的键：`parent_session` / `origin` / `relation` / `basis` / `source` / `member` /
    `path_parent`（仅在冲突或路径线索时）/ `reason` 与 `unverified_parent`（仅在判不到时）/
    `role` 与 `role_from`（角色证据；本入口没有首行，所以只可能来自 runtime）。

    本函数只是 `classify_transcript(path, None, …)` 的薄封装：**判定只有一份实现**。
    """
    return classify_transcript(path, None, runtime=runtime, sessions=sessions)


def _classify_source(path: str | os.PathLike | None, *, runtime: WbRuntime | None = None,
                     sessions: WbSessions | None = None) -> dict:
    """四条来源优先级的**实现**（`classify` / `classify_transcript` 共用，不经外）。

    单独抽出来是为了让 `role` 那层证据（需要首行）能叠在判定之上，而不必把判定
    写成两个版本。
    """
    decision: dict[str, Any] = {
        "parent_session": None, "origin": "unknown", "relation": None,
        "basis": BASIS_MISSING, "source": SOURCE_MISSING, "member": None,
    }
    if path is None:
        # 没有路径 = 只有首行证据：连"在不在 subagents/ 下"都判不了 → 判不到
        decision["reason"] = "no_path"
        return decision
    path = Path(path)
    parent = path_parent(path)
    member = runtime.member_of_path(path) if runtime is not None else None
    decision["member"] = member
    if parent is not None:
        decision["path_parent"] = parent

    # ① runtime：成员 → 父 = lead（**不用路径父**，冲突时也以 runtime 为准）
    if member is not None and member["lead"]:
        decision.update(parent_session=member["lead"], origin="subagent",
                        relation="team_member", basis=BASIS_RUNTIME,
                        source=SOURCE_RUNTIME)
        return decision
    if member is not None:
        # runtime 里连 leadSessionId 都没有：这份 runtime 残缺，不能拿它判父（退 path）
        decision["reason"] = "runtime_without_lead"

    # ② path：路径父；但"父在 sessions 表里不存在"是孤儿（J-D4）→ unknown/missing
    if parent is not None:
        if sessions is not None and sessions.available and not sessions.has(parent):
            decision.update(reason="parent_not_in_sessions", unverified_parent=parent)
            return decision
        decision.update(parent_session=parent, origin="subagent", relation="spawn",
                        basis=BASIS_PATH,
                        source=(SOURCE_RUNTIME_MISS if (runtime is not None and runtime.failed)
                                else SOURCE_PATH))
        return decision

    # ③ missing：不在 `…/<父>/subagents/` 下 → 判不到父，**绝不兜回 main**
    decision["reason"] = "not_in_subagents"
    return decision


# ============================================================ 节点构造


def _node(**fields) -> "SessionNode":
    """延迟 import `SessionNode`（顶层不碰 fish：113 §2.5 的叶子方向）。"""
    from fish_lineage import SessionNode      # noqa: PLC0415 - 见模块 docstring

    return SessionNode(**fields)


def _base_fields(path: Path, stem: str, mtime: float | None = None) -> dict:
    """transcript 节点的骨架：所有列都给默认值，`classify` 再覆盖判定那几列。

    为什么把 18 列一次列全：`upsert_node` 会拒未知字段、补 DEFAULT——但"少给一列"
    它看不出来（补默认值就过了）。这里列全，缺值就是**明确的** `None` 而不是"被 DEFAULT
    悄悄补上"。
    """
    return {
        "host": HOST,
        "session_id": stem,
        "parent_session": None,
        "lineage_path": "",
        "depth": 0,
        "depth_host": None,        # WB transcript 首行没有 delegationDepth（实测）
        "origin": "unknown",
        "relation": None,
        "agent_id": stem,
        "agent_role": None,
        "agent_label": None,
        "agent_path": None,        # 本期恒 NULL（113 §2.3 第 4 条）
        "cwd": None,
        "agent_preset": None,
        "basis": BASIS_MISSING,
        "source": SOURCE_MISSING,
        "extra": None,
        "observed_at": _mtime_iso(path, mtime),
    }


def transcript_node(path: str | os.PathLike, *, runtime: WbRuntime | None = None,
                    sessions: WbSessions | None = None,
                    header: dict | None = None,
                    mtime: float | None = None) -> "SessionNode":
    """一个 transcript 文件 → 一个节点（`classify` 判来源，这里只装配 + 记证据）。

    `session_id` 取 `Path(path).stem`：**这一行是全模块唯一的 id 来源**（113 §1-7）。
    """
    path = Path(path)
    stem = path.stem
    if header is None:
        header = read_header(path)
    decision = classify_transcript(path, header, runtime=runtime, sessions=sessions)
    node = _base_fields(path, stem, mtime)
    parent = decision["parent_session"]
    node.update(parent_session=parent, origin=decision["origin"],
                relation=decision["relation"], basis=decision["basis"],
                source=decision["source"], depth=1 if parent else 0,
                lineage_path=f"/{parent}" if parent else "")
    extra: dict[str, Any] = {"agent_id_from": "transcript_filename"}
    if header is None:
        extra["header"] = "unreadable"
    else:
        extra["transcript_session_id"] = _text(header.get("sessionId"))
        if header.get("cwd"):
            node["cwd"] = _text(header.get("cwd"))
    # 角色只从 `role_evidence` 取（runtime 前缀 > 首行 providerData.agent），
    # 与语料侧 `role` 列同一份实现——两处各写一份必然漂开（见 `role_evidence`）。
    if decision["role"]:
        extra["role_from"] = decision["role_from"]
        node["agent_role"] = decision["role"]
    member = decision["member"]
    if member is not None:
        extra["team"] = member["team"]
        if member["updated_at"]:
            extra["runtime_updated_at"] = member["updated_at"]
        if member["agent_key"]:
            # runtime 的 `agentId`（`role@team`）是**标签**不是会话 id：放证据，不占 agent_id
            extra["agent_key"] = member["agent_key"]
        if member["agent_label"]:
            node["agent_label"] = member["agent_label"]
        if decision.get("path_parent") and decision["path_parent"] != parent:
            # 冲突：runtime 赢，路径只留证据（113 §4.2 第 4 条）
            extra["path_parent"] = decision["path_parent"]
            extra["path_conflict"] = 1
    for key in ("reason", "unverified_parent"):
        if decision.get(key):
            extra[key] = decision[key]
    node["extra"] = extra
    return _node(**node)


def runtime_member_node(member: dict) -> "SessionNode | None":
    """只有 runtime、没有 transcript 文件的成员 → 节点（**没有会话键就 None**）。

    什么时候会走到这里：runtime 说这个成员有 `transcriptPath`，但文件已经不在了
    （实测 7 个 / 828）；或者成员没有 `transcriptPath`（3 个 `unresumable`）而有
    `resumeSessionId`。后者实测那 3 个连 resume 都没有 → 返回 `None`：
    **没有会话键时造一行，等于伪造一个不存在的会话**（113 §4.2 退化②）。
    """
    session_id = member["agent_id"] or member["resume"]
    if not session_id:
        return None
    extra: dict[str, Any] = {"team": member["team"], "agent_id_from": "transcript_filename"}
    if member["updated_at"]:
        extra["runtime_updated_at"] = member["updated_at"]
    if member["no_transcript"]:
        extra["no_transcript"] = 1                  # 退化②：成员行，但没有 transcript
    if member["transcript_missing"]:
        extra["transcript_missing"] = 1             # runtime 说在、磁盘上没了
    if member["agent_key"]:
        extra["agent_key"] = member["agent_key"]    # 'role@team' 标签，只作证据
    if member["status"]:
        extra["status"] = member["status"]
    node = {
        "host": HOST, "session_id": session_id,
        "parent_session": member["lead"], "lineage_path": f"/{member['lead']}",
        "depth": 1 if member["lead"] else 0, "depth_host": None,
        "origin": "subagent" if member["lead"] else "unknown",
        "relation": "team_member" if member["lead"] else None,
        "agent_id": member["agent_id"],             # 退化②：无 transcript 时是 NULL
        "agent_role": member["agent_role"], "agent_label": member["agent_label"],
        "agent_path": None, "cwd": None, "agent_preset": None,
        "basis": BASIS_RUNTIME, "source": SOURCE_RUNTIME_NOFILE,
        "extra": extra, "observed_at": member["updated_at"] or _now_iso(),
    }
    return _node(**node)


def db_root_node(session_id: str, *, sessions: WbSessions) -> "SessionNode | None":
    """`sessions` 表里的一行 → **根节点**（`origin='main'`、`basis='db'`）。

    为什么需要这条：树要有根。lead 与顶层会话不是 transcript（它们在 `sessions` 表里），
    没有这条就只剩一片"父 id 指向不存在的行"的子代理。`origin='main'` 的依据是
    "它是 WB 的顶层层会话"（`sessions.id`），`agent_id` 恒 NULL——
    `origin='main' AND agent_id LIKE 'agent-%'` 必须恒 0（J3③）。
    """
    row = sessions.row(session_id)
    if row is None:
        return None
    extra: dict[str, Any] = {"from": "sessions"}
    if not row.get("updated_at"):
        extra["observed_at"] = "fallback:now"
    node = {
        "host": HOST, "session_id": session_id,
        "parent_session": None, "lineage_path": "",
        "depth": 0, "depth_host": None,
        "origin": "main", "relation": None,
        "agent_id": None, "agent_role": None, "agent_label": None,
        "agent_path": None, "cwd": row.get("cwd"), "agent_preset": None,
        "basis": BASIS_DB, "source": SOURCE_DB,
        "extra": extra, "observed_at": row.get("updated_at") or _now_iso(),
    }
    return _node(**node)


# ============================================================ 枚举与解析


def _subdirs(path: Path) -> Iterator[Path]:
    """`path` 下的子目录（排序、跳过 IO 错）。

    为什么用 `os.scandir` 而不是 `Path.iterdir` + `is_dir()`：`DirEntry.is_dir()`
    在 Windows 上由**目录枚举**一次带回（`FindFirstFile` 的属性位），不再额外 `stat`；
    2,099 份 transcript 上实测省掉 1.3 秒（`scan` 是 W6 回填要反复跑的东西）。
    一个坏链接也不该让整次扫描停摆，所以 IO 错只跳过。
    """
    try:
        with os.scandir(str(path)) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_dir():
                yield Path(entry.path)
        except OSError:
            continue


def _transcript_entries_in(directory: Path) -> Iterator[tuple[Path, float | None]]:
    """`directory` 下**直接**的 `agent-*.jsonl` → `(路径, mtime)`（不递归）。

    顺手把 `DirEntry.stat().st_mtime` 带出来：它就是 `observed_at` 的来源，
    在 `scan` 里再 `stat` 一次纯属重复（见 `_mtime_iso` 的说明）。
    """
    try:
        with os.scandir(str(directory)) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError:
        return
    for entry in entries:
        if not is_transcript_name(entry.name):
            continue
        try:
            if not entry.is_file():
                continue
            mtime: float | None = entry.stat().st_mtime
        except OSError:
            continue
        yield Path(entry.path), mtime


def _iter_transcript_entries(root: str | os.PathLike | None = None, *,
                             strays: bool = True) -> Iterator[tuple[Path, float | None]]:
    """`iter_transcripts` 的带 mtime 版本（`scan` 用；对外契约仍是 `iter_transcripts`）。"""
    projects = projects_dir(root)
    if not projects.is_dir():
        return
    normal: list[tuple[Path, float | None]] = []
    stray: list[tuple[Path, float | None]] = []
    for ws in _subdirs(projects):
        stray.extend(_transcript_entries_in(ws))                # stray ①：<ws>/agent-*.jsonl
        for entry in _subdirs(ws):
            sub = entry / SUBAGENTS_DIR
            if sub.is_dir():
                normal.extend(_transcript_entries_in(sub))       # 正常位：<父>/subagents/
            # `<父>/agent-*.jsonl` **无论 subagents/ 在不在**都要查：把文件挪出子目录时，
            # 空掉的 `subagents/` 通常还留在原地（只搬文件不删目录），漏了这一支就等于
            # "挪出去的文件既不正常、也不算 stray"——它会从树上消失，而不是落成 missing。
            stray.extend(_transcript_entries_in(entry))          # stray ②：<父>/agent-*.jsonl
    for path, mtime in sorted(normal, key=lambda item: str(item[0])):
        yield path, mtime
    if strays:
        for path, mtime in sorted(stray, key=lambda item: str(item[0])):
            yield path, mtime


def iter_transcripts(root: str | os.PathLike | None = None, *,
                     strays: bool = True) -> Iterator[Path]:
    """枚举所有 transcript：先正常位（`<父>/subagents/`），再 stray 位。

    为什么 stray 也要枚举：把 transcript 挪出 `subagents/` 之后，如果扫描只认正常位，
    这个会话就**从树上消失**——"消失"和"判不到"看起来一样，而后者必须落成
    `unknown/missing` 留痕（113 §4.3 J3③ 的变异就是打这一点）。

    stray 的范围是**有界**的：只查 `<ws>/` 与 `<父>/` 两层。不递归进
    `subagents/<agent>/tool-results/`（实测 2 万+ 文件、多花 10 秒，那里不可能放
    transcript；113 §0.3 实测"subagents/ 外 0 个"）。
    """
    for path, _mtime in _iter_transcript_entries(root, strays=strays):
        yield path


def find_transcript(session_id: str,
                    root: str | os.PathLike | None = None) -> Path | None:
    """按 `session_id`（= 文件名 stem）在 projects 下找出 transcript。

    为什么按文件名找而不是按 id 猜路径：`projects/<ws>` 这一层是**工作区**（27 个），
    id 里没有任何东西能指回它；只有文件名是唯一稳定的键（113 §1-7 的反面：
    首行 `sessionId` 与文件名不是一回事）。
    """
    if not session_id:
        return None
    for path in iter_transcripts(root, strays=True):
        if path.stem == session_id:
            return path
    return None


def resolve_path(path: str | os.PathLike, *, runtime: WbRuntime | None = None,
                 sessions: WbSessions | None = None,
                 header: dict | None = None) -> "SessionNode | None":
    """按路径判一个会话（文件在 → transcript 节点；文件不在但 runtime 有 → runtime 节点）。"""
    path = Path(path)
    if path.is_file():
        return transcript_node(path, runtime=runtime, sessions=sessions, header=header)
    member = runtime.member_of_path(path) if runtime is not None else None
    if member is not None:
        return runtime_member_node(member)
    return None


def resolve(session_id: str, *, hint: dict | None = None,
            root: str | os.PathLike | None = None,
            runtime: WbRuntime | None = None,
            sessions: WbSessions | None = None) -> "SessionNode | None":
    """判单个会话（**判不到就 None，不许拿 main 冒充**，113 §2.4）。

    接受四种输入，顺序即"证据从强到弱"：
      1. `hint={'path': …}` 或本身就是个路径 → 按路径（最直接，transcript 就在那儿）；
      2. runtime 的 `agentId` 标签（`role@team`）→ 成员行；
      3. transcript 文件名 stem（`agent-<uuid>`）→ runtime 或文件查找；
      4. `sessions` 表里的会话 id → 根节点（`basis='db'`）。
    都没有 → `None`：这条会话在本适配器的证据面之外，编一个 main 会让树多出一条假根。
    """
    root_path = default_root(root)
    runtime = load_runtime(root_path) if runtime is None else runtime
    sessions = load_sessions(root_path) if sessions is None else sessions
    sid = _text(session_id)

    hinted = None
    if isinstance(hint, dict):
        hinted = _text(hint.get("path")) or _text(hint.get("transcript_path"))
    if hinted:
        return resolve_path(hinted, runtime=runtime, sessions=sessions)
    if sid and (os.sep in sid or "/" in sid) and sid.endswith(TRANSCRIPT_SUFFIX):
        return resolve_path(sid, runtime=runtime, sessions=sessions)
    if not sid:
        return None

    # ② runtime 的标签（role@team）：标签不是会话 id，只在有 transcript 时借它定位
    member = runtime.by_agent_key.get(sid)
    if member is not None:
        if member["transcript"] and Path(member["transcript"]).is_file():
            return transcript_node(Path(member["transcript"]), runtime=runtime,
                                   sessions=sessions)
        return runtime_member_node(member)

    # ③ transcript stem
    member = runtime.by_transcript.get(sid.lower() + TRANSCRIPT_SUFFIX)
    if member is not None:
        if member["transcript"] and Path(member["transcript"]).is_file():
            return transcript_node(Path(member["transcript"]), runtime=runtime,
                                   sessions=sessions)
        return runtime_member_node(member)

    # ④ `sessions` 表的会话 id（UUID 形态，先查库：不命中就不必走 0.25 秒的文件枚举）
    if not sid.startswith(TRANSCRIPT_PREFIX):
        node = db_root_node(sid, sessions=sessions)
        if node is not None:
            return node

    found = find_transcript(sid, root=root_path)
    if found is not None:
        return transcript_node(found, runtime=runtime, sessions=sessions)
    return None


def scan(*, since: str | None = None, limit: int | None = None,
         root: str | os.PathLike | None = None,
         runtime: WbRuntime | None = None,
         sessions: WbSessions | None = None,
         strays: bool = True) -> list["SessionNode"]:
    """批量枚举 transcript 节点（**行数 = 扫描到的 `agent-*.jsonl` 数**）。

    · `since`：`observed_at`（UTC `%Y-%m-%dT%H:%M:%SZ`）字符串下界，串比较与时间比较同序；
    · `limit`：按 `observed_at` 倒序取前 N 个（**排序后再截**，否则截到的是文件序）；
    · `strays=True`：把"挪出 `subagents/`"的文件也枚举出来，判成 `unknown/missing`。

    不设 `max_depth`：WB 的树只有"父会话 → 子代理"一层，但适配器不该假设这件事
    （113 §0.1 第 4 条：不设上限，只做防环与孤儿诚实）。
    """
    root_path = default_root(root)
    runtime = load_runtime(root_path) if runtime is None else runtime
    sessions = load_sessions(root_path) if sessions is None else sessions
    nodes: list[dict] = []
    for path, mtime in _iter_transcript_entries(root_path, strays=strays):
        node = transcript_node(path, runtime=runtime, sessions=sessions, mtime=mtime)
        if since and node["observed_at"] < since:
            continue
        nodes.append(node)
    nodes.sort(key=lambda item: (item["observed_at"], item["session_id"]), reverse=True)
    if limit is not None:
        nodes = nodes[:max(0, int(limit))]
    return nodes


# ============================================================ 适配器（协议实现）


class WbLineageAdapter:
    """`fish_lineage.SessionLineageAdapter` 的 WorkBuddy 实现（113 §4.1）。

    注册表接线（把 `fish_lineage.ADAPTERS['workbuddy']` 换成本类）由主理人在 W2/W3
    两波之后统一做——本模块**不改** `fish_lineage.py`（两波同写一个文件必然打架，
    113 §2.5）。

    实例内缓存 runtime/sessions：一次扫描里 `sessions` 表要 read 310 行、runtime 10 份，
    每个节点各读一遍就是 2,099 次重复 IO。缓存是**这一轮**的真相；宿主改库之后要调
    `refresh()`（`.scan()` 是离线投影，不做增量订阅）。
    """

    host = HOST

    def __init__(self, root: str | os.PathLike | None = None) -> None:
        self.root = default_root(root)
        self._runtime: WbRuntime | None = None
        self._sessions: WbSessions | None = None

    @property
    def runtime(self) -> WbRuntime:
        if self._runtime is None:
            self._runtime = load_runtime(self.root)
        return self._runtime

    @property
    def sessions(self) -> WbSessions:
        if self._sessions is None:
            self._sessions = load_sessions(self.root)
        return self._sessions

    def refresh(self) -> None:
        """丢掉缓存（宿主新增了 team / 会话之后重扫用）。"""
        self._runtime = None
        self._sessions = None

    def resolve(self, session_id: str, *,
                hint: dict | None = None) -> "SessionNode | None":
        return resolve(session_id, hint=hint, root=self.root,
                       runtime=self.runtime, sessions=self.sessions)

    def scan(self, *, since: str | None = None, limit: int | None = None,
             strays: bool = True) -> Iterable["SessionNode"]:
        return scan(since=since, limit=limit, root=self.root,
                    runtime=self.runtime, sessions=self.sessions, strays=strays)

    def __repr__(self) -> str:      # pragma: no cover
        return f"<WbLineageAdapter root={self.root}>"
