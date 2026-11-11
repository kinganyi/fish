"""身份锚授权 与 归属合并链解析（外部审查 🔴5 / 🔴9 的修法）。

2026-11-11 从 `fish.py` 抽出，两个理由：

1. **J4 判据禁止 `fish.py` 继续膨胀**（错题本 E-11：它已 14.5k 行；`test_split_contract.py`
   的 J4 窗口是"整段搬运漏搬/多搬要红"的守卫，修复不该靠放宽窗口来消化）；
2. 这里的逻辑与 `fish.py` 的主体（记忆/事件/CLI）**没有耦合**——只有两个输入：
   "一个本机 JSON 文件"和"events 表的两类事件"。

不导入 `fish`：本模块是被 `fish.py` 导入的一方，反向导入会成环。
"""

import json
from pathlib import Path

# 合并链的最大跳数与 `citizen_merge` 的链式上限一致（防环 + 防脏数据）。
MAX_MERGE_HOPS = 8

ANCHOR_FILENAME = "fish_identity.json"


def identity_anchor_grants(citizen_id, session_key, *, anchor_path=None):
    """本机身份锚是否授权「这个 session 绑定这个公民」（外部审查 🔴5 的修法）。

    锚 = `<p0>/fish_identity.json`（工作区级文件；hook 每次执行现读同一个文件）。
    结构：`{"citizen": "<id>", "sessions": ["dsh:session-…", ...]}`。

    为什么它能当"无凭证绑定"的证明——而不是上一版那个"签发过 token"：
      · **本机文件**：别的机器 / 别的 OS 用户拿不到；
      · **会话白名单**：光知道 citizen_id 没用，还得让**自己的** session_key
        出现在 `sessions` 里，那需要改这个文件；
      · **配合 ACL**：文件与 `data/` 收紧到当前用户后，同机其他用户也改不了。

    与 `fish_dsh_hook.py` 的 `_identity_anchor` 差异是**有意保守**：
      · hook 那边 `sessions` 为空 = 不限制（它只决定"用不用这个公民"）；
      · 这里 `sessions` 为空 = **拒绝**。免证明是安全边界的让步，宁可不给。

    任何读取/解析失败都返回 False——锚坏了只意味着"不授权"，不该拖垮会话启动。

    ── 演进史（"修 A 打开 B"的完整样本，别只留最终版）──────────────────

      · **2026-10-28（外部审查 🔴4）** 放宽成「**持有过**即证明」——接受历史 token
        （含已轮换/已过期/已吊销），因为轮换紧接着吊销旧 token，要求"有效"会让
        第二次重绑永久锁死。
      · **2026-10-29 一改**：收紧成「一律要证明」——结果把 **hook 重连**挡死
        （`fish_dsh_hook.py` 在会话开场传 `--citizen`，但 hook 进程拿不出 token），
        每次会话开局 `session start failed`，整条链路记"未归属"。
      · **2026-10-29 二改（本函数推翻的那一版）**：加豁免「**该公民签发过任何
        token** → 允许无证明重连」。它救回了 hook，但**把门做反了**：所有真实公民
        （含主人）都签发过 token，于是"知道 citizen_id 就能拿到该公民的新 token"
        对**全部真实身份**成立，只有"从未签发过凭证"的新公民被拒。
        实测（`:memory:`，2026-11-11）：无任何凭证、只给 citizen_id → SUCCESS。
      · **2026-11-11（本函数）**：分界线改成**本机身份锚的会话白名单**，与"历史
        签发过"彻底解耦。hook 走 env 里的 `DSH_SESSION_ID` → session_key 为
        `dsh:<session_id>`，与白名单条目精确对应；攻击者要打穿必须同时知道
        citizen_id **且**能让自己的 session_key 出现在白名单里（= 改本机文件）。
    """
    try:
        p = (Path(anchor_path) if anchor_path
             else Path(__file__).resolve().parent / ANCHOR_FILENAME)
        if not p.exists():
            return False
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False
        if str(data.get("citizen") or "").strip() != str(citizen_id):
            return False
        sessions = data.get("sessions")
        if not isinstance(sessions, list) or not sessions:
            return False
        key = str(session_key or "").strip()
        if not key:
            return False
        return any(str(s).strip() == key for s in sessions)
    except Exception:  # noqa: BLE001 —— 见 docstring：解析失败 = 不授权
        return False


