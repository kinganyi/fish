#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fish · PreToolUse hook —— **撞车提示**（三态：`off` / `warn`（默认） / `ask`）。

## 为什么默认是 `warn` 而不是 `ask`（用户 2026-10-26 拍板 + 主理人决策）

`ask` 在「批准被禁用/自动拒绝」的宿主环境里会被**自动拒绝**——于是"撞车提示"
变成了"撞车拒写"，那正好违背它想达成的目的（提醒人协调，而不是打死工作流）。
所以默认 `warn`：**不阻塞**，靠三条更温和的渠道提醒：

  · `activity_overlap` 推送候选（别的会话 30 分钟内写过我声明的面 → 工具后递一句）；
  · 看板「观察到的重叠（推断）」一节；
  · **下一次 PostToolUse 的 `additionalContext`**（评审 L3 的修复点）：warn 时往
    `_audit/hooks/warn-pending-<会话哈希>.json` 留一个一次性标记，PostToolUse 读走
    并递一句人话——模型看得见、不阻塞、只发一次、文件留着可审计。

只有把 `board.collision_ask` 显式改成 `ask`（且宿主确实开着批准），才输出
`permissionDecision="ask"`。**`off` 什么都不留**（连 pending 标记也不写）。

## 三条边界（缺一条就会变成"打扰源"）

  1. **只问写类工具**：读不会撞车。matcher 已筛一遍，这里是双保险。
  2. **只在真重叠时开口**：快照缺失/损坏/过旧、无身份、路径解析不出、冷却中
     → **零输出 = 放行**。绝不 `deny`。
  3. **可解释**：原因里带名字 + 项目 + 路径 + 分钟数；**不含 ULID、不含内容**。

## 撞车面上的**代理维**（113 §9.3 · W5）

命中行多一列 `agent_role`（来自 `session_lineage.agent_role`）：同一个人名下两个代理
改同一块时，"是谁的哪个代理"比"是谁"更接近该找谁协调。它有两处刻意的**限制**：

  · **离线投影**：读的是 `session_lineage`（宿主日志的投影），不是实时状态；
  · **三态分明**：`agent_role_basis='projection'` = 查过投影（值可能 `None` = 投影里
    没这一行）；`'missing'` = **没查**（无会话键 / 前缀不属已知宿主 / 表不存在）。
    两者都不许用会话键或 `main` 冒充代理。

为什么这里自己写一条 SQL 而不 import `fish_workbench_api.lineage_of`：那条路 import
**390 ms**（实测：拖进 http.server / fish_hosts / fish_push_audit），而本 hook 每次工具
调用都要跑。撞车命中是稀有路径，但把 390 ms 装进 hook 是拿热路径换复用。

## 审计

