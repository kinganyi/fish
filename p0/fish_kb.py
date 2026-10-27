#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知识库（B3）子系统（`fish.py` 拆包第一轮 · T03 · 设计 §1.2）。

它是什么
--------
`fish.py` 里所有 `kb_*` / `_kb_*` 顶层函数、所有 `_?KB*` 常量与 DDL，加上域错误
`KBError` 与严格布尔 `_strict_bool`（+ `_STRICT_TRUE/_STRICT_FALSE`），整段搬到这里。
**唯一例外**是 `_kb_push_candidates`：它属于推送管线（要用 `extract_terms`），
留在 `fish.py`，第二轮随 `fish_push` 一起改名再迁。

依赖方向
--------
    fish.py → fish_kb → fish_common

本模块**不许** import fish（判据 J2：静态 AST + 运行时 `sys.modules` 双查）。
所以 `fish.py` 那边用"显式命名再导出"维持调用面：`fish.kb_tree` 与
`fish_kb.kb_tree` 是同一个对象，外部 335 处引用一行都不用改。

`__all__` 为什么是算出来的
--------------------------
手写清单会漏（漏一个的后果是 `fish` 侧**静默**少一个名字）。这里用
"globals 减去 import 名"推出来，再由 `tools/gen_reexport.py --check` 与
`test_split_contract.py` 的 J1b 从 AST 反向校验。
"""
import datetime
import fnmatch
import functools
import hashlib
import json
import ntpath
import os
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

from fish_common import _exc_text, count_tokens, iso_local, now_utc, ulid

# K3：`None → 'unattributed'` 的映射只在写入审计时做一次；常量在这里定死，
# MCP 侧（mcp_server.UNATTRIBUTED）靠判据锁住两处字面量一致。
KB_UNATTRIBUTED = "unattributed"

# K2：错误码是契约，中文文案是解释。CLI 与 MCP 都按 code 判，不按文案判。
KB_ERROR_CODES = (
    "bad_usage", "path_invalid", "outside_vault", "reparse_point", "ignored",
    "not_found", "offline", "too_large", "media", "bad_encoding",
    "not_writable", "exists", "stale_base", "backup_missing",
    "content_too_large", "vault_missing", "mode_ro", "bad_action",
    "bad_frontmatter", "sensitive",
)

# §2.3：kb_access 的 action / result / via 词表。库层刻意不设 CHECK（词表会长），
# 校验放 Python 层——加一个词只改这里一处，不用迁移表。
KB_ACTIONS = ("vaults", "tree", "open", "search", "search_hit",
              "create", "append", "update", "rollback", "reject", "scan")
KB_RESULTS = ("ok", "denied", "not_found", "too_large", "bad_encoding",
              "offline", "media", "ignored", "stale_base", "sensitive")
KB_VIAS = ("cli:kb", "mcp:kb", "mcp:kb_write")

# §5.6：`kb_access.meta` 白名单——超出的键直接丢弃（query 原文、正文片段永不入库）。
# `reason_code`（2026-10-26 第二轮）：审计要能按**错误码**统计拒绝原因，而不是正则猜中文。
KB_META_KEYS = (
    "depth", "limit", "max_chars", "max_tokens", "scan_ms",
    "scanned_files", "scanned_bytes", "skipped_large", "skipped_binary",
    "skipped_encoding", "skipped_sensitive", "query_hash", "qlen", "write_id",
    "reason", "reason_code", "eol", "truncated", "truncated_reasons",
    "same_name_count", "undo_action", "stale", "allow_sensitive",
    "backup_dirs", "backup_bytes", "backup_oldest", "backup_newest",
    "keep_days", "pruned",
)

# ------------------------------------------------------ 知识库 DDL（B3 · 2026-10-26）
# 依据：架构设计 §2.2 / §2.3。三处对 PRD 的修正都是实测驱动，不是口味：
#   ① `path` 不做 UNIQUE，改由 `path_key` 唯一——SQLite 的 UNIQUE 是二进制比较，
#      Windows 上 `E:\Obsidian\X` 与 `e:\obsidian\x` 会被当成两个库；
#      path_key = normcase(realpath(path)) 把大小写、短名、junction 别名一起归一。
#   ② `mode` 保留 CHECK：这是**权限位**（rw = 允许 AI 写人的十年笔记），
#      与 citizens.kind 那类"会长的词表"不同，权限位不该静默扩展。
#   ③ `last_scan_*` 只由显式 `kb vault scan` 写：读路径不写库，否则 `kb tree` 会变成写操作。
_KB_VAULT_DDL = """CREATE TABLE IF NOT EXISTS kb_vault (
  vault_id       TEXT PRIMARY KEY,
  name           TEXT NOT NULL UNIQUE,
  path           TEXT NOT NULL,
  path_key       TEXT NOT NULL,
  mode           TEXT NOT NULL DEFAULT 'ro' CHECK (mode IN ('ro','rw')),
  ignore_globs   TEXT NOT NULL DEFAULT '[".obsidian/**",".trash/**","90-模板/**"]',
  max_open_bytes INTEGER NOT NULL DEFAULT 2097152,
  is_default     INTEGER NOT NULL DEFAULT 0,
  last_scan_at   TEXT,
  last_scan_files   INTEGER,
  last_scan_notes   INTEGER,
  last_scan_bytes   INTEGER,
  note           TEXT,
  status         TEXT NOT NULL DEFAULT 'active',
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);"""

# 审计流水：**只增不改**。刻意不设 FK——vault 被 remove 之后，审计仍要能读。
# `path` 存 vault 内相对路径（分隔符统一 '/'），'' = 整个库级别的动作。
_KB_ACCESS_DDL = """CREATE TABLE IF NOT EXISTS kb_access (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  vault_id    TEXT NOT NULL,
  path        TEXT NOT NULL,
  citizen_id  TEXT NOT NULL DEFAULT 'unattributed',
  account_id  TEXT,
  action      TEXT NOT NULL,
  result      TEXT NOT NULL DEFAULT 'ok',
  via         TEXT NOT NULL,
  ts          TEXT NOT NULL,
  bytes       INTEGER,
  sha256      TEXT,
  backup_path TEXT,
  meta        TEXT
);"""

_KB_VAULT_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_kb_vault_path ON kb_vault(path_key)",
    # 部分唯一索引：默认库最多一个（is_default=0 的行可以无限多）
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_kb_vault_default "
    "ON kb_vault(is_default) WHERE is_default = 1",
)

_KB_ACCESS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_kb_access_path ON kb_access(vault_id, path, ts DESC)",
    "CREATE INDEX IF NOT EXISTS ix_kb_access_citizen ON kb_access(citizen_id, ts DESC)",
    "CREATE INDEX IF NOT EXISTS ix_kb_access_action ON kb_access(action, ts DESC)",
)

# 聚合视图：回答"谁在何时看过/改过哪篇 + 备份在哪"。事件流水能回答，
# 聚合表答不了（回滚是用户拍板的硬需求，只有流水能定位到具体那次写）。
_KB_PATH_STATS_VIEW = """CREATE VIEW IF NOT EXISTS kb_path_stats AS
SELECT vault_id,
       path,
       COUNT(*)                                                    AS hits,
       MAX(ts)                                                     AS last_at,
       SUM(CASE WHEN action='open' THEN 1 ELSE 0 END)               AS open_count,
       SUM(CASE WHEN action IN ('create','append','update') THEN 1 ELSE 0 END) AS write_count,
       SUM(CASE WHEN action='rollback' THEN 1 ELSE 0 END)           AS rollback_count,
       COUNT(DISTINCT citizen_id)                                   AS readers
FROM kb_access
GROUP BY vault_id, path;"""

# 视图"自愈"的契约列：`_ensure_kb_views` 拿 PRAGMA table_info 读真实列与它比对。
_KB_PATH_STATS_COLUMNS = ("vault_id", "path", "hits", "last_at", "open_count",
                          "write_count", "rollback_count", "readers")

# 两份 DDL 共用同一份常量给两个消费者（SCHEMA 建新库 / _ensure_kb_tables 升旧库）。
# 再复制一份到 migrate 里就是漂移的起点（_ACCESS_LOG_DDL 的注释写过同一条理由）。
_KB_SCHEMA = "\n" + "\n\n".join((_KB_VAULT_DDL, _KB_ACCESS_DDL)) + "\n"

def _kb_backfill_mandatory_globs(conn: sqlite3.Connection) -> list:
    """把 `KB_MANDATORY_DENY` 回填进**既有** `kb_vault` 行（S2 · 幂等）。

    ## 为什么需要（评审 🟠-N1 的现场）

    强制忽略项过去只是 `kb vault add` 写进行里的**初值**：R5 之前登记的库行里没有
    `90-模板/**`，而 `set-mode` 不重合并，于是唯一的真实库在合并后 `--mode rw` 就能写
    `90-模板/`——裁决 Q13 的硬边界在生产上不成立，而判据用的是新建库（永远绿）。

    现在有两道：判定层 `_kb_ignored` 永远并强制项（**这次**调用就有效），本函数把行
    **补成一致的形态**（`kb vault list` / 回执里的"生效忽略规则"不再说谎）。
    两道都要：只靠回填，`migrate()` 没跑过的连接仍会漏；只靠判定层合并，
    行与生效规则长期漂移（文档/排查会读行）。

    坏 JSON 的行**跳过不猜**：那是 `_kb_vault_norm` 的响亮失败面（`bad_usage`），
    迁移期不该替它决定"应该是什么规则"。
    """
    changed = []
    for vault_id, raw in conn.execute("SELECT vault_id, ignore_globs FROM kb_vault"):
        try:
            current = _kb_json_globs(raw)
        except KBError:
            continue
        merged = _kb_effective_globs(current)
        if merged == current:
            continue
        conn.execute(
            "UPDATE kb_vault SET ignore_globs=?, updated_at=? WHERE vault_id=?",
            (json.dumps(merged, ensure_ascii=False), now_utc(), vault_id))
        changed.append(vault_id)
    return changed

class KBError(Exception):
    """域错误（唯一异常类，CLI 与 MCP 共用）。

    `code` 是契约（`KB_ERROR_CODES`），`message` 是人读的中文，`hint` 是"下一步怎么做"。
    **判据认 code，不认文案**（K2）——文案会改，code 不会。

    为什么 `__str__` 就自带"下一步"：`mcp_server.call_tool` 已把任何 Exception 转成
    `isError=True` 的文本（无新错误通道），所以这里拼好的两步文案会被 AI 原样看到。
    """

    def __init__(self, code, message, *, hint=None, detail=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.detail = detail

    def __str__(self):
        return self.message + (f"\n  下一步：{self.hint}" if self.hint else "")

def kb_sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def kb_sha256_file(path) -> str:
    """文件 sha256（分块读：门面体检要扫 420 篇正文，一次性读进内存没有必要）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def kb_detect_eol(data: bytes) -> str:
    """判换行风格：`"lf"` | `"crlf"`（按**字节**判，解码前就能用）。

    为什么只取前 64 KB：判据是"这个文件主要用哪种换行"，不需要读全量；
    而写回时如果统一成 LF，会在 Obsidian/git 里制造"整文件变动"的假 diff
    （实测抽样：52/60 篇 md 是 CRLF）。

    为什么 `crlf > lf`：纯 LF 文件里 `\r\n` 计数为 0、`\n` 计数 >0 → lf；
    CRLF 文件里每一行都同时贡献一个 `\r\n` 和一个 `\n`，所以要先减去 crlf 才是"独立 LF"。
    """
    head = data[:65536]
    crlf = head.count(b"\r\n")
    lf = head.count(b"\n") - crlf
    return "crlf" if crlf > lf else "lf"


def kb_atomic_write(path, data: bytes, *, eol="lf") -> dict:
    """原子写：同目录临时文件 → fsync → `os.replace`（KB-15 的底座）。

    为什么必须是"同目录"：`os.replace` 的原子性只在同一文件系统内成立；
    跨盘/跨卷的 replace 不是原子重命名（某些实现会退化成复制+删除）。

    为什么临时文件名以 `.` 开头：Obsidian 不索引点开头的文件，
    所以即使进程崩溃留下残留，也不会变成一篇"幽灵笔记"。
    """
    path = Path(path)
    tmp = path.parent / f".{path.name}.tmp-{ulid()[:13]}"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.replace(tmp, path)
    except OSError:
        if tmp.exists():
            tmp.unlink()
        raise
    if tmp.exists():                   # 兜底：replace 成功后不该存在
        tmp.unlink()
    return {"path": str(path), "bytes": len(data),
            "sha256": kb_sha256_bytes(data), "eol": eol}

def _kb_commands_ready() -> bool:
    """知识库读写命令是否已接入（按模块符号判，不写死批次号）。

    为什么这样判：CAPABILITIES 页是"当前构建"的投影。T02 渲染时 `kb_tree` 还不存在，
    页上就该说"尚未接入"；T04 之后同一段代码自动变成"已接入"，不需要改本页。
    """
    return "kb_tree" in globals() and "kb_write" in globals()

# ---------------------------------------------------------------- 5.4 知识库（B3）
# 依据：架构设计 §2.2/§2.3（表）、§3.2（CLI）、§4.1–§4.5（tree/open/search/write/rollback）、
# §5.1–§5.6（路径/忽略/类型/原子写/回滚/隐私）。
#
# 这一段的四条硬边界（破了任何一条都算越权）：
#   ① **只读不索引**：知识库内容永不进 `memory_fts` / `recall` / 注入包（裁决 C1 + KB-10）；
#      `kb search` 是"按需扫描"，回执上必须写明"未建索引"——它不是第二个 recall；
#   ② **路径只能相对**：`..` / 绝对 / 盘符 / UNC / `\\?\` / junction 一律拒绝，
#      判定用 `realpath + commonpath`，**禁止 `str.startswith`**（裁决 §四.7 / K8）；
#   ③ **写要显式授权**：只有 `mode='rw'` 的 vault 能写；写前只备份**被写的那一个文件**，
#      原子替换，写后重读校验 sha256；不允许删/移/改名（create 的撤销除外）；
#   ④ **审计只记事实**：`kb_access.meta` 只存 query_hash 前 12 位 + 长度 + 耗时，
#      不存 query 原文、不存正文（裁决 §四.6 / §5.6 白名单）。

# 忽略规则（§5.2）：`.obsidian/**` 与 `.trash/**` 是 Obsidian 自己的运行目录，
# 既不读也不写；`90-模板/**`（裁决 Q13）：模板决定人以后所有产出，AI 一律不许写。
# `attachments/**` 刻意**不排除**——tree 只统计不展开，排除它反而丢规模信息。
#
# ## 2026-10-26 第四轮（S2 · 评审 🟠-N1 / 安全 N5）：强制项与默认项分开
#
# 原实现把三条规则当成"新登记时写进行里的初值"：既有行（R5 之前登记的库）里只有两条，
# 于是唯一的真实库在 `set-mode rw` 之后**能写 `90-模板/`**——裁决 Q13 的硬边界在生产上
# 不成立。现在拆成两层：
#   · `KB_MANDATORY_DENY`：**强制项**，`_kb_ignored` 每次判定都并进来（用户删不掉）；
#   · `KB_DEFAULT_IGNORE_GLOBS`：登记新库时写进行里的初值（= 强制项，保留旧名字兼容）。
KB_MANDATORY_DENY = (".obsidian/**", ".trash/**", "90-模板/**")
KB_DEFAULT_IGNORE_GLOBS = KB_MANDATORY_DENY

# 敏感名单（R15 · 安全 F4）：命中即 `sensitive`，要读正文必须显式二次授权
# （CLI `--allow-sensitive` / MCP `allow_sensitive=true`）；`search` 永不进这些文件的片段。
# 为什么是**内置不可删**：凭证类文件的保护不能依赖"用户记得加规则"。
#
# 2026-10-26 第四轮（S5 · 安全 N2）两条口径修订：
#   ① 判定范围从 `basename` 扩到**路径的每一段**：`凭证目录/账号清单.md` 的目录名命中
#      也要敏感（原来只护文件名，命中目录名而文件名平常的凭证文件完全不受保护）；
#   ② `*密码*` 过宽会误伤 `密码学笔记.md`（正常笔记默认读不了）→ 改用
#      `_KB_PASSWORD_RE = 密码(?!学)`（负向断言），`密码本.md` / `密码.md` 仍然命中。
# 扩展名族（`*.pem` / `*.key` / `.env*` / `id_rsa*`）不变：它们是高置信度形态。
KB_SENSITIVE_PATTERNS = (
    "*凭证*", "*credential*", "*secret*", "*密码*",
    # 安全审计 🟡-7（2026-10-27）：英文口令类文件名此前完全漏网
    # （`passwords.md` / `api_keys.md` 会入索引并被搜出片段）。只加低误伤四条：
    # `*token*` 刻意不加——它会打到 `tokenize` / `tokens 统计` 这类正常笔记。
    "*password*", "*passwd*", "*apikey*", "*api-key*",
    "*.pem", "*.key", "id_rsa*", ".env*",
)
# 上表里唯一需要"负向断言"的那条：`*密码*` 由 `_KB_PASSWORD_RE` 单独处理
# （`密码学` 是正常笔记，不能被锁）。单列成常量，避免在匹配循环里写字面量。
_KB_PASSWORD_PATTERN = "*密码*"

# 媒体扩展名黑名单（§5.3）：改名成 `.md` 也拦得住（魔数双判的第二重）。
KB_MEDIA_EXTS = frozenset({
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".png", ".jpg", ".jpeg", ".webp",
    ".gif", ".bmp", ".tif", ".tiff", ".svgz", ".mp3", ".wav", ".flac", ".m4a",
    ".ogg", ".zip", ".7z", ".rar", ".gz", ".tar", ".pdf", ".docx", ".xlsx",
    ".pptx", ".bin", ".exe", ".dll", ".so", ".dylib", ".iso",
})

# 首 8 KB 的魔数表（§5.3）：`(签名, 偏移, 名字)`。`RIFF….WEBP` 与 `ftyp` 要按偏移判。
KB_MAGIC_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", 0, "png"),
    (b"\xff\xd8\xff", 0, "jpeg"),
    (b"GIF8", 0, "gif"),
    (b"%PDF", 0, "pdf"),
    (b"PK\x03\x04", 0, "zip"),
    (b"7z\xbc\xaf\x27\x1c", 0, "7z"),
    (b"Rar!\x1a\x07", 0, "rar"),
    (b"ID3", 0, "mp3"),
    (b"fLaC", 0, "flac"),
    (b"OggS", 0, "ogg"),
    (b"\x7fELF", 0, "elf"),
    (b"MZ", 0, "exe"),
    (b"SQLite format 3\x00", 0, "sqlite"),
    (b"WEBP", 8, "webp"),        # RIFF....WEBP
    (b"ftyp", 4, "mp4"),         # ....ftyp
)

KB_DEFAULT_MAX_OPEN_BYTES = 2097152      # Q11：单文件 2 MiB（可按 vault 配）
KB_SEARCH_MAX_FILE_BYTES = 2097152       # 检索时单文件上限（超过跳过并计数）
KB_MAX_CONTENT_BYTES = 512 * 1024        # 单次写入 ≤ 512 KiB（§5.3）
KB_MAX_ITEMS_PER_DIR = 20                # tree 每个目录最多列 20 项（§4.1）
KB_TREE_MAX_DEPTH = 4                    # tree 深度上限（更深请显式给 path）
KB_MAX_LIMIT = 30                        # search 结果条数上限（§4.3）
KB_SAME_NAME_LIMIT = 10                  # create 时同名笔记最多报 10 条


def _kb_json_globs(raw) -> list:
    """`ignore_globs` 列（JSON 文本）→ list。坏 JSON 不静默生效，直接报 bad_usage。"""
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw]
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise KBError("bad_usage", f"ignore_globs 不是合法 JSON：{text[:60]!r}", detail=exc)
    if not isinstance(data, list):
        raise KBError("bad_usage", f"ignore_globs 必须是 JSON 数组，收到 {type(data).__name__}")
    return [str(x) for x in data]


_KB_VAULT_COLS = ("vault_id", "name", "path", "path_key", "mode", "ignore_globs",
                  "max_open_bytes", "is_default", "last_scan_at", "last_scan_files",
                  "last_scan_notes", "last_scan_bytes", "note", "status",
                  "created_at", "updated_at")


def _kb_vault_dict(row) -> dict:
    """`kb_vault` 行 → dict（`ignore_globs` 解析成 list；K1：永不返回 sqlite3.Row）。

    入参可以是 `sqlite3.Row`、按 `_KB_VAULT_COLS` 列序的 tuple，或已经归一过的 dict
    ——最后一种是为了让 `_kb_vault_norm` 复用同一段语义，不出现"两个实现各解释一次"。
    """
    if row is None:
        return None
    if isinstance(row, dict):
        data = dict(row)
    else:
        data = dict(zip(_KB_VAULT_COLS, row))
    data["ignore_globs"] = _kb_json_globs(data.get("ignore_globs"))
    data["is_default"] = bool(data.get("is_default"))
    return data