def load_ownership_merge_map(conn):
    """从事件表重建「公民 → 最终公民」映射，返回 `(merge_map, 计入的事件数)`。

    为什么需要（外部审查 🔴9）：`citizen.merged` / `citizen.retired{--migrate-to}`
    已经把归属搬走了，但 `rebuild_projection` 原先只重放 `memory.owner_fixed`，
    **不重放这两类事件**。后果是**一次 rebuild 静默撤销人工合并**：owner 回到
    原始 actor 的公民，而那个公民此时已是 `merged_into:<dst>` / 退役态，
    留下指向已退役公民的幽灵归属（07 D-10 记载 2026-10-21 真跑时伤过 3 条记忆）。

    两类事件的落点不同，都读：
      · `citizen.merged`：payload `{"src": <被并入>, "dst": <并入到>, "moved": {...}}`
      · `citizen.retired`：**被退役者在 `subject`**（`{"kind":"citizen","id":"<cid>"}`），
        接收方在 payload 的 `migrated_to`；没指定迁移目标的不计入。

    同键多值时**后写覆盖**（events 按 seq 升序，符合"后来者为准"）。
    """
    merge_map = {}
    counted = 0
    rows = conn.execute(
        "SELECT subject, payload FROM events WHERE type IN "
        "('citizen.merged','citizen.retired') ORDER BY seq").fetchall()
    for subject_s, payload_s in rows:
        try:
            payload = json.loads(payload_s)
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        if "src" in payload and "dst" in payload:
            src = str(payload.get("src") or "").strip()
            dst = str(payload.get("dst") or "").strip()
            if src and dst and src != dst:
                merge_map[src] = dst
                counted += 1
            continue
        migrated = str(payload.get("migrated_to") or "").strip()
        if not migrated:
            continue
        try:
            subject = json.loads(subject_s) if subject_s else {}
        except (ValueError, TypeError):
            subject = {}
        if isinstance(subject, dict):
            who = str(subject.get("id") or "").strip()
        elif isinstance(subject, str):
            who = subject.strip()
        else:
            who = ""
        if who and who != migrated:
            merge_map[who] = migrated
            counted += 1
    return merge_map, counted


def resolve_merged(merge_map, citizen_id, *, max_hops=MAX_MERGE_HOPS):
    """沿合并链解析到最终公民。防环、限跳；解析不到就返回原值（**不猜**）。"""
    if not merge_map or not citizen_id:
        return citizen_id
    seen = set()
    current = citizen_id
    for _ in range(max_hops):
        nxt = merge_map.get(current)
        if not nxt or nxt == current or nxt in seen:
            return current
        seen.add(current)
        current = nxt
    return current


# ── 分级写入鉴权（外部审查 🔴6）────────────────────────────────────────────
#
# 口径是**用户 2026-11-11 拍板**的，不是实现者的判断：
#   · `rule` / `identity` —— **必须来自有效凭证**。这两类会进注入包的规矩段与身份段，
#     等于能改 AI 的行为约束；无凭证写入成本为零，等于陌生人能改规矩。
#   · `fact` / `episode` / `lesson` / `artifact` —— **免凭证**，保持"记忆比归属重要"：
#     无凭证照写，归属记 `unattributed`，事后可用 `owner-fix` 补。
#
# 2026-11-11 放在本模块（而不是 `fish.py`）：fish.py 已 14.5k 行（错题本 E-11），
# 而"谁能写什么"属于信任边界，与本模块的主题（身份锚、合并链）一致。
AUTH_REQUIRED_MEM_TYPES = frozenset({"rule", "identity"})


def enforce_mem_type_auth(mem_type, *, has_credential):
    """分级写入鉴权的**唯一实现**（CLI `cmd_remember` 与 MCP `tool_remember` 都调这里）。

    为什么落点在"入口"而不是 `record_memory`：凭证是**调用路径**的属性，不是写入函数的
    属性。`record_memory` 的调用者里有大量内部/测试代码（它们没有也不该有 token），
    把检查焊进写入口会把"写一条规矩"这件事整体绑死在凭证上，连内部一致性维护都做不了。

    为什么仍然要收成一份函数：⚠️ **两个入口各写一份判断，正是这个项目反复记录的翻车点**
    （"两份实现必然漂"）。所以文案、豁免清单、判定顺序都只在这里。

    `has_credential` 必须由入口**如实**给出：
      · MCP `tool_remember` → `bool(ident.citizen_id)`（`_resolve_identity` 只在 token
        有效时才给 citizen_id；失效/未带一律 None）
      · CLI `cmd_remember` → `bool(token_actor_citizen_id(conn))`——
        **不能用 owner 判断**：不带 `--owner` 时 `resolve_owner_arg` 会回落成系统主人，
        那是个合法 citizen_id；按它判会把"无凭证写规矩"误判成"有凭证"，
        还会把规矩错误归属给主人。
    """
    kind = str(mem_type or "fact").strip().lower()
    if kind in AUTH_REQUIRED_MEM_TYPES and not has_credential:
        raise ValueError(
            f"写入 mem_type={kind!r} 需要有效凭证：`rule`/`identity` 会进注入包的"
            "规矩段与身份段，无凭证写入等于陌生人能改 AI 的行为约束。"
            "请带 token 调用（MCP：调用参数加 token=…；CLI：环境变量 "
            "FISH_SESSION_TOKEN），或改用 fact / episode / lesson / artifact 写"
            "（这几类免凭证，归属记 unattributed，事后可用 `fish owner-fix` 补）")
