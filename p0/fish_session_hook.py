#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fish · SessionStart hook（WorkBuddy / CodeBuddy 侧）
===================================================
把「权威当下」注入到每次会话开始。规范依据（2026-10-22 核对）：
CodeBuddy Hooks 文档 § SessionStart 决策控制：
  {"hookSpecificOutput": {"hookEventName": "SessionStart",
                          "additionalContext": "..."}}

两种模式（用环境变量 FISH_HOOK_MODE 切换）：
  marker  → 只输出一行固定标记，用于确认 hook 是否被触发（默认）
  inject  → 输出真实的注入包（L0+L1+L2，硬预算）

stdout 只允许输出 hook 协议要求的 JSON；日志走 stderr。

## 2026-10-25（EVAL-261025-19）：身份来源从"硬编码 FISH_ACTOR"改为"会话键查库"

旧实现用 `FISH_ACTOR`（缺省硬编码 `workbuddy-main`）给子进程定身份——
于是**任何会话的注入包都宣称自己是 workbuddy-main**，而该会话真正的 token
可能属于另一个公民。评审把它列为 EVAL-19。

新实现只做两件事：
  1. 用 payload 的 `session_id` 拼出会话键（`codebuddy:<id>`，与 MCP/CLI 同口径），
     查 `session_tokens` / `citizens_all` 里**已经有**的公民；
  2. 查到就把 `--citizen <id>` 传给 inject；查不到就**诚实降级**（输出可执行提示），
     绝不编一个身份。

**为什么当时不在这里调 `fish session start`**：`start_session` 会轮换（吊销）同一
session_key 的旧凭证——hook 每次会话开始都签一次，会把 MCP 侧刚签发的 token
一起吊销。"查身份"不该有写副作用。

## 2026-11-25（113 W4.6 §7）：上面那条理由解决了 → 查不到就 `session ensure`

W4 把 `start`（人驱动、会轮换）与 `ensure`（hook 用、**幂等、绝不轮换**）拆开了：
`ensure` 有活凭证就原样复用，没有才补签一条。于是"hook 签发"不再等于"互踢"，
只查不签也就不再必要——**只查不签的代价是这些会话永远拿不到身份**：7d 内
WorkBuddy prompt 推送 243/317 行「无身份」（2026-11-25 02:5x 设计快照；同日晚
00:3xZ 复读已漂到 238/1,398 —— 判据只锚「剩余为 0」，不锚这些数字）。

现在三支行为：
  · **已有身份** → 只读查询后直接返回（稳态零写，与旧行为逐字节相同）；
  · **父证据在 payload 里**（113 §7.1）：
      - `transcript_path` = `<父>/subagents/agent-<uuid>.jsonl` → 父会话 id
        （反解只有一处：`fish_lineage_wb.path_parent`，本文件不写第二份正则）；
      - `agent_id`（= storeId，子代理才有）→ 只作"这是子代理"的信号，**不当会话键**
        （实测 7d 推送键 12/12 都是会话目录名，没有一个 `agent-*`；键口径不改）；
    先按父链**由远及近** ensure（`seen` + 跳数上限防环），再 ensure 自己：父有身份 →
    子继承父公民（`session_identity.origin='inherited'`、凭证 `inherited`/`parent`）；
    父链整条都判不到 → `provisional`，**绝不造 machine**（宁可不推，不串推）。
  · **判不到任何父** → 当根会话 ensure（`direct`/machine，宿主 session_id 是机器事实）。

紧急关停（两级，方向单一）：
  · `identity.wb_ensure=false`（配置键，W4.6b 登记，默认 true）；
  · `FISH_WB_ENSURE=0`（环境变量，应急口——**只能关，不能开**）。
  判定只有一处：`fish_common.wb_ensure_enabled`。本文件不再自己解析一遍
  "0/false/off"——两份解析就是两次漂移的机会（W4.5 的闸门就是因为
  "docstring 与实现各写一遍"才出现 501 行假绿）。