def _kb_vault_norm(row) -> dict:
    """**入口归一**：把"原始库行"变成 `kb_vault_get` 那种标准 dict（R4 · API 面）。

    ## 为什么必须有这一层

    `kb_tree` / `kb_open` / `kb_search` 的 `vault_row` 是**公开入口参数**：CLI 与 MCP
    走 `kb_vault_get`（已归一），但直接调库的代码（脚本、测试、将来的 MCP 变体）很可能
    把 `conn.execute(_KB_VAULT_SELECT).fetchone()` 原样塞进来。那时 `ignore_globs` 还是
    **JSON 字符串**，`_kb_ignored` 会把它当可迭代对象**逐字符**匹配——每个单字符都匹配
    不上任何路径，于是 `.obsidian/**`、`.trash/**` 全部失效：**不报错、只泄漏**
    （配置目录、回收站内容被一起列出来）。这是最难查的一类缺陷，所以入口先归一。

    ## 契约

    对"原始行"与"归一后的 dict"调用这三个入口，回执必须**逐字节相同**（除 `as_of`
    与扫描耗时这类时间戳字段）；忽略规则在两种入参下必须同样生效。

    ## 非法值为什么必须响亮失败

    `ignore_globs` 的库内形态是 **JSON 数组文本**。给它一个不是合法 JSON 数组的值
    （手写的 `.obsidian/**`、数字、嵌套对象）时 `_kb_json_globs` 抛 `bad_usage`——
    宁可这次调用失败，也不能让一个"看起来生效、实际什么都没挡"的忽略表上线。
    行对象本身不认识（字符串/整数/None）时同样 `bad_usage`，并把收到的类型说出来。
    """
    if row is None:
        raise KBError("vault_missing", "没有给知识库行（vault_row=None）",
                      hint="先用 `fish kb vault list` / `kb_vault_get` 取一条")
    if isinstance(row, dict):
        data = dict(row)
    elif isinstance(row, (list, tuple)):
        data = dict(zip(_KB_VAULT_COLS, row))
    else:
        try:                                # sqlite3.Row / 任何带 keys() 的 Mapping
            data = {key: row[key] for key in row.keys()}
        except (AttributeError, TypeError, IndexError, ValueError, KeyError) as exc:
            raise KBError("bad_usage", f"vault 行无法解析：{type(row).__name__}",
                          hint="传 kb_vault_get() 的结果，或 sqlite3.Row / dict 行",
                          detail=exc) from exc
    if not data:
        raise KBError("bad_usage", "vault 行是空的（0 列）",
                      hint="传 kb_vault_get() 的结果，或 sqlite3.Row / dict 行")
    # 与 `kb_vault_add` 同一套校验：空串规则也不许混进来（"看起来有规则、实际不匹配"）
    data["ignore_globs"] = _kb_validate_globs(_kb_json_globs(data.get("ignore_globs")))
    path = str(data.get("path") or "").strip()
    if not path:
        raise KBError("bad_usage",
                      f"vault 行缺少 path（vault_id={data.get('vault_id')!r}）",
                      hint="原始库行里 path 是必填列；不要传半截 dict")
    data["path"] = path
    name = str(data.get("name") or "").strip()
    data["name"] = name or Path(path).name or path
    mode = str(data.get("mode") or "ro")
    if mode not in ("ro", "rw"):
        raise KBError("bad_usage", f"vault 行的 mode 只能是 ro / rw，收到 {mode!r}")
    data["mode"] = mode
    try:
        limit = int(data.get("max_open_bytes") or KB_DEFAULT_MAX_OPEN_BYTES)
    except (TypeError, ValueError) as exc:
        raise KBError("bad_usage",
                      f"vault 行的 max_open_bytes 不是整数："
                      f"{data.get('max_open_bytes')!r}", detail=exc) from exc
    if limit <= 0:
        raise KBError("bad_usage", "vault 行的 max_open_bytes 必须大于 0")
    data["max_open_bytes"] = limit
    data["vault_id"] = str(data.get("vault_id") or "")
    data["is_default"] = bool(data.get("is_default"))
    status = str(data.get("status") or "active")
    if status != "active":
        raise KBError("vault_missing",
                      f"知识库已停用：{data['name']}（status={status}）",
                      hint="fish kb vault add 重新登记，或换一个库")
    data["status"] = status
    return data


_KB_VAULT_SELECT = ("SELECT vault_id, name, path, path_key, mode, ignore_globs, "
                    "max_open_bytes, is_default, last_scan_at, last_scan_files, "
                    "last_scan_notes, last_scan_bytes, note, status, created_at, "
                    "updated_at FROM kb_vault")


def _kb_validate_globs(globs) -> list:
    out = []
    for raw in globs:
        g = str(raw or "").strip()
        if not g:
            raise KBError("bad_usage", "忽略规则不能是空串")
        if g not in out:
            out.append(g)
    return out


def kb_vault_add(conn, *, name, path, mode="ro", ignore=(), is_default=False,
                 note=None, max_open_bytes=None) -> dict:
    """登记一个 vault（**不搬运任何文件**；§2.6 m5 / 裁决 Q9）。

    幂等口径（KB-1 的加强版）：按 `path_key = normcase(realpath(path))` 判重，不是按
    `path` 字符串。所以 `E:\\Obsidian\\X` 与 `e:\\obsidian\\x` 是同一个库——SQLite 的
    UNIQUE 是二进制比较，直接给 `path` 建唯一索引会在这里裂出两个 vault。

    重复 add 不新增行，只更新 `name/mode/ignore_globs/max_open_bytes/note/updated_at`
    （并 `status='active'`：一个被 remove 过的库重新 add 应当恢复可用，而不是报重复）。
    """
    if mode not in ("ro", "rw"):
        raise KBError("bad_usage", f"mode 只能是 ro / rw，收到 {mode!r}")
    clean_name = str(name or "").strip()
    if not clean_name:
        raise KBError("bad_usage", "vault 名字不能为空",
                      hint="--name <名字>，通常取根目录名")
    raw_path = str(path or "").strip()
    if not raw_path:
        raise KBError("bad_usage", "vault 路径不能为空", hint="--path <绝对路径>")
    p = Path(raw_path)
    if not p.is_absolute():
        raise KBError("bad_usage", f"vault 路径必须是绝对路径：{raw_path!r}",
                      hint="例如 --path \"E:\\Obsidian\\安逸知识库\"")
    if not p.exists():
        raise KBError("not_found", f"路径不存在：{raw_path}",
                      hint="先确认盘符/目录名；本命令只登记，不创建目录")
    if not p.is_dir():
        raise KBError("not_found", f"不是目录：{raw_path}")
    # 安全审计 🟡-4（2026-10-27）：落库存 **realpath**。否则 `…/v/../v` 这样的注册会让
    # 索引写出 `rel_path='../v/a.md'`——破坏"rel 是库内相对路径"这条下游不变量
    # （`kb open` 会拒绝 `..`，于是索引产出的键读接口不认）。
    real = os.path.realpath(str(p))

    key = os.path.normcase(os.path.realpath(str(p)))
    globs = _kb_validate_globs(list(KB_DEFAULT_IGNORE_GLOBS) + list(ignore or ()))
    try:
        max_open = int(max_open_bytes if max_open_bytes is not None
                       else KB_DEFAULT_MAX_OPEN_BYTES)
    except (TypeError, ValueError) as exc:
        raise KBError("bad_usage", f"max_open_bytes 必须是整数，收到 {max_open_bytes!r}",
                      detail=exc)
    if max_open <= 0:
        raise KBError("bad_usage", "max_open_bytes 必须大于 0")

    now = now_utc()
    row = conn.execute(f"{_KB_VAULT_SELECT} WHERE path_key=?",
                       (key,)).fetchone()
    if row:
        vault = _kb_vault_dict(row)
        other = conn.execute("SELECT vault_id FROM kb_vault WHERE name=? AND vault_id<>?",
                             (clean_name, vault["vault_id"])).fetchone()
        if other:
            raise KBError("exists", f"名字已被另一个 vault 占用：{clean_name}",
                          hint="换一个名字，或先 `fish kb vault remove` 掉那个库")
        conn.execute(
            "UPDATE kb_vault SET name=?, path=?, mode=?, ignore_globs=?, "
            "max_open_bytes=?, note=COALESCE(?, note), status='active', updated_at=? "
            "WHERE vault_id=?",
            (clean_name, real, mode, json.dumps(globs, ensure_ascii=False),
             max_open, note, now, vault["vault_id"]))
        vault_id = vault["vault_id"]
    else:
        other = conn.execute("SELECT vault_id FROM kb_vault WHERE name=?",
                             (clean_name,)).fetchone()
        if other:
            raise KBError("exists", f"名字已被另一个 vault 占用：{clean_name}",
                          hint="同一个库换个大小写重 add 是幂等的；不同库请换名字")
        vault_id = ulid()
        conn.execute(
            "INSERT INTO kb_vault (vault_id,name,path,path_key,mode,ignore_globs,"
            "max_open_bytes,is_default,note,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (vault_id, clean_name, real, key, mode,
             json.dumps(globs, ensure_ascii=False), max_open, 0, note, "active",
             now, now))
    if is_default:
        conn.execute("UPDATE kb_vault SET is_default=0 WHERE is_default=1 AND vault_id<>?",
                     (vault_id,))
        conn.execute("UPDATE kb_vault SET is_default=1 WHERE vault_id=?", (vault_id,))
    conn.commit()
    out = kb_vault_get(conn, vault_id)
    return out


def kb_vault_list(conn) -> list:
    """全部 active vault（默认库在前，其次按名字）。"""
    rows = conn.execute(f"{_KB_VAULT_SELECT} WHERE status='active' "
                        "ORDER BY is_default DESC, name").fetchall()
    return [_kb_vault_dict(r) for r in rows]


def kb_vault_get(conn, key=None) -> dict:
    """按 name / vault_id / 缺省（默认库）取一条。取不到 → `KBError('vault_missing')`。"""
    if key in (None, ""):
        row = conn.execute(f"{_KB_VAULT_SELECT} WHERE is_default=1 AND status='active'")
        row = row.fetchone()
        if row is None:
            raise KBError("vault_missing", "还没有默认知识库",
                          hint="fish kb vault add --name <名字> --path <绝对路径> --default")
        return _kb_vault_dict(row)
    text = str(key).strip()
    row = conn.execute(f"{_KB_VAULT_SELECT} WHERE (vault_id=? OR name=?) "
                       "ORDER BY is_default DESC LIMIT 1", (text, text)).fetchone()
    if row is None:
        raise KBError("vault_missing", f"没有这个知识库：{text}",
                      hint="fish kb vault list 看已登记的库")
    vault = _kb_vault_dict(row)
    if vault["status"] != "active":
        raise KBError("vault_missing", f"知识库已停用：{text}",
                      hint="fish kb vault add 重新登记，或换一个库")
    return vault


def kb_vault_set_default(conn, key) -> dict:
    vault = kb_vault_get(conn, key)
    conn.execute("UPDATE kb_vault SET is_default=0 WHERE is_default=1 AND vault_id<>?",
                 (vault["vault_id"],))
    conn.execute("UPDATE kb_vault SET is_default=1, updated_at=? WHERE vault_id=?",
                 (now_utc(), vault["vault_id"]))
    conn.commit()
    return kb_vault_get(conn, vault["vault_id"])


def kb_vault_set_mode(conn, key, mode) -> dict:
    if mode not in ("ro", "rw"):
        raise KBError("bad_usage", f"mode 只能是 ro / rw，收到 {mode!r}")
    vault = kb_vault_get(conn, key)
    conn.execute("UPDATE kb_vault SET mode=?, updated_at=? WHERE vault_id=?",
                 (mode, now_utc(), vault["vault_id"]))
    conn.commit()
    return kb_vault_get(conn, vault["vault_id"])


def kb_vault_remove(conn, key) -> dict:
    """只删注册行，**不动磁盘**；删后 `kb_access` 的审计仍可读（无 FK）。"""
    vault = kb_vault_get(conn, key)
    conn.execute("DELETE FROM kb_vault WHERE vault_id=?", (vault["vault_id"],))
    conn.commit()
    return {"removed": True, "vault_id": vault["vault_id"], "name": vault["name"],
            "path": vault["path"], "disk_untouched": True}


def kb_vault_scan(conn, key=None) -> dict:
    """walk + stat 汇总，写 `last_scan_*`（**不读正文**）。"""
    vault = kb_vault_get(conn, key)
    root = Path(vault["path"])
    if not root.is_dir():
        raise KBError("offline", f"知识库不在线：{root}",
                      hint="检查盘符/目录是否改名；offline 时读命令会回退到上次扫描数")
    t0 = time.perf_counter()
    files = notes = total_bytes = 0
    for ent in _kb_walk(vault, root):
        if ent["is_dir"]:
            continue
        files += 1
        total_bytes += int(ent["size"] or 0)
        if str(ent["rel"]).lower().endswith(".md"):
            notes += 1
    ms = int((time.perf_counter() - t0) * 1000)
    scanned_at = now_utc()
    conn.execute("UPDATE kb_vault SET last_scan_at=?, last_scan_files=?, "
                 "last_scan_notes=?, last_scan_bytes=?, updated_at=? WHERE vault_id=?",
                 (scanned_at, files, notes, total_bytes, scanned_at, vault["vault_id"]))
    conn.commit()
    _kb_log_quiet(conn, vault, rel="", action="scan", via="cli:kb",
                  meta={"scan_ms": ms, "scanned_files": files,
                        "scanned_bytes": total_bytes})
    return {"vault": {"name": vault["name"], "vault_id": vault["vault_id"],
                      "path": vault["path"], "mode": vault["mode"]},
            "files": files, "notes": notes, "bytes": total_bytes, "ms": ms,
            "scanned_at": scanned_at}


# ------------------------------------------------------------------ 路径校验（§5.1）


def _kb_effective_globs(globs) -> list:
    """**生效忽略规则** = 强制项 ∪ 行内规则（S2 · 2026-10-26 第四轮）。

    为什么在判定层合并而不是只靠 `migrate()` 回填：回填修的是"下次打开库时行里有没有"，
    而判定层合并修的是"**这次**调用有没有"——两者都要。库里那条行可能是旧格式、
    可能是别的分支写进来的、也可能在 `migrate()` 之后被人手工改掉；只要规则是
    "用户删不掉"的硬边界，就不能把生效性寄托在某一次迁移跑过。

    `globs` 允许是 JSON 文本（原始库行形态）：本函数在判定层的入口处，宁可多一次
    解析也不能让"字符串被当成可迭代对象逐字符匹配"的老坑复活。
    """
    if isinstance(globs, str):
        try:
            globs = _kb_json_globs(globs)
        except KBError:
            globs = []                  # 坏 JSON 由 `_kb_vault_norm` 响亮报错，这里不猜
    out = list(KB_MANDATORY_DENY)
    for raw in globs or ():
        text = str(raw or "")
        if text and text not in out:
            out.append(text)
    return out


def _kb_ignored(rel_posix, globs):
    """命中忽略规则 → 返回**命中的那条规则**（没命中 → None）。

    为什么要返回规则串而不是 bool：回执里要能解释"为什么这里少了一堆东西"
    （PRD §7.3 的硬要求）。匹配对象是"路径本身 + 每一级祖先目录（补 `/`）"，
    这样 `.trash/**` 能覆盖 `.trash/a/b.md`，`**/_media/**` 能覆盖任意深度。

    ## 2026-10-26 第二轮（R10 · 安全 F1）：先归一、再匹配

    Windows 是大小写不敏感、且会**吃掉组件尾随的点/空格**（`.obsidian ` 与 `.obsidian`
    是同一个目录）。原实现用 `fnmatchcase` 直接比原串，于是 `.OBSIDIAN/app.json`、
    `.obsidian /app.json` 都能绕过"禁读禁写 `.obsidian/**`"。
    修法：**只用于判定**地做逐段 `normcase` + `rstrip('. ')`（不改真实落点），
    规则串也 `normcase`——默认黑名单因此变成大小写不敏感。

    ## 2026-10-26 第四轮（S2）：强制项永远合并
    """
    raw = str(rel_posix or "").replace("\\", "/")
    rel = raw.strip("/")
    if not rel:
        return None
    parts = [_kb_judge_component(p) for p in rel.split("/") if p]
    if not parts:
        return None
    rel = "/".join(parts)
    # 同时试"文件形态"与"目录形态"：`.obsidian/**` 这类规则要能挡住**目录本身**
    # （否则 `kb open .obsidian/app.json` 会被拒，而 `kb open .obsidian` 却能过——
    #  同一条规则对同一棵树给出两种答案，是最难解释的那种不一致）。
    candidates = [rel, rel + "/"]
    for i in range(1, len(parts)):
        candidates.append("/".join(parts[:i]) + "/")
    for pattern in _kb_effective_globs(globs):
        pat = str(pattern or "")
        if not pat:
            continue
        # 规则串也逐段归一（R10）：`os.path.normcase` 对整串会把 `/` 换成 `\`，
        # 那样 `.obsidian/**` 就永远匹配不上——所以只在**段**上做大小写归一。
        pat_norm = "/".join(_kb_judge_component(p)
                            for p in pat.replace("\\", "/").split("/") if p)
        for cand in candidates:
            if fnmatch.fnmatchcase(cand, pat_norm) or fnmatch.fnmatchcase(rel, pat_norm):
                return pat
    return None


@functools.lru_cache(maxsize=8192)
def _kb_judge_component(part) -> str:
    """忽略判定的单段归一（R10）：大小写 + 尾随点/空格（只用于判定，不改落点）。

    ## 为什么加 `lru_cache`（2026-10-27 实测驱动）

    cProfile 显示它在**一次** `_kb_search_multi` 里被调 **29,972 次**，但**不同的
    `part` 只有 2,158 个** —— **重复率 92.8%**。最热的几个是常量：
    `'**'` 8,220 次、`'.obsidian'` 2,742 次、`'.trash'` 2,741 次、`'90-模板'` 2,740 次。

    根因：`_kb_ignored` 对**每个路径分量** × **每条 glob** 各调一次，而 glob 列表
    只有 3 条、路径分量大量重复（同一个目录名在千百个文件路径里反复出现）。

    **为什么安全**：本函数是**纯函数**——只读 `part` 一个字符串，调
    `os.path.normcase`（Windows 上是 `str.lower()`）与 `str.rstrip(". ")`，
    无 IO、无全局状态、无副作用。同一输入永远同一输出，缓存不改变任何行为。

    **为什么 maxsize=8192**：实测量级 2,158 个不同分量；留 4 倍余量吸收长路径的
    偶发分量，同时把内存钉在"几千个短字符串"这个量级。

    **为什么不缓存 `_kb_ignored` 本身**：实测它的 `rel` 参数 **2741 次调用对应
    2741 个不同值（重复率 0%）**——缓存它只会白占内存。热点在"分量级"，不在
    "整条路径级"。
    """
    return os.path.normcase(str(part or "").rstrip(". "))


# `密码` 的负向断言（S5）：`密码学笔记.md` 是正常笔记，不该因为"密码"两个字被锁。
_KB_PASSWORD_RE = re.compile("密码(?!学)")


def _kb_is_sensitive_name(rel) -> bool:
    """路径命中内置敏感名单（R15 / S5）→ True。**逐段**判，大小写不敏感。

    ## 2026-10-26 第四轮（S5 · 安全 N2）：从 basename 扩到"路径任一段"

    `凭证目录/账号清单.md`：目录名叫凭证、文件名平常——只判 basename 时它**完全不受
    保护**（默认可读、search 出片段）。反过来 `*密码*` 太宽：`20-学习/密码学笔记.md`
    被标敏感、正常笔记默认读不了。两条一起修：
      · 每一段（目录与文件名）都参与判定 → 目录名命中即敏感；
      · `密码` 用 `密码(?!学)`：`密码本/密码.md` 命中，`密码学…` 不命中。
    扩展名族（`.pem` / `.key` / `.env*` / `id_rsa*`）仍按"文件名"语义匹配——它们本来就
    是文件名形态的规则，误伤面为零；用 `fnmatch` 对每一段跑一遍不会引入新误判
    （目录名 `x.env` 本来也该敏感）。
    """
    parts = [p for p in str(rel or "").replace("\\", "/").split("/")
             if p and p not in (".", "..")]
    for part in parts:
        name = part.casefold()
        for pattern in KB_SENSITIVE_PATTERNS:
            if pattern == _KB_PASSWORD_PATTERN:
                continue                    # 交给负向断言（`密码学` 不算）
            if fnmatch.fnmatchcase(name, pattern.casefold()):
                return True
        if _KB_PASSWORD_RE.search(name):
            return True
    return False


# Windows 设备名（R7）：这些名字在任何目录下都被系统当成设备，不是文件。
_KB_DEVICE_NAMES = frozenset(
    ["con", "prn", "aux", "nul"]
    + [f"com{i}" for i in range(1, 10)]
    + [f"lpt{i}" for i in range(1, 10)]
)


def _kb_is_device_name(part) -> bool:
    """`CON` / `NUL` / `COM1` / `CON.md` 全都是设备名（Windows 只看主干名）。"""
    stem = str(part or "").split(".", 1)[0].strip().casefold()
    return stem in _KB_DEVICE_NAMES


