#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""身份/会话域（`fish.py` 拆包 · W3-a · 144 刀一）：公民记录、会话令牌、身份解析、workspace 映射、档期与会话生命周期。
只 import 下层模块（fish_common/fish_db/fish_trust/fish_surface/fish_naming/fish_lineage/fish_activity/fish_identity_diag），
禁止 import fish（J2）；70 个函数 + 26 个常量逐字搬自 fish.py，函数体零改动（J8 式 AST 等价复核）。
`__all__` = 70 个函数名（含 `_` 私有）；常量由 fish.py 另行 import 再导出，旧名字一个不少。"""
import datetime, hashlib, math, os, re, secrets, sqlite3
from pathlib import Path
import fish_activity, fish_lineage, fish_naming, fish_surface
from fish_common import (_utc_str, citizen_ids_known, get_config, now_utc, parse_utc, resolve_owner, ulid, warn_ignored)
from fish_db import _CITIZENS_ALL_COLUMNS, append_event
from fish_identity_diag import related_citizens_lines as _rc_lines_raw
from fish_trust import identity_anchor_grants
# 旧 CLI 的 --owner 默认值。它从来不是公民 ID，只是"人类"的占位说法。
# 2026-10-22 吃狗粮事故：这个默认值把 AI 的写入静默记到主人名下、且指向不存在的公民
# （见 06 §9.7-2）。保留常量只为识别并归一历史值，不再作为默认值使用。
LEGACY_OWNER_DEFAULT = "human-anyi"
def resolve_owner_arg(conn, owner):
    """把调用方传来的 owner 归一成**有效的 citizen_id**，或者响亮报错。

    语义（2026-10-22 定，见 07 D-13）：
      - 未指定（None / 空）→ 系统主人 `resolve_owner()`；再没有就 "unknown"
      - 旧默认值 "human-anyi" → 同上（它的原意就是"人类"，不是某个公民 ID）
      - 有效 citizen_id → 原样返回
      - 其他任何字符串（显示名、拼错的 ID、随手写的名字）→ 报错并给下一步

    为什么不再静默接受：`owner` 按 02 §2.3 是公民主键。收下不存在的 ID 会写出指向幽灵
    公民的归属，而且没有任何提示——这是 D-10 之外另一个静默数据缺陷。
    """
    if owner in (None, ""):
        return resolve_owner(conn) or "unknown"
    # 2026-11-11（序3 归档验证）：有效性要含 `citizens_archive`——已归档公民的 id
    # 仍是**合法归属目标**（它名下的记忆还在；`--owner` 指向它不该被拒）。
    if owner in citizen_ids_known(conn):
        return owner
    if owner == LEGACY_OWNER_DEFAULT:
        return resolve_owner(conn) or "unknown"
    cands = conn.execute(
        "SELECT citizen_id,display_name,kind FROM citizens_all WHERE status='active' "
        "ORDER BY created_at").fetchall()
    listing = "\n".join(f"    {c[0]}  {c[1]}（{c[2]}）" for c in cands) or "    （库里还没有公民）"
    raise ValueError(
        f"owner 必须是公民 ID，收到 {owner!r}。\n"
        f"  可用公民：\n{listing}\n"
        f"  下一步：`fish citizen-list` 查 ID；不给 --owner 则默认记到系统主人。")
def set_owner(conn, citizen_id):
    """把某个公民设为系统主人。唯一性由部分唯一索引 + 先清后设保证。"""
    row = conn.execute("SELECT kind,status,display_name FROM citizens WHERE citizen_id=?",
                       (citizen_id,)).fetchone()
    if not row:
        raise ValueError(f"公民不存在：{citizen_id}")
    kind, status, name = row
    if status != "active":
        raise ValueError(f"公民 {name} 状态为 {status}，不能设为主人")
    if kind != "resident":
        raise ValueError(
            f"主人必须是 resident 形态（{name} 当前是 {kind}）。"
            "若确需，请先把它改为 resident。")
    try:
        conn.execute("UPDATE citizens SET is_owner=0 WHERE is_owner=1")
        conn.execute("UPDATE citizens SET is_owner=1 WHERE citizen_id=?", (citizen_id,))
        append_event(conn, "config.changed",
                     {"citizen_id": "system", "evidence_level": "signed"},
                     {"kind": "citizen", "id": citizen_id},
                     {"key": "owner", "new_value": citizen_id}, commit=False)
        conn.commit()
    except Exception: conn.rollback(); raise
    return name
def find_citizen_by_name(conn, name):
    """按 display_name 查活跃公民；查不到返回 None。

    2026-10-24 修（外部评估会话暴露的真问题）：原来是裸 `fetchone()` 且**没有
    ORDER BY**。当同一个 display_name 有多条活跃公民时（实测 `fish外部评估`
    就有 2 条），返回哪一条不确定 —— 后果是 `resolve_actor` 的归属会在多条
    重复公民之间漂移，两次调用可能给出不同结果，"谁写的"变得不可复现。

    修法：`ORDER BY created_at, citizen_id` —— 取**最早创建**的那条，再按 id
    兜底保证完全确定。为什么选最早：`register` 的语义是幂等复用，幂等失败时
    先建的那条才是"原始身份"，后建的是误建。

    ⚠️ 这只是让**结果确定**，不是去重。重复公民本身要靠人工/merge 清理 ——
    发现重复时该报出来，不该静默挑一条（`find_duplicate_display_names` 就是为此）。
    """
    if not name:
        return None
    return conn.execute(
        "SELECT citizen_id,display_name,kind,role,host FROM citizens "
        "WHERE display_name=? AND status='active' "
        "ORDER BY created_at, citizen_id LIMIT 1", (name,)).fetchone()
def find_same_name_citizens(conn, display_name, exclude_citizen_id=None) -> list:
    """列出**同名的在用公民**（FB-08）：`[{citizen_id, kind}, ...]`。

    为什么签发完才查（而不是签发前）：这里问的不是"能不能叫这个名字"，而是
    "**你是不是又给自己建了一个身份**"。名字不唯一是既定决策（14：同名视作两个
    公民，不合并），所以不能拦；但不说一声，AI 就会在两个公民名下各写一半记忆，
    而两条通道全都返回 OK——那种分裂只有比十六进制串才发现。

    `status='active'` 与 `citizen_id<>?` 来自设计给定的查询：已退出的公民不算
    "还有人在用这个名字"，自己也不算冲突。排序用 `created_at` 保证输出确定。
    """
    name = (display_name or "").strip()
    if not name:
        return []
    rows = conn.execute(
        "SELECT citizen_id,kind FROM citizens "
        "WHERE display_name=? AND status='active' AND citizen_id<>? "
        "ORDER BY created_at, citizen_id",
        (name, exclude_citizen_id or "")).fetchall()
    return [{"citizen_id": r[0], "kind": r[1] or "unknown"} for r in rows]
def same_name_notice(citizen_id, kind) -> str:
    """FB-08 的回执文案（**唯一实现**）：CLI 与 MCP 两个入口必须说同一句话。

    一句话三件事：命中了谁（id + 形态）、不做什么（不自动合并）、下一步怎么做
    （`--citizen` 绑定）。少任何一件，读的人都会误以为系统"已经帮你合并了"。
    """
    return (f"检测到同名公民 {citizen_id}（{kind or 'unknown'}）；"
            "如需沿用请用 --citizen 绑定（不自动合并）")
from fish_identity_diag import related_citizens_lines as _rc_lines_raw
def related_citizens_lines(conn, citizen_id=None) -> list:
    """转发到 `fish_identity_diag`；它只吃 citizen dict，不吃 id。

    这么分是为了让新模块**零内部依赖**：拿 citizen 记录是调用方（这里）的事，
    没必要为了它制造一条 `identity_diag → fish` 的上行依赖。
    """
    return _rc_lines_raw(conn, load_citizen(conn, citizen_id) if citizen_id else None)
def find_duplicate_display_names(conn) -> list:
    """列出有重复的 display_name（同名多公民）。

    为什么单独一个函数：「结果确定」和「数据干净」是两件事。`find_citizen_by_name`
    修了前者（ORDER BY），但重复本身还在 —— 它会让 `whoami`、归属统计、
    推送画像全部带上噪声，而且**永远不会有报错**（只是数字不对）。
    所以要把"发现重复"做成一个可被查询、可被判据盯住的动作。
    """
    return [
        {"display_name": r[0], "count": r[1], "citizen_ids": r[2]}
        for r in conn.execute(
            "SELECT display_name, COUNT(*) AS n, GROUP_CONCAT(citizen_id) "
            "FROM citizens WHERE status='active' AND display_name IS NOT NULL "
            "GROUP BY display_name HAVING n > 1 ORDER BY n DESC, display_name")
    ]
def rename_citizen(conn, citizen_id, new_name, *, actor=None, reason="") -> dict:
    """把公民的 display_name 改成 new_name。**只改标签，不改身份。**

    返回 `{"citizen_id","old_name","new_name","changed","same_name_exists","event"}`。

    为什么需要它（2026-10-25 用户裁定：这是 BUG，不是增强）：
      用户开场说"你是 X"，系统里却没有任何机制把这个名字绑到当前公民上——
      `display_name` 只在**公民创建的那一瞬间**产生一次，此后全库 6 条
      `UPDATE citizens` 没有一条碰它（改的是 is_owner / last_seen_at /
      account_id / status）。直接后果：用户指定的名字在系统里**无处安放**，
      只能以"另一条孤儿公民"的形式存在（`register` 建行但不绑定），
      于是"可被检索的名字"对用户来说根本不存在。

    三条边界（都不许越）：
      1. **名字不是身份**：只改 `display_name`，不动 `citizen_id` / 凭证 / 证据级。
         历史记忆的 owner 是 `citizen_id`，所以改名**不会**让任何历史记录换主人——
         第一棒担心的"历史记忆指向陌生名字"是**显示层**的事，不是数据完整性。
      2. **事件留痕**：写 `citizen.renamed`（payload 带 from/to/actor/reason）。
         "它原来叫什么"必须可查，否则改名就是静默改写历史。
      3. **同名幂等**：新旧名相同 → 不写事件、`changed=False`（避免无意义噪声）。

    重名**允许**（决策 14：同名视作两个公民，不合并），但 `same_name_exists`
    会把同名的其它公民报出来——让调用方显示"已有同名公民 X"，而不是静默重复。

    `reason` 用于记录"谁在什么场景下改的名"（例如"用户在开场指定"）。
    """
    new_name = (new_name or "").strip()
    if not new_name:
        raise ValueError("display_name 不能为空")
    if len(new_name) > 64:
        # 与 `code_registry.prefix` 的 20 字符上限同理：名字是给人看的短标识，
        # 超长名字在注入包的一行里会挤掉别的信息。取 64 是因为要考虑角色后缀
        # （`host:role` 形态）与中文名，比前缀宽松但仍有界。
        raise ValueError(f"display_name 太长（{len(new_name)} 字符，上限 64）")

    row = conn.execute(
        "SELECT display_name, status FROM citizens WHERE citizen_id=?",
        (citizen_id,)).fetchone()
    if not row:
        raise ValueError(f"公民不存在：{citizen_id}")
    old_name, status = row
    if status != "active":
        raise ValueError(f"只能给活跃公民改名（{citizen_id} 当前状态 {status}）")

    if old_name == new_name:
        return {"citizen_id": citizen_id, "old_name": old_name, "new_name": new_name,
                "changed": False, "same_name_exists": [], "event": None}

    # 2026-11-26（W2 评审 🟠 P1-2 残留）：名字与 `citizen.renamed` 必须同一事务。
    # 旧写法先 commit 名字再写事件——事件失败时名字已落盘、事件 0 条，改名不可审计。
    try:
        cur = conn.execute("UPDATE citizens SET display_name=? WHERE citizen_id=?",
                           (new_name, citizen_id))
        if cur.rowcount == 0:
            # 公民在归档表里（`citizens` 热表查不到）——`load_citizen` 走 citizens_all
            # 能读到它，但改名只该发生在活跃行上。响亮失败，不静默成功。
            raise ValueError(f"公民不在热表里（可能已归档），不能改名：{citizen_id}")

        same = []
        for d in find_duplicate_display_names(conn):
            if d["display_name"] != new_name:
                continue
            for cid in str(d["citizen_ids"] or "").split(","):
                cid = cid.strip()
                if cid and cid != citizen_id:
                    same.append(cid)

        append_event(conn, "citizen.renamed",
                     {"citizen_id": citizen_id, "evidence_level": "self-reported"},
                     {"kind": "citizen", "id": citizen_id},
                     {"from": old_name, "to": new_name,
                      "actor": actor, "reason": reason or ""}, commit=False)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"citizen_id": citizen_id, "old_name": old_name, "new_name": new_name,
            "changed": True, "same_name_exists": same, "event": "citizen.renamed"}



def resolve_actor_from_token(conn, token=None):
    """把 `FISH_SESSION_TOKEN`（或显式给的 token）解成公民行；解不出返回 None。

    为什么行动者解析要读凭证（2026-10-26 复审 ⚪N6）：`FISH_SESSION_TOKEN` 原先只有
    `whoami` 一个消费者，于是同一个 AI 走 MCP 有归属、走 CLI 得到「未确定行动者」——
    **token 是唯一归属**这句话在 CLI 面上是空的。这里把它接进行者解析的主路，
    `fish inject` / `fish whoami` 因此与 MCP 同源（同一个 token → 同一个公民）。

    返回形状必须与 `find_citizen_by_name` **完全一致**（citizen_id, display_name,
    kind, role, host）——调用点按位置取 `actor[0]` / `actor[1]`，形状变了会静默错位。

    无效/吊销/过期凭证一律返回 None（判定只有 `validate_session_token` 一处）；
    已归档的公民也返回 None：归档 = 这个身份不再参与在册行动。
    """
    raw = (token or "").strip() or env_session_token()
    if not raw:
        return None
    citizen_id = validate_session_token(conn, raw)
    if not citizen_id:
        return None
    return conn.execute(
        "SELECT citizen_id,display_name,kind,role,host FROM citizens_all "
        "WHERE citizen_id=? AND status='active'", (citizen_id,)).fetchone()
def token_actor_citizen_id(conn):
    """env 凭证解出的 citizen_id（无凭证 / 无效 / 已归档 → None）。

    给"没显式指定归属"的写入口用（`fish remember` 缺 `--owner`）：MCP 侧的口径是
    `_owner_value` = "token 有效就记给该公民"，CLI 侧原先只会记给系统主人——
    同一个 AI 从两个入口写同一条记忆，归属却不同（2026-10-26 复审 ⚪N6）。
    **只补"没指定"这一档**：显式给了 `--owner` 时以调用方为准，绝不覆盖。
    """
    row = resolve_actor_from_token(conn)
    return row[0] if row else None
def resolve_actor(conn, actor_name=None):
    """当前行动者：按名字查公民；查不到返回 None（未登记访客）。

    与 resolve_owner 的区别（本轮修复的核心）：
      owner  = 系统主人（human/resident），一个系统只有一个
      actor  = 此刻在动手的公民（visitor/role），可能每次会话都不同
    whoami 必须返回 actor，否则 AI 会自称主人。

    优先级（2026-10-26 复审 ⚪N6 补齐最后一档）：
      显式 actor_name > `FISH_ACTOR` env > `config.actor_name` > **env 凭证的公民**。
    最后那一档为什么必须有：名字是"描述性标签"，凭证是"归属证据"；只认名字时，
    一个只带凭证的 CLI 会话会得到"没有行动者"，而它手上明明握着系统签发的归属。
    显式给了 `actor_name` 但查不到时**不回落**到凭证：那是调用方写错了名字
    （拼错/刚退役），静默换成另一个人比报空更坏。
    """
    if actor_name is not None:
        return find_citizen_by_name(conn, actor_name)
    # 优先级：环境变量 FISH_ACTOR > config.actor_name
    # 环境变量优先，因为 MCP hook 是独立进程，配置不一定能传进来
    name = os.environ.get("FISH_ACTOR") or get_config(conn, "actor_name")
    row = find_citizen_by_name(conn, name)
    if row:
        return row
    return resolve_actor_from_token(conn)
# ------------------------------------------------------ 身份层 P1-b：会话身份与凭证
# 依据：`18-身份层实现设计-P1b.md`（+ `16` §2 / `17` §2.4-2.5 / 评审 R-2）。
#
# 一句话理由：**MCP 面拿不到真实会话身份（宿主把 MCP 进程的 env scrub 掉了），
# 所以身份只能由"能读到 env 的一方"签发，然后交给 MCP 校验。**
#
# 铁律（R-2）：`session` 是 **CLI 专属命令族**，绝不注册成 MCP 工具
# （`mcp_server.TOOLS` 仍是 11 个，由 test_identity_p1b 判据 H 冻结）。
# MCP 侧将来只读 `session_tokens` 做校验，永远不签发 machine 级——它的环境里
# 没有任何东西可比对，声称 machine 就是自欺。
#
# 凭证三条纪律（铁律⑮）：
#   1. 明文只在 `session start` / `session agent` 的 **stdout** 出现一次：
#      不写文件、不进日志、不进事件 payload（事件里只写 sha256）；
#   2. 同一会话重跑 start = 轮换凭证（旧凭证立即吊销），公民不变（18 §10 的幂等承诺）；
#   3. `end` 只标 revoked_at，不删行——审计需要"谁什么时候拿过凭证、何时失效"。
#
# 已知边界（18 §9，实现层面必须记住的四条）：
#   ① 明文会落进宿主会话日志 → 缓解只有 TTL + 可吊销，做不到"日志里也没有"；
#   ② 没有 hook 时 AI 必须手动调 `session start`——它会忘（幂等就是为这个留的后路）；
#   ③ 宿主不给 session id 时身份退化为 self-reported（下面 build_session_identity 负责）；
#   ④ account_id 允许为空，表示"还没归属到某个 AI 账号"，后续可补。
SESSION_TOKEN_TTL_KEY = "session.token_ttl_hours"
SESSION_EVIDENCE_LEVELS = ("machine", "inherited", "self-reported", "provisional")
SESSION_CHANNELS = ("cli", "hook", "parent")
HOST_SESSION_ENVS = (
    ("dsh", "DSH_SESSION_ID"),
    ("codebuddy", "CODEBUDDY_SESSION_ID"),
    ("claude", "CLAUDE_SESSION_ID"),
)
SESSION_ALIAS_ENVS = ("FISH_SESSION_ACTOR", "FISH_ACTOR")
SELF_SESSION_PREFIX = "self:"
ANON_SESSION_PREFIX = "anon:"
# 「会话名入参」里可能**已经带着**的前缀（FB-05）。两类：
#   · 自建前缀 `self:` / `anon:`（AI 把上一次回执里的 session_key 原样回传）；
#   · 宿主前缀 `dsh:` / `codebuddy:` / `claude:`（AI 把 whoami/日志里看到的机器键回传），
#     外加历史遗留的 `zcode:` / `workbuddy:`（宿主方言名，见 prompt-push 的 choices）。
# 为什么必须识别它们：再拼一层就是 `self:self:xxx`——同一个会话两条键，
# 而"同一个会话 → 同一个公民"正是这套身份的幂等承诺（生产库已实锤
# `self:self:fish外部评估`，就是这条没做）。
SESSION_NAME_PREFIXES = (SELF_SESSION_PREFIX, ANON_SESSION_PREFIX,
                         "dsh:", "codebuddy:", "claude:", "zcode:", "workbuddy:")
def strip_session_name_prefixes(name):
    """剥掉会话名里**已经带着**的已知前缀（幂等：`self:self:x` → `x`）。

    只剥前缀、不动名字本体：`self:abc` 与 `abc` 必须落到**同一个** session_key，
    否则"重跑 start 命中同一个公民"这句话是假的（这就是 FB-05 的判据）。
    循环剥是为了收拾历史脏值（`self:self:self:x` 在各种重试里是会长出来的）。
    """
    text = (name or "").strip()
    changed = True
    while changed:
        changed = False
        low = text.lower()
        for prefix in SESSION_NAME_PREFIXES:
            # `len(text) > len(prefix)`：纯前缀（`"self:"`）不剥——剥成空串等于
            # 把"给了名字"变成"没给名字"，那是另一条分支（匿名），不该在这里被制造。
            if low.startswith(prefix) and len(text) > len(prefix):
                text = text[len(prefix):].strip()
                changed = True
                break
    return text
SESSION_TOKEN_PREFIX = "fsh_"
_CITIZEN_READ_FIELDS = ("citizen_id", "display_name", "kind", "role", "host", "status",
                        "account_id", "host_session_key", "last_seen_at", "archived_at")
_STINT_READ_FIELDS = ("id", "citizen_id", "account_id", "project", "from_ts", "set_by")
def _citizen_row(row):
    return dict(zip(_CITIZEN_READ_FIELDS, row)) if row else None
def host_session_facts(environ=None) -> dict:
    """读进程环境里的**机器事实**：哪个宿主、会话 id 是什么。

    只认环境变量，**不接受任何调用方传入的会话名**——这就是 machine 级的定义：
    它来自进程环境，不来自模型文本。一个都没命中时返回 evidence=None，
    让上层去走自报/匿名分支，而不是在这里编一个（编出来的值就是自欺）。
    """
    environ = os.environ if environ is None else environ
    for host, var in HOST_SESSION_ENVS:
        raw = (environ.get(var) or "").strip()
        if raw:
            return {"host": host, "session_id": raw,
                    "session_key": f"{host}:{raw}", "evidence": "machine", "source": var}
    return {"host": None, "session_id": None, "session_key": None,
            "evidence": None, "source": None}
def env_session_alias(environ=None):
    """自报会话名（来自 env），没有就 None。"""
    environ = os.environ if environ is None else environ
    for var in SESSION_ALIAS_ENVS:
        raw = (environ.get(var) or "").strip()
        if raw:
            return raw
    return None
# 凭证明文的 env 来源（2026-10-26 · F3）。
# 与 `SESSION_ALIAS_ENVS` 的区别：那两个是**会话名**（描述性标签），这个是**凭证**
# （决定归属的 bearer token），语义完全不同，所以分开一组常量，不混在一起。
SESSION_TOKEN_ENVS = ("FISH_SESSION_TOKEN",)
def env_session_token(environ=None):
    """凭证的 env 来源（`FISH_SESSION_TOKEN`）——CLI 侧的"我手上这条 token"。

    为什么需要它（2026-10-26 · F3）：MCP 的 token 是**工具入参**（每次调用显式带），
    而 CLI 一次进程一条命令，没有"这次调用"可挂；`fish whoami` 若只认 `--token`，
    每次都要把明文写进命令行（会落进 shell 历史与宿主日志）。env 是 CLI 唯一的
    会话级载体，与 `FISH_SESSION_ACTOR`（会话名）、`FISH_ACTOR`（账号）同一族。

    **有效性判定不在这里做**：拿到明文之后一律交给 `validate_session_token`。
    """
    environ = os.environ if environ is None else environ
    for var in SESSION_TOKEN_ENVS:
        raw = (environ.get(var) or "").strip()
        if raw:
            return raw
    return None
def env_actor_name(environ=None):
    """AI 账号的 env 来源（`FISH_ACTOR`）。与"会话名"不是一个概念：账号跨会话，会话不跨。"""
    environ = os.environ if environ is None else environ
    raw = (environ.get("FISH_ACTOR") or "").strip()
    return raw or None
def build_session_identity(alias=None, environ=None) -> dict:
    """把「进程环境 + 调用参数」翻译成会话身份（**不碰库**）。

    返回 {session_key, evidence, channel, host, alias, source, warning}。

    优先级 env（machine）> 自报名（self-reported）> 匿名：
      三者的可信度严格单调，所以顺序不能换——把 alias 放在 env 前面，
      一条自报就能覆盖机器事实，那正是 R-2 说的自欺。
    匿名（既无 env 也无别名）**每次都新建公民**：没有任何东西能证明"这是同一个会话"，
    硬编一个稳定 key 等于凭空宣布一个事实；宁可多建一个匿名公民，也不假装认识它。
    """
    facts = host_session_facts(environ)
    # FB-05：入参里的会话名先**剥掉已知前缀**再拼（唯一入口就在这里）。
    # 顺序不能反：先拼后剥会漏掉"env 里的自报名也带前缀"这一路（历史环境的残留值）。
    reported = strip_session_name_prefixes(
        (alias or "").strip() or env_session_alias(environ))
    if facts["evidence"] == "machine":
        return {"session_key": facts["session_key"], "evidence": "machine",
                "channel": "cli", "host": facts["host"], "alias": reported,
                "source": facts["source"], "warning": None}
    if reported:
        return {"session_key": f"{SELF_SESSION_PREFIX}{reported}",
                "evidence": "self-reported", "channel": "cli", "host": "cli",
                "alias": reported, "source": "alias", "warning": None}
    return {"session_key": f"{ANON_SESSION_PREFIX}{ulid()}", "evidence": "self-reported",
            "channel": "cli", "host": "cli", "alias": None, "source": "anonymous",
            "warning": ("没有任何会话标识（env 里没有宿主 session id，也没给 --alias）："
                        "本次建成一个匿名公民，重跑 start 会再建一个。"
                        "想复用同一个公民 → 带上 --alias 或设 FISH_SESSION_ACTOR。")}
def resolve_account_id(account=None, alias=None, environ=None):
    """账号锚点的来源：`--account` > `FISH_ACTOR` env > None。

    **2026-10-25 改**（外部评估 `EVAL-261025-03`）：**不再用 `--alias` 兜底**。

    为什么：alias 是"这个会话叫什么"（给人看的追溯标签），account 是"这个 AI 是哪个账号"
    （归属锚点）。把前者当后者，等于**用一个描述性字段推身份**——而两者语义不同：

      · 同一个 AI 开两个会话 → 两个 alias、同一个 account（应该）
      · 旧逻辑会得出两个不同的 account → 建出两个公民 → **归属分裂且无法合并**

    实测代价（`EVAL-261025-03` 亲历）：`session start --alias X` 建出的公民
    与 MCP `register` 给的公民**不是同一个**，而三条命令全都返回 OK，
    只有去比 `citizen_id` 两串十六进制才会发现不对。**最贵的是它静默。**

    空着仍然比编出来诚实（18 §9-4）：调用方要归属，就显式给 `--account`。
    """
    for cand in (account, env_actor_name(environ)):
        text = (cand or "").strip()
        if text:
            return text
    return None
def auto_session_display_name(host, session_key, role=None) -> str:
    """自动占位名（`未命名·<宿主>#<短码>`）。实现已下沉到 `fish_naming`（曾 173 个漏判）。"""
    return fish_naming.auto_session_display_name(host, session_key, role)
def hash_session_token(token: str) -> str:
    """库里只存 sha256(token)。**明文没有第二份副本**（表里没有明文列）。"""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()
def session_token_ttl_hours(conn) -> int:
    return int(get_config(conn, SESSION_TOKEN_TTL_KEY))
def _new_expires_at(conn, ttl_hours=None):
    """凭证到期时刻。0 或负数 = 永不过期（返回 None，与 decay.*=0 同一套约定）。"""
    ttl = session_token_ttl_hours(conn) if ttl_hours is None else int(ttl_hours)
    if ttl <= 0:
        return None
    moment = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=ttl)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
def _is_expired(expires_at, at=None) -> bool:
    """expires_at 为空 = 永不失效。字符串比较即可：定长 UTC ISO 的字典序就是时序。"""
    if not expires_at:
        return False
    return expires_at <= (at or now_utc())
def remaining_token_hours(expires_at, at=None) -> int:
    """到 `expires_at` 还剩几小时：**未过期时向上取整、至少 1；已过期返回 0**。

    为什么方向是"向上"：剩 20 分钟报 1 小时比报 0 更接近事实（"还没到期"这件事
    在读数上不能被抹平成 0），而 `max(1, ...)` 兜住"剩 1 秒"被算成 0 的边界——
    同一秒内的读数不该在 1 和 0 之间跳。

    **2026-10-26 修（评审 🟠1）**：旧实现对**过去**的 `expires_at` 也走同一条
    `max(1, ...)`，于是 `expires_at` 已经过去 3 小时的凭证被念成「剩余 1 小时」——
    而同一份回执的另一行（身份来源）老实写着「未归属」。两句话互相矛盾，读的人
    会以为"还能用一小时"，然后带着一条死凭证去写入，写入被记成 unattributed。
    现在**已过期返回 0**，由调用方（`credential_state_line`）走「已过期」那一档：
    读数不许再靠 `max(1, …)` 伪造一个"还剩一点"的假象。

    非标准 `expires_at`（长度/分隔符不是定长 UTC）会抛 `ValueError`：这是底层
    读数函数，**判据应当能看见坏数据**；whoami 那侧另有兜底（见
    `credential_state_line`），不许让探针崩在措辞层。
    """
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    moment_text = at or now_utc()
    if _is_expired(expires_at, moment_text):
        return 0
    moment = datetime.datetime.strptime(moment_text, fmt)
    expire = datetime.datetime.strptime(expires_at, fmt)
    seconds = (expire - moment).total_seconds()
    return max(1, int(math.ceil(seconds / 3600.0)))
def issue_session_token(conn, *, citizen_id, session_key, evidence, channel,
                        parent_hash=None, ttl_hours=None) -> dict:
    """签发一条凭证：生成明文 → 存 hash → 把明文交回调用方（**全流程唯一一次**）。

    明文只经手这一处，调用方唯一的合法动作是 print 到 stdout。想事后审计就查
    `token_hash`——明文离开 stdout 就没有了，这是设计而不是缺陷（铁律⑮）。
    词表校验放在这里：入口再多，bad row 都从这一道门过去（DDL 的 CHECK 是第二道）。

    **为什么 token 带 `fsh_` 前缀**（相对 18 §2 的 `secrets.token_urlsafe(32)` 是一处偏离，
    理由是实测出来的）：token_urlsafe 的字母表里有 `-`，约 1.3% 的 token 会以 `-` 开头；
    而 `fish session agent --parent <token>` 走 argparse，**以 `-` 开头的值会被当成选项**，
    报 `argument --parent: expected one argument`（2026-10-24 全量测试实测命中，
    400 次抽样中 5 次）。前缀顺手还解决了两件事：token 在日志里自解释、可被扫描器识别。
    熵不变（仍是 32 字节随机）。
    """
    if evidence not in SESSION_EVIDENCE_LEVELS:
        raise ValueError(f"evidence 只能是 {SESSION_EVIDENCE_LEVELS}，收到 {evidence!r}")
    if channel not in SESSION_CHANNELS:
        raise ValueError(f"channel 只能是 {SESSION_CHANNELS}，收到 {channel!r}")
    token = f"{SESSION_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    thash = hash_session_token(token)
    issued_at = now_utc()
    expires_at = _new_expires_at(conn, ttl_hours)
    conn.execute(
        "INSERT INTO session_tokens (token_hash,citizen_id,session_key,evidence,channel,"
        "parent_token,issued_at,expires_at) VALUES (?,?,?,?,?,?,?,?)",
        (thash, citizen_id, session_key, evidence, channel, parent_hash, issued_at,
         expires_at))
    return {"token": token, "token_hash": thash, "citizen_id": citizen_id,
            "session_key": session_key, "evidence": evidence, "channel": channel,
            "parent_token": parent_hash, "issued_at": issued_at,
            "expires_at": expires_at}
#: 2026-11-25（113 W4 · §5.5）：`parent_hash` 口径改成**递归吊销全部后代**。
#:
#: 四个坑（110 §3.6.4 实测，判据在 `test_revoke_recursive` 逐条钉住）：
#:   ① `UNION` 不是 `UNION ALL`：`t.hash` 去重防环（环只会重复"已见 hash"）；
#:   ② 保留 `revoked_at IS NULL`——幂等全靠它（重复 end 从"报错"变"0 条"）；
#:   ③ `WITH … UPDATE` 的 `cursor.rowcount` 返回 **-1**，真实吊销数走 `SELECT changes()`；
#: ④ 返回值语义从"直接子级数"变成"全部后代数"（`children_revoked` 的输出/事件同步）。
#:
#: **成对纪律（113 §5.6 末）**：本函数与 `assert_agent_parent_allowed` 的"放开一跳"
#: 必须同波上线、也必须**成对回退**——只退一半（单跳吊销 + 仍允许链式）比今天更糟。
_REVOKE_DESCENDANTS = """
WITH RECURSIVE t(hash) AS (
  SELECT token_hash FROM session_tokens WHERE parent_token = :root
  UNION
  SELECT s.token_hash FROM session_tokens s JOIN t ON s.parent_token = t.hash
)
UPDATE session_tokens SET revoked_at = :now
 WHERE token_hash IN (SELECT hash FROM t) AND revoked_at IS NULL
