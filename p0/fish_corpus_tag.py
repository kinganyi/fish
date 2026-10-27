#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打标：会话级标签的唯一一份实现（原始对话语料层 · T02 · 设计 §5）。

这条模块只做一件事：**把"一段对话属于谁、哪个项目、哪个公民"变成可判定的事实**，
并且拒绝一切"猜一个最像的"。PRD C-P0-3 的四条红判据就是它的四个分支：

| 现场 | 必须得到 | 反例（红） |
|---|---|---|
| `cwd=I:\\app\\anyi-studio` | `project='anyi-studio'`, `source='mapped'` | — |
| `cwd=…\\workspace\\learn`（441 条，未映射） | `project=''`, `source='unmapped'` | 硬塞成「大肥鱼生命系统」 |
| 目录名 `--I-app-…~5DE5…--` | **不参与**判项目（R1） | 用目录名判出项目 |
| 子代理缺 `parent_session/delegation_depth` | `TagError`（→ 降级 `bad_header`） | 静默当主会话 |

为什么解析器要注入（设计 §2 判据）
--------------------------------
`resolve_project_by_cwd`（`fish.py:5464`）与 `fish_activity.resolve_citizen`
住在门面/采集侧，若本模块 `import fish` 就成循环依赖（`fish` 要 import `fish_corpus`）。
所以两个解析器都以 `Callable` 注入，本模块**不认识**主库。缺解析器时不是猜，
而是写清 `project_source='unmapped'` + `project_note` 说明"没有解析器参与"。

`project_source` 四档的含义（设计 §3）
------------------------------------
- `mapped`   —— 命中了 `workspace_map` 的最长前缀（R2 的正向）。
- `unmapped` —— 有 cwd，但没有任何映射命中。**这不是错误**，是"补映射前的事实"。
- `no_cwd`   —— 会话头没带 cwd（例如 WB 早期文件），连比都没法比。
- `conflict` —— 解析器给出了**不可用**的值（抛异常/非字符串/空串当命中），
  此时宁可记"归属有冲突"也不回填一个项目。这是本实现对该码的解释：
  设计只列了四档、没定义 `conflict` 的触发条件（见交付报告"偏离"）。