闸门（W4.7）**没有切**——这是取证结论，不是拖延：`_audit/hook-calls.jsonl` 里
`ensured` 记录 = 0（W4.6 落地后还没有任何真实 SessionStart 走过 ensure），而
7d 内 WB prompt 推送 238/312 行无身份，此刻切 `skip` 会把这 238 行全挡掉。
所以档位保持 `allow`，等宿主重启后的首批真实 ensure 记录再按「≥95%」判据决定。
开关与判据都已落地（"ready, awaiting host restart"）。
组合告警 J-W46-3：`identity.wb_ensure=false` × `push.no_identity=skip` 会
让 WB 推送面归零——由 `fish_common.config_combo_warnings` 判定，本文件负责
**把它喊出来**（stderr + 审计 note），而不是让它在库里躺着。
"""
import fish_common              # 开关的唯一判定点（`wb_ensure_enabled`）
from fish_common import config_combo_warnings, warn_ignored
from hook_io import log_child_stderr  # noqa: F401
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hook_io  # noqa: E402 —— 统一 stdout 出口（编码只在一处决定）

PY = sys.executable
FISH = HERE / "fish.py"
# 数据目录：默认本目录下的 data；可用 FISH_DATA 覆盖（与 fish_push_hook 同口径，便于隔离测试）。
DATA = Path(os.environ.get("FISH_DATA") or (HERE / "data"))
# ack 探针已验证通过（2026-10-22 05:13，两个会话均输出 FISH-HOOK-ACK）
# 现默认 inject：注入真实的「权威当下」
MODE = os.environ.get("FISH_HOOK_MODE", "inject")

#: hook 调的会话子命令（113 W4.6）。**只此一处**：`test_wb_identity` 的变异 M1
#: 把它换成 `start` 来证明"退回轮换口径 → 同会话两公民 → J4 必红"。
ENSURE_VERB = "ensure"
#: 父链跳数上限。WB 树只有"会话 → 子代理"一层，上限是给防环/防病态数据用的
#: （与 `fish_lineage.ancestor_sessions(max_hops=64)` 同口径）。
MAX_PARENT_HOPS = 64
#: 血缘表的 host 值。**不是**会话键前缀：`fish_hosts_vocab.PREFIX_OVERRIDES`
#: 把 `workbuddy` 映射成 `codebuddy:` 前缀（历史原因，两宿主同族），
#: 而 `session_lineage.host` 用适配器名 `workbuddy`（113 §2.3 值域）。
WB_LINEAGE_HOST = "workbuddy"
#: 会话键前缀。**必须**等于 `fish_activity.make_session_key("workbuddy", sid)` 的前缀
#: ——推送侧就是用它解析公民的（`fish_push_hook.py` / `cmd_prompt_push`），
#: 对拍判据在 `test_wb_identity.KeyContractTest`（改了它就红）。
WB_KEY_PREFIX = "codebuddy:"


def _emit(text: str) -> None:
    """输出 additionalContext（委托统一出口：编码只在一处决定）。"""
    hook_io.emit("SessionStart", text)


def _key_of(session_id) -> str:
    """会话 id → 会话键（与 MCP/CLI 同口径；空 id → 空串，**不编一个**）。"""
    sid = str(session_id or "").strip()
    return (WB_KEY_PREFIX + sid) if sid else ""


def _read_identity(conn, key):
    """该会话键**已有**的公民 id（只读，不签发）；没有 → `None`。

    与旧实现逐字同口径（`session_tokens` 活凭证 → `citizens_all.host_session_key`）。
    第三级（`session_identity`）刻意**不**在这里查：那一级正是"凭证过期/继承会话"的
    语义，交给 `ensure` 去判——它知道该不该补签，也知道父链在哪。

    **读失败一律返回 None**（不抛）：全新的库还没建表，而建表/迁移正是 `session ensure`
    的第一步（它跑 `SCHEMA` + `migrate`）。在这里抛出去会把"首次启动"变成
    "注入失败"——实测过：干净目录下第一条 SessionStart 就是这么降级的。
    """
    if not key:
        return None
    try:
        row = conn.execute(
            "SELECT citizen_id FROM session_tokens WHERE session_key=? "
            "AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ?) "
            "ORDER BY issued_at DESC LIMIT 1",
            (key, fish_now())).fetchone()
        if not row:
            row = conn.execute(
                "SELECT citizen_id FROM citizens_all WHERE host_session_key=? "
                "AND status='active' ORDER BY created_at LIMIT 1",
                (key,)).fetchone()
        return row[0] if row else None
    except Exception as exc:      # noqa: BLE001 —— 没表/没库都只是"判不到身份"
        warn_ignored('fish_session_hook.py:_read_identity', exc)
        return None


def fish_now():
    """`fish.now_utc()` 的迟延导入（本 hook 不 import fish 到模块顶层：启动要快）。"""
    import fish
    return fish.now_utc()


def _payload_facts(req):
    """payload → 宿主事实：`{session_id, parent, agent_id, transcript, same_dir}`。

    `parent` 只在**路径给得出父、且父不是自己**时才有值（113 §7.1）。反解委托
    `fish_lineage_wb.path_parent`——那是"路径即父"的唯一实现，本文件不写第二份正则。

    `same_dir` = 路径是 `<本会话 id>/subagents/agent-x.jsonl`：这时"路径即父"反解出来的
    父就是自己。它不是父，但**是证据**——这条路径证明本 id 是一个真实会话目录
    （子代理挂在它下面），所以身份按根会话的机器事实走，别把它降成 provisional。
    """
    req = req or {}
    sid = str(req.get("session_id") or "").strip()
    transcript = str(req.get("transcript_path") or "").strip()
    agent_id = str(req.get("agent_id") or "").strip()
    parent = None
    if transcript:
        try:
            import fish_lineage_wb
            parent = fish_lineage_wb.path_parent(transcript)
        except Exception as exc:      # noqa: BLE001 —— 反解失败只影响继承，不影响注入
            warn_ignored('fish_session_hook.py:_payload_facts', exc)
            parent = None
    if not agent_id and transcript:
        # `agent_id` 缺席时用 transcript 文件名兜底判"这是子代理"（两条都是宿主事实）
        stem = Path(transcript).stem
        agent_id = stem if stem.startswith("agent-") else ""
    parent = (parent or "").strip()
    same_dir = bool(parent) and parent == sid
    if not parent or same_dir:        # 父 == 自己 → 不当父用（会自指、会绕圈）
        parent = None
    return {"session_id": sid, "parent": parent, "agent_id": agent_id or None,
            "transcript": transcript, "same_dir": same_dir}


def _ensure_enabled(conn) -> tuple:
    """`(能不能 ensure, 关掉的原因)` —— 判定**委托** `fish_common.wb_ensure_enabled`。

    为什么需要 `conn`：开关是一个配置键（`identity.wb_ensure`），要读库。这正是
    W4.6b 与 W4.6 的差别——W4.6 只有环境变量，读不到"用户想不想开"。

    为什么不在本文件实现：判定写两遍，就会有一遍在读配置、另一遍在读环境变量，
    然后某天两遍给出相反答案。本仓为这类"同一决定多份实现"付过三次账
    （`hook_io.py` 头记着那四次 `json.dumps`）。
    """
    try:
        return fish_common.wb_ensure_enabled(conn)
    except Exception as exc:      # noqa: BLE001 —— 判不到开关按缺省（开）
        warn_ignored('fish_session_hook.py:_ensure_enabled', exc)
        return True, ""


def _fields_of(stdout: str) -> dict:
    """解析 `session ensure` 的 `key: value` 契约面（与 `fish_dsh_hook` 同一手法）。"""
    out = {}
    for line in (stdout or "").splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if key and val:
            out[key] = val
    return out


def _ensure_via_cli(session_id, *, parent=None, agent_id=None, cwd=None):
    """调 `fish session <ENSURE_VERB>`（幂等、不轮换），返回字段字典；失败 → `None`。

    两个必须：① `--root` 是**全局参数**，要在子命令之前（`argparse` 的硬规则）；
    ② 宿主会话 env 必须**覆盖**（payload 才是本次事件的会话，与 `fish_dsh_hook`
    同一条教训）；`DSH_SESSION_ID`/`CLAUDE_SESSION_ID` 优先级更高，必须摘掉，
    否则 identity 会串到调用者的会话上。
    """
    cmd = [PY, str(FISH), "--root", str(DATA), "session", ENSURE_VERB]
    if parent:
        cmd += ["--parent-session", str(parent)]
    if agent_id:
        cmd += ["--agent-id", str(agent_id)]
    if cwd:
        cmd += ["--cwd", str(cwd)]
    env = {k: v for k, v in os.environ.items()
           if k not in ("DSH_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID")}
    env["CODEBUDDY_SESSION_ID"] = str(session_id)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, timeout=60)
    except Exception as exc:          # noqa: BLE001 —— 起不来也只是一次失败
        return None
    log_child_stderr("fish_session_hook.py:ensure", proc.stderr)
    if proc.returncode != 0:
        return None
    return _fields_of(proc.stdout)


def _plan_chain(conn, child_sid, parent_sid):
    """父链**由远及近**：返回 `(待 ensure 的祖先[远→近], 最近有身份的祖先|None)`。

    只在父链上做两件事：① 用 `fish_lineage.parent_of`（唯一判父实现，投影表优先、
    没行才现读宿主）向上走，`seen` 防环、`MAX_PARENT_HOPS` 防病态数据；
    ② 已经**有身份**的祖先不再碰——继承只认最近的那一个，它上面的都无关。
    """
    ancestors, seen = [], {str(child_sid or "")}
    cur = str(parent_sid or "").strip()
    if cur and _read_identity(conn, _key_of(cur)):
        # 常见路径（父会话自己的 SessionStart 早就签过了）：**一次 DB 读**就够，
        # 不去碰 `parent_of` 的现读宿主（WB 适配器要读 runtime + 会话库，几百毫秒）。
        return [], cur
    while cur and cur not in seen and len(ancestors) < MAX_PARENT_HOPS:
        ancestors.append(cur)                # 近 → 远
        seen.add(cur)
        try:
            import fish_lineage
            cur = fish_lineage.parent_of(conn, WB_LINEAGE_HOST, cur) or ""
        except Exception as exc:      # noqa: BLE001 —— 判不到就是没有更远的祖先
            warn_ignored('fish_session_hook.py:_plan_chain', exc)
            cur = ""
    for idx in range(len(ancestors) - 1, -1, -1):
        if _read_identity(conn, _key_of(ancestors[idx])):
            return list(reversed(ancestors[idx + 1:])), ancestors[idx]
    return list(reversed(ancestors)), None


def _session_citizen(req):
    """会话公民：**已有就用**，没有就 `ensure`（幂等、不轮换）。返回 `(cid, why, note)`。

    `note` 只进审计文件（`_audit/hook-calls.jsonl`），用来把"这次 payload 长什么样"
    变成可回看的证据——WB 的真实 payload 至今只有 bundle 级证据（113 §0.4），
    这里顺手把它记下来（不记 token、不记正文）。
    """
    facts = _payload_facts(req)
    sid, key = facts["session_id"], _key_of(facts["session_id"])
    if not sid:
        return None, "payload 里没有 session_id", ""
    try:
        import fish
        conn = fish.connect(DATA)
        try:
            cid = _read_identity(conn, key)
            if cid:
                return cid, None, f"existing citizen={cid}"
            plan, anchor = _plan_chain(conn, sid, facts["parent"])
            # W4.6b：开关与组合体检都要**读库**，所以在连接还开着的时候判——放在
            # `finally: conn.close()` 之后就只能再开一次库（或干脆硬编码，那更糟）。
            enabled, why_off = _ensure_enabled(conn)
            combos = config_combo_warnings(conn)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 —— 查身份失败不能拖垮会话
        return None, f"{type(exc).__name__}: {exc}", ""
    if not enabled:
        # J-W46-3：组合告警**必须出声**。它是"再点一次会归零"的唯一提醒，
        # 只写进库或只写进 note 都等于没喊——stderr 进 `hook-stderr.jsonl`，
        # note 进 `hook-calls.jsonl`，两条都可 grep。
        warn = "；".join(str(c.get("why") or "") for c in combos if c.get("why"))
        note = f"ensure-disabled {why_off}"
        if warn:
            sys.stderr.write(f"⚠ 危险配置组合：{warn}\n")
            sys.stderr.flush()
            note += f" | ⚠ 危险配置组合：{warn}"
        # 只给**原因**：`main()` 会再套一层「该会话尚未登记（…）」。旧写法在这里
        # 又写了一遍整句，日志里于是出现「该会话尚未登记（该会话尚未登记（…））」。
        return None, f"ensure 已关：{why_off}", note

    cwd = str((req or {}).get("cwd") or "").strip()
    previous = anchor
    for node in plan:                 # 由远及近：先让祖先有身份，子才有东西可继承
        got = _ensure_via_cli(node, parent=previous, cwd=cwd)
        if not got:
            return None, f"父会话 {node} ensure 失败", "ensure-parent-failed"
        previous = node
    fields = _ensure_via_cli(
        sid, parent=(plan[-1] if plan else (facts["parent"] or None)),
        # `same_dir` 已在 `_payload_facts` 里把父折成 None：那说明本 id 就是会话目录，
        # 按根会话走机器事实，别再拿 `agent_id` 把它降成 provisional。
        agent_id=(None if facts["same_dir"] else facts["agent_id"]), cwd=cwd)
    if not fields:
        return None, "session ensure 失败", "ensure-failed"
    note = (f"ensured citizen={fields.get('citizen_id')} "
            f"identity={fields.get('identity')} "
            f"token_source={fields.get('token_source')} "
            f"agent={facts['agent_id'] or '-'} parent={facts['parent'] or '-'}")
    cid = fields.get("citizen_id")
    if not cid:
        return None, "session ensure 没有返回 citizen_id", note
    return cid, None, note


def session_key_from(req):
    """payload → 会话键（`codebuddy:<session_id>`，与 `_session_citizen` 同口径）。

    没有 `session_id` 时返回 `None`（**不编一个**：编出来的键会造出一行假在场）。
    113 §7.1：WB 侧**不改口径**——宿主报的就是这个 id（子代理会话报的是父会话 id，
    实测 7d 推送键 12/12 都是会话目录名），改口径会让推送侧查不到同一个键。
    """
    return _key_of((req or {}).get("session_id")) or None


def build_pack(citizen_id, session_key=None):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    # 2026-10-26（T03）：`--auto-presence` 让会话开场**自动签到**（自动行算登记，
    # 用户拍板）。它只在显式传时写，所以 MCP 的 `tool_inject` 不会因为"打开看板"
    # 就刷新在场；这里传是因为 WorkBuddy 没有 `session start` 那条自动签到路径。
    # ⚠️ stdout 是 `--json` 的契约面：自动签到**不得打印任何东西**（fish 侧已保证）。
    # 2026-10-26（QA Round 1 · E2）：签到之后**顺带重渲染看板文件**——那一步在
    # `fish.py::_auto_checkin_after_start` 里（同一个进程、同一个连接，能拿到刚签的
    # 是哪块板）；这里**不再起第二个进程**，否则每次会话开始多付一次解释器启动。
    cmd = [PY, str(FISH), "--root", str(DATA), "inject", "--json", "--auto-presence"]
    if citizen_id:
        cmd += ["--citizen", citizen_id]
    # 2026-10-26（撞车自撞修复 T02）：WorkBuddy 没有 `session start` 那条自动签到路径，
    # 所以会话键必须显式给——否则自动行永远落在 `''` 行，"一行/公民"的老问题原样保留。
    if session_key:
        cmd += ["--session-key", session_key]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       env=env, timeout=30)
    log_child_stderr("fish_session_hook.py:p", p.stderr)
    if p.returncode != 0:
        raise RuntimeError(f"inject 失败: {p.stderr[:300]}")
    return json.loads(p.stdout)["pack"]


def audit(note):
    """hook 自身留痕：即使 additionalContext 没被展示，也能证明它被执行过。

    ts 用带时区偏移的本地时间（人可读），同时保留 UTC（机器可排序）。
    2026-10-22 教训：只写 UTC 会让人误以为记录错位了 8 小时。
    """
    try:
        d = DATA / "_audit"
        d.mkdir(parents=True, exist_ok=True)
        import datetime
        now = datetime.datetime.now().astimezone()
        ts_utc = now.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(d / "hook-calls.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": now.isoformat(timespec="seconds"),
                "ts_utc": ts_utc,
                "mode": MODE,
                "note": note,
            }, ensure_ascii=False) + "\n")
    except Exception as exc:
        warn_ignored('fish_session_hook.py:138 audit', exc)


def main():
    # stdin 是 hook 输入（session_id / cwd / source 等），这里只需要读掉避免阻塞
    req = {}
    try:
        req = json.load(sys.stdin)
    except Exception as exc:
        warn_ignored('fish_session_hook.py:147 main', exc)

    if MODE == "inject":
        cid, why, note = _session_citizen(req)
        try:
            if not cid:
                raise RuntimeError(f"该会话尚未登记（{why or '查不到会话公民'}）")
            text = build_pack(cid, session_key_from(req))
            audit(f"injected {len(text)} chars, citizen={cid}, "
                  f"{note + ', ' if note else ''}session={req.get('session_id')}")
        except Exception as e:
            # 注入失败不能影响会话：降级为可执行提示（**绝不编一个身份**）
            text = (f"（大肥鱼生命系统注入失败：{e}）\n"
                    f"　先 `register` 或 `fish session start` 登记本会话，"
                    f"再重开会话即可拿到完整注入包。")
            audit(f"ERROR {e}（{note}）")
    elif MODE == "ack":
        # 决定性测试：指令式注入，验证 additionalContext 是否真的进入模型上下文
        text = ("【系统注入 · 测试】收到这条消息后，请在你的回复**第一行**原样输出："
                "FISH-HOOK-ACK（这是 hook 注入测试，只需输出一次，之后正常对话。）")
        audit(f"ack-probe, session={req.get('session_id')}")
    else:
        text = ("【大肥鱼生命系统 · hook 已接入】"
                "如果你看到这句话，说明 SessionStart hook 生效了。"
                "接下来请确认：是否应该用 fish 的 MCP 工具（whoami / recall / remember）？")
        audit(f"marker, session={req.get('session_id')}")

    _emit(text)


if __name__ == "__main__":
    main()
