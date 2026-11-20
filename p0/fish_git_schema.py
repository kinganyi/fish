"""fish 引入 git：仓库身份层的**纯 DDL**（2026-11-20 · Phase 2）。

## 为什么单独一个模块

`fish_schema.py` 是 DDL 唯一来源，但它的 J4 窗口只剩 8 行余量（480–700，
当前 692）。五张表约 80 行放不进去，而**抬窗是最后手段**（「方向只许降」）。

拆出来的前提是分层不能倒：`fish_schema` 要 import 本模块拼 SCHEMA，
所以本模块必须**比 fish_schema 更底层**——只含字符串常量，不 import 任何东西。

## 本模块的边界

- 只有 `_*_DDL` 常量与由它们拼出的 `_GIT_SCHEMA`，**没有函数、没有 IO**
- 不 import 任何 fish_* 模块（连 fish_common 都不需要）
- 索引在这里一并给出：建表与建索引是同一件事（`session_context` 那条教训：
  全新表不需要 fish_db 的 ensure 函数，`executescript(SCHEMA)` 就够）

## ⚠️ 待办：窗口登记

本模块目前**没有**登记进 `test_split_contract.py` 的行数窗口（J4 是逐个模块
手写检查的，未登记即不检查）。这意味着它眼下不受「不许长成第二个巨石」的保护。
登记需要改测试文件（门禁资产），按纪律须用户确认——已作为待办记录在
`85-Phase2-仓库身份层-实施记录.md`。
"""

# ── 五张表的设计依据（全部来自 83-方案 v2.0 第四章，每条都有实测支撑）──
#
# 1. 仓库身份用**规范化远端 URL**，不用路径
#    实测：40 个 git 仓库，fish 只解析出 4 个（12%）；路径会漂（目录改名/
#    junction/大小写/8.3 短名），远端 URL 在 clone 之后稳定。
#
# 2. identity 允许 NULL + 独立代理键
#    反例：空仓库（git init 无 commit）在 `NOT NULL UNIQUE` 下**根本无法登记**。
#    `first_sha` 只作弱证据——shallow 仓库 unshallow 后首 SHA 会变。
#
# 3. ownership 默认 `unknown`，不是 `owned`
#    这条是安全默认：发现器一跑，ComfyUI、deepseek-harness 这些第三方仓库
#    全会被标成「自己的」。P10：external 一律只读，所以默认必须保守。
#
# 4. 一个仓库 N 个远端（`git_remote`）
#    实测反例：6~13 个仓库**无远端**；1 个代理镜像（ghfast.top）；1 个镜像站
#    （gitcode.com，非上游身份）；2 个双 remote（origin + ssh-origin）；
#    还有远端本身是本地路径的。单值 remote_url 表达不了这些。
#
# 5. 一个仓库 N 份 checkout（`git_checkout`）
#    脏净/分支/ahead 都是**某份工作副本**的属性，不是仓库的属性。
#    两份 clone 可以一个净一个脏；v1.0 方案把它挂在 repo 上是设计缺陷。
#
# 6. 观测必须带采样时刻 + 四态（`git_obs`）
#    判不到 ≠ 干净。`unknown`（没采到）/`degraded`（采到但不可靠：bare /
#    shallow / partial clone / 权限 / 损坏 / 超时）必须与 `clean` 分开。
#    ahead/behind 允许 NULL——没有 upstream 的本地分支算不出来，写 0 就是编。

_GIT_REPO_DDL = """CREATE TABLE IF NOT EXISTS git_repo (
  repo_id            INTEGER PRIMARY KEY AUTOINCREMENT,
  identity           TEXT,              -- 规范化远端 URL；NULL = 身份未定（不猜）
  identity_kind      TEXT NOT NULL DEFAULT 'unknown'
                     CHECK (identity_kind IN ('remote','local','unknown')),
  identity_source    TEXT NOT NULL DEFAULT 'discovered'
                     CHECK (identity_source IN ('manual','discovered','inferred')),
  first_sha          TEXT,              -- 弱证据：shallow 仓 unshallow 后会变
  display_name       TEXT,
  ownership          TEXT NOT NULL DEFAULT 'unknown'
                     CHECK (ownership IN ('owned','external','unknown')),
  ownership_evidence TEXT,              -- owned/external 的判定依据（必须可查）
  missing_since      TEXT,              -- 仓库从磁盘消失的时刻（tombstone）
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL
);"""