"""
def revoke_session_tokens(conn, *, session_key=None, citizen_id=None, token_hash=None,
                          parent_hash=None) -> int:
    """吊销凭证（**只标 revoked_at，不删行**），返回吊销条数。

    四种口径互不重叠，全部走这一处——"什么算失效"只能有一份判断，多一份就会漂：
      session_key  同一会话的所有活凭证（start 轮换 / end 当前会话）
      citizen_id   某公民的所有活凭证（公民退役时用；**end 永不用它**，110 §7-12）
      token_hash   单条（`end --token`）
      parent_hash  某条凭证派生出的**全部后代**（父会话结束时级联，见 end_session）
    一个过滤条件都没给就报错：全表吊销是灾难，不能靠"忘了传参"触发。
    `parent_hash` 支是递归 CTE，**不与其它口径组合**（今天没有组合调用；要组合请先
    想清楚"递归集合 AND 单键"的语义，别让它在某天静默变成另一个意思）。
    """
    if parent_hash is not None:
        conn.execute(_REVOKE_DESCENDANTS, {"root": parent_hash, "now": now_utc()})
        # 坑 ③：`WITH … UPDATE` 的 cursor.rowcount 恒为 -1，必须问 changes()
        return int(conn.execute("SELECT changes()").fetchone()[0] or 0)
    sets, where, params = ["revoked_at=?"], [], [now_utc()]
    for column, value in (("session_key", session_key), ("citizen_id", citizen_id),
                          ("token_hash", token_hash)):
        if value is not None:
            where.append(f"{column}=?")
            params.append(value)
    if not where:
        raise ValueError("吊销必须给至少一个条件（session_key/citizen_id/token_hash/父凭证）")
    where.append("revoked_at IS NULL")
    sql = f"UPDATE session_tokens SET {', '.join(sets)} WHERE {' AND '.join(where)}"
    return conn.execute(sql, params).rowcount
def lookup_session_token(conn, token):
    """按明文查凭证行（**不判有效性**：吊销/过期也返回），查不到返回 None。

    为什么留一个"能查出坏凭证"的口子：`session status` 的价值就在回答
    "我这条 token 为什么不能用"——只返回有效行，失效原因就永远看不见。
    有效性判断只有一处：`validate_session_token`。
    """
    if not token:
        return None
    row = conn.execute(
        "SELECT token_hash,citizen_id,session_key,evidence,channel,parent_token,"
        "issued_at,expires_at,revoked_at FROM session_tokens WHERE token_hash=?",
        (hash_session_token(token),)).fetchone()
    if not row:
        return None
    keys = ("token_hash", "citizen_id", "session_key", "evidence", "channel",
            "parent_token", "issued_at", "expires_at", "revoked_at")
    info = dict(zip(keys, row))
    info["expired"] = _is_expired(info["expires_at"])
    info["valid"] = (not info["revoked_at"]) and (not info["expired"])
    return info
def validate_session_token(conn, token, at=None):
    """**MCP 侧的校验契约**（18 §7）：有效 → citizen_id；无效（吊销/过期/不存在）→ None。

    这就是那条 SQL 的唯一实现：
      token_hash=? AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now)
    P1-c 的 MCP 工具只调这一处，不许各自再写一遍——两处实现必然漂，
    而"两处实现的失效判断"正是凭证体系里最不该漂的东西。
    """
    info = lookup_session_token(conn, token)
    if info is None:
        return None
    moment = at or now_utc()
    if info["revoked_at"] and info["revoked_at"] <= moment:
        return None
    if info["expires_at"] and info["expires_at"] <= moment:
        return None
    # 身份生命周期（外部审查 🔴5 / 🔴11，2026-10-29）：**公民状态也算失效的一部分**。
    # 旧实现只查 revoked/expires，实测 `RETIRED_TOKEN_VALID`——退役/合并后的 bearer
    # token 还能继续用，等于"退役"只是个标签。status 非 active（retired/merged_into/…）
    # 或公民行不存在，一律拒绝。
    row = conn.execute("SELECT status FROM citizens WHERE citizen_id=?",
                       (info["citizen_id"],)).fetchone()
    if row is None or str(row[0] or "") != "active":
        return None
    return info["citizen_id"]
# ------------------------------------------- 身份来源 / 凭证状态（MCP 与 CLI 共用一份措辞）
#
# 为什么必须共用（2026-10-26 · FB-03 / FB-07 / F3）：同一件事（"这次回执凭什么算身份"
# "我这条凭证还能用多久"）在两个入口各写一份，漂移是必然的——MCP 改了口径，CLI 还念
# 旧台词，读的人会把两句话当两个事实。所以**文案与判定都收在这里**，两个入口只调。
IDENTITY_SOURCE_TOKEN = "身份来源：token（session_tokens）"
IDENTITY_SOURCE_REGISTER = "身份来源：register 登记（不参与归属）"
IDENTITY_SOURCE_UNATTRIBUTED = "身份来源：未归属"
# 归属哨兵：**"当时没凭证"的唯一表示**（而不是 None）。
# 为什么不是 None：owner 是"这条记忆归谁"的唯一字段，None 会被读侧当成"老数据没这一列"
# （同一个 NULL 两种含义 = 审计毒药）。'unattributed' 是明确的"当时没凭证"，
# 能被筛选、能被 `owner-fix` 补归属。
# 2026-11-11：原先 `mcp_server.py:55` 自己定义了一份同名常量，现统一到这里再被两边引用
# ——两处各写一份哨兵值，正是本文件反复警告的"两份实现必然漂"。
# 2026-11-11：哨兵已下沉 `fish_common`（见该文件说明）；同名绑定由上面的
# from-import 提供，既有引用不用改。删掉这里是为了**只有一处真相**。
CREDENTIAL_NO_TOKEN = "凭证状态：未提供凭证（无凭证写入的归属会记成 unattributed）"
CREDENTIAL_UNKNOWN = ("凭证状态：未知（这条 token 不在库中——明文只在签发时输出一次，"
                      "丢了就重签：同一个 session_name → 同一个公民，只换凭证）")
CREDENTIAL_EXPIRED_FMT = "凭证状态：已过期（{expires_at}）；重跑 session_start 换一条"
def identity_source_line(citizen_id=None, *, registered=False) -> str:
    """一行写明**这条回执的身份是从哪来的**（三档，MCP / CLI 共用）。

      · `token（session_tokens）`：有一条**有效**凭证，归属以它为准；
      · `register 登记（不参与归属）`：register 的回执——它只建公民、不绑定会话，
        所以这条身份不参与任何写入归属；
      · `未归属`：什么都没有（没带凭证，也不是 register 的回执）。

    判定不在这一层：`citizen_id` 必须由 `validate_session_token` 算出来
    （全仓唯一一处"什么算有效"）。这里只负责措辞。
    """
    if citizen_id:
        return IDENTITY_SOURCE_TOKEN
    return IDENTITY_SOURCE_REGISTER if registered else IDENTITY_SOURCE_UNATTRIBUTED
def credential_state_line(conn, token) -> str:
    """本条凭证**处于什么状态**（一行五档，MCP / CLI 共用唯一实现）。

    五档：没带凭证 / 不在库中 / 已吊销 / **已过期** / 永久·剩余 N 小时。
    与 `ident.status` 的分工：后者只回答"这次调用能不能署名"，这一行走得更远——
    AI 看到「身份未核」时分得清是**哪种**未核，才知道该不该重签（重签是唯一出口）。

    **2026-10-26 修（评审 🟠1）**：旧实现只有四档，**没有「已过期」**。过期凭证落到
    最后一档，被 `remaining_token_hours` 的 `max(1, …)` 念成「剩余 1 小时」——而同一
    回执的「身份来源」行老实写着「未归属」。同一屏两句话，一句说还能用、一句说不管用，
    读的人只会相信前一句（它更具体），于是带着死凭证去写入、把归属丢成 unattributed。
    现在按 `_is_expired` 单列一档，并把 `expires_at` 原值印出来（可核对，不用猜）：
    过期与吊销的处置**不同**（吊销要查是谁吊销的，过期只需重签），所以档位必须分开。

    **判定仍然不在这里做**：吊销与到期都读 `lookup_session_token` 的现查结果，
    "什么算失效"永远只有 `validate_session_token` 一个答案；这里只负责措辞。
    非标准 `expires_at`（坏数据）**不许把 whoami 弄崩**：`remaining_token_hours`
    抛 ValueError 时回落打印原始值——探针崩掉的代价比读数不精确大得多（评审 ⚪12）。
    """
    if not token:
        return CREDENTIAL_NO_TOKEN
    info = lookup_session_token(conn, token)
    if info is None:
        return CREDENTIAL_UNKNOWN
    if info["revoked_at"]:
        return f"凭证状态：已吊销（{info['revoked_at']}）——重跑 session_start 换一条"
    if not info["expires_at"]:
        return "凭证状态：永久（随会话 end 吊销）"
    # 2026-10-26（复审 ⚪N10）：**先校验格式，再判断"过没过期"**。
    #
    # 旧实现直接问 `_is_expired`，而它靠"定长 UTC 的字典序 = 时序"工作——
    # 垃圾值 `!!bad!!` 与 `2026-…` 比较时字典序更小，于是被判成**已过期**，
    # 回执念出「已过期（!!bad!!）」：把一条数据损坏说成了一个具体事实。
    # 先 `parse_utc` 校验：解析不了就统一说"无法解析"（格式坏 = 归因未知，
    # 不是"过期"也不是"还有几小时"）。判定仍只有 `validate_session_token` 一处。
    if parse_utc(info["expires_at"]) is None:
        return (f"凭证状态：到期时刻无法解析（{info['expires_at']!r}）"
                "——这条凭证不算有效（归属会被记成 unattributed）；"
                "重跑 session_start 换一条")
    if _is_expired(info["expires_at"]):
        return CREDENTIAL_EXPIRED_FMT.format(expires_at=info["expires_at"])
    try:
        hours = remaining_token_hours(info["expires_at"])
    except ValueError:
        # 坏 `expires_at`（不是定长 UTC）：如实印原值 + 说明无法解析。
        # 不抛：whoami 是探针，崩在措辞层等于让"我这条凭证怎么了"这个问题无解。
        return (f"凭证状态：到期时刻无法解析（{info['expires_at']!r}）"
                "——重跑 session_start 换一条")
    return f"凭证状态：剩余 {hours} 小时"
def _session_identity_row(conn, session_key):
    """**第一级解析**：`session_identity`（N 个会话键 → 1 个公民，方案 A）。

    键一律剥掉 `#role` 尾（113 §1-5：`identity_key_of` 是全仓唯一剥离点）——
    W4 **不执行** 110-⑥ 的去尾，所以 `issue_agent_token` 的子键仍带 `#<role>`，
    而身份行只按"会话 id"登记。

    为什么公民必须仍在 `citizens_all` 且状态 active：身份行是**投影**，公民可能已退役/
    归档。投影指向一个退役公民时，正确行为是"查不到"（让上层重新解析），不是复活它。
    表不存在（老库还没 migrate）也返回 None：那不是"这个会话没身份"，而是"这张表还没有"，
    但两者对调用方的动作是同一条（继续走下一级），所以不值得抛。
    """
    try:
        row = conn.execute(
            "SELECT citizen_id,origin,inherited_from,evidence FROM session_identity "
            "WHERE session_key=?", (fish_lineage.identity_key_of(session_key),)).fetchone()
    except sqlite3.Error as exc:
        warn_ignored('fish.py _session_identity_row', exc)
        return None
    if not row:
        return None
    citizen = load_citizen(conn, row[0])
    if citizen is None or str(citizen.get("status") or "") != "active":
        return None
    citizen.update({"identity_origin": row[1], "inherited_from": row[2],
                    "identity_evidence": row[3]})
    return citizen
def find_session_citizen(conn, session_key, *, allow_identity=True):
    """按宿主会话标识找**活跃**会话公民（两级解析，113 §5.2）。

      1. `session_identity`：会话级投影（子会话继承父公民后，子键就登记在这里）；
      2. `citizens_all.host_session_key`：W4 之前的唯一一级（单值列，老数据仍靠它）。

    只认 status='active'：退役/归档的公民不该被一条会话标识"复活"。
    `allow_identity=False` 只走第 2 级——留给"回退/诊断"与变异判据（113 §5.8 的变异 ②）。
    """
    if not session_key:
        return None
    if allow_identity:
        found = _session_identity_row(conn, session_key)
        if found is not None:
            return found
    return _citizen_row(conn.execute(
        "SELECT citizen_id,display_name,kind,role,host,status,account_id,"
        "host_session_key,last_seen_at,archived_at FROM citizens_all "
        "WHERE host_session_key=? AND status='active' ORDER BY created_at LIMIT 1",
        (session_key,)).fetchone())
def _write_session_identity(conn, session_key, citizen_id, *, origin, inherited_from,
                            evidence, at=None):
    """写一行身份投影（幂等 UPSERT；**键剥 `#role`**）。身份解析的唯一写口。

    为什么是 UPSERT 而不是 INSERT：同一会话重跑 ensure 必须落在同一行（`session_key`
    是主键）；而"重跑"正是本波要保护的动作（113 §5.4）。
    """
    stamp = at or now_utc()
    conn.execute(
        "INSERT INTO session_identity (session_key,citizen_id,inherited_from,origin,"
        "evidence,created_at,observed_at) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(session_key) DO UPDATE SET citizen_id=excluded.citizen_id, "
        "inherited_from=excluded.inherited_from, origin=excluded.origin, "
        "evidence=excluded.evidence, observed_at=excluded.observed_at",
        (fish_lineage.identity_key_of(session_key), citizen_id, inherited_from, origin,
         evidence, stamp, stamp))
def load_citizen(conn, citizen_id):
    """读一个公民（走 citizens_all：冷热一致，owner 永不悬空）。"""
    if not citizen_id:
        return None
    return _citizen_row(conn.execute(
        "SELECT citizen_id,display_name,kind,role,host,status,account_id,"
        "host_session_key,last_seen_at,archived_at FROM citizens_all "
        "WHERE citizen_id=?", (citizen_id,)).fetchone())
def touch_citizen(conn, citizen_id, *, at=None) -> int:
    """刷新 last_seen_at。返回受影响行数（0 = 它不在热表里，通常意味着已归档）。"""
    return conn.execute("UPDATE citizens SET last_seen_at=? WHERE citizen_id=?",
                        (at or now_utc(), citizen_id)).rowcount
def find_or_create_session_citizen(conn, *, session_key, evidence, host, display_name,
                                   account_id=None, kind="visitor", role=None,
                                   identity_origin="direct", commit=True) -> dict:
    """按会话标识找活跃公民（两级解析）；没有就建。返回 dict（含 created 标记）。

    **这是幂等的全部实现**（18 §10：token 丢了重跑 start → 公民不变、凭证换新）。
    主键选 host_session_key 而不是 display_name：库里已有 `dsh-main` vs
    `dsh-维护工程师` 这种不一致，拿名字当主键意味着改名就断血缘（17 §2.4）。

    复用只补两件事，**绝不改名**（改名让历史记忆指向一个陌生的名字）：
      · last_seen_at = now（活跃痕迹）
      · account_id 原来为空、这次能推断出来 → 补上；已有值不覆盖（那是别的会话的归属）

    2026-11-25（113 W4 · §5.2）：**新建公民时同事务写 `session_identity`**。
    `identity_origin` 只有两个合法来源：`direct`（根会话/显式绑定）与 `provisional`
    （父链存在但整链无身份）。写在这里而不是调用方，是因为"新建公民"与"登记身份"
    必须同事务——只建不登记等于下一次解析又查不到，子会话便会再建一个。
    """
    found = find_session_citizen(conn, session_key)
    if found:
        citizen_id = found["citizen_id"]
        touch_citizen(conn, citizen_id)
        if account_id and not found.get("account_id"):
            conn.execute("UPDATE citizens SET account_id=? WHERE citizen_id=?",
                         (account_id, citizen_id))
            found["account_id"] = account_id
        # 返回的是**库里生效的** account_id（没覆盖就仍是原来那个）：输出与库一致，
        # 否则"打印说 A、库里是 B"是审计时最难解释的那种不一致。
        return {"citizen_id": citizen_id, "created": False,
                "display_name": found["display_name"],
                "account_id": found.get("account_id"),
                "archived": bool(found.get("archived_at"))}
    citizen_id = ulid()
    stamp = now_utc()
    conn.execute(
        "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at,"
        "is_owner,account_id,device_id,host_session_key,last_seen_at,archived_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (citizen_id, display_name, kind, role, host, "active", stamp,
         0, account_id, None, session_key, stamp, None))
    _write_session_identity(conn, session_key, citizen_id, origin=identity_origin,
                            inherited_from=None, evidence=evidence, at=stamp)
    append_event(conn, "citizen.registered",
                 {"citizen_id": citizen_id, "evidence_level": evidence},
                 {"kind": "citizen", "id": citizen_id},
                 {"display_name": display_name, "kind": kind, "role": role,
                  "host": host, "host_session_key": session_key,
                  "account_id": account_id, "via": "session.start"}, commit=commit)
    return {"citizen_id": citizen_id, "created": True, "display_name": display_name,
            "account_id": account_id, "archived": False}
# ------------------------------------------------------ 工作区 → 项目映射（读写口）
# 依据与归一化纪律见 _WORKSPACE_MAP_DDL 的说明。这一节只做"路径 ↔ 项目"的映射，
# 不碰档期（那在下面）、也不碰注入包渲染（那在 build_injection_pack 附近）。
_WORKSPACE_MAP_FIELDS = ("path_prefix", "project", "note", "created_at")
# 正文里抓绝对路径：盘符 + 分隔符之后，遇到空白或常见中英文标点就截断。
# 刻意把反引号/引号/右括号列进"停止字符"：正文里的路径常被 `…`、"…"、（）包着，
# 那些标点是正文的，不是路径的一部分。
_ABS_PATH_RE = re.compile(r"[A-Za-z]:[/\\][^\s\"'`（）()，,；;：:｜|\[\]<>]+")
_PATH_FILE_EXTS = frozenset({
    ".md", ".txt", ".json", ".jsonl", ".py", ".js", ".ts", ".mjs", ".ps1", ".bat",
    ".yml", ".yaml", ".toml", ".ini", ".cfg", ".log", ".csv", ".xlsx", ".docx",
    ".pptx", ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".zip",
    ".sln", ".csproj", ".cs", ".html", ".css", ".exe", ".dll",
})
# seed 的降噪清单：这些目录不是"某个项目的工作区"。
# 刻意**不**整片跳过 c:/users —— 临时目录与部分真实工作区就在用户目录下；
# 只跳真正的系统目录与 AppData（那里全是工具自己的状态文件）。
# `.workbuddy` 同理：那是 agent 的状态/记忆目录，不是代码工作区
# （它常出现在路径里，如 `.../anyi-studio-AI工作空间/.workbuddy/memory`——
#  真正的候选是它的父目录，不是它自己）。
_SEED_SKIP_PREFIXES = ("c:/windows", "c:/program files", "c:/programdata")
_SEED_SKIP_PARTS = ("/appdata/", "/.workbuddy/")
def normalize_workspace_path(path) -> str:
    """把工作区路径归一成 workspace_map 的**存法**与**比法**：正斜杠 · 无尾斜杠 · 全小写。

    三条规则各自的理由：
      · 正斜杠：Windows 上 `\\` 与 `/` 混用是常态（手输、日志、JSON 各不同一套），
        不统一就会出现"明明映射了却匹配不上"；主键也只能有一个写法。
      · 无尾斜杠：`/a/b/` 与 `/a/b` 是同一个目录，而前缀匹配是字符串操作，差一个
        字符就漏。根路径（`/`、`c:/`）保留最小形态，不塌成空串。
      · 全小写：Windows 的路径比较不区分大小写。这是**本机平台的取舍**——在 POSIX
        大小写敏感的系统上，两个只差大小写的路径会被当成同一个（已在此声明）。

    UNC（`\\\\server\\share`）会被压成 `/server/share`：两侧用同一条归一化，
    匹配语义不受影响；显示上少一个斜杠是可接受的代价（比多一套分支可靠）。

    空值/纯空白返回空串：由调用方决定它是"写入错误"还是"这次没给"。
    """
    if path is None:
        return ""
    raw = str(path).strip()
    if not raw:
        return ""
    s = raw.replace("\\", "/")
    s = re.sub(r"/+", "/", s)
    # 2026-10-24 修（自查发现）：之前漏了 `.` / `..` 归一化，导致
    # `/a/b/../bc` 被按字面前缀判成 `/a/b` 的子路径（它实际是 `/a/bc`）。
    # 虽然 process.cwd() 返回的路径本就归一，但 `--cwd` 与 hook payload 都是
    # 可构造输入——按"判据要能挡住坏输入"的原则补上。
    # ⚠️ 两个坑，都是实测出来的：
    #   ① posixpath.normpath('c:/') == 'c:'（去掉根斜杠）→ 会破坏下面"根路径保留最小形态"
    #      的约定，所以要把盘符根补回 `c:/`；
    #   ② 必须用 posixpath 而不是 os.path：后者在 Windows 上会把 `/` 转回 `\`，
    #      让上面两行的正斜杠归一化白做。
    import posixpath
    s = posixpath.normpath(s)
    # ⚠️ 顺序不能反：先去尾斜杠，再补盘符根的斜杠。
    # 反过来的话，补上的 `/` 会被这条 while 立刻砍掉（实测踩过：C:\ 出来是 c:）。
    while len(s) > 1 and s.endswith("/"):
        s = s[:-1]
    # ⚠️ 正则必须容忍大写盘符：此时还没走到末尾的 s.lower()，
    # `C:\` 归一后是 `C:`，用 r"[a-z]:" 会匹配不上、盘符根就不补斜杠了（实测踩过）。
    if re.fullmatch(r"[A-Za-z]:", s):
        s += "/"
    return s.lower()
def resolve_project_by_cwd(conn, cwd) -> "str | None":
    """按**最长前缀 + 路径分量边界**把工作区路径解析成项目前缀；没有匹配返回 None。

    完整语义（最长前缀 / 分量边界 / 读侧归一 / canonical 兜底 / tie-break）只有一份：
    `fish_surface.longest_prefix`。本函数只做两件事——**查表**，然后把比对交出去。

    为什么在 Python 里全表扫、不用 SQL LIKE：前缀里的 `_` / `%` 是 LIKE 的通配符，
    要正确匹配就得转义（转义写错是**静默错配**，最难查的那类 bug）；而这张表是按
    "项目"维护的配置表，行数是个位数。全扫 + 内存里比长度更不容易错，真到几千行再换。

    为什么读侧还要归一一次（在 `longest_prefix` 里）：库里若有人手写了大写/尾斜杠的行，
    这里不让它变成幽灵路径（写侧 `set_workspace_mapping` 已经归一，读侧这次是防手写数据）。

    为什么还要 canonical 兜底：junction / symlink / 8.3 短路径会让同一个物理目录有两种
    写法（2026-10-27 的现场）。它只在文本没命中时补一次，最长前缀与分量边界语义不变。

    **没有匹配返回 None，绝不回退到"猜一个"**：R1 的病根就是猜（见
    `_resolve_injection_project` 的四档说明）。返回 None 让上层能直说"未声明"。
    """
    pairs = conn.execute(
        "SELECT path_prefix, project FROM workspace_map").fetchall()
    hit = fish_surface.longest_prefix(cwd, pairs)
    return hit[1] if hit else None
def list_workspace_candidates(conn, cwd) -> list:
    """cwd **之下**（严格子目录）已映射的项目。**只读查询，一行都不改。**

    为什么需要它（2026-10-24 真实踩坑）：注入包只写「项目 : （未声明）」时，
    人分不清是 ①hook 没传 cwd、②cwd 不在映射里、③解析算法坏了——三种处置完全不同，
    输出却一模一样。②里最常见的一种是"我就在某个已映射项目的**父目录**里干活"
    （典型：`workspace/learn` 下放着 8 个项目，每个都映射了，只有父目录没映射）。
    把 cwd 之下已有的映射列出来，人就能直接抄一条 `fish workspace set` 修好，
    不用去猜、也不用读库。

    为什么是"严格子目录"（不含 cwd 自己）：等值那条一旦存在，`resolve_project_by_cwd`
    就已经命中了，根本走不到"未声明"分支——把它算成候选是给一条不可达的分支写文案。

    为什么在 Python 里比前缀、不用 SQL LIKE：与 `resolve_project_by_cwd` 同一条理由
    （LIKE 的 `_`/`%` 是通配符，转义写错是静默错配）；这张表按项目维护，行数是个位数。

    返回：[{path_prefix, project, note}]，**由浅到深**排序（先看最外层的项目目录）；
    同长度按路径字典序，保证输出不随查询顺序漂。没有任何候选时返回空列表（调用方
    据此决定"别写一堆废话"）。
    """
    target = normalize_workspace_path(cwd)
    if not target:
        return []
    found = []
    for prefix, project, note in conn.execute(
            "SELECT path_prefix, project, note FROM workspace_map").fetchall():
        norm = normalize_workspace_path(prefix)      # 防手写数据：库里若有大写/尾斜杠
        if not norm or norm == target:
            continue
        if not norm.startswith(target + "/"):
            continue
        found.append({"path_prefix": norm, "project": project, "note": note})
    found.sort(key=lambda it: (len(it["path_prefix"]), it["path_prefix"]))
    return found
def set_workspace_mapping(conn, path, project, *, note=None) -> dict:
    """新增或更新一条映射（唯一写入口：CLI `fish workspace set`）。

    返回 {"path_prefix", "project", "note", "created"}；created=False 表示覆盖了旧行。

    为什么用 UPSERT 而不是"先查再插"：两句 SQL 之间没有并发保护，而"同一路径两条
    映射"在语义上就是错的（主键也会拒）。UPSERT 把判定交给库自己，一次往返。
    **created_at 只在第一次写**：它记的是"这条路什么时候被声明归属"，不是最后修改
    时间；要最后修改时间该另加一列，不在这里混成一个含义模糊的字段。
    **note 是声明式覆盖**：不传就清空（想保留旧备注就把旧值带上）。半覆盖（没给就
    保持原值）会让"这条到底是什么备注"取决于它被改过几次，比清空更难解释。
    """
    prefix = normalize_workspace_path(path)
    if not prefix:
        raise ValueError("路径不能为空")
    if not isinstance(project, str):
        raise TypeError(f"project 必须是字符串，收到 {type(project).__name__}")
    name = project.strip()
    if not name:
        raise ValueError("项目前缀不能为空")
    if note is not None and not isinstance(note, str):
        raise TypeError(f"note 必须是字符串，收到 {type(note).__name__}")
    clean_note = (note or "").strip() or None
    existed = conn.execute(
        "SELECT 1 FROM workspace_map WHERE path_prefix=?", (prefix,)).fetchone()
    try:
        conn.execute(
            "INSERT INTO workspace_map (path_prefix, project, note, created_at) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(path_prefix) DO UPDATE SET project=excluded.project, note=excluded.note",
            (prefix, name, clean_note, now_utc()))
        append_event(conn, "workspace.mapped",
                     {"citizen_id": "system", "evidence_level": "signed"},
                     {"kind": "workspace", "id": prefix},
                     {"project": name, "note": clean_note, "created": not bool(existed)}, commit=False)
        conn.commit()
    except Exception: conn.rollback(); raise
    return {"path_prefix": prefix, "project": name, "note": clean_note,
            "created": not bool(existed)}
def list_workspace_mappings(conn) -> list:
    """全部映射（按路径升序）。字段见 _WORKSPACE_MAP_FIELDS。"""
    rows = conn.execute(
        "SELECT path_prefix, project, note, created_at FROM workspace_map "
        "ORDER BY path_prefix").fetchall()
    return [dict(zip(_WORKSPACE_MAP_FIELDS, r)) for r in rows]
def remove_workspace_mapping(conn, path) -> bool:
    """删掉一条映射；返回是否真的删了（幂等：不存在 → False，不报错）。"""
    prefix = normalize_workspace_path(path)
    if not prefix:
        raise ValueError("路径不能为空")
    try:
        cur = conn.execute("DELETE FROM workspace_map WHERE path_prefix=?", (prefix,))
        removed = bool(cur.rowcount)
        if removed:
            append_event(conn, "workspace.unmapped",
                         {"citizen_id": "system", "evidence_level": "signed"},
                         {"kind": "workspace", "id": prefix}, {"removed": True}, commit=False)
        conn.commit()
    except Exception: conn.rollback(); raise
    return removed
def _workspace_path_from_text(raw) -> str:
    """把一个正文/路径片段裁成候选工作区路径（已归一化）；不可用返回空串。

    裁剪三件事：
      1. 去掉正文包裹字符与句末标点（反引号、引号、右括号、顿号/句号等）；
      2. 只在**后缀是已知文件扩展名**时退到父目录（`.../README.md` → 目录）；
      3. 归一化（小写/正斜杠/无尾斜杠）。
    最后挡掉盘符根（`c:`）、根路径 `/` 与系统目录：它们不是"某个项目的工作区"。
    """
    s = (raw or "").strip().rstrip("`'\"）)，,；;：:｜|]>.。")
    if not s:
        return ""
    s = normalize_workspace_path(s)
    if Path(s).suffix.lower() in _PATH_FILE_EXTS:
        s = normalize_workspace_path(str(Path(s).parent))
    if not s or s == "/" or re.fullmatch(r"[a-z]:", s):
        return ""
    if s.endswith("/.workbuddy"):
        return ""
    if any(s == p or s.startswith(p + "/") for p in _SEED_SKIP_PREFIXES):
        return ""
    if any(part in s for part in _SEED_SKIP_PARTS):
        return ""
    return s
def _covering_mapping(rows, path):
    """在已有映射里找**覆盖** path 的那一条（等值或祖先前缀，最长者优先）。"""
    best = None
    for row in rows:
        prefix = row["path_prefix"]
        if path == prefix or path.startswith(prefix + "/"):
            if best is None or len(prefix) > len(best["path_prefix"]):
                best = row
    return best
def workspace_seed_candidates(conn, root) -> dict:
    """从现有记忆反推"工作区 → 项目"候选。**只读：不写库、不改任何行。**

    数据来源（都是已经落库的事实，不靠猜）：
      1. `code_index.file_path` 本身是绝对路径 → 它（或其父目录）是候选；
      2. 记忆正文文件（`<root>/<file_path>`）里出现的绝对路径 → 候选。
    为什么必须有第 2 条：当前 `file_path` 全是 `projects/<项目>/<编码>.md`（记忆正文
    的存放位置），它并不携带"工作区"信息；真正的工作区路径只出现在正文叙述里。

    也正因为路径是"正文提到的"，它可能是**别人的**项目（实测：ANYI 的正文里提到
    `I:/app/codex`）——所以 seed 只打印建议，**不自动写入**。归属由人确认后走
    `fish workspace set`。

    置信规则：
      · 同一路径只出现在一个项目下 → suggest（可考虑 set）；
      · 同一路径出现在多个项目下 → conflict（**不建议**，人要先判断）；
      · 某路径是**另一个项目**路径的祖先 → conflict（共享父目录：整片归属会把
        别的项目也算进来）；
      · 同一路径的真子目录已被父路径建议覆盖 → 只留父路径（映射是前缀语义，
        列出子目录只会让建议表变长而不增加信息）；
      · 已被现有映射覆盖：归属一致 → mapped；不一致 → conflict（附 existing）。
      · 候选目录必须在本机**真实存在**：不存在的多半是笔误或文件引用，写进映射只会
        多一条永远匹配不上的幽灵记录。
      · 记忆根目录（`<root>` 及其下 `projects/`、`blobs/`、`_audit/`…）整片跳过：
        那是**数据仓库**，不是谁的工作区。正文里经常提到 `.../p0/data/index.sqlite`
        这类路径，把它们当候选只会污染建议表。
    """
    counts = {}

    def bump(path, project):
        if path and project:
            key = (path, project)
            counts[key] = counts.get(key, 0) + 1

    rows = conn.execute(
        "SELECT project, file_path FROM code_index "
        "WHERE file_path IS NOT NULL AND project IS NOT NULL AND project <> '' "
        "AND status='active'").fetchall()
    for project, rel in rows:
        p = Path(rel)
        if p.is_absolute():
            bump(_workspace_path_from_text(str(p if p.is_dir() else p.parent)), project)
            continue
        try:
            text = (Path(root) / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue                  # 正文文件缺失不算错：seed 只是建议器
        for m in _ABS_PATH_RE.finditer(text):
            bump(_workspace_path_from_text(m.group(0)), project)

    by_path = {}
    for (path, project), n in counts.items():
        by_path.setdefault(path, {})[project] = n

    # 只保留本机真实存在、且不在记忆根目录内部的工作区目录
    # （不存在的路径写进映射 = 幽灵记录；根目录内部是数据仓库，不是工作区）
    root_norm = normalize_workspace_path(root)
    paths = [p for p in sorted(by_path)
             if Path(p).is_dir()
             and not (root_norm and (p == root_norm or p.startswith(root_norm + "/")))]

    # 共享父目录：path 的某个子孙路径带有别的项目 → path 不能整片归属某个项目。
    ambiguous = set()
    for path in paths:
        mine = set(by_path[path])
        for other in paths:
            if other == path or not other.startswith(path + "/"):
                continue
            if set(by_path[other]) - mine:
                ambiguous.add(path)
                break

    existing = list_workspace_mappings(conn)
    suggest, conflicts, mapped = [], [], []
    for path in paths:
        projects = by_path[path]
        cover = _covering_mapping(existing, path)
        existing_project = cover["project"] if cover else None
        if len(projects) > 1:
            conflicts.append({
                "path": path, "project": None, "projects": sorted(projects),
                "count": sum(projects.values()),
                "existing": existing_project, "reason": "multi_project"})
            continue
        project = next(iter(projects))
        item = {"path": path, "project": project, "count": projects[project],
                "existing": existing_project}
        if path in ambiguous:
            conflicts.append({**item, "projects": [project], "reason": "shared_parent"})
            continue
        if cover:
            if existing_project == project:
                mapped.append(item)
            else:
                conflicts.append({**item, "reason": "mapping_mismatch"})
            continue
        suggest.append(item)

    # 降噪：父路径已被建议时，其子路径不再单列（前缀匹配本来就被父路径覆盖）。
    suggest = [it for it in suggest
               if not any(other["path"] != it["path"]
                          and it["path"].startswith(other["path"] + "/")
                          for other in suggest)]
    return {"suggest": suggest, "conflicts": conflicts, "mapped": mapped}
def current_open_stints(conn, citizen_id) -> list:
    """当前所有开档期（按 from_ts 升序）。没有返回空列表。

    2026-10-24 起"复数"才是真相：一个会话可以同时在多个项目上开档（AI 常跨项目干活）。
    排序键是 `(from_ts, id)` 而不是单独 from_ts：`now_utc()` 是**秒级**时间戳，同一秒内
    开两个档期完全可能（测试与批量脚本里常见），少了 id 兜底输出顺序就不稳定——
    而 status 的打印顺序、以及 current_open_stint 选"最近一个"的结果都会随之漂。
    """
    rows = conn.execute(
        "SELECT id,citizen_id,account_id,project,from_ts,set_by FROM project_stints "
        "WHERE citizen_id=? AND to_ts IS NULL ORDER BY from_ts ASC, id ASC",
        (citizen_id,)).fetchall()
    return [dict(zip(_STINT_READ_FIELDS, r)) for r in rows]
def current_open_stint(conn, citizen_id):
    """**兼容保留**：当前开档期里**最近开的**那一个（多项目时就是"主项目"）。没有返回 None。

    保留理由（不是历史包袱）：`session_status` 等调用方要一轮轮切到列表版；
    一次性把"档期语义变更"和"所有调用方改写"混在同一轮里，出问题时切不开是语义错
    还是调用方错。所以本轮把单数版保留为**列表版的最后一个**——它的返回值语义
    （"最近开的那条"）与旧实现一致，旧调用方的行为不变。

    注意它**不再是**"唯一开档期"：想知道全部，用 current_open_stints。
    """
    stints = current_open_stints(conn, citizen_id)
    return stints[-1] if stints else None
def close_open_stints(conn, citizen_id, *, reason, at=None) -> int:
    """关掉该公民**所有**开档期，返回关闭条数。`end_session` 用它。

    "关所有"是刻意的（与本轮"多项目并行"并不矛盾）：`session end` 的语义是**会话结束**，
    会话结束了，它在任何项目上的档期都该收尾——多项目并行只在会话活着时成立。
    """
    return conn.execute(
        "UPDATE project_stints SET to_ts=?, closed_reason=? "
        "WHERE citizen_id=? AND to_ts IS NULL",
        (at or now_utc(), reason, citizen_id)).rowcount
def open_project_stint(conn, citizen_id, project, *, account_id=None, set_by=None) -> dict:
    """把公民切到 project 档期。返回 {action, project, stint_id, from_ts, closed}。

    2026-10-24 改：**不再"先关后开"**。理由：多项目并行是合法状态，不是切换。
    新语义（三条，都在判据里钉住）：
      · 该项目**已有**开档期 → 复用（action='reused'，不写新行）
      · 该项目**没有**开档期 → 新建（action='opened'）
      · **不关任何其他项目的档期**（关自己的档期是 end_session 的事）
    为什么同项目要复用而不是重开：档期是一段时间，不是打卡记录。重开会在表里多切一条
    边界，"这个会话什么时候在哪个项目"被自己的重试切碎，追溯反而失真。

    `closed` 字段**保留但恒为 0**：语义是"本次调用关掉了几条档期"，本轮起本函数一条
    都不关。老调用方读它得到的仍是"本函数关了几条"，所以不用改。

    原来那句 `close_open_stints(..., reason="project_switch")` 已删（函数本身保留，
    end_session 还在用）。删它是本轮最核心的语义变更：判据 C 拿它当变异目标——
    把"先关后开"加回去，ANYI 的 to_ts 就不再是 NULL，判据当场红。
    """
    project = (project or "").strip()
    if not project:
        raise ValueError("project 不能为空（想只签发凭证不记档期，就别传 --project）")
    for st in current_open_stints(conn, citizen_id):
        if st["project"] == project:
            return {"action": "reused", "project": project, "stint_id": st["id"],
                    "from_ts": st["from_ts"], "closed": 0}
    from_ts = now_utc()
    cur = conn.execute(
        "INSERT INTO project_stints (citizen_id,account_id,project,from_ts,set_by) "
        "VALUES (?,?,?,?,?)", (citizen_id, account_id, project, from_ts, set_by))
    return {"action": "opened", "project": project, "stint_id": cur.lastrowid,
            "from_ts": from_ts, "closed": 0}
def _normalize_projects(projects=None, project=None) -> list:
    """把 projects（字符串 / 字符串列表）与兼容关键字 project 归一成**去重保序**的列表。

    为什么要归一：同一个事实有三个入口（`--project A --project B`、`projects=[...]`、
    旧关键字 `project=`），各写一套拼接逻辑必然漂。归一之后下面只有一条路要走。

    元素规则：
      · 必须是字符串；strip 后为空视为"没给"（跳过，不报错——空值多半来自计算出来的
        默认值，为它报错会把"本轮不开档期"这种合法情况变成立即失败）；
      · 非字符串（比如误传 dict）直接 TypeError：那是调用方写错，静默 str() 只会把错埋深；
      · 去重保序：同一个项目传两次不该开两条档期（第二次本就是 reused，但别让它先
        走一遍"没有→插入"的判断浪费一次往返）。
    """
    if projects is None:
        raw = []
    elif isinstance(projects, str):
        raw = [projects]
    elif isinstance(projects, (list, tuple)):
        raw = list(projects)
    else:
        raise TypeError(
            f"projects 必须是字符串或字符串列表，收到 {type(projects).__name__}")
    if project is not None:
        raw.append(project)              # 旧关键字与列表并用时按"追加"处理
    out = []
    for item in raw:
        if not isinstance(item, str):
            raise TypeError(
                f"projects 的元素必须是字符串，收到 {type(item).__name__}")
        name = item.strip()
        if name and name not in out:
            out.append(name)
    return out
def _prove_citizen_binding(conn, target_id, *, session_key, bind_token=None):
    """S-01：绑定**已存在**公民必须证明持有它（`start` 与 `ensure` 共用，唯一实现）。

    原先是 `start_session` 里的闭包 `_prove`；W4 提成模块级是因为 `session ensure`
    的 `--citizen` 锚路径走同一道门——两份证明逻辑必然漂，而这是安全门。

    证明口径（2026-10-28 晚，按外部审查 🔴4 修正）：**持有过**即证明——接受该公民的
    **历史 token**（含已轮换/已过期/已吊销），因为轮换紧接着就吊销旧 token，要求
    "有效"会让第二次重绑永久锁死。
    """
    proof = str(bind_token or os.environ.get("FISH_SESSION_BIND_TOKEN") or "").strip()
    if proof:
        info = lookup_session_token(conn, proof)
        if info is None:
            raise ValueError("bind_token 从未签发过（不是这个系统的凭证）")
        if info["citizen_id"] != target_id:
            raise ValueError("bind_token 不属于目标公民（拒绝：防接管）")
        # 外部审查 🟠7（2026-10-29）：**吊销的历史 token 只在 7 天内可作证明**。
        # 永久接受等于把"泄露后被轮换掉的凭证"升级成长期通行证。
        revoked_at = info.get("revoked_at")
        if revoked_at:
            try:
                age = (parse_utc(now_utc()) - parse_utc(revoked_at)).total_seconds()
            except (TypeError, ValueError):
                age = 0.0
            if age > 7 * 86400:
                raise ValueError("bind_token 已吊销超过 7 天，不再作为证明（请用当前凭证）")
        return
    # 外部审查 🔴5（2026-11-11）：**取消"签发过 token 即放行"**。
    # 那一版（10-29 二改）本意是恢复 hook，但把门做反了——所有真实公民都签发过
    # token，于是"知道 citizen_id 就能接管"对全部真实身份成立。
    # 现在只由**本机身份锚的会话白名单**授权；完整演进史见
    # `fish_trust.identity_anchor_grants` 的 docstring。
    if identity_anchor_grants(target_id, session_key):
        return
    raise ValueError(
        "绑定已有公民需要证明：fish session start --citizen <id> "
        "--bind-token <该公民签发过的任一 token（吊销 7 天内仍有效）>"
        "（或设 FISH_SESSION_BIND_TOKEN）；"
        "本机 hook 请改用身份锚 p0/fish_identity.json"
        "（{\"citizen\": <id>, \"sessions\": [<该会话的 session_key>]}）"
        "——只写 citizen 而不给 sessions 白名单不再放行")
def _forced_citizen(conn, forced, *, session_key, bind_token=None, account_id=None):
    """`--citizen` 显式绑定：校验存在/未归档/证明，返回 `who` dict（不签发、不轮换）。"""
    row = conn.execute(
        "SELECT citizen_id, display_name, account_id, archived_at FROM citizens "
        "WHERE citizen_id=?", (forced,)).fetchone()
    if not row:
        raise ValueError(f"指定的公民不存在：{forced}")
    if row[3]:
        raise ValueError(
            f"指定的公民已归档：{forced}。先跑 `fish citizen-restore {forced}` 把它搬回热表再绑"
            "（还原只恢复身份，不恢复凭证；要凭证请重新 `fish session start`）")
    _prove_citizen_binding(conn, forced, session_key=session_key, bind_token=bind_token)
    who = {"citizen_id": row[0], "created": False, "display_name": row[1],
           "account_id": row[2], "archived": False}
    if account_id and not row[2]:
        conn.execute("UPDATE citizens SET account_id=? WHERE citizen_id=?",
                     (account_id, row[0]))
        who["account_id"] = account_id
    return who
def rotate_token(conn, *, session_key, citizen_id, evidence, channel, ttl_hours=None) -> dict:
    """**人驱动**的换证：吊销该会话全部活凭证，再签一条新的（`session start` 的唯一实现）。

    与 `ensure_session` 的分工就是 W4 那句话（113 §5.1）：`rotate` 会吊销（"重跑 start
    = 换一条能用的"），`ensure` 只要还有活的就原样返回（**绝不轮换**）——hook 每次
    SessionStart 都跑，那里若还轮换，多个进程就会互相踢掉对方的凭证。
    返回 `{"revoked": int, "issued": {...含明文 token...}}`。
    """
    revoked = revoke_session_tokens(conn, session_key=session_key)
    issued = issue_session_token(conn, citizen_id=citizen_id, session_key=session_key,
                                 evidence=evidence, channel=channel, ttl_hours=ttl_hours)
    return {"revoked": revoked, "issued": issued}
def start_session(conn, *, projects=None, project=None, account=None, alias=None,
                  kind="visitor", role=None, environ=None, ttl_hours=None,
                  citizen=None, bind_token=None) -> dict:
    """`session start` 的全部逻辑（CLI 只负责打印）。返回 dict，**含明文 token**。

    六步就是 18 §2 的六步，顺序不能换：
      环境事实 → 证据分级 → 找/建公民 → 轮换 + 签发凭证 → 开档期 → 事件

    2026-10-24：`projects` 支持**多个项目**（CLI 上 `--project` 可重复给）。逐个开档期，
    每个项目的档期互不干扰；一个都没给就只签发凭证、不记档期（合法用法）。
    兼容：旧关键字 `project=` 保留，等价于 `projects=[project]`——既有调用点与
    "单项目"这种最常见叫法都还走它，没必要为改名把调用面全改一遍。

    返回里 `stint` 是**第一条档期**（单项目时就是那一条，旧调用方不用改），
    `stints` 是完整列表（单项目也返回列表，口径统一，调用方不必写两套分支）。
    """
    project_list = _normalize_projects(projects, project)
    ident = build_session_identity(alias=alias, environ=environ)
    account_id = resolve_account_id(account=account, alias=ident["alias"], environ=environ)
    display_name = ident["alias"] or auto_session_display_name(
        ident["host"], ident["session_key"], role)
    # 2026-10-25 加（`EVAL-261025-05` 建议 3）：允许**显式指定公民**。
    # 场景：同一个 AI 在 MCP 侧已经登记成公民 A，但 CLI 这边的 host_session_key 不同，
    # 于是 `find_or_create` 会新建一个 B——两条通道的归属从此对不上，且无法合并。
    # `--citizen` 让调用方把这条会话**直接绑到已存在的公民**上，是那个问题的显式出口。
    # S-01（安全审计 2026-10-28）：证明逻辑在 `_prove_citizen_binding`（与 ensure 共用）。
    forced = (citizen or "").strip() if isinstance(citizen, str) else citizen
    try:
        if forced:
            who = _forced_citizen(conn, forced, session_key=ident["session_key"],
                                  bind_token=bind_token, account_id=account_id)
        else:
            who = find_or_create_session_citizen(
                conn, session_key=ident["session_key"], evidence=ident["evidence"],
                host=ident["host"], display_name=display_name, account_id=account_id,
                kind=kind, role=role, commit=False)
            # 2026-10-29（外部审查 🔴1）：`self:` 认领**不再**要求证明——`session_key` 就是
            # "同一个会话"（重跑 start 幂等由 J7/T3 钉死；接管洞在显式 `citizen=`）。P2-6#1：
            # 原先这里还有一个 `and False:` 死分支（里面调证明），已删。
        # 轮换：同一会话的新凭证生效时，旧的活凭证立即作废。
        # 为什么轮换而不是"留着"：重跑 start 的语义是"拿到一条新的、能用的凭证"，
        # 留着旧的意味着泄露面随时间只增不减；轮换后任一时刻一个会话只有一条活凭证。
        # 与 `ensure_session` 的分工见 `rotate_token` 的 docstring。
        rotated_out = rotate_token(conn, session_key=ident["session_key"],
                                   citizen_id=who["citizen_id"], evidence=ident["evidence"],
                                   channel=ident["channel"], ttl_hours=ttl_hours)
        rotated, issued = rotated_out["revoked"], rotated_out["issued"]
        stints = [
            open_project_stint(conn, who["citizen_id"], name, account_id=account_id,
                               set_by=f"cli:{ident['evidence']}")
            for name in project_list
        ]
        append_event(
            conn, "session.opened",
            {"citizen_id": who["citizen_id"], "evidence_level": ident["evidence"]},
            {"kind": "citizen", "id": who["citizen_id"]},
            # 事件里**只有 hash**，没有明文：不可变表会把任何写进去的东西永久留下。
            {"session_key": ident["session_key"], "evidence": ident["evidence"],
             "channel": ident["channel"], "host": ident["host"],
             "account_id": account_id,
             # project 保持单值口径（只开一个项目时它就是那个项目，多项目时为 None），
             # projects 记全量。两个都写：审计要能回答"这次 start 开了哪几个档期"，
             # 又不能给只看 project 的旧查询突然塞一个逗号串。
             "project": project_list[0] if len(project_list) == 1 else None,
             "projects": project_list,
             "token_hash": issued["token_hash"], "expires_at": issued["expires_at"],
             "citizen_created": who["created"], "rotated": rotated,
             "stint_actions": [{"project": s["project"], "action": s["action"]}
                               for s in stints]}, commit=False)
        conn.commit()
    except Exception: conn.rollback(); raise
    # FB-08：签发后查**同名在用公民**。查在签发之后是刻意的——名字不唯一是既定
    # 决策（不合并、不拦），这里只负责让"你可能又建了一个自己"这件事可见。
    same_name = find_same_name_citizens(
        conn, who["display_name"], who["citizen_id"])
    return {"token": issued["token"], "token_hash": issued["token_hash"],
            "citizen_id": who["citizen_id"], "session_key": ident["session_key"],
            "evidence": ident["evidence"], "channel": ident["channel"],
            "host": ident["host"], "display_name": who["display_name"],
            "account_id": account_id, "created": who["created"],
            "rotated": rotated,
            "stint": stints[0] if stints else None, "stints": stints,
            "warning": ident["warning"],
            "issued_at": issued["issued_at"], "expires_at": issued["expires_at"],
            "project": project_list[0] if len(project_list) == 1 else None,
            "projects": project_list, "archived": who["archived"],
            "name_conflicts": same_name}
def evidence_root(conn, token_hash, *, max_hops=64) -> str:
    """沿 `parent_token` 向上走到**根**，返回根的 evidence（113 §5.6 的防漂白）。

    返回值：`machine` / `inherited` / `self-reported` / `provisional` /
    `unknowable`（走不到根：父行缺失、成环、或超过 `max_hops`）。

    为什么需要它：`inherited` 链的可信度**等于根的等级**。放开一跳之后，一条
    "provisional 根 → 继承 → 再继承"的链如果继续标 inherited，就是把临时身份洗成
    有根身份。所以子凭证的强度必须回到根上取，而不是看父那一行的标签。
    """
    seen, cur, hops = set(), token_hash, 0
    while cur and hops <= max_hops:
        row = conn.execute(
            "SELECT evidence,parent_token FROM session_tokens WHERE token_hash=?",
            (cur,)).fetchone()
        if row is None:
            return "unknowable"
        if not row[1]:
            return str(row[0] or "unknowable")
        if cur in seen:
            return "unknowable"          # 环：链没有根，不许当成"根就是它"
        seen.add(cur)
        cur = row[1]
        hops += 1
    return "unknowable"
def assert_agent_parent_allowed(conn, parent) -> str:
    """子 agent 签发的守卫：父凭证不存在/已失效/证据不达标 → 抛 ValueError。

    2026-11-25（113 W4 · §5.6）：**放开"只许一跳"**。父可以是 `machine` 或
    `inherited`——N≥3 层时，孙必须能拿 inherited 父再签一次；仍拒 `self-reported`
    （自报值被抬成 inherited 就是证据升级，比链式继承更糟）。

    **防漂白靠根**：允许链式之后，"证据升级"改由 `evidence_root` 兜——父是 inherited
    时，必须一路走到 `machine` 根才放行；根不是 machine（provisional/自报/走不到）
    一律拒绝，因为 `channel='parent'` 强制 `evidence='inherited'`（DDL CHECK），
    而把没有 machine 根的链接标成 inherited 正是漂白本身。

    返回根的等级（当前恒为 `machine`），供调用方写事件与回执。
    """
    if parent is None:
        raise ValueError("父 token 不存在：明文只在签发时输出一次，"
                         "丢了就重跑 `fish session start` 拿新的 machine token")
    if parent["revoked_at"]:
        raise ValueError(f"父 token 已吊销（{parent['revoked_at']}），不能为子 agent 背书")
    if parent["expired"]:
        raise ValueError(f"父 token 已过期（{parent['expires_at']}），不能为子 agent 背书")
    grade = str(parent["evidence"] or "")
    if grade == "self-reported":
        raise ValueError(
            "不能链式继承：父 token 的 evidence=self-reported。"
            "自报值再被继承一次就被抬成 inherited（证据升级比链式继承更糟）。"
            "→ 回主会话重跑 `fish session start` 拿一个新的 machine token。")
    if grade not in ("machine", "inherited", "provisional"):
        raise ValueError(f"父 token 的 evidence={grade!r} 不在闭集里，拒绝背书")
    root = evidence_root(conn, parent["token_hash"])
    if root != "machine":
        raise ValueError(
            f"不能继承：这条链的根 evidence={root}（父={grade}）。"
            "inherited 链的可信度等于根的等级，而 channel='parent' 只允许 evidence='inherited'"
            "——把无 machine 根的链接标成 inherited 就是漂白。"
            "→ 让根会话重跑 `fish session start` 拿 machine token，"
            "或用 `fish session ensure`（它会把证据如实降为 provisional）。")
    return root
def issue_agent_token(conn, *, parent_token, role, ttl_hours=None) -> dict:
    """给子 agent 签 inherited 凭证（W4 起允许 N 跳，根必须是 machine）。返回 dict，含明文 token。

    子 agent **不新建公民**（本实现的决定，记录在交付报告里）：
    `citizen_agents` 表还没建（P1-a 只建了 4 张表），现在给子 agent 造一个"独立公民"
    会把子 agent 的动作记成另一个人的历史——而 16 §2.1 要的是"共享父身份、各自可区分"。
    所以：citizen_id 复用父的，区分靠 session_key 的 `#<role>` 后缀 + 凭证上的
    evidence/channel。等 citizen_agents 建表（P1-d）再拆。

    **`#role` 尾保留**（113 §1-5 / W4 **不执行** 110-⑥）：去掉之后子键与父键相同，
    子会话的 SessionStart 会在同一键上轮换、吊销父的活 token。
    """
    role = (role or "").strip()
    if not role:
        raise ValueError("--role 不能为空：子 agent 的身份就是「它为哪个角色干活」")
    parent = lookup_session_token(conn, parent_token)
    assert_agent_parent_allowed(conn, parent)
    child_key = f"{parent['session_key']}#{role}"
    try:
        issued = issue_session_token(
            conn, citizen_id=parent["citizen_id"], session_key=child_key,
            evidence="inherited", channel="parent", parent_hash=parent["token_hash"],
            ttl_hours=ttl_hours)
        # 子凭证不能比父凭证活得久：父失效后它就没有根了，留着等于一条无源凭证。
        if parent["expires_at"] and issued["expires_at"] and \
                issued["expires_at"] > parent["expires_at"]:
            conn.execute("UPDATE session_tokens SET expires_at=? WHERE token_hash=?",
                         (parent["expires_at"], issued["token_hash"]))
            issued["expires_at"] = parent["expires_at"]
        append_event(
            conn, "session.agent_issued",
            {"citizen_id": parent["citizen_id"], "evidence_level": "inherited"},
            {"kind": "citizen", "id": parent["citizen_id"]},
            {"parent_token_hash": parent["token_hash"], "session_key": child_key,
             "role": role, "evidence": "inherited", "channel": "parent",
             "token_hash": issued["token_hash"], "expires_at": issued["expires_at"]}, commit=False)
        conn.commit()
    except Exception: conn.rollback(); raise
    return {"token": issued["token"], "token_hash": issued["token_hash"],
            "citizen_id": parent["citizen_id"], "session_key": child_key,
            "role": role, "evidence": "inherited", "channel": "parent",
            "parent_token_hash": parent["token_hash"], "issued_at": issued["issued_at"],
            "expires_at": issued["expires_at"]}
# ------------------------------------------------------ 会话身份 ensure / 继承（W4 · 方案 A）
#
# 110 §3.3 定案：**身份解析扩到会话级，不动吊销粒度**。`ensure` 与 `rotate` 的分工：
#   rotate（人驱动 `session start`）——换一条能用的，旧的立即吊销；
#   ensure（hook 每次 SessionStart）——幂等，只要还有活的就原样返回，**绝不轮换**。
# 为什么必须拆：SessionStart 会在同一会话上重复触发，两个入口都轮换就会互相吊销
# 对方的凭证（`fish_session_hook.py:29-31` 那句"不在这里调 start"就是这个坑）。
def _session_id_of(session_key) -> str:
    """会话键 → 宿主会话 id（`dsh:abc` / `dsh:abc#role` → `abc`）。判父只能吃裸 id。"""
    key = fish_lineage.identity_key_of(session_key)
    return key.split(":", 1)[1] if ":" in key else key
def _alive_session_token(conn, session_key):
    """该会话键当前**活着**的凭证行（吊销/过期都不算）；没有 → None。

    与 `fish_activity.SQL_SESSION_TOKEN_CITIZEN` 同一条有效性口径，但这里要的是**整行**
    ——ensure 要复用它、而不是重新签发（重签就是轮换，正是本波要禁止的动作）。
    """
    row = conn.execute(
        "SELECT token_hash,citizen_id,evidence,channel,parent_token,issued_at,expires_at "
        "FROM session_tokens WHERE session_key=? AND revoked_at IS NULL "
        "AND (expires_at IS NULL OR expires_at > ?) "
        "ORDER BY issued_at DESC LIMIT 1", (session_key, now_utc())).fetchone()
    if not row:
        return None
    keys = ("token_hash", "citizen_id", "evidence", "channel", "parent_token",
            "issued_at", "expires_at")
    return dict(zip(keys, row))
def _nearest_identity_ancestor(conn, *, host, session_key):
    """父链上**最近一个**有身份的祖先：`(祖先会话键, 公民行)`；没有 → `(None, None)`。

    判父只有一条路（113 §5.7）：`fish_lineage.ancestor_sessions`（投影表优先、投影没行
    就现读宿主会话头）。走不到 / 成环 / 超跳都只是"没有祖先"，**不猜**。
    """
    for sid in fish_lineage.ancestor_sessions(conn, host, _session_id_of(session_key)):
        ancestor_key = fish_activity.make_session_key(host, sid)
        hit = find_session_citizen(conn, ancestor_key)
        if hit is not None:
            return ancestor_key, hit
    return None, None
def _explicit_parent_key(session_key, parent_session):
    """调用方给的父（裸 id 或完整键）→ 完整会话键；**自指/空 → None**。

    只做**键形态归一**，不判存在性（判不到父就不许编，113 §7.1「宁可不推，不串推」）。
    自指必须折成 None：宿主把本会话自己的 id 报成父时（WB 子代理会话报父 sid 的形态），
    当父用会让 `inherited_from` 指向自己，`inherited` 的 CHECK 与祖先遍历都会绕圈。
    """
    parent = str(parent_session or "").strip()
    key = str(session_key or "").strip()
    if not parent or not key:
        return None
    parent_key = parent if ":" in parent else (
        f"{key.split(':', 1)[0]}:{parent}" if ":" in key else "")
    if not parent_key:
        return None
    same = fish_lineage.identity_key_of(parent_key) == fish_lineage.identity_key_of(key)
    return None if same else parent_key
def _inherited_token_shape(conn, ancestor_key, *, channel):
    """继承链上的凭证口径：`(evidence, channel, parent_hash, parent_expires_at)`。

    根是 machine 且祖先**还有活凭证** → 正常继承（`inherited`/`parent`，可级联吊销）；
    否则 → `provisional` 且**不挂父**。这是 113 §5.6 的防漂白落点：没有 machine 根的链
    不许继续标 inherited，而 DDL 规定 `channel='parent'` ⇒ `evidence='inherited'`，
    所以唯一诚实的写法就是降级 + 断开父链（identity 的 `inherited_from` 仍记录血缘）。
    """
    parent = _alive_session_token(conn, ancestor_key)
    if parent is not None and evidence_root(conn, parent["token_hash"]) == "machine":
        return "inherited", "parent", parent["token_hash"], parent["expires_at"]
    return "provisional", channel, None, None
def ensure_session(conn, *, session_key, host, evidence, display_name=None,
                   account_id=None, kind="visitor", role=None, ttl_hours=None,
                   channel="cli", projects=None, citizen=None, bind_token=None,
                   parent_session=None, agent_id=None) -> dict:
    """幂等：有身份就复用，没身份就继承最近有身份祖先 / 退 provisional。**绝不轮换**。

    四支行为（113 §5.4）：
      · 已有身份   → 复用（`session_identity` 第一级 / `host_session_key` 第二级）；
      · `--citizen`→ 显式绑定（证明口径与 `start` 共用 `_prove_citizen_binding`）；
      · `inherited`→ 父链上最近一个有身份的祖先；凭证 `inherited`+`parent`；
      · 无可继承   → 根会话 `direct`（机器事实照旧），有父链但整链无身份 `provisional`。

    `parent_session` / `agent_id`（113 §7.1 · W4.6）：宿主 payload 里**已有父证据**的
    入口（WorkBuddy hook 从 `transcript_path` 反解父、从 `agent_id` 知道这是子代理）。
    给了 `parent_session` 就**不再走 lineage IO**——证据已经在手上，判父只有一处实现
    （`fish_lineage_wb.path_parent`），这里只负责用上；`agent_id` 只作"这是子代理"的
    信号：有父证据但真判不到父时**宁可 provisional，也不造 machine**（宁可不推，不串推）。

    签发规则（**唯一的"写"**）：该 session_key 当前**没有活凭证**时才 `issue_session_token`；
    已经有活的就原样返回（`token=None` + `token_source='existing'`）。
      → 重跑 SessionStart 不会互踢（113 §5.4）
      → 首次启动仍有明文 token（hook 打印得出来）
    **重绑是唯一的例外**（P1-6，2026-11-25）：本次要签给**另一个公民**时旧活凭证当场吊销——不是轮换，是收回。

    返回 `{token, token_source, citizen_id, session_key, evidence, channel, host,
    display_name, account_id, created, identity_origin, inherited_from, issued_at,
    expires_at, stints, projects, project, ...}`。
    """
    key = (session_key or "").strip()
    if not key:
        raise ValueError("ensure_session 需要 session_key（没有会话标识就没有「同一会话」）")
    project_list = _normalize_projects(projects)
    display_name = display_name or auto_session_display_name(host, key, role)
    found = find_session_citizen(conn, key)
    citizen_id = found["citizen_id"] if found else None
    if found is not None:
        # 复用身份时用**库里生效的名字**（与 find_or_create 同一条"绝不改名"）：
        # 继承来的子会话该显示父公民的可读名，而不是它自己的自动占位名。
        display_name = found["display_name"]
    origin = (found or {}).get("identity_origin") or "direct"
    inherited_from = (found or {}).get("inherited_from")
    created = False
    account_id = (found or {}).get("account_id") or account_id
    try:
        if found is not None and (found or {}).get("identity_origin") is None:
            # 老库的第二级命中（`citizens.host_session_key` 有、`session_identity` 没有）：
            # 补一行 direct 投影。这是**一次性回填**，也是"第三级解析以后能查到它"的前提。
            _write_session_identity(conn, key, citizen_id, origin="direct",
                                    inherited_from=None, evidence=evidence)
        ancestor_key = None
        if citizen:
            # 显式锚**永远优先**（与 `start --citizen` 同语义）：人改了身份锚就该生效，
            # 不能被"上一次 ensure 写下的投影"顶掉。
            who = _forced_citizen(conn, (citizen or "").strip(), session_key=key,
                                  bind_token=bind_token, account_id=account_id)
            citizen_id, origin, inherited_from = who["citizen_id"], "direct", None
            display_name = who["display_name"]
            account_id = who.get("account_id") or account_id
            _write_session_identity(conn, key, citizen_id, origin="direct",
                                    inherited_from=None, evidence="machine")
        if citizen_id is None:
            explicit = _explicit_parent_key(key, parent_session)
            if explicit:
                # 父证据来自宿主 payload（WB hook 的 `transcript_path`）：不再现读会话头。
                # 父**没有**身份时就到此为止（下面的 provisional 支）——替父建公民是另一个
                # 进程的另一条 `ensure`（hook 先 ensure 父再 ensure 子），不在库里顺手造。
                ancestor_key, hit = explicit, find_session_citizen(conn, explicit)
            else:
                ancestor_key, hit = _nearest_identity_ancestor(conn, host=host, session_key=key)
            if hit is not None:
                citizen_id, origin = hit["citizen_id"], "inherited"
                inherited_from = ancestor_key
                display_name = hit["display_name"]
                account_id = hit.get("account_id") or account_id
                _write_session_identity(conn, key, citizen_id, origin="inherited",
                                        inherited_from=ancestor_key, evidence="inherited")
            else:
                # 没有可继承的祖先：**有父链但整链无身份**才是 provisional；
                # 根会话（判不到父）仍是 direct —— 那是今天就在走的机器事实路径。
                # `agent_id` 是第四个"有父"信号：宿主说这是子代理，只是真判不到父是谁
                # （payload 里没有可反解的 transcript_path）→ provisional，**不许**造 machine。
                has_parent = bool(explicit) or bool(agent_id) or bool(
                    fish_lineage.ancestor_sessions(conn, host, _session_id_of(key)))
                origin = "provisional" if has_parent else "direct"
                who = find_or_create_session_citizen(
                    conn, session_key=key,
                    evidence="provisional" if origin == "provisional" else evidence,
                    host=host, display_name=display_name, account_id=account_id,
                    kind=kind, role=role, identity_origin=origin, commit=False)
                citizen_id, created = who["citizen_id"], who["created"]
                display_name = who["display_name"]
        # ---- 凭证（唯一的"写"）：有活的就复用，没有才签 ----
        alive = _alive_session_token(conn, key)
        revoked = 0        # P1-6：重绑时吊销掉的旧活凭证数（事件与返回都要能看见）
        token_evidence, token_channel, parent_hash, parent_expires = evidence, channel, None, None
        if alive is not None and alive["citizen_id"] == citizen_id:
            issued = None
            token_source = "existing"
            issued_at, expires_at = alive["issued_at"], alive["expires_at"]
            token_evidence, token_channel = alive["evidence"], alive["channel"]
        else:
            # P1-6（2026-11-25）：**重绑不是重跑**——旧活凭证必须当场吊销，否则同一会话键有两条活凭证。
            revoked = revoke_session_tokens(conn, session_key=key) if alive is not None else 0
            if origin == "inherited":
                ancestor_key = ancestor_key or inherited_from
                token_evidence, token_channel, parent_hash, parent_expires = \
                    _inherited_token_shape(conn, ancestor_key, channel=channel)
            elif origin == "provisional":
                token_evidence = "provisional"
            issued = issue_session_token(
                conn, citizen_id=citizen_id, session_key=key, evidence=token_evidence,
                channel=token_channel, parent_hash=parent_hash, ttl_hours=ttl_hours)
            if parent_hash and parent_expires and issued["expires_at"] is not None \
                    and issued["expires_at"] > parent_expires:
                # 子凭证不能比父凭证活得久（与 issue_agent_token 同一条纪律）
                conn.execute("UPDATE session_tokens SET expires_at=? WHERE token_hash=?",
                             (parent_expires, issued["token_hash"]))
                issued["expires_at"] = parent_expires
            token_source = "issued"
            issued_at, expires_at = issued["issued_at"], issued["expires_at"]
        stints = [
            open_project_stint(conn, citizen_id, name, account_id=account_id,
                               set_by=f"ensure:{origin}")
            for name in project_list
        ]
        append_event(
            conn, "session.ensured",
            {"citizen_id": citizen_id, "evidence_level": token_evidence},
            {"kind": "citizen", "id": citizen_id},
            # 事件里**只有 hash**，没有明文（凭证纪律：不可变表会永久留下写进去的东西）
            {"session_key": key, "host": host, "identity_origin": origin,
             "inherited_from": inherited_from, "evidence": token_evidence,
             "channel": token_channel, "token_source": token_source,
             "token_hash": (issued or {}).get("token_hash"), "expires_at": expires_at,
             "revoked": revoked, "citizen_created": created, "projects": project_list,
             "stint_actions": [{"project": s["project"], "action": s["action"]}
                               for s in stints]}, commit=False)
        conn.commit()
    except Exception: conn.rollback(); raise
    same_name = find_same_name_citizens(conn, display_name, citizen_id) if created else []
    return {"token": (issued or {}).get("token"),
            "token_hash": (issued or {}).get("token_hash"),
            "token_source": token_source, "citizen_id": citizen_id, "session_key": key,
            "host": host, "evidence": token_evidence, "channel": token_channel,
            "display_name": display_name, "account_id": account_id,
            "created": created, "identity_origin": origin,
            "inherited_from": inherited_from, "rotated": revoked,
            "issued_at": issued_at, "expires_at": expires_at,
            "stint": stints[0] if stints else None, "stints": stints,
            "project": project_list[0] if len(project_list) == 1 else None,
            "projects": project_list, "warning": None,
            "archived": bool((found or {}).get("archived_at")),
            "name_conflicts": same_name}
def end_session(conn, *, token=None, environ=None, reason="session_end") -> dict:
    """`session end`：吊销凭证 + 关档期 + 刷新 last_seen_at。**幂等**。

    两种入口都必须支持：
      · `--token`       校验这条凭证，吊销它 + 它派生出的子凭证
      · 不带 `--token`  用 env 定位当前会话，把该会话所有活凭证一起吊销
    幂等的含义：重复调不报错、不产生新副作用（凭证已是吊销态就只补齐档期与活跃时间）。
    """
    children = 0
    try:
        if token:
            info = lookup_session_token(conn, token)
            if info is None:
                raise ValueError("token 不存在：明文只在签发时输出一次，"
                                 "丢了就重跑 `fish session start`（公民不变，凭证换新）")
            citizen_id, session_key = info["citizen_id"], info["session_key"]
            already = bool(info["revoked_at"])
            revoked = revoke_session_tokens(conn, token_hash=info["token_hash"])
            # 父会话结束，派生出来的子凭证不该继续有效——它已经失去来源。
            children = revoke_session_tokens(conn, parent_hash=info["token_hash"])
        else:
            ident = build_session_identity(environ=environ)
            found = find_session_citizen(conn, ident["session_key"])
            if found is None:
                raise ValueError(
                    f"当前会话没有登记过公民（session_key={ident['session_key']}）："
                    "先跑 `fish session start`")
            citizen_id, session_key = found["citizen_id"], ident["session_key"]
            revoked = revoke_session_tokens(conn, session_key=session_key)
            # 一条活的都没吊销 = 这个会话早就收过工了。用"实际吊销数"判定，而不是靠另一个标志位，
            # 这样"重复 end"在两条入口（--token / env）上的语义才是同一个。
            already = revoked == 0
        stints = close_open_stints(conn, citizen_id, reason=reason)
        touch_citizen(conn, citizen_id)
        append_event(
            conn, "session.closed",
            {"citizen_id": citizen_id, "evidence_level": "machine"},
            {"kind": "citizen", "id": citizen_id},
            {"session_key": session_key, "revoked": revoked, "children_revoked": children,
             "stints_closed": stints, "reason": reason, "already_revoked": already}, commit=False)
        conn.commit()
    except Exception: conn.rollback(); raise
    return {"citizen_id": citizen_id, "session_key": session_key,
            "revoked": revoked, "children_revoked": children,
            "stints_closed": stints, "already": already}
def session_status(conn, *, token=None, environ=None) -> dict:
    """`session status` 的事实层（CLI 只负责排版）。

    带 token：以**凭证**为准（它是这件事的真相），失效也照样报出原因。
    不带 token：用 env 定位（回答"我这个进程现在算谁"）。

    2026-10-24：档期从单值改**列表**（`stints`）。`stint` 保留为"最近开的那一条"，
    语义与旧版一致，所以只读 `stint` 的调用方不用改；要全部就取 `stints`。
    """
    if token:
        info = lookup_session_token(conn, token)
        if info is None:
            raise ValueError("token 不存在：明文只在签发时输出一次，"
                             "丢了就重跑 `fish session start`（公民不变，凭证换新）")
        citizen = load_citizen(conn, info["citizen_id"])
        stints = current_open_stints(conn, info["citizen_id"])
        return {"source": "token", "token": info, "citizen": citizen,
                "identity": None, "stints": stints,
                "stint": stints[-1] if stints else None}
    ident = build_session_identity(environ=environ)
    found = find_session_citizen(conn, ident["session_key"])
    citizen = load_citizen(conn, found["citizen_id"]) if found else None
    stints = current_open_stints(conn, citizen["citizen_id"]) if citizen else []
    # 2026-11-25（113 W4 · §5.2）：多一行 `identity_origin`（**旧键一个不动**）。
    # 它回答的是"这个会话是自建的、还是继承来的、还是临时身份"——只看 evidence 看不出来。
    return {"source": "env", "token": None, "citizen": citizen, "identity": ident,
            "identity_origin": (found or {}).get("identity_origin"),
            "inherited_from": (found or {}).get("inherited_from"),
            "stints": stints, "stint": stints[-1] if stints else None}
CITIZEN_ARCHIVE_REASON = "闲置归档"
def _has_open_stint(conn, citizen_id) -> bool:
    """还有开着的档期吗（`to_ts IS NULL` = 此刻正在某个项目上）。"""
    return bool(conn.execute(
        "SELECT 1 FROM project_stints WHERE citizen_id=? AND to_ts IS NULL LIMIT 1",
        (citizen_id,)).fetchone())
def _has_live_token(conn, citizen_id, at) -> bool:
    """还有没失效的凭证吗：未吊销 且（永久 或 到期时间在 `at` 之后）。"""
    return bool(conn.execute(
        "SELECT 1 FROM session_tokens WHERE citizen_id=? AND revoked_at IS NULL "
        "AND (expires_at IS NULL OR expires_at > ?) LIMIT 1",
        (citizen_id, at)).fetchone())
def archive_citizens(conn, *, days, dry_run=False, now=None,
                     reason=CITIZEN_ARCHIVE_REASON) -> dict:
    """把闲置超过 `days` 天的**非主人 / 无档期 / 无凭证**公民搬进 `citizens_archive`。

    归档 = INSERT 冷表 + DELETE 热表 + 一条 `citizen.archived` 事件（含 reason 与
    判定依据）。**搬而不是复制**：`citizens_all` 是 UNION ALL，同一 id 两边都有
    会被看见两条（那是搬迁出错的信号，不该被去重掩盖）。

    返回 `{days, now, cutoff, dry_run, checked, planned, archived, archived_count,
    skipped}`。`dry_run=True` 只算不动——归档是"搬行"，先看计划再执行是纪律
    （同正文归档 `fish archive` 默认演练）。

    `now` 接受 ISO 串或 `datetime`（判据要能钉住时钟："2 天前活跃"不该依赖测试
    跑多快）。`days<1` **响亮拒绝**：0 的语义是"不自动归档"，把它当阈值会归档掉
    所有非主人公民；跳过这句判断的责任在调用方（daily_report / CLI）。
    """
    days = int(days or 0)
    if days < 1:
        raise ValueError(
            f"days 必须 ≥1（收到 {days}）：0 的语义是「不自动归档」，"
            "调用方应先判 0 再决定跳过。")
    now_ts = _utc_str(now) if now is not None else now_utc()
    now_dt = parse_utc(now_ts)
    if now_dt is None:
        raise ValueError(
            f"now 必须是 ISO8601 UTC（如 2026-10-26T04:00:00Z），收到 {now_ts!r}")
    cutoff = (now_dt - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    idle_before = now_dt - datetime.timedelta(days=days)

    rows = conn.execute(
        "SELECT citizen_id, display_name, kind, last_seen_at FROM citizens "
        "WHERE archived_at IS NULL AND COALESCE(is_owner,0)=0 "
        "ORDER BY citizen_id").fetchall()
    planned, skipped, archived = [], [], []
    for cid, name, kind, last_seen in rows:
        last = (last_seen or "").strip()
        entry = {"citizen_id": cid, "display_name": name or "", "kind": kind or ""}
        if not last:
            skipped.append({**entry, "why": "没有 last_seen_at（无法判定闲置）"})
            continue
        seen_dt = parse_utc(last)
        if seen_dt is None:
            skipped.append({**entry, "why": f"last_seen_at 无法解析（{last}）"})
            continue
        if seen_dt >= idle_before:
            skipped.append({**entry, "why": f"仍活跃（最后活跃 {last} ≥ 截止 {cutoff}）"})
            continue
        if _has_open_stint(conn, cid):
            skipped.append({**entry, "why": "有未关档期（正在某个项目上干活）"})
            continue
        if _has_live_token(conn, cid, now_ts):
            skipped.append({**entry, "why": "还有未失效凭证（搬走会让它变成未归属）"})
            continue
        planned.append({**entry, "last_seen_at": last})

    if not dry_run:
        cols = ", ".join(_CITIZENS_ALL_COLUMNS)
        marks = ", ".join("?" for _ in _CITIZENS_ALL_COLUMNS)
        for it in planned:
            row = conn.execute(f"SELECT {cols} FROM citizens WHERE citizen_id=?",
                               (it["citizen_id"],)).fetchone()
            if row is None or row[_CITIZENS_ALL_COLUMNS.index("archived_at")]:
                continue                      # 被并发处理过：跳过，不当成失败
            info = dict(zip(_CITIZENS_ALL_COLUMNS, row))
            info["archived_at"] = now_ts
            conn.execute(f"INSERT INTO citizens_archive ({cols}) VALUES ({marks})",
                         tuple(info[c] for c in _CITIZENS_ALL_COLUMNS))
            conn.execute("DELETE FROM citizens WHERE citizen_id=?", (it["citizen_id"],))
            append_event(
                conn, "citizen.archived",
                # 这是**系统按配置判的**，不是某个会话的主张：证据级别写 machine，
                # 依据逐条进 payload，事后能回答"凭什么把它搬走"。
                {"citizen_id": it["citizen_id"], "evidence_level": "machine"},
                {"kind": "citizen", "id": it["citizen_id"]},
                {"reason": reason,
                 "evidence": {"days": days, "now": now_ts, "cutoff": cutoff,
                              "last_seen_at": it["last_seen_at"],
                              "is_owner": 0, "live_token": False, "open_stint": False},
                 "display_name": it["display_name"], "kind": it["kind"],
                 "archived_at": now_ts})
            archived.append(it["citizen_id"])
        conn.commit()

    return {"days": days, "now": now_ts, "cutoff": cutoff, "dry_run": bool(dry_run),
            "checked": len(rows), "planned": planned, "archived": archived,
            "archived_count": len(archived), "skipped": skipped}
def fix_owner(conn, code, citizen_id, reason=""):
    """修正一条记忆的归属（EVAL-261025-21：归属错了必须有修正通道）。

    ## 为什么是"追加事件 + 更新投影"，不是改事件

    `code_index.owner` 是投影；`events` 不可变（触发器锁死）。修正的正确形态：
    追加一条 `memory.owner_fixed`（记录 old/new/reason），再更新投影。
    这样"谁在何时把归属从 A 改到 B"永远可追溯。

    ## 边界

    · 只改 `code_index.owner`；**不改任何其他字段**，也不动正文文件。
    · 编码与公民都必须存在，且公民必须活跃——不给"猜一个"的余地。
    """
    row = conn.execute("SELECT owner FROM code_index WHERE code=?", (code,)).fetchone()
    if not row:
        raise ValueError(f"编码不存在：{code}")
    old = row[0]
    c = conn.execute(
        "SELECT display_name, status, archived_at FROM citizens_all WHERE citizen_id=?",
        (citizen_id,)).fetchone()
    if not c:
        raise ValueError(f"公民不存在：{citizen_id}")
    if (c[1] or "").lower() != "active" or c[2]:
        raise ValueError(
            f"公民不是活跃态：{citizen_id}（status={c[1]} archived={bool(c[2])}）")
    try:
        conn.execute("UPDATE code_index SET owner=? WHERE code=?", (citizen_id, code))
        append_event(conn, "memory.owner_fixed",
                     {"citizen_id": "system", "evidence_level": "signed"},
                     {"kind": "memory", "id": code},
                     {"code": code, "old_owner": old, "new_owner": citizen_id,
                      "reason": reason or ""}, commit=False)
        conn.commit()
    except Exception: conn.rollback(); raise
    return {"code": code, "old_owner": old, "new_owner": citizen_id,
            "display_name": c[0]}

# ------------------------------------------------------------------ 导出面
_IMPORTED = frozenset({"datetime", "hashlib", "math", "os", "re", "secrets", "sqlite3", "Path", "fish_activity", "fish_lineage", "fish_naming", "fish_surface", "_utc_str", "citizen_ids_known", "get_config", "now_utc", "parse_utc", "resolve_owner", "ulid", "warn_ignored", "_CITIZENS_ALL_COLUMNS", "append_event", "_rc_lines_raw", "identity_anchor_grants", "_IMPORTED"})
__all__ = sorted(n for n, v in globals().items() if not n.startswith("__") and n not in _IMPORTED and callable(v) and not isinstance(v, type))
