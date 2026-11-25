"""注入包的 lesson / rule 两段取数（F-P1-10 God module 拆分的第二刀）。

## 为什么单独成模块

`fish.py` 顶在 12,700 行的 J4 窗口上，最后只剩 1 行余量——**它开始挡路了**：
修一处真实泄露（`find_similar_memories`）需要 6 行，当时挤不出来，只好整条回退。

这 6 个函数是一个**完整单元**：注入包里"错题本"与"规矩"两段的取数，
加上只服务这两段的两个计数器与**作者信任门**（P0-1：谁能进这两段）。

## 依赖方向（J2）

只依赖 `fish_common`（配置、`UNATTRIBUTED`）与 `fish_visibility`（可见性谓词）。
**不 import `fish`**——向上依赖会让下一个拆分变成循环。

`RULES_PER_SOURCE_LIMIT` 在这里**重新派生一次**，而不是从 `fish.py` 取：
两者都从同一个 `CONFIG_DEFAULTS["inject.rules_per_source"][0]` 算出来，
单一出处在配置表，符合原来那句"默认值只在配置表里写一次"。
"""

import sqlite3   # 搬过来的代码 catch sqlite3.OperationalError；漏 import 就是 NameError

from fish_common import CONFIG_DEFAULTS, UNATTRIBUTED, get_config
from fish_visibility import _visibility_enforced, _visibility_sql

RULES_PER_SOURCE_LIMIT = int(CONFIG_DEFAULTS["inject.rules_per_source"][0])


# ── 作者信任门（P0-1，外部审查 2026-11-25）──────────────────────────────
#
# **问题**：`_rule_rows` / `count_rule_rows` / `_lesson_rows` / `count_lesson_rows`
# 原来只按 mem_type / status / project 过滤，**不看作者**。而 MCP 的 `session_start`
# 对任何调用方无门槛签发 visitor 公民 + token（自助身份），`lesson` 又免凭证可写：
#   → 任何本地 MCP 客户端写一条 lesson /（历史行）rule，它进的却是**所有会话**的
#     注入包。这不是"内容不好看"的展示问题，是提示注入 / 行为约束劫持。
# 生产库实测（2026-11-25）：rule 97 条里 11 条 visitor、14 条 owner='unknown'；
# lesson 159 条里 68 条 visitor。它们**不需要新写**就已经在包里。
#
# **判定**（唯一一条 SQL，四个取数函数共用——`build_short_rules_block` 的两栏正是
# 走这两个函数，所以不另写一份判定）：
#   可信   = owner 非空、不是 unattributed/unknown、且不是 kind='visitor' 的公民
#   不可信 = owner 为空 / unattributed / unknown / visitor
# `resident` / `role` 是**被授予**的（同口径见 `fish_trust.AUTHORIZED_CITIZEN_KINDS`），
# visitor 是自助登记的——所以"有主"不等于"可信"。
#
# **放行开关** `inject.allow_untrusted`（默认 0；旧键 `inject.allow_untrusted_lessons`
# 保留为别名）：只放行**无主**（NULL / unattributed / unknown），**不放行 visitor**
# ——visitor 是自助身份，能用运维开关把它重新放进来就等于门没关。
UNTRUSTED_OWNERS = (UNATTRIBUTED, "unknown")
UNTRUSTED_KINDS = ("visitor",)


def _allow_untrusted(conn) -> bool:
    """未归属内容是否允许进注入包（`inject.allow_untrusted`，默认**不允许**）。

    为什么默认关：`lesson` 按 2026-11-11 的决策**免凭证可写**（"记忆比归属重要"），
    而它进的是注入包的错题本段——等于能影响所有会话里 AI 的行为约束。
    两者放一起就是"陌生人能改教训"：写入成本为零，影响面是全库。

    默认关不等于"lesson 一定要有主"：无凭证照写不误，只是**不自动进包**，
    事后可用 `fish owner-fix` 补归属，那时它才进包。

    旧键 `inject.allow_untrusted_lessons`（F-P0-3 那一版）是同一件事的历史名：
    保留为别名，避免已设该键的库在升级后**静默变成关闭**。两个键都没有时按
    "不允许"处理（读不到配置 = 不放行，宁可少列不可投毒）。
    """
    for key in ("inject.allow_untrusted", "inject.allow_untrusted_lessons"):
        try:
            if int(get_config(conn, key) or 0):
                return True
        except Exception:      # A档：配置读不到按"不允许"处理
            continue
    return False


#: 旧名（`fish.py` 仍从这里再导出）——同一函数，不是第二份实现。
_allow_untrusted_lessons = _allow_untrusted


