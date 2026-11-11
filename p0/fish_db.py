#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""建表 / 补列 / 迁移辅助（2026-11-11 从 `fish.py` 抽出，错题本 E-11 拆包第四刀）。

## 这一族是什么

`fish.py` 启动时要确保库结构与代码一致：缺表就建、缺列就补、旧索引就重建。
这一族共 22 个成员 / 约 540 行，分布在 `fish.py` 里两处相隔很远的位置——
读 `migrate` 的人很难看出它到底依赖了什么。它们**只跟表结构打交道**，
不参与记忆、推送、看板等任何业务判断。

## 为什么不连 `migrate` 一起搬

`migrate` 是编排者，它要调 `_ensure_kb_tables` / `_ensure_kb_views`，
而那两个必须依赖 `fish_kb`（用它的 `_kb_backfill_mandatory_globs`）。
若把它们拉进本模块，依赖方向会变成 `fish_db → fish_kb`——**上行依赖**。
所以：**编排者留在 `fish.py`，纯执行者搬到这里**。

## 依赖方向（J2）

只依赖 `fish_common`（`_board_seen_columns` / `_presence_columns` / `warn_derived`）、
`fish_schema`（全部 DDL 与 `SCHEMA`）与 stdlib。**不 import `fish`**。
"""
import datetime
import json
import re
import sqlite3
import sys
from pathlib import Path

from fish_common import (_board_seen_columns, _key_type, _presence_columns, board_relpath, get_config, now_utc, ulid, warn_derived, warn_ignored)
from fish_schema import (
    AUDIT_DIR_NAME, FALLBACK_PROJECT_NAME, SCHEMA, _ACCESS_LOG_COLS,
    _ACCESS_LOG_DDL, _ACCESS_LOG_INDEXES, _ACTIVITY_INDEXES, _ACTIVITY_SCHEMA,
    _AGENT_ACCOUNTS_DDL, _BOARDS_DDL, _BOARDS_INDEXES, _BOARD_POSTS_DDL,
    _BOARD_POSTS_INDEXES, _BOARD_SCHEMA, _BOARD_SEEN_DDL, _BOARD_SEEN_PK_COLS,
    _BOARD_TABLE_DDLS, _CITIZENS_ARCHIVE_DDL, _CITIZENS_NEW_COLS, _IDENTITY_INDEXES,
    _IDENTITY_SCHEMA, _IDENTITY_TABLE_DDLS, _KB_ACCESS_DDL, _KB_ACCESS_INDEXES,
    _KB_PATH_STATS_COLUMNS, _KB_PATH_STATS_VIEW, _KB_SCHEMA, _KB_VAULT_COLS,
    _KB_VAULT_DDL, _KB_VAULT_INDEXES, _LINKS_COLS, _LINKS_DDL,
    _LINKS_INDEXES, _NAME_ALIASES_DDL, _NAME_SCHEMA, _PRESENCE_DDL,
    _PRESENCE_NEW_COLS, _PRESENCE_PK_COLS, _PRESENCE_V2_COLS, _PRESENCE_V2_DDL,
    _PRESENCE_V3_COLS, _PROJECT_STINTS_DDL, _PUSH_LOG_COLS, _PUSH_LOG_DDL,
    _PUSH_LOG_INDEXES, _PUSH_SCHEMA, _SESSION_TOKENS_DDL, _STINT_OPEN_INDEX_COLS,
    _STINT_OPEN_INDEX_DDL, _STINT_OPEN_INDEX_NAME, _TOOL_ACTIVITY_COLS, _TOOL_ACTIVITY_DDL,
    _WORKSPACE_MAP_DDL, _WORKSPACE_SCHEMA)


_CITIZENS_ALL_COLUMNS = (
    "citizen_id", "display_name", "kind", "role", "host", "status", "created_at",
    "is_owner", "account_id", "device_id", "host_session_key", "last_seen_at",
    "archived_at",
)

def _citizens_all_view_ddl() -> str:
    """citizens_all = 热表 ∪ 归档表。**显式列名，不用 `SELECT *`。**

    为什么不用 `*`：两边列一不同步，`*` 会静默错位（多/少一列就整行右移），
    而显式列名在缺列时直接报 no such column——错得响，才修得动。

    为什么是 UNION ALL 不是 UNION：归档是"搬"不是"复制"，同一个 citizen_id 不该在
    两张表里同时存在；万一真的同时存在，那是搬迁逻辑的真错误，UNION 会把它去重吞掉，
    视图就成了坏数据的遮羞布。宁可让上层看见两条、去查搬迁。
    """
    cols = ", ".join(_CITIZENS_ALL_COLUMNS)
    return ("CREATE VIEW IF NOT EXISTS citizens_all AS "
            f"SELECT {cols} FROM citizens "
            f"UNION ALL SELECT {cols} FROM citizens_archive")

_PRESENCE_SESSION_BACKFILL = (
    "CASE WHEN agent_id IS NOT NULL AND ("
    " agent_id GLOB 'dsh:?*' OR agent_id GLOB 'codebuddy:?*'"
    " OR agent_id GLOB 'workbuddy:?*' OR agent_id GLOB 'cli:?*'"
    " OR agent_id GLOB 'self:?*') THEN agent_id ELSE '' END"
)

class PresenceMigrationError(RuntimeError):
    """presence 迁移失败。**原表一字不动**，调用方决定降级还是中止。

    为什么是独立异常类型：迁移失败有两种处置（步 3 降级 / 步 6 回滚中止），
    调用方必须能区分"迁移没做"与"别的地方出错"，否则只会 `except Exception` 静默吞掉。
    """

def _has_unique_index(conn, table, columns) -> bool:
    """表上是否已有覆盖这几列的唯一索引（**含 CREATE TABLE 里 UNIQUE 自动建的**）。

    为什么不用「表是不是本次新建的」来判断：真实路径都是先 executescript(SCHEMA)
    再 migrate()，所以到 migrate 时表**永远已存在**——那样判断会变成恒真，
    于是每张新库上都会多建一条与自动索引重复的唯一索引（白占空间与写入）。
    按语义问「去重约束在不在」才问得准：links 的 DDL 自带 UNIQUE(src,dst,rel)，
    自动索引已经满足去重，不必再加一条。
    """
    want = tuple(columns)
    for row in conn.execute(f"PRAGMA index_list({table})").fetchall():
        index_name, is_unique = row[1], row[2]
        if not is_unique:
            continue
        cols = tuple(r[2] for r in conn.execute(f"PRAGMA index_info({index_name})"))
        if cols == want:
            return True
    return False

def _ensure_push_tables(conn: sqlite3.Connection):
    """幂等：把推送地基层的表建好、补齐缺列、再建索引（新旧库都能反复跑）。

    顺序是硬纪律（就是 citizens.is_owner 那条教训）：
    **先建表 → 再补列 → 最后建索引**。反过来做，旧库里一张「同名但缺列」的表
    就会让 CREATE INDEX 报 no such column，整个升级路径崩在启动时。

    索引只放这里、不放 SCHEMA：SCHEMA 先于 migrate 执行，旧库的列还没补齐。
    """
    for table, ddl, cols, indexes in (
            ("memory_access_log", _ACCESS_LOG_DDL, _ACCESS_LOG_COLS, _ACCESS_LOG_INDEXES),
            ("links", _LINKS_DDL, _LINKS_COLS, _LINKS_INDEXES),
            ("push_log", _PUSH_LOG_DDL, _PUSH_LOG_COLS, _PUSH_LOG_INDEXES)):
        if not conn.execute(f"PRAGMA table_info({table})").fetchall():
            conn.execute(ddl)          # 旧库根本没这张表：按 SCHEMA 的严格版建
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        for stmt in indexes:
            conn.execute(stmt)
        if table == "links" and not _has_unique_index(conn, "links", ("src", "dst", "rel")):
            # 更早的实验版可能建了一张**没有** UNIQUE 的 links 表：补唯一索引把它锁住，
            # 否则 add_link 的 INSERT OR IGNORE 去重会静默失效（边越写越多）。
            # 旧表里若已有重复边，建唯一索引会失败——此时不阻断升级（去重留给后续 rebuild）。
            try:
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_links_edge ON links(src,dst,rel)")
            except sqlite3.DatabaseError as exc:
                warn_ignored('fish.py:956 _ensure_push_tables', exc)

def _presence_pk_cols_of(conn, table) -> tuple:
    """某张表的主键列（按 `PRAGMA table_info` 的 pk 序）。表不存在 → `()`。"""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return ()
    keyed = sorted((int(r[5] or 0), str(r[1])) for r in rows if int(r[5] or 0) > 0)
    return tuple(name for _seq, name in keyed)

def _presence_pk_cols(conn) -> tuple:
    """`presence` 当前主键列（按序）。**三列 = 已迁移，两列 = 未迁移/已降级**。"""
    return _presence_pk_cols_of(conn, "presence")

def _presence_data_root(conn):
    """这个连接指向的数据目录（拿不到 → `None` → 迁移**只补列、不重建**）。

    为什么不用 `conn_data_dir`：它对内存库会回落到 `DEFAULT_ROOT`（生产 data 目录），
    于是测试里的 `:memory:` 连接会把备份写进生产目录——那是最难发现的一类污染。
    """
    try:
        for row in conn.execute("PRAGMA database_list").fetchall():
            if row[1] == "main" and row[2]:
                return Path(row[2]).parent
    except sqlite3.Error:
        return None
    return None

def _presence_backup(conn, data_root) -> str:
    """迁移前的库快照，返回路径；失败抛 `PresenceMigrationError`（**不迁移**）。

    为什么用 SQLite 官方 backup API 而不是文件拷贝：WAL 模式下直接 copy 会拿到
    **撕裂快照**（拷到一半的 WAL 还没合并），还原回去是一份不一致的库。
    命名与既有 `data/_backups/index-before-*.sqlite` 同风格；**永不自动删**
    （回滚点是人的凭据，不是缓存——与 `kb backup` 同一条纪律）。
    """
    if data_root is None:
        raise PresenceMigrationError("拿不到数据目录，无法备份 → 本次不迁移（宁可不升级）")
    out_dir = Path(data_root) / "_backups"
    stamp = datetime.datetime.now().strftime("%y%m%d-%H%M%S")
    dest = out_dir / f"index-before-presence-session-{stamp}.sqlite"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = sqlite3.connect(str(dest))
        try:
            conn.backup(dst)
        finally:
            dst.close()
    except (OSError, sqlite3.Error) as exc:
        raise PresenceMigrationError(f"备份失败（{dest}）：{exc}") from exc
    return str(dest)

def _board_seen_backup(conn, data_root) -> str:
    """迁移前的库快照（SQLite backup API，WAL 下不撕裂）；失败 → 不迁移。"""
    if data_root is None:
        raise PresenceMigrationError("拿不到数据目录，无法备份 → 本次不迁移（宁可不升级）")
    out_dir = Path(data_root) / "_backups"
    stamp = datetime.datetime.now().strftime("%y%m%d-%H%M%S")
    dest = out_dir / f"index-before-board-seen-session-{stamp}.sqlite"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = sqlite3.connect(str(dest))
        try:
            conn.backup(dst)
        finally:
            dst.close()
    except (OSError, sqlite3.Error) as exc:
        raise PresenceMigrationError(f"备份失败（{dest}）：{exc}") from exc
    return str(dest)

def _ensure_board_seen_session(conn, *, data_root=None) -> dict:
    """幂等：把 `board_seen` 升到三列主键（P0-3：会话维度）。

    状态机与 `_ensure_presence_session` 同构（备份 → 事务内重建 → 三重校验 → 失败回滚）：

      0. 表不存在 → 空操作（全新库由 `_BOARD_SEEN_DDL` 直接建三列）
      1. 已三列主键 → 空操作（幂等出口，不备份、不重建）
      2. 旧两列 → 备份 + 重建（旧行 `session_key=''`）
      3. 有列但主键不符/顺序不符 → `pending`（人工处置，不重建）
      4. 备份失败 → 不迁移（写侧 `_board_seen_write_shape` 自动降级两列）
    """
    result = {"rebuilt": False, "backup": None, "rows_before": 0, "rows_after": 0,
              "pk": (), "pending": None}
    cols = _board_seen_columns(conn)
    if not cols:
        return result
    pk = _presence_pk_cols_of(conn, "board_seen")
    result["pk"] = pk
    rows_before = int(conn.execute("SELECT COUNT(*) FROM board_seen").fetchone()[0] or 0)
    result["rows_before"] = rows_before
    result["rows_after"] = rows_before
    if pk == _BOARD_SEEN_PK_COLS:
        return result
    if "session_key" in cols:
        result["pending"] = "pk_mismatch" if len(pk) == 3 else "rebuild"
        return result
    root = Path(data_root) if data_root is not None else _presence_data_root(conn)
    try:
        conn.commit()
        result["backup"] = _board_seen_backup(conn, root)
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        committed = False
        try:
            conn.execute("DROP TABLE IF EXISTS board_seen_v3")
            head = "CREATE TABLE IF NOT EXISTS board_seen"
            if _BOARD_SEEN_DDL.count(head) != 1:
                raise PresenceMigrationError("_BOARD_SEEN_DDL 形态变了：找不到唯一的建表头")
            conn.execute(_BOARD_SEEN_DDL.replace(head, "CREATE TABLE IF NOT EXISTS board_seen_v3", 1))
            conn.execute(
                "INSERT INTO board_seen_v3 (board_id,citizen_id,session_key,last_post_id,"
                "last_presence_at,updated_at) "
                "SELECT board_id,citizen_id,'',last_post_id,last_presence_at,updated_at "
                "FROM board_seen")
            rows_after = int(
                conn.execute("SELECT COUNT(*) FROM board_seen_v3").fetchone()[0] or 0)
            pk_after = _presence_pk_cols_of(conn, "board_seen_v3")
            if rows_after != rows_before:
                raise PresenceMigrationError(
                    f"行数不守恒：{rows_before} → {rows_after}（已回滚，原表未动）")
            if pk_after != _BOARD_SEEN_PK_COLS:
                raise PresenceMigrationError(f"主键列不符：{pk_after}（已回滚）")
            conn.execute("DROP TABLE board_seen")
            conn.execute("ALTER TABLE board_seen_v3 RENAME TO board_seen")
            conn.execute("COMMIT")
            committed = True
        except Exception:
            if not committed:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error as exc:      # 事务状态不确定 → 出声（静默清零）
                    warn_derived(conn, "rollback", exc)
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
        result.update({"rebuilt": True, "rows_after": rows_after,
                       "pk": _presence_pk_cols_of(conn, "board_seen")})
        return result
    except PresenceMigrationError:
        raise
    except sqlite3.Error as exc:
        raise PresenceMigrationError(f"重建失败（原表未动）：{exc}") from exc

def _presence_rebuild_ddl(table="presence_v3") -> str:
    """把 v3 的 `_PRESENCE_DDL` 改成建到临时表名——**DDL 只有一份**。

    两份 DDL 必然漂（本项目的老账），所以这里只做改名，不复制声明；万一
    `_PRESENCE_DDL` 形态变了（找不到唯一的建表头）就响亮失败，绝不猜。
    """
    head = "CREATE TABLE IF NOT EXISTS presence"
    if _PRESENCE_DDL.count(head) != 1:
        raise PresenceMigrationError("_PRESENCE_DDL 形态变了：找不到唯一的建表头")
    return _PRESENCE_DDL.replace(head, f"CREATE TABLE IF NOT EXISTS {table}", 1)

def _presence_session_counts(conn) -> tuple:
    """`(有会话归属的行数, 无会话归属的行数)`；列不存在 → `(0, 总行数)`。"""
    cols = _presence_columns(conn)
    if not cols:
        return 0, 0
    total = int(conn.execute("SELECT COUNT(*) FROM presence").fetchone()[0] or 0)
    if "session_key" not in cols:
        return 0, total
    filled = int(conn.execute(
        "SELECT COUNT(*) FROM presence WHERE session_key != ''").fetchone()[0] or 0)
    return filled, total - filled

def _ensure_presence_session(conn, *, data_root=None) -> dict:
    """幂等：把 `presence` 升到 v3（三列主键 + `session_key`）。

    返回 `{"rebuilt", "backup", "rows_before", "rows_after", "backfilled", "blank",
    "pk", "pending"}`；`pending` 非空表示"补列成功但未重建"（留给显式脚本）。

    ## 七步状态机（设计 §2.2）

      0. 表不存在 → 什么都不做（由 `_BOARD_TABLE_DDLS` 直接建 v3，全新库路径）
      1. 已三列主键 → 空操作（幂等出口；**不备份、不重建、零写入**）
      2. 无 `session_key` 列 → 备份 + 事务内重建 + 三重校验（行数 / 主键序 / 回填数）
      3. 有列、两列主键 → 只报 `pending="rebuild"`（半迁移态，人工处置）
      4. 三列但顺序不符 → `pending="pk_mismatch"` + 大声记日志
      5. 备份失败 → **不迁移**（宁可这次不升级，也不拿生产库赌）
      6. 校验不过 → `ROLLBACK`，原表一字不动

    ## 三个容易踩错的点（都写进代码而不是注释）

      · `PRAGMA foreign_keys` 在**事务内会被静默忽略**，所以必须先 `commit()` 再关；
      · 重建会连带 DROP 掉 `ix_presence_last`，索引必须排在本函数**之后**建；
      · 校验不通过时 `ROLLBACK` 之后还要把 FK 重新打开（否则这个连接后续写不校验外键）。
    """
    result = {"rebuilt": False, "backup": None, "rows_before": 0, "rows_after": 0,
              "backfilled": 0, "blank": 0, "pk": (), "pending": None}
    cols = _presence_columns(conn)
    if not cols:
        return result                                  # 步 0：全新库，由建表负责
    pk = _presence_pk_cols(conn)
    result["pk"] = pk
    rows_before = int(conn.execute("SELECT COUNT(*) FROM presence").fetchone()[0] or 0)
    result["rows_before"] = rows_before
    result["rows_after"] = rows_before
    if pk == _PRESENCE_PK_COLS:
        result["backfilled"], result["blank"] = _presence_session_counts(conn)
        return result                                  # 步 1：幂等出口
    if "session_key" in cols:
        result["pending"] = "pk_mismatch" if len(pk) == 3 else "rebuild"
        result["backfilled"], result["blank"] = _presence_session_counts(conn)
        return result                                  # 步 3/4：不重建，只报状态

    root = Path(data_root) if data_root is not None else _presence_data_root(conn)
    try:
        conn.commit()                                  # FK 开关必须在事务外
        expected = int(conn.execute(
            f"SELECT COUNT(*) FROM presence WHERE ({_PRESENCE_SESSION_BACKFILL}) != ''"
        ).fetchone()[0] or 0)
        result["backup"] = _presence_backup(conn, root)
        fill_cols = ", ".join(_PRESENCE_V3_COLS)
        select_cols = ", ".join(
            _PRESENCE_SESSION_BACKFILL if name == "session_key" else name
            for name in _PRESENCE_V3_COLS)
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        committed = False
        try:
            conn.execute("DROP TABLE IF EXISTS presence_v3")
            conn.execute(_presence_rebuild_ddl("presence_v3"))
            conn.execute(
                f"INSERT INTO presence_v3 ({fill_cols}) SELECT {select_cols} FROM presence")
            rows_after = int(
                conn.execute("SELECT COUNT(*) FROM presence_v3").fetchone()[0] or 0)
            pk_after = _presence_pk_cols_of(conn, "presence_v3")
            filled = int(conn.execute(
                "SELECT COUNT(*) FROM presence_v3 WHERE session_key != ''"
            ).fetchone()[0] or 0)
            if rows_after != rows_before:
                raise PresenceMigrationError(
                    f"行数不守恒：{rows_before} → {rows_after}（已回滚，原表未动）")
            if pk_after != _PRESENCE_PK_COLS:
                raise PresenceMigrationError(f"主键列不符：{pk_after}（已回滚）")
            if filled != expected:
                raise PresenceMigrationError(
                    f"回填数不符：预期 {expected}，实际 {filled}（已回滚）")
            conn.execute("DROP TABLE presence")
            conn.execute("ALTER TABLE presence_v3 RENAME TO presence")
            for stmt in _BOARDS_INDEXES:
                conn.execute(stmt)
            bad = conn.execute("PRAGMA foreign_key_check(presence)").fetchall()
            if bad:
                raise PresenceMigrationError(f"外键校验失败：{len(bad)} 行（已回滚）")
            conn.execute("COMMIT")
            committed = True
        except Exception:
            if not committed:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error as exc:      # 事务状态不确定 → 出声（静默清零）
                    warn_derived(conn, "rollback", exc)                               # 事务已不在（例如校验后已 COMMIT）
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
        result.update({"rebuilt": True, "rows_after": rows_after,
                       "backfilled": filled, "blank": rows_after - filled,
                       "pk": _presence_pk_cols(conn)})
        return result
    except PresenceMigrationError:
        raise
    except sqlite3.Error as exc:
        raise PresenceMigrationError(f"重建失败（原表未动）：{exc}") from exc

def _ensure_board_tables(conn: sqlite3.Connection):
    """幂等：把看板三张表建好、补齐缺列、建索引（新旧库都能反复跑）。

    顺序与 `_ensure_push_tables` 同一条纪律：**先建表 → 再补列 → 最后建索引**。
    看板是 2026-10-25 新加的，没有"旧版缺列"的历史包袱，但仍按同一形态写——
    下次加列时不用再想一遍顺序。

    2026-10-26（T02）：`presence` 补 3 列 `auto_*`（自动占用的缓存）。
    补列必须在建索引之前——否则旧库里 `CREATE INDEX ... (auto_seen_at)` 会
    报 no such column，整个升级路径崩在启动时（就是 citizens.is_owner 那条教训）。
    """
    for ddl in _BOARD_TABLE_DDLS:
        # 表名从 DDL 里取，避免再维护一份名单（两份名单就是漂移的起点）
        m = re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", ddl)
        if not m:
            continue
        table = m.group(1)
        if not conn.execute(f"PRAGMA table_info({table})").fetchall():
            conn.execute(ddl)
    have = {r[1] for r in conn.execute("PRAGMA table_info(presence)")}
    for name, decl in _PRESENCE_NEW_COLS:
        if name not in have:
            conn.execute(f"ALTER TABLE presence ADD COLUMN {name} {decl}")
    # 2026-10-26（撞车自撞修复 T01 · 设计 §2.2）：`presence` 升到 v3
    # （三列主键 + `session_key`）。落点在结构段、**索引之前**——重建会连带
    # DROP 掉 `ix_presence_last`，索引语句必须排在重建之后。
    # 失败**不阻断启动**：原表一字未动，写侧自动降级两列（`_presence_write_shape`）；
    # 但"正在降级跑"必须响亮（R3），否则看板悄悄回到"一行/公民"而没人知道。
    presence_migration = {"rebuilt": False, "pending": None, "pk": ()}
    try:
        presence_migration = _ensure_presence_session(conn)
    except PresenceMigrationError as exc:
        print(f"警告：presence 会话维度迁移未完成（本次降级为两列主键）：{exc}",
              file=sys.stderr)
    for stmt in _BOARDS_INDEXES + _BOARD_POSTS_INDEXES:
        conn.execute(stmt)
    if presence_migration.get("pending"):
        print(f"警告：presence 主键待重建（pending="
              f"{presence_migration['pending']}）——同公民多会话会互相覆盖，"
              "跑 `python migrate_presence_session.py --root data --apply`。",
              file=sys.stderr)
    # 2026-10-27（P0-3）：`board_seen` 升三列主键（会话维度）。失败不阻断启动：
    # 写侧 `_board_seen_write_shape` 自动降级两列；但"正在降级跑"必须响亮。
    board_seen_migration = {"rebuilt": False, "pending": None}
    try:
        board_seen_migration = _ensure_board_seen_session(conn)
    except PresenceMigrationError as exc:
        print(f"警告：board_seen 会话维度迁移未完成（本次降级为两列主键）：{exc}",
              file=sys.stderr)
    if board_seen_migration.get("pending"):
        print(f"警告：board_seen 主键待重建（pending="
              f"{board_seen_migration['pending']}）——同公民多会话会互相吞推送。",
              file=sys.stderr)

def _ensure_activity_table(conn: sqlite3.Connection):
    """幂等：建 `tool_activity` + 4 条索引（表 → 列 → 索引 的同一条顺序纪律）。

    旧库特征：表不存在 → `CREATE TABLE IF NOT EXISTS`；**没有历史数据要迁**
    （动作流是本批才开始记的，迁不了也不该编）。
    """
    have = conn.execute("PRAGMA table_info(tool_activity)").fetchall()
    if not have:
        conn.execute(_TOOL_ACTIVITY_DDL)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tool_activity)")}
    for name, decl in _TOOL_ACTIVITY_COLS:
        if name not in cols:
            conn.execute(f"ALTER TABLE tool_activity ADD COLUMN {name} {decl}")
    for stmt in _ACTIVITY_INDEXES:
        conn.execute(stmt)

def _ensure_citizens_all_view(conn: sqlite3.Connection):
    """幂等地建 citizens_all；**定义与列契约不符就重建**（自愈）。

    为什么需要"不符就重建"：`CREATE VIEW IF NOT EXISTS` 只在视图缺席时建，
    已存在的旧定义不会被更新；视图定义一旦与 _CITIZENS_ALL_COLUMNS 漂开，
    查询会静默少列/错位。所以这里拿 PRAGMA 读**视图真实暴露的列**与契约比对。

    为什么建完立刻 `SELECT * FROM citizens_all LIMIT 0`：SQLite 的视图是**惰性校验**的
    ——允许建一个引用了不存在列的视图，错误留到查询时才报（实测：CREATE 成功，
    PRAGMA table_info 才抛 no such column）。不在这里真查一次，坏视图会一路活到
    某个调用方手里。视图是派生对象，在这里重建/报错的代价都可控。

    若**归档表本身**列不对，上面这一查会直接抛错——刻意不自动重建 citizens_archive：
    那张表迟早装着被搬走的行，静默重建 = 静默删数据。宁可升级当场失败让人来看。
    """
    want = list(_CITIZENS_ALL_COLUMNS)
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='view' AND name='citizens_all'").fetchone()
    if exists:
        try:
            have = [r[1] for r in conn.execute("PRAGMA table_info(citizens_all)")]
        except sqlite3.DatabaseError:
            have = []          # 旧定义引用了还不存在的列（半升级态）→ 重建
        if have == want:
            return
        conn.execute("DROP VIEW citizens_all")
    conn.execute(_citizens_all_view_ddl())
    # 把"惰性校验"提前成"立刻校验"：列不齐 / 两表列数不同 → 就在这里响。
    # 这里用 `*` 是刻意的：要验的就是"13 列全拿得到"，不该挑列（视图定义里才禁止 `*`）。
    conn.execute("SELECT * FROM citizens_all LIMIT 0")

def _ensure_stint_open_index(conn: sqlite3.Connection):
    """幂等：把开档期唯一索引统一成**两列版**（citizen_id, project）。

    这是本轮唯一需要"先删再生"的对象，根因是**名字复用、定义变了**：
    旧库里 `ux_stint_open` 是 `ON project_stints(citizen_id) WHERE to_ts IS NULL`，
    而 `CREATE UNIQUE INDEX IF NOT EXISTS` 见同名索引就跳过——不先 DROP，旧的一列
    索引会带着旧语义原地活下来，多项目并行被它拦住（判据 G 就是拿这个场景验的）。

    **顺序硬纪律：先判 → 再 DROP → 最后 CREATE。** 反过来（先 CREATE）什么都不会发生，
    因为 IF NOT EXISTS 让那句成为空操作，随后"DROP 掉再建"才是唯一能改定义的路径。

    为什么 DROP 之后再建一定不会失败（这是本轮唯一被质疑过的一步）：
      · 旧索引（一列）比新索引（两列）**更严**——旧库最多允许同一公民 1 条开档期，
        这种数据在新索引下必然合法（更宽的约束不会拒绝原本更少的数据）；
      · 反过来"旧库里同一公民有多个开档期"在旧索引下**根本插不进去**（那正是旧不变
        量），所以不存在"DROP 之后重建失败"的正常路径；就算有人手工删过旧索引造出
        这种数据，新索引也容得下（多项目并存合法），不会因为"一人多档"而失败；
      · 唯一会让 CREATE 失败的是**同一 citizen + 同一 project 两条开档期**，那在旧索引
        下更不可能存在。→ 结论：DROP → CREATE 是安全的单向升级。

    出现真重复时**不吞异常**：那是数据病，要让人看见。静默跳过索引的后果是
    "以为有唯一约束、其实没有"，比升级当场失败危险得多。

    判据式实现（不拿正则去扒 sqlite_master.sql）：`PRAGMA index_info` 是库自己报的
    列名与列序，与 DDL 文本怎么换行、怎么加引号都无关。
    """
    have = tuple(r[2] for r in conn.execute(
        f"PRAGMA index_info({_STINT_OPEN_INDEX_NAME})"))
    if have == _STINT_OPEN_INDEX_COLS:
        return                              # 已经是两列版：一个字节都不动
    if have:
        # 存在但列不对（旧库一列版 / 更早的实验版）→ 必须先删掉旧定义
        conn.execute(f"DROP INDEX IF EXISTS {_STINT_OPEN_INDEX_NAME}")
    conn.execute(_STINT_OPEN_INDEX_DDL)

def _ensure_identity_tables(conn: sqlite3.Connection):
    """幂等：身份层结构一次到位（新表 → citizens 补列 → 视图 → 索引）。

    顺序仍是那条硬纪律（citizens.is_owner 事故的教训）：**先建表 → 再补列 →
    最后建索引/视图**。视图与 ix_citizens_account 都依赖 citizens 的最终列集，
    提前一步就会撞 `no such column`，整个升级路径崩在启动时。

    刻意**不**为 citizens 本身写"表不存在就建"的分支：migrate 前面几步（is_owner 补列、
    owner 回填）已经在用它，走到这里时它必然存在。建表归 SCHEMA，补列归这里。

    本轮只做结构：不写任何行、不改任何既有查询、不归档、不签发 token。
    """
    for ddl in _IDENTITY_TABLE_DDLS:
        conn.execute(ddl)                      # IF NOT EXISTS：已有的表原样不动

    ccols = {r[1] for r in conn.execute("PRAGMA table_info(citizens)")}
    for name, decl in _CITIZENS_NEW_COLS:
        if name not in ccols:
            conn.execute(f"ALTER TABLE citizens ADD COLUMN {name} {decl}")

    _ensure_citizens_all_view(conn)
    # 开档期唯一索引单独处理：它的**定义换过**（一列 → 两列），必须按 PRAGMA 判断后
    # DROP 重建，不能靠清单里的幂等 CREATE（同名旧索引会让那条成为空操作）。
    _ensure_stint_open_index(conn)
    for stmt in _IDENTITY_INDEXES:
        conn.execute(stmt)

def _ensure_workspace_table(conn: sqlite3.Connection):
    """幂等：建 workspace_map（旧库升级路径）。

    为什么必须走 migrate（而不是只在 SCHEMA 里建）：`CREATE TABLE IF NOT EXISTS` 对
    **已存在**的旧库什么都不做，所以"升级"这件事只能由 migrate 负责——它是每条
    打开路径（CLI 各命令 / MCP server / 测试装配）都会跑的那一步。SCHEMA 里也放一份
    是给全新库用的，两份共用 `_WORKSPACE_MAP_DDL` 这一个常量（第三份拷贝就是漂移起点）。

    这里刻意不做"把旧行里的尾斜杠/大写批量修一遍"的迁移：这张表是本轮新建的，
    **不存在旧行**；将来若真需要改已存数据，那是数据迁移，该单独一件事单独判据。
    """
    conn.execute(_WORKSPACE_MAP_DDL)

def _ensure_name_tables(conn):
    """幂等：建别名表（名字治理的第一层）。

    与 `_ensure_workspace_table` 同一纪律：表结构在两个消费者（SCHEMA 新库 /
    migrate 旧库）各建一次，共用同一份 DDL。索引刻意不加——别名是个位数级别，
    解析在 Python 里做（`_alias_map`），加索引只是给"看起来很专业"付利息。
    """
    conn.executescript(_NAME_ALIASES_DDL)
    conn.commit()

def _ensure_boards_home_paths(conn, *, degraded=None):
    """幂等：把 `boards.path` 归一到"相对 fish-home"的口径（裁决 C3 / §2.4）。

    判据式实现（不扒 `sqlite_master.sql`、不做字符串手术）：**用 `board_relpath` 重算，
    对比才改**。两条理由：
      ① `board_relpath` 是唯一入口，与 `board_ensure` 同源——迁移和写入不可能漂开；
      ② 重复执行零写入（第二次 SELECT 出来全部 already_ok），
         所以挂在 `migrate()` 链尾不会每次都制造 WAL 变更。

    为什么不用 `REPLACE(path,'projects/','10-项目库/')`：字符串手术对 `scope_key` 里
    含 `projects/` 的数据会改错；PRD 的 branch 分支表达式里出现两次 `scope_key`，
    `scope_key` 一变就错位。而 `scope='branch'` 在生产库是 0 行——**从未被执行过的
    分支里的 bug 会一直潜伏到第一次有人开分支看板**（架构设计 A21/A29）。

    ## 未知 scope（2026-10-26 第二轮 R17 · 评审 🟡-2）

    原 docstring 自相矛盾（"一行不碰" vs "抛 ValueError 让人看见"），实际是后者：
    而本函数挂在 `migrate()` 链尾、`migrate()` 被**每条命令**（含 hook）调用 →
    **一行脏数据就能让全系统起不来**。
    现在的口径：**跳过该行**，把原因写进调用方传入的 `degraded` 列表（渲染时可见），
    绝不静默——"看得见"由 degraded 段承担，不由"炸掉整条链"承担。
    返回本次改了几行——供 `layout render` / 交付报告引用，不写库。
    """
    changed = []
    rows = conn.execute(
        "SELECT id, scope, scope_key, period_key, path FROM boards").fetchall()
    for row in rows:
        try:
            want = board_relpath(row[1], row[2], row[3])
        except ValueError as exc:
            if degraded is not None:
                degraded.append({"source": f"boards#{row[0]}",
                                 "reason": f"未知 scope（跳过该行）：{exc}"})
            continue
        if row[4] != want:
            conn.execute("UPDATE boards SET path=? WHERE id=?", (want, row[0]))
            changed.append({"id": row[0], "from": row[4], "to": want})
    conn.commit()
    return changed


# ============================================================
# 事件写入原语（2026-11-11 从 `fish.py` 下沉，E-11 拆包第五刀的前置）
#
# 为什么它必须在 DB 层：`append_event` 是**事件溯源的唯一写入口**（ADR-001），
# 而事件是整套系统的真相源。它只做一件事——往 `events` 表 INSERT 一行，
# 不依赖任何领域概念（记忆 / 看板 / 推送 / 画像）。
#
# 下沉它的直接动机：`fish_profile`（主人画像）必须写事件（否则重建不保真，
# 就是 D-10 那类事故的温床），而领域模块不许 import `fish`（J2 分层：
# `fish_common ← fish_schema ← fish_db ← fish_profile ← fish`）。
# 把写原语放在 DB 层，领域模块就能合法地写事件——**而不是各自另造一套**。
# ============================================================

def append_event(conn, etype, actor, subject, payload, cause=None, *,
                 commit=True) -> int:
    """追加一条事件。

    `commit=True`（默认）保持旧行为：事件写进去就提交。
    **`commit=False` 让调用方自己控制事务边界**（外部审查 🔴9：旧实现把 commit 焊死在
    这里，上层没法把"业务写 + 事件"组成一个事务——merge/retire 的归属变更因此不可重放）。
    """
    cur = conn.execute(
        "INSERT INTO events (event_id, ts, type, actor, subject, payload, cause, schema) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (ulid(), now_utc(), etype, json.dumps(actor, ensure_ascii=False),
         json.dumps(subject, ensure_ascii=False), json.dumps(payload, ensure_ascii=False),
         cause, "evt@1"),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


# ============================================================
# 配置写入（2026-11-11 从 `fish.py` 下沉，与 `get_config` 同层语义）
#
# 它必须跟 `append_event` 在同一个模块：写配置要记 `config.changed` 事件，
# 而 `append_event` 已下沉到这里。放 `fish_common` 会形成 common → db 的**上行依赖**。
# 下沉之后 `fish_profile`（画像）才能合法地读写自己的配置键（提问节律等）。
# ============================================================

def set_config(conn, key, value):
    default, typ, allowed = _key_type(key)
    if typ == "int":
        value = int(value)
    elif typ == "float":
        value = float(value)
    elif typ == "bool":
        value = bool(value) if not isinstance(value, str) else value.lower() in ("1", "true", "yes")
    if allowed and str(value) not in allowed:
        raise ValueError(f"{key} 只能是 {allowed}，收到 {value}")
    old = get_config(conn, key)
    conn.execute(
        "INSERT INTO config (key,value,type,updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, str(value).lower() if isinstance(value, bool) else str(value), typ, now_utc()),
    )
    conn.commit()
    append_event(conn, "config.changed",
                 {"citizen_id": "system", "evidence_level": "signed"},
                 {"kind": "config", "id": key},
                 {"key": key, "old_value": old, "new_value": value})
    return value