def _kb_is_reparse_entry(entry) -> bool:
    """目录项是不是链接/联接点（R6）：符号链接 **或** reparse point。

    为什么必须在这里判：`kb_resolve_target` 只保护**显式给出的路径**；遍历面
    （tree/search）按名字走目录，junction 在 Windows 上 `is_dir(follow_symlinks=False)`
    仍为 True，不额外判就会顺着链接读到库外去——同一棵树两套边界。
    """
    try:
        if entry.is_symlink():
            return True
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return False
    return bool(getattr(st, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))



def _kb_first_reparse(real_root, target):
    """从 real_root 逐级走到 target，返回第一个 reparse point 的路径（没有 → None）。

    为什么逐级：只查叶子会漏掉"中间目录是 junction"的情形——那时叶子不是链接，
    但路径早就越界了。`st_file_attributes` 用 `getattr(...,0)` 兜底（非 Windows 没这属性），
    位用 `stat.FILE_ATTRIBUTE_REPARSE_POINT`（0x400），不硬编码魔数。
    """
    root = Path(real_root)
    target = Path(target)
    try:
        parts = target.relative_to(root).parts
    except ValueError:
        return str(target)                 # 不在根下：交给 commonpath 判（这里保守报断点）
    cur = root
    for part in parts:
        cur = cur / part
        try:
            st = os.lstat(cur)
        except OSError:
            return None                    # 不存在的段交给"存在性"那一步判
        attrs = getattr(st, "st_file_attributes", 0)
        if attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            return str(cur)
    return None