每次"决定要问/要提醒"都往 `_audit/hooks/pretool-asks.jsonl` 追加一行
（`{ts, session_key, target, decision, hits}`）。误报率就是用这个文件算的——
没有它，"撞车误报"只能靠感觉。
"""
from fish_common import warn_ignored
import functools
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hook_io  # noqa: E402 —— 统一 stdout 出口（编码只在一处决定）
import fish_activity  # noqa: E402 —— 快照/撞车/审计的共享核心（不 import fish）
import fish_surface  # noqa: E402 —— 路径归一（唯一一份）
import hook_fastpath  # noqa: E402 —— P0-⑥ 快通道（关闭态秒退/冷却；只 stdlib）

# 数据目录：默认本目录下的 data；可用 FISH_DATA 覆盖（测试隔离用）。
DATA = Path(os.environ.get("FISH_DATA") or (HERE / "data"))
SNAPSHOT_PATH = DATA / fish_activity.SNAPSHOT_RELPATH
ASKS_PATH = DATA / fish_activity.ASKS_RELPATH


#: 本 hook 的缺省宿主（历史值：DSH 的 PreToolUse 入口）。命令行总是显式带 `--host`。
DEFAULT_HOST = "dsh"
#: 诊断输出：实现在 `hook_io.dbg`（全仓唯一一份）；这里只绑 tag。
_dbg = functools.partial(hook_io.dbg, "fish_pretool_hook")


def _emit_decision(reason: str) -> None:
    """输出 `ask` 决策（委托统一出口：协议与编码只在一处决定）。"""
    hook_io.emit_decision("PreToolUse", "ask", reason)


def describe_hits(hits, target_abs, *, limit: int = 2, question: bool = True) -> str:
    """把命中列表拼成一句**人能立刻行动**的话（最多 2 个来源，不含 ULID/内容）。

    `question=False` 用于 `warn`（评审 L3）：warn 不阻塞、写已经要发生/已经发生，
    再问"确认要覆盖/合并吗？"就是**问一个没有答案的问题**——改成陈述式提醒，
    提醒本身由下一次 PostToolUse 递到模型眼前（见 `fish_activity.set_warn_pending`）。
    """
    parts = []
    for hit in hits[:limit]:
        # QA R2-S3 / 设计 D7：跨多块板时展示「·多项目」，让人看得出这条命中的归属。
        multi = "·多项目" if len(hit.get("projects") or ()) > 1 else ""
        where = f"（{hit['project']}{multi}）" if hit.get("project") else ""
        surface = hit.get("surface") or ""
        minutes = hit.get("minutes_ago")
        when = f"{minutes} 分钟前" if isinstance(minutes, int) else "刚刚"
        suffix = "·另一会话" if hit.get("another_session") else ""
        # 2026-10-26（撞车自撞修复 T03 · 设计 §4.3）：
        #   · 同公民的**另一会话**：保留既有 `·另一会话` 后缀（既有判据按它断言），
        #     再补一句"你名下的"——否则人会怀疑"我怎么会跟我自己撞"；
        #   · 会话**未标注**（`session_key=''`）：照报，但说清"不知道是哪个会话"。
        #     '' 不猜（设计 §3.3 的不对称代价：漏报 > 多一句"可能是你自己"）。
        if hit.get("another_session"):
            note = "（你名下的另一会话）"
        elif hit.get("session_unlabeled"):
            note = "（会话未标注：可能是你自己的另一条路径）"
        else:
            note = ""
        # P5-2（2026-11-20）：判不到仓库锚点时，这条命中**可能只是同名路径**。
        # 必须标出来——「警告掺假就会被学会忽视」是这个文件里已有的原话，
        # 而不标注的后果正是把假撞车当成真撞车。
        if hit.get("repo_unanchored"):
            note += "（未锚定仓库，可能是误报：两边至少一侧没有仓库信息）"
        # 113 §9.3（W5）：代理维**只在判得到时**说，且必须标"离线投影·非实时"——
        # 拿一张定期回填的投影当实时状态报，就是在制造"刚才明明是它"的争论。
        if hit.get("agent_role"):
            note += f"（代理：{hit['agent_role']}·离线投影，非实时）"
        parts.append(f"{hit['label']}{suffix}{where} {when}写过 {surface}{note}")
    if len(hits) > limit:
        parts.append(f"另有 {len(hits) - limit} 个会话")
    short_target = fish_surface.relativize(target_abs, None) or target_abs
    tail = (f"。你要写 {short_target}。确认要覆盖/合并吗？" if question
            else f"。你要写 {short_target}——注意协调，别互相覆盖。")
    return "别的会话正在写这块：" + "；".join(parts) + tail


def evaluate(payload, *, host="dsh", data_dir=None, now=None) -> dict:
    """纯判定（不输出、不写审计）。返回 `{mode, decision, reason, hits, target_abs}`。

    `decision` 取值：`off` / `skip`（不该问）/ `warn` / `ask`。
    把判定与副作用拆开是为了让判据能直接测"该不该问"，而不必捕获 stdout。
    """
    data_dir = Path(data_dir or DATA)
    result = {"mode": "?", "decision": "skip", "reason": "", "hits": [],
              "target_abs": "", "session_key": ""}
    if not isinstance(payload, dict):
        return result
    tool_name = payload.get("tool_name") or ""
    if not fish_surface.is_write_tool(tool_name):
        result["decision"] = "skip"
        result["reason"] = "非写类工具"
        return result
    mode = str(fish_activity.read_cfg(data_dir, "board.collision_ask"))
    result["mode"] = mode
    if mode == "off":
        result["reason"] = "board.collision_ask=off"
        return result
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    raw_path = ""
    for field in ("file_path", "notebook_path", "path"):
        value = tool_input.get(field)
        if isinstance(value, str) and value.strip():
            raw_path = value.strip()
            break
    if not raw_path:
        result["reason"] = "没有可解析的目标路径"
        return result
    cwd = payload.get("cwd") or ""
    target_abs = fish_surface.make_abs(raw_path, cwd)
    if not target_abs:
        result["reason"] = "目标路径归一后为空"
        return result
    session_key = fish_activity.make_session_key(host, payload.get("session_id"))
    result["session_key"] = session_key

    max_age = int(fish_activity.read_cfg(data_dir, "activity.snapshot_max_age_sec"))
    snapshot = fish_activity.read_snapshot(data_dir / fish_activity.SNAPSHOT_RELPATH,
                                           max_age_sec=max_age)
    if not snapshot:
        result["reason"] = "快照缺失/损坏/过旧"
        return result

    # ── P1-1（2026-11-20）：目标路径必须能绝对化，否则不判 ──────────────────
    # 缺 cwd 时 `make_abs` 原样返回相对串，而 `find_collisions` 的 `path_hit` 会拿
    # 相对面去做「分量链包含」匹配**绝对**目标——那正是假撞车（实测记录
    # `warn-pending-6f4bbb96.json`：surface 是 CZHY 工作区相对路径，target 是
    # 飞书建设工作空间的绝对路径）的成因。
    #
    # 兜底顺序：payload 的 cwd → 快照里本会话的 cwd（P1-3 落库后由 build_snapshot
    # 带出）→ 都拿不到就 skip。**宁可漏报，不要假撞车**——假撞车比漏报更坏，
    # 因为警告一旦掺假就会被学会忽视（fish_activity.py 里老注释的原话）。
    if not fish_surface.is_absolute(target_abs):
        for _e in (snapshot.get("entries") or ()):
            if isinstance(_e, dict) and _e.get("session_key") == session_key:
                _scwd = _e.get("cwd") or ""
                if _scwd:
                    target_abs = fish_surface.make_abs(raw_path, _scwd)
                break
    if not fish_surface.is_absolute(target_abs):
        result["reason"] = "目标路径无法绝对化（缺 cwd 且路径是相对的），跳过撞车判定"
        return result
    result["target_abs"] = target_abs
    min_segments = int(fish_activity.read_cfg(data_dir, "board.collision_min_segments"))
    # ── P5-2（2026-11-20）：我自己的仓库锚点 ──────────────────────────────
    # 从**快照**里取（不额外连库）：如果我自己的 entry 里带 repo_id，就按它做
    # 「不同仓库一律不冲突」的前置过滤；取不到就传 None，判定退化为纯路径匹配，
    # 且结果会被标注「未锚定仓库，可能是误报」。
    target_repo_id = None
    for _entry in (snapshot.get("entries") or ()):
        if isinstance(_entry, dict) and _entry.get("session_key") == session_key:
            target_repo_id = _entry.get("repo_id")
            break
    hits = fish_activity.find_collisions(
        snapshot, target_abs=target_abs, own_session_key=session_key,
        min_segments=min_segments, now=now, target_repo_id=target_repo_id,
        declared_window_min=int(fish_activity.read_cfg(data_dir, "board.overlap_window_min")),
        auto_window_min=int(fish_activity.read_cfg(data_dir, "board.collision_window_min")))
    if not hits:
        result["reason"] = "无重叠"
        return result
    # 「同一公民的另一会话」**不排除**（两个窗口同改一个文件是真撞车），但要标出来，
    # 否则人会怀疑"我怎么会跟我自己撞"。
    own_citizen = own_citizen_of(data_dir, session_key)
    # 113 §9.3（W5）：撞车面的**代理维**（离线投影，非实时；判不到就是 None）。
    roles, role_basis = _agent_roles_of(data_dir, [h.get("session_key") for h in hits])
    for hit in hits:
        hit["another_session"] = bool(own_citizen) and hit.get("citizen_id") == own_citizen
        hit["agent_role"] = roles.get(hit.get("session_key") or "")
        hit["agent_role_basis"] = role_basis
    result["hits"] = hits
    result["decision"] = "ask" if mode == "ask" else "warn"
    result["reason"] = describe_hits(hits, target_abs, question=result["decision"] == "ask")
    return result


def own_citizen_of(data_dir, session_key):
    """本会话属于哪个公民（只读、失败返回 None）。仅用于标「另一会话」。"""
    if not session_key:
        return None
    conn = None
    try:
        import sqlite3
        db = Path(data_dir) / "index.sqlite"
        if not db.exists():
            return None
        conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=2.0)
        return fish_activity.resolve_citizen(conn, session_key)
    except (sqlite3.Error, OSError):
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error as exc:
                warn_ignored('fish_pretool_hook.py:195 own_citizen_of', exc)


#: 会话键前缀 → 会话树宿主。与 `fish_workbench_api._LINEAGE_HOSTS` 同一口径，但刻意
#: 各存一份：那个模块 import 390 ms（拖进 HTTP 面），而本文件是每条工具调用都要跑的 hook。
_LINEAGE_HOSTS = {"dsh": "dsh", "workbuddy": "workbuddy", "codebuddy": "workbuddy"}


def _agent_roles_of(data_dir, session_keys):
    """撞车面上的一列 `agent_role`（113 §9.3）：**离线投影，非实时**。

    返回 `(roles, basis)`：`basis='projection'` = 查过（值可以是 `None`，= 投影里没有
    这一行）；`basis='missing'` = **没查**（没有会话键 / 前缀不属已知宿主 / 库或表不在）。
    只读连接、只查一列，失败一律落到 `missing`（hook 绝不能因为这张表挂了而阻塞工具）。
    """
    keys = [k for k in dict.fromkeys(session_keys or ()) if k]
    if not keys:
        return {}, "missing"
    pairs = []
    for key in keys:
        proto, _sep, ident = str(key).partition(":")
        table_host = _LINEAGE_HOSTS.get(proto.strip().lower())
        if table_host and ident.strip():
            pairs.append((key, table_host, ident.strip()))
    if not pairs:
        return {}, "missing"
    conn = None
    try:
        import sqlite3
        db = Path(data_dir) / "index.sqlite"
        if not db.exists():
            return {}, "missing"
        conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=2.0)
        where = " OR ".join("(host = ? AND session_id = ?)" for _ in pairs)
        params = [v for _k, host, ident in pairs for v in (host, ident)]
        rows = conn.execute(
            f"SELECT host, session_id, agent_role FROM session_lineage "
            f" WHERE {where}", params).fetchall()
        index = {(r[0], r[1]): r[2] for r in rows}
        roles = {key: index.get((host, ident))
                 for key, host, ident in pairs}
        return roles, "projection"
    except (sqlite3.Error, OSError):
        return {}, "missing"
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error as exc:
                warn_ignored('fish_pretool_hook.py:_agent_roles_of', exc)


def main(argv=None) -> int:
    """入口包装：只多做一件事——把**自测耗时**记进冷却状态（🔴3 残留，2026-11-11）。

    为什么包在外面而不是塞进 `_run` 的每条 return：`_run` 有十来条提前返回路径，
    逐条加一行必然漏一条；包一层能把"每次执行都记一次"变成**结构保证**。

    这一层补的是外部审查 🔴3 的最后一个残口：`hook_fastpath.cooldown_remaining`
    一直在查冷却，但**从来没人写入**冷却状态，于是"连续超时退避"永不武装。
    超时是宿主杀进程、hook 等不到那个事件，所以只能自测耗时（见 `note_elapsed`）。
    """
    t0 = time.time()
    try:
        return _run(argv)
    finally:
        try:
            hook_fastpath.note_elapsed(DATA, "pretool", t0)
        except Exception:      # noqa: BLE001 —— 记冷却失败绝不能影响工具放行
            pass


def _run(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    host = hook_io.host_from_argv(argv, DEFAULT_HOST)
    # P0-⑥（外部审查 🔴3）：关闭态秒退 + 冷却（与 posttool 同一口径）。
    if not hook_fastpath.enabled(DATA, "push.enabled", True):
        return 0
    if hook_fastpath.cooldown_remaining(DATA, "pretool"):
        return 0
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:      # noqa: BLE001
        pass
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        fish_activity.heartbeat(DATA, event="PreToolUse", host=host, bad_payload=True)
        return 0
    fish_activity.heartbeat(DATA, event="PreToolUse", host=host,
                            tool_name=str((payload or {}).get("tool_name") or ""),
                            session=fish_activity.make_session_key(
                                host, (payload or {}).get("session_id")))

    res = evaluate(payload, host=host, data_dir=DATA)
    if res["decision"] not in ("warn", "ask") or not res["hits"]:
        _dbg("skip: %s" % res["reason"])
        return 0

    cooldown = int(fish_activity.read_cfg(DATA, "board.collision_ask_cooldown_sec"))
    if fish_activity.recent_ask(ASKS_PATH, session_key=res["session_key"],
                                target_abs=res["target_abs"], cooldown_sec=cooldown):
        _dbg("冷却中，不再问")
        return 0

    fish_activity.log_ask(ASKS_PATH, {
        "ts": fish_activity.utc_now(),
        "session_key": res["session_key"],
        "target": res["target_abs"],
        "mode": res["mode"],
        "decision": res["decision"],
        "hits": [{"label": h.get("label"), "project": h.get("project"),
                  "surface": h.get("surface"), "minutes_ago": h.get("minutes_ago"),
                  "agent_role": h.get("agent_role"), "kind": h.get("kind")}
                 for h in res["hits"]],
    })
    if res["decision"] == "ask":
        _emit_decision(res["reason"])
    else:
        # warn：**不输出决策**（不阻塞），但也不能"什么都不留"——
        # 模型看到的零输出与"根本没撞车"无法区分（评审 L3）。留一次性标记，
        # 由下一次 PostToolUse（就是这次调用的收尾）递一句 additionalContext。
        fish_activity.set_warn_pending(
            DATA, session_key=res["session_key"], reason=res["reason"],
            target_abs=res["target_abs"], hits=res["hits"])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:      # noqa: BLE001 —— 兜底：hook 异常一律放行（工具照常执行）
        sys.exit(0)