# identity 的唯一性用**部分索引**（WHERE identity IS NOT NULL）：
# 空仓库允许有多行 identity=NULL，但不能有两行同一个 identity。
_GIT_REPO_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_git_repo_identity "
    "ON git_repo(identity) WHERE identity IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_git_repo_ownership ON git_repo(ownership)",
)

_GIT_REMOTE_DDL = """CREATE TABLE IF NOT EXISTS git_remote (
  repo_id    INTEGER NOT NULL REFERENCES git_repo(repo_id),
  name       TEXT NOT NULL,             -- origin / ssh-origin / upstream / ...
  url        TEXT NOT NULL,
  url_kind   TEXT NOT NULL DEFAULT 'direct'
             CHECK (url_kind IN ('direct','proxy','mirror','local_path')),
  created_at TEXT NOT NULL,
  PRIMARY KEY (repo_id, name)
);"""

_GIT_REMOTE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_git_remote_url ON git_remote(url)",
)

# 身份别名：`git remote set-url`（换协议/换代理）、仓库发布（本地→加 remote）、
# fork 转正，都会让「同一个仓库」算出第二个 identity。没有别名表就只能出现两行。
_GIT_REPO_ALIAS_DDL = """CREATE TABLE IF NOT EXISTS git_repo_alias (
  alias_identity TEXT PRIMARY KEY,
  repo_id        INTEGER NOT NULL REFERENCES git_repo(repo_id),
  reason         TEXT,
  created_at     TEXT NOT NULL
);"""

_GIT_CHECKOUT_DDL = """CREATE TABLE IF NOT EXISTS git_checkout (
  checkout_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  repo_id       INTEGER NOT NULL REFERENCES git_repo(repo_id),
  path          TEXT NOT NULL,          -- 归一化绝对路径（fish_surface.normalize_path）
  path_key      TEXT NOT NULL UNIQUE,   -- 归一后的唯一键（大小写/junction 统一后）
  is_primary    INTEGER NOT NULL DEFAULT 0,
  last_seen_at  TEXT NOT NULL,
  missing_since TEXT
);"""

_GIT_CHECKOUT_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_git_checkout_repo ON git_checkout(repo_id)",
)

_GIT_OBS_DDL = """CREATE TABLE IF NOT EXISTS git_obs (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  checkout_id     INTEGER NOT NULL REFERENCES git_checkout(checkout_id),
  sampled_at      TEXT NOT NULL,        -- 采样时刻 UTC（没有它，快照等于谎言）
  branch          TEXT,                 -- detached 时为 NULL
  head_sha        TEXT,
  upstream        TEXT,
  ahead           INTEGER,              -- NULL = 算不出来（无 upstream）
  behind          INTEGER,
  dirty           INTEGER,              -- 1 脏 / 0 净 / NULL 判不到
  staged_count    INTEGER,
  unstaged_count  INTEGER,
  untracked_count INTEGER,
  state           TEXT NOT NULL DEFAULT 'unknown'
                  CHECK (state IN ('clean','dirty','unknown','degraded')),
  degraded_reason TEXT,                 -- bare/shallow/partial/permission/corrupt/timeout
  probe_ms        INTEGER,
  UNIQUE(checkout_id, sampled_at)
);"""

_GIT_OBS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_git_obs_checkout "
    "ON git_obs(checkout_id, sampled_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_git_obs_state ON git_obs(state)",
)

# ── Phase 4：事件与关联（2026-11-20）──────────────────────────────────
#
# ## git_commit 是**派生投影**，不是真源
#
# 真源是 `events` 表里的 `type='git.commit'` 事件；本表可由事件重放重建
# （与 `code_index` 之于 events 是同一种关系，`fish_rebuild.py` 已有先例）。
# 所以它**没有 append-only 触发器**——重建时本来就要能删能写。
#
# `content_root`（tree SHA）是为「抗 commit SHA 失效」：rebase/force-push 会改
# commit SHA 但不改 tree/blob 内容。存下 tree SHA，才能在 SHA 变了之后仍回答
# 「这段内容还在不在」。
_GIT_COMMIT_DDL = """CREATE TABLE IF NOT EXISTS git_commit (
  sha           TEXT PRIMARY KEY,
  repo_id       INTEGER NOT NULL REFERENCES git_repo(repo_id),
  authored_at   TEXT NOT NULL,
  committed_at  TEXT NOT NULL,
  author_email  TEXT,
  author_name   TEXT,
  subject       TEXT,
  is_merge      INTEGER NOT NULL DEFAULT 0,
  files_changed INTEGER,
  insertions    INTEGER,
  deletions     INTEGER,
  branch_seen   TEXT,
  content_root  TEXT,
  event_id      TEXT,
  superseded_by TEXT,
  unreachable_since TEXT
);"""