def _owner_trust_sql(conn):
    """作者可信度的**唯一 SQL 谓词**（P0-1）→ `(sql, params)`，直接拼进 WHERE。

    返回带 `" AND (...)"` 前缀而不是裸条件：四个调用点的形状都是"已经有一段 WHERE
    条件"，带前缀才能只做 `sql += frag` / `params += ps` 两件事——判定只有一处，
    它们不会各自变形成另一套口径（"两份实现必然漂"是本项目反复记录的翻车点）。

    为什么是**黑名单**（列不可信）而不是白名单（列可信）：`owner` 是历史列，存量里
    有已归档/已合并的公民 id 与各种手工值；白名单会把"查不到 kind 的旧行"整体删掉
    （那是又一次"用猜替代判"）。黑名单只排除**能证明是自助身份**的那一类，
    再把"无主"三种字面值一起排除，剩下的仍按 owner 原值显示。
    """
    strict_sql = ("owner IS NOT NULL AND owner NOT IN (?,?) "
                  "AND owner NOT IN (SELECT citizen_id FROM citizens WHERE kind=?)")
    strict_params = list(UNTRUSTED_OWNERS) + list(UNTRUSTED_KINDS)
    if not _allow_untrusted(conn):
        return " AND (" + strict_sql + ")", strict_params
    # 开关只放行"无主"：visitor **有主也不放行**（自助身份不能靠运维开关翻白）。
    return (" AND (owner IS NULL OR owner IN (?,?) OR (" + strict_sql + "))",
            list(UNTRUSTED_OWNERS) + strict_params)


def _rule_rows(conn, *, project=None, limit=RULES_PER_SOURCE_LIMIT, exclude=(),
                viewer_citizen_id=None):
    """取某一栏规矩的 `(code, title)`：`mem_type='rule'`、active、未休眠，按时间降序。

    分栏口径见本节开头：**按适用范围，不按作者**（`owner` 是作者，不是适用范围）。
    注意两件事要分开：**分栏**不看作者，但**能不能进包**看作者可信度（P0-1 信任门，
    见 `_owner_trust_sql`）——visitor / 无主的规矩两栏都不进。
      · `project` 给定 → `project = ?`（本项目规矩）；
      · `project` 为 None → `project IS NULL`（全局规矩——不绑定任何项目才是全局）。
    空字符串/纯空白按"没有项目"处理、归到全局栏：那是"未绑定项目"，
    不是"项目名叫空"（`record_memory` 不给 project 时不会产出空串，但老数据/手工 SQL 可能）。
    **只取标题**——正文按需 `resolve_code` 读（见本节开头的取舍）。
    `exclude` 里的编码不列（现行两栏谓词互斥，理论上撞不上；这条是防御性去重）。
    多取 `len(exclude)` 条再在 Python 里剔，是为了剔除后仍凑满 `limit` 条。
    """
    key = (project or "").strip() or None
    if key is None:
        clause, where_params = "project IS NULL", []
    else:
        clause, where_params = "project = ?", [key]
    # 第四轮评审 🔴-2：rule 进注入包「规矩」段，同上——必须过滤。
    _rv, _rp = _visibility_sql(conn, viewer_citizen_id)
    if _rv:
        clause = clause + _rv
        where_params = list(where_params) + _rp
    # P0-1（外部审查 2026-11-25）：**作者信任门**。visitor / 无主作者的规矩
    # 不许进「规矩」段——那一段进的是系统提示，等于能改 AI 的行为约束。
    _tv, _tp = _owner_trust_sql(conn)
    clause += _tv
    where_params = list(where_params) + _tp
    sql = ("SELECT code,title FROM code_index WHERE mem_type='rule' "
           "AND status='active' AND dormant=0 AND " + clause +
           " ORDER BY recorded_at DESC, code DESC LIMIT ?")
    params = where_params + [int(limit) + len(set(exclude))]
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        # 老库缺列/缺表：注入包不能因此崩（本段只是锦上添花，不是身份行）。
        return []
    out = []
    for code, title in rows:
        if code in exclude:
            continue
        out.append((code, (title or "").strip()))
        if len(out) >= int(limit):
            break
    return out


def count_rule_rows(conn, *, project=None, viewer_citizen_id=None) -> int:
    """某一栏**总共有多少条**规矩（不受 limit 限制）——段头「显示 N/M 条」的 M。

    为什么要单独一条 count：`_rule_rows` 已经按 limit 截过，从返回值里数不出
    "本来有 7 条、只列了 3 条"。而**没列出来的那 4 条必须被说出来**——否则
    AI 会以为"这个项目的规矩就这 3 条"，那比少列更糟（少列是省预算，不说才是骗）。
    口径与 `_rule_rows` 完全一致（rule / active / 未休眠 / 同一栏谓词）；
    老库缺列时返回 0（注入包不能因为这段崩）。
    """
    key = (project or "").strip() or None
    if key is None:
        clause, params = "project IS NULL", []
    else:
        clause, params = "project = ?", [key]
    # 第五轮评审 🔴：段头 N/M 的 M 也必须过滤，否则它把私有的算进总数、
    # 再说"因预算省略"——那两句都是假的。
    _crsql, _crparams = _visibility_sql(conn, viewer_citizen_id)
    # P0-1：M 与列表**同口径**（`_rule_rows` 的同一谓词）——否则段头说 3 条、
    # 只列 1 条，读者会以为"其余因预算省略"，而真相是它们不可信。
    _trsql, _trparams = _owner_trust_sql(conn)
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM code_index WHERE mem_type='rule' "
            "AND status='active' AND dormant=0 AND " + clause + _crsql + _trsql,
            list(params) + _crparams + _trparams).fetchone()[0])
    except sqlite3.Error:
        return 0