"""
from __future__ import annotations

import json

import fish_activity
import fish_corpus_redact as redact_mod
import fish_surface
import fish_corpus_store as store

PROJECT_MAPPED = "mapped"
PROJECT_UNMAPPED = "unmapped"
PROJECT_NO_CWD = "no_cwd"
PROJECT_CONFLICT = "conflict"
PROJECT_SOURCES = (PROJECT_MAPPED, PROJECT_UNMAPPED, PROJECT_NO_CWD, PROJECT_CONFLICT)

ORIGIN_MAIN = "main"
ORIGIN_SUBAGENT = "subagent"
ORIGINS = (ORIGIN_MAIN, ORIGIN_SUBAGENT)

IDENTITY_SESSION_TOKEN = "session_tokens"

NOTE_NO_RESOLVER = "（未注入 project_of：workspace_map 未参与解析）"
NOTE_CANDIDATE_LIMIT = 3


class TagError(ValueError):
    """打标失败（子代理缺父/缺深度、解析器不可用等）。**必须响亮，不许降级成空值**。"""


def _safe(text):
    """可回显列的落盘前脱敏（安全新🟠）：`cwd`/`workspace`/`project_note`/标题统一走这一份。

    为什么在打标层做：这三个字段分别由两个适配器写（DSH 头、WB 补 cwd），在适配器里
    各写一遍就会有一侧漏（旧实现 `cwd` 完全不脱敏，而 `stats.unmapped_cwds` 会把它
    原样回显给 AI）。规则只有一份（`fish_corpus_redact.RULES`），这里只负责"写完之前"。
    """
    if not text:
        return text
    cleaned, _hits, _kinds = redact_mod.redact(str(text))
    return cleaned


def _header_get(header, *names, default=None):
    """取头部字段的第一个非空值（两个适配器的键名不完全一样）。"""
    for name in names:
        if name in header and header[name] not in (None, ""):
            return header[name]
    return default


def _project_of_cwd(project_of, cwd) -> tuple:
    """`cwd → (project, source, note)`；四档的**唯一**判定点。"""
    if not cwd:
        return "", PROJECT_NO_CWD, None
    if project_of is None:
        return "", PROJECT_UNMAPPED, NOTE_NO_RESOLVER
    try:
        project = project_of(cwd)
    except Exception as exc:                       # noqa: BLE001 —— 注入方的锅也要记下来
        return "", PROJECT_CONFLICT, "%s: %s" % (type(exc).__name__, exc)
    if project is None:
        return "", PROJECT_UNMAPPED, None
    if not isinstance(project, str):
        return "", PROJECT_CONFLICT, "解析器返回了非字符串：%r" % (project,)
    project = project.strip()
    if not project:
        # "命中但值为空"是数据损坏，不是"没命中"——两者处置完全不同。
        return "", PROJECT_CONFLICT, "解析器命中了空项目名"
    return project, PROJECT_MAPPED, None


def _note_candidates(candidates_of, cwd) -> "str | None":
    """未映射时把 cwd **之下**已映射的项目抄给人（`list_workspace_candidates`）。

    为什么要有这条：441 条会话的 cwd 是 `workspace\\learn`，而它下面 8 个项目都映射了
    ——不列候选的话，"为什么全是未归属"就只能靠读库才能解释（`fish.py:5500` 的既有裁决）。
    """
    if candidates_of is None or not cwd:
        return None
    try:
        cands = candidates_of(cwd) or []
    except Exception:                              # noqa: BLE001 —— 提示失败不该拖垮采集
        return None
    items = []
    for cand in cands[:NOTE_CANDIDATE_LIMIT]:
        if isinstance(cand, dict):
            project = cand.get("project")
            prefix = cand.get("path_prefix") or cand.get("prefix")
        else:
            project, prefix = (cand + (None,))[:2] if isinstance(cand, tuple) else (None, None)
        if project and prefix:
            items.append("%s ← %s" % (project, prefix))
    if not items:
        return None
    more = "" if len(cands) <= NOTE_CANDIDATE_LIMIT else \
        "（另有 %d 条候选）" % (len(cands) - NOTE_CANDIDATE_LIMIT)
    return "cwd 之下已映射：" + "；".join(items) + more


class Tagger:
    """把"会话头"翻成 `corpus_session` 的一行（设计 §3 类图）。

    四个可注入点（全部可选，缺了就是"不知道"，不是"猜一个"）：
    - `project_of(cwd) -> str | None`   —— 最长前缀项目解析（门面注入）；
    - `citizen_of(session_key) -> str | None` —— 会话键 → 公民；
    - `candidates_of(cwd) -> list`      —— 未映射时的可抄候选；
    - `strict_subagent=True`            —— 子代理缺父/缺深度时**抛错**（C-P0-3 ③）。
    """

    def __init__(self, project_of=None, citizen_of=None, candidates_of=None, *,
                 strict_subagent: bool = True):
        if project_of is not None and not callable(project_of):
            raise TagError("project_of 必须是可调用对象")
        if citizen_of is not None and not callable(citizen_of):
            raise TagError("citizen_of 必须是可调用对象")
        if candidates_of is not None and not callable(candidates_of):
            raise TagError("candidates_of 必须是可调用对象")
        self.project_of = project_of
        self.citizen_of = citizen_of
        self.candidates_of = candidates_of
        self.strict_subagent = bool(strict_subagent)

    # ------------------------------------------------------------------ 单会话
    def tag_session(self, header: dict, host: str = "dsh") -> dict:
        """`header → corpus_session` 行（不含 segments/shadowed，那两个是派生计数）。"""
        if not isinstance(header, dict):
            raise TagError("会话头必须是 dict，收到 %r" % type(header).__name__)
        session_id = _header_get(header, "session_id", "id")
        if not session_id:
            raise TagError("会话头没有 session_id/id（无法定位一段对话）")
        host = (host or "").strip().lower()
        if host not in ("dsh", "workbuddy"):
            raise TagError("未知宿主：%r" % host)

        extra = dict(header.get("extra") or {})
        cwd = _header_get(header, "cwd")
        project, source, note = _project_of_cwd(self.project_of, cwd)
        if source == PROJECT_UNMAPPED and not note:
            note = _note_candidates(self.candidates_of, cwd)
        origin, basis = self._origin_of(header, extra)
        parent = _header_get(header, "parent_session", "parentSession")
        depth_raw = _header_get(header, "delegation_depth", "delegationDepth")
        depth = 0
        if depth_raw is not None:
            try:
                depth = int(depth_raw)
            except (TypeError, ValueError):
                raise TagError("delegation_depth 不是整数：%r" % (depth_raw,))
        if depth < 0:
            raise TagError("delegation_depth 不能为负：%r" % (depth_raw,))
        if origin == ORIGIN_SUBAGENT and self.strict_subagent:
            if not parent:
                raise TagError("子代理会话缺 parent_session（C-P0-3 ③）")
            if depth_raw is None:
                raise TagError("子代理会话缺 delegation_depth（C-P0-3 ③）")

        citizen_id, identity_source = self._identity_of(host, session_id)
        mismatch = header.get("path_id_mismatch")
        if mismatch:
            extra["path_id_mismatch"] = 1
        # 落盘前脱敏（安全新🟠）：`project` 仍按**原始 cwd** 解析（映射表要真路径），
        # 但 cwd/workspace/project_note 三列是**可回显文本**（`stats.unmapped_cwds`
        # 会原样把它们回给 AI）——写进去之前过同一份 `RULES`。
        workspace = fish_surface.normalize_path(cwd) if cwd else ""

        return {
            "host": host,
            "session_id": str(session_id),
            "sid8": store.sid8(session_id),
            "title": _safe(_header_get(header, "title")),
            "cwd": _safe(cwd),
            "workspace": _safe(workspace),
            "project": project,
            "project_source": source,
            "project_note": _safe(note),
            "citizen_id": citizen_id,
            "identity_source": identity_source,
            "origin": origin,
            "origin_basis": basis,
            "parent_session": str(parent) if parent else None,
            "delegation_depth": depth,
            "role": _header_get(header, "role"),
            "agent_preset": _header_get(header, "agent_preset", "agentPreset"),
            "conversation_type": "unknown",     # P1 位（C-P1-5）：P0 恒 unknown
            "started_at": _header_get(header, "created_at", "createdAt"),
            "last_event_at": _header_get(header, "last_event_at"),
            "extra": json.dumps(extra, ensure_ascii=False, sort_keys=True) if extra else None,
        }

    def _origin_of(self, header, extra: dict) -> tuple:
        """`(origin, origin_basis)`；含"有 parent 无 origin"这种实测组合（§4.1）。

        实测 474 个会话有 `parentSession`，其中 14 个**没有** `origin`
        （样例 `delegationDepth=0`、`agentPreset=software-team`）。这两种事实矛盾时
        按"parent 存在即从属"处理，但把矛盾本身记进 `extra.parent_without_origin`
        ——判据要看得到它，而不是要人猜。
        """
        origin = _header_get(header, "origin")
        parent = _header_get(header, "parent_session", "parentSession")
        if origin == ORIGIN_SUBAGENT:
            return ORIGIN_SUBAGENT, "header"
        if origin:
            return ORIGIN_MAIN, "header"
        if parent:
            extra["parent_without_origin"] = 1
            return ORIGIN_MAIN, "header"
        return ORIGIN_MAIN, "default"

    def _identity_of(self, host, session_id) -> tuple:
        """`(citizen_id, identity_source)`；查不到就 `(None, None)`——**不猜**。"""
        if self.citizen_of is None:
            return None, None
        try:
            key = fish_activity.make_session_key(host, session_id)
        except Exception as exc:                   # noqa: BLE001
            raise TagError("会话键构造失败：%s: %s" % (type(exc).__name__, exc))
        try:
            citizen_id = self.citizen_of(key)
        except Exception as exc:                   # noqa: BLE001
            raise TagError("citizen_of 失败：%s: %s" % (type(exc).__name__, exc))
        if not citizen_id:
            return None, None
        # 来源标注只是"这条路查到了"，不冒充"凭证校验通过"：`session_tokens`
        # 与 `host_session_key` 的实际命中由注入方决定，这里只标它给了什么。
        return str(citizen_id), IDENTITY_SESSION_TOKEN

    # ------------------------------------------------------------------ 重算
    def retag(self, conn, *, host=None, session_id=None, all_: bool = True,
              dry_run: bool = False, allow_downgrade: bool = False) -> dict:
        """按库里已有的 `cwd` **重算**会话标签（R5）。幂等：只 UPDATE，不重采、不改段数。

        为什么必须有它：现状 514/514 未命中 `workspace_map`（实测）——等 O10 拍板补了
        映射，历史会话若不能重算，"项目标注覆盖率"就永远停在 0，语料层等于白采。
        为什么不能缺 `project_of`：那会把已映射的历史全部改回 `unmapped`，
        是"越跑越差"的静默故障，所以这里直接拒绝。

        **`mapped → unmapped` 必须响亮**（安全 🟡 · 2026-10-27 终批）：库里的 `cwd` 是
        **写时脱敏后**的文本（`_safe(cwd)`），拿它重算可能命中不了映射前缀——实测
        一次 retag 就把已映射的会话全降成未映射（`changed=1`，`mapped=0`）。所以：
          · 重算不出映射、而库里写着 `mapped` + 有项目名时，**保留写时算好的归属**，
            并把它记进 `counters["kept_mapped"]` 与 `counters["warnings"]`（CLI 会印）
            ——"悄悄变差"比"报个错"糟得多；
          · 真要取消映射（映射表真被删了）就显式传 `allow_downgrade=True`，那是
            一次**有意**的动作，不是重算的副作用。

        **身份列不许被重算抹成 NULL**（安全官实测 · 2026-10-27 终验黄项）：`_identity_of`
        在没注入 `citizen_of` 时返回 `(None, None)`，旧实现直接把它写进 UPDATE——
        `public:selftest/session_tokens` 只跑一次 retag 就变成 `None/None`（还让
        `changed` 首次虚高）。现在两条都堵上：
          · 解析器**没给出值**（没注入，或 `session_tokens` 里查不到这个会话键）→
            **保留库里原值**，计入 `kept_identity`（CLI 会印）。一次忘传解析器、一次
            令牌清理，都不该让历史语料集体失去归属——真要清身份是显式的改库动作；
          · 解析器**给出了公民** → 写回（`None → 公民` 的补齐是有意的），且 `changed`
            只在**真有列变化**时才加（`identity_source` 也进比对）。
        """
        if self.project_of is None:
            raise TagError("retag 需要 project_of 解析器（否则会把已映射会话改回未映射）")
        sql = "SELECT * FROM corpus_session WHERE 1=1"
        args = []
        if host:
            sql += " AND host=?"
            args.append(host)
        if session_id:
            sql += " AND session_id=?"
            args.append(str(session_id))
        if not all_ and not (host or session_id):
            raise TagError("retag 需要范围（all_=True 或给 host/session_id）")
        rows = conn.execute(sql + " ORDER BY host, session_id", args).fetchall()

        counters = {"sessions": 0, "changed": 0, "mapped": 0, "unmapped": 0,
                    "no_cwd": 0, "conflict": 0, "kept_mapped": 0, "kept_identity": 0,
                    "warnings": [], "dry_run": bool(dry_run)}
        updates = []
        for row in rows:
            row = dict(row)
            cwd = row.get("cwd")
            project, source, note = _project_of_cwd(self.project_of, cwd)
            stored_project = row.get("project") or ""
            downgrade = (source == PROJECT_UNMAPPED and not allow_downgrade
                         and stored_project
                         and row.get("project_source") == PROJECT_MAPPED)
            if downgrade:
                # 已映射 → 未映射：保留写时的归属，并把这件事记进告警（见 docstring）。
                counters["kept_mapped"] += 1
                counters["warnings"].append(
                    "%s:%s 重算不出映射（库里的 cwd 已脱敏）——保留写时的项目 %r；"
                    "如确要取消映射，请显式传 allow_downgrade=True"
                    % (row["host"], row["session_id"], stored_project))
                project, source = stored_project, PROJECT_MAPPED
                note = row.get("project_note")
            elif source == PROJECT_UNMAPPED and not note:
                note = _note_candidates(self.candidates_of, cwd)
            note = _safe(note)                 # 与 `tag_session` 同口径（可回显列）
            if self.citizen_of is None:
                # 没注入解析器：`_identity_of` 只会给 `(None, None)`，写回去就是**抹身份**。
                resolved = (None, None)
            else:
                resolved = self._identity_of(row["host"], row["session_id"])
            if resolved[0]:
                citizen_id, identity_source = resolved
            else:
                # 解析器没给出公民（含"查不到这个会话键"）：保留原列，与 `kept_mapped`
                # 同一条道理——重算不该让库变差；清身份要显式改库，不是重算的副作用。
                citizen_id, identity_source = row.get("citizen_id"), row.get("identity_source")
                if citizen_id or identity_source:
                    counters["kept_identity"] += 1
            counters["sessions"] += 1
            counters[source] = counters.get(source, 0) + 1
            workspace = _safe(fish_surface.normalize_path(cwd)) if cwd else ""
            # `workspace` 也进比对（2026-10-27 复审 🟡-18）：它落在 SQL 的 UPDATE 里，
            # 漏掉它就会出现"改了 workspace 却说 changed=0"——一次静默的不生效。
            # `identity_source` 同理（终验黄项）：两列一起写，就得一起比；否则
            # "身份来路变了但 changed=0" 又是同一种静默。
            if (row.get("project"), row.get("project_source"), row.get("project_note"),
                    row.get("citizen_id"), row.get("identity_source"),
                    row.get("workspace")) != (
                    project, source, note, citizen_id, identity_source, workspace):
                counters["changed"] += 1
                updates.append((project, source, note, citizen_id, identity_source,
                                workspace, row["host"], row["session_id"]))
        if dry_run:
            counters["planned"] = len(updates)
            return counters
        for args_row in updates:
            conn.execute(
                "UPDATE corpus_session SET project=?, project_source=?, project_note=?,"
                " citizen_id=?, identity_source=?, workspace=? WHERE host=? AND session_id=?",
                args_row)
        conn.commit()
        counters["planned"] = len(updates)
        return counters


def tag_session(header: dict, host: str = "dsh", **kwargs) -> dict:
    """模块级便捷入口（等价于 `Tagger(**kwargs).tag_session(header, host)`）。"""
    return Tagger(**kwargs).tag_session(header, host)