_GIT_COMMIT_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_git_commit_repo "
    "ON git_commit(repo_id, authored_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_git_commit_root ON git_commit(content_root)",
)

# ## git_link 为什么是独立表，不扩展 `links`
#
# `links` 的两端必须是 `code_index` 里的编码——`gov_apply.py:292` 的健康检查
# （悬空边计数）与 `fish_maintenance.py:423` 的 24h 维护都会强制这一点。
# 把 commit SHA 塞进去会立刻变成「悬空边」，24 小时后被 `suppressed=1` 压掉。
# 独立表是唯一不破坏现有不变量的做法。
#
# `removed_at` 是**软删除**：撤销一次关联要能追溯（J10 判据），物理删除做不到。
# `content_hash` 是 blob 级内容血缘：rebase 改 SHA 不改内容，force-push 后
# 仍能按 (blob_sha, path) 找到「内容还在、SHA 变了」的提交。
_GIT_LINK_DDL = """CREATE TABLE IF NOT EXISTS git_link (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  src_kind     TEXT NOT NULL CHECK (src_kind IN ('memory','board','kb','event')),
  src_id       TEXT NOT NULL,
  dst_kind     TEXT NOT NULL CHECK (dst_kind IN ('commit','repo','checkout')),
  dst_id       TEXT NOT NULL,
  rel          TEXT NOT NULL
               CHECK (rel IN ('implemented_by','produced','revised_in','discussed_in')),
  source       TEXT NOT NULL CHECK (source IN ('explicit','inferred')),
  confidence   REAL NOT NULL DEFAULT 1.0,
  evidence     TEXT,
  content_hash TEXT,
  created_at   TEXT NOT NULL,
  removed_at   TEXT,
  UNIQUE(src_kind, src_id, dst_kind, dst_id, rel)
);"""

_GIT_LINK_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_git_link_src "
    "ON git_link(src_kind, src_id, removed_at)",
    "CREATE INDEX IF NOT EXISTS ix_git_link_dst "
    "ON git_link(dst_kind, dst_id, removed_at)",
)


def _build_schema() -> str:
    """把五张表与它们的索引拼成一段可进 `executescript` 的 SQL。

    两条硬约束（都踩过）：

    1. **前导 `\\n` 是必须的**：调用方（`fish_schema.SCHEMA`）用字符串加法拼接，
       若本段以 `CREATE` 直接起头，拼出来会变成 `...);CREATE TABLE ...` —— 实测
       `sqlite3.OperationalError: near "CREATE": syntax error`。
    2. **每条语句都必须以 `;` 结尾**：用 `";\\n".join(indexes)` 只在**中间**放分号，
       最后一条没有——它和下一段 DDL 撞在一起，报的仍是同一句
       `near "CREATE": syntax error`，但错在**上一段**，很难从报错定位。
       所以这里用「每条各自加 `;` 再拼」而不是 join 分隔符。
    """
    def block(ddl: str, indexes=()) -> str:
        out = ddl.rstrip()
        if not out.endswith(";"):
            out += ";"
        for st in indexes:
            out += "\n" + st.rstrip().rstrip(";") + ";"
        return out

    parts = [
        block(_GIT_REPO_DDL, _GIT_REPO_INDEXES),
        block(_GIT_REMOTE_DDL, _GIT_REMOTE_INDEXES),
        block(_GIT_REPO_ALIAS_DDL),
        block(_GIT_CHECKOUT_DDL, _GIT_CHECKOUT_INDEXES),
        block(_GIT_OBS_DDL, _GIT_OBS_INDEXES),
        block(_GIT_COMMIT_DDL, _GIT_COMMIT_INDEXES),
        block(_GIT_LINK_DDL, _GIT_LINK_INDEXES),
        # events 的 type 索引（方案 §4.3 明确要求）：实测此前只有 event_id 的
        # autoindex，而按 type 找「所有 git.commit 事件」是重放投影的必经查询。
        "CREATE INDEX IF NOT EXISTS ix_events_type ON events(type, seq DESC);",
    ]
    return "\n" + "\n".join(parts) + "\n"


_GIT_SCHEMA = _build_schema()