def count_lesson_rows(conn, since_z, exclude=(), viewer_citizen_id=None):
    """窗口内 lesson 的**总条数**（不受 `per_type` 限制）——段⑤ 段头 N/M 的 M。

    谓词与 `_lesson_rows(same_project=None)` 一致（lesson / active / 未休眠 / 窗口内），
    `exclude` 是"已在别段印过的编码"——被别段占用的条目不算本段的可用量，
    否则段头会把它们算进 M，读者一对照就发现"说 3 条只列 1 条"。

    读失败返回 **None**（与 `count_open_loop_rows` 同一纪律，2026-10-26 复审 ⚪N8）：
    "读不出来"和"一条都没有"在段头文本上必须是两句话，否则「显示 2/2 条」会让
    读者相信错题本就这两条。
    """
    sql = ("SELECT COUNT(*) FROM code_index WHERE mem_type='lesson' AND status='active' "
           "AND dormant=0 AND recorded_at >= ?")
    params = [since_z]
    # F-P0-3（2026-11-18 外部审查）：未归属 lesson **不计入**注入包。
    # 它们免凭证可写，而错题本段会影响所有会话的 AI 行为——两者放一起就是
    # "陌生人能改教训"。P0-1（2026-11-25）把口径扩到**作者可信度**（visitor 不算），
    # 判定仍在 `_owner_trust_sql` 一处；放行开关见 inject.allow_untrusted。
    _tl_sql, _tl_params = _owner_trust_sql(conn)
    sql += _tl_sql
    params += _tl_params
    extra = [c for c in (exclude or ()) if c]
    if extra:
        sql += " AND code NOT IN (" + ",".join("?" * len(extra)) + ")"
        params += extra
    # 第五轮评审 🔴：M 也要过滤（同 count_rule_rows）。
    _clsql, _clparams = _visibility_sql(conn, viewer_citizen_id)
    sql += _clsql
    params += _clparams
    try:
        return int(conn.execute(sql, params).fetchone()[0])
    except sqlite3.Error:
        return None


def _lesson_rows(conn, since_z, limit, exclude=(), same_project=None,
                 viewer_citizen_id=None):
    """窗口内 lesson 的 `(code, title)`：时间降序；`same_project` 给定时只取该项目。

    `same_project=None` = 不限项目（用于跨项目补齐阶段，也用于"当前项目未声明"）。
    """
    where, params = "recorded_at >= ?", [since_z]
    # 第四轮评审 🔴-2：lesson 进注入包「错题本」段，**每个会话开场打印**。
    _lv, _lp = _visibility_sql(conn, viewer_citizen_id)
    if _lv:
        where += _lv
        params.extend(_lp)
    # F-P0-3 + P0-1：与 count_lesson_rows 同口径（同一谓词），否则段头说 2 条、列表只 1 条。
    _tl_sql, _tl_params = _owner_trust_sql(conn)
    where += _tl_sql
    params.extend(_tl_params)
    if same_project is not None:
        where += " AND project = ?"
        params.append(same_project)
    sql = ("SELECT code,title FROM code_index WHERE mem_type='lesson' "
           "AND status='active' AND dormant=0 AND " + where +
           " ORDER BY recorded_at DESC, code DESC LIMIT ?")
    params.append(int(limit) + len(set(exclude)))
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for code, title in rows:
        if code in exclude:
            continue
        out.append((code, (title or "").strip()))
        if len(out) >= int(limit):
            break
    return out


def _recent_lesson_rows(conn, since_z, limit, exclude=(), project=None,
                        viewer_citizen_id=None):
    """B 类「最近的坑」：窗口内的 lesson，**同项目优先**，不足再用跨项目补齐。

    排序键 = (是否同项目 DESC, recorded_at DESC)（2026-10-25 改）。
    为什么不是"纯时间取最近 2 条"：生产库实测近 7 天有 39 条 lesson；纯按时间很可能
    取到**别的项目**的坑——给 A 项目工作的 AI，最该先知道 A 自己的坑。
    为什么还要跨项目：通用教训（如"判断谁在写"的证据顺序）跨项目有效，不该被排除，
    只是**排序靠后**——同项目满额时不占位，不足额时用它补满（不空着）。
    当前项目未声明（project 为空）时没有"同项目"可比，退化为纯时间序。

    实现用**两段查询**而不是一条 CASE 排序：两段各自 LIMIT，语义直白，
    也让判据能分别钉住"同项目优先"（B′）与"跨项目补齐"（B2）两件事。
    """
    limit = int(limit)
    exclude = set(exclude)
    key = (project or "").strip() or None
    out = _lesson_rows(conn, since_z, limit, exclude, same_project=key,
                             viewer_citizen_id=viewer_citizen_id) if key else []
    if len(out) < limit:
        taken = exclude | {code for code, _ in out}
        out += _lesson_rows(conn, since_z, limit - len(out), taken, same_project=None,
                             viewer_citizen_id=viewer_citizen_id)
    return out
