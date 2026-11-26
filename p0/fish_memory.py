#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆域（`fish.py` 拆包 · W3-b · 144 刀二）：记忆写链（record/supersede/archive/access-log）
+ 读链（search/list/count/recent/distill/neighbors）+ 关系链（link/alias/similar）。

只 import 叶子模块（`fish_common` / `fish_db` / `fish_visibility` / `fish_attach` /
`fish_atomic` / `fish_profile`），**禁止 import fish**（J2）。43 个函数 + 18 个常量逐字搬自
`fish.py`，函数体零改动（AST 等价复核）。

`_recorded_body_sha` 是 144 取证（HEAD 7dc278b）之后 P2-2 新增的 `read_body_path` 专用
helper，在设计图 43 名之外；随域迁入但**不进 `__all__`**，`fish._recorded_body_sha` 仍从
fish.py 再导出。

`__all__` = 43 个设计图函数名（含 `_` 私有）；常量由 fish.py 另行 import 再导出。
"""
import datetime
import json
import re
import shutil
import sqlite3
from pathlib import Path

import fish_atomic
import fish_profile
from fish_attach import store_blob, validate_attachment
from fish_common import (_ALIAS_KINDS, _alias_map, _walk_alias, _index_one_fts,
                         body_relpath, fts_match_expr, index_tokens, local_date,
                         now_utc, parse_utc, resolve_name, resolve_project_names,
                         warn_derived, warn_ignored)
from fish_db import append_event
from fish_visibility import (_filter_by_visibility, _visibility_enforced,
                             _visibility_sql)

def normalize_occurred_at(value):
    """`occurred_at` 校验：缺省 = 现在；给了就必须是可解析的 ISO8601 UTC。

    为什么要校验：它是"这件事什么时候发生"的事实（02 §2.3 双时态的起点）。
    收下一个解析不了的字符串，等于让时间线悄悄断掉。
    """
    if value in (None, ""):
        return now_utc()
    if parse_utc(value) is None:
        raise ValueError(
            f"occurred_at 必须是 ISO8601 UTC 格式（如 2026-10-22T04:00:00Z），收到 {value!r}")
    return value

# ---------------------------------------------------------------- 正文文件名（2026-10-25）
# 正文文件名从「纯编码」改成「编码 + 标题缩写」：
#
#     projects/anyi-studio/anyi-studio-261022-05__协作铁律·行为层（CR-2-5-6-7-11_+.md
#
# 为什么（用户 2026-10-25 需求）：
#   AI 不看目录（实测 recall 78.6% / read_body 5.6%），但**人**会用文件管理器、
#   Everything 之类的工具按文件名搜。纯编码文件名对这个场景等于零信息。
#
# 三条纪律：
#   ① **纯函数**：同样的 (project, code, title) 必须永远算出同一个路径——
#      否则 `rebuild` 会算出和 `record_memory` 不同的文件名，重建即断链。
#   ② **标题是记录时的快照**：`memory.revised` 改标题后**文件名不变**
#      （用户 2026-10-25 拍板）。跟着变会让所有外部引用（人写的文档、别的 AI 的笔记）失效。
#   ③ **标题截 24 字符**（用户拍板）。实测标题平均 38.7 / 最长 81，
#      不截会让文件名到 100+ 字符，反而难用。
# Windows 文件名非法字符 → 安全替换。**只换非法字符**，全角标点（（）·：）是中文字符，保留。
def archive_relpath(file_path, year) -> str:
    """把 `projects/<项目>/<文件名>` → `projects/<项目>/archive/<年>/<文件名>`。

    ## 为什么**只换目录、不改文件名**

    文件名里的 slug 是**记录那一刻**由 title 算出来的。之后 `memory.revised` 可能改过
    `code_index.title`，此时重算 slug 会得到不同的名字——**那正是当年 rebuild 断链
    73 条的根因**（`rebuild_projection` 用事件里的旧 title 重算路径）。
    归档只该搬位置，不该重命名：移动是可逆的，改名不是。

    ## 为什么按年分目录

    用户的原话是「在很久很久以后」。平铺的问题是**给人看的**——AI 查询全走
    `code_index` 索引与 `memory_fts`，全仓零目录遍历（`os.walk`/`listdir`/`glob`
    命中数 = 0），所以文件放在哪一层对查询速度毫无影响。
    按年分是为了让 `archive/2026/` 这类目录的规模可控，而不是为了 AI。
    """
    p = Path(str(file_path).replace("\\", "/"))
    parts = p.parts
    if len(parts) == 3 and parts[0] == "projects":
        return f"projects/{parts[1]}/archive/{year}/{parts[2]}"
    raise ValueError(f"只支持 projects/<项目>/<文件> 形式，收到：{file_path!r}")


def archive_body(conn, root, code, *, year=None, reason=None, dry_run=False) -> dict:
    """把一条记忆的正文移进 `archive/<年>/`，并同步 `code_index.file_path`。

    ## 它不改什么（这是"不影响机制"的全部原因）

    - **不改 `status`**：归档是**位置**，不是**语义**。`superseded` 早已是语义，
      归档只是把文件挪走
    - **不改正文内容**：一个字节都不动
    - **不改事件**：只追加一条 `memory.archived`
    - **不删任何东西**

    ## 为什么所有工具都不用改

    全仓零目录遍历——`read_body` / `recall` / `neighbors` 全部走
    `code_index.file_path` 这个字段。**字段指到哪，就读到哪。**
    而 `rebuild_projection` 已有「快照优先」逻辑（2026-10-25 修）：
    只要 `file_path` 指向的文件存在，重建就保留它，不会算回平铺位置。
    """
    row = conn.execute(
        "SELECT code, project, title, file_path, status FROM code_index WHERE code=?",
        (code,)).fetchone()
    if not row:
        raise ValueError(f"编码不存在：{code}")
    code, project, title, old_fp, status = row
    if not old_fp:
        raise ValueError(f"{code} 没有 file_path，无法归档")
    old_abs = Path(root) / old_fp
    if not old_abs.exists():
        raise ValueError(f"{code} 的正文文件不存在：{old_fp}（先跑 rebuild 修复投影）")

    # **幂等**：已经在 archive/ 下的直接返回"已归档"，不报错。
    # 2026-10-25 副本实测抓到：先单条归档、再跑全量 sweep 时，这条会撞上
    # `archive_relpath` 的格式检查并报"只支持 projects/<项目>/<文件> 形式"——
    # 那是**把幂等当成错误**。归档本身就该可重复调用。
    if "/archive/" in old_fp.replace("\\", "/"):
        return {"code": code, "from": old_fp, "to": old_fp,
                "moved": False, "already": True}

    # 年份**从编码里取**：`<前缀>-<YYMMDD>-<NN>` → `20YY`。
    # 为什么不是从 file_path 取：路径里只有 `<编码>__<slug>.md`，前 4 位是编码前缀
    # （比如 `anyi`），不是年份——2026-10-25 dry-run 当场抓到这个 bug（算成了 `2610`）。
    if year:
        y = str(year)
    else:
        m = re.search(r"-(\d{2})\d{4}-\d+$", code)
        y = ("20" + m.group(1)) if m else local_date()[:4]
    new_fp = archive_relpath(old_fp, y)
    new_abs = Path(root) / new_fp
    if dry_run:
        return {"code": code, "from": old_fp, "to": new_fp, "moved": False}

    new_abs.parent.mkdir(parents=True, exist_ok=True)
    # 用 `os.replace` 之外的保守做法：先确认目标不存在，再 move
    # （Windows 上 os.rename 会静默覆盖同名文件——`exact_exists` 的教训）
    if new_abs.exists():
        raise ValueError(f"目标已存在，拒绝覆盖：{new_fp}")
    shutil.move(str(old_abs), str(new_abs))
    conn.execute("UPDATE code_index SET file_path=? WHERE code=?", (new_fp, code))
    append_event(conn, "memory.archived",
                 {"citizen_id": "system", "evidence_level": "signed"},
                 {"kind": "memory", "id": code},
                 {"code": code, "from": old_fp, "to": new_fp,
                  "reason": (reason or "").strip() or "superseded",
                  "status": status})
    return {"code": code, "from": old_fp, "to": new_fp, "moved": True}


def archive_sweep(conn, root, *, include_withdrawn=True, year=None,
                  dry_run=False, limit=None) -> dict:
    """批量归档：**只动 `superseded`（和可选 `withdrawn`）**，`active` 一律不碰。

    ## 为什么只动这两种状态（这是"不影响推送"的关键）

    全仓所有取数口都硬过滤 `status='active'`（`fish.py:1768 / 2542` 等多处）。
    所以只要不动 `active`，**推送、召回、注入包、邻近查询的行为一个字节都不会变**——
    它们本来就看不见 `superseded`/`withdrawn` 的那些。

    用户的原话是「不影响我们目前的记忆、推送等机制」——这条红线就是那样守住的：
    归档的**准入判据直接复用系统已有的 `status`**，不新造任何"归档状态"。
    """
    want = ["superseded"] + (["withdrawn"] if include_withdrawn else [])
    rows = conn.execute(
        "SELECT code FROM code_index WHERE status IN (%s) AND file_path IS NOT NULL "
        "ORDER BY recorded_at" % ",".join("?" * len(want)), want).fetchall()
    done, skipped, errors = [], [], []
    already = 0
    for (code,) in (rows[:limit] if limit else rows):
        try:
            r = archive_body(conn, root, code, year=year, dry_run=dry_run)
            # `already` 与 `moved` 分开计数：重复 sweep 时报"移动 38 条"是假读数
            # （文件一个都没动）——2026-10-25 幂等验证当场抓到。
            if r.get("already"):
                already += 1
            else:
                done.append(r)
        except ValueError as e:
            # 文件缺失 / 目标已存在 → 跳过并**如实记下**，不静默吞
            skipped.append({"code": code, "why": str(e)})
        except OSError as e:
            errors.append({"code": code, "why": f"{type(e).__name__}: {e}"})
    conn.commit()
    return {"total": len(rows), "moved": len(done), "already": already,
            "skipped": skipped, "errors": errors, "items": done}


def locate_body(root, code, file_path):
    """定位一条记忆的正文文件——**先按记录路径，再按编码兜底搜索**。

    ## 为什么需要它（2026-10-25 · 用户明确要求）

    用户的原话：

    > 人类会非常自由的修改项目文件夹内甚至包括项目文件夹本身，包括但不限于
    > 随意删除任意文件、调整里面的任意内容、修改项目文件名、修改项目文件夹名、
    > 改变文件顺序、改变文件位置……我们不接受系统因此而崩溃或无法运行。

    ## 三档返回

      `("exact", Path)`      —— 按 `code_index.file_path` 找到了（正常情况）
      `("relocated", Path)`  —— 原文没了，但**按编码在 projects/ 下找到了**
                               （人类改了文件名 / 改了项目文件夹名 / 挪了位置）
      `("missing", None)`    —— **真的找不到**（被删了，或挪到 projects/ 之外）

    ## 为什么按编码搜是有效的

    文件名格式是 `<编码>__<标题slug>.md`，**编码在文件名里**。
    所以只要人类没有连文件名里的编码一起改掉，改名/改目录/挪位置都能救回来。
    这也是本系统编码设计的意外红利：**编码是内容的稳定锚**。

    ## 为什么只搜 `projects/`

    人类的自由度是"项目文件夹内**甚至包括项目文件夹本身**"，不是在整块盘上漫游。
    限定 `projects/` 做**有界搜索**，避免在几万文件的工作区里 rglob 卡住。
    如果连 `projects/` 都整个挪走了——那已经不是"自由修改"，是搬家，
    此时返回 `missing` 并给出明确提示（**不崩溃**，见 `read_body_path`）。
    """
    p = (Path(root) / str(file_path)) if file_path else None
    if p is not None and p.exists() and p.is_file():
        return "exact", p

    base = Path(root) / "projects"
    if not base.is_dir():
        return "missing", None
    try:
        # ① `<编码>__<slug>.md`（标准形态）
        hits = sorted(base.rglob(f"{code}__*.md"))
        if not hits:
            # ② 人类可能把 `__标题` 那截删了，只剩 `<编码>.md`
            hits = sorted(base.rglob(f"{code}.md"))
        if not hits:
            # ③ 最宽松：文件名里含这个编码（人类加了前后缀，如 `-副本`、`（备份）`）
            hits = sorted(base.rglob(f"*{code}*.md"))
    except OSError:
        return "missing", None
    if hits:
        return "relocated", hits[0]
    return "missing", None


def _recorded_body_sha(conn, code):
    """P2-2：该编码最近一条 recorded/imported 事件的 body_sha256（含改名折算）；None = 无事件/旧数据。"""
    try:
        ren = load_rename_map(conn)
        cands = {code} | {old for old, new in ren.items() if new == code}
        row = conn.execute(
            "SELECT payload FROM events WHERE type IN ('memory.recorded','memory.imported') "
            "AND json_extract(payload,'$.code') IN (%s) ORDER BY seq DESC LIMIT 1"
            % ",".join("?" * len(cands)), tuple(cands)).fetchone()
        return (json.loads(row[0]).get("body_sha256") or None) if row else None
    except Exception:      # noqa: BLE001 —— 读路径不抛；判不到按旧数据（unverified）
        return None


def read_body_path(conn, root, code, *, viewer_citizen_id=None, verify=True):
    """读正文的**唯一入口**（CLI 与 MCP 共用）——永远返回 `(text, info)`，**不抛异常**。

    `info` 是给人看的状态：`{"state": "exact"|"relocated"|"missing"|"corrupt"|"unverified",
    "path": 实际读到的路径或 None, "healed": 是否顺手修回了 file_path}`。
    `verify=False` 关闭正文哈希校验（默认校验）。

    ## 核心纪律：**文件不在 ≠ 系统崩**

    用户要求"不接受系统因此崩溃或无法运行"。所以这里的原则是：

      · `exact`     → 正常返回正文
      · `relocated` → **顺手自愈**：把 `code_index.file_path` 改成新位置，
                     然后返回正文 + 一句"文件被移动过，已自动修回"。
                     **人类改了名，系统自己找回来**——这才是"不崩溃"的正确含义
      · `missing`   → 返回一段**说明文字**（不是异常），讲清三种可能：
                     被删了 / 挪出 projects 了 / 文件名里的编码也被改了，
                     并给出修复命令

    为什么把"缺文件"做成**返回值而不是异常**：
    异常会让调用方（MCP 工具、CLI、批量脚本）整条链断掉。而"某一条正文不见了"
    是**局部数据问题**，不该升级成"整个系统不可用"。
    """
    # 第四轮评审 🔴-2：正文按可见性放行。原来拿到编号就能读全文，而
    # `fish://index` 曾无身份列出全部编号——两个一拼就能读任何人的私有正文。
    # 不抛异常（本函数契约是"永远返回 (text, info)"）：拒绝返回 state="forbidden"。
    row = conn.execute(
        "SELECT code, file_path, project, title, status, visibility, owner "
        "FROM code_index WHERE code=?",
        (code,)).fetchone()
    if not row:
        raise ValueError(f"编码不存在：{code}")
    _vis_deny = False
    if row[5] and str(row[5]).strip().lower() == "private" and _visibility_enforced(conn):
        if str(row[6] or "") != str(viewer_citizen_id or ""):
            _vis_deny = True
    if _vis_deny:
        return "", {"state": "forbidden", "path": None, "healed": False,
                    "hint": (f"属于别人的私有记忆：{row[0]}\n"
                             "　可见性开关开着（privacy.visibility_enforced），"
                             "而本次调用没有匹配的身份。")}
    row = row[:5]
    code, file_path, project, title, status = row
    # F-P2-7：`file_path` 是库里的值，被改过就能指到任意位置。判**解析后的
    # 真实路径**（resolve 展开 `..` 与链接），不是字符串前缀——后者一绕就破。
    _ext = None
    if file_path:
        try:
            Path(root, file_path).resolve().relative_to(Path(root).resolve())
        except (ValueError, OSError):
            _ext = Path(root, file_path).resolve()
    if _ext is not None:
        return "", {"state": "forbidden", "path": None, "healed": False,
                    "hint": (f"记录的位置在库外，拒绝读取：{code}（{file_path}）\n"
                             "　code_index 可能被外部改过；用 `fish rebuild` 校正。")}
    state, p = locate_body(root, code, file_path)

    if state == "missing":
        return "", {"state": "missing", "path": None, "healed": False,
                    "hint": (
                        f"正文缺失：{code}\n"
                        f"　记录位置：{file_path or '（无）'}\n"
                        "　三种可能：① 文件被删了 ② 被挪到 projects/ 之外 ③ "
                        "文件名里的编码也被改掉了\n"
                        "　仓库里还有：标题 / 类型 / 标签 / 事件（`fish events --code "
                        f"{code}`），**只有正文内容不在库内**\n"
                        "　如果只是挪了位置又挪回来，本命令会自动认出来")}

    healed = False
    rel = str(Path(p).relative_to(Path(root))).replace("\\", "/")
    if state == "relocated" and rel != (file_path or ""):
        # **自愈**：人类改了名/挪了位置，系统自己修回记录
        conn.execute("UPDATE code_index SET file_path=? WHERE code=?", (rel, code))
        append_event(conn, "memory.relocated",
                     {"citizen_id": "system", "evidence_level": "signed"},
                     {"kind": "memory", "id": code},
                     {"code": code, "from": file_path, "to": rel})
        healed = True

    try:
        text = p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        # 文件在但读不出来（编码被改坏 / 权限）—— 同样不崩
        return "", {"state": state, "path": rel, "healed": healed,
                    "hint": (f"正文存在但读不出来：{rel}\n"
                             f"　原因：{type(e).__name__}: {e}\n"
                             "　人类可能改过编码或权限——文件是权威，系统不覆盖它")}
    info = {"state": state, "path": rel, "healed": healed, "location": state}
    if not verify:
        info["verify"] = "skipped"
        return text, info
    sha = _recorded_body_sha(conn, code)
    if not sha:
        info.update(state="unverified", verify="absent")
        return text, info
    if sha != fish_atomic.sha256_of_file(p):
        info.update(state="corrupt", verify="mismatch")
        info["hint"] = f"正文 body_sha256 与事件记录不一致：{code}（文件可能被外部改坏）"
        text = "<!-- ⚠ 正文校验失败（body_sha256 不一致），以下原文仍照常返回 -->\n\n" + text
        return text, info
    return text, {**info, "verify": "ok"}


def next_code(conn: sqlite3.Connection, prefix: str) -> str:
    """P0 已验证：BEGIN IMMEDIATE + 撞号重试，50 并发零重号。"""
    today = local_date()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT last_date, last_seq, status FROM code_registry WHERE prefix=?", (prefix,)
        ).fetchone()
        if not row:
            raise ValueError(f"未知项目前缀：{prefix}（先运行 project-add）")
        last_date, last_seq, status = row
        if status != "active":
            raise ValueError(f"项目 {prefix} 状态为 {status}，不能分配新编码")
        seq = 1 if last_date != today else int(last_seq or 0) + 1
        while True:
            code = f"{prefix}-{today}-{seq:02d}"
            if not conn.execute("SELECT 1 FROM code_index WHERE code=?", (code,)).fetchone():
                break
            seq += 1
        conn.execute("UPDATE code_registry SET last_date=?, last_seq=? WHERE prefix=?",
                     (today, seq, prefix))
        conn.commit()
        return code
    except Exception:
        conn.rollback()
        raise


# ------------------------------------------------------------------ 遗忘

def record_memory(conn, root: Path, *, project: str, title: str, body: str = "",
                  mem_type: str = "fact", tags=None, visibility: str = "shared",
                  owner=None, session_actor=None, attachments=None,
                  allow_sensitive_attachments=False,
                  allow_unenforced_private=False,
                  occurred_at=None, supersedes=None, cause=None, hints=None,
                  supersede_notes=None) -> tuple:
    """**唯一的记忆写入入口** —— CLI 与 MCP 都必须走这里。

    为什么要抽出来：2026-10-22 发现 cmd_remember 与 MCP tool_remember 是两份
    几乎相同但独立的实现，导致 inject 的身份 bug 在两条路径上不一致。
    两条路径必须共用一份写入逻辑。

    attachments: [路径字符串] 或 [{"path":..., "caption":...}]
    返回 (code, rel_path)
    """
    # 先校验时间参数：它若非法，必须在分配编码、落盘之前就失败，
    # 否则会留下一个空号 + 一个孤儿正文文件（编码一经分配不可回收）。
    occurred_at = normalize_occurred_at(occurred_at)
    # visibility 归一与校验（2026-10-29，评审 🔴1）——**必须与 occurred_at 同在
    # `next_code` 之前**：校验若发生在编码分配之后，拒绝时会烧掉一个编码号 + 留下
    # 一个孤儿正文文件（评审实测 `--visibility public` → last_seq 被烧 + 孤儿文件）。
    # "编码一经分配不可回收"这条不变量，本函数已为 occurred_at / project_writable
    # 各写过一次，这是第三处。
    visibility = str(visibility or "shared").strip().lower()
    if visibility not in ("shared", "private"):
        raise ValueError(
            f"未知 visibility={visibility!r}（可用值：shared / private）")
    # ---- F-P1-2：`private` 不许变成骗人的标签 ----
    # 默认档写 private 不生效、且写入方收不到提示。取审查方的"拒绝写入"
    # （一个判定点，补读路径漏一处就是安静泄露）。开关打开时放行——那时过滤是真的。
    # 用户 2026-10-29「做成开关、不强制」的决定没被推翻：开关还在，只是必须真有效。
    if visibility == "private" and not allow_unenforced_private:
        try:
            enforced = _visibility_enforced(conn)
        except Exception:      # noqa: BLE001 —— 判不到就当没强制（宁严勿松）
            enforced = False
        if not enforced:
            raise ValueError(
                "`visibility=private` 当前不会生效——它只会变成一个标签，"
                "而你不会收到任何提示。\n"
                "　原因：`privacy.visibility_enforced` 关着（默认）。\n"
                "　要真私有：`fish config set privacy.visibility_enforced 1` 后再写。\n"
                "　只要标签：加 `--allow-unenforced-private`（并接受它不保护任何东西）。")
    # 项目可写性（2026-10-25 · 阶段 D）：closed / retired 拒绝写入。
    # 为什么拦在**这里**而不是 MCP 层：`record_memory` 是唯一写入口（CLI 与 MCP 共用），
    # 拦在别处必然有一条路径漏掉——那正是"两条路径必须共用一份写入逻辑"的原始教训。
    # 必须在 `next_code` **之前**：先拒绝，才不会留下空号（编码一经分配不可回收）。
    _ok, _why = project_writable(conn, project)
    if not _ok:
        raise ValueError(_why)
    # F-P1-X（独立评审 🟠-6）：**附件校验必须在分配编码之前**。
    # 旧顺序是 next_code -> 校验附件，于是被拒的附件把一个编码号烧掉了，
    # 下一条记忆从 -02 开始。而本函数上方自己写着「编码一经分配不可回收」——
    # 一条违反自己不变量的路径，比没有这条不变量更糟：它让人以为它成立。
    # 这里只**校验**不落盘；落盘仍在 next_code 之后（避免留下更多孤儿 blob）。
    for item in attachments or []:
        _ap = Path(item["path"]) if isinstance(item, dict) else Path(item)
        if not _ap.exists():
            raise FileNotFoundError(f"附件不存在：{_ap}")
        validate_attachment(_ap, conn=conn,
                           allow_sensitive=bool(allow_sensitive_attachments))
    code = next_code(conn, project)
    # 正文路径统一走 `body_relpath`（编码 + 标题缩写）。
    # 为什么不在这里直接拼：`rebuild_projection` 也要算同一个路径，两处各拼一次
    # 必然会漂移——这类"两份实现"的亏本项目吃过多次（D-10 的旧重建就是手抄一份逻辑）。
    rel = body_relpath(project, code, title)
    d = root / "projects" / project
    d.mkdir(parents=True, exist_ok=True)

    # ---- 附件：内容寻址 + 去重 ----
    atts = []
    for item in attachments or []:
        if isinstance(item, dict):
            src, cap = Path(item["path"]), item.get("caption", "")
        else:
            src, cap = Path(item), ""
        if not src.exists():
            raise FileNotFoundError(f"附件不存在：{src}")
        meta = store_blob(root, src, conn=conn,
                          allow_sensitive=bool(allow_sensitive_attachments))
        meta["caption"] = cap
        atts.append(meta)

    # ---- 正文 ----
    lines = [f"# {title}", "",
             f"> 编码：{code} ｜ 类型：{mem_type} ｜ 记录于 {now_utc()}", ""]
    if body:
        lines += [body, ""]
    if atts:
        lines.append("## 附件")
        for at in atts:
            cap = f" — {at['caption']}" if at["caption"] else ""
            lines.append(f"- [{at['name']}]({at['blob']}) "
                         f"({at['size']} B, sha256:{at['sha256'][:12]}…){cap}")
        lines.append("")
    # F-P2-8：原子写（崩在中途会留半个文件，而投影看着完全正常）
    # F-P2-8：哈希算**落盘的字节**（原始 body 少了标题行/附件段，校验会假红）
    _body_text = "\n".join(lines) + "\n"
    fish_atomic.write_text_atomic(root / rel, _body_text)
    body_sha256 = fish_atomic.sha256_of_file(root / rel)   # 算盘上的字节，不是字符串

    # ---- 索引 + 事件 ----
    # 时间事实只取一次：occurred_at（这件事什么时候发生）进事件 payload，
    # recorded_at（我们什么时候知道）等于事件时间。两份事实各只有一处来源。
    # 2026-10-22 修 07 D-11：旧版 payload 不含 occurred_at，导致"忠实重建"在事件层面不可能。
    # 时间事实：occurred_at 已在函数开头校验归一；recorded_at 就是"现在"。
    recorded_at = now_utc()
    # ---- 投影 + 事件 + 全文索引：**一个事务**（外部审查 F-P0-1）----
    #
    # 旧实现是三次独立提交：code_index 先 commit，append_event 自己 commit，
    # _index_one_fts 再自己 commit。后两者任一失败就留下
    # "投影有、事件无、FTS 无"的半状态——重建不忠实、检索断链、审计缺一条，
    # 而**没有任何东西会红**：query 照常返回那条记忆。
    #
    # 现在三件事只提交一次；异常整体回滚，并把已写下的正文文件删掉
    # （否则留下一个没有任何索引指向的孤儿文件，比没有更糟）。
    try:
        conn.execute(
            "INSERT INTO code_index (code,title,project,owner,mem_type,visibility,status,tags,"
            "file_path,occurred_at,recorded_at,access_count,dormant,session_actor,attachments) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,?,?)",
            (code, title, project, owner or "unknown", mem_type, visibility, "active",
             json.dumps(tags or [], ensure_ascii=False), rel,
             occurred_at, recorded_at, session_actor,
             json.dumps(atts, ensure_ascii=False) if atts else None))
        append_event(conn, "memory.recorded",
                     {"citizen_id": owner or "unknown", "evidence_level": "machine"},
                     {"kind": "memory", "id": code},
                     {"code": code, "title": title, "body_ref": rel,
                      # F-P2-8：正文哈希。没有它，**写出之后**被改坏（磁盘损坏/外部
                      # 篡改）查不出来——事件里有引用，但引用不校验内容。
                      "body_sha256": body_sha256,
                      "mem_type": mem_type, "tags": tags or [], "visibility": visibility,
                      "occurred_at": occurred_at,
                      "session_actor": session_actor,
                      # 存完整元数据（不只 name+sha256）：投影要能逐字段重建回来
                      "attachments": atts},
                     cause=cause, commit=False)
        # 全文索引（ADR-005）：正文/标题入 FTS，让单号与中文子串可查
        _index_one_fts(conn, code, title, tags or [], body, commit=False)
        conn.commit()
    except Exception:
        conn.rollback()
        try:
            (root / rel).unlink()
        except OSError:
            pass  # A档：删孤儿文件失败不该掩盖原始异常（原始异常在下面 re-raise）
        raise

    # ---- 替代旧条目（写新的同时标记旧的）----
    # **顺序即承诺**（FB-11）：新条目已经落盘并提交，才轮到置旧条目 superseded。
    # 反过来会留下"旧的已死、新的没写成"的半状态——那比不替代糟得多。
    # `supersede_notes`（可选）把每条的结果/原因交回调用方：CLI 要按 MCP 同口径
    # 打印，而"静默吞掉失败"正是双入口不一致的老毛病。
    if supersedes:
        olds = supersedes if isinstance(supersedes, (list, tuple)) else [supersedes]
        for old in olds:
            try:
                msg = supersede_memory(conn, old, code, reason="被新记录替代", actor=owner)
                if supersede_notes is not None:
                    supersede_notes.append({"old": old, "ok": True, "message": msg})
            except ValueError as e:
                # 旧条目不存在就不阻断写入（新条目已经成功），但要在返回值里体现
                if supersede_notes is not None:
                    supersede_notes.append({"old": old, "ok": False, "message": str(e)})

    # ---- 写入时建关联边（13 推送系统设计 §2.3 路 1）----
    # 兜底两层：build_links_on_write 内部逐条兜，这里再兜一次整体。
    # 建边是派生数据，绝不能让 remember 失败——写入才是主流程。
    try:
        build_links_on_write(conn, code, title=title, body=body or "", cause=cause)
    except Exception as exc:      # noqa: BLE001 —— 派生数据失败不阻塞主流程
        warn_derived(conn, "build_links_on_write", exc)

    # ---- 写前查重（2026-10-25 · 阶段 C）----
    # 名字叫"写前"，位置在写入**之后**，两者不矛盾：对照的是**下一次**写。
    # 这一次已经落盘（不阻塞是硬约束：每次写入都弹确认，AI 会绕过系统直接写文件），
    # 但把相似项交给调用方印进回执，AI 下一次写之前就会看见。
    # `hints=None`（不传）= 完全零开销，旧调用一个字都不变；传 `[]` 才计算。
    if hints is not None:
        try:
            # 第五轮评审 🔴：不传 viewer 时等于"过滤掉所有 private"，
            # 于是主人自己写第 2 条同名私有记忆时查重**恒为空**（实测 hints=[]）。
            hints.extend(find_similar_memories(
                conn, title=title, body=body or "", exclude={code},
                viewer_citizen_id=(owner or None)))
        except Exception as exc:      # 查重是提示：不阻塞写入，但失败要出声（静默清零）
            warn_derived(conn, "find_similar_memories", exc)
    # 2026-11-11（R1-4 提问节律）：每成功写一篇**记忆文档**就推进一格游标。
    # 为什么不放进事务里：这是**节律游标**，不是业务事实——写失败最多让下一次
    # 提问晚一篇，不值得为它把整条写入回滚。反过来，若放进事务且回滚，
    # 计数就会和真实文档数悄悄脱钩。
    try:
        fish_profile.bump_question_counter(conn)
    except Exception as _exc:      # noqa: BLE001 —— 计数失败不该让记忆写入失败
        warn_ignored('fish.py record_memory bump_question_counter', _exc)
    return code, rel


def supersede_memory(conn, old_code: str, new_code: str, reason: str = "",
                     actor=None) -> str:
    """把 old_code 标记为被 new_code 替代。

    为什么需要（2026-10-22 反馈 ANYI-261022-17）：同主题两条 rule 并存时，
    AI 无法判断哪条是当前的。superseded 状态让"当前的"唯一。
    """
    o = conn.execute("SELECT status,title,superseded_by FROM code_index WHERE code=?",
                     (old_code,)).fetchone()
    b = conn.execute("SELECT status,title FROM code_index WHERE code=?", (new_code,)).fetchone()
    if not o:
        raise ValueError(f"旧编码不存在：{old_code}")
    if not b:
        raise ValueError(f"新编码不存在：{new_code}")
    if old_code == new_code:
        raise ValueError("不能自己替代自己")
    if o[0] == "superseded":
        # 回显必须报库内实际替代者，不能回显入参（2026-10-22 反馈 ANYI-261022-34）：
        # 后到的会话看到「被 <我的入参> 替代过」会误以为自己的替代生效了，得到假成功。
        actual_by = o[2] or "（未记录）"
        if actual_by == new_code:
            return f"{old_code} 已是 superseded，实际替代者就是 {new_code}（本次未改动）"
        return (f"{old_code} 已经是 superseded，本次调用未生效：\n"
                f"实际替代者是 {actual_by}，不是传入的 {new_code}。\n"
                f"替代者锁定为首次替代者，不允许改写。\n"
                f"下一步：当前结论请看 {actual_by}；若要以 {new_code} 为权威版本，"
                f"请对 {actual_by} 再调一次 supersede（形成 {old_code}→{actual_by}→{new_code} 的链）。")

    try:
        conn.execute("UPDATE code_index SET status='superseded', superseded_by=? WHERE code=?",
                     (new_code, old_code))
        append_event(conn, "memory.superseded",
                     {"citizen_id": actor or "unknown", "evidence_level": "machine"},
                     {"kind": "memory", "id": old_code},
                     {"old": old_code, "new": new_code, "reason": reason}, commit=False)
        # 双写 links（13 §2.2「supersedes：已有 superseded_by 列 → 双写」）。
        # 方向与 code_index.superseded_by 同向：**被替代者 → 替代者**，同一事实两种存储。
        # 单独兜异常：状态已经写成功了，建边失败不能让人以为"替代没生效"。
        try:
            add_link(conn, old_code, new_code, "supersedes", "explicit", 1.0,
                     reason or "被新记录替代", commit=False)
        except Exception as exc:      # 取代边丢了 → 出声（静默清零）
            warn_derived(conn, "add_link(supersedes)", exc)
        conn.commit()
    except Exception: conn.rollback(); raise
    return f"{old_code} 已标记为被 {new_code} 替代"


def record_access(conn, codes, source, *, counted=True, actor=None):
    """记录一次「被想起」：刷新访问字段 + **写事件**（2026-10-22 修 07 D-12）。

    为什么要写事件：ADR-001 规定事件是唯一真相源，而访问记录此前只 UPDATE 投影、
    从未落事件——一次灾后重建就永久丢失。现在每次访问都留一笔，重建可重放。

    事件粒度：**一次调用一条事件**（payload 带 codes 列表），不是每条记忆一条。
    否则 recall 一次命中 10 条就写 10 条事件，事件量会被「看」这个动作吃掉。

    counted=False 用于「被看见但不算主动查询」的场景（inject 注入包 / read_body 读正文）：
    只刷新 last_accessed_at，不增加 access_count（保持既有语义）。
    """
    codes = [c for c in (codes or []) if c]
    if not codes:
        return 0
    ts = now_utc()
    try:
        if counted:
            conn.executemany(
                "UPDATE code_index SET last_accessed_at=?, access_count=COALESCE(access_count,0)+1, "
                "dormant=0 WHERE code=?", [(ts, c) for c in codes])
        else:
            conn.executemany(
                "UPDATE code_index SET last_accessed_at=?, dormant=0 WHERE code=?",
                [(ts, c) for c in codes])
        append_event(conn, "memory.accessed",
                     {"citizen_id": actor or "unknown", "evidence_level": "machine"},
                     {"kind": "memory", "id": codes[0] if len(codes) == 1 else "*"},
                     {"codes": codes, "source": source, "counted": bool(counted)}, commit=False)
        conn.commit()
    except Exception: conn.rollback(); raise
    return len(codes)


# ------------------------------------------------------ 推送地基 · 访问日志（13 §2.1）
# 与 code_index 的分工（两者都要，不是二选一）：
#   access_count / last_accessed_at = **全局热度**：这条热不热、还该不该休眠
#   memory_access_log               = **按人事实**：谁读的、哪个会话、在哪个项目里读
# 为什么必须有后者：用户要判「其他 AI 经常读、但他不读」的遗漏（§3.3-B）。
# 只有全局计数时这个判据算不出来——这就是 P0 的第一块地基。
ACCESS_VIAS = ("recall", "resolve", "read_body", "inject", "push", "manual")


def append_access_log(conn, codes, *, via, citizen_id=None, session_key=None,
                      project=None, payload=None, accessed_at=None) -> int:
    """写「按人」访问记录，返回写入行数。

    容错边界（**故意的**）：本函数不做 try/except。它是唯一写入口，容错责任留在
    调用方（MCP 工具层）——这样"日志失败不影响工具调用"这条才能被单独验证；
    把 except 藏在最里层，测试就再也证明不了调用方真的兜住了。

    project 缺省时按编码回查 code_index.project：档期（stint）机制还没落地，
    这是当前能给出的最诚实的"当时在哪个项目"（不是猜，是已知事实）。
    """
    if isinstance(codes, str):
        codes = [codes]
    codes = [c for c in (codes or []) if c]
    if not codes:
        return 0
    if via not in ACCESS_VIAS:
        raise ValueError(f"未知访问来源 via={via!r}（可用：{', '.join(ACCESS_VIAS)}）")
    ts = accessed_at or now_utc()
    proj = {}
    if project is None:
        proj = dict(conn.execute(
            "SELECT code,project FROM code_index WHERE code IN ("
            + ",".join("?" * len(codes)) + ")", codes).fetchall())
    payload_s = json.dumps(payload, ensure_ascii=False) if payload is not None else None
    rows = [(c, citizen_id, session_key,
             project if project is not None else proj.get(c), via, payload_s, ts)
            for c in codes]
    conn.executemany(
        "INSERT INTO memory_access_log (code,citizen_id,session_key,project,via,payload,"
        "accessed_at) VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    return len(rows)


def access_log_rows(conn, code=None, citizen_id=None, session_key=None, via=None,
                    limit=200) -> list:
    """读访问记录（测试与后续画像/遗漏检测共用一份读法，避免两处各写一份过滤）。"""
    sql = ("SELECT code,citizen_id,session_key,project,via,payload,accessed_at "
           "FROM memory_access_log WHERE 1=1")
    params = []
    for col, val in (("code", code), ("citizen_id", citizen_id),
                     ("session_key", session_key), ("via", via)):
        if val is not None:
            sql += f" AND {col}=?"
            params.append(val)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    return conn.execute(sql, params).fetchall()

# ------------------------------------------------------ 推送地基 · 关联边（13 §2.2/§2.3）
# 五种关系沿用 03 §F 的既有设计，不新造。
# 存储约定：**边单向存（src→dst）、查询双向做**——同一条关系不写两条记录。
LINK_RELS = ("supersedes", "related", "derived", "contradicts", "precedes")
LINK_SOURCES = ("explicit", "structural", "semantic", "manual")


def add_link(conn, src, dst, rel, source="explicit", weight=1.0, evidence=None,
             created_at=None, *, commit=True) -> bool:
    """建一条单向边 src→dst。返回 True=新建 / False=已存在（幂等）。

    幂等靠 UNIQUE(src,dst,rel) + INSERT OR IGNORE：同一对、同一关系重复生成不叠加。
    `commit=False` 供调用方把"业务写 + 事件 + 边"组成同一个事务（P1-2）。
    """
    if not src or not dst:
        raise ValueError("边的两端都必须是编码")
    if src == dst:
        raise ValueError("不能自己指向自己")
    if rel not in LINK_RELS:
        raise ValueError(f"未知关系 rel={rel!r}（可用：{', '.join(LINK_RELS)}）")
    if source not in LINK_SOURCES:
        raise ValueError(f"未知来源 source={source!r}（可用：{', '.join(LINK_SOURCES)}）")
    w = 1.0 if weight is None else float(weight)
    cur = conn.execute(
        "INSERT OR IGNORE INTO links (src,dst,rel,source,weight,evidence,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (src, dst, rel, source, w, evidence, created_at or now_utc()))
    if commit:
        conn.commit()
    return cur.rowcount > 0


# 正文里「像编码」的串（13 §2.3 路 1 第 3 步）。三次迭代后的口径，每条都是实测踩出来的：
#   · **不能只认 `[A-Z]{2,8}`**：库里有中文前缀（企业智能 / 言纹 / 常用会话，07 D-14），
#     只认大写字母那些记忆永远建不出「正文提及」边；
#   · **前缀字符集含数字与下划线**（2026-10-25 T2）：旧口径让 `A1` / `PROJ_2` / `3D`
#     这类前缀**永远**建不出边——recall 查得到、正文里写着编码却连不上，静默假阴性；
#   · **右锚定到任意非空白字符**（2026-10-25 R2，研判见 `33-编码方案研究与研判`）：
#     `anyi-studio` 里的 `-` 正是分隔符，旧口径对它直接返回 None（16 组用例实测）。
#     右边 `<6位日期>-<2~4位序号>` 是固定格式，天然是锚点——「取满足该后缀的最右一段，
#     左边剩下的全是前缀」，与 semver 从右解析 prerelease 同构（204 条既有编码逐条一致）。
#
# 中文没有空格，所以这里只做**粗筛**，真伪交给 `code_mentions_in()` 用库里真实编码集合
# 裁决（`tails()` 还会剥掉左侧误并进来的字符）。**误报的代价是几次空 IN 查询，
# 漏报的代价是一条永久的断链**——这就是每次都选择放宽的理由。
#
# ⚠️ 必须是**非贪婪** `[^\s]+?`：贪婪版在「先看 PROJA-261025-01，再看企业智能-261025-01」
# 里会一路吃到句尾（中文逗号不是空白），把第一个编码整个吞掉——回归判据 M3 当场红。
CODE_MENTION_RE = re.compile(
    r"(?<![0-9A-Za-z_\u4e00-\u9fff])([^\s]+?)-(\d{6})-(\d{2,4})(?!\d)")


def load_rename_map(conn) -> dict:
    """读所有 `code.renamed.batch` 事件，返回 `{旧编码: 新编码}`。

    2026-10-25 加（外部评估 `EVAL-261025-06` 报的读取侧死路）：
    `rebuild_projection` 早就用它还原编码，但**读取侧（resolve_code / read_body）没接**。
    于是改名之后，AI 拿旧编码来查会得到「编码不存在」——而正文明明在。

    后果比"查不到"更糟：**AI 会据此判定这条记忆已丢失，然后重记一条**，制造重复。
    而且它会**持续复发**——本地工作空间的 MEMORY.md / daily / artifacts 里
    还有大量 `ANYI-YYMMDD-NN` 写法，每个照旧编码查的 AI 都会踩一次。

    多次改名时**后写的覆盖先写的**（同一旧编码在时间上被复用过的情况，
    以最后一次映射为准——这与 `rebuild_projection` 的口径一致）。
    """
    m = {}
    for (payload_s,) in conn.execute(
            "SELECT payload FROM events WHERE type='code.renamed.batch' ORDER BY seq"):
        try:
            mp = json.loads(payload_s).get("mapping") or {}
        except (ValueError, TypeError):
            mp = {}
        for k, v in mp.items():
            if k and v:
                m[k] = v
    return m


def resolve_renamed(conn, code):
    """若 `code` 是**改名前**的旧编码，返回新编码；否则返回 None。

    只查一层（不做链式追踪）：改名映射是扁平的，链式追踪会让"旧→新→更新"
    这种历史噪音混进来，而实际只会有一层改名。
    """
    m = load_rename_map(conn)
    return m.get(code)


# 语义边的两个门槛：
#   SIM_MIN_TOKENS —— 少于 3 个 token 时"重合"没有证据力（"备份"两个字就能让两条无关记忆
#                     相似度 1.0，那是噪音不是关联）
#   SIM_MIN_SIM    —— Jaccard 下限。太高会漏；太低会把"都写过 remember"这种弱关联写成边。
#                     0.15 是 P0 的起点，P2 拿真实使用数据校准。
SIM_MIN_SIM = 0.15
SIM_MIN_TOKENS = 3

# 正文最多看多少处「编码形态」的候选：粗筛会把左侧汉字并进前缀，每个候选还要展开
# 成「所有后缀前缀」，所以候选数直接决定 IN 查询的绑定参数个数（SQLite 有上限）。
_MENTION_MAX_RAWS = 20
_MENTION_MAX_PROBES = 400


def code_mentions_in(conn, text, exclude=None, limit=50) -> list:
    """文本里**真实存在**的编码（去重、按出现顺序）。

    真值来自 code_index：正则只负责粗筛，是否存在由库裁决——
    不凭正则把 typo 写成一条指向空号的边。
    """
    if not text:
        return []
    raws = []
    # 用 finditer + group(0) 而不是 findall：新正则（R2 右锚定）有**两个捕获组**，
    # `findall` 会返回元组列表（('261022','01')），`tails()` 直接崩。
    # 这里要的是**完整匹配串**（含前缀），所以取 group(0)。
    for m in CODE_MENTION_RE.finditer(text):
        raw = m.group(0)
        if raw not in raws:
            raws.append(raw)
        if len(raws) >= _MENTION_MAX_RAWS:
            break
    if not raws:
        return []

    def tails(raw):
        """粗筛串 → 所有「≥2 字符前缀」的候选编码（去掉左侧误并进来的汉字）。"""
        parts = raw.rsplit("-", 2)
        if len(parts) != 3:
            return []
        prefix, date_part, seq = parts
        cands = []
        for i in range(0, max(1, len(prefix) - 1)):
            if len(prefix) - i >= 2:
                cands.append(f"{prefix[i:]}-{date_part}-{seq}")
        return cands

    probes = set()
    for raw in raws:
        probes.update(tails(raw))
    if not probes:
        return []
    # 只认 active（2026-10-24 修）：正文提到一个**已作废 / 已被替代**的编码时不该建边——
    # 那会把新记忆指向死条目。候选集合仍来自 code_index（真值），只是把退役的排掉。
    found = {r[0] for r in conn.execute(
        "SELECT code FROM code_index WHERE code IN ("
        + ",".join("?" * min(len(probes), _MENTION_MAX_PROBES)) + ") AND status = 'active'",
        sorted(probes)[:_MENTION_MAX_PROBES])}
    if not found:
        return []
    out = []
    for raw in raws:
        hit = raw if raw in found else next((t for t in tails(raw) if t in found), None)
        if hit and hit != exclude and hit not in out:
            out.append(hit)
            if len(out) >= max(1, int(limit)):
                break
    return out


def similar_codes(conn, code, body, *, title="", limit=3, min_sim=SIM_MIN_SIM,
                  min_tokens=SIM_MIN_TOKENS) -> list:
    """FTS 粗召 + Jaccard 精排，返回 [(编码, 相似度)]（13 §2.3 路 1 第 4 步）。

    为什么两段：FTS5 的 bm25 给的是"命中强度"，不是可比的 0~1 相似度；
    links.weight 要的是 0~1（§2.2），所以相似度用 token 集合的 Jaccard 自己算。

    候选池只取 `code_index.status='active'` 的编码（2026-10-24 修）：`memory_fts` 是
    全局派生索引、不区分状态；不筛状态就会把**已作废 / 已被替代**的记忆当相似候选，
    `build_links_on_write` 随即建出指向死条目的语义边。索引本身不动，过滤只发生在读路径。
    """
    toks = {t for t in index_tokens(f"{title or ''} {body or ''}") if len(t) >= 2}
    if len(toks) < max(1, int(min_tokens)):
        return []
    expr = " OR ".join('"' + t.replace('"', '""') + '"' for t in sorted(toks)[:60])
    # 状态过滤必须**下推到 LIMIT 之前**（JOIN code_index）：先取 100 条再过滤，会被
    # 一批非 active 命中占满名额，active 候选被截断成假阴性——与 search_memories 同一个
    # 教训（ANYI-261024-05）。用 FTS 表名而非别名做 MATCH 左侧，与那边保持同一种写法。
    #
    # 2026-10-25 修（外部评估 `EVAL-261025-02` 的 N1）：**必须 ORDER BY bm25**。
    # FTS5 在没写 ORDER BY 时按**插入顺序**（rowid）返回，于是 LIMIT 100 截断的是
    # "最早入库的那 100 条命中"，不是"最相关的那 100 条"。记忆超过 200 条后，
    # 新增的那批永远进不了候选池 → **查重与写时建边静默失效**（不报错、只是不生效）。
    # 这是"没有判据就会静默"的典型：功能看着在，实际只对老数据有效。
    cands = [r[0] for r in conn.execute(
        "SELECT f.code FROM memory_fts f JOIN code_index ci ON ci.code = f.code "
        "WHERE memory_fts MATCH ? AND ci.status = 'active' "
        "ORDER BY bm25(memory_fts) LIMIT 100", (expr,))]
    cands = [c for c in cands if c and c != code]
    if not cands:
        return []
    scored = []
    for other, txt in conn.execute(
            "SELECT code, COALESCE(title_idx,'') || ' ' || COALESCE(body_idx,'') "
            "FROM memory_fts WHERE code IN (" + ",".join("?" * len(cands)) + ")",
            cands).fetchall():
        other_toks = set((txt or "").split())
        if not other_toks:
            continue
        union = len(toks | other_toks)
        if not union:
            continue
        sim = len(toks & other_toks) / union
        if sim >= min_sim:
            scored.append((sim, other))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [(c, s) for s, c in scored[:max(1, int(limit))]]


def find_similar_memories(conn, *, title="", body="", limit=3,
                          min_sim=SIM_MIN_SIM, min_tokens=SIM_MIN_TOKENS,
                          exclude=(), viewer_citizen_id=None) -> list:
    """写入**前**查重：给定标题/正文，找出库里已有的相似记忆。

    返回 `[{"code","title","similarity"}]`，按相似度降序。

    为什么单独一个函数、而不是让调用方自己拼：
      `similar_codes` 的入参是"**已经写进库**的一条记忆"（它要排除自己）；写入前
      没有 code 可给。这里用 `code=""`（空串不匹配任何真实编码）**复用它**——
      判定口径只有一份（FTS 粗召 + Jaccard 精排），不在调用方另写一套。

    **它只产提示，不拦写入**。用户 2026-10-25 的硬约束：如果每次写入都弹确认，
    AI 会绕过系统直接写文件——那比不查重糟糕得多。

    `exclude`：调用方已知的编码（例如 supersedes 的目标），不再重复提示。
    """
    pairs = similar_codes(conn, "", body or "", title=title or "", limit=limit,
                          min_sim=min_sim, min_tokens=min_tokens)
    skip = set(exclude or ())
    out = []
    for code, sim in pairs:
        if code in skip:
            continue
        row = conn.execute(
            "SELECT title, status, visibility, owner FROM code_index WHERE code=?",
            (code,)).fetchone()
        if not row or row[1] != "active":
            continue
        # 🟠-4：查重提示会显示标题 → 它也是泄露渠道，必须过滤。
        if (str(row[2] or "shared").strip().lower() == "private" and _visibility_enforced(conn)
                and str(row[3] or "") != str(viewer_citizen_id or "")):
            continue
        out.append({"code": code, "title": row[0] or "",
                    "similarity": round(float(sim), 3)})
    return out


def find_similar_projects(conn, prefix, *, namespace="", limit=3) -> list:
    """创建前缀**前**查重：给出"可能是同一个项目"的候选。**只提示，不拦。**

    返回 `[{"prefix","namespace","reasons"}]`。

    三条判据（从强到弱），刻意**不用编辑距离**：
      1. **大小写归一后相同** —— 实测 `cdql` 与 `CDQL` 就是同一项目的两个入口；
      2. **前缀互为包含** —— `TESTP` / `TESTP2` 这类手滑；
      3. **命名空间描述高度重合**（token Jaccard ≥ 0.5）—— 名字完全不同但描述同义。

    为什么不用编辑距离：`TESTP` 与 `测试项目` 是两个**真实存在**的同义前缀，
    而它们的编辑距离毫无意义（一个英文一个中文）。用距离判，最该提示的那一对
    反而会被漏掉——这是"看起来更聪明的判据反而更差"的典型。
    """
    p = (prefix or "").strip()
    if not p:
        return []
    p_low = p.lower()
    ns_toks = {t for t in index_tokens(namespace or "") if len(t) >= 2}
    hits = []
    for other, other_ns in conn.execute(
            "SELECT prefix, namespace FROM code_registry WHERE status='active'"):
        if other == p:
            # 精确重名由 `add_project` 自己的查重负责（那边直接拒绝复用）；
            # 这里刻意不重复报，免得同一条信息说两遍。
            continue
        o_low = other.lower()
        reasons = []
        if o_low == p_low:
            reasons.append("大小写不同但同形")
        elif o_low.startswith(p_low) or p_low.startswith(o_low):
            reasons.append("前缀互为包含")
        if ns_toks:
            o_toks = {t for t in index_tokens(other_ns or "") if len(t) >= 2}
            if o_toks:
                union = len(ns_toks | o_toks)
                if union and len(ns_toks & o_toks) / union >= 0.5:
                    reasons.append("命名空间描述高度重合")
        if reasons:
            hits.append({"prefix": other, "namespace": other_ns or "",
                         "reasons": reasons})
    return hits[:max(1, int(limit))]


def format_dup_hint(mem_hints=None, project_hints=None) -> str:
    """把查重结果拼成回执尾巴。**全空返回空串**（不写"暂无相似"这种噪音）。

    文案纪律：先说"没拦你"，再给候选——否则 AI 会把提示当成拒绝信号，
    反而回去找绕路（那正是这条机制要避免的失败模式）。
    """
    lines = []
    if mem_hints:
        lines.append(f"查重提示（**没有拦你**，但建议先看一眼）：库里已有 "
                     f"{len(mem_hints)} 条相似记忆")
        for h in mem_hints:
            lines.append(f"  - {h['code']} {h['title']}（相似度 {h['similarity']}）")
    if project_hints:
        lines.append(f"查重提示（**没有拦你**）：已有 {len(project_hints)} 个相似前缀")
        for h in project_hints:
            lines.append(f"  - {h['prefix']}（{h['namespace']}）"
                         f" —— {'；'.join(h['reasons'])}")
    return ("\n".join(lines) + "\n") if lines else ""


# ------------------------------------------------------ 名字解析（阶段 D · 单一入口）
#
# 纪律：**所有**名字相关的查询都走 `resolve_name` / `resolve_project_names`。
# 为什么必须单一入口：`09` E-06 的教训是"顺手的 SQL 忘条件"；到了别名上，这个病的
# 后果更隐蔽——只要有一处不走这里，就会出现"**查得到但写不进**"的半截行为
# （recall 展开别名、写入却不展开，于是同一个项目看起来有两份记忆），而且**永远不会报错**。


def project_writable(conn, project) -> tuple:
    """项目现在能不能接收新记忆。返回 `(能不能写, 原因)`。

      · **active** → 可以写；
      · **closed** → 不能写，提示 reopen（用户拍板："close 是可逆的状态，要写就打开"）；
      · **retired** → 不能写（名字已废弃，前缀不再分配）；
      · **库里没有这个前缀** → **可以写**。这不是漏判：老库与测试库里
        `code_registry` 行可能缺失，而 `record_memory` 历来允许"先写后登记"。
        把"没登记"升级成拒绝，等于用新规则改写历史，会让所有既有数据当场不可写。
    """
    p = (project or "").strip()
    if not p:
        return True, ""
    row = conn.execute("SELECT status FROM code_registry WHERE prefix=?", (p,)).fetchone()
    if not row:
        return True, ""
    st = row[0]
    if st == "closed":
        return False, (f"项目 {p} 已闭合（closed）。要往里写，先 "
                       f"`fish project reopen {p}`（闭合是可逆的，不是锁死）")
    if st == "retired":
        return False, f"项目 {p} 已退役（retired），不再接收新记忆"
    return True, ""


def link_alias(conn, kind, from_name, to_name, *, actor=None, reason="") -> dict:
    """建立一条"`from_name` 就是 `to_name`"的别名。**单向**。

    拒绝四种情况（每一种都是真会踩的）：
      1. kind 不合法 / 名字为空 / `from == to`；
      2. **`to_name` 不存在** —— 别名必须指向一个**真实存在**的规范名，否则等于指向空号，
         查出来是"有别名、无实体"，比没有更糟；
      3. `from_name` **已有别名** —— 要改先 `unlink_alias`，不允许悄悄覆盖一条映射；
      4. **成环** —— 单向图成环之后谁都解析不出来（`A→B→A`），且不会报错。

    `to_name` 本身可以是另一个别名：会先解析到真正的规范名再落库（避免 A→B、B→C
    这种链式写入后 A 只走一跳就到 B 的歧义）。
    """
    if kind not in _ALIAS_KINDS:
        raise ValueError(f"kind 只能是 {_ALIAS_KINDS}，收到 {kind!r}")
    f = (from_name or "").strip()
    t = (to_name or "").strip()
    if not f or not t:
        raise ValueError("from_name / to_name 都不能为空")
    if f == t:
        raise ValueError("from_name 与 to_name 相同——那不是别名，是同一件事")

    info = resolve_name(conn, kind, t)
    if not info["found"]:
        raise ValueError(f"规范名不存在：{t}（别名必须指向一个真实存在的名字）")
    t = info["canonical"]
    if t == f:
        raise ValueError(f"{f} 本来就解析到 {t}，再建一条就是成环")

    row = conn.execute("SELECT to_name FROM name_aliases WHERE kind=? AND from_name=?",
                       (kind, f)).fetchone()
    if row:
        raise ValueError(f"{f} 已有别名 → {row[0]}；要改请先 unlink_alias")
    if _walk_alias(_alias_map(conn, kind), t)[0] == f:
        raise ValueError(f"会成环：{f} → {t} → … → {f}")

    try:
        conn.execute(
            "INSERT INTO name_aliases (kind,from_name,to_name,created_at,created_by,reason) "
            "VALUES (?,?,?,?,?,?)", (kind, f, t, now_utc(), actor, reason or ""))
        append_event(conn, "name.alias_linked",
                     {"citizen_id": actor, "evidence_level": "self-reported"},
                     {"kind": kind, "id": f},
                     {"from": f, "to": t, "reason": reason or ""}, commit=False)
        conn.commit()
    except Exception: conn.rollback(); raise
    return {"kind": kind, "from": f, "to": t, "created": True}


def unlink_alias(conn, kind, from_name, *, actor=None, reason="") -> bool:
    """撤销一条别名。返回是否真的删了（**幂等**：不存在 → False，不报错）。

    为什么允许撤销：别名表达的是"我判断这两个名字是一个东西"，判断可能错。
    撤销只是删一行映射，**不碰任何记忆的 project 字段**——所以它天然可逆。
    """
    if kind not in _ALIAS_KINDS:
        raise ValueError(f"kind 只能是 {_ALIAS_KINDS}，收到 {kind!r}")
    f = (from_name or "").strip()
    if not f:
        return False
    try:
        cur = conn.execute("DELETE FROM name_aliases WHERE kind=? AND from_name=?",
                           (kind, f))
        deleted = bool(cur.rowcount)
        if deleted:
            append_event(conn, "name.alias_unlinked",
                         {"citizen_id": actor, "evidence_level": "self-reported"},
                         {"kind": kind, "id": f},
                         {"from": f, "reason": reason or ""}, commit=False)
        conn.commit()
    except Exception: conn.rollback(); raise
    return deleted


def build_links_on_write(conn, code, *, title="", body="", cause=None) -> dict:
    """写入时建关联边（13 §2.3 路 1 的 explicit + semantic 两条）。

    `supersedes` 不在这里重复：它在 supersede_memory() 里双写——两条写入路径
    （remember 的 supersedes 参数、独立的 supersede 工具）共用同一处，不会各写一份。

    每条边各自兜异常：**建边失败绝不能让 remember 失败**（边是派生数据）。
    返回 {"derived": n, "mentioned": n, "similar": n}，供测试与调试读数。
    """
    stats = {"derived": 0, "mentioned": 0, "similar": 0}

    # ① derived：cause 指向的编码（B 由 A 得出）
    try:
        parents = code_mentions_in(conn, cause or "", exclude=code, limit=5)
    except Exception:
        parents = []
    for parent in parents:
        try:
            if add_link(conn, parent, code, "derived", "explicit", 1.0, "写入时 cause 指向"):
                stats["derived"] += 1
        except Exception as exc:
            warn_ignored('fish.py:2958 build_links_on_write', exc)

    # ② related · explicit：正文里提到的编码（"这条接着那条"的显式线索）
    try:
        mentioned = code_mentions_in(conn, body or "", exclude=code)
    except Exception:
        mentioned = []
    for other in mentioned:
        try:
            if add_link(conn, code, other, "related", "explicit", 0.8, "正文提及"):
                stats["mentioned"] += 1
        except Exception as exc:
            warn_ignored('fish.py:2970 build_links_on_write', exc)

    # ③ related · semantic：FTS 相似 top-3（排除自己）
    try:
        similars = similar_codes(conn, code, body or "", title=title, limit=3)
    except Exception:
        similars = []
    for other, sim in similars:
        try:
            if add_link(conn, code, other, "related", "semantic", round(float(sim), 4),
                        "正文相似"):
                stats["similar"] += 1
        except Exception as exc:
            warn_ignored('fish.py:2983 build_links_on_write', exc)
    return stats

# ------------------------------------------------------------------ 全文检索（ADR-005）
# 切词三函数（index_tokens / fts_text / fts_match_expr）已下沉 `fish_common`
# （2026-10-27 · 语料层 T01 · 设计 D09）：语料层要按**同一套**切法建 corpus_fts，
# 两套切法的症状是「存进去搜不到」而两边单测都绿。这里只留注释，名字由上面那行
# `from fish_common import …` 再导出（同一对象，不是副本）。


def search_memories(conn, *, query=None, code=None, project=None, tags=None,
                    scope="all", limit=10, include_superseded=False,
                    viewer_citizen_id=None, since=None, until=None):
    """检索记忆——**CLI 与 MCP 共用的唯一读入口**（ADR-005）。

    排序优先级：精确编码 > 编码片段 > 标题命中 > 正文命中。
    扫描只发生在 SQL；切分与去重在这里，两条调用路径不许各写一份。

    返回 (rows, sources)：rows 每项是
      (code, title, project, mem_type, status, file_path, dormant, 匹配来源)
    """
    limit = max(1, int(limit or 10))
    order, src_of = [], {}

    def add(codes, source):
        for c in codes:
            if c and c not in src_of:
                src_of[c] = source
                order.append(c)

    q = (query or "").strip()

    # 过滤器在 FTS 命中同一次查询里完成（2026-10-24 修 · 反馈 ANYI-261024-05）：
    # 旧写法先 `LIMIT n` 取候选、再按 status 过滤，于是"最早那几条命中恰好都已 superseded"
    # 的查询会被整体滤空——记忆明明存在却查不到（实测 query="反馈"：命中 23 条、返回 0 条，
    # 其中 active 有 8 条）。假阴性比报错更难发现，所以状态/项目/标签必须和 MATCH 一起下推。
    # 阶段 D（2026-10-25）：项目过滤要展开别名——但它**只影响检索**，不改写入归属
    # （用户拍板）。同一份名字集合必须用在**两个地方**：FTS 命中的下推过滤，以及
    # 最后按 order 回取行时的过滤。两处用不同的集合 = 结果忽多忽少且不报错。
    _pnames = resolve_project_names(conn, project) if project else []
    _filters, _fparams = [], []
    if not include_superseded:
        _filters.append("ci.status = 'active'")
    if project:
        # 展开成 IN（含别名）：`recall(project='cdql')` 要能看到 `CDQL` 的记忆。
        _filters.append("ci.project IN (" + ",".join("?" * len(_pnames)) + ")")
        _fparams.extend(_pnames)
    for _t in ([tags] if isinstance(tags, str) else (tags or [])):
        if _t:
            # 评审 🟡9：模式与 ESCAPE 声明成对出现（见 `_like_literal`），
            # 否则 tag 里的 `%` / `_` 会当通配符用。
            _filters.append("ci.tags LIKE ?" + LIKE_ESCAPE_SQL)
            _fparams.append(tag_like_pattern(_t))
    # 2026-11-22：日期边界。**必须和 MATCH 一起下推**——否则先 LIMIT 再过滤，
    # 会把"当天确实命中"的条目整体滤空（这正是上面那段注释记的同类病）。
    for _bound, _op, _end in ((since, ">=", False), (until, "<=", True)):
        _v = _norm_date_bound(_bound, end=_end)
        if _v:
            _filters.append(f"COALESCE(ci.occurred_at, ci.recorded_at) {_op} ?")
            _fparams.append(_v)
    _where = (" AND " + " AND ".join(_filters)) if _filters else ""
    _fts_sql = ("SELECT f.code FROM memory_fts f JOIN code_index ci ON ci.code = f.code "
                "WHERE memory_fts MATCH ?" + _where + " LIMIT ?")

    if code:
        add([r[0] for r in conn.execute(
            "SELECT code FROM code_index WHERE code=?", (code,))], "编码")

    if q:
        add([r[0] for r in conn.execute(
            "SELECT code FROM code_index WHERE code=?", (q,))], "编码")
        # 编码片段：FTS 对"部分编码"无能为力（token 化会把号码切碎），保留 LIKE 兜底
        add([r[0] for r in conn.execute(
            "SELECT code FROM code_index WHERE code LIKE ? ORDER BY code DESC LIMIT ?",
            (f"%{q}%", limit))], "编码")
        expr = fts_match_expr(q)
        if expr:
            # F-2（2026-11-21）：**精确优先，但绝不空手而归**。中文按 bigram 切词会产生
            # 跨词冗余 token（`积分账本 漂移` → `"积分" AND "分账" AND "账本" AND "漂移"`，
            # `分账` 很少出现 → 整条零命中）。**这不是"不分词"，是"分词后 AND 太严"。**
            # 所以 AND 零命中时降级 OR，并标注"宽匹配"——不让人以为零命中是真没有。
            _wide = expr.replace(" AND ", " OR ") if " AND " in expr else None
            for _sc, _label in (("title", "标题"), ("body", "正文")):
                if scope not in ("all", _sc):
                    continue
                got = [r[0] for r in conn.execute(
                    _fts_sql, (f"{_sc}_idx:({expr})", *_fparams, limit))]
                via = _label
                if not got and _wide:
                    got = [r[0] for r in conn.execute(
                        _fts_sql, (f"{_sc}_idx:({_wide})", *_fparams, limit))]
                    via = _label + "(宽匹配)"
                if got:
                    add(got, via)

    if not order:
        return [], {}

    sql = ("SELECT code,title,project,mem_type,status,file_path,dormant FROM code_index "
           "WHERE code IN (" + ",".join("?" * len(order)) + ")")
    params = list(order)
    if not include_superseded:
        # 默认只给 active：superseded（被取代）与 withdrawn（作废）都不该出现在默认检索里
        sql += " AND status = 'active'"
    if project:
        sql += " AND project IN (" + ",".join("?" * len(_pnames)) + ")"
        params.extend(_pnames)
    for t in ([tags] if isinstance(tags, str) else (tags or [])):
        if t:
            sql += " AND tags LIKE ?" + LIKE_ESCAPE_SQL
            params.append(tag_like_pattern(t))

    found = {r[0]: r for r in conn.execute(sql, params)}
    # 可选可见性过滤（默认关；开关打开时才按 owner 剔除 private）
    found = _filter_by_visibility(conn, found, viewer_citizen_id)
    rows = []
    for c in order:
        if c in found:
            rows.append(tuple(found[c]) + (src_of[c],))
            if len(rows) >= limit:
                break
    return rows, src_of


# ------------------------------------------------- 列出模式（无 query 的记忆列举）
#
# 2026-10-26（FB-01）：在这之前，"只看某个项目里带某个标签的条目"这件事**做不到**——
#   · `recall(project=..., tags=...)` 没有 query 时会走"未找到 + 最近条目"，
#     也就是明明给了完整的过滤条件，回包却说"没找到"，然后丢一串**不受 tags 约束**的
#     最近条目（`recent_fallback` 应用了 tags，但语义仍是"没找到"）；
#   · `recall(tags=[...])` 单独用则直接报错「query / code / project 至少要给一个」——
#     报错本身没错（没有限定范围），错的是**没说清怎么修**。
# 所以补一个显式的"列出"读入口：它不猜关键词，只按 项目 + 标签 过滤后按时间倒序给。
# 与 `search_memories` 同一个纪律：**CLI 与 MCP 共用一份**，并且返回同样形状的行，
# 这样两边的渲染器也共用（两处各写一份必然漂，见 `recent_fallback` 那次跨项目串台）。

LIST_SOURCE = "列出"        # 行的"匹配来源"列：告诉读者这条不是搜到的，是列出来的
# 列出模式的默认条数（比检索的 10 宽）：没有关键词时，"多看几条"本身就是目的。
LIST_DEFAULT_LIMIT = 20

# `tags` 单独用时的引导语（MCP 与 CLI 共用一处）。为什么是引导而不是报错：
# 报错只说明"不行"，而调用方真正缺的是"那该怎么写"。这句话就是下一步。
TAGS_ONLY_HINT = (
    "tags 只作附加过滤；请加 project 限定"
    "（例：recall(project=\"anyi-studio\", tags=[\"fish-feedback\"])）")


def _like_literal(value) -> str:
    """把用户给的字符串转成 LIKE 的**字面量**：转义 `\\` / `%` / `_`（评审 🟡9）。

    为什么必须转义：标签是**用户数据**，而 `%` 与 `_` 在 LIKE 里是通配符。
    旧实现 `tags LIKE '%"<t>"%'` 里 t 直接拼进模式，于是 tag `a_b` 会命中 `axb`、
    `a%b` 会命中任何夹在中间的值——**查错了却查到了**，是比查不到更坏的一类错
    （判据/SQL 都不会报错，只有人肉对比才发现多了一条）。

    转义字符选反斜杠，调用点的 SQL 必须同步写 `ESCAPE '\\'`（SQL 侧是**一个**字符）。
    两处只改一处等于没改：模式转义了但 SQL 没声明 ESCAPE，反斜杠就被当字面量。
    """
    text = "" if value is None else str(value)
    return (text.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_"))


# 与 `_like_literal` 成对出现的 SQL 片段（含引号内的单个反斜杠 → SQL 里 ESCAPE '\'）。
# 抽成常量是为了让"改一处忘另一处"不可能发生：模式与 ESCAPE 声明必须同时在场。
LIKE_ESCAPE_SQL = " ESCAPE '\\'"


def tag_like_pattern(tag) -> str:
    """标签过滤的 LIKE 模式：`%"<转义后的标签>"%`（匹配 tags JSON 里的一个完整标签）。

    为什么带引号包住：`tags` 列存的是 JSON 数组文本，标签边界就是引号——
    不包引号的话 `a` 会命中 `ab`（那是"前缀"不是"同一个标签"）。

    **两层转义，顺序不能反**（2026-10-26 复审 ⚪N5）：
      ① 先过 **JSON**：库里的文本是 `json.dumps` 的产物，标签里的 `\\` 与 `"` 已经被
         JSON 转义过一次（`a\\b` 存成 `"a\\\\b"`）。拿原串直接去匹配，含反斜杠的
         标签**永远查不到**；
      ② 再过 **LIKE**：JSON 转义吐出来的反斜杠，在 LIKE 里必须再转义一次，
         否则会被 SQL 的 `ESCAPE '\\'` 当转义符吃掉。
    先 LIKE 后 JSON 等于把①的产物当成用户数据再转义一遍——两层都会错。
    `ensure_ascii=False`：中文标签在库里是原字面量（写入侧同一份 dumps 口径）。
    """
    text = "" if tag is None else str(tag)
    json_literal = json.dumps(text, ensure_ascii=False)[1:-1]
    return f'%"{_like_literal(json_literal)}"%'


def _norm_date_bound(value, *, end=False):
    """把日期边界规范成可直接比较的 ISO 时间戳（`recall --since/--until` 用）。

    ## 为什么不能直接拿日期去比字符串

    `occurred_at` 存的是 `2026-11-22T08:42:08Z`。若 `--until 2026-11-22` 直接拼成
    `occurred_at <= '2026-11-22'`，**当天的每一条都会被排除**（`'…T08:42:08Z' > '2026-11-22'`）
    ——这是日期过滤最常见的 off-by-one：用户要"到 22 号"，拿到的是"到 21 号"。
    所以纯日期补足：`since` → `T00:00:00Z`，`until` → `T23:59:59Z`。
    完整 ISO 时间戳原样透传（调用方给了精确时刻就尊重它）。

    `COALESCE(occurred_at, recorded_at)`：`occurred_at` 可能为空（虽然现存 681 条
    全有值，但历史导入与未来写入路径都不保证）。为空退回记录时间——
    **宁可多返回一条，也不让一条真记忆因为缺字段而永远查不到**。
    """
    s = str(value or "").strip()
    if not s:
        return None
    # 已经是完整时间戳（含 T 或空格分隔）就原样用
    if len(s) > 10 and ("T" in s or " " in s):
        return s
    return f"{s}T23:59:59Z" if end else f"{s}T00:00:00Z"


def _list_where(conn, *, project=None, tags=None, include_superseded=False,
                since=None, until=None):
    """列出模式的 WHERE 子句 + 参数——`count_memories` 与 `list_memories` 共用。

    分出来是因为"总数"与"这一页"必须用**同一个谓词**：两处各拼一份，
    迟早出现"共 5 条"却只回了 3 条这种无法自证的不一致。
    项目过滤展开别名（与 `search_memories` / `recent_fallback` 同口径）。
    """
    where, params = [], []
    if not include_superseded:
        where.append("status = 'active'")
    pnames = resolve_project_names(conn, project) if project else []
    if pnames:
        where.append("project IN (" + ",".join("?" * len(pnames)) + ")")
        params.extend(pnames)
    for t in ([tags] if isinstance(tags, str) else (tags or [])):
        if t:
            where.append("tags LIKE ?" + LIKE_ESCAPE_SQL)
            params.append(tag_like_pattern(t))
    # 2026-11-22：日期边界（`recall --since/--until`）。用 COALESCE 兜底，
    # 理由见 `_norm_date_bound` 的 docstring。
    for _bound, _op, _end in ((since, ">=", False), (until, "<=", True)):
        _v = _norm_date_bound(_bound, end=_end)
        if _v:
            where.append(f"COALESCE(occurred_at, recorded_at) {_op} ?")
            params.append(_v)
    return ((" WHERE " + " AND ".join(where)) if where else ""), params


def count_memories(conn, *, project=None, tags=None, include_superseded=False,
                    viewer_citizen_id=None, since=None, until=None) -> int:
    """列出模式命中多少条（**不含** limit）——回包首行的"共 N 条"用它，不用 len(rows)。"""
    clause, params = _list_where(conn, project=project, tags=tags,
                                 include_superseded=include_superseded,
                                 since=since, until=until)
    # F-P1-X（第二轮评审）：计数必须和列表**同口径**。
    # 否则回执写"共 2 条"却只列 1 条——不是泄露，是回执在说谎。
    _cvis_sql, _cvis_params = _visibility_sql(conn, viewer_citizen_id)
    if _cvis_sql:
        _ccond = _cvis_sql.strip()[4:]
        clause = (clause + " AND " + _ccond) if clause.strip() else (" WHERE " + _ccond)
        params = list(params) + _cvis_params
    try:
        return int(conn.execute("SELECT COUNT(*) FROM code_index" + clause,
                                params).fetchone()[0])
    except sqlite3.Error:
        return 0


def list_memories(conn, *, project=None, tags=None, limit=LIST_DEFAULT_LIMIT,
                  include_superseded=False, viewer_citizen_id=None,
                  since=None, until=None):
    """**列出模式**：无 query 时按 项目（含别名展开）+ 标签 过滤，时间倒序。

    与 `search_memories` 的区别是"不猜"：检索要做 FTS + 排序 + 来源标注，
    列出只做过滤 —— 所以它给的每一行都是"符合你写的条件"的条目，而不是
    "碰巧命中关键词"的条目。回包的 `来源` 列写 `列出`，让读者一眼分清。

    返回行形状与 `search_memories` 完全一致：
      (code, title, project, mem_type, status, file_path, dormant, LIST_SOURCE)
    ——同一个形状 = 同一个渲染器，CLI 与 MCP 不必各写一份。

    排序 `recorded_at DESC, code DESC`：先按"这件事什么时候发生"，同刻再按编码
    （编码里的日期段与序号同向，所以这是个稳定的次序）。
    """
    limit = max(1, int(limit or LIST_DEFAULT_LIMIT))
    clause, params = _list_where(conn, project=project, tags=tags,
                                 include_superseded=include_superseded,
                                 since=since, until=until)
    # F-P1-X（评审 🟠-2）：private 必须在**列出模式**里也过滤。
    # 只挡 search 不挡 list，等于"搜不到但列得出来"——那不是私有，是难找。
    #
    # **条件必须插进 WHERE 段，不能接在 SQL 末尾**：末尾已经是
    # `ORDER BY … LIMIT ?`，接 ` AND …` 会变成 `LIMIT ? AND …`——
    # 语法错（实测报 "no such column: visibility"）。第一版就错在这里。
    _vis_sql, _vis_params = _visibility_sql(conn, viewer_citizen_id)
    if _vis_sql:
        cond = _vis_sql.strip()[4:]          # 去掉前导 "AND "
        clause = (clause + " AND " + cond) if clause.strip() else (" WHERE " + cond)
        params = list(params) + _vis_params
    sql = ("SELECT code,title,project,mem_type,status,file_path,dormant FROM code_index"
           + clause + " ORDER BY recorded_at DESC, code DESC LIMIT ?")
    params = list(params) + [limit]
    return [tuple(r) + (LIST_SOURCE,) for r in conn.execute(sql, params).fetchall()]


def format_list_declaration(*, project, tags, shown, total) -> str:
    """列出模式回包的首行声明：**说清这次按什么过滤、共几条**。

    为什么要声明：检索模式的第一行会交代"关键词匹配，不是语义检索"；列出模式
    也必须交代自己的口径，否则读者无法判断"这些就是全部，还是只是被过滤后的子集"。
    全量条数（total）与实际给出条数（shown）分开写，被 limit 截过时读者看得出来。
    """
    conds = [f"project={project}"] if project else []
    tl = [t for t in ([tags] if isinstance(tags, str) else (tags or [])) if t]
    if tl:
        conds.append("tags=" + "、".join(tl))
    head = ("已按 " + ("、".join(conds) if conds else "无过滤")
            + " 过滤，共 " + str(total) + " 条（时间倒序")
    if int(shown) < int(total):
        head += f"，仅列前 {int(shown)} 条"
    return head + "）"


# -------------------------------------- 检索结果的状态列（CLI 与 MCP 共用同一处渲染）
#
# 2026-10-26（FB-10）：`status` 与 `dormant` 是**两个维度**——
#   status  说"这条还能不能用"（active / superseded / withdrawn）
#   dormant 说"它衰减到休眠了没有"（可被召回唤醒）
# 旧输出把 dormant 挂在方括弧**外面**：`[fact/active] (休眠)`。两个后果：
#   · 一个空格的间隔，长行里几乎看不见 → "休眠但能唤醒"这层意思被漏读；
#   · 更像"另一个 status 值"，读者会以为状态名就叫「休眠」。
# 现在两者合并进同一个标签：`[fact/active+dormant]`——**一个方括弧 = 一条条目的完整快照**。
RECALL_DIMENSION_NOTE = "说明：status 与 dormant 是两个维度（dormant=衰减休眠，可被召回唤醒）"


def format_status_tag(mem_type, status, dormant) -> str:
    """状态列的唯一渲染点：`[类型/状态]`，休眠时合并成 `[类型/状态+dormant]`。"""
    return f"[{mem_type}/{status}{'+dormant' if dormant else ''}]"


def recent_fallback(conn, project=None, tags=None, limit=5, include_superseded=False,
                    viewer_citizen_id=None):
    """未命中时给"最近的条目"作提示——**必须遵守与检索同样的过滤**。

    2026-10-22 缺陷（反馈 `ANYI-261022-114` 报的面）：旧实现直接
    `SELECT code,title FROM code_index ORDER BY code DESC LIMIT 5`，取全库最近 5 条，
    于是 `project=ANYI` 的查询未命中时，会捞出一串别的项目的条目（跨项目串台）。
    把它收进这里，CLI 与 MCP 共用一份——两边各写一份正是这个 bug 的来源。

    2026-10-24 补 status：上面那句"同样的过滤"长期只是**诺言**——实现只筛了
    project / tags，于是**作废 / 已被替代**的条目会出现在"最近的条目"里。默认只给
    active，与 `search_memories` 的默认口径一致；`include_superseded=True` 才放开
    （显式要看退役条目时用，例如排查"这条为什么查不到"），调用方按关键字传：
    `recent_fallback(conn, project=p, include_superseded=True)`。
    """
    limit = max(1, int(limit or 5))
    sql = "SELECT code,title FROM code_index"
    where, params = [], []
    if not include_superseded:
        where.append("status = 'active'")
    if project:
        # 2026-10-25：**必须展开别名**，与 `search_memories` 走同一口径。
        # 旧实现是等值 `project = ?`，于是「同一项目的两个入口」（cdql / CDQL、
        # 或 ANYI → anyi-studio）在**未命中的回退列表**里只给一半——而回退列表
        # 往往是用户唯一看到的东西（主检索没命中的时候）。
        # 这正是 2026-10-22 那个"回退列表不遵守过滤"的 bug 的同族：过滤条件只落实了一半。
        pnames = resolve_project_names(conn, project)
        if len(pnames) <= 1:
            where.append("project = ?")
            params.append(pnames[0] if pnames else project)
        else:
            where.append("project IN (" + ",".join("?" * len(pnames)) + ")")
            params.extend(pnames)
    for t in ([tags] if isinstance(tags, str) else (tags or [])):
        if t:
            where.append("tags LIKE ?" + LIKE_ESCAPE_SQL)
            params.append(tag_like_pattern(t))
    # F-P1-X（评审 🟠-2）：未命中时给的"最近条目"同样必须过滤——
    # 否则"没找到私有条目"之后紧跟一句"最近的是这条私有条目"，等于绕过检索。
    # 加进 `where` 列表（不是接在 sql 后面）：本函数的条件是按列表拼的，
    # 直接 `sql + " AND …"` 会在没有 WHERE 时变成语法错误的裸 AND。
    _vis_sql, _vis_params = _visibility_sql(conn, viewer_citizen_id)
    if _vis_sql:
        where.append(_vis_sql.strip()[4:])      # 去掉前导 "AND "
        params.extend(_vis_params)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY code DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


# -------------------------------------- FB-15 提炼建议层（distill；人确认后才入库）
#
# 依据：`16-最终架构-v1-整合裁决.md` §11.4 的 `fish distill <项目> --from episode
# --days 30`。三步里本轮**只做前两步**：按 tags 聚类 → 生成建议文件。
# 三条硬约束（本层一条都不许松）：
#   ① **不自动入库**：产出是「建议」，不是记忆——不写 lesson、不碰 code_index；
#   ② **保留溯源**：建议文件逐条列 source episode 的 code，人确认后写 lesson 时
#      `cause` 可以直接指向这批 code（本轮不生成正文，所以只留 code）；
#   ③ **原 episode 不删**：本轮连 `distilled_into` 都不标（标记是下一轮的事）。
# 为什么不生成 lesson 正文：fish 没有内置 LLM（R1-7 列在 P2）。靠字符串拼接硬造一段
# "总结"等于用模板冒充判断，比不给更糟——建议文件里留着原文编码，人来判断更可靠。
DISTILL_DIRNAME = "提炼建议"
DISTILL_DEFAULT_DAYS = 30
DISTILL_MIN_GROUP = 2
DISTILL_MAX_GROUPS = 10
DISTILL_PREVIEW_CODES = 5


def _distill_now_z(now=None) -> str:
    """把 `now` 归一到库里的时间口径（`%Y-%m-%dT%H:%M:%SZ`，UTC）。

    缺省 = 现在；`datetime`（无 tz 视为 UTC）或 ISO8601 UTC 字符串都接受。
    解析不了就**响亮失败**：窗口算错的建议文件会漏掉该提炼的那批 episode，
    而且漏得无声无息——这比直接报错坏得多。
    """
    if now in (None, ""):
        return now_utc()
    if isinstance(now, datetime.datetime):
        moment = now if now.tzinfo else now.replace(tzinfo=datetime.timezone.utc)
        return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parsed = parse_utc(str(now))
    if parsed is None:
        raise ValueError(
            f"now 必须是 ISO8601 UTC（如 2026-10-26T00:00:00Z），收到 {now!r}")
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_tags_column(raw) -> list:
    """解析 `code_index.tags`（JSON 数组字符串）→ 去重后的标签列表。

    解析失败按空处理、**不抛**：这是读侧聚类，一行坏数据不该让整个 `distill` 崩掉
    （这一条只是不参与聚类，真相不受影响）。非字符串 / 空白 tag 同样跳过。
    """
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for item in value:
        if not isinstance(item, str):
            continue
        tag = item.strip()
        if tag and tag not in out:
            out.append(tag)
    return out


def distill_clusters(conn, *, project, days=DISTILL_DEFAULT_DAYS,
                     min_group=DISTILL_MIN_GROUP,
                     max_groups=DISTILL_MAX_GROUPS, now=None,
                     viewer_citizen_id=None) -> dict:
    """把窗口内的 episode 按 tag 聚成"同主题候选组"——**只读，不写任何东西**。

    口径（与 `list_memories` 同一条）：
      · `mem_type='episode'` 且 `status='active'`（superseded / withdrawn 不算素材：
        它们已经是"被否掉的过去"，再拿去提炼只会把结论带回老路）；
      · `recorded_at >= now - days`（无 `recorded_at` 的行不进窗口）；
      · 项目**展开别名**（`resolve_project_names`，与检索同口径）。

    返回
      `{"project": 规范名, "days": 天数, "min_group": 阈值, "checked": 窗口内条数,
        "clusters": [{"tag": t, "count": n,
                      "episodes": [{"code","title","recorded_at"}, ...]}, ...]}`
      ——组内按 `code` 升序；组间按条数降序、同条数按 tag 升序（要确定性：
      同样的库两次跑必须给出同一个顺序，否则"建议"本身不可复核）。
      `checked` 是窗口内 episode 总数（不管有没有 tag），用来说明"看了多少条"。
    """
    p = (project or "").strip()
    if not p:
        raise ValueError("distill 需要一个项目名（如 测试项目）")
    try:
        days = int(days)
        min_group = int(min_group)
        max_groups = int(max_groups)
    except (TypeError, ValueError):
        raise ValueError("days / min_group / max_groups 必须是整数")
    if days < 0:
        raise ValueError(f"days 不能为负（收到 {days}）")
    if min_group < 1:
        raise ValueError(f"min_group 至少为 1（收到 {min_group}）")
    if max_groups < 1:
        raise ValueError(f"max_groups 至少为 1（收到 {max_groups}）")

    now_z = _distill_now_z(now)
    cutoff = (parse_utc(now_z) - datetime.timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    pnames = resolve_project_names(conn, p) or [p]
    # F-P1-X（第三轮评审）：聚类也返回 `title`，所以它同样必须过滤 private。
    # 这是"补读路径"那轮漏掉的最后一处——`fish.py` 注释当时写"17 处"，
    # 实际只补了 6 处；`distill` 自带 SQL，最容易被这种清单漏掉。
    _dvis_sql, _dvis_params = _visibility_sql(conn, viewer_citizen_id)
    sql = ("SELECT code, title, tags, recorded_at FROM code_index "
           "WHERE mem_type='episode' AND status='active' AND recorded_at IS NOT NULL "
           "AND recorded_at >= ? AND project IN ("
           + ",".join("?" * len(pnames)) + ")" + _dvis_sql + " ORDER BY code")
    rows = conn.execute(sql, [cutoff] + list(pnames) + _dvis_params).fetchall()

    groups = {}
    for code, title, tags, recorded_at in rows:
        for tag in parse_tags_column(tags):
            groups.setdefault(tag, []).append(
                {"code": code, "title": title or "", "recorded_at": recorded_at})

    clusters = []
    for tag, eps in groups.items():
        # 变异自证①：删掉这一行 → 单条 tag 也会成组（E2 必红）。
        if len(eps) < min_group:
            continue
        clusters.append({"tag": tag, "count": len(eps), "episodes": eps})
    clusters.sort(key=lambda c: (-c["count"], c["tag"]))
    return {"project": pnames[0], "days": days, "min_group": min_group,
            "checked": len(rows), "clusters": clusters[:max_groups]}


def write_distill_suggestion(root, project, clusters, *, now=None) -> Path:
    """把聚类结果落成**建议文件**（`data/projects/<项目>/提炼建议/<时间戳>.md`）。

    `clusters` 收 `distill_clusters()` 的原样返回（能带上 checked / days / min_group
    这些说明"这份建议是在什么窗口下得出的"的上下文），也收它的 `clusters` 列表
    （调用方想先裁剪再写时用）。**本函数不接收 conn**：写建议不碰真相，
    这是结构性的保证，不是纪律承诺（变异自证②：给它加一句 INSERT，E4 必红）。

    文件里每条 source episode 以 `- <code> <title>` 出现 = 溯源；确认后写 lesson 时
    `cause` 指向这批 code 即可。返回落盘路径。
    """
    if isinstance(clusters, dict):
        items = list(clusters.get("clusters") or [])
        checked = clusters.get("checked")
        days = clusters.get("days")
        min_group = clusters.get("min_group")
        project = project or clusters.get("project") or ""
    else:
        items = list(clusters or [])
        checked = days = min_group = None

    p = (project or "").strip()
    if not p:
        raise ValueError("write_distill_suggestion 需要项目名（建议文件按项目落盘）")
    now_z = _distill_now_z(now)
    target_dir = Path(root) / "projects" / p / DISTILL_DIRNAME
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = parse_utc(now_z).strftime("%Y%m%d-%H%M%S")
    path = target_dir / f"{stamp}.md"
    seq = 1
    while path.exists():                    # 同一秒跑两次不许互相覆盖
        path = target_dir / f"{stamp}-{seq}.md"
        seq += 1

    head = [f"# 提炼建议 · {p}", "",
            "> 本文件是**建议**，不是记忆：未入库、不自动生效；"
            "确认后由人或 AI 另行 remember。", "",
            f"as-of {now_z}"
            + (f" ｜ 窗口 {days} 天" if days is not None else "")
            + (f" ｜ 窗口内 episode {checked} 条" if checked is not None else "")
            + (f" ｜ 组内 ≥{min_group} 条" if min_group is not None else "")
            + f" ｜ 共 {len(items)} 组", ""]
    body = []
    if not items:
        body += ["（本轮没有达到阈值的同主题 episode，未产生建议。）", ""]
    for idx, cluster in enumerate(items, 1):
        tag = cluster.get("tag") or ""
        eps = cluster.get("episodes") or []
        body += [f"## {idx}. {tag}（{len(eps)} 条）", ""]
        for ep in eps:
            code = ep.get("code") or ""
            title = (ep.get("title") or "").strip()
            body.append(f"- {code} {title}".rstrip())
        body.append("")
    tail = ["---", f"来源：fish distill {p}（只读聚类）；本文件不改变任何记忆状态。", ""]
    path.write_text("\n".join(head + body + tail), encoding="utf-8")
    return path


def withdraw_memory(conn, code: str, reason: str = "", actor=None) -> str:
    """作废一条记忆：`status → withdrawn`，**编码不回收**（ADR-003）。

    为什么用 withdrawn 而不是删除：
      - 事件永不删除（ADR-001，`ev_tables` 上有触发器拦 UPDATE/DELETE）；
      - 正文文件删掉会造成断链，`healthcheck` 会报（02 §8「断链数」是自检项之一）；
      - ADR-003 的编码状态机本来就写了 `active → superseded / withdrawn`，
        并明确「作废只标状态、号不回收」；02 §1.2 的事件 #7 `memory.retired`
        用 payload 的 `status` 区分 superseded / withdrawn。
    所以「弃用」是规格内的动作，不发明任何新东西（2026-10-22 关闭 07 D-6）。

    与 supersede 的区别：supersede 是「被谁取代」（有接替者、链能走），
    withdraw 是「作废」（没有接替者）。两者语义不同，不许混用。
    """
    row = conn.execute("SELECT status,title FROM code_index WHERE code=?", (code,)).fetchone()
    if not row:
        raise ValueError(f"编码不存在：{code}")
    if row[0] == "withdrawn":
        return f"{code} 已经是 withdrawn（本次未改动）"
    if row[0] == "superseded":
        raise ValueError(
            f"{code} 已被 supersede（有替代者）。取代与作废是两种语义："
            f"要作废请先确认那条替代链是否也要处理。")
    try:
        conn.execute("UPDATE code_index SET status='withdrawn' WHERE code=?", (code,))
        append_event(conn, "memory.retired",
                     {"citizen_id": actor or "unknown", "evidence_level": "machine"},
                     {"kind": "memory", "id": code},
                     {"code": code, "status": "withdrawn", "reason": reason}, commit=False)
        conn.commit()
    except Exception: conn.rollback(); raise
    return f"{code} 已作废（withdrawn，编码不回收）"

# ------------------------------------------------------------------ 邻近

def _row_dict(r):
    keys = ("code", "title", "project", "mem_type", "tags", "occurred_at", "status", "dormant")
    return dict(zip(keys, r))


def _day(s):
    d = parse_utc(s)
    return d.strftime("%y%m%d") if d else ""


def score_candidate(base, cand) -> float:
    """mixed 策略的打分。权重写死在代码里，可解释、可测试。"""
    s = 0.0
    if cand["project"] == base["project"]:
        s += 10
    bd, cd = _day(base["occurred_at"]), _day(cand["occurred_at"])
    if bd and cd:
        if bd == cd:
            s += 5
        else:
            try:
                diff = abs((datetime.datetime.strptime(cd, "%y%m%d") -
                            datetime.datetime.strptime(bd, "%y%m%d")).days)
            except ValueError:
                diff = 999
            if diff <= 7:
                s += 2
            s -= 2 / (1 + diff)
    if cand["mem_type"] == base["mem_type"]:
        s += 1
    bt = set(json.loads(base["tags"] or "[]"))
    ct = set(json.loads(cand["tags"] or "[]"))
    s += 3 * len(bt & ct)
    return s


def neighbors(conn, code, n=3, strategy="mixed", cross_project=False,
              include_dormant=False, viewer_citizen_id=None):
    row = conn.execute(
        "SELECT code,title,project,mem_type,tags,occurred_at,status,dormant "
        "FROM code_index WHERE code=?", (code,)
    ).fetchone()
    if not row:
        return None
    base = _row_dict(row)

    sql = "SELECT code,title,project,mem_type,tags,occurred_at,status,dormant FROM code_index WHERE code<>?"
    params = [code]
    if not cross_project:
        sql += " AND project=?"
        params.append(base["project"])
    # 第二轮评审 🟠-2：邻接也按可见性过滤——否则"搜不到私有笔记"与
    # "邻居给了我它的标题"同时成立，私有只藏了正文、没藏住存在。
    _vis_sql, _vis_params = _visibility_sql(conn, viewer_citizen_id)
    if _vis_sql:
        sql += _vis_sql
        params.extend(_vis_params)
    if not include_dormant:
        sql += " AND dormant=0"
    cands = [_row_dict(r) for r in conn.execute(sql, params).fetchall()]

    if strategy == "code":
        before = sorted([c for c in cands if c["code"] < code], key=lambda x: x["code"], reverse=True)[:n]
        after = sorted([c for c in cands if c["code"] > code], key=lambda x: x["code"])[:n]
        return {"strategy": "code", "prev": before, "next": after}

    if strategy == "time":
        # 复合键 (occurred_at, code)：同一秒内按编码决胜负，保证排序稳定、不重叠
        def key(c):
            return (c["occurred_at"] or "", c["code"])

        bk = key(base)
        before = sorted([c for c in cands if key(c) < bk], key=key, reverse=True)[:n]
        after = sorted([c for c in cands if key(c) > bk], key=key)[:n]
        return {"strategy": "time", "prev": before, "next": after}

    if strategy == "tag":
        bt = set(json.loads(base["tags"] or "[]"))
        scored = []
        for c in cands:
            shared = bt & set(json.loads(c["tags"] or "[]"))
            if shared:
                scored.append((len(shared), c))
        scored.sort(key=lambda x: (-x[0], x[1]["code"]))
        return {"strategy": "tag", "hits": [c for _, c in scored[:n]]}

    if strategy == "type":
        hits = [c for c in cands if c["mem_type"] == base["mem_type"]]
        hits.sort(key=lambda x: x["code"], reverse=True)
        return {"strategy": "type", "hits": hits[:n]}

    # mixed
    scored = sorted(((score_candidate(base, c), c) for c in cands),
                    key=lambda x: (-x[0], x[1]["code"]))
    return {"strategy": "mixed", "hits": [{"score": round(s, 2), **c} for s, c in scored[:n]]}

# ------------------------------------------------------------------ 导出面
# `_IMPORTED` = 顶层 import 进来的名字（生成 __all__ 时剔除）；`_recorded_body_sha`
# 是设计图外的域内 helper，单独在 fish.py 再导出，不计入 `__all__`。
_IMPORTED = frozenset({
    "datetime", "json", "re", "shutil", "sqlite3", "Path",
    "fish_atomic", "fish_profile", "store_blob", "validate_attachment",
    "_ALIAS_KINDS", "_alias_map", "_walk_alias", "_index_one_fts", "body_relpath",
    "fts_match_expr", "index_tokens", "local_date", "now_utc", "parse_utc",
    "resolve_name", "resolve_project_names", "warn_derived", "warn_ignored",
    "append_event", "_filter_by_visibility", "_visibility_enforced", "_visibility_sql",
    "_IMPORTED",
})
__all__ = [
    "_day", "_distill_now_z", "_like_literal", "_list_where",
    "_norm_date_bound", "_row_dict", "access_log_rows", "add_link",
    "append_access_log", "archive_body", "archive_relpath", "archive_sweep",
    "build_links_on_write", "code_mentions_in", "count_memories", "distill_clusters",
    "find_similar_memories", "find_similar_projects", "format_dup_hint", "format_list_declaration",
    "format_status_tag", "link_alias", "list_memories", "load_rename_map",
    "locate_body", "neighbors", "next_code", "normalize_occurred_at",
    "parse_tags_column", "project_writable", "read_body_path", "record_access",
    "record_memory", "recent_fallback", "resolve_renamed", "score_candidate",
    "search_memories", "similar_codes", "supersede_memory", "tag_like_pattern",
    "unlink_alias", "withdraw_memory", "write_distill_suggestion",
]