def kb_resolve_target(vault_row, rel, *, for_write=False) -> dict:
    """把"vault 内相对路径"解析成绝对路径，并在途中做全部 8 类拒绝（§5.1）。

    拒绝清单（判据认 `code`，不认文案）：`..` / 绝对 / 盘符 / UNC / `\\?\\` / `/etc` /
    空（写路径）/ junction 子路径 / 忽略目录 / 不存在。

    为什么用 `realpath + commonpath` 而不是 `str.startswith`：`E:\\Obsidian\\安逸知识库-evil`
    会被 `startswith(根)` 判成"在库里"——这是**假前缀**漏洞（裁决 §四.7 明确禁止）。
    `commonpath` 按**路径段**比，`\\` 与 `/`、大小写、junction 别名都先归一。
    """
    root = Path(vault_row["path"])
    globs = vault_row.get("ignore_globs") or []
    raw_text = "" if rel is None else str(rel)
    # ⓪ NUL 字符（R7 · 安全 F7）：`os.path.realpath`/`Path` 会抛裸 ValueError，
    #    在 CLI 上变成 traceback、在 MCP 上丢掉契约里的错误码，所以在这里先判。
    if "\x00" in raw_text:
        raise KBError("path_invalid", "路径含 NUL 字符（\\x00），拒绝处理",
                      hint="路径里不可能有 NUL；检查调用方是不是把二进制/拼接串当路径了")
    raw = raw_text.strip()
    if raw == "":
        if for_write:
            raise KBError("path_invalid", "写路径不能为空", hint="给 vault 内相对路径，如 20-PPT/x.md")
        return {"ok": True, "abs": Path(os.path.realpath(str(root))), "rel": "",
                "real_root": Path(os.path.realpath(str(root))), "ignored": False,
                "resolved_rel": "", "sensitive": False}

    # ① 只接受相对路径：绝对 / 盘符 / UNC / 设备路径各自点名拒绝（文案不同，code 相同）
    if raw.startswith(("/", "\\")):
        if raw.startswith("\\\\?\\") or raw.startswith("\\\\.\\"):
            raise KBError("path_invalid", f"不接受设备路径（\\\\?\\ …）：{raw}",
                          hint="给 vault 内相对路径，用 / 分隔")
        if raw.startswith("\\\\") or raw.startswith("//"):
            raise KBError("path_invalid", f"不接受 UNC 路径：{raw}",
                          hint="给 vault 内相对路径，用 / 分隔")
        raise KBError("path_invalid", f"不接受绝对路径：{raw}",
                      hint="给 vault 内相对路径，用 / 分隔")
    if ntpath.splitdrive(raw)[0]:
        raise KBError("path_invalid", f"不接受盘符：{raw}",
                      hint="给 vault 内相对路径，如 20-PPT/x.md")

    # ② 归一 + 组件检查（`..` 必须先拒：realpath 会把穿越后的路径算成"合法"）
    rel_posix = raw.replace("\\", "/")
    parts = [p for p in rel_posix.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise KBError("path_invalid", f"路径含 `..` 穿越：{raw}",
                      hint="vault 内相对路径不允许向上跳")
    # ②' 组件级拒绝（R7）：设备名（CON/NUL/COM1…）与 ADS（`note.md:hidden`）。
    #    Windows 会把它们当设备/备用数据流打开——真实落点根本不在 vault 里。
    for part in parts:
        if ":" in part:
            raise KBError("path_invalid",
                          f"路径含冒号（Windows 备用数据流 ADS）：{part}",
                          hint="文件名里不能有 `:`；ADS 形态（x.md:hidden）一律拒绝")
        if _kb_is_device_name(part):
            raise KBError("path_invalid", f"拒绝 Windows 设备名：{part}",
                          hint="CON/NUL/COM1 这类名字指向设备，不是库里的文件")
    rel_posix = "/".join(parts)
    if not rel_posix:
        raise KBError("path_invalid", f"路径归一后为空：{raw!r}")

    # ③ 忽略规则（组件级 + glob；显式给忽略目录也拒——它不可写也不该被读）
    hit = _kb_ignored(rel_posix, globs)
    if hit:
        raise KBError("ignored", f"该路径按约定忽略：{rel_posix}（规则 {hit}）",
                      hint="换个路径；忽略规则用 `fish kb vault add --ignore` 配置")

    # ④ realpath + commonpath（**不用 startswith**）
    try:
        real_root = Path(os.path.realpath(str(root)))
        # `lexical` 保留"未解析"的路径（含 junction 段），`target` 才是真实落点。
        # 为什么两个都要：commonpath 必须比**真实**路径（否则 junction 逃逸抓不到），
        # 而 reparse 检查必须走**未解析**路径——`realpath` 会把 junction 换成目标路径，
        # 之后再 lstat 永远看不到 reparse 位（设计 §5.1 的伪代码在这一点上是错的，
        # 照抄会得到一个永远返回 None 的检查；偏离记录见 docs/偏离记录-*.md）。
        lexical = real_root.joinpath(*parts)
        target = Path(os.path.realpath(str(lexical)))
    except (OSError, ValueError) as exc:
        raise KBError("path_invalid", f"路径无法解析：{rel_posix}",
                      hint="检查文件名是否合法（长度/保留字/非法字符）",
                      detail=exc)
    try:
        common = os.path.commonpath([str(real_root), str(target)])
        inside = os.path.normcase(common) == os.path.normcase(str(real_root))
    except ValueError:                     # 不同盘符 → 不可能是子路径
        inside = False
    if not inside:
        raise KBError("outside_vault", f"越出知识库根：{rel_posix}",
                      hint="只允许 vault 内的相对路径")

    # ④' **解析后路径**复判（S1 · 安全 N1 · 2026-10-26 第四轮）
    #
    # 忽略与敏感两项原本只判在**字面串**上，而真正打开/写入的是 realpath 之后的落点。
    # 两者在 Windows 上可以不是同一个名字：8.3 短名（`CREDEN~1.MD` → `credentials-prod.md`）、
    # vault 内 junction（`alias` → `.obsidian`）、大小写与尾随空格变体。于是
    # `kb open alias/app.json` 能读出 Obsidian 配置、`kb create OBSIDI~1/x.md` 能往配置
    # 目录写文件——**F1/R10 的承诺在别名形态下第二次失效**。
    #
    # 修法：realpath 之后算 `resolved_rel`，对**它**再跑一次忽略判定；敏感命中也算在
    # `resolved_rel` 上（`sensitive` 随返回值给调用方，读面按二次授权放行、写面直接拒）。
    # 正常路径下 `resolved_rel == rel_posix`（大小写可能不同），不会误伤——这一点由
    # `test_normal_paths_are_not_over_rejected` 钉住。
    #
    # 为什么忽略判定放在 reparse 检查**之前**：junction 是"名字不同、落点相同"的另一种
    # 形态，`alias -> .obsidian` 必须回答 `ignored`（与原名同一条规则、同一个错误码），
    # 而不是换一个"这里有个链接"的码——同一棵树两套答案正是本轮要收口的东西。
    try:
        resolved_rel = target.relative_to(real_root).as_posix()
    except ValueError:                     # 老 Python 的 relative_to 对大小写敏感
        resolved_rel = os.path.relpath(str(target), str(real_root)).replace("\\", "/")
    if resolved_rel in (".", ""):
        resolved_rel = ""
    hit = _kb_ignored(resolved_rel, globs)
    if hit:
        raise KBError("ignored",
                      f"该路径解析后落在忽略目录：{rel_posix}"
                      + (f"（实际落点 {resolved_rel}）" if resolved_rel != rel_posix else "")
                      + f"（规则 {hit}）",
                      hint="换个路径；忽略规则（含内置强制项）对解析后的真实落点同样生效")
    sensitive = (_kb_is_sensitive_name(resolved_rel)
                 or _kb_is_sensitive_name(rel_posix))

    # ⑤ reparse point 逐级检查（junction / symlink 都会把路径带到别处）
    bad = _kb_first_reparse(real_root, lexical)
    if bad:
        raise KBError("reparse_point", f"路径含链接/联接点：{bad}（已拒绝）",
                      hint="Obsidian vault 内不应有 junction；换成真实目录")

    # ⑥ 存在性：读路径必须存在；写路径允许"待创建"，但父目录链必须已存在且在库内
    if not target.exists():
        if not for_write:
            raise KBError("not_found", f"知识库里没有：{rel_posix}",
                          hint="fish kb tree 看目录，或用完整相对路径")
        parent = lexical.parent
        if parent != real_root and not parent.exists():
            raise KBError("not_found", f"父目录不存在：{parent}",
                          hint="P0 只写已存在目录内（不自动建目录）")
        bad = _kb_first_reparse(real_root, parent)
        if bad:
            raise KBError("reparse_point", f"父目录链含链接/联接点：{bad}（已拒绝）")

    return {"ok": True, "abs": target, "rel": rel_posix, "real_root": real_root,
            "ignored": False, "resolved_rel": resolved_rel, "sensitive": sensitive}


# ------------------------------------------------------------------ 类型 / 编码（§5.3）


def kb_detect_text(data: bytes, *, ext="") -> dict:
    """判"这是不是可以读的文本"（扩展名 + 首 8 KB 魔数双判）。

    返回 `{"is_text","encoding","media","reason"}`：
      · 媒体扩展名 / 魔数 / 出现 `\\x00` / 非可打印控制字符 > 5% → `media=True`
      · 解码依次试 utf-8 → utf-8-sig → gb18030（严格），全失败 → `bad_encoding`
    为什么要双判：`.png` 改名成 `.md` 仍要被识别（KB-12）。为什么不 `errors="replace"`：
    宁可拒绝，也不悄悄给一份乱码——读错的内容比读不到更危险。
    """
    suffix = str(ext or "").lower()
    if suffix and suffix in KB_MEDIA_EXTS:
        return {"is_text": False, "encoding": None, "media": True,
                "reason": f"extension:{suffix}"}
    head = data[:8192] if data else b""
    for sig, off, name in KB_MAGIC_SIGNATURES:
        if head[off:off + len(sig)] == sig:
            return {"is_text": False, "encoding": None, "media": True,
                    "reason": f"magic:{name}"}
    if b"\x00" in head:
        return {"is_text": False, "encoding": None, "media": True,
                "reason": "nul_byte"}
    encoding = None
    try:
        text = data.decode("utf-8")
        encoding = "utf-8-sig" if text.startswith("\ufeff") else "utf-8"
    except UnicodeDecodeError:
        try:
            data.decode("utf-8-sig")
            encoding = "utf-8-sig"
        except UnicodeDecodeError:
            try:
                data.decode("gb18030")
                encoding = "gb18030"
            except UnicodeDecodeError:
                encoding = None
    if encoding is None:
        return {"is_text": False, "encoding": None, "media": False,
                "reason": "bad_encoding"}
    sample = data[:8192].decode(encoding, errors="ignore")
    controls = sum(1 for ch in sample
                   if ord(ch) < 32 and ch not in ("\t", "\n", "\r"))
    if sample and controls / len(sample) > 0.05:
        return {"is_text": False, "encoding": None, "media": True,
                "reason": "binary_ratio"}
    return {"is_text": True, "encoding": encoding, "media": False, "reason": None}


def kb_read_text(vault_row, rel, *, max_bytes=None) -> dict:
    """读一个文件并解码（`kb open` 的底座）：`{data,text,encoding,sha256,bytes}`。"""
    limit = int(max_bytes if max_bytes is not None
                else (vault_row.get("max_open_bytes") or KB_DEFAULT_MAX_OPEN_BYTES))
    target = kb_resolve_target(vault_row, rel)
    abs_path = target["abs"]
    size = abs_path.stat().st_size
    if size > limit:
        raise KBError("too_large",
                      f"文件 {size} B 超过单文件上限 {limit} B",
                      hint="长文档请分段：kb open <path> --max-chars，或直接用文件工具读",
                      detail={"bytes": size, "limit": limit})
    raw = abs_path.read_bytes()
    detected = kb_detect_text(raw, ext=abs_path.suffix)
    if detected["media"]:
        raise KBError("media", f"这是媒体/二进制文件（{detected['reason']}），不按文本读",
                      hint="用对应的查看器打开；知识库只读文本")
    if not detected["is_text"]:
        raise KBError("bad_encoding", "不是可识别的文本编码（UTF-8 / GB18030 都失败）",
                      hint="另存为 UTF-8 再读")
    return {"data": raw, "text": raw.decode(detected["encoding"]),
            "encoding": detected["encoding"], "sha256": kb_sha256_bytes(raw),
            "bytes": len(raw)}


# ------------------------------------------------------------------ 遍历（只 stat 不 open）


def _kb_walk(vault_row, start_abs):
    """从 `start_abs` 起遍历 vault（跳过忽略项），产出 `{rel,abs,is_dir,size,mtime}`。

    **只 `os.scandir` + `stat`，绝不 `open()`**——这是 KB-2 判据
    （`test_tree_never_opens_files` 用 monkeypatch 数 `builtins.open`）的落点。
    被忽略的目录不下降（少走路，也让 tree 的"生效忽略规则"与结果自洽）。
    """
    root = Path(os.path.realpath(str(vault_row["path"])))
    globs = vault_row.get("ignore_globs") or []
    start = Path(start_abs)
    stack = [start]
    while stack:
        cur = stack.pop()
        try:
            entries = sorted(os.scandir(cur), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            abs_path = Path(entry.path)
            try:
                rel = abs_path.relative_to(root).as_posix()
            except ValueError:
                continue
            # 链接/联接点一律**不列、不下降**（R6 · 安全 F1/F12 的遍历面）：
            # 它指向哪里由显式路径的 reparse 检查负责，遍历面不替它做决定。
            if _kb_is_reparse_entry(entry):
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            # 目录用带尾斜杠的形态再判一次：`.obsidian/**` 不该只挡住里面的文件，
            # 还要把 `.obsidian/` 这个**目录本身**挡在外面（否则 tree 会列出空目录节点）。
            if _kb_ignored(rel + "/" if is_dir else rel, globs):
                continue
            size = 0
            mtime = 0.0
            if not is_dir:
                try:
                    st = entry.stat(follow_symlinks=False)
                    size, mtime = int(st.st_size), float(st.st_mtime)
                except OSError:
                    size, mtime = 0, 0.0
            yield {"rel": rel, "abs": abs_path, "is_dir": is_dir,
                   "size": size, "mtime": mtime}
            if is_dir:
                stack.append(abs_path)


def _kb_fmt_mtime(ts) -> str:
    if not ts:
        return "—"
    try:
        dt = datetime.datetime.fromtimestamp(float(ts)).astimezone()
    except (ValueError, OSError, OverflowError):
        return "—"
    return iso_local(dt)


def _kb_parse_budget(name, value) -> int:
    """预算参数（`max_chars` / `max_tokens`）→ int（S9 · 评审 🟡-N6）。

    `0 / None / ""` = 该上限不启用。**非整数一律 `KBError('bad_usage')`**：原实现
    在 `_kb_fit_text` 里直接 `int(...)`，程序化/MCP 面传 `"x"` 会漏出裸 `ValueError`
    （CLI 有 `type=int` 挡着，所以这是"只在程序面看得见"的契约缺口）。
    """
    if value in (None, "", 0, False):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise KBError("bad_usage", f"{name} 必须是整数，收到 {value!r}", detail=exc)


def _kb_fit_text(lines, *, max_chars, max_tokens) -> dict:
    """按**双上限**截断（K7：两个都要成立），并说明截断原因。

    顺序：先字符、后 token。中文场景 token 才是实际约束（1200 tokens ≈ 800 汉字），
    所以 token 那一步必须**留出截断标记本身的 token**，否则"加了省略号又超限"。
    """
    chars_cap = _kb_parse_budget("max_chars", max_chars)
    token_cap = _kb_parse_budget("max_tokens", max_tokens)
    text = "\n".join(lines)
    reasons = []
    if chars_cap and len(text) > chars_cap:
        text = text[:max(chars_cap - 20, 0)] + "\n…（已按字符预算截断）"
        reasons.append("chars")
    if token_cap:
        marker = "\n…（已截断）"
        if count_tokens(text) > token_cap:
            budget = token_cap - count_tokens(marker)
            if budget <= 0:
                # 预算比截断标记本身还小：什么都不放，否则"加了标记又超限"
                text = ""
            else:
                kept = text.split("\n")
                while kept and count_tokens("\n".join(kept)) > budget:
                    kept.pop()
                text = ("\n".join(kept) + marker) if kept else ""
            reasons.append("tokens")
    if chars_cap and len(text) > chars_cap:     # 兜底：两个上限同时成立
        text = text[:chars_cap]
    return {"text": text, "chars": len(text), "tokens": count_tokens(text),
            "truncated": bool(reasons), "truncated_reasons": reasons}


_KB_FIT_MARKER_RE = re.compile(r"\n?…（(?:已按字符预算截断|已截断)）\s*$")


def _kb_within_budget(text, *, max_chars, max_tokens) -> bool:
    """两个上限**同时**成立（`0/None` = 该上限不启用）。"""
    chars_cap = _kb_parse_budget("max_chars", max_chars)
    token_cap = _kb_parse_budget("max_tokens", max_tokens)
    if chars_cap and len(text) > chars_cap:
        return False
    if token_cap and count_tokens(text) > token_cap:
        return False
    return True


def _kb_fit_text_with_tail(lines, tail, *, max_chars, max_tokens, short_tail="") -> dict:
    """按双预算截断，并把 `tail` **钉在最后一行**（R4 · 截断后的可读性）。

    为什么不直接 `_kb_fit_text(lines + [tail])`：常规截断按行/按字符投，最后一行最容易
    被切掉——"截断了就告诉你怎么深入"于是成了一句空话。这里反过来做：先常规截断拿到
    正文，再把 `tail` 贴上去；只要任一上限被顶破，就**从尾部一行一行撤正文**，直到
    `正文 + tail` 同时满足 `chars ≤ max_chars` 与 `tokens ≤ max_tokens`。

    正文撤空还放不下时换 `short_tail`（短一句），再不行就缩到空串：宁可这次只回一句
    "已截断、加大预算"，也不超限、也不假装截断没发生。
    """
    base = _kb_fit_text(lines, max_chars=max_chars, max_tokens=max_tokens)
    tail = str(tail or "").strip()
    if not tail or not base["truncated"]:
        return base
    short = str(short_tail or "").strip()

    def assemble(body, sentence):
        return f"{body}\n{sentence}" if body else sentence

    text = _KB_FIT_MARKER_RE.sub("", str(base["text"] or ""))
    sentence = tail
    while not _kb_within_budget(assemble(text, sentence),
                                max_chars=max_chars, max_tokens=max_tokens):
        kept = text.split("\n") if text else []
        if kept:
            text = "\n".join(kept[:-1])
            continue
        if short and sentence != short:
            sentence = short
            continue
        sentence = _kb_fit_text([short or tail], max_chars=max_chars,
                                max_tokens=max_tokens)["text"]
        break
    candidate = assemble(text, sentence)
    return {"text": candidate, "chars": len(candidate),
            "tokens": count_tokens(candidate), "truncated": True,
            "truncated_reasons": list(base["truncated_reasons"])}


# ------------------------------------------------------------------ 审计（§5.6）


def _kb_meta_json(meta) -> str:
    """`meta` 白名单过滤 + 1 KiB 上限（超出尾部丢弃并标 `meta_truncated`）。"""
    if not meta:
        return None
    clean = {}
    for key, value in dict(meta).items():
        if key not in KB_META_KEYS:
            continue
        if isinstance(value, (list, tuple)):
            clean[key] = [str(v)[:80] for v in list(value)[:8]]
        elif isinstance(value, bool) or value is None:
            clean[key] = value
        elif isinstance(value, (int, float)):
            clean[key] = value
        else:
            clean[key] = str(value)[:200]
    encoded = json.dumps(clean, ensure_ascii=False)
    if len(encoded.encode("utf-8")) <= 1024:
        return encoded
    for key in list(clean)[::-1]:
        clean.pop(key, None)
        clean["meta_truncated"] = True
        encoded = json.dumps(clean, ensure_ascii=False)
        if len(encoded.encode("utf-8")) <= 1024:
            return encoded
    return json.dumps({"meta_truncated": True}, ensure_ascii=False)


def kb_log_access(conn, vault_row, *, rel, action, result="ok", citizen_id=None,
                  account_id=None, via="cli:kb", bytes_=None, sha256=None,
                  backup_path=None, meta=None) -> int:
    """写一条 `kb_access` 流水，返回主键 id（写入路径的 `write_id` 前缀来自它）。

    词表校验放 Python 层（库层不设 CHECK）：非法值 → `KBError('bad_action')`。
    `citizen_id=None → 'unattributed'` **只在这里做一次**（K3）；重复做会让
    "未归属"与"空串"两种语义混起来。
    **本函数不吞异常**（K4）：容错责任在调用方，才能被单独验证。
    """
    if action not in KB_ACTIONS:
        raise KBError("bad_action", f"未知的 kb_access.action：{action!r}")
    if result not in KB_RESULTS:
        raise KBError("bad_action", f"未知的 kb_access.result：{result!r}")
    if via not in KB_VIAS:
        raise KBError("bad_usage", f"未知的 via：{via!r}（可用：{', '.join(KB_VIAS)}）")
    vault_id = str((vault_row or {}).get("vault_id") or "")
    cur = conn.execute(
        "INSERT INTO kb_access (vault_id,path,citizen_id,account_id,action,result,via,"
        "ts,bytes,sha256,backup_path,meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (vault_id, str(rel or ""), citizen_id or KB_UNATTRIBUTED, account_id,
         action, result, via, now_utc(),
         None if bytes_ is None else int(bytes_), sha256, backup_path,
         _kb_meta_json(meta)))
    conn.commit()
    return int(cur.lastrowid)


def _kb_log_quiet(conn, vault_row, *, rel, action, result="ok", **kw):
    """审计的**容错包装**（K4）：写不进不影响主流程，但也不假装写过（返回 None）。"""
    try:
        return kb_log_access(conn, vault_row, rel=rel, action=action, result=result, **kw)
    except (sqlite3.Error, KBError, KeyError):
        return None


def _kb_reject(conn, vault_row, rel, exc, *, citizen_id=None, via="cli:kb",
               extra_meta=None):
    """把一次拒绝写进流水（`action='reject'`）。`exc` 是 `KBError`。

    R18（2026-10-26 第二轮）：`meta.reason_code` 存**错误码**，`reason` 只存截断中文——
    否则"最常见的拒绝原因"只能靠正则猜中文（评审 🟡-3 抓到文档与实现不符）。
    """
    result = exc.code if exc.code in KB_RESULTS else "denied"
    meta = {"reason_code": getattr(exc, "code", None),
            "reason": (getattr(exc, "message", "") or "")[:160]}
    if extra_meta:
        meta.update(dict(extra_meta))
    _kb_log_quiet(conn, vault_row, rel=rel or "", action="reject", result=result,
                  citizen_id=citizen_id, via=via, meta=meta)


def _kb_guard(conn, vault_row, rel, fn, *, citizen_id=None, via="cli:kb",
              audit_rel=None, audit_meta=None):
    """执行一次读/写动作；`KBError`（以及漏网的 `OSError`）先记 reject 再抛。

    ## 两个参数为什么要分开（R1 · 评审 🔴-1 / 安全 F2）

    `rel` 是"给人看的位置"，`audit_rel` 才是"要落库的路径"。原实现让 `kb_search`
    把 **query** 从 `rel` 这个通道借过去，于是一旦失败，检索词原文就被写进
    `kb_access.path`（只增不删的审计表 = 永久搜索历史）。分开之后：
    搜索的审计路径恒为空串，query 只以 `meta.query_hash` 存在。

    ## 为什么还要捕 `OSError`（R7 · 安全 F7）

    原实现只捕 `KBError`，于是"目标文件读不了 / 路径里有 `\\x00` / 设备形态"
    这类 OS 层异常**既不落审计、也没有错误码**（CLI 直接吐 traceback）。
    这里兜一层：转成域错误 + 记 reject，然后再抛。
    """
    path_for_audit = rel if audit_rel is None else audit_rel
    try:
        return fn()
    except KBError as exc:
        _kb_reject(conn, vault_row, path_for_audit, exc, citizen_id=citizen_id,
                   via=via, extra_meta=audit_meta)
        raise
    except OSError as exc:
        code = "not_writable" if isinstance(exc, PermissionError) else "path_invalid"
        err = KBError(code, f"文件系统错误：{exc.strerror or exc}",
                      hint="检查路径是否存在、是否被占用/无权限；库外/设备路径一律拒绝",
                      detail=exc)
        _kb_reject(conn, vault_row, path_for_audit, err, citizen_id=citizen_id,
                   via=via, extra_meta=audit_meta)
        raise err from exc


# ------------------------------------------------------------------ tree（§4.1）


def _kb_tree_dir(vault_row, abs_dir, *, level, max_depth, globs):
    """**统计层**：递归汇总一个目录 → `{rel,notes,attachments,files,bytes,mtime,
    children,more,dirs,hidden_dirs,partial}`。

    本函数只负责"数得对、扫得省"，**不决定显示成什么样**——渲染口径的唯一权威在
    `_kb_tree_lines`（它拿 `depth` 独立裁剪层级）。两层分开的理由见 `_kb_tree_lines`
    的 docstring（评审 🟠-2 的成因就是"统计深度"与"显示深度"共用一个开关）。

    口径：
      · `level` = 本目录相对起点的层数（起点 = 0）；
      · `level == max_depth` 的目录**仍然扫**（要列出它自己的直接文件），但**不再下钻**
        ——于是 `depth=1` 的 `scandir` 次数 = 起点 + 一级目录数（成本判据 R3）；
      · 每目录子项按 `mtime DESC, rel ASC` 排序后截到 `KB_MAX_ITEMS_PER_DIR` 项，
        余下记 `more=N`（确定性 + 预算可控）；
      · `hidden_dirs` = 本轮**没**下钻的目录数（第 `max_depth+1` 层），`partial` 随之；
        绝不拿一个"看起来完整"的合计冒充全树统计（安全审计 F6 就是这个形态）。

    ## 链接（R6）

    目录项若是符号链接/junction → **不列、不下降**：遍历面不替它决定落点；
    显式路径的 `reparse_point` 仍由 `kb_resolve_target` 负责（两套边界各守一段）。
    """
    root = Path(os.path.realpath(str(vault_row["path"])))
    try:
        entries = sorted(os.scandir(abs_dir), key=lambda e: e.name)
    except OSError:
        entries = []
    notes = attachments = files = total_bytes = 0
    mtime = 0.0
    children = []
    subdirs = []
    hidden_dirs = 0
    for entry in entries:
        abs_path = Path(entry.path)
        try:
            rel = abs_path.relative_to(root).as_posix()
        except ValueError:
            continue
        if _kb_is_reparse_entry(entry):
            continue
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
            is_dir = False
        if _kb_ignored(rel + "/" if is_dir else rel, globs):
            continue
        item = {"rel": rel, "name": entry.name, "is_dir": is_dir}
        if is_dir:
            if level >= max_depth:
                # 超深处只汇总：记下"这里还有目录"，**不再 scandir**
                hidden_dirs += 1
                item.update({"notes": None, "attachments": None, "bytes": None,
                             "mtime": None, "not_scanned": True})
            else:
                child = _kb_tree_dir(vault_row, abs_path, level=level + 1,
                                     max_depth=max_depth, globs=globs)
                notes += child["notes"]
                attachments += child["attachments"]
                files += child["files"]
                total_bytes += child["bytes"]
                hidden_dirs += child["hidden_dirs"]
                mtime = max(mtime, child["mtime"])
                item.update({"notes": child["notes"],
                             "attachments": child["attachments"],
                             "bytes": child["bytes"], "mtime": child["mtime"],
                             "not_scanned": False})
                subdirs.append(child)
        else:
            try:
                st = entry.stat(follow_symlinks=False)
                size, m = int(st.st_size), float(st.st_mtime)
            except OSError:
                size, m = 0, 0.0
            files += 1
            total_bytes += size
            mtime = max(mtime, m)
            if entry.name.lower().endswith(".md"):
                notes += 1
            else:
                attachments += 1
            item.update({"notes": 0, "attachments": 0 if entry.name.lower().endswith(".md") else 1,
                         "bytes": size, "mtime": m})
        children.append(item)
    children.sort(key=lambda it: (-float(it.get("mtime") or 0), it["rel"]))
    # 子目录也按 rel 排：渲染顺序不能依赖 `os.scandir` 的返回序（那是各平台各异的）。
    subdirs.sort(key=lambda it: it["rel"])
    return {"rel": abs_dir.relative_to(root).as_posix() if abs_dir != root else "",
            "notes": notes, "attachments": attachments, "files": files,
            "bytes": total_bytes, "hidden_dirs": hidden_dirs,
            "partial": hidden_dirs > 0,
            "mtime": mtime, "children": children[:KB_MAX_ITEMS_PER_DIR],
            "more": max(0, len(children) - KB_MAX_ITEMS_PER_DIR), "dirs": subdirs}


_KB_TREE_DEPTH_RULE = (
    "> 深度口径（唯一权威）：depth=N = **显示到第 N 层目录**，起点为第 0 层；"
    "每个被显示的目录都列出它的直接 `.md` 文件"
    "（相对库根的完整路径，可直接喂给 `kb open`）")


def _kb_tree_lines(vault, start_rel, tree, *, depth, as_of, ignore_globs) -> list:
    """**显示层**：把统计树渲染成文本——`depth` 的唯一语义定义在这里（R4）。

    ## 判据口径（本函数就是权威，测试与实现以它为准）

    `depth` = **显示的最大目录层级**，起点（`path` 给的目录，缺省=库根）为**第 0 层**：

      · `depth=1` → 起点 + 起点的**一级子目录** + 这两类目录**各自的直接文件**；
        一级子目录的子目录**不显示**；
      · `depth=2` → 再加上二级子目录及其直接文件；依此类推（上限
        `KB_TREE_MAX_DEPTH`）；
      · **每个被显示的目录都列出它的直接 `.md` 文件**（§7.3：附件只计数不列名），
        每目录最多 `KB_MAX_ITEMS_PER_DIR` 项，超出记 `more=N`；
      · 文本随 `depth` 增大**单调不减**（预算内不截断）：显示集合是**前缀闭包**
        ——显示第 L 层必然显示 0..L-1 层，且既有行只可能变长（层级更深的计数更大），
        不会因为 depth 变化而整段消失。

    ## 上一版为什么违反单调性（评审 🟠-2 的真实成因）

    旧实现既当统计器又当渲染器：
      ① 每个节点都挂一行"…还有 N 个下级目录未展开"，depth 越过这些目录后**整行消失**；
      ② 全局那段"深层未展开"提示在 `hidden_dirs == 0` 时也不见了。
    于是一次 `depth 2 → 3` 会同时删掉 3 行说明，输出**比 depth=2 更短**（671 → 580 字）。
    现在这些信息改由**成本恒定**的头部两行 + JSON 的 `scan.hidden_dirs/partial` 承载：
    说明不随 depth 时有时无，单调性就不再依赖"恰好内容比提示长"。

    ## 为什么要 `level >= max_level` 这道闸

    显示层独立持有 depth，**不信任统计层裁到什么深度**：将来若把统计扩到全树（"统计全树、
    显示按 depth"），渲染这一侧也不会跟着泄漏。这是"统计"与"显示"分开的落点。
    """
    max_level = max(0, int(depth))
    lines = [
        f"# 知识库目录 · {vault['name']}",
        "",
        f"> source=知识库（未建索引）｜as_of {iso_local(as_of)}",
        f"> vault=`{vault['path']}`（mode={vault['mode']}）｜起点=`{start_rel or '/'}`"
        f"｜深度={depth}",
        f"> 生效忽略规则：{', '.join(ignore_globs) if ignore_globs else '（无）'}",
        _KB_TREE_DEPTH_RULE,
        f"> 统计面：数到第 {depth} 层（更深的目录不显示、也不计入合计）；"
        "只统计不读正文：笔记 = .md 计数，附件 = 其余文件计数",
        f"> 每目录最多列 {KB_MAX_ITEMS_PER_DIR} 项（超出记 more=N）；"
        "更深请加大 --depth（上限 4），或给 path=<目录> 直接进入",
        "",
    ]

    def emit(node, level):
        pad = "  " * level
        # 根节点用 **vault 名**（R3：原实现 `node["rel"] or "/"` 再拼 `/`，渲染成 `//`）
        name = node["rel"] or vault["name"]
        # S3（第四轮 · 评审 🟠-N3 / PRD §7.2 KB-2）：目录行必须带"最近修改"——
        # 取该目录**子树内文件**的 max mtime（空目录 / 未扫描 → `—`）。数据早在
        # `_kb_tree_dir` 里算好（`mtime`），这里只是把它渲染出来；不新增遍历成本。
        lines.append(f"{pad}- {name}/  {node['notes']} 篇笔记 / "
                     f"{node['attachments']} 附件 / {node['bytes']} B"
                     f" / 最近修改={_kb_fmt_mtime(node.get('mtime'))}")
        for child in node["children"]:
            if child["is_dir"]:
                continue
            # §7.3 / R8：附件**只计数、不列名**（几百个图名会把预算吃光，而"有几张图"
            # 这个信息已经写在目录汇总行里了）。
            if not str(child["name"]).lower().endswith(".md"):
                continue
            # 文件行给**相对库根的完整路径**（R4）：只给 basename 时，"A/B/ 显示了但
            # b.md 永远不出现"——重名文件分不清，也没法直接粘进 `kb open`。
            lines.append(f"{pad}  - {child['rel']}  {child['bytes']} B"
                         f"  {_kb_fmt_mtime(child.get('mtime'))}")
        if node["more"]:
            lines.append(f"{pad}  - …还有 {node['more']} 项未列出（more={node['more']}）")
        if level >= max_level:
            return
        for sub in node["dirs"]:
            emit(sub, level + 1)

    emit(tree, 0)
    return lines


def _kb_tree_tail_hint(tree, start_rel) -> str:
    """截断时钉在末尾的那一句（R4）：给一个**能直接粘回去**的目录。"""
    for sub in tree.get("dirs") or []:
        rel = _kb_rel_posix(sub.get("rel"))
        if rel:
            return (f"> 已截断：用 `path={rel}` 深入"
                    "（或加大 --max-chars / --max-tokens；完整读数见 --json）")
    parent = _kb_rel_posix(start_rel)
    if parent:
        return (f"> 已截断：用 `path={parent}` 深入某一级子目录"
                "（或加大 --max-chars / --max-tokens）")
    return "> 已截断：加大 --max-chars / --max-tokens 再看（起点下没有可深入的子目录）"



def kb_tree(conn, vault_row, *, rel="", depth=2, max_chars=8000, max_tokens=1200,
            citizen_id=None, via="cli:kb") -> dict:
    """看目录（只统计、不读正文、双预算）——§4.1 / KB-2 / KB-3。

    ## `depth` 的唯一语义（R4 · 评审 🟠-2 的收口；实现见 `_kb_tree_lines`）

    `depth` = **显示的最大目录层级**，起点（`rel` 指的目录，缺省=库根）为**第 0 层**：

      · `depth=1`：起点 + 起点的一级子目录 + 它们**各自的直接 `.md` 文件**；
        一级子目录的子目录不显示；
      · `depth=2`：再加上二级子目录及其直接文件；依此类推（上限 `KB_TREE_MAX_DEPTH`）；
      · **每个被显示的目录都列出它的直接文件**（附件只计数不列名，§7.3），
        每目录最多 `KB_MAX_ITEMS_PER_DIR` 项，超出记 `more=N`；
      · 文本随 `depth` 增大**单调不减**；只有命中双预算时才可能变短，那时
        `truncated=True` + `truncated_reasons`，并在**文本末尾**留一句
        "已截断：用 `path=<目录>` 深入"。
        （截断分支里 `chars` 不再是单调量：正文保留量由 token 预算定，深一层会把同一
        份预算花在更细的嵌套上——这正是"单调不减（除预算截断）"那条判据的例外出口，
        而**内容层面的前缀闭包**仍然成立：depth=2 出现的行在 depth=3 里仍会出现，
        只是可能落在截断点之后。）

    统计（`scan` / `summary`）数到第 `depth` 层：更深的目录不显示、也不计入合计
    （`scan.partial` / `scan.hidden_dirs` 如实说明还有多少个目录没展开）。统计深度与
    显示深度共用同一个参数是**成本约束**（R3：`depth=1` 的 scandir 次数必须 = 起点 +
    一级目录数），但两层在代码里是分开的——渲染侧自己按层级裁剪，不信任统计层。

    `vault_row` 可以是原始库行（`sqlite3.Row`，`ignore_globs` 还是 JSON 字符串）或
    `kb_vault_get` 归一后的 dict：入口先归一（`_kb_vault_norm`），两种入参回执逐字节一致。
    """
    vault_row = _kb_vault_norm(vault_row)
    return _kb_guard(conn, vault_row, rel, lambda: _kb_tree_impl(
        conn, vault_row, rel=rel, depth=depth, max_chars=max_chars,
        max_tokens=max_tokens, citizen_id=citizen_id, via=via),
        citizen_id=citizen_id, via=via)


def _kb_tree_impl(conn, vault_row, *, rel, depth, max_chars, max_tokens,
                  citizen_id, via) -> dict:
    as_of = datetime.datetime.now().astimezone()
    try:
        want_depth = int(depth)
    except (TypeError, ValueError) as exc:
        raise KBError("bad_usage", f"depth 必须是整数，收到 {depth!r}", detail=exc)
    want_depth = max(1, min(want_depth, KB_TREE_MAX_DEPTH))
    # S2：回执里"生效忽略规则"必须是**生效**的那份（= 强制项 ∪ 行内规则）。
    # 旧行缺 `90-模板/**` 时，只要还打印"行里写的两条"，回执就在说谎——
    # 而 `_kb_ignored` 实际用的是三条约。回填（`migrate`）负责把行补一致，
    # 这里负责"这一份读数与真正生效的规则同源"。
    globs = _kb_effective_globs(vault_row.get("ignore_globs") or [])
    root = Path(vault_row["path"])
    if not root.is_dir():
        # 离线：**先判目录在不在，再 resolve**——反过来的话 `kb_resolve_target` 会先抛
        # not_found，降级分支永远走不到（"有旧扫描数就能看"这条能力就成了一句空话）。
        if vault_row.get("last_scan_at"):
            text = "\n".join([
                f"# 知识库目录（离线）· {vault_row['name']}",
                "",
                f"> 知识库不在线（最后扫描 {vault_row['last_scan_at']}）："
                f"路径 `{vault_row['path']}` 打不开。",
                f"> 上次扫描读数：{vault_row.get('last_scan_files')} 文件 / "
                f"{vault_row.get('last_scan_notes')} 笔记 / "
                f"{vault_row.get('last_scan_bytes')} 字节。",
                "> 下面没有任何实时内容——不是空库，是离线。",
            ])
            fitted = _kb_fit_text([text], max_chars=max_chars, max_tokens=max_tokens)
            _kb_log_quiet(conn, vault_row, rel=_kb_rel_posix(rel), action="tree",
                          result="offline", citizen_id=citizen_id, via=via,
                          bytes_=fitted["chars"],
                          meta={"depth": want_depth, "max_chars": max_chars,
                                "max_tokens": max_tokens, "stale": True})
            return {"vault": _kb_vault_brief(vault_row), "as_of": iso_local(as_of),
                    "root": _kb_rel_posix(rel), "depth": want_depth,
                    "ignore_globs": globs,
                    "dirs": [], "summary": None,
                    "scan": {"files": None, "bytes": None, "ms": 0,
                             "stale": True, "partial": False, "hidden_dirs": 0},
                    "text": fitted["text"], "chars": fitted["chars"],
                    "tokens": fitted["tokens"], "truncated": fitted["truncated"],
                    "truncated_reasons": fitted["truncated_reasons"]}
        raise KBError("offline", f"知识库不在线：{root}",
                      hint="检查盘符/目录是否改名；有历史扫描数时会降级显示")
    target = kb_resolve_target(vault_row, rel)
    start = target["abs"]
    if not start.exists():
        raise KBError("not_found", f"知识库里没有这个目录：{target['rel']}",
                      hint="fish kb tree 看根目录，再逐层进入")
    t0 = time.perf_counter()
    tree = _kb_tree_dir(vault_row, start, level=0, max_depth=want_depth,
                        globs=globs)
    ms = int((time.perf_counter() - t0) * 1000)
    # R3：**一次遍历**同时算树与合计。原实现先建树再 `_kb_walk` 走第二遍只为拿
    # files/bytes——成本 2×，而且两遍之间文件变动会让两个数字互相打脸。
    files = tree["files"]
    total = tree["bytes"]
    as_of = datetime.datetime.now().astimezone()
    lines = _kb_tree_lines(vault_row, target["rel"], tree, depth=want_depth,
                           as_of=as_of, ignore_globs=globs)
    # R4：命中预算时末尾必须留一句"怎么深入"——`_kb_fit_text_with_tail` 保证它不被切掉
    fitted = _kb_fit_text_with_tail(
        lines, _kb_tree_tail_hint(tree, target["rel"]),
        max_chars=max_chars, max_tokens=max_tokens,
        short_tail="> 已截断：加大 --max-chars / --max-tokens 再看（或 --json）")
    _kb_log_quiet(conn, vault_row, rel=target["rel"], action="tree",
                  citizen_id=citizen_id, via=via, bytes_=fitted["chars"],
                  meta={"depth": want_depth, "max_chars": max_chars,
                        "max_tokens": max_tokens, "scan_ms": ms,
                        "scanned_files": files, "scanned_bytes": total,
                        "truncated": fitted["truncated"],
                        "truncated_reasons": fitted["truncated_reasons"]})
    return {"vault": _kb_vault_brief(vault_row), "as_of": iso_local(as_of),
            "root": target["rel"], "depth": want_depth,
            # `dirs` = 起点下的**一级目录**（§3.5 的样例形态）；起点的汇总在 `summary`。
            # S3：`mtime`（该目录子树内文件的最大 mtime，空目录=0）同时进 dirs 与 summary
            # ——文本里的"最近修改"与 JSON 读数必须来自同一份数，别让两边各算一次。
            "dirs": sorted(tree["dirs"], key=lambda d: d["rel"]),
            "summary": {k: tree[k] for k in ("rel", "notes", "attachments", "files",
                                             "bytes", "more", "children", "mtime")},
            "ignore_globs": globs,
            # `partial`：合计只覆盖已展开的层（R3 的诚实声明）
            "scan": {"files": files, "bytes": total, "ms": ms,
                     "stale": False, "partial": tree["partial"],
                     "hidden_dirs": tree["hidden_dirs"]},
            "text": fitted["text"], "chars": fitted["chars"],
            "tokens": fitted["tokens"], "truncated": fitted["truncated"],
            "truncated_reasons": fitted["truncated_reasons"]}


_KB_TOC_TITLE_BYTES = 2048      # 只读文件头这么多字节找标题（information scent 够用）


def _kb_toc_title(path) -> str:
    """取 `.md` 的标题：首个 `# xxx` 行；没有则用文件名（stem）。**只读文件头**。

    为什么自己解码而不是调 `kb_detect_text`：那个函数只判"是不是文本"（返回
    is_text/encoding/media），**不回传正文**；toc 只需要标题，按同一顺序试
    utf-8-sig → utf-8 → gb18030 即可（与 KB-12 的编码口径一致）。
    """
    try:
        raw = path.read_bytes()[:_KB_TOC_TITLE_BYTES]
    except OSError:
        return Path(path).stem
    text = ""
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            # 评审 🟠-3（2026-10-27）：2048B 会切断多字节序列——`errors="ignore"` 让
            # 截断处不炸（真库 21% 的标题此前因此静默退化成文件名）。
            text = raw.decode(enc, errors="ignore")
            break
        except UnicodeDecodeError:
            continue
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# ") and len(line.strip()) > 2:
            return line[2:].strip()[:60]
    return Path(path).stem


def kb_toc(conn, vault_row, *, rel="", depth=2, limit=None, offset=0,
           max_chars=8000, max_tokens=1200, citizen_id=None, via="cli:kb") -> dict:
    """目录快照（TOC）：**分类概览 + 每组前 3 个标题**（36/37 号的"档 1"最小版）。

    与 `kb_tree` 的分工：
      · `kb_tree` = "看某一层"（可递归展开到 depth，列文件名）；
      · `kb_toc`  = "一眼看全库结构"：每个目录只给 **文件数 + 前 3 个标题**（information scent），
        支持 `limit/offset` 对**当前层目录/文件**分页——海量库下 token 省 90%+。

    为什么必须给标题（36 号 §2.4 的 GraphRAG 判据）：只有「10-漫剧（194 个文件）」
    时，调用方判断不了"要不要下钻"；补上前 3 个标题（「西游逆局」「sss级机娘」…）
    才有 information scent——这是分页"有效"与"无效"的分界。

    判据：T1（分类+计数+前 3 标题）/ T2（token ≤ 同 depth 的 kb_tree 的 40%）/
    T3（offset 越界 → 空 + 明说"不是空库"）/ T4（`limit` 截断必须给翻页提示）。
    """
    as_of = datetime.datetime.now().astimezone()
    try:
        want_depth = max(1, min(int(depth), KB_TREE_MAX_DEPTH))
    except (TypeError, ValueError) as exc:
        raise KBError("bad_usage", f"depth 必须是整数，收到 {depth!r}", detail=exc)
    globs = _kb_effective_globs(vault_row.get("ignore_globs") or [])
    root = Path(vault_row["path"])
    if not root.is_dir():
        raise KBError("offline", f"知识库不在线：{root}",
                      hint="检查盘符/目录是否改名；toc 需要实时目录（无缓存）")
    target = kb_resolve_target(vault_row, rel)
    start = target["abs"]
    if not start.exists():
        raise KBError("not_found", f"知识库里没有这个目录：{target['rel']}")
    t0 = time.perf_counter()
    tree = _kb_tree_dir(vault_row, start, level=0, max_depth=want_depth, globs=globs)
    ms = int((time.perf_counter() - t0) * 1000)
    by_rel = {d["rel"]: d for d in tree["dirs"]}
    items = []
    for it in tree["children"]:
        if it["is_dir"]:
            child = by_rel.get(it["rel"])
            titles = []
            if child:
                for c in child["children"]:
                    if not c["is_dir"] and c["name"].lower().endswith(".md"):
                        titles.append(_kb_toc_title(root / c["rel"]))
                        if len(titles) >= 3:
                            break
            items.append({"kind": "dir", "name": it["name"], "rel": it["rel"],
                          "files": child["notes"] if child else 0,
                          "titles": titles})
        else:
            items.append({"kind": "file", "name": it["name"], "rel": it["rel"],
                          "files": 1,
                          "titles": [_kb_toc_title(root / it["rel"])]})
    # 评审 🟠-1（2026-10-27）：`_kb_tree_dir` 每层只留前 `KB_MAX_ITEMS_PER_DIR` 项，
    # 余下在 `tree["more"]` 里。`total` 必须报**真实子项数**——否则 >20 条的目录
    # 会被 `offset` 越界分支说成"没有更多内容"（真库 634 条目录实测过）。
    real_total = len(tree["children"]) + int(tree["more"] or 0)
    total = len(items)
    off = max(0, int(offset or 0))
    lim = None if limit in (None, 0) else max(1, int(limit))
    page = items[off: off + lim] if lim else items[off:]
    lines = [f"# 知识库目录快照 · {vault_row['name']}", ""]
    lines.append(f"> {tree['notes']} 篇笔记 / {tree['files']} 个文件 / "
                 f"{len(tree['dirs'])} 个目录（本层 {total} 项，显示 {len(page)} 项；"
                 f"depth={want_depth}）")
    lines.append("")
    for row in page:
        if row["kind"] == "dir":
            head = f"{row['name']}/  {row['files']} 篇"
            if row["titles"]:
                head += "  ← " + "、".join(f"「{t}」" for t in row["titles"])
            lines.append(head)
        else:
            title = row["titles"][0] if row["titles"] else row["name"]
            lines.append(f"{row['name']}  ← 「{title}」")
    if off >= real_total and real_total:
        lines += ["", f"> offset={off} 越过本层 {real_total} 项——没有更多内容（不是空库）。"]
    elif lim and off + lim < total:
        # 调用方主动 `limit` 截断：**优先**给翻页提示（别把"调用方截断"说成"显示上限"）
        lines += ["", f"> 还有 {total - off - lim} 项（翻页：--offset {off + lim} "
                      f"--limit {lim}）"]
    elif off + len(page) < real_total:
        # 本层真实子项多于**显示上限**：明说"还有多少没列"，不许说"没了"（评审 🟠-1）
        lines += ["", f"> 本层共 {real_total} 项（显示上限 {KB_MAX_ITEMS_PER_DIR}）；"
                      f"还有 {real_total - off - len(page)} 项未列出"
                      f"——请缩 `--depth` 或换个起点看。"]
    fitted = _kb_fit_text(lines, max_chars=max_chars, max_tokens=max_tokens)
    _kb_log_quiet(conn, vault_row, rel=target["rel"], action="toc",
                  citizen_id=citizen_id, via=via, bytes_=fitted["chars"],
                  meta={"depth": want_depth, "total": real_total, "offset": off,
                        "limit": lim, "scan_ms": ms})
    return {"vault": _kb_vault_brief(vault_row), "as_of": iso_local(as_of),
            "root": _kb_rel_posix(target["rel"]), "depth": want_depth,
            "total": real_total, "offset": off, "limit": lim, "shown": len(page),
            "more": max(0, real_total - off - len(page)),
            "items": page, "text": fitted["text"], "chars": fitted["chars"],
            "tokens": fitted["tokens"], "truncated": fitted["truncated"]}


def _kb_vault_brief(vault_row) -> dict:
    return {"name": vault_row.get("name"), "id": vault_row.get("vault_id"),
            "mode": vault_row.get("mode"), "path": vault_row.get("path")}


# ------------------------------------------------- 入参布尔解析（N7 · 第五轮）

# 「真/假」的唯一词表（字符串按 `casefold()` 比，首尾空白先剥掉）。不在表里的一律报错：
# 这个开关是**授权**，模糊写法既可能是人手误打，也可能是模板/配置渲染出的半截值——
# 把它猜成"同意"就是 fail-open。
_STRICT_TRUE = ("true", "1")
_STRICT_FALSE = ("false", "0")


def _strict_bool(value, name):
    """把「布尔开关」入参解析成 bool——**只接受明确的布尔形态**（N7 · 安全 🟡）。

    ## 为什么不能 `bool(value)`

    `bool("false") is True`。宿主/客户端把布尔当字符串传（YAML / 环境变量 / 模板渲染
    都很常见）时，`allow_sensitive="false"` 会被读成"同意读凭证正文"——一个**看起来
    拒绝、实际放行**的 fail-open。安全审计 N7 实测 `"false"` / `"0"` / `"true"` / `1`
    全部放行，只有整数 `0` 被拒。

    ## 接受什么

    | 入参 | 结果 |
    |---|---|
    | `True` / `False`（真布尔） | 原样 |
    | `1` / `0`（整数；`bool` 是 `int` 子类，先拦） | `True` / `False` |
    | `"true"` / `"1"` / `"TRUE"` / `" true "`（大小写、首尾空白不敏感） | `True` |
    | `"false"` / `"0"` / `"False"` | `False` |
    | `None`（未给：CLI 没带 `--allow-sensitive` / MCP 不带这个键） | `False` |
    | 其它（`"yes"` / `"on"` / `"真"` / `""` / `2` / 浮点 / 列表…） | `KBError('bad_usage')` |

    `None → False` 不是"猜"，是**缺省即拒绝**：schema 的 `default: False`、CLI 的
    `store_true`、这里三条路径本来就同一个答案，把它写成一条解析是为了让"字符串形态"
    也走同一条路——解析放在 `kb_open` / `kb_write` 的实现里，CLI、MCP、直接调用三方
    共用，不存在哪条通道可以绕过。
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value)
    elif isinstance(value, str):
        text = value.strip().casefold()
        if text in _STRICT_TRUE:
            return True
        if text in _STRICT_FALSE:
            return False
    raise KBError(
        "bad_usage",
        f"{name} 只接受 true / false（也接受 1 / 0），收到 {value!r}",
        hint="布尔开关不接受模糊写法：写 true 或 false 才是明确的授权/拒绝；"
             "CLI 不带 `--allow-sensitive` 即拒绝")


# ------------------------------------------------------------------ open（§4.2）


def kb_open(conn, vault_row, rel, *, max_chars=64000, allow_sensitive=False,
            citizen_id=None, via="cli:kb") -> dict:
    """读单文件（必须给显式相对路径）——§4.2 / KB-7 / KB-8。

    三句声明是**固定文案**（判据认它们）：
      ① 来源=知识库（未建索引），不是 fish 记忆；
      ② 本库未建索引，检索按需扫描；
      ③ 是否截断 + 字节比例。
    不写 `events`（KB-8：open 前后 events 行数不变）——知识库的读不产生"记忆事件"。

    `allow_sensitive`（R15）：命中内置敏感名单的文件，默认只给"文件名 + 大小 + 建议"，
    给 `True` 才返回正文——凭证类内容的读取必须是**显式二次授权**，不能是默认行为。
    第五轮（N7）起由 `_strict_bool` 解析：`"false"` / `"0"` 是**拒绝**（不是"非空字符串
    即同意"），`"yes"` 这类模糊写法直接 `bad_usage`。

    `vault_row` 同样接受原始库行（`sqlite3.Row` / `ignore_globs` 是 JSON 字符串）：
    入口先 `_kb_vault_norm` 归一，于是忽略规则不会被逐字符误判、`max_open_bytes`
    不会因为拿到的是字符串而比较错。
    """
    vault_row = _kb_vault_norm(vault_row)
    return _kb_guard(conn, vault_row, rel, lambda: _kb_open_impl(
        conn, vault_row, rel, max_chars=max_chars, allow_sensitive=allow_sensitive,
        citizen_id=citizen_id, via=via),
        citizen_id=citizen_id, via=via)


def _kb_sensitive_notice(rel_posix, size) -> str:
    """敏感文件的**拒读说明**（R15）：文件名 + 大小 + 建议在 Obsidian 里打开。"""
    return (f"`{rel_posix}` 是敏感文件（{size} B），默认不把正文送进模型上下文；"
            "建议在 Obsidian 里直接打开。确需由 AI 读取，请显式二次授权")


def _kb_open_impl(conn, vault_row, rel, *, max_chars, allow_sensitive, citizen_id,
                  via) -> dict:
    # N7（第五轮 · 安全 🟡）：授权旗先**解析成严格布尔**再判定——`"false"` 不是同意。
    # 放在这里（而不是只在 MCP 层）是为了让 CLI / MCP / 直接调 `fish.kb_open`
    # 三条入口共用同一解析，不会有一条通道"绕过"。
    allow_sensitive = _strict_bool(allow_sensitive, "allow_sensitive")
    limit = int(vault_row.get("max_open_bytes") or KB_DEFAULT_MAX_OPEN_BYTES)
    target = kb_resolve_target(vault_row, rel)          # 只解析一次（复用它算 abs/rel）
    abs_path = target["abs"]
    try:
        cap = int(max_chars)
    except (TypeError, ValueError) as exc:
        raise KBError("bad_usage", f"max_chars 必须是整数，收到 {max_chars!r}", detail=exc)
    cap = max(1, cap)
    # R7：目标必须是**常规文件**。空路径会解析到库根、显式给目录也常见——
    # 原实现在 `read_bytes()` 上抛 `PermissionError [Errno 13]`（CLI 直接打 traceback）。
    if abs_path.is_dir():
        raise KBError("bad_usage", f"目标是目录，不是文件：{target['rel'] or '/'}",
                      hint="用 `fish kb tree <目录>` 看目录；open 只读单个文件")
    if not abs_path.is_file():
        raise KBError("not_found", f"知识库里没有这个文件：{target['rel']}",
                      hint="`fish kb tree` 看目录结构，再给完整相对路径")
    size = abs_path.stat().st_size
    if size > limit:
        raise KBError("too_large",
                      f"文件 {size} B 超过单文件上限 {limit} B",
                      hint="长文档请分段读：`kb open <path> --max-chars`，或直接用文件工具读",
                      detail={"bytes": size, "limit": limit})
    # R15：敏感名单（凭证/密钥/.env…）→ 默认只给元数据，正文要显式授权。
    # S1（第四轮）：判定取自 `kb_resolve_target` 的 `sensitive`——它同时看了**字面串**与
    # **解析后落点**，所以 8.3 短名（`CREDEN~1.MD`）不会被"名字里没有 credential"绕过。
    sensitive = bool(target.get("sensitive"))
    if sensitive and not allow_sensitive:
        raise KBError("sensitive", _kb_sensitive_notice(target["rel"], size),
                      hint="CLI 加 `--allow-sensitive`；MCP 传 allow_sensitive=true")
    raw = abs_path.read_bytes()
    detected = kb_detect_text(raw, ext=abs_path.suffix)
    if detected["media"]:
        raise KBError("media", f"这是媒体/二进制文件（{detected['reason']}），不按文本读",
                      hint="用对应的查看器打开；知识库只读文本")
    if not detected["is_text"]:
        raise KBError("bad_encoding", "不是可识别的文本编码（UTF-8 / GB18030 都失败）",
                      hint="另存为 UTF-8 再读")
    text = raw.decode(detected["encoding"])
    sha = kb_sha256_bytes(raw)
    truncated = len(text) > cap
    shown_text = text[:cap]
    shown_bytes = (len(shown_text.encode("utf-8")) if truncated else len(raw))
    statements = [
        "来源=知识库（未建索引），不是 fish 记忆；不进 recall / 全文索引。",
        "本库未建索引；要检索请显式 `fish kb search`（按需扫描）。",
        ("已截断" if truncated else "未截断") +
        f"（{len(raw)}/{limit} B 上限；"
        f"显示 {len(shown_text)}/{len(text)} 字）",
    ]
    if sensitive:
        statements.append("本文件命中敏感名单：正文是在**显式授权**（allow_sensitive）下读出的。")
    # N9（第五轮 · 安全 ⚪）：**只有真授权才记 `allow_sensitive=true`**，普通读不长这个
    # 字段——读/写两面同一口径（写面见 `_kb_write_impl` 的 `write_meta`）。
    # 恒为 false 的字段既无信息量，又多一处要解释的隐私面。
    open_meta = {"max_chars": cap, "truncated": truncated}
    if allow_sensitive:
        open_meta["allow_sensitive"] = True
    _kb_log_quiet(conn, vault_row, rel=target["rel"], action="open",
                  citizen_id=citizen_id, via=via, bytes_=len(raw), sha256=sha,
                  meta=open_meta)
    return {"rel": target["rel"], "abs": str(abs_path), "bytes": len(raw),
            "sha256": sha, "encoding": detected["encoding"],
            "eol": kb_detect_eol(raw), "truncated": truncated,
            "sensitive": sensitive, "shown": shown_bytes, "limit": limit,
            "chars_shown": len(shown_text), "chars_total": len(text),
            "statements": statements, "text": shown_text}


def _kb_rel_posix(rel) -> str:
    """把 rel 归一成"审计里存的那种相对路径"（`/` 分隔、无前导/尾随分隔符）。"""
    return str(rel or "").replace("\\", "/").strip("/")


# ------------------------------------------------------------------ search（§4.3）


def _kb_search_multi(conn, vault_row, needles, *, limit_per_needle=2,
                     citizen_id=None, via="cli:kb") -> dict:
    """**一次 walk 跑多个检索词**（`_kb_push_candidates` 专用）。

    ## 为什么需要它（2026-10-27 实测驱动的重构）

    `_kb_push_candidates` 原本对每个检索词各调一次 `kb_search`，而 `kb_search`
    **每次都完整遍历 vault**：实测 297 个 md / 2738 个条目 ≈ **0.9s/次**，
    3 个词 = 3 次全遍历 ≈ **2.7s**。而 hook 的 `UserPromptSubmit` 超时是 **10s**，
    加上 Python 启动与 `tool_after` 路径的其他开销，就贴着超时线走
    （实测 `prompt-push` 5.4–16.7s，不稳定）。

    **cProfile 定位**（3 次 search / 5.22s 累计）：
      `_kb_walk` 2.945s（56%），其中 `_kb_ignored` 1.372s（被调 8223 次）、
      `_kb_judge_component` 0.440s（**89,916 次**）、`nt.scandir` 0.354s；
      `kb_detect_text` 0.913s（17%）；`read_bytes` 0.801s（15%）。

    → **遍历、读文件、编码检测这三件对"多个词"是同一份工作**，重复了 3 次。

    本函数把它们做成**一次**，再对每个词各算一份命中。

    ## 与 `kb_search` 的等价性（必须逐条对上，否则就是静默行为变更）

    · 同样的 `_kb_walk`（同样的忽略规则、同样的 symlink/reparse 处理）；
    · 同样的过滤：`.md` 后缀、`_kb_is_sensitive_name`、`KB_SEARCH_MAX_FILE_BYTES`、
      `kb_detect_text` 的 `is_text`；
    · 同样的匹配：`casefold()` + 子串、`occurrences` 累加、片段截断 240 字；
    · 同样的排序 key：`(-occurrences, -mtime, rel)`；
    · 同样的 `limit` 上限：`max(1, min(limit, KB_MAX_LIMIT))`；
    · 同样的审计：一次 `action='search'` + 每个最终命中一次 `action='search_hit'`。

    ## 返回

    以**原始检索词**为键的 dict，每项：
    `{"hits": [...], "total": N, "ms": M, "scanned_files": F, "scanned_bytes": B,
      "skipped_large" / "skipped_binary" / "skipped_encoding" / "skipped_sensitive": n}`

    `hits` 元素与 `_kb_search_impl` 完全同构（rel / abs / occurrences / lines / mtime）。
    """
    vault_row = _kb_vault_norm(vault_row)
    qs = [str(n or "").strip() for n in (needles or [])]
    qs = [n for n in qs if n]
    if not qs:
        return {}
    root = Path(vault_row["path"])
    if not root.is_dir():
        return {}
    want = max(1, min(int(limit_per_needle), KB_MAX_LIMIT))
    folded = [n.casefold() for n in qs]
    per = {n: {"hits": [], "scanned_files": 0, "scanned_bytes": 0,
               "skipped_large": 0, "skipped_binary": 0,
               "skipped_encoding": 0, "skipped_sensitive": 0} for n in qs}
    t0 = time.perf_counter()
    for ent in _kb_walk(vault_row, root):
        if ent["is_dir"] or not str(ent["rel"]).lower().endswith(".md"):
            continue
        if _kb_is_sensitive_name(ent["rel"]):
            for n in qs:
                per[n]["skipped_sensitive"] += 1
            continue
        for n in qs:
            per[n]["scanned_files"] += 1
        if int(ent["size"] or 0) > KB_SEARCH_MAX_FILE_BYTES:
            for n in qs:
                per[n]["skipped_large"] += 1
            continue
        try:
            raw = ent["abs"].read_bytes()
        except OSError:
            continue
        for n in qs:
            per[n]["scanned_bytes"] += len(raw)
        detected = kb_detect_text(raw, ext=".md")
        if not detected["is_text"]:
            key = "skipped_binary" if detected["media"] else "skipped_encoding"
            for n in qs:
                per[n][key] += 1
            continue
        try:
            text = raw.decode(detected["encoding"])
        except (UnicodeDecodeError, LookupError):
            continue
        lines = text.splitlines()
        folded_lines = [ln.casefold() for ln in lines]
        for n, nd in zip(qs, folded):
            matched = []
            occurrences = 0
            for lineno, (raw_line, fl) in enumerate(zip(lines, folded_lines), 1):
                if nd in fl:
                    occurrences += fl.count(nd)
                    snippet = raw_line.strip()
                    if len(snippet) > 240:
                        pos = fl.find(nd)
                        start = max(0, pos - 60)
                        snippet = ("…" if start else "") + snippet[start:start + 240] + "…"
                    matched.append({"lineno": lineno, "text": snippet})
            if matched:
                per[n]["hits"].append({
                    "rel": ent["rel"], "abs": str(ent["abs"]),
                    "occurrences": occurrences, "lines": matched[:3],
                    "mtime": ent["mtime"]})
    ms = int((time.perf_counter() - t0) * 1000)
    result = {}
    for n in qs:
        hits = per[n]["hits"]
        hits.sort(key=lambda h: (-h["occurrences"], -float(h["mtime"]), h["rel"]))
        result[n] = dict(per[n], hits=hits[:want], total=len(hits), ms=ms)
    # 审计与 `kb_search` 同口径：一次 search + 每个最终命中一次 search_hit。
    # 失败静默——审计写不进去不该让检索整体失败（与 `_kb_log_quiet` 的契约一致）。
    try:
        _kb_log_quiet(conn, vault_row, rel="", action="search", via=via,
                      citizen_id=citizen_id)
        for n in qs:
            for hit in result[n]["hits"]:
                _kb_log_quiet(conn, vault_row, rel=hit["rel"], action="search_hit",
                              via=via, citizen_id=citizen_id)
    except Exception:      # noqa: BLE001
        pass
    return result


def kb_search(conn, vault_row, query, *, limit=10, max_chars=8000, max_tokens=1800,
              citizen_id=None, via="cli:kb") -> dict:
    """在 vault 内**按需扫描**检索（不建索引）——§4.3 / KB-10 / KB-9。

    只扫已登记 vault 的 `.md`；回执头部必须写明「来源=知识库（未建索引）」+
    「扫描 N 文件 / M 字节 / x ms」。**它与 `recall` 是两套东西**：不查 `memory_fts`、
    不写 `code_index`、不产生记忆事件——KB-10 的判据就是拿一个只存在于知识库里的独特词
    去 `recall`（0 命中）与 `kb search`（1 命中）做对照。

    ## 审计通道（R1 · 评审 🔴-1 / 安全 F2）

    `query` **绝不**走 `rel` 通道：失败时 `_kb_guard` 的审计路径恒为空串，
    query 只以 `meta.query_hash`（sha256 前 12）出现——检索词属于人不想留痕的那类内容。

    `vault_row` 接受原始库行（`sqlite3.Row` / `ignore_globs` 是 JSON 字符串）：入口先
    `_kb_vault_norm` 归一。这一层尤其要紧——`search` 的遍历面完全靠 `ignore_globs`
    挡 `.obsidian/`、`.trash/`，逐字符误判会把配置目录里的文件也扫进片段。
    """
    vault_row = _kb_vault_norm(vault_row)
    q = str(query or "").strip()
    # 失败行也要能按"同一个检索词"聚合（哈希不是原文，可存）
    audit_meta = None
    if q:
        audit_meta = {"query_hash":
                      hashlib.sha256(q.casefold().encode("utf-8")).hexdigest()[:12]}
    return _kb_guard(conn, vault_row, "", lambda: _kb_search_impl(
        conn, vault_row, query, limit=limit, max_chars=max_chars,
        max_tokens=max_tokens, citizen_id=citizen_id, via=via),
        citizen_id=citizen_id, via=via, audit_rel="", audit_meta=audit_meta)


def _kb_search_impl(conn, vault_row, query, *, limit, max_chars, max_tokens,
                    citizen_id, via) -> dict:
    q = str(query or "").strip()
    if not q:
        raise KBError("bad_usage", "检索词不能为空", hint="fish kb search <关键词>")
    root = Path(vault_row["path"])
    if not root.is_dir():
        raise KBError("offline", f"知识库不在线：{root}", hint="检查盘符/目录是否改名")
    try:
        want = int(limit)
    except (TypeError, ValueError) as exc:
        raise KBError("bad_usage", f"limit 必须是整数，收到 {limit!r}", detail=exc)
    want = max(1, min(want, KB_MAX_LIMIT))
    needle = q.casefold()
    as_of = datetime.datetime.now().astimezone()
    t0 = time.perf_counter()
    scanned_files = scanned_bytes = 0
    skipped_large = skipped_binary = skipped_encoding = skipped_sensitive = 0
    hits = []
    for ent in _kb_walk(vault_row, root):
        if ent["is_dir"] or not str(ent["rel"]).lower().endswith(".md"):
            continue
        # R15：敏感文件**不进片段**（连"命中了某凭证文件"都不说，只计数）
        if _kb_is_sensitive_name(ent["rel"]):
            skipped_sensitive += 1
            continue
        scanned_files += 1
        if int(ent["size"] or 0) > KB_SEARCH_MAX_FILE_BYTES:
            skipped_large += 1
            continue
        try:
            raw = ent["abs"].read_bytes()
        except OSError:
            continue
        scanned_bytes += len(raw)
        detected = kb_detect_text(raw, ext=".md")
        if not detected["is_text"]:
            if detected["media"]:
                skipped_binary += 1
            else:
                skipped_encoding += 1
            continue
        text = raw.decode(detected["encoding"])
        matched = []
        occurrences = 0
        for lineno, line in enumerate(text.splitlines(), 1):
            folded = line.casefold()
            if needle in folded:
                occurrences += folded.count(needle)   # 一行命中两次要算两次
                snippet = line.strip()
                if len(snippet) > 240:
                    pos = snippet.casefold().find(needle)
                    start = max(0, pos - 60)
                    snippet = ("…" if start else "") + snippet[start:start + 240] + "…"
                matched.append({"lineno": lineno, "text": snippet})
        if matched:
            hits.append({"rel": ent["rel"], "abs": str(ent["abs"]),
                         "occurrences": occurrences, "lines": matched[:3],
                         "mtime": ent["mtime"]})
    ms = int((time.perf_counter() - t0) * 1000)
    hits.sort(key=lambda h: (-h["occurrences"], -float(h["mtime"]), h["rel"]))
    total = len(hits)
    shown = hits[:want]
    lines = [
        f"# 知识库检索 · {vault_row['name']}",
        "",
        f"> source=知识库（未建索引）｜as_of {iso_local(as_of)}",
        f"> scan={scanned_files} files / {scanned_bytes} bytes / {ms} ms"
        f"｜命中 {total} 文件，显示 {len(shown)} 个",
        f"> 跳过：大文件 {skipped_large} / 二进制 {skipped_binary} / 编码不可读 "
        f"{skipped_encoding} / 敏感名单 {skipped_sensitive}"
        f"（单文件上限 {KB_SEARCH_MAX_FILE_BYTES} B）",
        "> 只扫已登记 vault 的 `.md`；不建索引、不进 `memory_fts`——找 fish 记忆请用 `recall`。",
        "",
    ]
    query_hash = hashlib.sha256(q.casefold().encode("utf-8")).hexdigest()[:12]
    for hit in shown:
        # PRD KB-4 / 设计 §4.3.7：每条命中给**库内全路径**（而不只是相对路径）——
        # "去哪个文件的第几行看"必须能直接落到磁盘上（R8 · QA F-QA-4）。
        # 同时保留库内相对路径：它是 `kb open` / `kb write` 的入参形态，缺了还要人换算。
        lines.append(f"- `{hit['abs']}`（库内 `{hit['rel']}`，命中 {hit['occurrences']} 处）")
        for item in hit["lines"]:
            lines.append(f"  - L{item['lineno']}: {item['text']}")
    if not shown:
        lines.append("- （没有命中）")
    fitted = _kb_fit_text(lines, max_chars=max_chars, max_tokens=max_tokens)
    _kb_log_quiet(conn, vault_row, rel="", action="search",
                  citizen_id=citizen_id, via=via, bytes_=fitted["chars"],
                  meta={"query_hash": query_hash, "qlen": len(q), "limit": want,
                        "scan_ms": ms, "scanned_files": scanned_files,
                        "scanned_bytes": scanned_bytes,
                        "skipped_large": skipped_large,
                        "skipped_binary": skipped_binary,
                        "skipped_encoding": skipped_encoding,
                        "skipped_sensitive": skipped_sensitive,
                        "truncated": fitted["truncated"]})
    for hit in shown:                      # 每个命中一条（不写正文、不写 query 原文）
        _kb_log_quiet(conn, vault_row, rel=hit["rel"], action="search_hit",
                      citizen_id=citizen_id, via=via,
                      meta={"query_hash": query_hash})
    return {"vault": _kb_vault_brief(vault_row), "as_of": iso_local(as_of),
            "query_hash": query_hash, "qlen": len(q), "limit": want,
            "source": "知识库（未建索引）",
            "scanned": {"files": scanned_files, "bytes": scanned_bytes, "ms": ms,
                        "skipped_large": skipped_large,
                        "skipped_binary": skipped_binary,
                        "skipped_encoding": skipped_encoding,
                        "skipped_sensitive": skipped_sensitive},
            "hits": shown, "total": total, "shown": len(shown),
            "text": fitted["text"], "chars": fitted["chars"],
            "tokens": fitted["tokens"], "truncated": fitted["truncated"],
            "truncated_reasons": fitted["truncated_reasons"]}







# ------------------------------------------------------------------ 写三档（§4.4）


_KB_FM_KEY_RE = re.compile(r"^([^\s:#][^:]*):(.*)$")


def _kb_scalar(value) -> str:
    """frontmatter 只接受标量（列表/字典/None → `bad_frontmatter`）。

    为什么不做 YAML 解析：本批不引入第三方依赖，而"自己实现半个 YAML"是 bug 的温床。
    只处理 `key: value` 单行标量，未知键**原样保留**——这是 R7 的边界（嵌套/锚点/多行
    字符串一律不碰，遇到就明说做不到，而不是猜着重写）。

    ## 2026-10-26 第二轮（R12 · 安全 F8）：字符串值禁换行/控制字符

    原来 `"x\\nadmin: true"` 会被原样拼进文件头，落盘后 `admin` 变成**真的顶层属性**
    （更狠的形态是值里带 `\\n---\\n`，直接把 frontmatter 提前闭合）。
    frontmatter 的语义是"一行一个标量"，所以含换行/控制字符的值一律拒绝——
    宁可写不进去，也不替人伪造属性。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        if any(ch in value for ch in "\r\n"):
            raise KBError("bad_frontmatter",
                          "frontmatter 值不能含换行（会注入出额外的顶层属性）",
                          hint="要写多行内容请放进正文；frontmatter 只接受单行标量")
        if any(ord(ch) < 32 and ch != "\t" for ch in value):
            raise KBError("bad_frontmatter",
                          "frontmatter 值不能含控制字符",
                          hint="去掉不可见字符（YAML 里它们会破坏结构）")
        return value
    raise KBError("bad_frontmatter",
                  f"frontmatter 值必须是标量（str/int/float/bool），收到 {type(value).__name__}",
                  hint="嵌套结构请手改文件；本批只做标量增改")


def _kb_quote(value: str) -> str:
    """按需加引号（含 `:`、`#`、引号、首尾空格等 YAML 敏感字符时）。

    R12：换行与控制字符**不靠加引号解决**（`_kb_scalar` 已拒），这里只做单行内转义。
    """
    s = str(value)
    if s == "" or s.strip() != s or any(ch in s for ch in ':#{}[]&*!|>\'"%@`,'):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _kb_split_frontmatter(text) -> dict:
    """拆 frontmatter：`{"head":[...],"body":str,"ok":bool,"has":bool}`。

    `ok=False` 只在"以 `---` 开头但没有收尾 `---`"时出现——那是坏文件，
    update 时必须拒绝而不是把它当正文重写（会把人的 frontmatter 变成正文第一段）。
    """
    norm = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = norm.split("\n")
    if not lines or lines[0].strip() != "---":
        return {"head": [], "body": norm, "ok": True, "has": False}
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            body = "\n".join(lines[idx + 1:])
            if body.startswith("\n"):
                body = body[1:]
            return {"head": lines[1:idx], "body": body, "ok": True, "has": True}
    return {"head": [], "body": norm, "ok": False, "has": True}


def _kb_merge_frontmatter(head, updates) -> list:
    """在已有 head 上**只增改给定键**：未知键、注释、空行一字不动（R7）。

    新键追加在末尾并按名字排序（确定性：同一输入两次写出的字节一样）。
    """
    updates = dict(updates or {})
    if not updates:
        return list(head)
    for key in updates:
        k = str(key or "").strip()
        if not k or ":" in k or "\n" in k or k.startswith("-"):
            raise KBError("bad_frontmatter", f"frontmatter 键非法：{key!r}")
    rendered = {str(k).strip(): _kb_quote(_kb_scalar(v)) for k, v in updates.items()}
    # R12：渲染结果再做一次"单行断言"（纵深防御：`_kb_scalar` 是唯一入口，
    # 但这里是最终拼进文件头的地方——真出了洞，宁可抛错也不写出坏头）。
    for key, text in rendered.items():
        if "\n" in text or "\r" in text:
            raise KBError("bad_frontmatter",
                          f"frontmatter 渲染结果不是单行：{key!r}",
                          hint="值里含换行；本批只接受单行标量")
    out, seen = [], set()
    for line in head:
        m = _KB_FM_KEY_RE.match(str(line))
        if not m:
            out.append(line)               # 注释 / 空行 / 无法识别的行：保留
            continue
        key = m.group(1).strip()
        if key in rendered and key not in seen:
            out.append(f"{key}: {rendered[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key in sorted(rendered):
        if key not in seen:
            out.append(f"{key}: {rendered[key]}")
    return out


def _kb_render_document(head, body, *, eol) -> bytes:
    """拼出最终字节：frontmatter（若有）+ 空行 + 正文；EOL 按目标文件的既有风格。"""
    body = str(body or "").replace("\r\n", "\n").replace("\r", "\n")
    parts = []
    if head:
        parts.append("---\n" + "\n".join(head) + "\n---\n\n")
    parts.append(body)
    text = "".join(parts)
    if text and not text.endswith("\n"):
        text += "\n"
    if eol == "crlf":
        text = text.replace("\n", "\r\n")
    return text.encode("utf-8")


def _kb_backup_slug(rel) -> str:
    """备份目录名里的 slug：斜杠换 `_`、去掉不安全字符、截 40 字符。"""
    raw = str(rel or "").replace("\\", "/")
    cleaned = re.sub(r"[^\w\u4e00-\u9fff.\-]+", "_", raw).strip("_")
    return cleaned[:40] or "root"


def kb_backup(data_root, vault_row, rel, data, meta) -> dict:
    """写前备份：`<data_root>/_backups/kb/<vault_id>/<ts>-<slug>/`（只备份被写的这一个文件）。

    整库 10.96 GB，绝不整库复制（裁决 §四.4）。`create` 时 `data=None`：目录里只有
    `meta.json`（回滚语义 = 删掉那个刚创建、之后无人改动的文件）。

    ## 两条边界（2026-10-26 第二轮）

    - **R13**：payload 与读/写路径同口径（`max_open_bytes`）——备份不该成为绕过
      "单文件上限"的后门；
    - **R21**（评审 ⚪-2）：同秒同路径的第 50 次写过去会**静默覆盖**旧备份，
      等于把旧回滚点顶掉。现在候选位耗尽后改用 uuid 后缀，**绝不复用已存在目录**。
    """
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    limit = int((vault_row or {}).get("max_open_bytes") or KB_DEFAULT_MAX_OPEN_BYTES)
    if data is not None and len(data) > limit:
        raise KBError("too_large",
                      f"要备份的内容 {len(data)} B 超过单文件上限 {limit} B，拒绝备份",
                      hint="先拆分文件；本工具不会为超大文件生成备份",
                      detail={"bytes": len(data), "limit": limit})
    vault_id = str((vault_row or {}).get("vault_id") or "unknown")
    base = Path(data_root) / "_backups" / "kb" / vault_id / f"{ts}-{_kb_backup_slug(rel)}"
    if base.exists():
        for idx in range(2, 50):
            candidate = base.with_name(f"{base.name}-{idx}")
            if not candidate.exists():
                base = candidate
                break
        else:
            # 50 个候选全被占：用随机后缀**新开**一个，绝不复用（R21）
            base = base.with_name(f"{base.name}-{secrets.token_hex(4)}")
            if base.exists():
                raise KBError("bad_usage",
                              f"备份目录分配失败（同名目录已存在）：{base.name}",
                              hint="同一秒同一路径的备份过多；等一秒重试或先清理 _backups")
    base.mkdir(parents=True, exist_ok=False)
    if data is not None:
        (base / "payload").write_bytes(data)
    record = dict(meta or {})
    record.update({"rel": str(rel or ""), "ts": ts,
                   "bytes_before": (len(data) if data is not None else None),
                   "sha256_before": (kb_sha256_bytes(data) if data is not None else None)})
    (base / "meta.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"backup_path": str(base), "bytes": (len(data) if data is not None else 0),
            "sha256": (kb_sha256_bytes(data) if data is not None else None)}


def _kb_backup_meta(backup_path) -> dict:
    p = Path(backup_path) / "meta.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _kb_backup_finalize(backup_path, **extra) -> bool:
    """写入完成后再补写 `meta.json`（`write_id` 只有在审计行落库后才知道）。

    返回 **True=写成功 / False=写失败**（S9 · 评审 🟡-N4）。原实现 `except OSError: pass`
    静默吞掉：写本身成功、回执照旧打印 `undo: fish kb rollback <id>-<hex>`，但备份
    `meta.json` 里没有 `write_id`，该凭据此后**恒** `not_found`，而文案还怪"你的凭据不对"。
    现在失败要能被调用方看见（回执带 warning + 撤销说明改成手工恢复）。
    """
    meta = _kb_backup_meta(backup_path)
    meta.update(extra)
    try:
        (Path(backup_path) / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return False                      # 备份目录不可写不影响主流程，但**不许静默**
    return True


def _kb_same_name(conn, vault_row, rel) -> list:
    """同名笔记扫描（仅 create）：同 basename 的其它 `.md`，最多报 `KB_SAME_NAME_LIMIT` 条。

    它**不阻断**写入（同名是 Obsidian 的正常现象），但必须让 AI 看见——
    "我要新建的这篇，库里已经有同名的了"是"别造重复内容"的最后一道提示。
    """
    name = Path(str(rel)).name.casefold()
    root = Path(vault_row["path"])
    if not name or not root.is_dir():
        return []
    out = []
    for ent in _kb_walk(vault_row, root):
        if ent["is_dir"]:
            continue
        if Path(ent["rel"]).name.casefold() != name:
            continue
        if ent["rel"] == rel:
            continue
        out.append({"rel": ent["rel"]})
        if len(out) >= KB_SAME_NAME_LIMIT:
            break
    return out


def kb_write(conn, data_root, vault_row, rel, *, action, content=None,
             frontmatter=None, base_sha256=None, reason=None, allow_sensitive=False,
             citizen_id=None, account_id=None, via="cli:kb") -> dict:
    """写三档：create / append / update（update 必带 base_sha256）——§4.4 / KB-13/15。

    铁律顺序：**先备份 → 再原子写 → 写后重读校验 sha256**。
    写失败不回滚已写入的字节（原子写只可能"整个换掉"或"完全没动"），
    但一定会给出 `write_id` 与 `undo` 命令，让人能撤。

    `account_id`（R11）：MCP 面把 `session_key` 记进 `kb_access.account_id`——
    只记 `citizen_id` 回答不了"是这个公民的**哪个会话**改的"。

    `allow_sensitive`（S6 · 第四轮）：写路径对敏感名单命中的目标默认**拒绝**
    （凭证不该由 AI 改，交 Obsidian/人工）；人显式授权（CLI `--allow-sensitive` /
    MCP `allow_sensitive=true`）才放行。判定取 `kb_resolve_target` 的 `sensitive`，
    所以 8.3 短名/别名落到敏感文件同样拦得住。
    第五轮（N7）起与读面共用 `_strict_bool`：`"false"` / `"0"` 是拒绝，模糊写法
    （`"yes"` / `"on"` / `2`）→ `bad_usage`（写一个字节都不会动）。
    """
    return _kb_guard(conn, vault_row, rel, lambda: _kb_write_impl(
        conn, data_root, vault_row, rel, action=action, content=content,
        frontmatter=frontmatter, base_sha256=base_sha256, reason=reason,
        allow_sensitive=allow_sensitive, citizen_id=citizen_id,
        account_id=account_id, via=via),
        citizen_id=citizen_id, via=via)


def _kb_write_impl(conn, data_root, vault_row, rel, *, action, content,
                   frontmatter, base_sha256, reason, allow_sensitive, citizen_id,
                   account_id, via) -> dict:
    # N7（第五轮 · 安全 🟡）：读面同一解析（`fish._strict_bool`）——写敏感文件是更强的
    # 授权，字符串 `"false"` 更不能被读成"同意"。
    allow_sensitive = _strict_bool(allow_sensitive, "allow_sensitive")
    if action not in ("create", "append", "update"):
        raise KBError("bad_action", f"未知写动作：{action!r}（只能 create / append / update）")
    if vault_row.get("mode") != "rw":
        raise KBError("mode_ro", f"知识库 `{vault_row.get('name')}` 是 mode=ro，拒绝写入",
                      hint=f"fish kb vault set-mode {vault_row.get('name')} rw（需人显式授权）")
    if content is None:
        raise KBError("bad_usage", f"{action} 必须给正文内容（--content-file / content 参数）")
    if frontmatter and action == "append":
        raise KBError("bad_usage", "append 不接受 frontmatter（追加不该动文件头）",
                      hint="要改 frontmatter 请用 update")
    body_text = str(content)
    if body_text.lstrip().startswith("---"):
        raise KBError("bad_frontmatter", "正文不要带 frontmatter（会写出两个文件头）",
                      hint="把 frontmatter 放进 frontmatter 参数；正文只放正文")
    content_bytes = body_text.encode("utf-8")
    if len(content_bytes) > KB_MAX_CONTENT_BYTES:
        raise KBError("content_too_large",
                      f"正文 {len(content_bytes)} B 超过单次写入上限 {KB_MAX_CONTENT_BYTES} B",
                      hint="拆成多篇或用 append 分段写")

    target = kb_resolve_target(vault_row, rel, for_write=True)
    abs_path = target["abs"]
    rel_posix = target["rel"]
    exists = abs_path.exists()

    # S6（第四轮 · 安全 N3）：敏感目标**写面也上闸**。
    # 原实现只护读：`append` 到凭证文件会静默追加（实测 49 → 65 B）、`create` 能在库内
    # 新建 `.env`/`凭证*.md`；`update` 只是因为要 `kb_open` 拿 sha 而"事实上"受阻——
    # 那是副作用，不是设计。这里统一拒（含 8.3 别名解析后的落点，见 S1）。
    if target.get("sensitive") and not allow_sensitive:
        raise KBError("sensitive",
                      f"`{rel_posix}` 命中敏感名单（凭证类文件），拒绝写入；"
                      "凭证内容请交 Obsidian / 人工维护",
                      hint="确需由 AI 改写，请在**显式二次授权**下重试："
                           "CLI 加 `--allow-sensitive`，MCP 传 allow_sensitive=true")

    if action == "create" and exists:
        raise KBError("exists", f"文件已存在：{rel_posix}",
                      hint="改用 --update（先 kb open 取 sha256）；本工具不覆盖既有笔记")
    if action in ("append", "update") and not exists:
        raise KBError("not_found", f"文件不存在：{rel_posix}",
                      hint="新建请用 --create")
    # R13（安全 F5）：写路径与**读路径同口径**——既有文件超过 `max_open_bytes` 一律拒，
    # 而不是"读不让看、写却整篇读进来再放大 3 倍"（内存 + 备份 + 原子写各一份）。
    limit = int(vault_row.get("max_open_bytes") or KB_DEFAULT_MAX_OPEN_BYTES)
    if exists:
        size = abs_path.stat().st_size
        if size > limit:
            raise KBError("too_large",
                          f"目标文件 {size} B 超过单文件上限 {limit} B，拒绝改写",
                          hint="长文档请分段（先拆分再用 append）；本工具不整篇重写大文件",
                          detail={"bytes": size, "limit": limit})
    before = abs_path.read_bytes() if exists else None
    if before is not None:
        detected = kb_detect_text(before, ext=abs_path.suffix)
        if detected["media"]:
            raise KBError("media", f"目标不是文本文件（{detected['reason']}），拒绝改写成 md",
                          hint="要改请人工确认；本工具只写 Markdown")
        if not detected["is_text"]:
            raise KBError("bad_encoding", "目标文件不是可识别文本编码，拒绝改写",
                          hint="另存为 UTF-8 后再试")
        # R2（评审 🟠-1 / 安全 F7）：写路径只接受 **UTF-8 族**。
        # 原实现把 GB18030 认出来之后**照样写**：append 产出"半 GBK 半 UTF-8"的混合编码
        # 且写后 sha256 校验必然通过（比的是"我写进去的字节"），等于静默弄坏人的笔记；
        # update 则用 strict-utf8 解码抛裸 `UnicodeDecodeError`（CLI 出 traceback、MCP 丢码）。
        # 设计 §5.8 的口径是"不写不确定编码"——这里明说拒绝，文件字节一个不动。
        if detected["encoding"] not in ("utf-8", "utf-8-sig"):
            raise KBError("bad_encoding",
                          f"目标文件编码是 {detected['encoding']}，写路径只接受 UTF-8",
                          hint="在 Obsidian 里另存为 UTF-8（或先转码）再写；本次未改动文件")
    if action == "update":
        if not base_sha256:
            raise KBError("stale_base", "update 必须带 base_sha256（乐观锁）",
                          hint="先 `fish kb open <path>` 拿到 sha256，再 --base-sha256")
        current_sha = kb_sha256_bytes(before)
        if str(base_sha256).strip() != current_sha:
            raise KBError("stale_base", "文件已被改动（base_sha256 不匹配）",
                          hint="重新 `fish kb open` 读一次再写；不要覆盖别人的改动")
    eol = kb_detect_eol(before) if before is not None else "lf"

    if action == "append":
        sep = b""
        if before and not (before.endswith(b"\n") or before.endswith(b"\r\n")):
            sep = b"\r\n" if eol == "crlf" else b"\n"
        addition = body_text.replace("\r\n", "\n").replace("\r", "\n")
        if eol == "crlf":
            addition = addition.replace("\n", "\r\n")
        new_bytes = before + sep + addition.encode("utf-8")
        if not new_bytes.endswith(b"\n"):
            new_bytes += b"\r\n" if eol == "crlf" else b"\n"
    else:
        if action == "update":
            # S7（第四轮 · 安全 N3）：按**检测到的编码**解码，保留 BOM 状态。
            # 原实现写路径放行 `utf-8-sig`（R2），update 分支却用 `decode("utf-8")`：
            # 文本以 `\ufeff` 开头 → `_kb_split_frontmatter` 认不出文件头 → `head=[]`
            # → 整篇按"无 frontmatter"重写，`aliases`/`tags` **静默消失**（断双链），
            # 而写后 sha256 校验照样通过（比的是"我写进去的字节"）。
            source_encoding = detected["encoding"] if before is not None else "utf-8"
            had_bom = source_encoding == "utf-8-sig"
            split = _kb_split_frontmatter(before.decode(source_encoding, errors="strict"))
            if not split["ok"]:
                raise KBError("bad_frontmatter", "现有文件的 frontmatter 没有收尾 `---`",
                              hint="先手动修好文件头，再 update")
            head = _kb_merge_frontmatter(split["head"], frontmatter or {})
        else:
            had_bom = False
            head = _kb_merge_frontmatter([], frontmatter or {})
        new_bytes = _kb_render_document(head, body_text, eol=eol)
        if had_bom and not new_bytes.startswith(b"\xef\xbb\xbf"):
            new_bytes = b"\xef\xbb\xbf" + new_bytes      # BOM 是原文件的属性，写回去

    # 备份先行（§4.4 步骤 5 必须在写入之前）
    backup = kb_backup(data_root, vault_row, rel_posix, before,
                       meta={"action": action, "citizen_id": citizen_id or KB_UNATTRIBUTED,
                             "reason": (str(reason) if reason else None), "eol": eol})
    written = kb_atomic_write(abs_path, new_bytes, eol=eol)
    verified = kb_sha256_file(abs_path) == written["sha256"]
    if not verified:                        # KB-15：写后必须重读校验（异步成功 ≠ 成功）
        raise KBError("not_writable",
                      f"写后校验失败：{rel_posix} 的实际 sha256 与预期不一致",
                      hint=f"备份在 {backup['backup_path']}，可人工比对或 rollback")

    same_name = _kb_same_name(conn, vault_row, rel_posix) if action == "create" else []
    # N9（第五轮 · 安全 ⚪）：写面补 `allow_sensitive`（与读面同口径、只在为真时记 true）。
    # 为什么值得一行成本：`write_id` 是本项目的撤销凭据、`kb_access` 是唯一的追责面，
    # "授权改了凭证类文件"这件事事后必须能解释——否则只剩一句"谁写的"。
    write_meta = {"reason": (str(reason)[:200] if reason else None), "eol": eol,
                  "same_name_count": len(same_name), "undo_action": "rollback"}
    if allow_sensitive:
        write_meta["allow_sensitive"] = True
    access_id = _kb_log_quiet(
        conn, vault_row, rel=rel_posix, action=action, citizen_id=citizen_id,
        account_id=account_id, via=via,
        bytes_=len(new_bytes), sha256=written["sha256"],
        backup_path=backup["backup_path"],
        meta=write_meta)
    write_id = f"{access_id}-{secrets.token_hex(4)}" if access_id else None
    warnings = []
    finalize_ok = True
    if write_id:
        finalize_ok = _kb_backup_finalize(backup["backup_path"], write_id=write_id,
                                          sha256_after=written["sha256"],
                                          bytes_after=len(new_bytes))
        if not finalize_ok:
            # S9（评审 🟡-N4）：备份元数据写失败 = 这个 write_id 在 rollback 里恒不被认。
            # 不能说成"你的凭据不对"，要让渲染者当场看见、并给出可执行的手工恢复路径。
            warnings.append(
                f"备份元数据写入失败（{backup['backup_path']}/meta.json）："
                f"`fish kb rollback {write_id}` 将拒绝该凭据；请按备份目录手工恢复")
    if write_id and finalize_ok:
        undo = f"fish kb rollback {write_id}"
    elif write_id:
        undo = f"撤销凭据不可用（备份元数据写失败）；备份在 {backup['backup_path']}，请手工恢复"
    else:
        undo = f"审计写入失败；备份在 {backup['backup_path']}，手工恢复"
    return {"write_id": write_id, "action": action, "rel": rel_posix,
            "bytes_before": (len(before) if before is not None else None),
            "bytes_after": len(new_bytes),
            "sha256_before": (kb_sha256_bytes(before) if before is not None else None),
            "sha256_after": written["sha256"], "backup_path": backup["backup_path"],
            "verified": verified, "eol": eol, "same_name_warnings": same_name,
            "warnings": warnings, "undo": undo}


# ------------------------------------------------------------------ 回滚（§4.5）


def kb_rollback(conn, data_root, write_id, *, force=False, citizen_id=None,
                via="cli:kb") -> dict:
    """按 `write_id` 撤回一次写——§4.5 / KB-13 的撤销面。

    三条边界（写死，不靠自觉）：
      ① 只撤 `kb_access` 里那一次 `create/append/update`；
      ② 当前 sha256 必须等于"写后值"——否则拒绝（`--force` 才继续，且**先再备份一次**）；
      ③ `create` 的回滚 = 删掉"自己刚建、此后无人改"的文件（这是唯一允许的删除）。

    ## 2026-10-26 第二轮（R4 · 评审 🟠-3 / 安全 F11）：`write_id` 是全串凭据

    原实现只用前缀（`kb_access.id`），8 位随机后缀**不参与校验**——任何 4–32 位十六进制
    都能"通过"。真风险：`index.sqlite` 重建后 AUTOINCREMENT 重排，旧凭据会命中**另一次写**，
    而 create 的回滚动作是删文件（"用旧凭据删掉别人的新笔记"）。
    现在把备份 `meta.json` 里的 `write_id` 读回来做**全串相等**校验，不符就是 `not_found`
    （文件一个字节不动）。

    ## 2026-10-26 第四轮（S9 · 评审 🟡-N5）：后缀比较大小写不敏感

    正则接受 `A-F`，而比较是全串逐字节——`1-F0B01C55` 与真实凭据只差大小写却被判
    `not_found`，文案还说"你的凭据不对"（误杀 + 归因错）。写侧产出的后缀本来就是小写
    （`secrets.token_hex`），所以这里把**输入**归一成小写、与存储值（也按小写比）对齐：
    形态大小写不再是判据，**内容**才是。
    """
    raw = str(write_id or "").strip()
    m = re.match(r"^(\d+)-([0-9a-fA-F]{4,32})$", raw)
    if not m:
        raise KBError("bad_usage", f"write_id 格式不对：{raw!r}",
                      hint="write_id 形如 17-9f3c1a2b（前缀是 kb_access.id）")
    access_id = int(m.group(1))
    canonical = f"{access_id}-{m.group(2).lower()}"       # S9：存小写、比小写
    row = conn.execute(
        "SELECT id,vault_id,path,action,result,sha256,backup_path FROM kb_access "
        "WHERE id=? AND action IN ('create','append','update')",
        (access_id,)).fetchone()
    if row is None:
        raise KBError("not_found", f"没有这次写入记录：{raw}",
                      hint="write_id 形如 17-9f3c1a2b；fish kb stats 看最近写")
    action, written_sha, backup_path, rel = row[3], row[5], row[6], row[2]
    backup_dir = Path(backup_path or "")
    meta = _kb_backup_meta(backup_dir) if backup_path else {}
    if not backup_path or not backup_dir.is_dir() or not (backup_dir / "meta.json").exists():
        raise KBError("backup_missing", f"备份已缺失，无法回滚：{rel}",
                      hint="备份可能已被清理（人为删除 / `fish kb backup prune --keep-days`）；"
                           "用 Obsidian 的文件恢复或 git 兜底")
    stored_id = str(meta.get("write_id") or "").strip().lower()
    if not stored_id or stored_id != canonical:
        raise KBError("not_found",
                      f"write_id 与备份记录不符（不接受只匹配前缀）：{raw}",
                      hint="核对回执里的完整 write_id（含 8 位后缀）；文件未被改动")
    meta_vault_id = row[1]
    vault_row = conn.execute(f"{_KB_VAULT_SELECT} WHERE vault_id=?",
                             (meta_vault_id,)).fetchone()
    if vault_row is None:
        raise KBError("vault_missing", f"知识库已不在登记表里（{meta_vault_id}），无法定位目标",
                      hint="先 `fish kb vault add` 重新登记，再 rollback")
    vault = _kb_vault_dict(vault_row)
    target = kb_resolve_target(vault, rel, for_write=True)
    abs_path = target["abs"]
    exists = abs_path.exists()
    forced_backup = None
    stale = False
    if exists:
        current_sha = kb_sha256_file(abs_path)
        if current_sha != (written_sha or ""):
            stale = True
            if not force:
                raise KBError("stale_base",
                              f"文件在那之后被改过：{rel}",
                              hint="确认要覆盖请加 `--force`（会先把当前内容再备份一次）")
            forced_backup = kb_backup(
                data_root, vault, rel, abs_path.read_bytes(),
                meta={"action": "force_pre_rollback", "citizen_id": citizen_id or KB_UNATTRIBUTED,
                      "write_id": canonical})["backup_path"]
    payload = backup_dir / "payload"
    restored = False
    removed = False
    if action == "create":
        if exists:
            abs_path.unlink()
            removed = True
    else:
        if not payload.exists():
            raise KBError("backup_missing", f"备份里没有原文件内容：{backup_dir}",
                          hint="该备份不完整；用 Obsidian 的文件恢复兜底")
        original = payload.read_bytes()
        kb_atomic_write(abs_path, original, eol=kb_detect_eol(original))
        ok_sha = kb_sha256_file(abs_path)
        expect = meta.get("sha256_before")
        if expect and ok_sha != expect:
            raise KBError("not_writable", "回滚后校验失败（sha256 与备份记录不一致）",
                          hint=f"备份在 {backup_dir}，请人工比对")
        restored = True
    final_sha = None
    if abs_path.exists():
        final_sha = kb_sha256_file(abs_path)
    elif action == "create":
        final_sha = meta.get("sha256_before")
    _kb_log_quiet(conn, vault, rel=rel, action="rollback", citizen_id=citizen_id,
                  via=via, sha256=final_sha, backup_path=backup_path,
                  meta={"write_id": canonical, "undo_action": action, "stale": stale})
    return {"restored": restored, "removed": removed, "sha256": final_sha,
            "bytes": (abs_path.stat().st_size if abs_path.exists() else 0),
            "write_id": canonical, "backup_path": backup_path,
            "forced_backup": forced_backup, "stale": stale}


# ------------------------------------------------------------------ stats / doctor（§3.2）


def _kb_scan_counts(vault_row) -> dict:
    """在线就现扫，离线就回退 `last_scan_*`（`source` 标明来源，不冒充实时）。"""
    root = Path(vault_row["path"])
    if not root.is_dir():
        return {"files": vault_row.get("last_scan_files"),
                "notes": vault_row.get("last_scan_notes"),
                "bytes": vault_row.get("last_scan_bytes"),
                "source": "last_scan", "online": False}
    files = notes = total = 0
    for ent in _kb_walk(vault_row, root):
        if ent["is_dir"]:
            continue
        files += 1
        total += int(ent["size"] or 0)
        if str(ent["rel"]).lower().endswith(".md"):
            notes += 1
    return {"files": files, "notes": notes, "bytes": total,
            "source": "live", "online": True}


def kb_stats(conn, key=None) -> dict:
    """规模 / 未读 / 最近变更 / 每库读写计数 / `kb_access` 总行数（§3.2 `stats`）。"""
    vaults = [kb_vault_get(conn, key)] if key else kb_vault_list(conn)
    as_of = datetime.datetime.now().astimezone()
    out = []
    for vault in vaults:
        counts = _kb_scan_counts(vault)
        opened = conn.execute(
            "SELECT COUNT(DISTINCT path) FROM kb_access WHERE vault_id=? "
            "AND action='open' AND result='ok'", (vault["vault_id"],)).fetchone()[0]
        writes = conn.execute(
            "SELECT COUNT(*) FROM kb_access WHERE vault_id=? "
            "AND action IN ('create','append','update') AND result='ok'",
            (vault["vault_id"],)).fetchone()[0]
        rollbacks = conn.execute(
            "SELECT COUNT(*) FROM kb_access WHERE vault_id=? AND action='rollback'",
            (vault["vault_id"],)).fetchone()[0]
        reads = conn.execute(
            "SELECT COUNT(*) FROM kb_access WHERE vault_id=? "
            "AND action IN ('open','tree','search','search_hit')",
            (vault["vault_id"],)).fetchone()[0]
        unread = None
        if counts["notes"] is not None:
            unread = max(0, int(counts["notes"]) - int(opened))
        out.append({"name": vault["name"], "vault_id": vault["vault_id"],
                    "mode": vault["mode"], "path": vault["path"],
                    "files": counts["files"], "notes": counts["notes"],
                    "bytes": counts["bytes"], "counts_source": counts["source"],
                    "online": counts["online"], "opened_paths": opened,
                    "unread_notes": unread, "reads": reads, "writes": writes,
                    "rollbacks": rollbacks,
                    "last_scan_at": vault.get("last_scan_at")})
    access_rows = conn.execute("SELECT COUNT(*) FROM kb_access").fetchone()[0]
    recent = [{"id": r[0], "ts": r[1], "path": r[2], "action": r[3],
               "result": r[4], "citizen_id": r[5]}
              for r in conn.execute(
                  "SELECT id, ts, path, action, result, citizen_id FROM kb_access "
                  "ORDER BY id DESC LIMIT 10").fetchall()]
    lines = ["# 知识库规模（stats）", "", f"> as_of {iso_local(as_of)}",
             f"> kb_access 总行数：{access_rows}"]
    for item in out:
        lines += ["", f"## {item['name']}（mode={item['mode']}）",
                  f"- 路径：{item['path']}",
                  f"- 规模：{item['files']} 文件 / {item['notes']} 笔记 / "
                  f"{item['bytes']} 字节（来源：{item['counts_source']}）",
                  f"- 未读（估算）：{item['unread_notes']} 篇（无 open 记录）",
                  f"- 读 {item['reads']} 次 / 写 {item['writes']} 次 / "
                  f"回滚 {item['rollbacks']} 次"]
    lines += ["", "## 最近变更", ""]
    if recent:
        for item in recent:
            lines.append(f"- {item['ts']} {item['action']}({item['result']}) "
                         f"`{item['path']}` by {item['citizen_id']}")
    else:
        lines.append("- （还没有 kb_access 记录）")
    return {"as_of": iso_local(as_of), "vaults": out, "access_rows": access_rows,
            "recent": recent, "text": "\n".join(lines)}


def _kb_app_running() -> bool:
    """Obsidian 进程是否在跑（**不启动它**：`Obsidian.com --version` 可能真把 app 拉起来）。"""
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Obsidian.exe", "/NH"],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return False
    return "obsidian.exe" in (proc.stdout or "").lower()


def _kb_obsidian_cli() -> dict:
    """探测 Obsidian CLI（KB-14）：app 未运行 → `available=False`，读写照常。"""
    path = None
    for cand in ("Obsidian.com", "Obsidian.exe", "obsidian"):
        found = shutil.which(cand)
        if found:
            path = found
            break
    if not path:
        return {"available": False, "path": None, "version": None,
                "reason": "not_in_path"}
    if not _kb_app_running():
        return {"available": False, "path": path, "version": None,
                "reason": "app_not_running"}
    return {"available": True, "path": path, "version": None, "reason": None}


def _kb_backup_stats(data_root, vault_id) -> dict:
    """备份目录读数（N2 · 安全 F10）：体积 / 份数 / 最旧 / 最新。

    只读**目录与 meta**，不读 payload——doctor 不该因为备份多就把库读一遍。
    `data_root` 没给（旧调用形态）时如实返回 `available=False`，不猜。
    """
    if data_root in (None, ""):
        return {"available": False, "dirs": None, "bytes": None,
                "oldest": None, "newest": None}
    base = Path(data_root) / "_backups" / "kb" / str(vault_id or "")
    if not base.is_dir():
        return {"available": True, "dir": str(base), "dirs": 0, "bytes": 0,
                "oldest": None, "newest": None}
    dirs = [p for p in base.iterdir() if p.is_dir()]
    total = 0
    stamps = []
    for d in dirs:
        total += _kb_dir_bytes(d)
        meta = _kb_backup_meta(d)
        stamps.append(str(meta.get("ts") or ""))
    stamps = [s for s in stamps if s]
    return {"available": True, "dir": str(base), "dirs": len(dirs), "bytes": total,
            "oldest": (min(stamps) if stamps else None),
            "newest": (max(stamps) if stamps else None)}


def kb_backup_prune(conn, data_root, *, keep_days, dry_run=False) -> dict:
    """**显式**清理回滚点：删掉 meta.ts 早于 `keep_days` 天的备份目录（N2）。

    ## 为什么不做自动清理（裁决 N2）

    自动删备份 = "回滚点可能在你想用它的前一天被系统删掉"。所以清理必须是人/AI
    **显式**跑这条命令；`kb doctor` 只报告体积与最旧/最新，不动手。
    `keep_days <= 0` → `bad_usage`（"保留 0 天"等于"全删"，不该由笔误触发）。
    """
    try:
        days = int(keep_days)
    except (TypeError, ValueError) as exc:
        raise KBError("bad_usage", f"--keep-days 必须是整数，收到 {keep_days!r}",
                      detail=exc)
    if days <= 0:
        raise KBError("bad_usage", "keep-days 必须大于 0（不做“保留 0 天=全删”）",
                      hint="例如 `fish kb backup prune --keep-days 30`")
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=days)).strftime("%Y%m%dT%H%M%SZ")
    scanned = removed = kept = freed = 0
    errors = []
    for vault in kb_vault_list(conn):
        base = Path(data_root) / "_backups" / "kb" / vault["vault_id"]
        if not base.is_dir():
            continue
        for d in sorted(p for p in base.iterdir() if p.is_dir()):
            scanned += 1
            meta = _kb_backup_meta(d)
            ts = str(meta.get("ts") or "")
            if not ts:
                kept += 1                       # 读不到 ts：不猜、不删
                continue
            if ts >= cutoff:
                kept += 1
                continue
            size = _kb_dir_bytes(d)
            if dry_run:
                removed += 1
                freed += size
                continue
            try:
                shutil.rmtree(d)
            except OSError as exc:
                errors.append({"dir": str(d), "reason": _exc_text(exc)})
                continue
            removed += 1
            freed += size
    return {"keep_days": days, "cutoff": cutoff, "scanned": scanned,
            "removed": removed, "kept": kept, "bytes_freed": freed,
            "dry_run": bool(dry_run), "errors": errors}


def _kb_dir_bytes(path) -> int:
    """目录树字节合计（与 `_kb_backup_stats` 同口径；读不到的文件跳过，不炸）。"""
    total = 0
    for f in Path(path).rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue
    return total


def kb_doctor(conn, *, data_root=None) -> dict:
    """逐 vault 体检 + obsidian_cli 探测。**诊断不因"发现问题"而失败**（exit 0）。

    检查项（§3.2）：存在 / 可写 / 忽略规则 / 条目统计 / 同名笔记 / 超限文件 /
    `obsidian_cli=available|unavailable` / **备份体积与最旧最新**（N2：只读，不清理）。
    """
    as_of = datetime.datetime.now().astimezone()
    cli = _kb_obsidian_cli()
    items = []
    for vault in kb_vault_list(conn):
        root = Path(vault["path"])
        exists = root.is_dir()
        writable = bool(exists and os.access(root, os.W_OK))
        counts = _kb_scan_counts(vault)
        same_name = {}
        oversize = []
        if exists:
            limit = int(vault.get("max_open_bytes") or KB_DEFAULT_MAX_OPEN_BYTES)
            for ent in _kb_walk(vault, root):
                if ent["is_dir"]:
                    continue
                name = Path(ent["rel"]).name.casefold()
                if str(ent["rel"]).lower().endswith(".md"):
                    same_name.setdefault(name, []).append(ent["rel"])
                if int(ent["size"] or 0) > limit and len(oversize) < 5:
                    oversize.append({"rel": ent["rel"], "bytes": int(ent["size"])})
        dupes = [{"name": k, "paths": v[:3]}
                 for k, v in sorted(same_name.items()) if len(v) > 1][:5]
        backups = _kb_backup_stats(data_root, vault["vault_id"])
        items.append({"name": vault["name"], "vault_id": vault["vault_id"],
                      "path": vault["path"], "mode": vault["mode"],
                      "exists": exists, "writable": writable,
                      "ignore_globs": vault.get("ignore_globs") or [],
                      "max_open_bytes": vault.get("max_open_bytes"),
                      "files": counts["files"], "notes": counts["notes"],
                      "bytes": counts["bytes"], "counts_source": counts["source"],
                      "same_name": dupes, "oversize": oversize,
                      "backups": backups,
                      "last_scan_at": vault.get("last_scan_at")})
    lines = ["# 知识库体检（doctor）", "", f"> as_of {iso_local(as_of)}",
             f"> obsidian_cli={'available' if cli['available'] else 'unavailable'}"
             + (f"（{cli['reason']}）" if cli.get("reason") else ""),
             "> 诊断不会因为发现问题而失败（exit 0）；读写不依赖 Obsidian CLI。"]
    if not items:
        lines += ["", "还没有登记任何知识库（`fish kb vault add`）。"]
    for item in items:
        lines += ["", f"## {item['name']}（mode={item['mode']}）",
                  f"- 路径：{item['path']}",
                  f"- 存在：{'是' if item['exists'] else '否（offline）'}"
                  f"｜可写：{'是' if item['writable'] else '否'}"
                  f"｜mode={item['mode']}",
                  f"- 忽略规则：{', '.join(item['ignore_globs']) or '（无）'}",
                  f"- 条目：{item['files']} 文件 / {item['notes']} 笔记 / "
                  f"{item['bytes']} 字节（{item['counts_source']}）",
                  f"- 单文件上限：{item['max_open_bytes']} B"]
        lines.append("- 同名笔记：" + ("；".join(
            f"{d['name']} ×{len(d['paths'])}" for d in item["same_name"])
            if item["same_name"] else "无"))
        lines.append("- 超限文件：" + ("；".join(
            f"`{o['rel']}` {o['bytes']} B" for o in item["oversize"])
            if item["oversize"] else "无"))
        bk = item["backups"]
        if not bk["available"]:
            lines.append("- 备份：未提供 data_root，未统计")
        else:
            lines.append(
                f"- 备份：{bk['dirs']} 份 / {bk['bytes']} B"
                f"｜最旧 {bk['oldest'] or '—'}｜最新 {bk['newest'] or '—'}"
                "（**永不自动清理**；要清请显式 `fish kb backup prune --keep-days N`）")
    return {"as_of": iso_local(as_of), "obsidian_cli": cli, "vaults": items,
            "text": "\n".join(lines)}


# ------------------------------------------------------------------ CLI（§3.2）


def _kb_exit_code(exc) -> int:
    """域错误 1 / 用法错误 2（K9）：只认 code，不认文案。"""
    return 2 if getattr(exc, "code", "") == "bad_usage" else 1


def _kb_read_content_file(path) -> str:
    """读 `--content-file`：UTF-8（有 BOM 也吃）。二进制/非 UTF-8 → bad_encoding。"""
    p = Path(str(path))
    if not p.exists():
        raise KBError("not_found", f"内容文件不存在：{p}", hint="--content-file <本地文件>")
    raw = p.read_bytes()
    detected = kb_detect_text(raw, ext=p.suffix)
    if detected["media"]:
        raise KBError("media", f"内容文件不是文本（{detected['reason']}）")
    if not detected["is_text"]:
        raise KBError("bad_encoding", "内容文件不是可识别文本编码", hint="另存为 UTF-8")
    return raw.decode(detected["encoding"])


def _kb_parse_frontmatter_args(pairs) -> dict:
    out = {}
    for raw in pairs or ():
        text = str(raw or "")
        if "=" not in text:
            raise KBError("bad_usage", f"--frontmatter 需要 k=v 形式，收到 {text!r}",
                          hint='例如 --frontmatter tags=认知')
        key, value = text.split("=", 1)
        key = key.strip()
        if not key:
            raise KBError("bad_usage", f"--frontmatter 的键为空：{text!r}")
        out[key] = value
    return out


def _kb_vault_line(vault) -> str:
    flags = []
    if vault.get("is_default"):
        flags.append("default")
    if vault.get("mode") == "rw":
        flags.append("rw")
    tail = ("｜" + "、".join(flags)) if flags else ""
    scan = vault.get("last_scan_at") or "未扫描"
    return (f"{vault['name']}\t`{vault['path']}`\tmode={vault['mode']}\t"
            f"最近扫描={scan}{tail}\tvault_id={vault['vault_id']}")


def _kb_cmd_vault(conn, args) -> int:
    act = getattr(args, "kb_vault_action", None)
    if act == "add":
        vault = kb_vault_add(conn, name=args.name, path=args.path, mode=args.mode,
                             ignore=args.ignore or (), is_default=args.default,
                             note=args.note)
        _kb_log_quiet(conn, vault, rel="", action="vaults", via="cli:kb",
                      meta={"reason": "vault_add"})
        print(f"已登记\t{_kb_vault_line(vault)}")
        print("（只登记，磁盘一个字节都没动）")
        return 0
    if act == "list":
        vaults = kb_vault_list(conn)
        if not vaults:
            print("还没有登记任何知识库。登记入口：fish kb vault add --name <n> "
                  "--path <绝对路径> [--mode ro|rw] [--default]")
            return 0
        for vault in vaults:
            print(_kb_vault_line(vault))
        print(f"共 {len(vaults)} 个知识库")
        return 0
    if act == "set-default":
        vault = kb_vault_set_default(conn, args.key)
        _kb_log_quiet(conn, vault, rel="", action="vaults", via="cli:kb",
                      meta={"reason": "set_default"})
        print(f"默认知识库 → {vault['name']}（{vault['vault_id']}）")
        return 0
    if act == "set-mode":
        vault = kb_vault_set_mode(conn, args.key, args.mode)
        _kb_log_quiet(conn, vault, rel="", action="vaults", via="cli:kb",
                      meta={"reason": f"set_mode:{args.mode}"})
        print(f"{vault['name']} → mode={vault['mode']}")
        if vault["mode"] == "rw":
            print("提示：rw = 允许 AI 写入。每次写都会先备份，可用 "
                  "`fish kb rollback <write_id>` 撤回。")
        return 0
    if act == "scan":
        res = kb_vault_scan(conn, args.key)
        print(f"扫描完成：{res['vault']['name']} → {res['files']} 文件 / "
              f"{res['notes']} 笔记 / {res['bytes']} 字节（{res['ms']} ms，不读正文）")
        print(f"已写入 last_scan_*：{res['scanned_at']}")
        return 0
    if act == "remove":
        res = kb_vault_remove(conn, args.key)
        print(f"已移除注册：{res['name']}（{res['vault_id']}）")
        print(f"磁盘未动：{res['path']}（审计 kb_access 仍可读）")
        return 0
    raise KBError("bad_usage", f"未知 vault 子命令：{act!r}")


def _kb_cmd_tree(conn, args) -> int:
    vault = kb_vault_get(conn, args.vault)
    res = kb_tree(conn, vault, rel=args.path or "", depth=args.depth,
                  max_chars=args.max_chars, via="cli:kb")
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    print(res["text"])
    scan = res["scan"]
    print("")
    print(f"合计：{scan['files']} 文件 / {scan['bytes']} 字节 / {scan['ms']} ms"
          + ("（离线：上次扫描数）" if scan.get("stale") else "")
          + f"｜chars={res['chars']} tokens={res['tokens']}"
          + f" truncated={res['truncated']}")
    return 0


def _kb_cmd_toc(conn, args) -> int:
    """`fish kb toc`（T05）：目录快照——分类概览 + 每组前 3 标题 + 分页。"""
    vault = kb_vault_get(conn, args.vault)
    res = kb_toc(conn, vault, rel=args.path or "", depth=args.depth,
                 limit=args.limit, offset=args.offset,
                 max_chars=args.max_chars, via="cli:kb")
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    print(res["text"])
    print("")
    print(f"合计：{res['total']} 项（显示 {res['shown']}）"
          + f"｜chars={res['chars']} tokens={res['tokens']}"
          + f" truncated={res['truncated']}")
    return 0


def _kb_cmd_open(conn, args) -> int:
    vault = kb_vault_get(conn, args.vault)
    # N7（第五轮）：这里**不做** `bool()` 强转——授权旗统一由 `kb_open` 里的
    # `_strict_bool` 解析，CLI 不再是"另一条转换规则"（store_true 本来给的就是 bool）。
    res = kb_open(conn, vault, args.path, max_chars=args.max_chars,
                  allow_sensitive=getattr(args, "allow_sensitive", False),
                  via="cli:kb")
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    for line in res["statements"]:
        print(f"> {line}")
    print("")
    print(res["text"])
    return 0


def _kb_cmd_search(conn, args) -> int:
    vault = kb_vault_get(conn, args.vault)
    res = kb_search(conn, vault, args.query, limit=args.limit, via="cli:kb")
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    print(res["text"])
    return 0


def _kb_cmd_write(conn, data_root, args) -> int:
    vault = kb_vault_get(conn, args.vault)
    action = "create" if args.create else ("append" if args.append else "update")
    content = _kb_read_content_file(args.content_file)
    frontmatter = _kb_parse_frontmatter_args(args.frontmatter)
    res = kb_write(conn, data_root, vault, args.path, action=action, content=content,
                   frontmatter=frontmatter, base_sha256=args.base_sha256,
                   reason=args.reason,
                   # N7（第五轮）：同 `_kb_cmd_open`，转换只在 `_strict_bool` 一处发生。
                   allow_sensitive=getattr(args, "allow_sensitive", False),
                   via="cli:kb")
    print(f"① 改了：{res['action']} `{res['rel']}` "
          f"{res['bytes_before'] or 0} → {res['bytes_after']} B"
          f"（sha256 {res['sha256_after'][:12]}，eol={res['eol']}，"
          f"校验={'通过' if res['verified'] else '失败'}）")
    print(f"② 备份：{res['backup_path']}")
    print(f"③ 撤销：{res['undo']}")
    for item in res["same_name_warnings"]:
        print(f"⚠ 同名笔记已存在：{item['rel']}（未覆盖，仅提示）")
    # S9：备份元数据写失败等"写成功但降级"的事实必须出现在回执里（不许静默）
    for warning in res.get("warnings") or []:
        print(f"⚠ {warning}", file=sys.stderr)
    return 0


def _kb_cmd_rollback(conn, data_root, args) -> int:
    res = kb_rollback(conn, data_root, args.write_id, force=args.force, via="cli:kb")
    if res["removed"]:
        print(f"已删除（撤销新建）：{res['write_id']}")
    elif res["restored"]:
        print(f"已恢复：{res['write_id']} → {res['bytes']} B"
              f"（sha256 {str(res['sha256'])[:12]}）")
    else:
        print(f"无需恢复（文件已不存在）：{res['write_id']}")
    print(f"用的备份：{res['backup_path']}")
    if res.get("forced_backup"):
        print(f"⚠ --force：改动后的版本已另存：{res['forced_backup']}")
    return 0


def _kb_cmd_stats(conn, args) -> int:
    res = kb_stats(conn, args.vault)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    print(res["text"])
    return 0


def _kb_cmd_doctor(conn, data_root, args) -> int:
    res = kb_doctor(conn, data_root=data_root)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    print(res["text"])
    return 0


def _kb_cmd_backup(conn, data_root, args) -> int:
    """`fish kb backup prune --keep-days N`（N2）：**显式**清理旧回滚点。

    默认什么都不做：保留策略只提供命令，不提供"自动删"（回滚点是安全网，
    自动清理等于让安全网自己挑时间消失）。
    """
    act = getattr(args, "kb_backup_action", None)
    if act != "prune":
        raise KBError("bad_usage", f"未知 backup 子命令：{act!r}（只有 prune）")
    res = kb_backup_prune(conn, data_root, keep_days=args.keep_days,
                          dry_run=bool(getattr(args, "dry_run", False)))
    tag = "（演练，未删）" if res["dry_run"] else ""
    print(f"{tag}保留 {res['keep_days']} 天（截止 {res['cutoff']}）："
          f"扫描 {res['scanned']} 份 / 删除 {res['removed']} 份 / 保留 {res['kept']} 份"
          f" / 释放 {res['bytes_freed']} B")
    for err in res["errors"][:10]:
        print(f"  失败 {err['dir']}：{err['reason']}", file=sys.stderr)
    return 0 if not res["errors"] else 1

# ------------------------------------------------------------------ 导出面
# `_IMPORTED` 只列**import 进来的**名字：`__all__` = globals 里除它以外的全部
# （常量 + 函数 + KBError + _strict_bool 都在内）。加一个 kb 符号不用改这里，
# 也就不会出现"新函数忘了导出"的静默缺口。
_IMPORTED = frozenset({
    # 2026-10-27：加了 `import functools`（给 `_kb_judge_component` 上 lru_cache），
    # **必须同步登记到这里**——否则它会被 `__all__` 当成导出面，
    # J1（fish 侧缺名）与 J1b（再导出清单不一致）会同时红。
    "datetime", "fnmatch", "functools", "hashlib", "json", "ntpath", "os", "re",
    "secrets", "shutil", "sqlite3", "stat", "subprocess", "sys", "time", "Path",
    "count_tokens", "iso_local", "now_utc", "ulid", "_exc_text",
    # 自己也在 globals 里，必须显式排除，否则会被算进 __all__
    "_IMPORTED",
})

__all__ = sorted(n for n in globals() if not n.startswith("__") and n not in _IMPORTED)
