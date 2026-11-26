#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fish v0.3 — 大肥鱼生命系统 · P1a
================================
在 P0 基础上增加：
  1. config 表 + 默认值（邻近策略、时区、注入预算）
  2. neighbors 四策略（code / time / tag / type / mixed）
  3. recall 记录访问（last_accessed_at / access_count / dormant）
  4. typed decay（按记忆类型分层的半衰期）
  5. citizens 启用，kind = resident / visitor / role / system
  6. forgetting / config / citizen / whoami 命令

P0 已验证且不变的部分：事件表 + 两个不可变触发器 + next_code 的
BEGIN IMMEDIATE 并发分配 + rebuild 逻辑。
"""
import argparse
import datetime
import fnmatch
import hashlib
import json
import math

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
import uuid
from pathlib import Path

# 共享原语（2026-10-26 · hooks 深度利用 T01）：路径/占用面、可读名、动作采集。
# 三者都是纯 stdlib 小模块，hook 侧也要 import 它们——**判定只许有一份实现**，
# 所以 fish 从这里 import 并原样再导出（`fish.surface_hit` 等旧名字仍然可用）。
import fish_activity
import fish_git_store
import fish_lineage
import fish_lineage_backfill
import fish_naming
import fish_surface
from fish_surface import _path_inside, normalize_surface, surface_hit  # noqa: F401 —— 对外仍是 fish 的名字
# 外部审查 🔴5/🔴6/🔴9 的修法（2026-11-11 抽出成模块，避免 fish.py 继续膨胀 · J4/E-11）
from fish_trust import (AUTH_REQUIRED_MEM_TYPES, AUTHORIZED_CITIZEN_KINDS,
                        citizen_kind_of, enforce_mem_type_auth,  # noqa: F401
                        identity_anchor_grants, load_ownership_merge_map, resolve_merged)
# 2026-11-11（E-11 拆包）：`rebuild_projection`（404 行，fish.py 里最长最危险的函数）
# 搬到 `fish_rebuild`。它不 import fish，用的是下层 fish_common + fish_trust（J2）。
from fish_rebuild import rebuild_projection  # noqa: F401
# 2026-11-11（E-11 拆包第二/三刀）：DDL 常量 + `SCHEMA` 拼装 + `connect`
# 全部归到 fish_schema（纯数据 + 8 行连接，只依赖 stdlib）。
from fish_schema import (AUDIT_DIR_NAME, FALLBACK_PROJECT_NAME, SCHEMA, _ACCESS_LOG_COLS,
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
                         _WORKSPACE_MAP_DDL, _WORKSPACE_SCHEMA, connect)
# 2026-11-11（E-11 拆包第四刀前置）：看板路径规则下沉共享层。
from fish_common import BOARD_SCOPES, UNATTRIBUTED, board_relpath  # noqa: F401
# 2026-11-11（E-11 拆包第四刀）：建表/补列/迁移辅助搬到 fish_db。
# `migrate` 本身仍在本文件（它编制 KB 专属的 ensure，搬走会造成上行依赖）。
# 2026-11-11（E-11 拆包五刀前置）：`append_event` 下沉 `fish_db`
# —— 事件是真相源，写原语属于 DB 层；领域模块据此可合法写事件（J2 分层不破）。
# 2026-11-11（E-11 拆包五刀前置）：`set_config` 下沉 `fish_db`
# —— 它与 `append_event` 必须同模块（写配置要记 config.changed 事件）。
from fish_db import (    PresenceMigrationError, _CITIZENS_ALL_COLUMNS, _PRESENCE_SESSION_BACKFILL, _board_seen_backup,
    _citizens_all_view_ddl, _ensure_activity_table, _ensure_board_seen_session, _ensure_board_tables,
    _ensure_boards_home_paths, _ensure_citizens_all_view, _ensure_identity_tables,
    _ensure_name_tables, _ensure_presence_session, _ensure_push_tables,
    _ensure_session_evidence_levels,
    _ensure_stint_open_index, _ensure_workspace_table,
    _has_unique_index, _presence_backup, _presence_data_root, _presence_pk_cols,
    _presence_pk_cols_of, _presence_rebuild_ddl, _presence_session_counts, append_event,
    set_config)

# 共享原语（2026-10-27 · 拆包 T02）：时间 / 摘要 / 文本计数只有一份实现，
# 家在 `fish_common`。`fish.now_utc` 等旧名字继续可用，且与 `fish_common.now_utc`
# 是**同一个对象**（`from … import` 再导出，不是复制一份）。
import fish_common  # noqa: F401 —— 模块本身也要在（判据 J1d 查对象同一性）
# 2026-11-11：主人画像（R1-1 槽位化）。领域逻辑在模块里，本文件只做 CLI 壳。
import fish_profile
from fish_common import (  # noqa: F401
    _exc_text, count_tokens, fts_match_expr, fts_text, index_tokens, iso_local,
    now_utc, ulid, citizen_ids_known, warn_ignored
)
# 推送渠道词表的唯一真相（112 W-B · A1）。单独成叶子模块是因为 `fish_common` 有
# 行数上界（`test_split_contract.FISH_COMMON_LINES_MAX`）——窗口是验收契约，不调阈值。
from fish_push_vocab import LEGACY_PUSH_CHANNELS, PUSH_CHANNELS  # noqa: F401

# 原始对话语料层（2026-10-27 · 语料层 T01/T02）：独立库 `corpus.sqlite`，与主库零耦合。
# 项目/公民解析器以回调注入（这里不把 fish 的解析逻辑搬进语料模块，避免循环导入）。
import fish_corpus  # noqa: F401
from fish_corpus import CorpusError, CorpusNotReady  # noqa: F401

# 知识库（B3）整体搬进 `fish_kb`（2026-10-27 · 拆包 T03 · 设计 §1.2/§1.4）。
# 下面这些名字是**显式再导出**：`fish.kb_tree` / `fish.KBError` / `fish._strict_bool`
# 全部继续可用，且与 `fish_kb.<name>` 是**同一个对象**。
# 为什么不用 `import *`：漏名是静默的，而这里每个名字都被判据 J1 逐个查对象同一性。
# 生成/校验：`python tools/gen_reexport.py --check`
# F-P1-5：数据面 CLI 入口（实现住 fish_dataplane，这里只做 re-export）
from fish_dataplane import cli as dataplane_cli  # noqa: F401
from fish_attach import (AttachmentRejected, store_blob,  # F-P1-3
                         validate_attachment,
                         ATTACH_MAX_BYTES_KEY, ATTACH_MAX_MB_DEFAULT,
                         ATTACH_SENSITIVE_PATTERNS, ATTACH_DENY_ROOTS,
                         _attach_max_bytes, _attach_check_name,
                         _attach_check_root)
import fish_atomic, fish_meta, fish_token  # noqa: E401 —— 薄模块挤行数窗口
import fish_push_vocab  # 推送词汇/采纳率的唯一真相（叶子）
from fish_inject import (  # noqa: F401  —— 拆分后仍从 fish 导出，调用方不用改
    _allow_untrusted_lessons, _lesson_rows, _recent_lesson_rows,
    count_lesson_rows, _rule_rows, count_rule_rows)
from fish_visibility import (_filter_by_visibility, _visibility_enforced,
                            _visibility_sql)  # F-P1-X：可见性执行层
# 看板与门面渲染层（独立模块：渲染只回答"写成什么样的字"，编排层只回答"什么时候写"）。
# 以下全部是**显式 re-export**：`fish.board_render` / `fish.HOME_DIRNAME` /
# `fish.resolve_home_root` 这些旧名字继续可用，且与 `fish_board.<name>` 是同一个对象。
# 判据 J1 逐个查对象同一性——复制一份实现会让"统一出口"退化成两个各自演化的出口。
from fish_board import (_presence_write_shape, _board_seen_write_shape, _fold_presence_for_inject,
    board_block, _project_namespace, _surfaces_touch, _surface_overlap_hit, _unnamed_hint,
    board_out_path, board_ensure, board_checkin, _is_auto_doing, _auto_doing_text, board_auto_checkin,
    board_post, autolink_memory_code, board_checkin_or_remind, board_auto_presence, _fmt_period, _hhmm, citizen_labels,
    who_label, board_overlaps, board_observed_overlaps, board_simulate, board_stale_rows,
    board_prev_note, effective_last, _age_minutes, _collapse_presence_raw, board_presence_rows,
    _presence_state, _state_suffix, board_post_rows, board_post_full, _actual_dirs_text, board_unchecked_sessions,
    _unchecked_dirs_text, render_unchecked_section, _presence_heading, _render_one, _pack_by_bytes,
    board_render_root, board_sweep, board_render, board_show, board_close, _home_guard_dirs,
    _guard_home_location, resolve_home_root, _board_entry_of, ACTIVITY_COLUMN_NOTE, BOARD_DOING_MAX,
    BOARD_MAX_BYTES, BOARD_MAX_POST_CHARS, BOARD_POSTS_PER_FILE, BOARD_PRESENCE_IDLE_SECONDS,
    HOME_DIRNAME, NO_ACTIVITY_TEXT, PST_DASH_NO_BOARD, UNCHECKED_EMPTY, UNCHECKED_HEADING,
    UNCHECKED_NOTE, UNCHECKED_SCOPE_NOTE, UNCHECKED_UNKNOWN_CITIZEN, _AUTO_DOING_MARK)
from fish_db import restore_citizen, set_citizen_kind  # F-P1-7：公民还原（域函数住 fish_db）
from fish_kb import ( # noqa: F401
 KBError, KB_ACTIONS, KB_DEFAULT_IGNORE_GLOBS, KB_DEFAULT_MAX_OPEN_BYTES,
 KB_ERROR_CODES, KB_MAGIC_SIGNATURES, KB_MANDATORY_DENY, KB_MAX_CONTENT_BYTES,
 KB_SOURCE_LABEL,
 KB_MAX_ITEMS_PER_DIR, KB_MAX_LIMIT, KB_MEDIA_EXTS, KB_META_KEYS, KB_RESULTS,
 KB_SAME_NAME_LIMIT, KB_SEARCH_MAX_FILE_BYTES, KB_SENSITIVE_PATTERNS,
 KB_TREE_MAX_DEPTH, KB_UNATTRIBUTED, KB_VIAS,
 _KB_DEVICE_NAMES, _KB_FIT_MARKER_RE, _KB_FM_KEY_RE, _KB_PASSWORD_PATTERN,
 _KB_PASSWORD_RE,
 _KB_TOC_TITLE_BYTES, _KB_TREE_DEPTH_RULE,
 _KB_VAULT_SELECT, _STRICT_FALSE, _STRICT_TRUE, _kb_app_running,
 _kb_backfill_mandatory_globs, _kb_backup_finalize, _kb_backup_meta,
 _kb_backup_slug, _kb_backup_stats, _kb_cmd_backup, _kb_cmd_doctor, _kb_cmd_open,
 _kb_cmd_rollback, _kb_cmd_search, _kb_cmd_stats, _kb_cmd_toc, _kb_cmd_tree,
 _kb_cmd_vault,
 _kb_cmd_write, _kb_commands_ready, _kb_dir_bytes, _kb_effective_globs,
 _kb_exit_code, _kb_first_reparse, _kb_fit_text, _kb_fit_text_with_tail,
 _kb_fmt_mtime, _kb_guard, _kb_ignored, _kb_is_device_name, _kb_is_fm_continuation, _kb_is_reparse_entry,
 _kb_is_sensitive_name, _kb_json_globs, _kb_judge_component, _kb_log_quiet,
 _kb_merge_frontmatter, _kb_meta_json, _kb_obsidian_cli, _kb_open_impl,
 _kb_parse_budget, _kb_parse_frontmatter_args, _kb_quote, _kb_read_content_file,
 _kb_reject, _kb_rel_posix, _kb_render_document, _kb_render_fm_value, _kb_same_name, _kb_scalar,
 _kb_scan_counts, _kb_search_impl, _kb_search_multi, _kb_sensitive_notice,
 _kb_split_frontmatter, _kb_toc_title, _kb_tree_dir, _kb_tree_impl, _kb_tree_lines,
 _kb_tree_tail_hint, _kb_validate_globs, _kb_vault_brief, _kb_vault_dict,
 _kb_vault_line, _kb_vault_norm, _kb_walk, _kb_within_budget, _kb_write_impl,
 _strict_bool, kb_atomic_write, kb_backup, kb_backup_prune, kb_detect_eol,
 kb_detect_text, kb_doctor, kb_log_access, kb_open, kb_read_text, kb_resolve_target,
 kb_rollback, kb_search, kb_sha256_bytes, kb_sha256_file, kb_stats, kb_toc, kb_tree,
 kb_vault_add, kb_vault_get, kb_vault_list, kb_vault_remove, kb_vault_scan, kb_vault_fix_path, kb_vault_availability,
    _kb_free_bytes, _kb_relocate_plan, _relocate_journal, _relocate_state,  # F-P1-4
    kb_vault_init, kb_vault_relocate,  # W2 (2026-11-18) vault init / relocate
 kb_vault_set_default, kb_vault_set_mode, kb_write)

# 2026-11-26（W3-a · 144 刀一）：身份/会话域整体搬进 `fish_session`。
# 下列名字是**显式再导出**：`fish.ensure_session` / `fish.start_session` 等全部继续可用，
# 且与 `fish_session.<name>` 是**同一个对象**（判据 J1）。
import fish_session
from fish_session import (
    _alive_session_token, _citizen_row, _covering_mapping, _explicit_parent_key, _forced_citizen, _has_live_token,
    _has_open_stint, _inherited_token_shape, _is_expired, _nearest_identity_ancestor, _new_expires_at,
    _normalize_projects, _prove_citizen_binding, _session_id_of, _session_identity_row, _workspace_path_from_text,
    _write_session_identity, archive_citizens, assert_agent_parent_allowed, auto_session_display_name,
    build_session_identity, close_open_stints, credential_state_line, current_open_stint, current_open_stints,
    end_session, ensure_session, env_actor_name, env_session_alias, env_session_token, evidence_root,
    find_citizen_by_name, find_duplicate_display_names, find_or_create_session_citizen, find_same_name_citizens,
    find_session_citizen, fix_owner, hash_session_token, host_session_facts, identity_source_line, issue_agent_token,
    issue_session_token, list_workspace_candidates, list_workspace_mappings, load_citizen, lookup_session_token,
    normalize_workspace_path, open_project_stint, related_citizens_lines, remaining_token_hours,
    remove_workspace_mapping, rename_citizen, resolve_account_id, resolve_actor, resolve_actor_from_token,
    resolve_owner_arg, resolve_project_by_cwd, revoke_session_tokens, rotate_token, same_name_notice, session_status,
    session_token_ttl_hours, set_owner, set_workspace_mapping, start_session, strip_session_name_prefixes,
    token_actor_citizen_id, touch_citizen, validate_session_token, workspace_seed_candidates,
)
# 常量随域迁移，但不在 `fish_session.__all__`（设计图：导出面 = 70 函数）；
# 这里单独 import，保证 `fish.HOST_SESSION_ENVS` 等旧名字一个不少。
from fish_session import (
    ANON_SESSION_PREFIX, CITIZEN_ARCHIVE_REASON, CREDENTIAL_EXPIRED_FMT, CREDENTIAL_NO_TOKEN, CREDENTIAL_UNKNOWN,
    HOST_SESSION_ENVS, IDENTITY_SOURCE_REGISTER, IDENTITY_SOURCE_TOKEN, IDENTITY_SOURCE_UNATTRIBUTED,
    LEGACY_OWNER_DEFAULT, SELF_SESSION_PREFIX, SESSION_ALIAS_ENVS, SESSION_CHANNELS, SESSION_EVIDENCE_LEVELS,
    SESSION_NAME_PREFIXES, SESSION_TOKEN_ENVS, SESSION_TOKEN_PREFIX, SESSION_TOKEN_TTL_KEY, _ABS_PATH_RE,
    _CITIZEN_READ_FIELDS, _PATH_FILE_EXTS, _REVOKE_DESCENDANTS, _SEED_SKIP_PARTS, _SEED_SKIP_PREFIXES,
    _STINT_READ_FIELDS, _WORKSPACE_MAP_FIELDS,
)
# 2026-11-26（W3-b · 144 刀二）：记忆域整体搬进 `fish_memory`。
# 下列名字是**显式再导出**：`fish.record_memory` / `fish.search_memories` 等全部继续可用，
# 且与 `fish_memory.<name>` 是**同一个对象**（判据 J1）。
import fish_memory
from fish_memory import (
    _day, _distill_now_z, _like_literal, _list_where, _norm_date_bound, _row_dict,
    access_log_rows, add_link, append_access_log, archive_body, archive_relpath,
    archive_sweep, build_links_on_write, code_mentions_in, count_memories,
    distill_clusters, find_similar_memories, find_similar_projects, format_dup_hint,
    format_list_declaration, format_status_tag, link_alias, list_memories,
    load_rename_map, locate_body, neighbors, next_code, normalize_occurred_at,
    parse_tags_column, project_writable, read_body_path, record_access, record_memory,
    recent_fallback, resolve_renamed, score_candidate, search_memories, similar_codes,
    supersede_memory, tag_like_pattern, unlink_alias, withdraw_memory,
    write_distill_suggestion,
)
# 常量随域迁移，但不在 `fish_memory.__all__`（设计图：导出面 = 43 函数）；
# 这里单独 import，保证 `fish.LINK_RELS` 等旧名字一个不少。
from fish_memory import (
    ACCESS_VIAS, CODE_MENTION_RE, DISTILL_DEFAULT_DAYS, DISTILL_DIRNAME, DISTILL_MAX_GROUPS,
    DISTILL_MIN_GROUP, DISTILL_PREVIEW_CODES, LIKE_ESCAPE_SQL, LINK_RELS, LINK_SOURCES,
    LIST_DEFAULT_LIMIT, LIST_SOURCE, RECALL_DIMENSION_NOTE, SIM_MIN_SIM, SIM_MIN_TOKENS,
    TAGS_ONLY_HINT, _MENTION_MAX_PROBES, _MENTION_MAX_RAWS,
)
# `_recorded_body_sha`：144 取证后 P2-2 新增的 `read_body_path` 专用 helper（43 名之外），
# 旧名字 `fish._recorded_body_sha` 仍可访问、与实现模块同一对象。
from fish_memory import _recorded_body_sha

# 2026-11-26（W3-c · 144 刀三）：注入装配 + 工具审计域整体搬进 `fish_inject_pack`。
# 下列名字是**显式再导出**：`fish.build_injection_pack` / `fish.build_attention_block`
# 等全部继续可用，且与 `fish_inject_pack.<name>` 是**同一个对象**（判据 J1）。
import fish_inject_pack
from fish_inject_pack import (
    _access_log_total, _access_scope_where, _attention_gaps, _audit_actor_values,
    _audit_in_window, _audit_ts, _candidate_scopes, _citizen_aliases, _citizen_by_value,
    _citizen_id_of, _clamp_rules_per_source, _display_name_of, _iter_tool_calls,
    _knowledge_gap_rows, _knowledge_gaps, _label_in_project, _parse_iso_utc,
    _peer_access_count, _peer_tool_counts, _project_activity, _project_scope,
    _record_in_scope, _render_project_head, _render_repo_line, _resolve_injection_project,
    _resolve_profile_session, _session_label, _titles_of, _tool_blind_spots, _tool_usage,
    announce_truncation, build_attention_block, build_injection_pack, build_lesson_block,
    build_short_rules_block, compute_session_profile, count_open_loop_rows,
    fallback_project, injection_contains_phrase, injection_tail_text, injection_tail_worst,
    normalize_inject_sections_arg, normalize_injection_sections, normalize_tool_name,
    read_tool_call_audit, retrieval_hint,
)
# 常量随域迁移，但不在 `fish_inject_pack.__all__`（设计图：导出面 = 46 函数）；
# 这里单独 import，保证 `fish.TOOL_UNIVERSE` / `fish.INJECTION_SECTIONS` 等旧名字一个不少。
from fish_inject_pack import (
    ATTENTION_BLOCK_MAX_TOKENS, ATTENTION_GAP_SCAN_LIMIT, ATTENTION_LESSON_WINDOW_DAYS,
    ATTENTION_PER_TYPE_LIMIT, AUDIT_TOOL_CALLS_FILE, BLIND_SPOT_MIN_PEER_CALLS,
    BLIND_SPOT_WINDOW_DAYS, BOARD_MAX_RATIO, INJECTION_SECTIONS,
    INJECTION_SECTIONS_NOTE, KNOWLEDGE_GAP_LIMIT, KNOWLEDGE_GAP_MIN_PEER_READS,
    KNOWLEDGE_GAP_WINDOW_DAYS, RULES_BLOCK_MAX_TOKENS, RULES_PER_SOURCE_LIMIT,
    RULES_PER_SOURCE_MAX, RULES_PER_SOURCE_MIN, TOOL_ALIASES, TOOL_UNIVERSE,
    _ATTENTION_LABELS, _BASIS_TEXT, _INFERRED_PROJECT_NOTE, _INJECTION_TAIL_WORST_AS_OF,
    _INJECTION_TAIL_WORST_COUNTS, _PROJECT_SOURCES, _RETRIEVAL_HINTS, _SECTION_ALIASES,
    _TAIL_FULL_CONTAINS, _TAIL_SECTION_LABELS, _UNDECLARED_PROJECT_LINE,
)


# 拆包兼容垫片：既有测试用 `mock.patch.object(fish, "<名字>", …)` 做变异自证。
# 被搬函数住在 `fish_session` / `fish_memory` / `fish_inject_pack`，其 globals 在那边；
# 只 patch `fish` 的绑定打不到调用点。
# 因此把对 `fish.<名字>` 的赋值同步到实现模块（含 `append_event` 等共享叶子），
# 保持拆分前的 patch 语义；仅影响“给 fish 模块属性赋值”，正常调用路径不经过它。
import types as _types

class _FishModule(_types.ModuleType):
    def __setattr__(self, name, value):
        # 同步**所有**新模块里也有的全局名（函数/常量/共享叶子如 append_event）：
        # 拆分前它们都是 fish 的全局，既有测试的 mock.patch.object(fish, …) 依赖这一点。
        if not name.startswith("__"):
            if name in fish_session.__dict__:
                setattr(fish_session, name, value)
            if name in fish_memory.__dict__:
                setattr(fish_memory, name, value)
            if name in fish_inject_pack.__dict__:
                setattr(fish_inject_pack, name, value)
        super().__setattr__(name, value)

_self_module = sys.modules.get(__name__)
if _self_module is not None and _self_module.__dict__ is globals():
    _self_module.__class__ = _FishModule


# T02：下沉到 fish_common 的基础层 + push 领域（re-export，判据 J1）
from fish_common import (ATTENTION_OPEN_KEYWORDS, ATTENTION_OPEN_WINDOW_DAYS, ATTENTION_TITLE_MAX_CHARS, CONFIG_DEFAULTS, _ALIAS_KINDS, _ALIAS_MAX_HOPS, _DEFAULT_DECAY, _alias_map, _board_seen_columns, _clip_chars, _index_one_fts, _key_type, _open_loop_predicate, _open_loop_rows, _presence_columns, _presence_has_session, _utc_str, _walk_alias, board_period, clean_prompt, compute_decay, ensure_fts, get_config, load_decay_cfg, local_date, parse_utc, push_outcome_window_hours, reindex_fts, resolve_name, resolve_project_names, warn_derived)  # noqa: F401
# 2026-11-11（E-11 拆包）：`record_memory` 与 `rebuild_projection` 共用的纯函数下沉到
# fish_common —— 这样 `fish_rebuild` 能在不 import fish 的前提下拿到它们（J2）。
from fish_common import (body_relpath, resolve_owner, slug_title,  # noqa: F401
                         _SLUG_MAX, split_code_prefix)
from fish_push import (FRESHNESS_EXEMPT_TYPES, PROMPT_MAX_TERMS, STALE_SENSITIVE_TYPES, _CJK_RUN_RE, _CODE_RE, _LATIN_RE, _PROMPT_STOPWORDS, _PUSH_TAG, _PUSH_W, _SILENCE_CAP_DAYS, _SILENCE_FLOOR, _activity_overlap_candidates, _activity_overlap_hit, _cand, _conv_push_candidates, _declared_row_fresh, _kb_push_candidates, _project_of, _push_history, _recent_pushed_codes, _seen_in_session, _session_labels_for, _silence_weight, build_prompt_candidates, build_push_block, build_push_candidates, extract_terms, links_for, mark_push_accepted, mark_push_accepted_prefix, memory_freshness, search_memories_any, select_push_candidates)  # noqa: F401


DEFAULT_ROOT = Path(__file__).resolve().parent / "data"


KINDS = ("resident", "visitor", "role", "system")


# ══════════════════════════════════════════════ B1/B2/B3 共享常量（2026-10-26）
# 依据：`docs/架构设计-B1B2B3-v0.1.md` §1.4 / §1.5 / §2.2 / §2.3 / §3.2 / §5.7。
# 为什么集中在这里：门面渲染、看板落点、知识库三块共用同一批取值；散在各自段里，
# "改一处忘一处"是这类常量最常见的漂移方式（代码地图里 17 vs 16 的幽灵工具就是这么来的）。

LAYOUT_TOP = ("00-首页", "10-项目库", "20-记忆库", "30-知识库", "90-系统")
LAYOUT_MAX_DEPTH = 3                  # HOME-1：任何文件相对 home_root 深度 ≤3
LAYOUT_MEMORY_DIR = "20-记忆库"
LAYOUT_SYSTEM_DIR = "90-系统"
LAYOUT_STATUS_TABLE = "10-项目库/项目状态表.md"

# R1（主理人裁决 · 2026-10-26）：`20-记忆库/索引-YYYY-MM.md` 的文件预算。
# PRD §6.3 的 "32 KB" 来自看板文件撑到 396 KB 而死的那个实证，但**索引不是看板**：
# 它不进注入上下文、按月份分片、是人逐行读的清单——32 KB 会把一个正常月份（246 行/65 KB）
# 误判成"超预算"。裁决：硬上限 128 KB；超限时按行截断并在页内脚注明示（不静默丢行）。
LAYOUT_INDEX_MAX_BYTES = 128 * 1024
# 标题列上限（R1）：完整标题会把整行宽度吃掉，人读时"归属/路径"反而看不见。
# 超出加 `…`；正文标题一字不改（只是在索引里省地方）。
LAYOUT_INDEX_TITLE_MAX_CHARS = 48

# B2 项目状态表（PST）· §4.8。六列顺序是契约（判据按列序断言，不按文案匹配）。
PST_COLUMNS = ("项目", "状态", "最近活跃", "看板入口", "主责 AI", "未了事项")
PST_OPEN_WINDOW_DAYS = 30            # Q5：与注入包 ATTENTION_OPEN_WINDOW_DAYS 同口径
PST_OWNER_FALLBACK_DAYS = 7          # Q4：无开档期时回退"最近 7 天记忆 owner"
# 三种显式 `—`：缺数据必须说清是哪一种，否则"没数据"和"没人"看起来一样（PST-5）。
PST_DASH_NO_MEMORY = "—（无记忆）"
PST_DASH_NO_OWNER = "—（无开档期）"
PST_DASH_NONE = "—"                  # 未了事项为 0
PST_DASH_UNAVAILABLE = "—（数据源不可用）"

# README 四要素（HOME-2）。判据按这四组关键词判，不认文案细节：
#   ① `## 这是什么`  ② `## 能做什么`（必须含"不能"）
#   ③ `## 谁在写`    ④ `生成时刻：` + "手改会被覆盖"
README_SECTION_MARKERS = ("这是什么", "能做什么", "谁在写")
README_STAMP_MARKERS = ("生成时刻：", "手改会被覆盖")
README_NEGATIVE_WORD = "不能"

# HOME-8 / §5.7：门面产物里绝不出现凭证形态。渲染后自检与 verify 共用同一份正则，
# 两处各写一份就会出现"渲染拦得住、体检看不见"这种假绿。
CREDENTIAL_PATTERNS = (
    r"fsh_[A-Za-z0-9_-]{20,}",           # fish 会话凭证形态
    r"token\s*=",                        # 环境变量 / 查询串里的 token=
    r"BEGIN [A-Z ]*PRIVATE KEY",         # 私钥头
)

# ------------------------------------------------------------------ 基础

# `warn_ignored` 的实现在 `fish_common`，这里只 import —— 不是"少写一份"，
# 而是**它必须在能被所有领域模块拿到的那一层**：corpus / push / kb / activity 都
# 不许 import fish（J2 单向依赖），但都 import fish_common。放在这里等于让它们
# 要么重复实现、要么把报错吞掉。import 进来后 `fish.warn_ignored` 仍指向同一个
# 对象（判据 J1 会查这一点），调用面一个字不用改。




# ------------------------------------------------------ 推送地基 DDL（单一定义来源）
# 依据：13-推送系统设计 §2 + 16 §5.6。这是推送系统的地基层，P0 必须先建：
#   没有 memory_access_log（谁读过什么）画像只能猜；没有 links（什么和什么有关）候选只能乱；
#   没有 push_log（我推过什么、用户怎么反应）时机策略永远无数据可学。
#
# 为什么提成常量而不是直接抄进 SCHEMA：同一段 DDL 有两个消费者——
#   `SCHEMA`（新库一次建好）与 `migrate()`（旧库补建）。复制两份是"改一处忘另一处"的标准起点。
#
# 为什么索引**不**写在这里（07 的 is_owner 事故，回归见 test_legacy_db_migration_owner）：
#   旧库里若已存在同名表而列不齐，`CREATE INDEX` 会报 no such column，
#   而 SCHEMA 是先于 migrate 执行的 → 整个升级路径直接崩。
#   索引统一放在 migrate() 里「先建表 → 再补列 → 最后建索引」。
# 字段语义（与 §2.1 一一对应）：
#   citizen_id  谁读的（会话公民；空 = 未登记/未归属）
#   session_key 哪次会话（同一宿主下区分 anyi-dev / anyi-review 这一级）
#   project     读的时候在哪个项目（档期机制未落地时 = 编码所属项目，见 append_access_log）
#   via         recall | resolve | read_body | inject | push | manual（代码里校验）
#   payload     可选 JSON：推送候选 id 等，用于度量采纳率（§8.2）

# 存储约定（§2.2）：**边单向存、查询双向做**。
#   rel    supersedes | related | derived | contradicts | precedes（见 LINK_RELS）
#   source explicit | structural | semantic | manual（见 LINK_SOURCES）
#   weight 0~1，可比；evidence 是"为什么建这条边"的人话，查不到理由的边等于噪音
# 表里不设 CHECK 约束：加关系类型是规格内动作（03 §F），不该每次都迁移表。
#
# 再巩固预留（14 §6 修正 1 / 15 §4，REALM arXiv 2609.16053）：
#   论文的结论是**边权重不是写入时算死的静态值**，而是每次被使用／被一次无效推送
#   之后都会被改写。要支持这一点，只靠 weight 一个数不够——还得知道"上次改成什么
#   时候、改过几次、这条边是不是已经被判定为噪音"。于是有这三列：
#     last_reinforced_at 最后一次被强化/调整的时间（NULL = 从没调过，不是 0 时刻）
#     reinforce_count    累计被调整次数（单调递增，用于抑制"反复抖动"的边）
#     suppressed         0=正常 1=已被抑制（反复引入无关结果 → 不再参与候选）
#   **本轮只加列，不实现任何再巩固逻辑**（那是 P1）：写入口一个都不动，三列全走默认值。
#   为什么不等到 P1 再加：现在建表就带上，将来不必为一个纯加列再做一次迁移——
#   而 07 的 is_owner 事故说明"后期补列"是这套库里最容易崩的一步。

# 推送批次日志（16 §5.6；论文《When Should the Agent Speak?》§6.3）。
#
# 论文的原话是：所有记忆系统只暴露一个查询——"检索当前请求相关的内容"，
# **没有一个暴露时机策略真正需要的那个**：用户对系统**过去干预**的响应历史。
# 补这个缺口不需要新架构，只需要把"我推了什么"和"用户怎么反应"写回存储。
#
# 一行 = 一次推送里的**一条记忆**：同一次 inject 展示 N 条 → N 行，共享 pushed_at /
# budget_tokens。为什么不把 N 条压成一行 JSON：采纳率要按 code 聚合（哪类记忆推了有用），
# 压在 JSON 里每次都得解包再算，ix_push_code 也会变成空转。
#
# 字段语义：
#   channel       inject | prompt | tool_after（词表唯一真相在 `fish_push_vocab.PUSH_CHANNELS`；
#                 历史词 hook/situational 只在写入校验里兼容，见 `_push_write_channels`）
#   budget_tokens 这次推送**批次**占的预算（同批次 N 行同值，不是单条成本）
#   candidate_id  候选池 id —— 候选池没实现之前恒为 NULL（**不许编**）
#   reaction      NULL=待判 | accepted | ignored | expired
#                 accepted = 之后读了正文 / 校验了编码；ignored = 窗口内没人理（daily_report 判）
#                 expired 只占位：它需要独立的过期信号源，P0 不实现（P0 不写它的任何值）
#   reacted_at    真正**发生反应**的时间；ignored 是"判定"不是"反应" → 保持 NULL
#   latency_sec   reacted_at − pushed_at（只对 accepted 有值）
#
# 与 16 §5.6 的两表方案（push_log + push_outcome）的差异：合并成一张。
# 理由是两张表严格 1:1、都靠 push_id 连，拆开只多一次 join 与一次"两边写不齐"的
# 机会；而 07 的教训是"同一件事不要存两处"。字段一个没少，语义不变。
# ------------------------------------------------------ 身份层 P1-a DDL（纯结构）
# 依据：身份层设计（账号 → 会话 → 凭证 → 档期 → 归档 → 合并视图）。
#
# ⚠️ 下面这几段"本轮只建结构、逻辑下一轮切"的记录已经**过期**（P1-a/P1-b/序2
# 都已落地），保留它们只会让后来者误以为还有未完成的分步。压缩成结论：
#   · 结构先就位、逻辑单独切——这个策略当时是对的，`citizens` 被 28 条 SQL
#     引用，在这张表上顺手改查询会让风险顺着全部调用点扩散；
#   · 沿用推送地基的同一套纪律：表 DDL 提成常量（SCHEMA 与 migrate 共用一份）、
#     索引与视图只放 migrate()、顺序是「先建表 → 再补列 → 最后建索引/视图」。
# 完整历史见 `56-身份层-P1a-设计` 与 `58-身份层-P1b`。


def _presence_prefix_conflicts(conn) -> list:
    """同一公民下「A 是 B 的前缀」的会话键清单——**只报告，不合并**。

    已知残留：`dsh:session-779a` 与 `dsh:session-779a464a-…` 像是同一会话的短/长
    两种写法。合并要靠人判断：猜错就是把两个会话的历史焊死，而且没有回滚路径。
    """
    if not _presence_has_session(conn):
        return []
    buckets = {}
    for cid, sk in conn.execute(
            "SELECT DISTINCT citizen_id, session_key FROM presence WHERE session_key != ''"):
        buckets.setdefault(cid, set()).add(str(sk))
    out = []
    for cid in sorted(buckets):
        keys = sorted(buckets[cid])
        for short in keys:
            for longer in keys:
                if short != longer and longer.startswith(short):
                    out.append({"citizen_id": cid, "short": short, "long": longer})
    return out


def presence_session_report(conn) -> dict:
    """presence 会话维度的**现状读数**（`--report` / `--dry-run` 共用这一份实现）。

    `pending` 的三种取值各自意味着什么（设计 §2.2 状态机）：
      · `None`        —— 已迁移（三列主键）或全新库，无需动作；
      · `"rebuild"`   —— 还没有 session_key 列（或只有列没重建）：需要显式 `--apply`；
      · `"pk_mismatch"` —— 三列主键但顺序不对：人工处置（本函数不猜）。
    """
    cols = _presence_columns(conn)
    total = int(conn.execute("SELECT COUNT(*) FROM presence").fetchone()[0] or 0) if cols else 0
    pk = _presence_pk_cols(conn)
    filled, blank = _presence_session_counts(conn)
    expected = filled
    if cols and "session_key" not in cols and "agent_id" in cols:
        expected = int(conn.execute(
            f"SELECT COUNT(*) FROM presence WHERE ({_PRESENCE_SESSION_BACKFILL}) != ''"
        ).fetchone()[0] or 0)
    pending = None
    if cols:
        if "session_key" not in cols:
            pending = "rebuild"
        elif pk != _PRESENCE_PK_COLS:
            pending = "pk_mismatch" if len(pk) == 3 else "rebuild"
    return {"rows": total, "pk": list(pk), "has_session_key": "session_key" in cols,
            "backfilled": filled, "blank": blank, "expected_backfilled": expected,
            "pending": pending, "prefix_conflicts": _presence_prefix_conflicts(conn)}


def presence_session_rollback(conn, *, data_root=None, dry_run=False) -> dict:
    """把 `presence` 退回两列主键（**合并**会话行、丢弃 `session_key`）。

    返回 `{"rebuilt", "backup", "rows_before", "rows_after", "dropped", "pk"}`；
    `dropped` 逐条给出被合并掉的行（`doing` / `surface` / 会话键），调用方负责
    打印到 stderr 并落 `_audit/`——**丢信息必须留证**（设计 §6.2 ②）。

    `dry_run=True`：只算"会丢什么"并返回（`rebuilt=False`、不备份、不写库）。
    CLI 的 `--dry-run` 与 `--apply` 因此共用同一份合并口径——两份实现必然漂。

    合并规则**写死在这里，不许临场判断**（设计 §6.2 ②）：
      · 每个 `(board_id, citizen_id)` 选 `max(last_seen_at, auto_seen_at)` 最大的行做主行；
      · `session_key` 丢弃，`agent_id` 取主行的值；
      · 主行 `first_seen_at` 换成该组**最早**的 `first_seen_at`（首签时间不该被合并改晚）。
    """
    cols = _presence_columns(conn)
    if not cols:
        return {"rebuilt": False, "backup": None, "rows_before": 0, "rows_after": 0,
                "dropped": [], "pk": ()}
    rows_before = int(conn.execute("SELECT COUNT(*) FROM presence").fetchone()[0] or 0)
    rows = conn.execute(
        "SELECT board_id, citizen_id, "
        + ("session_key, " if "session_key" in cols else "'', ")
        + "agent_id, status, doing, surface, first_seen_at, last_seen_at, "
        "auto_surface, auto_seen_at, auto_source FROM presence").fetchall()
    groups, order = {}, []
    for row in rows:
        key = (row[0], row[1])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)
    kept, dropped = [], []
    for key in order:
        group = groups[key]
        main = max(group, key=lambda r: max(r[8] or "", r[10] or ""))
        earliest = min((r[7] or "" for r in group), default="") or (main[7] or "")
        kept.append((key[0], key[1], main[3], main[4], main[5], main[6], earliest,
                     main[8], main[9], main[10], main[11]))
        for row in group:
            if row is main:
                continue
            dropped.append({"board_id": row[0], "citizen_id": row[1],
                            "session_key": row[2] or "", "doing": row[5] or "",
                            "surface": row[6] or ""})
    root = Path(data_root) if data_root is not None else _presence_data_root(conn)
    if dry_run:
        return {"rebuilt": False, "backup": None, "rows_before": rows_before,
                "rows_after": len(kept), "dropped": dropped,
                "pk": _presence_pk_cols(conn), "dry_run": True}
    conn.commit()
    backup = _presence_backup(conn, root)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN IMMEDIATE")
    committed = False
    try:
        conn.execute("DROP TABLE IF EXISTS presence_v2")
        conn.execute(_PRESENCE_V2_DDL)
        marks = ",".join("?" * len(_PRESENCE_V2_COLS))
        conn.executemany(
            f"INSERT INTO presence_v2 ({', '.join(_PRESENCE_V2_COLS)}) VALUES ({marks})",
            kept)
        conn.execute("DROP TABLE presence")
        conn.execute("ALTER TABLE presence_v2 RENAME TO presence")
        for stmt in _BOARDS_INDEXES:
            conn.execute(stmt)
        bad = conn.execute("PRAGMA foreign_key_check(presence)").fetchall()
        if bad:
            raise PresenceMigrationError(f"回滚后外键校验失败：{len(bad)} 行（已回滚）")
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
    return {"rebuilt": True, "backup": backup, "rows_before": rows_before,
            "rows_after": len(kept), "dropped": dropped, "pk": _presence_pk_cols(conn)}


def _ensure_kb_views(conn: sqlite3.Connection):
    """幂等：kb_path_stats 视图**定义不符才重建**（照 `_ensure_citizens_all_view` 的模式）。

    为什么不无条件 `DROP VIEW` + 重建：DROP 要拿写锁，多进程并发打开时会把别人的
    查询打断。视图列与 `_KB_PATH_STATS_COLUMNS` 一致时，这里一个字节都不动。

    建完立刻 `SELECT * ... LIMIT 0`：SQLite 的视图是**惰性校验**的——允许建一个
    引用了不存在列的视图，错误留到查询时才报。不在这里真查一次，坏视图会一路活到
    某个调用方手里（citizens_all 那次教训的同一形态）。
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='view' AND name='kb_path_stats'").fetchone()
    if exists:
        try:
            have = tuple(r[1] for r in conn.execute("PRAGMA table_info(kb_path_stats)"))
        except sqlite3.DatabaseError:
            have = ()                      # 旧定义引用了还不存在的列（半升级态）→ 重建
        if have == _KB_PATH_STATS_COLUMNS:
            return
        conn.execute("DROP VIEW kb_path_stats")
    conn.execute(_KB_PATH_STATS_VIEW)
    conn.execute("SELECT * FROM kb_path_stats LIMIT 0")


def _ensure_kb_tables(conn: sqlite3.Connection):
    """幂等：知识库两表 + 索引 + 视图（新旧库都能反复跑）。

    顺序仍是那条硬纪律：**先建表 → 再建索引 → 最后建视图**。视图引用了 kb_access 的列，
    提前一步会撞 `no such column`（旧库是"同名表缺列"时更明显），整个升级路径崩在启动时。

    本函数只做结构，不写任何行：vault 登记是 `kb vault add` 的事，审计行是读写路径的事。
    """
    conn.execute(_KB_VAULT_DDL)
    conn.execute(_KB_ACCESS_DDL)
    for stmt in _KB_VAULT_INDEXES + _KB_ACCESS_INDEXES:
        conn.execute(stmt)
    _ensure_kb_views(conn)

# ------------------------------------------------------ 身份层 P1-a：建表 / 补列 / 视图
# 113 §9.2（W5）：`presence.agent_id` 三步清退。三条谓词**共用一份**，①的"形如会话键"
# 直接复用当初回填用的 `_PRESENCE_SESSION_BACKFILL`——口径写第二份就会漂。
#: ③ 的**存量截点**：只标本波上线之前就存在的"无键无代理"行。没有它，之后任何
#: "手工签到没带会话键"的新行都会被标成 `legacy`——那是**给它安一个它没有的来历**，
#: 而且会让 `board render` 这类命令在测试里莫名多出一份备份（`test_board_home_root`
#: 的"render 不许改 data_root"就是这么红的）。截点取 W5 上线当天（整日，按 UTC）。
_RETIRE_LEGACY_BEFORE = "2026-11-25"

_RETIRE_STEPS = (
    ("session_key=agent_id, agent_id=NULL",
     "COALESCE(session_key,'')='' AND ({}) <> ''".format(_PRESENCE_SESSION_BACKFILL)),
    # ② 的 `agent_id <> ''` 不能省：两列都空时 `'' = ''` 也成立，会把 ③ 要标 legacy 的
    # 21 行当成"假代理"清掉（计数就说不清到底搬了几行）。
    ("agent_id=NULL", "agent_id IS NOT NULL AND agent_id <> '' AND agent_id = session_key"),
    ("auto_source='legacy'",
     "COALESCE(session_key,'')='' AND COALESCE(agent_id,'')='' "
     "AND COALESCE(auto_source,'')='' AND COALESCE(first_seen_at,'') <> '' "
     f"AND first_seen_at < '{_RETIRE_LEGACY_BEFORE}'"),
)


def _retire_presence_agent_ids(conn) -> dict:
    """113 §9.2：`presence.agent_id` 存量清退（幂等、可中断、有备份）。

    三步见 `_RETIRE_STEPS`：① 列空且旧列形如会话键 → 搬进 `session_key`；② `agent_id`
    抄的就是会话键 → 清空（上线前 599 行假代理）；③ 两列都空 → `auto_source='legacy'`。

    **没有待清退的行就不备份**：migrate 每条命令都跑，动不动复制一份 38MB 的库不行。
    有变化才备份，且在 UPDATE **之前**；回退 = 用 `_backups/` 里那份快照替换。
    """
    where = " OR ".join(cond for _set, cond in _RETIRE_STEPS)
    if not conn.execute(f"SELECT COUNT(*) FROM presence WHERE {where}").fetchone()[0]:
        return {"backup": None, "counts": [0, 0, 0]}
    root = _presence_data_root(conn)
    if root is None:                  # 内存库 / 拿不到目录：宁可不迁移，也不无备份就改数据
        return {"backup": None, "counts": [0, 0, 0], "skipped": "no_data_root"}
    backup = _presence_backup(conn, root)
    counts = [conn.execute(f"UPDATE presence SET {set_} WHERE {cond}").rowcount
              for set_, cond in _RETIRE_STEPS]
    conn.commit()
    return {"backup": backup, "counts": counts}


def migrate(conn: sqlite3.Connection):
    """旧库升级：缺列则补 + **幂等**数据迁移（数据迁移段在函数末，逐段带注释）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(code_index)")}
    additions = [
        ("last_accessed_at", "TEXT"),
        ("access_count", "INTEGER DEFAULT 0"),
        ("dormant", "INTEGER DEFAULT 0"),
        # 会话级追溯：同一宿主身份下区分是哪个会话写的
        ("session_actor", "TEXT"),
        # 附件索引（JSON 数组）：正文里也写引用，这里是可查询的副本
        ("attachments", "TEXT"),
        # 替代关系：被哪条新记忆取代（status='superseded' 时有效）
        ("superseded_by", "TEXT"),
    ]
    for name, decl in additions:
        if name not in cols:
            conn.execute(f"ALTER TABLE code_index ADD COLUMN {name} {decl}")

    # 2026-11-22（F6）：`git_commit` 新增两列，**老库必须在这里补**。
    #
    # 为什么不能指望 `_GIT_SCHEMA`：它是 `CREATE TABLE IF NOT EXISTS`——表已存在时
    # 一个列都不会加；而 `fish_db.migrate()` 只管它自己那三张表。现场实验（造一个
    # 没有 session_key 的库再跑 migrate）证实：不补列的话，新代码查 session_key
    # 直接 `no such column: session_key` 报错。
    try:
        gcols = {r[1] for r in conn.execute("PRAGMA table_info(git_commit)")}
        for name, decl in (("session_key", "TEXT"), ("has_trailer", "INTEGER")):
            if gcols and name not in gcols:
                conn.execute(f"ALTER TABLE git_commit ADD COLUMN {name} {decl}")
    except sqlite3.Error as exc:      # 老到连 git 表都没有：留给 SCHEMA 建
        warn_ignored("fish.py migrate git_commit columns", exc)

    # citizens.is_owner（v0.3.1）：owner 从 config 迁回实体属性
    ccols = {r[1] for r in conn.execute("PRAGMA table_info(citizens)")}
    if "is_owner" not in ccols:
        conn.execute("ALTER TABLE citizens ADD COLUMN is_owner INTEGER DEFAULT 0")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_citizens_owner "
        "ON citizens(is_owner) WHERE is_owner = 1")
    # 回填：若 config 里已指定 owner 且该公民存在，则标记为 owner
    row = conn.execute("SELECT value FROM config WHERE key IN ('owner_citizen','default_citizen') "
                       "AND value<>'' ORDER BY key='owner_citizen' DESC LIMIT 1").fetchone()
    if row:
        has = conn.execute("SELECT 1 FROM citizens WHERE is_owner=1").fetchone()
        if not has:
            conn.execute("UPDATE citizens SET is_owner=1 WHERE citizen_id=?", (row[0],))

    # 推送系统 P0（13 §2）：两张地基表。顺序纪律见 _ensure_push_tables 的说明。
    _ensure_push_tables(conn)

    # 身份层 P1-a（纯结构）：citizens 补 5 列 + 4 张新表 + citizens_all 视图 + 索引。
    # 顺序纪律见 _ensure_identity_tables 的说明。**不动任何既有查询。**
    _ensure_identity_tables(conn)

    # 2026-11-25（113 W4 · §5.3）：`session_tokens.evidence` 的 CHECK 扩 `provisional`。
    # 必须排在 `_ensure_identity_tables` **之后**（表与三索引先就位，重建会一并补回）。
    # 失败**不阻断启动**（与 presence 迁移同一纪律）：既有三档一字未动，只是"临时身份"
    # 这一档暂不可用，而 `issue_session_token` 会在用到时响亮失败。
    evidence_migration = {"rebuilt": False, "backup": None}
    try:
        evidence_migration = _ensure_session_evidence_levels(conn)
    except PresenceMigrationError as exc:
        print(f"警告：session_tokens.evidence 迁移未完成（provisional 暂不可用）：{exc}",
              file=sys.stderr)
    if evidence_migration.get("rebuilt"):
        print(f"（session_tokens 已重建以扩 evidence CHECK；备份："
              f"{evidence_migration.get('backup')}）", file=sys.stderr)

    # 工作区 → 项目映射（R1 根治）：项目归属的权威来源，替掉"最近更新者即当前项目"的猜测。
    # 与身份层无依赖，放在后面是为了让"结构升级"的既有顺序一个字节都不动。
    _ensure_workspace_table(conn)
    # 名字治理（2026-10-25 · 阶段 D）：别名表。放最后，同样是为了让"既有结构升级
    # 顺序一个字节都不动"——新增结构永远加在链尾，不改前人的顺序。
    _ensure_name_tables(conn)
    # 看板（P1-b · 2026-10-25）：三张表 + 索引。同样加在链尾。
    _ensure_board_tables(conn)
    # ── 结构段（两条线的新结构都在这里；铁律：结构全做完，才允许数据迁移）
    #   · 知识库：建表/索引/视图（结构）
    #   · 动作流：tool_activity + 索引（结构）—— 加在结构段末尾，不改前人顺序
    _ensure_kb_tables(conn)
    _ensure_activity_table(conn)
    # 会话树投影（113 W1 · §2.2）：两张空表 + 索引，**一行数据都不写**、既有查询一个不改；
    # 它属于"结构段"末（铁律：结构全做完才允许数据迁移），建表→建索引的顺序纪律见 fish_lineage。
    fish_lineage.ensure_tables(conn)
    # ── 数据迁移段（必须排在所有结构升级之后）
    #   · 看板落点：改 boards.path 的值（相对 fish-home）
    #   · 强制忽略项回填既有行（改 kb_vault.ignore_globs 的值）
    # 两段都幂等：连跑两次，第二次是空操作。
    _ensure_boards_home_paths(conn)
    _kb_backfill_mandatory_globs(conn)
    # 113 §9.2（W5）：presence.agent_id 存量清退（幂等；回退 = `_backups/` 的库快照）
    try:
        _retire_presence_agent_ids(conn)
    except PresenceMigrationError as exc:
        print(f"警告：presence.agent_id 清退未完成（下次启动重试）：{exc}", file=sys.stderr)
    conn.commit()
    try: fish_meta.stamp(conn)   # F-P2-5：补完列后盖章
    except sqlite3.OperationalError: pass   # A档：只读库写不了版本号


# ------------------------------------------------------------------ 配置


# ------------------------------------------------------------------ 编码


# ------------------------------------------------------ 推送批次与结果（16 §5.6）
# 与 memory_access_log 的分工（**两个维度，不是重复**）：
#   memory_access_log = **被看见**：谁、哪个会话、在哪个项目读了什么（用户行为）
#   push_log          = **这一次推送**：我们主动塞了什么、占多少预算、有没有反应
# inject 已经写过 access_log(via='push')——那记的是"这条被展示了"，记不出"这次推送批次"。
# 而批次恰恰是时机策略的计量单位：推 3 条读了 1 条 ≠ 推 1 条读了 1 条。
# 渠道词表：**唯一真相在 `fish_push_vocab.PUSH_CHANNELS`**（112 W-B · A1）。
# `fish.PUSH_CHANNELS` 是从 `fish_common` 再导出的同一个对象（不是副本）——测试与
# 既有调用方继续用这个名字；`fish_push_audit.CHANNELS` 也是同一个对象。
# 词表里只有实际三词；`hook`/`situational` 两个历史词各自来自另一个维度
# （会话通道 / 候选 kind），只保留写入兼容，见下面的 `_push_write_channels`。
# 反应值是一个**封闭词表**（NULL=待判 / accepted / ignored / expired），但它没有写入校验点：
# reaction 只由 mark_push_accepted 与 close_stale_pushes 两个函数写，取值写死在 SQL 里。
# 刻意不设 PUSH_REACTIONS 常量：现在没有任何地方**校验**它，那只会变成一份会漂移的注释。
# 也刻意没有 dismissed：它需要"用户主动关掉推送"这个信号源，现在没有，不许编。

_UNSET = object()          # "不过滤"的哨兵；None 本身就是 reaction 的合法取值（待判）


def _push_write_channels() -> tuple:
    """写端接受的渠道 = 实际三词 + 历史词；**每次现算**，不预先拼常量。

    为什么现算：`test_prompt_push.test_prompt_not_in_channels_goes_red` 会
    `mock.patch.object(fish, "PUSH_CHANNELS", (...))` 来证明"少一个值就响亮失败"。
    预先拼好的元组会让那个补丁失效——判据就此变成假绿。
    """
    return tuple(PUSH_CHANNELS) + tuple(LEGACY_PUSH_CHANNELS)


def append_push_log(conn, codes, *, channel, session_key=None, citizen_id=None,
                    project=None, candidate_id=None, budget_tokens=None,
                    pushed_at=None, content_hash=None, hash_by_code=None,
                    agent_id=None, parent_session=None) -> int:
    """写"这次推送"的日志：一条记忆一行，返回写入行数。

    容错边界与 append_access_log 完全一致（**故意的**）：本函数不做 try/except，
    容错责任留在调用方（MCP 工具层）——这样"推送日志失败不影响工具调用"这条
    才能被单独验证；把 except 藏在最里层，测试就再也证明不了调用方真的兜住了。

    project 缺省时按编码回查 code_index.project（与 append_access_log 同一取舍：
    档期机制没落地之前，编码所属项目是当前能给出的最诚实的"当时在哪个项目"）。

    `agent_id` / `parent_session`（113 §6.1 · W4.5）：**这一次投递的目标维度**，
    与身份（`citizen_id`）正交——一个公民可以有父 + N 个子，投递看的是会话/代理。
    缺省 `None` = 这次没有这个维度（**旧调用点一律保持缺省**：DSH 宿主根本没有
    agent id，判不到父也不许编），所以旧行为逐字节等价。
    """
    if isinstance(codes, str):
        codes = [codes]
    codes = [c for c in (codes or []) if c]
    if not codes:
        return 0
    if channel not in _push_write_channels():
        raise ValueError(
            f"未知推送渠道 channel={channel!r}"
            f"（可用：{', '.join(_push_write_channels())}）")
    ts = pushed_at or now_utc()
    proj = {}
    if project is None:
        proj = dict(conn.execute(
            "SELECT code,project FROM code_index WHERE code IN ("
            + ",".join("?" * len(codes)) + ")", codes).fetchall())
    budget = int(budget_tokens) if budget_tokens is not None else None
    # P0-2 第二段：`hash_by_code` 逐条给指纹（kb 候选各带各的）；单值 `content_hash`
    # 保留给"整批同一份内容"的调用点。
    rows = [(session_key, citizen_id,
             project if project is not None else proj.get(c),
             c, candidate_id, channel, budget, ts,
             ((hash_by_code or {}).get(c) or content_hash) if hash_by_code
             else content_hash, agent_id, parent_session) for c in codes]
    conn.executemany(
        "INSERT INTO push_log (session_key,citizen_id,project,code,candidate_id,channel,"
        "budget_tokens,pushed_at,content_hash,agent_id,parent_session) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return len(rows)


def push_gate_no_identity(conn, citizen_id, *, strict_anonymous=False, mode=None) -> bool:
    """无身份时这次该不该**不推**（`push.no_identity` 的唯一落点，113 §6.2 · W4.5）。

    两档：`skip` = 无身份一律不推；`allow`（默认）= 照旧推 —— 那是**今天 prompt 侧
    的真实行为**（`test_prompt_push.test_no_identity_gives_no_output` 一直钉着它，
    而 docstring 曾把"无身份"写成否定条件：501 条 `citizen_id` 为空的 prompt 推送
    就是这句谎话的代价）。

    `strict_anonymous=True` 给 tool_after 通道（`fish_push_hook.py`）：它**今天就有**
    "无身份不猜"这条自己的规矩（该文件头第 3 条）。W4.5 的硬约束是"默认值钉在今天
    的行为"，所以只建开关、不放宽既有闸门——否则 allow 档会把无身份会话的 tool_after
    推送从"不可能"变成"可能"，那就不是"只多两列"了。

    `mode` 只给**只读回放**用：生产库以 `mode=ro` 打开时 `set_config` 写不进去，
    而回放要同时看两档的结论。
    """
    if citizen_id:
        return False
    return (mode or get_config(conn, "push.no_identity")) == "skip" or bool(strict_anonymous)


def push_target_of(conn, *, host, session_key, target_mode=None) -> tuple:
    """这一次推送该记的 `(agent_id, parent_session)`（113 §6.1/§6.3 · W4.5）。

    · `push.target_mode='citizen'`（旧口径）→ `(None, None)`：两列都不写，读侧无感；
    · `session` 档（默认）→ DSH 写父会话 id。DSH **没有** agent id（110 §3.5.1 规则 2
      的 `agent_id` 恒 NULL），父只能从投影表/会话头判（`fish_lineage.parent_of`）；
    · WorkBuddy 本波**一律 NULL**：它的 `session_id` 被宿主改写成父键
      （`resolveHookSessionId`），真父子要靠 `agent_id` + `transcript_path` 双校正——
      那是 **W4.6**，W4.5 不猜（宁可不写，不串推）。
    判不到父 → `None`，**不兜 main**（110 §3.6 的"孤儿诚实"）。
    """
    try:
        # 配置读不进来（老库/只读介质）也只该少两列，不该让这次递送整个消失。
        if (target_mode or get_config(conn, "push.target_mode")) != "session" or host != "dsh":
            return None, None
        return None, fish_lineage.parent_of(conn, host, _session_id_of(session_key))
    except Exception as exc:       # noqa: BLE001 —— 少一列绝不能影响这次递送
        warn_ignored("fish.py push_target_of", exc)
        return None, None

def push_log_rows(conn, code=None, session_key=None, channel=None, reaction=_UNSET,
                  limit=200) -> list:
    """读推送日志（测试与后续度量共用一份读法，避免两处各写一份过滤）。

    reaction 用哨兵而不是 None 缺省：**None 是一个有意义的取值（待判）**，
    "不过滤"必须能和它区分开，否则 `reaction=None` 会静默变成"全都不排队"。
    返回列顺序：code, session_key, citizen_id, project, channel, budget_tokens,
               pushed_at, reaction, reacted_at, latency_sec, candidate_id。
    """
    sql = ("SELECT code,session_key,citizen_id,project,channel,budget_tokens,pushed_at,"
           "reaction,reacted_at,latency_sec,candidate_id FROM push_log WHERE 1=1")
    params = []
    for col, val in (("code", code), ("session_key", session_key), ("channel", channel)):
        if val is not None:
            sql += f" AND {col}=?"
            params.append(val)
    if reaction is not _UNSET:
        sql += " AND reaction IS ?"          # IS 而不是 =：待判行必须用 NULL 查得到
        params.append(reaction)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    return conn.execute(sql, params).fetchall()


def close_stale_pushes(conn, *, window_hours=None, now=None) -> int:
    """把超过窗口仍无反应的推送判为 ignored，返回判定行数。

    为什么"没反应"只能靠等：窗口内没读 ≠ 忽略（可能只是还没用到）。窗口是**唯一**
    能证明"这次推送白费了"的证据——正因如此，它只能由 daily_report 定时跑。

    ignored 只写 reaction：**不写 reacted_at / latency_sec**。那两列是"反应"的时间，
    这里发生的是"判定"；把判定时间冒充反应时间，画像就会以为用户在第 25 小时点了头。

    window_hours<=0 视为关闭判定（与 decay.*=0 的约定一致），返回 0。
    """
    window = push_outcome_window_hours(conn) if window_hours is None else int(window_hours)
    if window <= 0:
        return 0
    now_dt = parse_utc(now) if now else datetime.datetime.now(datetime.timezone.utc)
    if now_dt is None:
        raise ValueError(f"now 必须是 ISO8601 UTC（如 2026-10-24T01:00:00Z），收到 {now!r}")
    cutoff = (now_dt - datetime.timedelta(hours=window)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = conn.execute(
        "UPDATE push_log SET reaction='ignored' WHERE reaction IS NULL AND pushed_at < ?",
        (cutoff,))
    conn.commit()
    return cur.rowcount or 0


# `related_citizens` 允许的**锚定列白名单**（关系名 → 列名）。
# 为什么是白名单而不是让调用方传列名：SQL 里的列名只能来自实现内部的常量表，
# 传进来的永远是**值**。否则这个函数就是一个 ORDER BY/WHERE 注入口。
# 2026-11-21（F-3）：身份诊断搬进 `fish_identity_diag`（约 110 行）——
# `fish.py` 是满窗口，而「诊断」本该有独立位置（它不发凭证、不裁决归属）。
# 这里只留转发，既有调用方零改动。
from fish_identity_diag import (RELATED_ANCHORS, RELATED_CITIZENS_LIMIT,  # noqa: F401
                                related_citizens)
from fish_identity_diag import (attribute_session as _attr_session,
                                render_unattributed_line as _unattr_line)
# 2026-11-21（A-3）：体检要复用 `scan_broken`（它只扫 active、带日期结构）。
# 方向是 `fish → fish_purge`（后者 J2 禁 import fish，反向才合法）。
import fish_purge


# CLI 入口的两个文件名（FB-13）。`install_cli.py --apply` 生成的就是它们：
#   fish.cmd        纯 ASCII 的一行转发（`@python "%~dp0fish_launch.py" %*`）
#   fish_launch.py  承载真实路径的启动器（Python 按 UTF-8 读源码 → 不受代码页影响）
# 这两个名字在 `install_cli.py` 里也有一份（它不能 import fish：那会把整个核心拖进来）。
# 两处必须一致——`test_cli_entry.py` 有判据钉住，改名时不会一漏一改。
CLI_ENTRY_SHIM = "fish.cmd"
CLI_ENTRY_LAUNCHER = "fish_launch.py"


def cli_entry_hint(p0_dir=None) -> str:
    """回执里的「CLI 入口」一行（FB-13）。**唯一实现**，whoami / register 共用。

    为什么把它写进回执：装好 MCP 的 AI 手上只有**工具**，没有**命令**。它想跑
    `fish session start` 时会得到 `'fish' 不是内部或外部命令`，然后**放弃并绕路**——
    绕路的具体形态就是"身份未核"的日常来源（明明能签 machine 级，却一直用
    self-reported 在跑）。一行路径的成本，换掉一次静默降级。

    2026-11-11：改成**状态感知**。以前不管装没装都说"去跑 install_cli.py --apply"，
    于是装好了它也看不出来（还得自己试一次）；更糟的是**装了却不说**会让 AI 以为
    只能用工具、继续绕路。现在按实况分两种说法。

    判定"已就绪"要看两件事，缺一不可：
      ① `shutil.which("fish")` 找得到（在 PATH 上）；
      ② 它旁边有 `fish_launch.py`（确认是**我们**的 shim，而不是别的同名程序）。
    只看 ① 会把一个撞名的 `fish.exe` 报成"已就绪"——那比不说更误导。
    """
    p0 = Path(p0_dir) if p0_dir else Path(__file__).resolve().parent
    found = shutil.which("fish")
    if found:
        try:
            beside = Path(found).resolve().parent / CLI_ENTRY_LAUNCHER
        except OSError:      # noqa: BLE001 —— 路径解析失败就当没装，别在回执里抛
            beside = None
        if beside is not None and beside.exists():
            return "CLI 入口：fish（已就绪，`fish --help` 验证）"
    return (f"CLI 入口：python {p0 / 'fish.py'}"
            f"（或 install_cli.py --apply 生成 {CLI_ENTRY_SHIM}，之后可直接敲 fish）")


# 物理路径归一（junction / symlink / 8.3 短路径）**唯一实现在 `fish_surface.canonical_path`**。
#
# 2026-11-24（112 W-C · A2）：原先这里有一份、`fish_surface` 没有，于是 hook 实际走的
# `fish_activity.resolve_project`（→ `fish_surface.longest_prefix`）拿不到 realpath 兜底——
# 同一 payload 两个解析器给不同项目（实测反例见 112 §三 A2）。
# `fish._canonical_path` 这个名字保留为**转发别名**：既有调用与 `test_workspace_map` 依赖它，
# 而它现在与 `fish_surface.canonical_path` 是**同一个对象**（不是副本）。
_canonical_path = fish_surface.canonical_path


# ------------------------------------------------------ 公民归档（FB-12 · 30 天裁决的另一半）
#
# 2026-10-25 的裁定只落到了一半：配置项 `session.archive_after_days` 有、冷表
# `citizens_archive` 有、`citizens_all` 视图也有，但**没有任何调用点**——于是整条
# 策略是"写着但不存在"的状态（冷表永远是空的，热表永远只增）。下面补齐执行面。
#
# 五条判据必须同时满足才动，缺一条就跳过并写明理由：
#   ① `archived_at IS NULL`      —— 没归档过（幂等）
#   ② `is_owner=0`               —— 主人永不自动归档（搬走主人 = 系统没有主人）
#   ③ `last_seen_at` 早于 now-days —— 闲置；**没有 last_seen_at 一律跳过**（无法判定）
#   ④ 无未关档期（`project_stints.to_ts IS NULL` 不存在）—— 正在项目上干活
#   ⑤ 无未失效凭证（未吊销且(永久或未到期)）—— 手里还有能用的 token
# 为什么 ④⑤ 是硬门槛：搬走一个正在干活/还握着凭证的公民，它下次调用会**查不到自己**
# （变成"未归属"）却毫无报错——那是静默降级，比不归档糟得多。
CITIZEN_ARCHIVE_DAYS_KEY = "session.archive_after_days"


# ------------------------------------------------------ 陈旧性（2026-10-26 · T04）
# 用户点名的核心：**相关性对了，但信息过时 = 错的信息**，比不推更糟。
#
# 为什么参考时间必须是 `occurred_at/recorded_at`（**内容年龄**）而不是
# `last_accessed_at`：`_touch()` 在每次注入展示时都会刷新 `last_accessed_at`
# （mcp_server.py:1090-1108），用它算陈旧性会把"刚被展示过的老事实"当成新鲜——
# 那正是这个功能要防的错。`access_count` 只做小幅置信度加成，绝不改年龄。
#
# 为什么复用 `compute_decay`：半衰期参数与 dormant 门槛只许有一处定义
# （`fish.py:4451` 那一份）；新造一套衰减公式等于迟早两套口径打架。




# ------------------------------------------------------------------ 命令

def cmd_init(args):
    root = Path(args.root)
    conn = connect(root)
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.commit()
    (root / "projects").mkdir(exist_ok=True)
    (root / "citizens").mkdir(exist_ok=True)
    append_event(conn, "config.changed",
                 {"citizen_id": "system", "evidence_level": "signed"},
                 {"kind": "config", "id": "init"},
                 {"key": "schema_version", "new_value": "p1a"})
    print(f"已初始化：{root}")
    # 2026-10-26（FB-09）：初始化是**唯一**该提"下一步"的时刻——空库里既没有主人、
    # 也没有凭证，只丢一句"已初始化"等于把人留在原地（`EVAL-261025-05` 的实测路径：
    # 试 add_project → 试 healthcheck → 最后才想到 CLI）。
    # 已有主人时保持安静：重复 init 是幂等的补装动作，每次都念一遍引导只是噪音，
    # 而且会把这次真正新增的输出淹掉。
    # 判定复用 `resolve_owner`（含旧的 config 回退），不在这里另写一条 SELECT——
    # "谁是主人"只能有一处判断，两份实现必然漂。
    if not resolve_owner(conn):
        print("下一步：设置主人 → fish owner <citizen_id>")
        print("签发凭证 → fish session start --project <项目>")
    conn.close()


def cmd_project_add(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    # EVAL-261025-17：与 MCP 面同口径——**先查库、再校验字符**（2026-10-25 修）。
    # 已存在的存量前缀（可能含 `-`）必须先报"已存在"，不能被字符规则误报成"非法"。
    row = conn.execute("SELECT namespace FROM code_registry WHERE prefix=?",
                       (args.prefix,)).fetchone()
    if row:
        print(f"项目已存在：{args.prefix} → {row[0]}")
        conn.close()
        return
    if "-" in args.prefix:
        print("错误：prefix 不能含 '-'（编码用它分隔段，如 ANYI-261022-01；"
              "含 '-' 的存量前缀不适用本命令）", file=sys.stderr)
        conn.close()
        return
    if len(args.prefix) > 20:
        print("错误：prefix 太长（≤20 字符）", file=sys.stderr)
        conn.close()
        return
    # 写前查重（2026-10-25 · 阶段 C）：只提示不拦——精确重名上面已经先挡掉。
    # 放在 INSERT 之前算，才是"写前"；提示走 stderr，stdout 保持"一行结果"的契约。
    hints = find_similar_projects(conn, args.prefix, namespace=args.name)
    try:
        conn.execute(
            "INSERT INTO code_registry (prefix, namespace, last_date, last_seq, status) VALUES (?,?,?,?,?)",
            (args.prefix, args.name, "", 0, "active"))
        append_event(conn, "project.opened",
                     {"citizen_id": "system", "evidence_level": "signed"},
                     {"kind": "project", "id": args.prefix},
                     {"prefix": args.prefix, "name": args.name}, commit=False)
        # 2026-10-25（看板 C 批）：**新项目一开自动出看板**（`09` §8.3 C1 的判据）。
        # 为什么放在 cmd_project_add 而不是 append_event 里：事件层不该知道"看板"这个概念——
        # 事件是不可变事实流，看板是组织层的投影层。让事件层依赖投影层，会污染 ADR-001。
        # 为什么失败不阻断：出看板是"顺手做的事"，不该成为开项目的前提。
        try:
            _b = board_ensure(conn, "project", args.prefix)
            board_render(conn, Path(args.root), _b)
        except Exception as _e:      # noqa: BLE001 —— 见上：不阻断开项目
            print(f"（看板自动创建失败，可稍后 `fish board render --project "
                  f"{args.prefix}` 重建：{_e}）", file=sys.stderr)
        conn.commit()
        print(f"项目已开立：{args.prefix} ({args.name})")
        hint = format_dup_hint(project_hints=hints)
        if hint:
            print(hint, file=sys.stderr)
    except sqlite3.IntegrityError:
        conn.rollback()
        print(f"项目已存在：{args.prefix}")
    except Exception: conn.rollback(); raise
    conn.close()


# ------------------------------------------------------ 项目前缀生命周期（retire / restore）
#
# 背景（2026-10-24）：`code_registry` 里出现了一个空壳前缀「测试项目」（ns 同为
# 「测试项目」、last_date 空、last_seq 0、0 条记忆），与既有的 `TESTP` 同义重复。
# 前缀一边多一个，`fish workspace set` 与注入包就多一处能选错的地方。
#
# 纪律（ADR-003）：**前缀不可回收**——编码一经分配就永久指向一条记忆，删前缀等于让
# 历史编码失去解释。所以这里刻意**不提供删除**，只提供**退役**：把
# `code_registry.status` 从 active 改成 retired（`next_code` 早就在这一步拒绝分配新编码），
# 行与历史全部保留。
#
# 这一族命令的默认动作是**打印计划**，`--apply` 才落库。理由不是保守：改一个前缀的
# 状态会影响**所有后续编码分配**，它必须先让人看见"会改什么、不会改什么"再说同意。

# 项目三态（2026-10-25 · 阶段 D）：
#   active  —— 在用：可写入、可分配新编码
#   closed  —— **已闭合**：这件事做完了。不再接收新记忆，但**不删任何东西**，
#              且可 `reopen` 回来。用户拍板原话："close 其实从代码设计上是可逆的，
#              如果要写，那就打开"。闭合 ≠ 删除（决策 16）。
#   retired —— 名字废弃：停止分配新编码，前缀**永久不可再分配**（ADR-003）。
#
# 两态变三态的理由：`retired` 回答"这个名字还用不用"，`closed` 回答"这件事做完了没有"。
# 合成一个值会让"项目完成"看起来像"项目废弃"——那正是 T10 说的"系统缺已完成语义"。
PROJECT_STATUSES = ("active", "closed", "retired")

# 事件类型：retire / reopen 各一个（与 `project.opened` 同一族命名）。
_EVENT_BY_PROJECT_STATUS = {"retired": "project.retired",
                            "closed": "project.closed",
                            # close / retire 回到 active 都用同一个事件名：
                            # "重新可写"是同一件事，不该因为来路不同就分两种事件。
                            "active": "project.reopened"}

# CLI 动作 → 目标状态。`restore` 与 `reopen` 都回到 active：对使用者是两句话，
# 对状态机是一件事——所以映射到同一个值，事件也共用 `project.reopened`。
_PROJECT_ACTION_TARGET = {"retire": "retired", "restore": "active",
                          "close": "closed", "reopen": "active"}


def plan_project_status_change(conn, prefix, target, *, force=False) -> dict:
    """算一遍"把 prefix 改成 target 状态会发生什么"。**只读：不写库、不 begin。**

    返回 dict：
      found / already  前缀在不在、是不是已经是目标状态（幂等分支）
      current          现在的 status（可能是人手改的未知值，不猜）
      counts           名下记忆数 / workspace_map 引用数——决定"退役是否安全"的证据
      blockers         拦住执行的理由（人话）；空 = 可以执行
      changes          会做的每一件事（人话，含"**不会**做什么"）

    **只读是判据**（test_onboarding 判据 F）：连跑一百遍，库必须一行不变。
    所以这里没有 commit、没有 begin，只有 SELECT。
    """
    if target not in PROJECT_STATUSES:
        raise ValueError(f"目标状态只能是 {PROJECT_STATUSES}，收到 {target!r}")
    name = (prefix or "").strip()
    if not name:
        raise ValueError("项目前缀不能为空")
    row = conn.execute(
        "SELECT prefix, namespace, last_date, last_seq, status FROM code_registry "
        "WHERE prefix=?", (name,)).fetchone()
    if not row:
        return {"found": False, "already": False, "prefix": name, "target": target,
                "current": None, "namespace": None, "last_date": None, "last_seq": None,
                "counts": {"memories": 0, "mappings": 0},
                "blockers": [f"code_registry 里没有前缀 {name}"], "changes": []}
    _prefix, namespace, last_date, last_seq, current = row
    memories = conn.execute("SELECT COUNT(*) FROM code_index WHERE project=?",
                            (name,)).fetchone()[0]
    mappings = conn.execute("SELECT COUNT(*) FROM workspace_map WHERE project=?",
                            (name,)).fetchone()[0]
    blockers, changes = [], []
    plan = {"found": True, "already": current == target, "prefix": name, "target": target,
            "namespace": namespace, "last_date": last_date, "last_seq": last_seq,
            "current": current,
            "counts": {"memories": memories, "mappings": mappings},
            "blockers": blockers, "changes": changes}
    if plan["already"]:
        return plan
    if current not in PROJECT_STATUSES:
        # 不猜一个不认识的旧状态能不能动：可能是人手改的，交给执行者判断。
        blockers.append(f"当前状态 {current!r} 不在已知状态集 {PROJECT_STATUSES} 里")
    if target == "retired":
        changes.append(f"code_registry.status：{current} → retired（只改这一个字段）")
        changes.append("**不删行、不回收前缀**：历史编码仍可 resolve_code / read_body（ADR-003）")
        changes.append("last_date / last_seq 原样保留：恢复后序数接着走，旧编码不复用")
        if memories:
            changes.append(f"名下 {memories} 条记忆**不动**：退役只停分配，不搬归属")
            if not force:
                blockers.append(
                    f"名下还有 {memories} 条记忆。退役只停止分配新编码、不动这些记忆；"
                    "确认接受请加 --force（或先把记忆迁走）")
        if mappings:
            changes.append(f"workspace_map 里有 {mappings} 条映射指向它（映射本身不动）："
                           "退役后这些目录解析到的项目不再分配新编码")
    elif target == "closed":
        changes.append(f"code_registry.status：{current} → closed（这件事做完了）")
        changes.append("**不删行、不动记忆、不回收前缀**：只是不再接收新记忆"
                       "（决策 16：闭合 ≠ 删除）")
        changes.append("要再往里写，先 `fish project reopen <前缀>`"
                       "（用户拍板：闭合是可逆的状态，不是锁死）")
        if memories:
            changes.append(f"名下 {memories} 条记忆**照常可查可读**（闭合不冻结检索）")
        if mappings:
            changes.append(f"workspace_map 里有 {mappings} 条映射指向它"
                           "（映射不动，但那些目录解析出的项目在 reopen 之前不接受新记忆）")
    else:
        changes.append(f"code_registry.status：{current} → active（恢复分配）")
        changes.append("next_code 接着 last_date / last_seq 往下发号（旧编码不复用）")
    return plan


def apply_project_status(conn, prefix, target, *, reason=None, actor=None) -> dict:
    """真正落库（**只有 CLI 的 `--apply` 会走到这里**）。

    一个事务两件事：改字段 + 写事件。顺序有讲究——先 UPDATE（尚未提交）再调
    `append_event`，它的 commit 会把这条 UPDATE 一起提交；中途抛错则由调用方
    rollback，不会出现"状态改了但没留痕"的半截事实。
    """
    if target not in PROJECT_STATUSES:
        raise ValueError(f"目标状态只能是 {PROJECT_STATUSES}，收到 {target!r}")
    name = (prefix or "").strip()
    try:
        cur = conn.execute("UPDATE code_registry SET status=? WHERE prefix=?",
                           (target, name))
        if not cur.rowcount:
            raise ValueError(f"code_registry 里没有前缀 {name}")
        # 2026-10-25（用户要求 C）：**项目闭合 → 该项目所有 open 看板一起归档**。
        #
        # 为什么放在这里而不是 MCP 的 `_project_state_change`：
        # CLI 的 `fish project close --apply` 与 MCP 的 `close_project` **都走这个函数**。
        # 放在上层就一定会有一条路径漏掉——那是"两层各写一份"的老毛病。
        #
        # 为什么连看板一起关：看板按日切、**没有别的东西会关旧板**，
        # 于是 `boards` 里 open 板会日复一日累积。项目都闭合了，它的板还 open 着，
        # 语义上说不通。
        boards_closed = 0
        if target == "closed":
            boards_closed = conn.execute(
                "UPDATE boards SET status='closed', closed_at=? "
                "WHERE scope='project' AND scope_key=? AND status='open'",
                (now_utc(), name)).rowcount
        append_event(conn, _EVENT_BY_PROJECT_STATUS[target],
                     # actor：CLI 走 "system"（signed），MCP 走真实 citizen_id。
                     # 两种来源的证据级不同，不该混成一个值。
                     {"citizen_id": actor or "system",
                      "evidence_level": "signed" if not actor else "self-reported"},
                     {"kind": "project", "id": name},
                     {"prefix": name, "status": target,
                      "reason": (reason or "").strip() or "manual",
                      "boards_closed": boards_closed})
    except Exception:
        conn.rollback()
        raise
    return {"changed": True, "prefix": name, "status": target,
            "boards_closed": boards_closed}


def _render_project_plan(plan, *, apply, reason) -> int:
    """把计划打印出来；返回退出码（0 = 可以继续，2 = 拒绝执行）。"""
    if not plan["found"]:
        print(f"错误：{plan['blockers'][0]}")
        print("（前缀用 `fish project-add <前缀>` 开立；这里不会凭空建一个）")
        return 2
    verb = {"retired": "退役：停止分配新编码",
            "closed": "闭合：这件事做完了（不再接收新记忆，可 reopen）",
            "active": "恢复：改回 active（可写、可分配新编码）"}.get(
                plan["target"], plan["target"])
    print(f"项目前缀：{plan['prefix']}（{plan['namespace'] or '-'}）")
    print(f"当前状态：{plan['current']}  →  目标状态：{plan['target']}（{verb}）")
    print(f"名下记忆：{plan['counts']['memories']} 条"
          f"；workspace_map 引用：{plan['counts']['mappings']} 条")
    if plan["already"]:
        print(f"\n已经是 {plan['target']}：什么都不用做（幂等）。")
        return 0
    print("\n会做的事：")
    for line in plan["changes"]:
        print(f"  · {line}")
    if plan["blockers"]:
        print("\n现在不能执行：")
        for line in plan["blockers"]:
            print(f"  ✗ {line}")
    if not apply:
        print("\n（本次只是打印计划：库一个字节都没改。确认后加 --apply 才落库）")
        return 0
    if plan["blockers"]:
        print("\n已拒绝执行（未写库）。")
        return 2
    if not (reason or "").strip():
        print("\n⚠ 没给 --reason：事件里会记 reason=manual（少一条可追溯的说明，但不拦截）")
    return 0


def cmd_project(args):
    """`fish project retire|restore <prefix>` —— 前缀生命周期（默认只打印计划）。

    `project-add` 仍是顶层扁平命令（不改它的名字：那是已有脚本与文档在用的入口）；
    新增的这族只负责"状态"，两者不重叠。
    """
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    target = _PROJECT_ACTION_TARGET[args.project_action]
    try:
        plan = plan_project_status_change(conn, args.prefix, target,
                                          force=getattr(args, "force", False))
        code = _render_project_plan(plan, apply=args.apply, reason=args.reason)
        if code:
            sys.exit(code)
        if not args.apply or plan["already"]:
            return
        res = apply_project_status(conn, plan["prefix"], target, reason=args.reason)
        print(f"\n已执行：{res['prefix']} → {res['status']}"
              f"（事件 {_EVENT_BY_PROJECT_STATUS[target]}，可追溯）")
    finally:
        conn.close()


def cmd_alias(args):
    """`fish alias link|unlink|list` —— 名字别名（**单向**：from 就是 to）。

    为什么只给人一条命令、不给 AI 一个工具（决策 13/17 的落地）：
      别名改变的是**检索语义**——一旦建错，`recall` 的结果会静默变化（多出或少掉
      一批记忆），而且不会报错。按"AI 提议、人采纳"的口径，这条通道留给使用者。
      MCP 面刻意不提供：那是"可以远程改检索行为"的权限，不该随身份一起发出去。
    """
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    try:
        action = args.alias_action
        if action == "link":
            res = link_alias(conn, args.kind, args.from_name, args.to_name,
                             reason=args.reason or "")
            print(f"已建立别名：{res['from']} → {res['to']}（kind={res['kind']}）")
            print("（只影响检索：project 过滤会展开别名；**写入仍按原名归属**）")
        elif action == "unlink":
            ok = unlink_alias(conn, args.kind, args.from_name, reason=args.reason or "")
            print("已撤销别名。" if ok else "没有这条别名（幂等：什么都没做）")
        else:
            rows = list(conn.execute(
                "SELECT kind,from_name,to_name,created_at,reason FROM name_aliases "
                "ORDER BY kind,from_name"))
            if not rows:
                print("（还没有任何别名）")
            for kind, f, t, at, why in rows:
                print(f"{kind}\t{f} → {t}\t{at}\t{why or ''}")
            # 顺手把"哪些名字解析成同一个"报出来——人不用自己拼图。
            for kind in _ALIAS_KINDS:
                groups = {}
                for (f,) in conn.execute(
                        "SELECT from_name FROM name_aliases WHERE kind=?", (kind,)):
                    canon = resolve_name(conn, kind, f)["canonical"]
                    groups.setdefault(canon, []).append(f)
                for canon, fs in groups.items():
                    print(f"[{kind}] {canon} ← {', '.join(sorted(fs))}")
    except ValueError as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(2)
    finally:
        conn.close()


def cmd_remember(args):
    root = Path(args.root)
    conn = connect(root)
    conn.executescript(SCHEMA)
    migrate(conn)
    # 参数校验失败时不要吐 traceback：报错里已写清原因和下一步，直接读即可。
    # （getattr 兼容测试里手工构造的 args 命名空间）
    try:
        # 2026-10-26（复审 ⚪N6）：没给 `--owner` 且手上有有效 env 凭证 → 记给凭证的
        # 公民（MCP 的 `_owner_value` 就是这一句）。给了 `--owner` 时一个字都不干预。
        #
        # 2026-11-11（用户裁定「**允许无主**」）：把"没给 --owner"再拆成两种情形。
        # 旧实现把两者都交给 `resolve_owner_arg(conn, None)`，而那个函数语义是
        # 「未指定 → **系统主人**」——于是"命令行随手写一条、手上又没凭证"会被
        # **静默记在主人头上**。那不是无主，是**假归属**：一条陌生人写的东西算在
        # 主人名下，事后无法与"主人真写的"区分，比未归属更糟。
        #
        # 新口径（与 MCP 的 `_owner_value` 对齐）：
        #   · 显式给了 `--owner`  → 照旧走严格校验（拼错就报错，不静默接受）
        #   · 有 env 凭证        → 记给凭证的公民
        #   · 两者都没有          → **`unattributed`**，并打印一句怎么补归属
        #
        # 注意 `resolve_owner_arg` 本身一个字没改：它"None→系统主人"的语义仍被
        # `test_p1a` 钉着，别的调用方（以及 API 语义）都还依赖它。变的是**调用方式**。
        _explicit_owner = getattr(args, "owner", None)
        _token_cid = token_actor_citizen_id(conn)
        if _explicit_owner:
            owner = resolve_owner_arg(conn, _explicit_owner)
        elif _token_cid:
            owner = resolve_owner_arg(conn, _token_cid)
        else:
            owner = UNATTRIBUTED
            # 「尽可能避免无主」的那一半：把**怎么补**直接写在输出里，而不是静静记下。
            # 不阻断写入（记忆比归属重要），只是让"无主"这件事可见、可修。
            print(f"⚠ 本次未带凭证，归属记为「未归属」（{UNATTRIBUTED}）。"
                  f"要记给某个公民：设环境变量 FISH_SESSION_TOKEN，或显式给 --owner <citizen_id>；"
                  f"事后补：fish owner-fix <编码> --owner <citizen_id>",
                  file=sys.stderr)
        occurred_at = normalize_occurred_at(getattr(args, "occurred_at", None))
        # 外部审查 🔴6（分级写入鉴权）：`has_credential` **只看 env 凭证**，
        # 不看 `owner`——不带 `--owner` 时 `resolve_owner_arg` 会回落成系统主人
        # （一个合法 citizen_id），按它判会把"无凭证写规矩"误判成有凭证，
        # 且会把规矩错误归属给主人。CLI 没有 `--token` 是有意为之
        # （明文进 shell 历史），凭证走环境变量 `FISH_SESSION_TOKEN`。
        _cid = token_actor_citizen_id(conn)
        enforce_mem_type_auth(
            getattr(args, "type", "fact"),
            has_credential=bool(_cid),
            citizen_kind=citizen_kind_of(conn, _cid))
    except ValueError as e:
        conn.close()
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(2)
    hints = []
    # FB-11：`remember --supersedes <旧编码>` 与 MCP 的 `supersedes` 入参同一条路
    # （都进 `record_memory`），所以"新的先写成功、再置旧"这个顺序只有一个实现。
    supersede_notes = []
    code, rel = record_memory(
        conn, root,
        project=args.project, title=args.title, body=args.body or "",
        mem_type=args.type, tags=args.tags or [], visibility=args.visibility,
        owner=owner, session_actor=args.session_actor,
        attachments=args.attach or [],
                       allow_sensitive_attachments=getattr(args, "allow_sensitive_attach", False),
                       allow_unenforced_private=getattr(args, "allow_unenforced_private", False), occurred_at=occurred_at, cause=args.cause,
        supersedes=getattr(args, "supersedes", None) or None,
        hints=hints, supersede_notes=supersede_notes)
    try:
        print(f"{code}\t{args.title}\t{rel}")
    except UnicodeEncodeError:
        print(code)
    # 查重提示走 **stderr**：stdout 是 `编码\t标题\t路径` 的契约面（脚本会按 tab 切），
    # 往里塞散文就会污染解析。诊断句走 stderr 是 fish 的既有纪律（同 session start）。
    hint = format_dup_hint(hints)
    if hint:
        print(hint, file=sys.stderr)
    # 替代结果也走 stderr（同理由）：成功一行、失败一行。
    # 失败**不回滚新条目**——新记忆已经写成了，"替代没生效"是另一件事，
    # 该说的是"旧条目不存在/不能替代"，而不是把调用方的成果一起撤掉。
    for note in supersede_notes:
        print(("已替代：" if note["ok"] else "替代未生效：") + note["message"],
              file=sys.stderr)
    conn.close()


def cmd_supersede(args):
    """`fish supersede <旧编码> --by <新编码>`（FB-11）：CLI 补上 MCP 早有的入口。

    **回执与 MCP 同口径**：两边都只调 `supersede_memory` 并原样打印它的返回串
    （MCP 那句只多一个结尾换行）。双入口各自拼一段话，就是"同一个动作两种说法"
    的老毛病——AI 见到两种说法会以为做了两件不同的事。
    """
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    try:
        reason = args.reason or ""
        try:
            msg = supersede_memory(conn, args.code, args.by, reason=reason,
                                   actor=resolve_owner(conn))
        except ValueError as e:
            print(f"错误：{e}", file=sys.stderr)
            sys.exit(2)
        print(msg)
    finally:
        conn.close()


def search_by_relevance(conn, query, *, limit=10, project=None, tags=None,
                        scope="all", code=None):
    """按 bm25 相关度检索（⑥ `--by relevance`，BUG-5 的收口）。

    与 `search_memories` 的分工：那个是"编码 > 标题 > 正文"的**优先级**排序
    （可预期、可复现，默认）；这里是 FTS5 的 bm25（更接近"问什么答什么"）。
    两条路共享同一份 `memory_fts`，都只读；默认仍是 priority（既有判据零回归）。
    返回与 `search_memories` 同形 `(rows, src_of)`，让 `cmd_recall` 直接复用下游。

    **四口径与默认搜索对齐**（外部审查 R-05/P，2026-10-29）：
      · `project` 展开别名（同一个 `resolve_project_names`，`recall --project cdql`
        要能看到 `CDQL` 的记忆）；
      · `tags` 用 `tag_like_pattern` + `LIKE_ESCAPE_SQL`（`%`/`_` 不当通配符）；
      · `scope`：all/title/body 真正生效（FTS 列前缀）；
      · `code`：给了就按编码精确查（旧实现**静默忽略** `--code`）。
    """
    where = " AND ci.status = 'active'"
    params = []
    if code:
        where += " AND ci.code = ?"
        params.append(str(code))
    _pnames = resolve_project_names(conn, project) if project else []
    if _pnames:
        where += " AND ci.project IN (" + ",".join("?" * len(_pnames)) + ")"
        params.extend(_pnames)
    for _t in ([tags] if isinstance(tags, str) else (tags or [])):
        if _t:
            where += " AND ci.tags LIKE ?" + LIKE_ESCAPE_SQL
            params.append(tag_like_pattern(_t))
    expr = fts_match_expr(query) if str(query or "").strip() else None
    if expr and scope == "title":
        expr = f"title_idx:({expr})"
    elif expr and scope == "body":
        expr = f"body_idx:({expr})"
    cols = ("SELECT ci.code, ci.title, ci.project, ci.mem_type, ci.status, "
            "ci.file_path, ci.dormant, 'bm25' ")
    if expr:
        sql = (cols + "FROM memory_fts JOIN code_index ci ON ci.code = memory_fts.code "
               "WHERE memory_fts MATCH ?" + where +
               " ORDER BY bm25(memory_fts) LIMIT ?")
        rows = conn.execute(sql, [expr] + params + [int(limit)]).fetchall()
    else:
        # 只有 code/项目/标签、没有 query：按编码/项目列（不硬塞一个空 MATCH）
        sql = cols + "FROM code_index ci WHERE 1=1" + where + " LIMIT ?"
        rows = conn.execute(sql, params + [int(limit)]).fetchall()
    return rows, {r[0]: "bm25" for r in rows}


def cmd_recall(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    # 2026-10-26（FB-01）：与 MCP 的 recall 同一套三分支。
    #   · 有 query / code → 检索（并保留"未命中 + 最近条目"的回退）；
    #   · 无 query / code，有 project → **列出**（按项目 + 标签过滤，时间倒序）；
    #   · 只有 tags → 引导（tags 是附加过滤，单独用没有范围可列）。
    # 为什么 CLI 要跟着改：`--project` / `--tags` 这两个过滤器本来就在，缺的只是
    # "不给关键词时也能用"这一档。若只有 MCP 面能"列"，同一句诉求在两个入口
    # 会得到不同答案——那正是本轮要收掉的漂移。
    q = (getattr(args, "query", None) or "").strip()
    project = getattr(args, "project", None)
    code = getattr(args, "code", None)
    tags = getattr(args, "tags", None)
    limit = int(getattr(args, "limit", 10) or 10)
    if not q and not code:
        if not project:
            if tags:
                print(TAGS_ONLY_HINT)
            else:
                print("要查什么？给一个关键词（fish recall <词>）、编码（--code）"
                      "或项目（--project）——本命令不猜。")
            conn.close()
            return
        _since = getattr(args, "since", None)
        _until = getattr(args, "until", None)
        rows = list_memories(conn, project=project, tags=tags, limit=limit,
                             since=_since, until=_until)
        total = count_memories(conn, project=project, tags=tags,
                               since=_since, until=_until)
        print(format_list_declaration(project=project, tags=tags,
                                      shown=len(rows), total=total))
        if not rows:
            # 评审 ⚪11：措辞要按**有没有给 tags** 分支。旧实现在没给 tags 时也说
            # "去掉 --tags 看全部"——让读者去删一个他根本没写的参数。提示的每一步
            # 都必须是**这个人此刻能做的动作**，否则它只是在暴露实现细节。
            if [t for t in ([tags] if isinstance(tags, str) else (tags or [])) if t]:
                print("（该项目下没有符合标签条件的条目；去掉 --tags 看全部）")
            else:
                print("（该项目下还没有记忆；有内容后这里会按时间倒序列出）")
            conn.close()
            return
        record_access(conn, [r[0] for r in rows], "recall")
        print(RECALL_DIMENSION_NOTE)
        for code_, title, proj, mtype, status, fp, dormant, src in rows:
            print(f"{code_}\t{format_status_tag(mtype, status, dormant)}"
                  f"\t{title}\t{fp}\t匹配:{src}")
        conn.close()
        return
    ensure_fts(conn, Path(args.root))
    if (getattr(args, "by", "priority") or "priority") == "relevance" and q:
        rows, src_of = search_by_relevance(conn, q, limit=limit,
                                           project=project, tags=tags,
                                           scope=getattr(args, "scope", "all"),
                                           code=code)
    else:
        rows, src_of = search_memories(
            conn, query=q, code=code, project=project, tags=tags,
            scope=getattr(args, "scope", "all"), limit=limit,
            since=getattr(args, "since", None), until=getattr(args, "until", None))
    # ⑥ 统计行（外部审查 R-03/Q 的收口，2026-10-29）：
    # 旧实现把"匹配来源分布（含被 status/superseded 过滤掉的）"与"返回条数"并排写，
    # 读者会做加法——真实库出现过 `8+7=15≠10`；而"扫描 N 条"是项目/标签的**全库量**、
    # 与 query 无关，于是出现更荒谬的 `扫描 0 条 ｜ … ｜ 返回 3`（`--code` 搜索时）。
    # 新口径：**只按最终 rows 重建**——"命中"就是这次真正匹配并返回的条数。
    hit_counts = {"编码": 0, "标题": 0, "正文": 0}
    for _row in rows:
        src_text = str(_row[7]) if len(_row) > 7 else ""
        key = ("编码" if "编码" in src_text
               else "标题" if "标题" in src_text else "正文")
        hit_counts[key] += 1
    total_hit = sum(hit_counts.values())
    stats_line = (f"> 命中 {total_hit} 条 ｜ 编码 {hit_counts['编码']} ｜ "
                  f"标题 {hit_counts['标题']} ｜ 正文 {hit_counts['正文']} ｜ "
                  f"返回 {len(rows)}")
    if not rows:
        cand = recent_fallback(conn, project=project, tags=tags)
        print(f"未找到「{q}」。最近的条目：")
        for c, t in cand:
            print(f"  {c}\t{t}")
        print(stats_line)
        conn.close()
        return
    # 记录访问：刷新衰减，被想起即唤醒（并写 memory.accessed 事件，见 07 D-12）
    record_access(conn, [r[0] for r in rows], "recall")
    # 2026-10-26（FB-10）：维度说明先说话——第一行就交代「+dormant 是什么」，
    # 否则标签得靠读者猜。状态列走 `format_status_tag`，与 MCP 的 recall 共用
    # 同一个常量 + 同一个函数：两处各写一份必然会漂。
    print(RECALL_DIMENSION_NOTE)
    # F-P2-9 补（2026-11-19）：**正文已经不在的行要当场说出来**。
    # INC-20261119-01 之后生产库有 335 条记录的正文文件没了；只列编码+标题的话，
    # 读者会以为它们是正常的，直到 `read` 才发现空——那等于把损失藏到第二步。
    # 判的是**盘上文件在不在**（`fp` 是相对 data_root 的路径），一次 stat 的事。
    root = Path(args.root)
    missing = 0
    for code, title, proj, mtype, status, fp, dormant, src in rows:
        gone = bool(fp) and not (root / fp).exists()
        missing += 1 if gone else 0
        print(f"{code}\t{format_status_tag(mtype, status, dormant)}"
              f"\t{title}\t{fp}\t匹配:{src}" + ("\t**正文缺失**" if gone else ""))
    print(stats_line)
    if missing:
        print(f"> ⚠ 其中 {missing} 条**正文文件不在盘上**（`fish read <编码>` 会说明）。"
              "历史原因见 INC-20261119-01。")
    conn.close()


def cmd_neighbors(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    strategy = args.strategy or get_config(conn, "neighbors.strategy")
    n = args.n if args.n is not None else get_config(conn, "neighbors.n")
    cross = args.cross_project if args.cross_project is not None else get_config(conn, "neighbors.cross_project")
    inc_dormant = args.include_dormant if args.include_dormant is not None else get_config(conn, "neighbors.include_dormant")

    res = neighbors(conn, args.code, n=n, strategy=strategy,
                    cross_project=cross, include_dormant=inc_dormant)
    if res is None:
        print(f"编码不存在：{args.code}")
        conn.close()
        return
    print(f"策略={res['strategy']} n={n} 跨项目={cross} 含休眠={inc_dormant}")
    if "prev" in res:
        print("前：")
        for c in reversed(res["prev"]):
            print(f"  {c['code']}\t{c['title']}")
        print(f"← {args.code}")
        print("后：")
        for c in res["next"]:
            print(f"  {c['code']}\t{c['title']}")
    else:
        if not res["hits"]:
            print("  （没有符合条件的邻近项：可能该策略下无同类记忆，"
                  "或需要 --cross-project / --include-dormant）")
        for c in res["hits"]:
            sc = f"[{c['score']}] " if "score" in c else ""
            print(f"  {sc}{c['code']}\t{c['title']}")
    conn.close()


def cmd_list(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    # 第五轮评审 🔴：清单里我写"只是渲染 list_memories 的结果，不自己查"——
    # **与源码相反**：这里自己跑 SQL 并把每一行的标题打印出来。
    # 修法不是改理由，是让它过滤。理由错了说明代码错了。
    _clv, _clp = _visibility_sql(conn, token_actor_citizen_id(conn))
    rows = conn.execute(
        "SELECT code,mem_type,status,dormant,title FROM code_index WHERE 1=1"
        + _clv + " ORDER BY code", _clp).fetchall()
    print(f"共 {len(rows)} 条")
    for code, mt, st, dm, title in rows:
        print(f"{code}\t{mt}\t{st}\t{'休眠' if dm else '活跃'}\t{title}")
    conn.close()


def cmd_forgetting(args):
    """列出衰减情况；--apply 时把 dormant 落库并写事件。"""
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    rows = conn.execute(
        "SELECT code,mem_type,last_accessed_at,recorded_at,dormant FROM code_index").fetchall()
    cfg = load_decay_cfg(conn)
    to_dormant, to_archive, stats = [], [], {}
    for code, mt, la, ra, dm in rows:
        decay, state = compute_decay(mt, la, ra, cfg)
        stats[state] = stats.get(state, 0) + 1
        if state == "dormant" and not dm:
            to_dormant.append(code)
        elif state == "archived":
            to_archive.append(code)
    print(f"记忆 {len(rows)} 条 ｜ 活跃 {stats.get('active', 0)} ｜ "
          f"休眠 {stats.get('dormant', 0)} ｜ 归档 {stats.get('archived', 0)}")
    if to_dormant:
        print(f"应转休眠 {len(to_dormant)} 条：{', '.join(to_dormant[:10])}"
              + (" ..." if len(to_dormant) > 10 else ""))
    if to_archive:
        print(f"已归档 {len(to_archive)} 条（仍可按编码召回）")
    if args.apply and to_dormant:
        try:
            conn.executemany("UPDATE code_index SET dormant=1 WHERE code=?",
                             [(c,) for c in to_dormant])
            for c in to_dormant:
                append_event(conn, "memory.dormant",
                             {"citizen_id": "system", "evidence_level": "signed"},
                             {"kind": "memory", "id": c},
                             {"code": c, "reason": "decay_below_threshold"}, commit=False)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        print(f"已标记休眠 {len(to_dormant)} 条，并写入 memory.dormant 事件")
    conn.close()


def cmd_distill(args):
    """FB-15：把窗口内同主题的 episode 聚成「提炼建议」。

    **默认演练**（只打印，不落文件）——建议层的第一条硬约束是"不自动入库"，
    演练是它最便宜的执行方式：先看聚成了什么，再决定要不要留痕。
    `--write` 只调 `write_distill_suggestion`：不写记忆、不改状态、不删任何东西。
    """
    root = Path(args.root)
    conn = connect(root)
    conn.executescript(SCHEMA)
    migrate(conn)
    try:
        try:
            res = distill_clusters(conn, project=args.project, days=args.days,
                                   min_group=args.min, max_groups=args.max_groups, viewer_citizen_id=token_actor_citizen_id(conn))
        except ValueError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            # 评审 🟡8：原来这里是 `return 2`，但 `main()` 结尾只顾调 `args.func(args)`、
            # **不传播返回值**——于是"参数错了"在退出码上表现为成功（0）。
            # 退出码是脚本唯一能被机器读到的结论，写错它等于让调用方的 `&&` 链条失效。
            # 与 `cmd_supersede` 同口径：参数错误 = 显式 `sys.exit(2)`。
            # （`try/finally` 里的 `conn.close()` 仍会执行：SystemExit 也走 finally。）
            sys.exit(2)
        clusters = res["clusters"]
        if not clusters:
            print(f"未找到可提炼的同主题 episode"
                  f"（窗口 {res['days']} 天，组内 ≥{res['min_group']} 条）")
            return 0
        print(f"窗口 {res['days']} 天 ｜ episode {res['checked']} 条 ｜ "
              f"同主题组 {len(clusters)} 个（组内 ≥{res['min_group']} 条）")
        for cluster in clusters:
            codes = [ep["code"] for ep in cluster["episodes"]]
            preview = "、".join(codes[:DISTILL_PREVIEW_CODES])
            if len(codes) > DISTILL_PREVIEW_CODES:
                preview += f" …（共 {len(codes)} 个）"
            print(f"  {cluster['tag']} ｜ {cluster['count']} 条 ｜ {preview}")
        if args.write:
            path = write_distill_suggestion(root, res["project"], res)
            print(f"建议已写入：{path}")
            print("（这是建议，不是记忆：未入库、不自动生效；"
                  "确认后由人或 AI 另行 remember）")
        else:
            print("（演练：未落文件。确认无误后加 --write 生成建议文件）")
        return 0
    finally:
        conn.close()


def cmd_citizen_add(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    if args.kind not in KINDS:
        print(f"kind 只能是 {KINDS}")
        conn.close()
        return
    cid = ulid()
    try:
        conn.execute("INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (cid, args.name, args.kind, args.role, args.host, "active", now_utc()))
        append_event(conn, "citizen.registered",
                     {"citizen_id": cid, "evidence_level": "machine"},
                     {"kind": "citizen", "id": cid},
                     {"display_name": args.name, "kind": args.kind,
                      "role": args.role, "host": args.host}, commit=False)
        conn.commit()
    except Exception: conn.rollback(); raise
    print(f"{cid}\n{args.name} / {args.kind}")
    conn.close()


def cmd_whoami(args):
    """我是谁（CLI 版）：身份段 + 凭证状态 + 身份来源 + 主人 + 机器事实。

    2026-10-26（F3 · 与 MCP 的 FB-03 / FB-07 对齐）：CLI 的 `whoami` 原先只有
    「登记身份 / 主人 / 机器事实」，没有 MCP 那两行——同一个 AI 从两个入口问
    "我是谁"，会得到详略不同的两种答案，而回答里最关键的"凭什么算身份""凭证还能用多久"
    恰好只在 MCP 那边。这里补的就是那两行，且口径与 MCP **同源**：
    `credential_state_line` / `identity_source_line` 是两边共用的唯一实现。

    token 从哪来：`--token` 优先，其次 env `FISH_SESSION_TOKEN`（CLI 没有"本次调用"
    可挂，env 是它唯一的会话级载体）。两者都没有 = 没带凭证，如实写「未提供凭证」；
    判有效性只问 `validate_session_token`，CLI 不许自己再判一遍吊销/过期。
    """
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)

    token = (getattr(args, "token", None) or "").strip() or env_session_token()
    token_citizen = validate_session_token(conn, token) if token else None

    owner_id = resolve_owner(conn)
    if args.citizen:
        # 契约面（test_identity_p1a 判据 G）：这一条是**改动前就存在**的 SQL（含 role/host），
        # 一字不改——基线的价值就在于"既有查询不许被顺手重写"。
        actor = conn.execute(
            "SELECT citizen_id,display_name,kind,role,host FROM citizens WHERE citizen_id=?",
            (args.citizen,)).fetchone()
        if not actor:
            print(f"公民不存在：{args.citizen}")
            conn.close()
            return
    else:
        actor = resolve_actor(conn)

    # 2026-10-26（复审 🟠N2）：**有效凭证必须进身份段**。
    #
    # 旧实现的身份段只走 `--citizen` / `resolve_actor`，token 只喂给"凭证状态 / 身份来源"
    # 两行。于是只带 env 凭证的 CLI 会话读到自己那屏是：
    #     你：未登记访客（…请 register）        ← 身份段
    #     凭证状态：剩余 23 小时                ← 同一个 token
    #     身份来源：token（session_tokens）     ← 同一个 token
    # 一屏三句话，前一句否认后两句。而"请 register"还是 CLI 根本没有的命令
    # （register 是 MCP 工具），照做只会造出第二个公民。
    #
    # 现在：token 有效 → 身份段以**凭证归属**为首句（与 MCP 的「登记身份」同源，
    # 同一 token 两个入口给出**同一个 citizen_id**）；名字解析出来的 `actor` 若存在
    # 就照旧再印一行（有 `--citizen` / FISH_ACTOR 的场景），两者不同则当场点明
    # 以哪个为准——"两套视图"只有在明面上才不伤人。
    token_owner_row = None
    if token_citizen:
        # 复用一个**已存在**的读点（`load_citizen` → citizens_all 全列），
        # 不在 `cmd_whoami` 里新增第三条 citizens 查询：判据 G 冻结的是**函数内的语句集**，
        # 新增一条同样会被判"改了契约"。新逻辑要新查询时，把它放进有名字的函数里。
        token_owner_row = load_citizen(conn, token_citizen)
    if token_citizen:
        t_name = (token_owner_row or {}).get("display_name") or token_citizen
        t_kind = (token_owner_row or {}).get("kind") or "unknown"
        print(f"凭证归属：公民 {token_citizen}（{t_name}，形态 {t_kind}）")
        print("　这条身份来自 session_tokens（与 MCP 的「登记身份」同源："
              "同一 token 两个入口给同一个 citizen_id）。")

    if actor:
        print(f"登记身份：{actor[1]}（公民 {actor[0]}，形态 {actor[2]}，"
              f"角色 {actor[3] or '-'}）")
        print("说明：这是你在 fish 的账户名（宿主级），与你的宿主身份不冲突。")
        if token_citizen and actor[0] != token_citizen:
            print(f"注意：名字解析到公民 {actor[0]}，凭证归属 {token_citizen}——"
                  "写入归属**以凭证为准**（FISH_ACTOR / config.actor_name 指向了别人）。")
    else:
        an = get_config(conn, "actor_name")
        if token_citizen:
            # 有效凭证已在上面的「凭证归属」段说过身份了，这里**绝不再说"未登记"**。
            print("（没有按名字解析到行动者；本次身份以上面的凭证归属为准。）")
        elif an:
            print(f"你：未登记访客（actor_name={an} 未找到）")
        else:
            print("你：未登记访客（未配置 actor_name，也没有可用的会话凭证）")
        if not token_citizen:
            # 下一步必须是**CLI 真有的命令**：register 是 MCP 工具，CLI 里没有它。
            print("　· 已在库里 → `fish citizen-add <名字>`（同名幂等复用）")
            print("　· 还没有凭证 → `fish session start --project <项目>` 签发一条后带 token")

    # 2026-10-26（F3）：与 MCP 的 whoami 同顺序、同措辞的两行。
    # 放在「主人」之前：它们回答"这次回执算不算我的归属"，属于身份信息；
    # 主人是另一个人，排在身份信息之后才不打断阅读。
    print(credential_state_line(conn, token))
    print(identity_source_line(token_citizen))

    if owner_id:
        ro = conn.execute("SELECT display_name,kind FROM citizens WHERE citizen_id=?",
                          (owner_id,)).fetchone()
        if ro:
            print(f"主人：{ro[0]}（公民 {owner_id}，形态 {ro[1]}）")
    else:
        print("主人：未设置（fish config set owner_citizen <id>）")

    # 2026-10-26（评审 🟠3）：`--all` 与 MCP 的 `all=true` 对齐。
    # 位置也刻意一致（主人之后、机器事实之前）：它是"还有谁和你占着同一个上下文"
    # 的诊断面。渲染走 `fish.related_citizens_lines`——与 MCP **同一份文案**，
    # 两个入口读到的行必须逐字相同（否则同一件事在两处看起来是两件事）。
    if getattr(args, "all", False):
        print("")
        for line in related_citizens_lines(conn, token_citizen):
            print(line)

    env = {k: v for k, v in
           ((k, os.environ.get(k)) for k in
            ("FISH_ACTOR", "DSH_SESSION_ID", "CODEBUDDY_SESSION_ID", "CLAUDE_SESSION_ID"))
           if v}
    if env:
        print(f"机器事实：{env}")
    conn.close()


# ---------------------------------------------------------------- 推送候选（P1-a）
# 设计来源：`16` §5。要点四条——
#   ① 判据是「收益 − 成本」，不是"这条有没有用"（只有收益项的公式会退化成尽量不说话）
#   ② 四类候选 50/25/15/10，**意外类至少 1 条**（防茧房闸 2）
#   ③ **防静默失败**：降权可以，归零不行（归零 = 吸收态，`15` 调研的核心修正）
#   ④ 每条必须带 reason（可解释性是契约，不是可选装饰）
# 与 ③④⑤ 的分工：那三段"按类型取固定条数"，本段"按分数选、带理由"；
# 两者**必须不重复编码**——同一条记忆在包里出现两次等于白占预算（由 exclude 保证）。

_PUSH_KIND_RATIO = {"rule": 0.50, "association": 0.25,
                    "situational": 0.15, "serendipity": 0.10}


def cmd_inject(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    # 与 MCP 的 tool_inject 保持一致：优先 actor（FISH_ACTOR / --citizen），
    # 而不是 default_citizen。否则 CLI 注入会把主人的身份当成行动者。
    if args.citizen:
        cid = args.citizen
    else:
        actor = resolve_actor(conn)
        cid = actor[0] if actor else None
    if not cid:
        # 2026-10-25 改（`EVAL-261025-04`）：原文「未登记行动者；设 FISH_ACTOR」
        # 把「没有行动者」说成了「没登记」——它是个可能错误的归因，
        # 而且指的修法（设 FISH_ACTOR）对 MCP 面根本无效（那是进程级变量）。
        # 改成中性描述 + **两条可执行路径**。
        # 2026-10-26（复审 ⚪N6）：解析链已含 `FISH_SESSION_TOKEN`（env 凭证），
        # 所以这句话现在只在"名字、配置、凭证三样都没有"时出现——提示同步补上凭证那一路。
        print("本次未确定行动者（未给 --citizen；FISH_ACTOR / config.actor_name / "
              "FISH_SESSION_TOKEN 都没能给出一个公民）。\n"
              "　· 已登记过 → `fish inject --citizen <你的 citizen_id>`\n"
              "　· 还没有凭证 → `fish session start --project <项目>` 签发一条"
              "（或 `fish citizen-add <名字>`）")
        conn.close()
        return
    # 2026-10-26（评审 🟠3）：`--sections` / `--no-board` 与 MCP 的 `sections` /
    # `include_board` 同口径（归一在同一函数里，见 `normalize_inject_sections_arg`）。
    # 缺省（两个都不给）= 全段 = 旧行为，一个字节都不变。
    try:
        want = normalize_inject_sections_arg(getattr(args, "sections", None),
                                             False if getattr(args, "no_board", False)
                                             else None)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        conn.close()
        sys.exit(2)
    pack, meta = build_injection_pack(conn, Path(args.root), cid, args.budget,
                                      project=args.project, cwd=args.cwd,
                                      sections=want)
    # 2026-10-26（T03）：`--auto-presence` 只在**显式传**时写（WorkBuddy 会话开场用）。
    # MCP 的 `tool_inject` 刻意不写：每次 inject 都刷在场，等于"打开看板就算上班"。
    # ⚠️ 自动签到绝不能往 stdout 打字：`inject --json` 的 stdout 是契约面，
    #     WorkBuddy hook 直接 `json.loads` 它。
    if getattr(args, "auto_presence", False):
        try:
            # 2026-10-26（撞车自撞修复 T02）：会话键优先取 `--session-key`，
            # 其次取环境变量 `FISH_SESSION_KEY`（与 `board checkin` 同一条回落链）。
            # WorkBuddy hook 会显式传 `codebuddy:<session_id>`。
            _auto_checkin_after_start(
                conn, {"citizen_id": cid,
                       "projects": [meta.get("project")] if meta.get("project") else [],
                       "session_key": (getattr(args, "session_key", None)
                                       or os.environ.get("FISH_SESSION_KEY"))},
                getattr(args, "cwd", None))
        except (sqlite3.Error, ValueError) as exc:
            warn_ignored('fish.py:8597 cmd_inject', exc)
    if args.json:
        print(json.dumps({"pack": pack, **meta}, ensure_ascii=False, indent=1))
    else:
        print("──── 注入包（权威当下） ────")
        print(pack)
        tail = (f" 裁剪={meta['trimmed']}" if meta["trimmed"] else "")
        flag = " ⚠ 超预算" if meta["over_budget"] else ""
        src = f" 项目来源={meta['project_source']}"
        print(f"──── {meta['tokens']} / {meta['budget']} token "
              f"(L0={meta['layers']['L0']} L1={meta['layers']['L1']} "
              f"看板={meta['layers'].get('board', 0)} "
              f"规矩={meta['layers']['rules']} 注意={meta['layers']['attention']} "
              f"L2={meta['layers']['L2']}){src}{tail}{flag} ────")
    conn.close()


def cmd_owner_fix(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    try:
        res = fix_owner(conn, args.code, args.citizen_id, reason=args.reason or "")
    except ValueError as e:
        print(f"错误：{e}", file=sys.stderr)
        conn.close()
        sys.exit(2)
    print(f"归属已修正：{res['code']}  {res['old_owner']} → {res['new_owner']}"
          f"（{res['display_name']}）")
    if args.reason:
        print(f"　理由：{args.reason}")
    conn.close()


def cmd_owner(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    if args.action == "show":
        oid = resolve_owner(conn)
        if not oid:
            print("未设置主人（fish owner set <citizen_id>）")
        else:
            row = conn.execute(
                "SELECT display_name,kind,is_owner FROM citizens WHERE citizen_id=?",
                (oid,)).fetchone()
            if row:
                src = "citizens.is_owner" if row[2] else "旧配置回退"
                print(f"{oid}\t{row[0]}\t{row[1]}\t来源={src}")
            else:
                print(f"⚠ 悬空：config 指向 {oid}，但 citizens 表里没有这个公民")
    else:
        try:
            name = set_owner(conn, args.citizen_id)
            print(f"主人已设为：{name}（{args.citizen_id}）")
        except (ValueError, KeyError) as e:
            print(f"错误：{e}")
    conn.close()


def _require_ulid(conn, value, label):
    """只接受 ULID（评审 R-02：生产有 2 组同名活跃公民，按名字解析会合并错人）。"""
    text = str(value or "").strip()
    if not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", text):
        raise ValueError(
            f"{label} 必须是 ULID（26 位 Crockford），收到 {value!r}；"
            "用 `fish citizen-list` 查 ID，别用显示名")
    return text


def _ownership_tables(conn):
    """自动发现"归属表"：任何带 `citizen_id` / `owner` 列的表（评审 R-02 的覆盖缺口）。

    排除 `citizens` 自身（`merged_into` 标记单独写）。
    """
    out = []
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"):
        if name in ("citizens", "citizens_archive", "session_tokens"):
            # `citizens` / `citizens_archive` 是**身份本体**（冷热两处），`session_tokens`
            # 是**凭证**——合并都不迁移：迁凭证等于把 src 的 token 变成 dst 身份（提权，
            # 外部审查 🔴10）；改归档表会把"曾经的身份"也改写，破坏可追溯。
            continue
        try:
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info({name})")}
        except sqlite3.Error:
            continue
        for col in ("citizen_id", "owner"):
            if col in cols:
                out.append((name, col))
    return sorted(out)


def citizen_merge(conn, *, src, dst, apply=False) -> dict:
    """把 src 公民的全部归属改指向 dst（⑥ 身份合并，2026-10-28）。

    **安全边界**（评审 R-02 + 安全 S-03 收口）：
      · 只接受 **ULID**（生产有 2 组同名活跃公民，按名字解析会合并错人）；
      · **dst 必须 active**；**拒绝合并系统主人**（先用 `owner` 切换）；
      · 归属表由 `PRAGMA table_info` **自动发现**（`citizen_id`/`owner` 列）；
      · `apply` 走**单事务**：任一表失败 → ROLLBACK + 抛错 + **不写 merged_into**；
      · 成功写 `citizen.merged` 事件（不可抵赖）；src 不删，标 `merged_into:<dst>`。
    """
    src_id = _require_ulid(conn, src, "src")
    dst_id = _require_ulid(conn, dst, "dst")
    if src_id == dst_id:
        raise ValueError("src 与 dst 是同一个公民")
    src_row, dst_row = load_citizen(conn, src_id), load_citizen(conn, dst_id)
    if not src_row or not dst_row:
        raise ValueError(f"身份不存在：src={src} dst={dst}")
    # 链式合并（A→B→C，外部审查 🔴10）：dst 若自己已被合并过，解析到最终目标。
    for _ in range(8):
        st = str(dst_row.get("status") or "")
        if not st.startswith("merged_into:"):
            break
        dst_id = st.split(":", 1)[1]
        dst_row = load_citizen(conn, dst_id)
        if not dst_row:
            raise ValueError(f"dst 的合并链断了：{dst_id}")
    else:
        raise ValueError("dst 的合并链过长（疑似环）")
    if dst_row["status"] != "active":
        raise ValueError(f"dst 必须是 active（现在是 {dst_row['status']}）")
    owner_row = conn.execute(
        "SELECT is_owner FROM citizens WHERE citizen_id=?", (src_id,)).fetchone()
    if owner_row and int(owner_row[0] or 0):
        raise ValueError("拒绝合并系统主人：先用 `owner` 切换主人再合并")
    # 外部审查 🔴10：**dst 是 owner 也要拒**——合并会把 src 的凭证/归属都变成主人
    # 身份（持 src token 者合并后即持 owner），那是提权路径。
    dst_owner = conn.execute(
        "SELECT is_owner FROM citizens WHERE citizen_id=?", (dst_id,)).fetchone()
    if dst_owner and int(dst_owner[0] or 0):
        raise ValueError("拒绝合并到系统主人（会变成身份提权）：先切换主人再合并")
    tables = _ownership_tables(conn)
    plan = {}
    for name, col in tables:
        try:
            plan[f"{name}.{col}"] = int(conn.execute(
                f"SELECT COUNT(*) FROM {name} WHERE {col}=?",
                (src_id,)).fetchone()[0] or 0)
        except sqlite3.Error:
            plan[f"{name}.{col}"] = "?"       # 判不到 ≠ 0（评审 R-02：别把错误说成空表）
    out = {"src": src_id, "dst": dst_id, "apply": bool(apply),
           "src_name": src_row.get("display_name"),
           "dst_name": dst_row.get("display_name"), "plan": plan}
    if not apply:
        return out
    moved = {}
    try:
        # 外部审查 🔴10/🔴11：凭证**先吊销、不迁移**——迁移会把 src 的 token 变成
        # dst 身份（提权），而"先吊销再迁"会让吊销查不到行。顺序必须是先吊销。
        revoke_session_tokens(conn, citizen_id=src_id)
        for name, col in tables:
            if name == "project_stints":
                # `(citizen_id, project)` 唯一约束（外部审查 🔴10）：src 与 dst 同项目时
                # 直接 UPDATE 必撞。只迁 dst **没有**的项目，其余行留在 src（历史可查，
                # 且 merged_into 已标明去向）。
                cur = conn.execute(
                    "UPDATE project_stints SET citizen_id=? WHERE citizen_id=? "
                    "AND project NOT IN (SELECT project FROM project_stints "
                    "WHERE citizen_id=?)", (dst_id, src_id, dst_id))
                moved[f"{name}.{col}"] = int(cur.rowcount or 0)
                continue
            cur = conn.execute(f"UPDATE {name} SET {col}=? WHERE {col}=?",
                               (dst_id, src_id))
            moved[f"{name}.{col}"] = int(cur.rowcount or 0)
        conn.execute("UPDATE citizens SET status=? WHERE citizen_id=?",
                     (f"merged_into:{dst_id}", src_id))
        # 🔴9（外部审查）：事件与业务写同一事务（append_event 不再自己 commit）
        append_event(conn, "citizen.merged", {"citizen_id": dst_id}, src_id,
                     {"src": src_id, "dst": dst_id, "moved": moved}, commit=False)
        conn.commit()
    except Exception:
        conn.rollback()          # 单事务：半迁移绝不留盘（评审 R-02 的核心）
        raise
    out["moved"] = moved
    return out


def cmd_citizen_merge(args):
    """`fish citizen-merge <src> <dst> [--apply]`（默认 dry-run）。"""
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    try:
        res = citizen_merge(conn, src=args.src, dst=args.dst,
                            apply=bool(getattr(args, "apply", False)))
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        conn.close()
        raise SystemExit(1)
    print(f"{'应用' if res['apply'] else '预演'}：{res['src']} → {res['dst']}")
    for key, n in res["plan"].items():
        print(f"  {key}: {n} 行")
    if res["apply"]:
        print("已迁移：" + ", ".join(f"{k}={v}" for k, v in res.get("moved", {}).items()))
    else:
        print("（预演模式：加 --apply 才真的改）")
    conn.close()


def cmd_citizen_restore(args):
    """把归档的公民搬回热表（F-P1-7，2026-11-18 外部审查）。

    为什么补这个命令：`resolve_owner_arg` 的报错一直写着「先 citizen-restore 再绑」，
    而 `fish` 从没有这个命令——**提示指向一个不存在的东西**。归档是搬行
    （INSERT 冷表 + DELETE 热表），没有还原入口就等于不可逆，还让人以为自己能做点什么。
    """
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    try:
        res = restore_citizen(conn, args.citizen_id, dry_run=bool(args.dry_run))
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2)
    finally:
        conn.close()
    if res["dry_run"]:
        w = res["will_restore"]
        print(f"（dry-run）将把 {args.citizen_id} 搬回热表："
              f"{w.get('display_name')} / {w.get('kind')} / 归档于 {w.get('archived_at')}")
    else:
        print(f"已还原：{res['citizen_id']}（{res.get('display_name')}）")
        print("　注意：**原凭证仍然失效**，要重新签发 `fish session start`。")


def cmd_citizen_retire(args):
    """退役一个公民。若有记忆，必须显式 --migrate-to 或 --force，避免静默丢归属。"""
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)

    row = conn.execute("SELECT display_name,kind,status,is_owner FROM citizens WHERE citizen_id=?",
                       (args.citizen_id,)).fetchone()
    if not row:
        print(f"公民不存在：{args.citizen_id}")
        conn.close()
        return
    name, kind, status, is_owner = row
    if status == "retired":
        print(f"{name} 已经是 retired 状态")
        conn.close()
        return
    if is_owner:
        print(f"拒绝：{name} 是系统主人（is_owner=1），不能退役。先设新主人。")
        conn.close()
        return

    n_mem = conn.execute("SELECT COUNT(*) FROM code_index WHERE owner=?",
                         (args.citizen_id,)).fetchone()[0]
    if n_mem and not args.migrate_to and not args.force:
        print(f"拒绝：{name} 名下有 {n_mem} 条记忆。"
              f"请指定 --migrate-to <citizen_id> 或 --force（保持归属不变）")
        conn.close()
        return

    if args.migrate_to:
        tgt = conn.execute("SELECT display_name,status FROM citizens WHERE citizen_id=?",
                           (args.migrate_to,)).fetchone()
        if not tgt or tgt[1] != "active":
            print(f"目标公民无效或非 active：{args.migrate_to}")
            conn.close()
            return
        conn.execute("UPDATE code_index SET owner=? WHERE owner=?",
                     (args.migrate_to, args.citizen_id))
        print(f"已迁移 {n_mem} 条记忆 → {tgt[0]}")

    conn.execute("UPDATE citizens SET status='retired' WHERE citizen_id=?", (args.citizen_id,))
    # P1（外部审查 🔴11，2026-10-29）：退役必须**吊销**该公民的活凭证——
    # 否则"退役"只是标签，bearer token 仍能调用（实测 RETIRED_TOKEN_VALID）。
    revoked = revoke_session_tokens(conn, citizen_id=args.citizen_id)
    # 🔴9（外部审查）：状态变更、吊销与事件**同一个事务**——中途失败时
    # "退役了但没事件"这种半状态不可接受（事件是重建的依据）。
    append_event(conn, "citizen.retired",
                 {"citizen_id": "system", "evidence_level": "signed"},
                 {"kind": "citizen", "id": args.citizen_id},
                 {"display_name": name, "kind": kind,
                  "reason": args.reason or "manual",
                  "migrated_to": args.migrate_to,
                  "memories": n_mem}, commit=False)
    conn.commit()
    if revoked:
        print(f"（同时吊销 {revoked} 条活凭证）")
    print(f"已退役：{name}（{args.citizen_id}）— {args.reason or 'manual'}")
    conn.close()


def cmd_citizen_set_kind(args):
    """薄壳：实现住 `fish_db.set_citizen_kind`（fish.py 贴着行数窗口）。"""
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    try:
        kind = str(args.kind or "").strip().lower()
        if kind not in KINDS:
            print(f"错误：kind 只能是 {'/'.join(KINDS)}", file=sys.stderr)
            raise SystemExit(2)
        try:
            res = set_citizen_kind(conn, args.citizen_id, kind)
        except ValueError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            raise SystemExit(2)
        if res["changed"]:
            print(f"已改：{res['citizen_id']}（{res['display_name']}）"
                  f"{res['from']} → {res['to']}")
            if res["to"] in ("resident", "role"):
                print("　注意：这让它现在有权写 rule / identity"
                      "（会进所有人的注入包）。")
        else:
            print(f"公民 {res['citizen_id']}（{res['display_name']}）"
                  f"已经是 {res['to']}，无需改动")
    finally:
        conn.close()


def cmd_citizen_list(args):
    """列出所有公民：状态、形态、宿主、记忆数、是否主人。"""
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)

    # F-P1-X（独立评审 🟠-4b）：补 `--all`。
    # 起因：`restore_citizen` 的报错写着「先用 `fish citizen-list --all` 确认编码」，
    # 而 `citizen-list` **一个参数都没有**——提示指向不存在的东西。
    # 这和 F-P1-7 修的是同一个病：把"我做不到"伪装成"你少做了一步"。
    # 归档的公民在 `citizens` 里根本不存在（归档是搬行），所以查归档必须走视图。
    if getattr(args, "all", False):
        rows = conn.execute(
            "SELECT citizen_id,display_name,kind,host,status,is_owner,created_at,archived_at "
            "FROM citizens_all ORDER BY (archived_at IS NULL) DESC, created_at").fetchall()
    else:
        rows = conn.execute(
            "SELECT citizen_id,display_name,kind,host,status,is_owner,created_at,NULL "
            "FROM citizens ORDER BY status='retired', created_at").fetchall()
    if not rows:
        print("还没有任何公民。用 citizen-add 或让 AI 调 register 登记。")
        conn.close()
        return

    print(f"{'名字':<18}{'形态':<10}{'宿主':<10}{'状态':<10}{'记忆':>4}  创建时间")
    print("─" * 76)
    for cid, name, kind, host, status, is_owner, created, _archived in rows:
        n = conn.execute("SELECT COUNT(*) FROM code_index WHERE owner=?", (cid,)).fetchone()[0]
        mark = "★" if is_owner else " "
        print(f"{name:<18}{kind or '-':<10}{host or '-':<10}{status or '-':<10}{n:>4}  {created} {mark}")
    retired = sum(1 for r in rows if r[4] == "retired")
    print(f"\n共 {len(rows)} 个（活跃 {len(rows) - retired} / 退役 {retired}），★=系统主人")
    conn.close()


# ------------------------------------------------------ 工作区映射命令族（R1 根治）

def cmd_workspace(args):
    """`fish workspace set|list|remove|seed`。

    为什么必须有 CLI：workspace_map 是"项目归属"的唯一权威来源；没有一个可维护的
    入口，用户就只能手改 SQLite——那等于把这张表交给运气，下次升级照样能写错。

    动作分级（与全局纪律一致）：
      · set / remove 是**显式**数据变更（写 events 留痕，可追溯谁改了哪条映射）；
      · list / seed 只读：seed 打印建议但**绝不自动写入**——正文里提到的路径可能
        属于别人的项目，归属必须由人确认。
    """
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    try:
        if args.workspace_action == "set":
            _workspace_cmd_set(conn, args)
        elif args.workspace_action == "list":
            _workspace_cmd_list(conn)
        elif args.workspace_action == "remove":
            _workspace_cmd_remove(conn, args)
        elif args.workspace_action == "seed":
            _workspace_cmd_seed(conn, args)
        else:
            raise ValueError(f"未知的 workspace 子命令：{args.workspace_action}")
    finally:
        conn.close()


def _workspace_cmd_set(conn, args):
    try:
        row = set_workspace_mapping(conn, args.path, args.project, note=args.note)
    except ValueError as e:
        print(f"错误：{e}")
        return
    print(f"已{'新增' if row['created'] else '更新'}映射：{row['path_prefix']} → {row['project']}")
    if row["note"]:
        print(f"  备注：{row['note']}")
    # 只警告不阻断：映射可以先于项目登记存在（例如先把目录归属定下来，再建项目）。
    known = conn.execute("SELECT 1 FROM code_registry WHERE prefix=?",
                         (row["project"],)).fetchone()
    if not known:
        print(f"⚠ 项目前缀 {row['project']} 还没在 code_registry 登记"
              f"（`fish project-add {row['project']}`）——注入包按它取数会是 0 条")


def _workspace_cmd_list(conn):
    rows = list_workspace_mappings(conn)
    if not rows:
        print("还没有任何映射。用 `fish workspace set <路径> <项目>` 添加，"
              "或 `fish workspace seed` 看看现有记忆里提到哪些工作区。")
        return
    # 不用固定列宽：路径里有中文时，按字符数补空格在终端里必然对不齐（CJK 占两列）。
    # 一行一条 + 缩进的备注，比一张"看起来是表格但列是歪的"表更好读。
    for r in rows:
        note = f"（{r['note']}）" if r["note"] else ""
        print(f"{r['path_prefix']}  →  {r['project']}{note}")
    print(f"\n共 {len(rows)} 条映射（路径一律小写/正斜杠/无尾斜杠，匹配不区分大小写）")


def _workspace_cmd_remove(conn, args):
    try:
        removed = remove_workspace_mapping(conn, args.path)
    except ValueError as e:
        print(f"错误：{e}")
        return
    prefix = normalize_workspace_path(args.path)
    print(f"已删除映射：{prefix}" if removed
          else f"没有这条映射：{prefix}（幂等：什么都不做）")


def _workspace_cmd_seed(conn, args):
    res = workspace_seed_candidates(conn, Path(args.root))
    reasons = {
        "multi_project": "多个项目都提到它",
        "shared_parent": "是别的项目路径的父目录（整片归属会误伤）",
        "mapping_mismatch": "与现有映射的归属不一致",
    }

    def show(items):
        for it in items:
            print(f"  {it['path']}  →  {it['project']}（出现 {it['count']} 次）")
            print(f"      fish workspace set \"{it['path']}\" {it['project']}")

    print("工作区 → 项目候选（**只打印，不写入**；确认后用 `fish workspace set` 落库）")
    strong = [it for it in res["suggest"] if it["count"] >= 2]
    weak = [it for it in res["suggest"] if it["count"] < 2]
    if strong:
        print("\n建议映射（正文里出现 ≥2 次，可信度较高）：")
        show(strong)
    if weak:
        print("\n弱候选（只出现 1 次：可能只是在讲别人的目录，先确认再 set）：")
        show(weak)
    if res["conflicts"]:
        print("\n需要人工判断（不给建议）：")
        for it in res["conflicts"]:
            who = " / ".join(it["projects"]) if it.get("projects") else it["project"]
            existing = f"，现有映射属于 {it['existing']}" if it.get("existing") else ""
            why = reasons.get(it.get("reason"), "")
            print(f"  {it['path']}  ← {who}{existing}"
                  f"（出现 {it['count']} 次；{why}）")
    if res["mapped"]:
        print("\n已被现有映射覆盖（无需处理）：")
        for it in res["mapped"]:
            print(f"  {it['path']}  →  {it['project']}（与现有映射一致，出现 {it['count']} 次）")
    if not (res["suggest"] or res["conflicts"] or res["mapped"]):
        print("\n没有找到候选：现有记忆正文里没有出现（且本机真实存在的）绝对路径。"
              "可以直接用 `fish workspace set` 手工声明。")
        return
    print("\n提示：一个目录下放着多个项目（共用工作区）时**不要整片映射**——"
          "拿不准就先只映射最具体的那个项目目录。")


# ------------------------------------------------------ 身份层 P1-b：session 命令族
#
# 为什么是 CLI 而不是 MCP 工具（评审 R-2）：MCP 进程的 env 被宿主 scrub 掉，
# 它拿不到任何"机器事实"，所以它签不出 machine 级。签发权只属于能读到 env 的 CLI；
# MCP 侧将来只调 `validate_session_token` 做校验。判据 H 把这条冻结成断言。

def _print_session_start(res):
    """`session start` 的输出（**契约**：AI 要能从这几行抓到 token）。

    前五行严格按 18 §2 第 6 步的顺序。纪律：全流程只有这一处 print 明文，且只到
    stdout——不 print 到 stderr（stderr 经常被重定向进日志文件），不写任何文件。

    多项目（2026-10-24）：`stint:` 一行一个档期（复用/新开都列出来），`project:` 仍是
    单值口径——只开一个项目时就是那个项目（旧解析不变），多项目时给逗号串。
    """
    print(f"token: {res['token']}")
    print(f"citizen_id: {res['citizen_id']}")
    print(f"session_key: {res['session_key']}")
    print(f"evidence: {res['evidence']}")
    print(f"project: {', '.join(res.get('projects') or []) or '-'}")
    print(f"channel: {res['channel']}")
    print(f"host: {res['host']}")
    print(f"display_name: {res['display_name']}")
    print(f"account_id: {res['account_id'] or '-'}")
    print(f"issued_at: {res['issued_at']}")
    print(f"expires_at: {res['expires_at'] or '永久'}")
    print(f"citizen: {'新建' if res['created'] else '复用'}")
    # 起名引导（2026-11-22）：只在自动占位名时出现，文案与注入包共用一份。
    if fish_naming.is_auto_name(res["display_name"], host=res["host"],
                                session_key=res["session_key"]):
        print("")
        print(f"你还没有可读名（当前占位 `{res['display_name']}`）。")
        print(fish_naming.rename_guidance())
    # FB-08：同名在用公民 → 一行提示（不拦截、不合并，只让人看见）。
    # 位置在 `citizen:` 之后、"档期"之前：读者刚看完"建的还是复用的"，紧接着就是
    # "其实还有另一个同名的人"。
    for it in res.get("name_conflicts") or []:
        print(same_name_notice(it["citizen_id"], it["kind"]))
    if res["rotated"]:
        # 字段行保持"可直接解析"（只有数字），解释另起一行——输出是契约，别让 prose 混进字段。
        print(f"rotated: {res['rotated']}")
        print(f"（同会话的旧凭证已吊销 {res['rotated']} 条）")
    for st in res.get("stints") or []:
        print(f"stint: {st['action']} {st['project']}")
        print(f"（档期 id={st['stint_id']}，自 {st['from_ts']} 起）")
    if res.get("archived"):
        print("注意：该会话公民在归档表里——本轮只复用身份，搬行归 P1-d")
    if res.get("warning"):
        print(f"⚠ {res['warning']}")
    print("（token 明文只此一次；丢了就重跑 session start——公民不变，凭证换新）")


def _print_session_ensure(res):
    """`session ensure` 的输出（**契约**：hook 按 `key: value` 抓 token / 身份 / 项目）。

    与 `_print_session_start` 的差别只有一处：ensure **可能不带 token**（已有活凭证时
    绝不轮换，113 §5.4）。所以用 `token_source` 两行说清，而**不是**把 `None` 印成一个
    看起来像凭证的东西——那会让 hook 把 "None" 当 token 注入进会话。
    """
    if res.get("token"):
        print(f"token: {res['token']}")
    print(f"token_source: {res['token_source']}")
    print(f"citizen_id: {res['citizen_id']}")
    print(f"session_key: {res['session_key']}")
    print(f"evidence: {res['evidence']}")
    print(f"project: {', '.join(res.get('projects') or []) or '-'}")
    print(f"channel: {res['channel']}")
    print(f"host: {res['host']}")
    print(f"display_name: {res['display_name']}")
    print(f"account_id: {res['account_id'] or '-'}")
    print(f"identity: {res['identity_origin']}")
    if res.get("inherited_from"):
        print(f"inherited_from: {res['inherited_from']}")
    print(f"issued_at: {res['issued_at']}")
    print(f"expires_at: {res['expires_at'] or '永久'}")
    print(f"citizen: {'新建' if res['created'] else '复用'}")
    for it in res.get("name_conflicts") or []:
        print(same_name_notice(it["citizen_id"], it["kind"]))
    for st in res.get("stints") or []:
        print(f"stint: {st['action']} {st['project']}")
        print(f"（档期 id={st['stint_id']}，自 {st['from_ts']} 起）")
    if res["token_source"] == "existing":
        print("（本会话已有活凭证：ensure 幂等，未重签也未轮换——避免与其它进程互踢）")
    else:
        print("（token 明文只此一次；丢了就重跑 session start——公民不变，凭证换新）")


def _print_session_status(res):
    info, citizen, stints = res["token"], res["citizen"], res.get("stints") or []
    ident = res["identity"] or {}
    print("会话身份")
    if info:
        print(f"  session_key  {info['session_key']}")
        print(f"  evidence     {info['evidence']}")
        print(f"  channel      {info['channel']}")
        print("  来源         --token")
    else:
        print(f"  session_key  {ident.get('session_key') or '-'}")
        print(f"  evidence     {ident.get('evidence') or '-'}")
        print(f"  channel      {ident.get('channel') or '-'}")
        print(f"  来源         env（{ident.get('source') or '-'}）")
    if citizen:
        print(f"  citizen_id   {citizen['citizen_id']}")
        print(f"  display_name {citizen['display_name']}")
        print(f"  kind/role    {citizen['kind']}/{citizen['role'] or '-'}")
        print(f"  host         {citizen['host'] or '-'}")
        print(f"  account_id   {citizen['account_id'] or '-'}")
        print(f"  last_seen_at {citizen['last_seen_at'] or '-'}")
        # 2026-11-25（W4）：多一行「身份解析」——只看 evidence 分不出"自建/继承/临时"。
        origin = res.get("identity_origin") or (citizen or {}).get("identity_origin")
        if origin:
            source = f" ← {res.get('inherited_from') or citizen.get('inherited_from')}"
            print(f"  身份解析     {origin}{'' if origin == 'direct' else source}")
        if citizen.get("archived_at"):
            print(f"  归档时间     {citizen['archived_at']}（身份在冷表里）")
    else:
        print("  公民         未登记（先跑 `fish session start`）")
    # 一行一个档期，续行对齐到"项目档期："之后——多项目时一眼能看出开了几条。
    # 单档期也走这个格式：两套排版只会让"多了一个档期"这种关键变化不显眼。
    if stints:
        for i, st in enumerate(stints):
            head = "项目档期：" if i == 0 else " " * 10
            print(f"{head}{st['project']}（自 {st['from_ts']}）")
    else:
        print("项目档期：无")
    if info:
        print("凭证")
        # 只露 hash 前 12 位：hash 不是凭证，但它能让人对着 session_tokens 找到那一行。
        print(f"  token_hash   {info['token_hash'][:12]}…（sha256 前 12 位；明文不落库）")
        print(f"  issued_at    {info['issued_at']}")
        print(f"  expires_at   {info['expires_at'] or '永久'}")
        if info["revoked_at"]:
            print(f"  状态         已吊销（{info['revoked_at']}）")
        elif info["expired"]:
            print(f"  状态         已过期（{info['expires_at']}）")
        else:
            print("  状态         有效")
        if info.get("parent_token"):
            print(f"  父凭证       {info['parent_token'][:12]}…")
    if ident.get("warning"):
        print(f"⚠ {ident['warning']}")


def cmd_session_start(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    # 2026-10-24 加：`--cwd` 走 workspace_map 解析项目（R1 的落地点之一）。
    # 优先级：显式 --project > --cwd 解析结果 > 不开档期（宁可没有，不猜）。
    # 为什么不让 hook 自己先查再传：多一次进程启动、多一处可能忘传；
    # 放这里，任何调用方（hook / AI / 脚本）只要给 cwd 就能拿到正确项目。
    projects = getattr(args, "project", None)
    cwd = getattr(args, "cwd", None)
    resolved_from_cwd = None
    fell_back = None
    if not projects and cwd:
        try:
            resolved_from_cwd = resolve_project_by_cwd(conn, cwd)
        except Exception:
            resolved_from_cwd = None
        if resolved_from_cwd:
            projects = [resolved_from_cwd]
        else:
            # T1（2026-10-25）：cwd 没匹配到具体项目 → 落**兜底项目**，而不是"不开档期"。
            # 这是决策 12 的落地点（生活/工作/未分类是真实项目，不是引导层），
            # 也是 `project_stints` 恒 0 的修复：会话不再因为"认不出目录"就一条档期都不留。
            # 仍然"不猜"——兜底项目自身的语义就是"我不知道"，且 L1 会把它标注出来。
            fell_back = fallback_project(conn)
            if fell_back:
                projects = [fell_back]
    try:
        res = start_session(
            conn, projects=projects,
            account=getattr(args, "account", None), alias=getattr(args, "alias", None),
            citizen=getattr(args, "citizen", None),
            bind_token=getattr(args, "bind_token", None))
    except ValueError as e:
        conn.close()
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(2)
    # ⚠️ 这两行必须走 stderr：stdout 是 `key: value` 的**契约面**（hook 按冒号
    # 切分）。诊断句里带冒号，走 stdout 会污染出一个无意义的 key。
    if resolved_from_cwd:
        print(f"（项目由 --cwd 经 workspace_map 解析：{resolved_from_cwd}）",
              file=sys.stderr)
    elif fell_back:
        print(f"（--cwd 未匹配任何 workspace_map 条目，落到兜底项目「{fell_back}」；"
              "用 `fish workspace set` 声明后即可归到真项目）", file=sys.stderr)
    elif cwd and not getattr(args, "project", None):
        print(f"（--cwd 未匹配任何 workspace_map 条目，未开档期：{cwd}）",
              file=sys.stderr)

    # 2026-10-25 加（外部评估 `EVAL-261025-07`）：**归属证据必须与参数回显分开标注**。
    #
    # 旧输出只有一行 `project: anyi-studio`——那只是 `--project` 的**回显**，
    # 不是"归属已确定"的证据。实测时 AI 把这行当成了"已命中映射"，
    # 而它实际工作的三个目录在 workspace_map 里一条都没有（判据是空的，但看起来成功）。
    # 这正是 fish 自己总结过的「宣称不代表建成」的同款形态。
    if getattr(args, "project", None) and not resolved_from_cwd:
        print("（注意：上面的 project 是 --project 的**回显**，不是 workspace 映射命中的证据。"
              "写归属的权威源是 workspace_map）", file=sys.stderr)
    try:
        n_map = conn.execute("SELECT COUNT(*) FROM workspace_map").fetchone()[0]
        if n_map < 2:
            print(f"（workspace_map 只有 {n_map} 条映射——多目录工作时请先 "
                  f"`fish workspace add <路径> --project <前缀>`，否则写入归属没有依据）",
                  file=sys.stderr)
    except sqlite3.Error as exc:
        # 映射表还没建（极旧库）时不阻断签发——但要出声（静默清单 B 档）
        warn_ignored("start_session.workspace_map", exc)

    _print_session_start(res)
    # 2026-10-26（T03）：会话开场**自动签到**（用户拍板「自动行算登记」）。
    # 三条纪律：① stdout 一个字节都不许动（hook 按 `key: value` 解析）；
    # ② 失败静默（会话启动不能被看板拖垮）；③ 多个项目逐个签，最多 3 个；
    # 一个项目都没有时签根板（"我今天有活动"这件事本身有价值）。
    _auto_checkin_after_start(conn, res, getattr(args, "cwd", None))
    conn.close()


def cmd_session_ensure(args):
    """`session ensure`（W4.0 · 113 §5.2）：hook 的入口。幂等、不轮换。

    与 `cmd_session_start` 的两处差别**都是刻意的**：
      · 不开 `--project` 之外的档期逻辑照旧（`--cwd` → workspace_map → 兜底项目），
        因为"这条会话在哪个项目上"这件事 hook 照样要报给模型；
      · 不打印 "token 明文只此一次" 那句（ensure 可能复用已有凭证），改由
        `_print_session_ensure` 用 `token_source` 说清。
    """
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    projects = getattr(args, "project", None)
    cwd = getattr(args, "cwd", None)
    resolved_from_cwd = None
    fell_back = None
    if not projects and cwd:
        try:
            resolved_from_cwd = resolve_project_by_cwd(conn, cwd)
        except Exception:
            resolved_from_cwd = None
        if resolved_from_cwd:
            projects = [resolved_from_cwd]
        else:
            fell_back = fallback_project(conn)
            if fell_back:
                projects = [fell_back]
    ident = build_session_identity(alias=getattr(args, "alias", None))
    try:
        res = ensure_session(
            conn, session_key=ident["session_key"], host=ident["host"],
            evidence=ident["evidence"], channel=ident["channel"],
            display_name=ident["alias"] or None,
            account_id=resolve_account_id(account=getattr(args, "account", None),
                                          alias=ident["alias"]),
            projects=projects, citizen=getattr(args, "citizen", None),
            bind_token=getattr(args, "bind_token", None),
            # 113 §7.1（W4.6）：WB hook 把 payload 里的父证据直接交进来
            # （`transcript_path` 反解的父 + `agent_id`），不在这里猜。
            parent_session=getattr(args, "parent_session", None),
            agent_id=getattr(args, "agent_id", None))
    except ValueError as e:
        conn.close()
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(2)
    # 诊断句必须走 stderr：stdout 是 `key: value` 的契约面（hook 按冒号切分）。
    if resolved_from_cwd:
        print(f"（项目由 --cwd 经 workspace_map 解析：{resolved_from_cwd}）",
              file=sys.stderr)
    elif fell_back:
        print(f"（--cwd 未匹配任何 workspace_map 条目，落到兜底项目「{fell_back}」）",
              file=sys.stderr)
    elif cwd and not getattr(args, "project", None):
        print(f"（--cwd 未匹配任何 workspace_map 条目，未开档期：{cwd}）",
              file=sys.stderr)
    _print_session_ensure(res)
    _auto_checkin_after_start(conn, res, cwd)
    conn.close()


def _auto_checkin_after_start(conn, res, cwd=None, *, session_key=None):
    """`session start` 之后自动签到（失败静默，**不写 stdout**）。

    ## 2026-10-26（QA Round 1 · E2）：签到之后顺手重渲染看板**文件**

    签到只改 `presence` 表，而 `projects/<项目>/看板-*.md` 是**渲染产物**。不重渲染，
    文件就永远停在「在场（0 人）」，而 `board show` / DB 已经是 1 人——同一份事实
    两个读数，读文件的人（人、别的 AI、外部评审）会以为没人开工。QA 就是这么抓到的。

    为什么在这里做（而不是采集快路径）：会话开始一次，代价可接受；而
    `handle_post_tool_use` 是**每次工具调用**都跑，在那儿加文件写 + 根板聚合会明显加 IO。

    为什么不在 `fish_session_hook.py` 再起一个进程：那要多付一次解释器启动，
    而且拿不到"刚签到的是哪块板"；`board_render()` 是子板刷新的唯一出口，
    根板由它内部处理（`scope != 'root'` 时刷一次），**这里不再重复调根板**。
    """
    if not get_config(conn, "board.auto_presence"):
        return []
    root = conn_data_dir(conn)
    projects = [p for p in (res.get("projects") or []) if p][:3] or [None]
    # 2026-10-26（撞车自撞修复 T02）：`session_key` 参数**优先**于 `res` 里的会话键
    # ——`inject --auto-presence` 拿不到 `session start` 的返回值，只能从 CLI/env 传。
    sk = (session_key or res.get("session_key") or "")
    done = []
    for project in projects:
        try:
            out = board_auto_checkin(
                conn, res["citizen_id"], project=project,
                session_key=(sk or None), source="session_start")
            done.append((project, out.get("created"), out.get("skipped")))
            _render_board_quietly(conn, root, out)
        except (sqlite3.Error, ValueError, KeyError):
            continue
    # 顺手刷一次快照：让**声明面**在第一次写之前就进快照（否则第一小时的空窗期里
    # PreToolUse 只能看到空的自己）。失败静默。
    try:
        fish_activity.build_snapshot(
            conn, out_path=Path(root) / fish_activity.SNAPSHOT_RELPATH,
            window_min=int(get_config(conn, "board.overlap_window_min")))
    except (sqlite3.Error, ValueError, OSError) as exc:
        warn_ignored('fish.py:9250 _auto_checkin_after_start', exc)
    return done


def _render_board_quietly(conn, root, checkin) -> bool:
    """自动签到之后重渲染那块看板文件（**失败静默**），返回是否真的渲染了。

    两条边界：
      · 只在这回**真动了行**（`created` / `refreshed`）时渲染——没动就没变化，
        多写一遍文件纯属浪费；
      · 根板不在这里调：`board_render()` 末尾已经会刷一次根板（`scope != 'root'`），
        再加一次就是"二次渲染"。
    """
    result = checkin if isinstance(checkin, dict) else {}
    board = result.get("board")
    if not isinstance(board, dict) or not board.get("path"):
        return False
    if not (result.get("created") or result.get("refreshed")):
        return False
    try:
        board_render(conn, Path(root), board)
        return True
    except Exception:      # noqa: BLE001 —— 看板渲染是展示层，失败绝不能拖垮会话启动
        return False


def conn_data_dir(conn) -> str:
    """连接指向的 data 目录（`fish_activity` 的审计/快照都挂在它下面）。"""
    try:
        for row in conn.execute("PRAGMA database_list").fetchall():
            if row[1] == "main" and row[2]:
                return str(Path(row[2]).parent)
    except sqlite3.Error as exc:
        warn_ignored('fish.py:9283 conn_data_dir', exc)
    return str(DEFAULT_ROOT)


def cmd_session_status(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    try:
        res = session_status(conn, token=getattr(args, "token", None))
    except ValueError as e:
        conn.close()
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(2)
    _print_session_status(res)
    conn.close()


def cmd_session_tree(args):
    """`session tree <session_id> [--depth 1]`（113 §9.3）：读投影，缺行时现读宿主头。"""
    import fish_workbench_api      # 局部 import（同 fish_purge）：HTTP 面不拖进每条命令
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    try:
        tree = fish_workbench_api.session_tree(conn, args.session_id, depth=args.depth)
    finally:
        conn.close()
    print(fish_workbench_api.session_tree_text(tree), end="")
    sys.exit(0 if tree.get("rows") else 2)


def cmd_lineage(args):
    """`lineage scan|report`（113 §10 · W6）：回填缺省 dry-run，report 只读。"""
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    try:
        print(fish_lineage_backfill.run_cli(
            conn, getattr(args, "lineage_action", "report"),
            host=getattr(args, "host", None), apply_scan=getattr(args, "apply", False)), end="")
    except (KeyError, OSError, sqlite3.Error) as exc:   # 未知宿主 / IO / SQL
        print(f"错误：{exc}", file=sys.stderr)
        sys.exit(2)
    finally:
        conn.close()
    sys.exit(0)


def cmd_session_end(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    try:
        res = end_session(conn, token=getattr(args, "token", None))
    except ValueError as e:
        conn.close()
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(2)
    if res["already"]:
        print(f"凭证本来已是吊销态：{res['session_key']}（公民 {res['citizen_id']}）"
              "——只补齐档期与活跃时间")
        if res["children_revoked"]:
            print(f"级联吊销后代凭证（含孙级）：{res['children_revoked']} 条")
    else:
        print(f"已结束会话：{res['session_key']}（公民 {res['citizen_id']}）")
        print(f"吊销凭证：{res['revoked']} 条；级联吊销后代凭证（含孙级）："
              f"{res['children_revoked']} 条")
    print(f"关闭档期：{res['stints_closed']} 个")
    conn.close()


def cmd_session_agent(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    try:
        res = issue_agent_token(conn, parent_token=getattr(args, "parent", None),
                                role=getattr(args, "role", None))
    except ValueError as e:
        conn.close()
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(2)
    # 输出字段与 start 对齐（前 4 行同构），AI 不需要两套解析逻辑。
    print(f"token: {res['token']}")
    print(f"citizen_id: {res['citizen_id']}")
    print(f"session_key: {res['session_key']}")
    print(f"evidence: {res['evidence']}")
    print(f"channel: {res['channel']}")
    print(f"role: {res['role']}")
    print(f"parent_token_hash: {res['parent_token_hash'][:12]}…")
    print(f"issued_at: {res['issued_at']}")
    print(f"expires_at: {res['expires_at'] or '永久'}")
    print("（W4 起子 agent 凭证可继续派生：根必须是 machine 级；`#role` 后缀区分角色）")
    conn.close()


def cmd_report(args):
    """使用日报/周报。设计原则：一周后要回答的问题，现在就要有数据。"""
    root = Path(args.root)
    conn = connect(root)
    conn.executescript(SCHEMA)
    migrate(conn)

    days = args.days
    now = datetime.datetime.now(datetime.timezone.utc)
    since = (now - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    label = f"近 {days} 天" if days > 1 else "今日"

    # ---------- 读审计 ----------
    def read_jsonl(name):
        p = root / "_audit" / name
        out = []
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    d = json.loads(line)
                    if (d.get("ts_utc") or d.get("ts") or "") >= since:
                        out.append(d)
                except Exception as exc:
                    warn_ignored('fish.py:9371 cmd_report', exc)
        return out

    calls = read_jsonl("tool-calls.jsonl")
    hooks = read_jsonl("hook-calls.jsonl")
    detours = read_jsonl("detours.jsonl")

    from collections import Counter
    by_tool = Counter(c.get("name") for c in calls if c.get("kind") == "tool")
    by_session = Counter(c.get("session_actor") or "(未标注)" for c in calls)
    fails = [c for c in calls if c.get("ok") is False]
    durs = [c["duration_ms"] for c in calls if isinstance(c.get("duration_ms"), int)]
    unreg = [c for c in calls if c.get("actor_registered") is False]

    # ---------- 读数据库 ----------
    new_mem = conn.execute("SELECT COUNT(*) FROM code_index WHERE recorded_at >= ?",
                           (since,)).fetchone()[0]
    total_mem = conn.execute("SELECT COUNT(*) FROM code_index").fetchone()[0]
    never = conn.execute("SELECT COUNT(*) FROM code_index WHERE access_count=0").fetchone()[0]
    by_type = conn.execute("SELECT mem_type, COUNT(*) FROM code_index WHERE recorded_at >= ? "
                           "GROUP BY mem_type ORDER BY 2 DESC", (since,)).fetchall()
    by_owner = conn.execute("SELECT owner, COUNT(*) FROM code_index WHERE recorded_at >= ? "
                            "GROUP BY owner ORDER BY 2 DESC", (since,)).fetchall()
    by_session_mem = conn.execute(
        "SELECT COALESCE(session_actor,'(未标注)'), COUNT(*) FROM code_index "
        "WHERE recorded_at >= ? GROUP BY 1 ORDER BY 2 DESC", (since,)).fetchall()
    _rpv, _rpp = _visibility_sql(conn, token_actor_citizen_id(conn))  # 第五轮评审 🔴
    hot = conn.execute("SELECT code,title,access_count FROM code_index "
                       "WHERE access_count>0" + _rpv
                       + " ORDER BY access_count DESC LIMIT 5", _rpp).fetchall()
    feedback = conn.execute("SELECT code,title FROM code_index "
                            "WHERE tags LIKE '%fish-feedback%'" + _rpv
                            + " ORDER BY recorded_at DESC", _rpp).fetchall()
    citizens = conn.execute("SELECT display_name,kind,status FROM citizens "
                            "WHERE status='active' ORDER BY created_at").fetchall()

    # ---------- 组装 ----------
    L = [f"# fish 使用报告 · {label}",
         f"> 生成于 {datetime.datetime.now().astimezone().isoformat(timespec='seconds')}",
         f"> 统计起点 {since}（UTC）", ""]

    L += ["## 一、行为量", "",
          f"- 工具调用：**{len([c for c in calls if c.get('kind')=='tool'])}** 次"
          f"（失败 {len(fails)}）",
          f"- 资源读取：{len([c for c in calls if c.get('kind')=='resource'])} 次",
          f"- hook 执行：{len(hooks)} 次",
          f"- **绕路（直接读文件/查库）：{len(detours)} 次**"]
    total_ops = len(calls) + len(detours)
    if total_ops:
        L.append(f"- 绕路率：**{len(detours)/total_ops*100:.1f}%**"
                 f"（越低越好，说明 AI 走正门）")
    if durs:
        L.append(f"- 工具耗时：中位 {sorted(durs)[len(durs)//2]} ms，"
                 f"最慢 {max(durs)} ms")
    if unreg:
        L.append(f"- ⚠ 未登记就调用：{len(unreg)} 次（说明有会话没走 register）")
    L.append("")

    L += ["## 二、工具使用分布", ""]
    if by_tool:
        L += ["| 工具 | 次数 |", "|---|---|"]
        L += [f"| {k} | {v} |" for k, v in by_tool.most_common()]
        dead = {"remember", "recall", "neighbors", "resolve_code", "whoami",
                "register", "inject", "healthcheck"} - set(by_tool)
        if dead:
            L += ["", f"⚠ **零调用工具**：{', '.join(sorted(dead))}（可能是死代码或太难被发现）"]
    else:
        L.append("（无调用）")
    L.append("")

    L += ["## 三、会话分布", ""]
    if by_session:
        L += ["| 会话 | 调用次数 |", "|---|---|"]
        L += [f"| {k} | {v} |" for k, v in by_session.most_common()]
    else:
        L.append("（无）")
    L.append("")

    L += ["## 四、记忆", "",
          f"- 新增：**{new_mem}** 条（全库 {total_mem} 条）",
          f"- **从未被召回：{never} 条**（占 {never/total_mem*100:.0f}%；高的离谱说明只写不读）"
          if total_mem else "- （空库）"]
    if by_type:
        L.append("- 类型分布：" + " / ".join(f"{t}={n}" for t, n in by_type))
    if by_owner:
        L.append("- 归属分布：" + " / ".join(f"{o or '空'}={n}" for o, n in by_owner))
    if by_session_mem:
        L.append("- 会话分布：" + " / ".join(f"{s}={n}" for s, n in by_session_mem))
    if hot:
        L += ["", "**最常被召回**："]
        L += [f"- {c} {t}（{n} 次）" for c, t, n in hot]
    L.append("")

    L += ["## 五、AI 反馈", ""]
    if feedback:
        L += [f"- {c} {t}" for c, t in feedback]
    else:
        L.append("（这一周还没有 AI 提反馈——如果没有，说明提示词里的反馈机制没生效）")
    L.append("")

    L += ["## 六、公民", ""]
    L += [f"- {n}（{k}）" for n, k, _ in citizens]

    text = "\n".join(L)
    print(text)

    # 落盘
    if args.save:
        rd = root / "_reports"
        rd.mkdir(exist_ok=True)
        fn = rd / f"report-{datetime.date.today().isoformat()}.md"
        fn.write_text(text, encoding="utf-8")
        print(f"\n[已保存] {fn}")
    conn.close()


def cmd_config(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    if args.action == "list":
        for k in sorted(CONFIG_DEFAULTS):
            print(f"{k}\t=\t{get_config(conn, k)}")
    elif args.action == "get":
        print(f"{args.key}\t=\t{get_config(conn, args.key)}")
    else:
        try:
            v = set_config(conn, args.key, args.value)
            print(f"{args.key} = {v}")
        except (KeyError, ValueError) as e:
            print(f"错误：{e}")
    conn.close()


# ---------------------------------------------------------------- 推送度量（P1-a · E 批）
# `16` §5.5 的硬要求：**四个数字永不合并**。论文原话是 "evaluation is the gating layer"——
# 干预质量不可测之前，主动性的奖励函数无从定义。而"不能只报一个综合分"的理由更直接：
# **只报综合分，"不说话"看起来最安全**（沉默永远没有误报）。
#
# 所以四个数字各自独立，任何"加权总分"都是对这条纪律的违反：
#   ① 采纳率 acceptance —— 收益侧：推的东西有没有被用
#   ② 盲区消除率 gap_cleared —— 收益侧：推完是否真的去读了
#   ③ 违规率 violation —— **成本侧，必须单独报**：推了但打扰了工作
#   ④ 每会话方差 session_stdev —— 聚合值必须和方差一起看
#      （强模型可能靠"放弃一部分会话"刷高精度，只看均值看不出来）

GAP_CLEAR_WINDOW_DAYS = 7      # 被推后多少天内读了正文，算"盲区消除了"
VIOLATION_WINDOW_MINUTES = 5   # 工具后推送后多久内毫无动静，算"白打扰"


def push_stats(conn, days=30, now=None):
    """推送效果度量（只读）。返回四个独立指标 + 分会话明细。

    **不返回"总分"**：那会把"多推"和"推得准"混成一个数，而它们方向相反。
    空数据时各率为 `None`（不是 0）——0 表示"推了但没人理"，None 表示"没推过"，
    这两件事完全不同；混用会让"还没开始推"看起来像"推了没用"。
    """
    now_dt = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = (now_dt - datetime.timedelta(days=int(days))).strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = conn.execute(
        "SELECT session_key, channel, code, pushed_at, reaction, latency_sec "
        "FROM push_log WHERE pushed_at >= ?", (cutoff,)).fetchall()

    total = len(rows)
    accepted = sum(1 for r in rows if r[4] == "accepted")
    ignored = sum(1 for r in rows if r[4] == "ignored")
    pending = sum(1 for r in rows if r[4] is None)
    decided = accepted + ignored                    # 只拿"已判"做分母

    lat = sorted(r[5] for r in rows if r[4] == "accepted" and isinstance(r[5], int))
    median_lat = lat[len(lat) // 2] if lat else None

    # ① 采纳率（唯一实现：`fish_push_vocab.accept_rate`；分母 = accepted + ignored）
    accept_rate = fish_push_vocab.accept_rate(accepted, ignored)

    # ② 盲区消除率：推过之后真的读了正文
    cleared = 0
    for _sess, _ch, code, pushed_at, _re, _la in rows:
        p_dt = parse_utc(pushed_at)
        if p_dt is None or not code:
            continue
        end = (p_dt + datetime.timedelta(days=GAP_CLEAR_WINDOW_DAYS)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        hit = conn.execute(
            "SELECT 1 FROM memory_access_log WHERE code=? AND via='read_body' "
            "AND accessed_at >= ? AND accessed_at <= ? LIMIT 1",
            (code, pushed_at, end)).fetchone()
        if hit:
            cleared += 1
    gap_rate = (cleared / total) if total else None

    # ③ 违规率（成本侧）：工具后推送出去，5 分钟内该会话一次访问都没有 = 白打扰
    ta = [r for r in rows if r[1] == "tool_after"]
    violated = 0
    for sess, _ch, _code, pushed_at, _re, _la in ta:
        p_dt = parse_utc(pushed_at)
        if p_dt is None:
            continue
        end = (p_dt + datetime.timedelta(minutes=VIOLATION_WINDOW_MINUTES)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        hit = conn.execute(
            "SELECT 1 FROM memory_access_log WHERE session_key IS ? "
            "AND accessed_at >= ? AND accessed_at <= ? LIMIT 1",
            (sess, pushed_at, end)).fetchone()
        if not hit:
            violated += 1
    violation_rate = (violated / len(ta)) if ta else None

    # ④ 分会话明细 + 方差
    by_session = {}
    for sess, _ch, _code, _pushed, reaction, _la in rows:
        key = sess or "(未归属)"
        s = by_session.setdefault(key, {"total": 0, "accepted": 0, "ignored": 0,
                                        "pending": 0, "accept_rate": None})
        s["total"] += 1
        if reaction == "accepted":
            s["accepted"] += 1
        elif reaction == "ignored":
            s["ignored"] += 1
        elif reaction is None:
            s["pending"] += 1
    rates = []
    for s in by_session.values():
        s["accept_rate"] = fish_push_vocab.accept_rate(s["accepted"], s["ignored"])
        if s["accept_rate"] is not None:
            rates.append(s["accept_rate"])
    if len(rates) >= 2:
        mean = sum(rates) / len(rates)
        session_stdev = (sum((r - mean) ** 2 for r in rates) / len(rates)) ** 0.5
    else:
        session_stdev = None      # 样本不足 2 个会话：算不出（不是 0）

    return {
        "window_days": int(days),
        "total": total,
        "accepted": accepted,
        "ignored": ignored,
        "pending": pending,
        "decided": decided,
        "accept_rate": accept_rate,              # ① 收益侧
        "gap_cleared": cleared,
        "gap_clear_rate": gap_rate,              # ② 收益侧
        "violation": violated,
        "violation_rate": violation_rate,        # ③ 成本侧（单独一栏）
        "tool_after_total": len(ta),
        "median_latency_sec": median_lat,
        "by_session": by_session,
        "session_stdev": session_stdev,          # ④ 聚合的方差
    }


def format_push_stats(st) -> str:
    """把度量渲染成人读文本。四栏分开，**不给总分**。"""
    def pct(v):
        return "—" if v is None else f"{v * 100:.1f}%"

    sdev = "—" if st["session_stdev"] is None else f"{st['session_stdev']:.3f}"
    lat = st["median_latency_sec"]
    lines = [f"推送效果（近 {st['window_days']} 天）",
             f"  推送 {st['total']} 次：accepted {st['accepted']} / "
             f"ignored {st['ignored']} / 待判 {st['pending']}",
             "",
             "【收益侧】",
             f"  ① 采纳率      {pct(st['accept_rate'])}"
             f"（分母=已判 {st['decided']}；待判不算，避免窗口未到就拉低）",
             f"  ② 盲区消除率  {pct(st['gap_clear_rate'])}"
             f"（{st['gap_cleared']}/{st['total']} 在推后 {GAP_CLEAR_WINDOW_DAYS} 天内读了正文）",
             f"     读后延迟中位数 {'—' if lat is None else str(lat) + ' 秒'}",
             "",
             "【成本侧】（单独一栏，不与收益合并）",
             f"  ③ 违规率      {pct(st['violation_rate'])}"
             f"（工具后推送 {st['tool_after_total']} 次里，{st['violation']} 次"
             f" {VIOLATION_WINDOW_MINUTES} 分钟内毫无动静）",
             "",
             "【分布】",
             f"  ④ 每会话方差  {sdev}",
             ]
    for k, v in sorted(st["by_session"].items(), key=lambda kv: -kv[1]["total"]):
        lines.append(f"    {k}: 推 {v['total']} / 采纳 {pct(v['accept_rate'])}")
    lines.append("")
    lines.append("  ⚠ 不报总分：只报综合分会让「不说话」看起来最安全（沉默永远没有误报）。")
    return "\n".join(lines)


def cmd_push(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    try:
        if args.action == "stats":
            print(format_push_stats(push_stats(conn, days=args.days)))
        elif args.action == "sweep":
            n = close_stale_pushes(conn)
            print(f"推送结果收口：{n} 条判为 ignored（窗口 "
                  f"{push_outcome_window_hours(conn)} 小时；判定只写 reaction，不写 reacted_at）")
        else:                                   # status
            print("push.enabled = %s" % bool(get_config(conn, "push.enabled")))
            for k in ("push.serendipity_ratio", "push.open_budget_tokens",
                      "push.after_tool_budget_tokens", "push.situational_budget_tokens",
                      "push.after_tool_every_n_calls", "push.gap_window_days"):
                print("  %-34s %s" % (k, get_config(conn, k)))
    finally:
        conn.close()


def cmd_token(args):
    """会话 token 的清点与清理（F-P2-6）。实现在 `fish_token`，这里只是薄壳。"""
    conn = connect(Path(args.root))
    try:
        act = getattr(args, "token_action", "list")
        if act == "list":
            st = fish_token.token_stats(conn)
            print(f"token 总数 {st['total']}；未撤销 {st['active']}；"
                  f"永不过期 {st['never_expires']}；已过期 {st['expired']}；"
                  f"陈旧(>{fish_token.STALE_DAYS_DEFAULT}天) {st['stale']}")
            return 0
        res = fish_token.token_prune(
            conn, expired=True, stale=bool(getattr(args, "stale", False)),
            dry_run=not bool(getattr(args, "apply", False)))
        if res["dry_run"]:
            print(f"**dry-run**：会删过期 {res.get('would_delete', 0)} 条；"
                  f"会把陈旧 {res.get('would_revoke', 0)} 条标记撤销。"
                  "真做加 `--apply`。")
        else:
            print(f"已删过期 {res['deleted']} 条；已撤销陈旧 {res['revoked']} 条。")
        return 0
    finally:
        conn.close()


def cmd_corpus(args):
    """语料层的 CLI 薄壳（M8）：动作/渲染/报错全在 `fish_corpus.run_command`。

    留在这里的只有"开主库连接 + 注入解析器"这件事：`resolve_project_by_cwd` /
    `list_workspace_candidates` / `fish_activity.resolve_citizen` / `mark_push_accepted`
    都要读**主库**，而语料模块按判据 J2 不许 import fish——注入之后，语料层只认
    "回调给什么"，不认识主库（副作用是它也天然可测：测试给假解析器）。

    ⚠️ 两个连接，别混：主库连接**只**用来喂解析器（projects/公民/候选/推送回填），
    语料库连接由门面自己开（主库上没有 corpus_* 表，D01）。
    """
    root = Path(args.root)
    conn = connect(root)
    conn.executescript(SCHEMA)
    migrate(conn)
    try:
        return fish_corpus.run_command(args, resolvers={
            "project_of": lambda cwd: resolve_project_by_cwd(conn, cwd),
            "candidates_of": lambda cwd: list_workspace_candidates(conn, cwd),
            "citizen_of": lambda key: fish_activity.resolve_citizen(conn, key),
            "mark_accepted": lambda codes, key: mark_push_accepted(
                conn, codes, session_key=key),
            # A①：帧上限的配置键读侧。语料层不许 import 主库（J2），只能注入。
            "config_of": lambda key: get_config(conn, key),
            # F-P0-4 第六轮 🔴-1：执行层需要知道自己是谁，否则 CLI 没有锁。
            # 无凭证时返回 ""（= 无身份，按最严处理：什么都读不到）。
            "actor_citizen": lambda: token_actor_citizen_id(conn) or "",
        })
    finally:
        conn.close()


def cmd_activity(args):
    """动作流的 CLI（`stats` 读数 / `prune` 清理）。**不占 MCP 工具位**（设计 §10.4）。

    为什么给 CLI 而不是 MCP 工具：动作流是**审计数据**，看它的是人（跑日报、查误报），
    不是模型每次对话都要调的东西；而 MCP 工具数已到用户设的上限。
    """
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    try:
        if args.action == "attribute":
            _m, _c = _attr_session(conn, args.session, args.citizen, apply=bool(args.apply))
            print(f"归属补账：匹配 {_m} 条，已改 {_c} 条" + ("" if args.apply else "（干跑；加 --apply 才真写）"))
            return
        if args.action == "prune":
            days = args.days if args.days is not None else int(
                get_config(conn, "activity.retention_days"))
            removed = fish_activity.prune(conn, days=days)
            print(f"动作流清理：删除 {removed} 行（保留 {days} 天）"
                  + ("（0 = 不清理）" if int(days) <= 0 else ""))
            return
        days = args.days if args.days is not None else 1
        data_dir = Path(args.root)
        st = fish_activity.stats(conn, days=days,
                                 asks_path=data_dir / fish_activity.ASKS_RELPATH)
        print(f"动作流（近 {st['days']} 天）")
        print(f"  总行数   {st['rows']}（写 {st['write_rows']} / 执行 {st['exec_rows']}）")
        print(f"  会话数   {st['sessions']}    宿主：{', '.join(st['hosts']) or '—'}")
        print(f"  撞车询问 {st['asked']} 条（误报审计看 {fish_activity.ASKS_RELPATH}）")
        if st["top_dirs"]:
            print("  高频目录（写）：")
            for name, count in st["top_dirs"][:8]:
                print(f"    {count:>4}  {name}")
        if st["by_hour"]:
            busiest = sorted(st["by_hour"], key=lambda kv: -kv[1])[:3]
            print("  写动作最多的小时（本地）："
                  + "、".join(f"{hour:02d}点 {count}" for hour, count in busiest))
    finally:
        conn.close()


def cmd_prompt_push(args):
    """**提示词时机**的递送（UserPromptSubmit hook 的子进程入口）。

    输入：stdin 上的 hook payload（`{"session_id","cwd","prompt",...}`）。
    输出：**要递给模型的纯文本**（可以是空串——那就什么都不输出）。

    六条否定条件（任一即不推，设计 §6.4）：
      `push.enabled`/`push.prompt_enabled` 关；**无身份且 `push.no_identity=skip`**
      （`allow` 档照旧推——见 `push_gate_no_identity`，2026-11-25 · W4.5 起与实现同源，
      在这之前这句话是谎的：实现从不 return）；prompt 洗成空；
      同一 prompt（hook 侧已挡，这里再算一次作为双保险）；
      候选为空；候选全是 24h 内推过的。

    只递**标题 + 编码 + 一句理由**，不递正文：模型要正文自己 `read_body`
    （那一步同时回填 accepted，采纳率才算得出来）。
    """
    host = getattr(args, "host", "dsh") or "dsh"
    raw = ""
    try:
        raw = sys.stdin.read()
    except Exception:      # noqa: BLE001 —— 读不到 stdin 就当没发生
        return
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    try:
        if not get_config(conn, "push.enabled") or not get_config(conn, "push.prompt_enabled"):
            return
        session_key = fish_activity.make_session_key(host, payload.get("session_id"))
        if not session_key:
            return
        citizen_id = fish_activity.resolve_citizen(conn, session_key)
        # W4.5（113 §6.2）：无身份是否推由开关决定，**默认 allow = 今天的实现行为**。
        if push_gate_no_identity(conn, citizen_id):
            return
        project = None
        cwd = payload.get("cwd") or ""
        if cwd:
            try:
                project = resolve_project_by_cwd(conn, cwd)
            except sqlite3.Error:
                project = None
        prompt = clean_prompt(payload.get("prompt") or "")
        if len(prompt) < 4:
            return
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        cands = build_prompt_candidates(
            conn, Path(args.root), prompt=prompt, citizen_id=citizen_id,
            session_key=session_key, project=project, now=now_dt,
            limit=int(get_config(conn, "push.prompt_max_items")) or 2)
        picked = select_push_candidates(
            conn, cands, limit=int(get_config(conn, "push.prompt_max_items")) or 2,
            prefer_kind="prompt_related")
        if not picked:
            return
        budget = int(get_config(conn, "push.prompt_budget_tokens")) or 120
        lines = []
        for c in picked:
            line = (f"- {c['code']} {_clip_chars(c['title'], ATTENTION_TITLE_MAX_CHARS)}"
                    f"（{c['reason']}）")
            if count_tokens("\n".join(lines + [line])) > budget:
                break
            lines.append(line)
        if not lines:
            return
        text = ("（fish 记忆：你这句话问到的东西已存过，直接看不要重新问）\n"
                + "\n".join(lines) + "\n")
        sys.stdout.write(text)
        sys.stdout.flush()
        codes = [ln[2:].split(" ", 1)[0] for ln in lines]
        # W4.5（113 §6.3）：解析父链**一次**（判不到就是 None，不兜 main）。
        # 不在这里做 WorkBuddy 校正：那是 W4.6，本波只把路由两列写对。
        agent_id, parent_session = push_target_of(conn, host=host, session_key=session_key)
        try:
            append_push_log(conn, codes, channel="prompt", citizen_id=citizen_id,
                            session_key=session_key, project=project,
                            candidate_id=codes[0] if codes else None,
                            budget_tokens=budget,
                            agent_id=agent_id, parent_session=parent_session,
                            hash_by_code={c["code"]: c.get("content_hash")
                                          for c in picked})
        except (sqlite3.Error, ValueError) as exc:
            # 记账失败不影响这次递送——但**必须出声**（采纳率缺数会让人得出错误结论）
            warn_derived(conn, "append_push_log", exc)
    except (sqlite3.Error, ValueError):
        return
    finally:
        conn.close()


def scan_orphan_bodies(conn, root) -> list:
    """P2-6：扫 `projects/**/*.md` 里**没有 code_index.file_path 指向**的孤儿正文。

    为什么会有：正文先原子写、再写库；异常路径会删正文，但 SIGKILL/断电不会——
    于是留下一个"文件在、索引无"的孤儿。它不会让任何查询出错，只会悄悄占盘、
    并让"记忆总数"与磁盘对不上。**列出来**，不自动删（可能是人工放进来的资料）。
    """
    root = Path(root)
    known = {str(r[0]).replace("\\", "/") for r in
             conn.execute("SELECT file_path FROM code_index WHERE file_path IS NOT NULL")}
    orphans = []
    proj = root / "projects"
    if proj.exists():
        for p in proj.rglob("*.md"):
            rel = p.relative_to(root).as_posix()
            if rel not in known:
                orphans.append(rel)
    return sorted(orphans)


def cmd_healthcheck(args):
    # F-P1-6（2026-11-18 外部审查）：体检必须是**只读**检查。
    #
    # 旧实现先 executescript(SCHEMA) + migrate(conn)——一个「检查」动作会写 schema：
    # 在只读介质、权限受限、生产排障时都可能改变现场。更糟的是库不存在时它会
    # **建一个空库**然后报告「一切正常」——把「没有」说成「没问题」，是体检最坏的形态。
    _root = Path(args.root)
    _db = _root / "index.sqlite"
    if not _db.exists():
        print(f"错误：库不存在（{_db}）——体检不会创建它", file=sys.stderr)
        raise SystemExit(2)
    # P2-5（2026-11-25）：只读连接复用 `fish_ui.open_readonly`（mode=ro + query_only 唯一实现）。
    import fish_ui as _ui          # 本地 import：只有体检需要，别让每次 CLI 启动都付
    conn = _ui.open_readonly(str(_db))
    total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    memories = conn.execute("SELECT COUNT(*) FROM code_index WHERE status='active'").fetchone()[0]
    dormant = conn.execute("SELECT COUNT(*) FROM code_index WHERE dormant=1").fetchone()[0]
    withdrawn = conn.execute(
        "SELECT COUNT(*) FROM code_index WHERE status='withdrawn'").fetchone()[0]
    broken = None               # P1-9：None = **判不到**（不是"0 条断链"），见 exit 2
    root = Path(args.root)
    # 2026-11-21（A-3）：断链读数交给 `fish_purge.scan_broken`——它**只扫 active**，
    # 且给出日期结构。原来的内联循环扫所有行（含已作废），于是"清理完还报 318 条"，
    # 那个读数会让"清理干净了吗"永远为假。日期结构则是定位根因的关键：
    # 85 号调查里正是按日期一分，才看出「10-28~11-18 连续 22 天 100% 断链 = 迁移事故」。
    try:
        _bi = fish_purge.scan_broken(conn, root)
        broken = _bi["codes"]
    except Exception as exc:      # noqa: BLE001 —— 体检不能因为一项检查挂掉整体
        warn_ignored("fish.py healthcheck scan_broken", exc)
        _bi = {"by_date": {}, "by_project": {}}
        # P1-9（2026-11-25）：原先只吞异常 → 打印"断链 0 条"→ exit 0（把判不到说成干净）。
        print(f"  断链：判不到（{type(exc).__name__}: {exc}）")
    print(f"事件 {total} 条 / 活跃记忆 {memories} 条 / 休眠 {dormant} 条 / 作废 {withdrawn} 条"
          f" / 断链 {'判不到' if broken is None else str(len(broken)) + ' 条'}")
    if _bi.get("by_date"):
        print("  断链按日期（连续多日 100% = 迁移/批量事故；零星 = 偶发删改）：")
        for _day, _n in _bi["by_date"].items():
            print(f"    {_day}  {_n}")
        print("  查详情/修复：fish purge-broken（先看 dry-run，确认后加 --apply）")
    # P2-6（2026-11-26）：孤儿正文——硬杀/断电留下"文件在、索引无"的 .md。
    try:
        _orphans = scan_orphan_bodies(conn, root)
        print(f"  孤儿正文 {len(_orphans)} 条"
              + (f"（前 5：{', '.join(_orphans[:5])}；确认后可人工清理）" if _orphans else ""))
    except Exception as exc:      # noqa: BLE001 —— 附属检查，判不到要出声
        print(f"  孤儿正文：判不到（{type(exc).__name__}: {exc}）")
    # git 关联度（2026-11-22 · F1/F8）：**让"关联不上"这件事可见**。
    #
    # 为什么必须有这一段：fish 建了完整的 git 观测体系（仓库/工作副本/观测/提交/关联
    # 五张表 + 22 条判据），但生产实测 `git_link` 是 **0 行**——自动关联的前提
    # （提交正文提到记忆编码）在真实提交里一次都没成立。**最坏的状态不是坏，是
    # "有实现、有判据、有数据，但产出为零，而且没有任何地方报告这个零"。**
    # 不报告它，下一个人（包括我自己）会以为关联能力已经可用。
    try:
        _nr = conn.execute("SELECT COUNT(*) FROM git_repo").fetchone()[0]
        _nc = conn.execute("SELECT COUNT(*) FROM git_commit").fetchone()[0]
        _nl = conn.execute(
            "SELECT COUNT(*) FROM git_link WHERE removed_at IS NULL").fetchone()[0]
        if _nc:
            print(f"  git 采集：{_nr} 仓库 / {_nc} 提交 / {_nl} 条关联")
            if _nl == 0:
                print("  ⚠ 提交全部未关联到记忆 —— 不是坏了，是关联的前提没成立：")
                print("     自动推断只认「提交正文提到记忆编码」，而真实提交很少这么写；")
                print("     要建立关联：用 `git_link` 工具显式断言"
                      "（implemented_by / produced / revised_in）")
    except sqlite3.Error as exc:      # 老库没有 git 表
        warn_ignored("fish.py healthcheck git_link", exc)
    # 全文索引哨兵（2026-10-24 实测教训：索引落后 1 条时，只有人工比对计数才发现——
    # 那种"存进去但搜不到"是最难发现的失真，必须在自检层报出来）
    try:
        fts = conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]
    except sqlite3.Error:
        fts = -1
    if fts < 0:
        print("  ⚠ 全文索引不可用（memory_fts 不存在）——跑 fish reindex 重建")
    else:
        gap = conn.execute("SELECT COUNT(*) FROM code_index").fetchone()[0] - fts
        print(f"  全文索引 {fts} 条" + (f"  ⚠ 落后 {gap} 条（跑 fish reindex 补齐）"
                                      if gap else "（与记忆同步）"))
    # owner 必须是有效公民（07 D-13）。库里有公民知识时才检查，空库不误报。
    #
    # 2026-11-11（用户裁定「**允许无主**」）：把两种"owner 不是公民"**分开报**——
    #   · `unattributed` = 当时没带凭证写入。**这是允许的正常态，不是故障**：
    #     它诚实（不冒充别人）、可筛选、可事后补。所以这里只告知，不催人归一。
    #   · 幽灵归属 = owner 指向一个**不存在的公民**（或别的乱值）。这才是真问题：
    #     归属指向了不存在的人，事后既查不到也补不回来。
    # 旧实现把两者混成一条并建议"跑 rebuild 归一到系统主人"——那等于把无主
    # **强行嫁祸给主人**，与"允许无主"正好相反。
    # 2026-11-11（序3）：必须含 `citizens_archive`。只用热表时，已归档公民名下的
    # 记忆会被误报成"幽灵归属"（实测归档 12 个后误报 1 条）。
    ids = citizen_ids_known(conn)
    _rows_owner = list(conn.execute("SELECT code,owner FROM code_index")) if ids else []
    _unattr = [c for c, o in _rows_owner if o == UNATTRIBUTED]
    _ghost = [c for c, o in _rows_owner
              if o != UNATTRIBUTED and o != "unknown" and o not in ids]
    if _unattr:
        print(f"  未归属 {len(_unattr)} 条（当时没带凭证写入；**允许无主**，可不处理）："
              f"{', '.join(_unattr[:5])}")
        print(f"        要补归属：fish owner-fix <编码> --owner <citizen_id>"
              f"（不补也合规）")
    if _ghost:
        print(f"  ⚠ owner 指向不存在的公民 {len(_ghost)} 条（幽灵归属，需要处理）："
              f"{', '.join(_ghost[:5])}")
    try:                            # 语料层（T03）：库不存在 → 空读数（"没采过"不是"坏了"）
        cst = fish_corpus.stats(root=root)
        print(_unattr_line(conn), end="")
        print(f"  语料 {cst['segments']} 段 / 会话 {cst['sessions']} / 降级 {len(cst['degraded'])}"
              + (f"  ⚠ 停滞 {cst['alarm']}（连续 {cst['stall']['zero_streak']} 轮 0 新增）"
                 if cst.get("alarm") else ""))
    except Exception as exc:        # noqa: BLE001 —— 语料体检失败不该拖垮主库体检
        # 2026-11-11：原先是裸 `pass` ——"判不到"被吞成"没问题"，违反三态分明。
        print(f"  语料：判不到（{type(exc).__name__}: {exc}）")
    # 知识库索引（外部审查 🔴1c，2026-11-11）：三条判据分开报，逻辑在索引层模块里。
    try:
        import fish_kb_index as _kbi
        for _line in _kbi.health_lines(conn):
            print(_line)
    except Exception as exc:        # noqa: BLE001
        print(f"  知识库索引：判不到（{type(exc).__name__}: {exc}）")
    # 采集活性（2026-11-20）：**每个 host 单独看**，不看总量。
    #
    # 为什么必须单列这一段：hook 采集断掉是静默的——插件加载失败、junction 断链、
    # 宿主升级改内部接口，表现全都是"不再新增"，而没有任何东西会因此报错。
    # 2026-11-09 起 DSH 侧断流 11 天没人发现，因为 WorkBuddy 一直在采，
    # 总数看起来一直"有数据"。**总量正常不等于每个通道都活着。**
    #
    # 这里的口径（详见 fish_activity.collection_liveness 的 docstring）：
    #   · 超过阈值没有任何新记录 = 疑似断流，单独报；
    #   · 声明过的 host 一次都没出现过 = "接了但没生效"，比断流更该看一眼；
    #   · 「没采过」与「很久没采」分开写——前者是没接过，后者才是断了。
    #
    # 退出码**不并入**这一项：24 小时阈值下，休假/周末本来就可能没有动作，
    # 把它算进 exit 1 会让"坏"这个信号同时代表两件事（与全文索引缺口同理）。
    try:
        live = fish_activity.collection_liveness(
            conn, hosts=("dsh", "workbuddy"))
        for name, item in sorted(live["hosts"].items()):
            if item.get("never_seen"):
                print(f"  ⚠ 采集活性：{name} 从未采到过 —— 接了但一次都没生效")
            elif item["stale"]:
                print(f"  ⚠ 采集活性：{name} 已停 {live['stale_hours']} 小时以上"
                      f"（最后 {item['last_seen']}）—— **疑似断流**，查 hook 插件是否加载")
            else:
                print(f"  采集活性：{name} 正常（最后 {item['last_seen']}，"
                      f"{live['stale_hours']}h 内 {item['recent']} 条）")
    except Exception as exc:        # noqa: BLE001 —— 活性检查失败不该拖垮主库体检
        print(f"  采集活性：判不到（{type(exc).__name__}: {exc}）")
    conn.close()
    # F-P1-6：**体检的结论必须能被机器读到**（退出码是脚本唯一可读的结论）。
    # 旧实现直接结束 → 永远 exit 0，于是「断链 1 条」在 && 链条里表现为成功。
    # 0 = 干净 / 1 = 有问题（断链或幽灵归属）/ 2 = 判不到（库缺失**或核心检查抛异常**）。
        # 判定口径**只按审查方声明的那条**：断链 / 幽灵归属 = 1。
    # 全文索引缺口（gap）不并入退出码——它属于另一条 P2 议题，混进来会让
    # 「exit 1」这个信号同时代表两件事，调用方无法区分该做什么。
    raise SystemExit(2 if broken is None else (1 if (broken or _ghost) else 0))


def cmd_reindex(args):
    """重建全文索引（ADR-005）。派生表，随时可重建，不影响事件与正文。"""
    root = Path(args.root)
    conn = connect(root)
    conn.executescript(SCHEMA)
    migrate(conn)
    before = conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]
    n = reindex_fts(conn, root)
    print(f"全文索引已重建：{before} → {n} 条（memory_fts）")
    conn.close()


def cmd_withdraw(args):
    """作废一条记忆（ADR-003 的 withdrawn：只标状态，编码不回收，文件保留）。"""
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    try:
        msg = withdraw_memory(conn, args.code, reason=args.reason or "",
                              actor=resolve_owner(conn))
    except ValueError as e:
        conn.close()
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(2)
    print(msg)
    conn.close()


def cmd_events(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    code = args.code
    if code:
        # B-4：按 subject.id 过滤（subject 是 JSON；用 LIKE 免去 json_extract 的编译依赖）
        like = '%"id": "' + code + '"%'
        rows = conn.execute(
            "SELECT seq,ts,type,subject FROM events WHERE subject LIKE ? "
            "ORDER BY seq DESC LIMIT ?", (like, args.limit)).fetchall()
    else:
        rows = conn.execute("SELECT seq,ts,type,subject FROM events ORDER BY seq DESC LIMIT ?",
                            (args.limit,)).fetchall()
    if not rows: print(f"（没有找到相关事件{f'：{code}' if code else ''}）")
    for r in rows:
        print("\t".join(str(x) for x in r))
    conn.close()


# ------------------------------------------------------------------ 看板（P1-b · 2026-10-25）
# 意义：**看板回答「此刻谁在动哪块」**——不是"发生了什么"（那是 daily）。
# 这条边界一旦破了，看板会退化成第二份 daily（`09` §4.6 的第三个已知坑）。
#
# 三条硬纪律（全部来自参照物的真实死亡）：
#   1. **无限追加必死** —— `active-sessions` v1 十天 114 条 → 396KB → 超读取上限而亡。
#      所以 `BOARD_MAX_BYTES` 是硬约束，不是建议。
#   2. **写入只走这里** —— AI 不许直接 write 渲染文件；文件是投影，可随时重建。
#   3. **只写自己行** —— 签到按 `(board_id, citizen_id)` 主键 upsert，不覆盖别人。


# ---------------------------------------------------------- 占用面：归一化与交集（2026-10-25）
# 外部评估 `EVAL-261025-09`：「surface 只声明不消费」——
# 两行 presence 都声明了 `p0/fish.py`，而 show 输出**无任何提示**。
# 全文件搜 `撞车|冲突|overlap` 只命中文案，**没有任何一处做交集比对**。
# 而「防撞车是这个功能存在的唯一理由」（工具描述原话），所以这是功能核心缺失，
# 不是体验瑕疵——它给人一个**假的安全感**，比没有更糟。


# 2026-10-26（hooks 深度利用 T01）：`normalize_surface` / `surface_hit` 已抽到
# `fish_surface.py`，本文件在顶部 `from fish_surface import ...` 原样再导出。
# 为什么抽走：这两个函数是**占用面判定的唯一实现**，而 hook（PreToolUse/PostToolUse）
# 不能 import fish（400ms）。抽成小模块后，hook 与 fish 共用同一份判定，
# 「板上一套前缀判定、ask 又一套」这个病从结构上消失。
# 现有 24 处调用（fish/CLI/MCP/测试）**一个都不用改**——名字还在，语义逐字不变。


def cmd_board(args):
    """看板 CLI：show / checkin / post / render / close。

    `--project` 是 `--scope project --key <项目>` 的便捷写法（日常最常用）。
    """
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    root = Path(args.root)
    try:
        act = args.board_action

        # sweep 不走 scope/key（它是**全局批量**动作），所以必须放在读 args.scope 之前——
        # 它的 parser 没有那些参数，先读会 AttributeError（2026-10-25 写完当场发现的）。
        if act == "sweep":
            sw = board_sweep(conn, root, getattr(args, "days", None))
            if not sw.get("cutoff"):
                print("跳过：board.sweep_days < 1")
                return 0
            print(f"已归档 {sw['swept']} 块（截止 {sw['cutoff']}）")
            for b in sw["closed"][:10]:
                print(f"  {b['scope']}:{b['scope_key']} {b['period_key']}")
            if len(sw["closed"]) > 10:
                print(f"  … 共 {len(sw['closed'])} 块")
            return 0

        scope, key = args.scope, args.key
        if getattr(args, "project", None):
            scope, key = "project", args.project

        if act == "checkin":
            # 2026-10-25 修（真实事故：anyi-studio 主 AI 签到被记成「安逸」）：
            # **不带 --citizen 时不再默认落到系统主人头上**。
            #
            # 为什么这必须改：签到是「声明我是谁」。默认成主人 = **冒充**。
            # 而且它是静默的——看板上出现一行"安逸在改 fish.py"，而安逸本人在睡觉。
            # 出事之后要靠人肉发现并清理，那正是"默认值把行为记到别人头上"的代价。
            citizen = (getattr(args, "citizen", None) or "").strip()
            # 2026-10-29（体验复测报告 B1）：`--token` 现在能替代 `--citizen`——
            # token 是**凭证**（比可读的 citizen_id 更可信），而且这样"签到忘了带
            # --citizen"不用再去 `whoami` 绕一圈。显式 `--citizen` 仍优先。
            if not citizen:
                _tok = (getattr(args, "token", None)
                        or os.environ.get("FISH_TOKEN") or "").strip()
                if _tok:
                    citizen = (validate_session_token(conn, _tok) or "").strip()
                    if not citizen:
                        print("错误：--token 无对应公民（凭证不存在/已吊销/已过期）。",
                              file=sys.stderr)
                        return 2
            if not citizen:
                print("错误：board checkin 必须带 --citizen。", file=sys.stderr)
                print("  签到是「声明我是谁」——不指定就默认记到系统主人头上等于冒充。",
                      file=sys.stderr)
                print("  不知道自己的 citizen_id？→ 先跑 `fish whoami`，或 "
                      "`fish session start --project X` 签发凭证后用回执里的 citizen_id。",
                      file=sys.stderr)
                return 2
            doing = (args.doing or "").strip()
            if not doing:
                print("错误：--doing 必填——看板的核心价值就是让别人靠这一句"
                      "判断会不会跟你撞车；没有 doing 的在场行等于没签到。",
                      file=sys.stderr)
                return 2
            st = getattr(args, "status", "active") or "active"
            # 2026-10-26（撞车自撞修复 T02）：终端里没有 token 上下文，所以会话键
            # 走 `--session-key` / env `FISH_SESSION_KEY`。`--agent` 语义不变：
            # 只在没给 agent_id 时由 `board_checkin` 回落到会话键。
            sk = (getattr(args, "session_key", None)
                  or os.environ.get("FISH_SESSION_KEY") or "").strip()
            board = board_checkin(conn, citizen, doing=doing,
                                  surface=getattr(args, "surface", "") or "",
                                  scope=scope, scope_key=key,
                                  agent_id=getattr(args, "agent", None),
                                  status=st, session_key=sk)
            board_post(conn, board, citizen, "checkin" if st == "active" else "left",
                       doing, session_key=sk)
            path, size, split = board_render(conn, root, board)
            verb = "已签到" if st == "active" else "已下板"
            print(f"{verb}\t{board['path']}\t{size} B" + ("（已切分）" if split else ""))
            return 0

        if act == "post":
            citizen = (getattr(args, "citizen", None) or resolve_owner(conn) or "unknown")
            board = board_ensure(conn, scope, key)
            sk = (getattr(args, "session_key", None) or os.environ.get("FISH_SESSION_KEY") or "").strip()
            text = autolink_memory_code(conn, args.text, sk)
            pid = board_post(conn, board, citizen, args.kind, text, session_key=sk)
            if pid is None:
                print("错误：动态内容为空", file=sys.stderr)
                return 2
            board_render(conn, root, board)
            print(f"已记录\t{board['path']}\t#{pid}")
            return 0

        if act == "render":
            # `--all`（2026-10-26 新增）：按 boards 表渲染**全部**看板。
            # 为什么需要它：`board_render` 只渲染一块板，而 `boards.path` 迁移后
            # 验收要求"迁移后 `fish board render` 仍能重建全部看板文件"（PRD §5.4 判据）。
            # 没有这个入口，"重建全部"只能靠调用方遍历表——那正是判据与实现分离要避免的。
            if getattr(args, "render_all", False):
                rows = conn.execute(
                    "SELECT id, scope, scope_key, period_key, path, status, created_at, "
                    "closed_at FROM boards ORDER BY scope, scope_key, period_key"
                ).fetchall()
                if not rows:
                    print("没有可渲染的看板（boards 为空）")
                    return 0
                keys = ("id", "scope", "scope_key", "period_key", "path", "status",
                        "created_at", "closed_at")
                total = 0
                for row in rows:
                    board = dict(zip(keys, row))
                    path, size, split = board_render(conn, root, board)
                    total += size
                    print(f"{path}\t{size} B" + ("（已切分）" if split else ""))
                print(f"共渲染 {len(rows)} 块看板，合计 {total} B")
                print(f"落点：{resolve_home_root(root, conn=conn)}")
                return 0
            board = board_ensure(conn, scope, key)
            path, size, split = board_render(conn, root, board)
            print(f"{path}\t{size} B" + ("（已切分）" if split else ""))
            return 0

        if act == "close":
            board = board_close(conn, root, scope, key)
            print(f"已归档\t{board['path']}")
            return 0

        # show（默认）
        info = board_show(conn, scope, key)
        if getattr(args, "as_json", False):
            # 机器视图：**保留 ID**（它就是主键），另带会话字段。
            # 人类面（上面的 markdown）反过来——永不含 26 位公民 ID。
            print(json.dumps({
                "board": info["board"],
                "presence": info["presence"],
                "posts": [{"citizen_id": p[0], "kind": p[1], "text": p[2],
                           "created_at": p[3]} for p in info["posts"]],
            }, ensure_ascii=False, indent=1))
            return 0
        print(_render_one(conn, info["board"], info["posts"]))
        return 0
    finally:
        conn.close()


# ══════════════════════════════════════ B1/B2/B3：门面 · 状态表 · 知识库
# 依据：`docs/架构设计-B1B2B3-v0.1.md` §3.4 / §5.1 / §5.4 / §5.7 / §7.3。
# 分区（与 §1.3 ⑤ 的子段一一对应）：
#   5.1 路径与根解析 / 原子写 / 摘要 / 错误模型    ← 本段（T01）
#   5.2 门面渲染（layout）                          ← T02
#   5.3 项目状态表（project status）                ← T03
#   5.4 知识库（kb）                                ← T04
# 纪律：新增代码只追加，不改既有段落；新增参数一律仅关键字（K10）。

# ---------------------------------------------------------------- 5.1 共用底座


def _parse_as_of(raw=None):
    """解析 `--as-of`：接受 `...Z` / `...+08:00` / 无时区（按本地时区补）。

    为什么不让 argparse 直接 `type=datetime.fromisoformat`：它的报错形态与
    其它用法错误不同（会抛 Traceback），而契约要求"解析失败 → exit 2"。
    这里统一抛 `ValueError`，由 CLI 层转成 `SystemExit(2)`。
    """
    if raw in (None, ""):
        return datetime.datetime.now().astimezone()
    s = str(raw).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError as exc:
        raise ValueError(
            f"--as-of 无法解析：{raw!r}（示例：2026-10-26T00:00:00+08:00 或 "
            "2026-10-26T00:00:00Z）") from exc
    if dt.tzinfo is None:
        dt = dt.astimezone()          # 无时区 = 本地时区（不是 UTC，见 iso_local 的说明）
    return dt


def _parse_as_of_arg(raw=None):
    """CLI 侧包装：解析失败打印一行并 `SystemExit(2)`（用法错误，不是域错误）。"""
    try:
        return _parse_as_of(raw)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2)


def _as_of_utc_z(dt) -> str:
    """as-of → 数据库时间戳口径（UTC + `Z`）：用于 `recorded_at <= ?` 这类比较。"""
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- 5.2 门面（B1）
# HOME-1~9 的判据面全在这里：`render_layout` 只写 `fish-home/**`，
# `verify_layout` 只读、逐条给 code。两条纪律贯穿本段：
#   ① 一切时间走 `as_of`（同一 as-of 两次渲染逐字节全等）——不许在产物里写 `now()`；
#   ② 一切落盘走 `_layout_write`（越界/写进 data_root 直接 raise，而不是"小心一点"）。

# 正文文件名形态（HOME-3 的第一重判据）：`编码__标题.md` 或 `编码.md`。
# 与 body_relpath 的实际命名同源；复制后改名的情况由 sha256 交集兜住（第二重判据）。
MEMORY_BODY_NAME_RE = re.compile(
    r"^[A-Z\u4e00-\u9fff]{2,8}-\d{6}-\d{2,3}(__.+)?\.md$")

_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)")
_AS_OF_RE = re.compile(r"as-of\s+([0-9T:\-+\.Z]+)")


def layout_paths(home_root) -> dict:
    """门面路径集合（唯一拼路径的地方，render/verify 共用，避免两处各拼一次）。"""
    home = Path(home_root)
    top = {name: home / name for name in LAYOUT_TOP}
    return {
        "home": home,
        "top": top,
        "status_table": home / LAYOUT_STATUS_TABLE,
        "memory": top[LAYOUT_MEMORY_DIR],
        # 注意：索引文件名按 as_of 的月份决定；这里给"当前月"的默认值仅为形状完整，
        # 真正渲染用 `_memory_index_relpath(as_of)`（as-of 冻结时不能用 now 的月份）。
        "index_current": top[LAYOUT_MEMORY_DIR] / f"索引-{datetime.datetime.now():%Y-%m}.md",
        "recent": top[LAYOUT_MEMORY_DIR] / "最近.md",
        "vaults": top["30-知识库"] / "vaults.md",
        "system": {
            "identity": top[LAYOUT_SYSTEM_DIR] / "IDENTITY.md",
            "capabilities": top[LAYOUT_SYSTEM_DIR] / "CAPABILITIES.md",
            "manifest": top[LAYOUT_SYSTEM_DIR] / "渲染清单.md",
        },
    }


def _memory_index_relpath(as_of) -> str:
    return f"{LAYOUT_MEMORY_DIR}/索引-{as_of.strftime('%Y-%m')}.md"

def _cell(value) -> str:
    """表格单元格消毒：`|` 会破表、换行会断行；None 显示空（由调用方决定用哪个 `—`）。"""
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def _read_text(path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _file_fingerprint(path):
    """`{"bytes","sha256"}`；读不到返回 None（清单里留给调用方决定怎么标）。"""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return {"bytes": len(data), "sha256": kb_sha256_bytes(data)}


def _scan_credentials(text: str) -> list:
    """凭证形态扫描（HOME-8）：返回命中的正则（空列表 = 干净）。"""
    return [pat for pat in CREDENTIAL_PATTERNS if re.search(pat, text or "")]


def _readme_missing_elements(text: str) -> list:
    """README 四要素缺失清单（空列表 = 全中）。判据只认这四组关键词。"""
    missing = []
    for marker in README_SECTION_MARKERS:
        if not re.search(rf"^##\s*{re.escape(marker)}\s*$", text or "", re.MULTILINE):
            missing.append(f"## {marker}")
    if README_NEGATIVE_WORD not in (text or ""):
        missing.append(f"“{README_NEGATIVE_WORD}”说明")
    for marker in README_STAMP_MARKERS:
        if marker not in (text or ""):
            missing.append(marker)
    return missing


def _layout_only_set(only):
    """函数层 `only` 归一（CLI 本批不暴露 `--only`，但签名预留，见 §3.4）。"""
    if only is None:
        return None
    items = [only] if isinstance(only, str) else list(only)
    clean = {str(x).strip().strip("/\\") for x in items if str(x).strip()}
    if not clean:
        return None
    unknown = clean - set(LAYOUT_TOP)
    if unknown:
        raise ValueError(f"only 只接受 {LAYOUT_TOP}，收到未知项：{sorted(unknown)}")
    return clean


def _degraded_section(source, exc) -> str:
    """HOME-9 的固定段：数据源读不到时**明说**，不许渲染空白文件、不许静默 0。"""
    return "\n".join([
        f"## 数据源不可用：{source}",
        "",
        f"原因：`{_exc_text(exc)}`",
        "",
        "本页**不是空文件**：缺失的内容就是读不到的那部分投影，不按 0 处理。",
        "修法：确认 `data_root/index.sqlite` 可读且表存在，然后重跑 "
        "`fish layout render --all`。",
    ])


def _layout_write(home_root, relpath, text, *, data_root) -> dict:
    """门面写盘的**唯一入口**（§6.4 三重保证的代码层）。

    三条断言：① relpath 必须是相对路径、且不含 `..`（不接受盘符/UNC/绝对路径）；
    ② 解析后必须在 `home_root` 内（`commonpath`，不是 `startswith`）；
    ③ **必须**不在 `data_root` 内——门面是投影，写回真相根就不再是投影（HOME-6）。

    ## 2026-10-26 第二轮（R16 · 评审 🟡-1）：`data_root` 改成**必有**参数

    它原来是可选参数（`data_root=None`），于是 9 个调用点里有 6 个没传 → 第三条断言
    在那些路径上**整段跳过**（README / 记忆索引 / 最近 / vaults 投影 / 两处降级分支）。
    设计说"所有门面写盘都过这条闸"，实现只过了 1/3——现在缺参数会在调用点当场 `TypeError`，
    "漏传一处"变成"写不出来"，而不是"悄悄少一道闸"。
    """
    if data_root is None:
        raise RuntimeError("门面写盘必须给 data_root（第三条断言需要它，R16）")
    home = Path(home_root)
    rel = Path(str(relpath))
    if rel.is_absolute() or str(relpath).startswith(("\\\\", "//")):
        raise RuntimeError(f"门面写盘只接受相对路径：{relpath!r}")
    if any(part == ".." for part in rel.parts):
        raise RuntimeError(f"门面写盘路径不得含 `..`：{relpath!r}")
    target = home / rel
    if not _path_inside(target, home):
        raise RuntimeError(f"门面写盘越界：{target} 不在 {home} 内")
    if _path_inside(target, data_root):
        raise RuntimeError(f"门面写盘禁止写进 data_root：{target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    data = (text if text.endswith("\n") else text + "\n").encode("utf-8")
    target.write_bytes(data)
    return {"path": rel.as_posix(), "bytes": len(data),
            "sha256": kb_sha256_bytes(data)}


_LAYOUT_README_SPEC = {
    "00-首页": {
        "what": "fish 的门面首页：一句话看懂三库（记忆 / 知识库 / 项目）在哪、自己该去哪里。",
        "can": "沿本目录进入 `10-项目库` / `20-记忆库` / `30-知识库` / `90-系统`；"
               "项目状态一屏表在 [项目状态表](../10-项目库/项目状态表.md)。",
        "cannot": "在这里写正文、改索引、放凭证——门面全是投影，手改会被下一次渲染覆盖。",
        "who": "`fish layout render --all`（fish 代码）；"
               "人只改 `data_root` 里的正文与知识库原目录。",
    },
    "10-项目库": {
        "what": "每个项目一个目录：当日看板（`看板-<YYMMDD>.md`）+ 一张项目状态表。",
        "can": "打开项目目录里的看板看「此刻谁在动哪块」；"
               "`项目状态表.md` 汇总所有项目的状态 / 最近活跃 / 看板入口 / 主责 AI / 未了事项。",
        "cannot": "在这里写记忆正文（正文唯一副本在 `data_root/projects/<项目>/`）；"
                  "看板也不在这里手写——它由 `fish board render` 生成。",
        "who": "`fish board render`（看板）与 `fish layout render --all`（状态表），都是 fish 代码。",
    },
    "20-记忆库": {
        "what": "记忆的**索引**：本月索引（`索引-YYYY-MM.md`）与最近 30 条（`最近.md`）。"
                "正文不在这里。",
        "can": "按编码 / 标题找到正文相对路径，再去 `data_root` 打开正文。",
        "cannot": "放正文副本——任何 `编码__标题.md` 形态的文件、"
                  "或与 `data_root/projects/**` 内容相同的 md，都会被 `fish layout verify` 判红。",
        "who": "`fish layout render --all` 从 `code_index` 渲染；人不需要手改。",
    },
    "30-知识库": {
        "what": "已登记知识库（Obsidian vault）的清单与最近扫描读数。",
        "can": "看有哪些 vault、路径、读写模式、忽略规则；用 `fish kb ...` 阅读与（显式授权时）写入。",
        "cannot": "把知识库内容复制进来——vault 原地不动，永不搬进 `fish-home`。",
        "who": "`fish layout render --all`（读 `kb_vault`）；登记入口是 `fish kb vault add`。",
    },
    "90-系统": {
        "what": "系统页：身份（IDENTITY）、能力（CAPABILITIES）、渲染清单（本目录的产物清单）。",
        "can": "确认当前是谁在写、哪些能力已接入、这份门面包含哪些文件。",
        "cannot": "出现任何凭证形态——`fsh_` 前缀、`token` 赋值形态、私钥头都会被"
                  "渲染期自检与 verify 判红。",
        "who": "`fish layout render --all`；`渲染清单.md` 是自描述的产物清单。",
    },
}


def _readme_text(top_name, as_of) -> str:
    spec = _LAYOUT_README_SPEC[top_name]
    lines = [
        f"# {top_name} · README",
        "",
        "## 这是什么",
        "",
        spec["what"],
        "",
        "## 能做什么",
        "",
        spec["can"],
        "",
        f"**{README_NEGATIVE_WORD}**做的事：{spec['cannot']}",
        "",
        "## 谁在写",
        "",
        spec["who"],
        "",
        f"生成时刻：{iso_local(as_of)}（as-of 冻结时间；同一 as-of 重渲染逐字节一致）",
        "",
        "> 手改会被覆盖：本文件由 `fish layout render --all` 生成。要保留你的修改，",
        "> 请改源数据，或先备份再用 `--force-readme` 覆盖。",
        "",
    ]
    return "\n".join(lines)


def render_readme(home_root, top_name, *, as_of, force=False, data_root):
    """渲染一个一级目录的 README；已存在且未 `force` → 返回 `None`（不覆盖，裁决 C5）。

    `data_root` 是**必有**参数（R16）：`_layout_write` 的第三条断言靠它，
    不给就等于让这条写盘路径少一道闸。
    """
    if top_name not in LAYOUT_TOP:
        raise ValueError(f"未知一级目录：{top_name!r}（可用：{', '.join(LAYOUT_TOP)}）")
    home = Path(home_root)
    rel = f"{top_name}/README.md"
    if (home / rel).exists() and not force:
        return None
    return _layout_write(home, rel, _readme_text(top_name, as_of), data_root=data_root)


def _short_citizen_id(citizen_id) -> str:
    """公民 id → 可读短 id（R1：归属列优先 display_name，取不到才回落到它）。

    为什么截到 12 再补 `…`：完整 ULID 是 26 字符，一列就把整行宽度吃掉；
    12 位足够在同一份索引里互相区分（判据按"不同人不同串"断言，不按具体长度）。
    """
    s = str(citizen_id or "").strip()
    if not s:
        return ""
    return s if len(s) <= 12 else s[:12] + "…"


def render_memory_index(conn, home_root, *, as_of, data_root,
                         viewer_citizen_id=None) -> dict:
    """`20-记忆库/索引-YYYY-MM.md`：**只列索引，不写正文副本**（HOME-3）。

    ## 2026-10-26 R1 修订（主理人裁决；原因是实测文件 65 KB / 246 行）

    1. **归属列**：完整 citizen ULID → 可读 `display_name`（查不到才回落短 id）。
       一行 26 字符的 ULID 对人不构成信息，而人读索引时第一个问题就是"这是谁写的"。
    2. **标题列**：截到 `LAYOUT_INDEX_TITLE_MAX_CHARS`（48）字，超出加 `…`。
       只截索引，**正文标题一字不改**（真相在 code_index / 正文文件里）。
    3. **正文相对路径列保留**（HOME-3 要求：它是"去 data_root 找正文"的唯一指针）。
    4. **文件预算 128 KB**（`LAYOUT_INDEX_MAX_BYTES`）：超出时**按行截断并脚注明示**，
       不静默丢行。原 PRD §6.3 的 32 KB 是看板口径（看板文件曾因 396 KB 而读不动），
       索引不进注入上下文、按月分片，故按裁决放宽（偏离记录见 docs/偏离记录-*.md）。
    """
    home = Path(home_root)
    month = as_of.strftime("%Y-%m")
    rel = _memory_index_relpath(as_of)
    head = [
        f"# 记忆索引 · {month}",
        "",
        f"> 生成器：`fish layout render --all` ｜ as-of {iso_local(as_of)}",
        "> 这里**只有索引**：正文唯一副本在 `data_root/projects/**`（最后一列是相对路径）。",
        "> 按 `code_index.recorded_at` 的月份投影；同月内按编码排序（确定性）。",
        f"> 文件预算：上限 {LAYOUT_INDEX_MAX_BYTES // 1024} KB（超限按行截断并在页脚明示）；"
        f"标题列截到 {LAYOUT_INDEX_TITLE_MAX_CHARS} 字。",
        "",
    ]
    try:
        # 第五轮评审 🔴：这两个渲染器把 code + 标题 + 归属 + 正文路径
        # **写成持久化明文**落进 `20-记忆库/`，每个会话读记忆时都会读到。
        # 比 `fish://index`（一次性响应）严重——必须过滤。
        _riv, _rip = _visibility_sql(conn, viewer_citizen_id)
        rows = conn.execute(
            "SELECT code, title, owner, status, file_path FROM code_index "
            "WHERE substr(recorded_at,1,7)=? AND recorded_at<=?" + _riv
            + " ORDER BY code",
            (month, _as_of_utc_z(as_of)) + tuple(_rip)).fetchall()
    except sqlite3.Error as exc:
        text = "\n".join(head + ["", _degraded_section("code_index", exc), ""])
        res = _layout_write(home, rel, text, data_root=data_root)
        res["degraded"] = True
        return res

    table_head = ["| 编码 | 标题 | 归属 | 状态 | 正文相对路径 |", "|---|---|---|---|---|"]
    names = {}                     # owner → 显示名（同一份索引里逐行查同一批公民是浪费）

    def _owner_label(owner) -> str:
        key = str(owner or "").strip()
        if not key:
            return "—"
        if key not in names:
            try:
                display = _display_name_of(conn, key)
            except sqlite3.Error:
                display = None
            names[key] = (display or "").strip() or _short_citizen_id(key)
        return names[key]

    body = []
    for code, title, owner, status, file_path in rows:
        body.append(
            f"| `{_cell(code)}` | {_cell(_clip_chars(title or '', LAYOUT_INDEX_TITLE_MAX_CHARS)) or '—'} "
            f"| {_cell(_owner_label(owner)) or '—'} "
            f"| {_cell(status) or '—'} | `{_cell(file_path) or '—'}` |")

    # 预算：按 UTF-8 字节逐行累加，留出脚注与合计行的余量（不写完再截，避免"自以为写全了"）。
    reserve = 600
    used = sum(len(x.encode("utf-8")) + 1 for x in head + table_head) + reserve
    kept = []
    for line in body:
        cost = len(line.encode("utf-8")) + 1
        if used + cost > LAYOUT_INDEX_MAX_BYTES:
            break
        kept.append(line)
        used += cost
    dropped = len(body) - len(kept)

    lines = head + table_head
    if kept:
        lines += kept
    else:
        lines.append("| — | （本月 `recorded_at` 无记录，或已达文件预算） | — | — | — |")
    lines += ["", f"合计：{len(rows)} 条" +
              (f"（本页显示 {len(kept)} 条，余 {dropped} 条未列出——已达 "
               f"{LAYOUT_INDEX_MAX_BYTES // 1024} KB 上限）" if dropped else "。")]
    if dropped:
        lines += [
            "",
            f"> 截断说明：本页按 {LAYOUT_INDEX_MAX_BYTES // 1024} KB 硬上限截断，"
            f"省略 {dropped} 条（不静默丢：原始 246 行规模的月份也会触发这里）。",
            "> 看其余条目：`fish recall <关键词>`，或按编码在 `data_root/projects/**` 找正文。",
        ]
    lines += [""]
    return _layout_write(home, rel, "\n".join(lines), data_root=data_root)


def render_memory_recent(conn, home_root, *, as_of, limit=30, data_root,
                          viewer_citizen_id=None) -> dict:
    """`20-记忆库/最近.md`：as-of 之前最近 N 条（默认 30），同样只列索引。"""
    home = Path(home_root)
    rel = f"{LAYOUT_MEMORY_DIR}/最近.md"
    head = [
        f"# 最近记忆 · {limit} 条",
        "",
        f"> 生成器：`fish layout render --all` ｜ as-of {iso_local(as_of)}",
        "> 只列索引；正文在 `data_root/projects/**`，本页不含任何正文副本。",
        "",
    ]
    try:
        _rcv, _rcp = _visibility_sql(conn, viewer_citizen_id)   # 同上，必须过滤
        rows = conn.execute(
            "SELECT recorded_at, code, title, owner, file_path FROM code_index "
            "WHERE recorded_at<=?" + _rcv
            + " ORDER BY recorded_at DESC, code DESC LIMIT ?",
            (_as_of_utc_z(as_of),) + tuple(_rcp) + (int(limit),)).fetchall()
    except sqlite3.Error as exc:
        text = "\n".join(head + ["", _degraded_section("code_index", exc), ""])
        res = _layout_write(home, rel, text, data_root=data_root)
        res["degraded"] = True
        return res
    lines = head + ["| 记录时间 | 编码 | 标题 | 归属 | 正文相对路径 |",
                    "|---|---|---|---|---|"]
    if rows:
        for recorded_at, code, title, owner, file_path in rows:
            lines.append(
                f"| {_cell(recorded_at) or '—'} | `{_cell(code)}` | {_cell(title) or '—'} "
                f"| {_cell(owner) or '—'} | `{_cell(file_path) or '—'}` |")
    else:
        lines.append("| — | — | （还没有记忆） | — | — |")
    lines += ["", f"合计：{len(rows)} 条（上限 {limit}）。", ""]
    return _layout_write(home, rel, "\n".join(lines), data_root=data_root)


def _vaults_projection(conn, *, as_of) -> dict:
    """`30-知识库/vaults.md` 的文本投影（`kb_vault` 读不到 → 固定降级段）。"""
    head = [
        "# 知识库登记（vaults）",
        "",
        f"> 生成器：`fish layout render --all` ｜ as-of {iso_local(as_of)}",
        "> vault 原地不动：这里只有登记信息与最近扫描读数，没有任何内容副本。",
        "",
    ]
    try:
        rows = conn.execute(
            "SELECT name, path, mode, ignore_globs, last_scan_at, last_scan_files, "
            "last_scan_notes, last_scan_bytes FROM kb_vault "
            "WHERE status='active' ORDER BY is_default DESC, name").fetchall()
    except sqlite3.Error as exc:
        return {"rows": [], "degraded": True,
                "text": "\n".join(head + ["", _degraded_section("kb_vault", exc), ""])}
    lines = head + ["| 名字 | 路径（原样） | 模式 | 忽略规则 | 最近扫描 | 文件 / 笔记 / 字节 |",
                    "|---|---|---|---|---|---|"]
    if rows:
        for name, path, mode, ig, scanned_at, files, notes, nbytes in rows:
            lines.append(
                f"| {_cell(name)} | `{_cell(path)}` | {_cell(mode)} | `{_cell(ig)}` "
                f"| {_cell(scanned_at) or '—（未扫描）'} "
                f"| {files if files is not None else '—'} / "
                f"{notes if notes is not None else '—'} / "
                f"{nbytes if nbytes is not None else '—'} |")
    else:
        lines.append("| — | （还没有登记任何知识库） | — | — | — | — |")
    lines += ["", "登记入口：`fish kb vault add --name <名字> --path <绝对路径> --mode ro|rw`。", ""]
    return {"rows": [dict(name=r[0], path=r[1], mode=r[2], ignore_globs=r[3],
                          last_scan_at=r[4], files=r[5], notes=r[6], bytes=r[7])
                     for r in rows],
            "degraded": False, "text": "\n".join(lines)}


def _render_vaults(conn, home_root, *, as_of, data_root) -> dict:
    proj = _vaults_projection(conn, as_of=as_of)
    res = _layout_write(home_root, "30-知识库/vaults.md", proj["text"],
                        data_root=data_root)
    if proj["degraded"]:
        res["degraded"] = True
    return res


def _layout_owner_and_counts(conn):
    """IDENTITY 页的数据：系统主人 + 规模读数。返回 `(owner, counts, error)`。

    **主人走既有读点**（`resolve_owner` + `_display_name_of`），不在这里新写一条
    `SELECT ... FROM citizens`：`test_identity_p1a::test_G_existing_queries_untouched`
    规定"任何**新增**函数里的 citizens 语句必须显式声明"，而本批不允许改那个测试文件。
    复用既有函数既满足纪律，也避免门面与 whoami 对"谁是主人"给出两种答案。
    """
    owner = {"display_name": None, "citizen_id": None}
    counts = {}
    try:
        cid = resolve_owner(conn) or ""
        owner = {"citizen_id": cid or None,
                 "display_name": (_display_name_of(conn, cid) if cid else None)}
        for key, sql in (
                ("memories", "SELECT COUNT(*) FROM code_index WHERE status='active'"),
                ("boards", "SELECT COUNT(*) FROM boards"),
                ("vaults", "SELECT COUNT(*) FROM kb_vault WHERE status='active'")):
            counts[key] = conn.execute(sql).fetchone()[0]
    except (sqlite3.Error, KeyError) as exc:
        return owner, counts, exc
    return owner, counts, None

def _system_page_lines(kind, *, as_of, conn, data_root, inventory, home_root):
    """三张系统页的正文（纯函数：同 as-of + 同数据 → 同字节）。"""
    stamp = [f"> 生成器：`fish layout render --all` ｜ as-of {iso_local(as_of)}", ""]
    if kind == "identity":
        owner, counts, err = _layout_owner_and_counts(conn)
        lines = [
            "# IDENTITY · 系统身份",
            "",
            *stamp,
            "## 这是什么",
            "",
            "这套 fish 系统的身份与规模，供新来的 AI 在 30 秒内确认"
            "「我在哪、谁在写、这里有多少东西」。",
            "",
        ]
        if err is not None:
            lines += [_degraded_section("身份 / 规模读数", err), ""]
        else:
            lines += [
                "## 系统主人",
                "",
                f"- 显示名：{_cell(owner['display_name']) or '—（未登记 is_owner）'}",
                f"- citizen_id：`{_cell(owner['citizen_id']) or '—'}`",
                "",
                "## 规模（只读投影）",
                "",
                "| 项 | 数量 |",
                "|---|---|",
                f"| 记忆（active） | {counts.get('memories', '—')} |",
                f"| 看板 | {counts.get('boards', '—')} |",
                f"| 已登记知识库 | {counts.get('vaults', '—')} |",
                "",
            ]
        lines += [
            "## 谁在写",
            "",
            "`fish layout render --all`。本页**不含任何凭证**：凭证只存在人的凭证管理文档"
            "与本机环境变量里，门面产物永远不落它。",
            "",
        ]
        return lines
    if kind == "capabilities":
        kb_ready = _kb_commands_ready()
        lines = [
            "# CAPABILITIES · 能力清单",
            "",
            *stamp,
            "## 这是什么",
            "",
            "**当前构建**已接入能力的导航页。判据以 CLI `--help` 与测试为准，本页只做索引，",
            "不承诺未接入的能力。",
            "",
            "## 记忆与检索",
            "",
            "- `fish remember` / `recall` / `neighbors` / `inject`：事件流 + 投影 + 注入包。",
            "- 真相：`data_root/index.sqlite`；正文：`data_root/projects/**`（唯一副本）。",
            "",
            "## 看板",
            "",
            "- `fish board checkin` / `post` / `render` / `close` / `sweep`："
            "落点已迁到 `fish-home/`（裁决 C3）。",
            "- 真相在 `boards` / `presence` / `board_posts`；文件是投影，删了可重建。",
            "",
            "## 门面",
            "",
            "- `fish layout render --all` / `fish layout verify`：本目录的来源与体检。",
            "",
            "## 知识库",
            "",
        ]
        if kb_ready:
            lines += ["- `fish kb vault` / `tree` / `open` / `search` / `write` / `rollback`："
                      "按需扫描（不建索引），写入有备份与回滚。",
                      "- 边界：知识库内容不进 `memory_fts` / `recall` / `inject`（KB-10）。"]
        else:
            lines += ["- 读写命令**尚未接入**本构建：当前只有 `kb_vault` / `kb_access` 表结构",
                      "  （`fish kb vault add` 等命令会明确报 `尚未实现` 并退出 2）。"]
        lines += [
            "",
            "## 不能做什么（边界）",
            "",
            "- 不把知识库内容复制进门面；不把门面写回 `data_root`；不在任何页写凭证。",
            "- 不把读到的内容自动写成记忆（KB-10 的永久边界）。",
            "",
            "## 谁在写",
            "",
            "`fish layout render --all`（fish 代码）。",
            "",
        ]
        return lines
    raise ValueError(f"未知系统页：{kind!r}")


def render_system_pages(conn, data_root, home_root, *, as_of, written) -> list:
    """渲染 `90-系统/` 三页；**渲染后立刻跑凭证自检**，命中则不落盘（HOME-8）。

    `written` 是本次渲染的产物清单（含被跳过覆盖的 README），`渲染清单.md` 用它
    作"这份门面包含哪些文件"的依据。清单按 `LAYOUT_TOP` 顺序 + 路径排序，
    与"本次是首次渲染还是二次渲染"无关——这是同一 as-of 逐字节全等的前提。

    返回：成功落盘的 `{"path","bytes","sha256"}`；被凭证自检拦下的
    `{"path","blocked":"credential_like","bytes":0,"sha256":""}`（由调用方记 degraded）。
    """
    home = Path(home_root)
    paths = layout_paths(home)
    results = []

    def _emit(kind, target):
        lines = _system_page_lines(kind, as_of=as_of, conn=conn, data_root=data_root,
                                   inventory=written, home_root=home)
        text = "\n".join(lines)
        rel = target.relative_to(home).as_posix()
        hits = _scan_credentials(text)
        if hits:
            return {"path": rel, "blocked": "credential_like",
                    "bytes": 0, "sha256": "", "patterns": hits}
        return _layout_write(home, rel, text, data_root=data_root)

    results.append(_emit("identity", paths["system"]["identity"]))
    results.append(_emit("capabilities", paths["system"]["capabilities"]))

    # 渲染清单：列出"这份门面包含的全部产物"，而不是"本次新写了哪些"。
    inventory = []
    seen = set()
    for item in list(written) + list(results):
        rel = str((item or {}).get("path") or "")
        if rel and rel not in seen:
            seen.add(rel)
            inventory.append(rel)

    def _order(rel):
        top = rel.split("/")[0]
        try:
            return (LAYOUT_TOP.index(top), rel)
        except ValueError:
            return (len(LAYOUT_TOP), rel)

    lines = [
        "# 渲染清单（manifest）",
        "",
        f"> 生成器：`fish layout render --all` ｜ as-of {iso_local(as_of)}",
        "> 本页列出这份门面的全部产物（相对 `fish-home/` 的路径 / 字节 / sha256 前 12）。",
        "> 全部只写 `fish-home/**`；`data_root` 零写入（HOME-6）。",
        "> 本页不列自己（自指的 hash 不可判）；README 已存在时不会被覆盖，但仍列在清单里。",
        "",
        "| 相对路径 | 字节 | sha256 前 12 |",
        "|---|---|---|",
    ]
    for rel in sorted(inventory, key=_order):
        fp = _file_fingerprint(home / rel)
        if fp is None:
            lines.append(f"| `{rel}` | — | —（文件缺失） |")
        else:
            lines.append(f"| `{rel}` | {fp['bytes']} | `{fp['sha256'][:12]}` |")
    lines += ["", f"合计：{len(inventory)} 个产物。", ""]
    target = paths["system"]["manifest"]
    text = "\n".join(lines)
    hits = _scan_credentials(text)
    if hits:
        results.append({"path": target.relative_to(home).as_posix(),
                        "blocked": "credential_like", "bytes": 0, "sha256": "",
                        "patterns": hits})
    else:
        results.append(_layout_write(home, target.relative_to(home).as_posix(), text,
                                     data_root=data_root))
    return results


def render_layout(conn, data_root, *, as_of, home_root=None, only=None,
                  force_readme=False, allow_external_home=False,
                  viewer_citizen_id=None) -> dict:
    """渲染整座门面（§4.6）。返回 §3.4 的形状 + `failed`（写盘失败逐条列，供 exit 1）。

    七类产物：五目录骨架、五个 README、记忆索引 / 最近、知识库登记投影、
    系统三页、项目状态表（B2 接入前是诚实占位页）、渲染清单。
    **看板不在本函数的职责里**：看板由 `board_render` 写，且它自带 `now()` 语义
    （在场/闲置判定），混进来会让"同一 as-of 两次渲染字节全等"当场失效（R8）。
    """
    data_root = Path(data_root)
    home = resolve_home_root(data_root, override=home_root, conn=conn,
                             allow_external_home=allow_external_home)
    only_set = _layout_only_set(only)
    written, skipped, degraded, failed = [], [], [], []
    # R17：未知 scope 不再打断整条 migrate 链——跳过该行并把原因记进 degraded
    changed_boards = _ensure_boards_home_paths(conn, degraded=degraded)

    def want(rel) -> bool:
        return only_set is None or rel.split("/")[0] in only_set

    def attempt(rel, fn):
        """单步渲染：失败按"写盘失败 / 数据源降级"两档归类（R20 · 评审 🟡-5）。

        为什么不是只捕 `OSError`：原实现除 `OSError` 外的异常会**从 render_layout 逃出**，
        与设计 §4.6「异常按源降级而不中断」相反。现在：
          · `OSError` / `sqlite3.Error` → `failed`（这一步真的没写成，exit 1）；
          · 其余异常 → `degraded`（该源降级，其余产物照写）；
          · 子渲染器自己标了 `degraded=True` → 汇总进 `degraded`（CLI 要能打印"降级"行）。
        """
        try:
            res = fn()
        except (OSError, sqlite3.Error) as exc:
            failed.append({"path": rel, "reason": _exc_text(exc)})
            return None
        except Exception as exc:            # noqa: BLE001 —— 设计 §4.6：按源降级，不中断
            degraded.append({"source": rel, "reason": _exc_text(exc)})
            return None
        if res is not None:
            written.append(res)
            if res.get("degraded") and res.get("path"):
                degraded.append({"source": res["path"],
                                 "reason": "数据源不可用（文件里已写明降级段）"})
        return res

    # ⑦ 目录骨架：五个一级目录（先建，后面每一步都不用自己判断父目录）
    for name in LAYOUT_TOP:
        try:
            (home / name).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            failed.append({"path": name, "reason": _exc_text(exc)})
    # 10-项目库/<prefix>/ 骨架：只 mkdir，不渲染看板（看板有自己的命令）
    try:
        prefixes = [r[0] for r in conn.execute(
            "SELECT prefix FROM code_registry ORDER BY prefix")]
    except sqlite3.Error as exc:
        prefixes = []
        degraded.append({"source": "code_registry", "reason": _exc_text(exc)})
    for prefix in prefixes:
        name = str(prefix or "")
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            continue
        try:
            (home / "10-项目库" / name).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            failed.append({"path": f"10-项目库/{name}", "reason": _exc_text(exc)})

    # ① 项目状态表（B2 的唯一产物入口。T03 起由 render_project_status 接管：
    #    六列 + 每格来源 + 三种 `—` + 脚注 + retired 折叠区）
    if want(LAYOUT_STATUS_TABLE):
        attempt(LAYOUT_STATUS_TABLE,
                lambda: render_project_status(conn, data_root, home, as_of=as_of))

    # ②③ 记忆索引 / 最近（HOME-3：只有索引，没有正文副本）
    if want(f"{LAYOUT_MEMORY_DIR}/"):
        attempt(_memory_index_relpath(as_of),
                lambda: render_memory_index(conn, home, as_of=as_of,
                                            data_root=data_root,
                                            viewer_citizen_id=viewer_citizen_id))
        attempt(f"{LAYOUT_MEMORY_DIR}/最近.md",
                lambda: render_memory_recent(conn, home, as_of=as_of,
                                             data_root=data_root,
                                             viewer_citizen_id=viewer_citizen_id))

    # ④ 知识库登记投影
    if want("30-知识库/"):
        attempt("30-知识库/vaults.md",
                lambda: _render_vaults(conn, home, as_of=as_of,
                                       data_root=data_root))

    # ⑥ 五个一级 README（存在且未 force_readme → 跳过，裁决 C5）
    for name in LAYOUT_TOP:
        rel = f"{name}/README.md"
        if not want(rel):
            continue
        try:
            res = render_readme(home, name, as_of=as_of, force=force_readme,
                                data_root=data_root)
        except (OSError, sqlite3.Error) as exc:
            failed.append({"path": rel, "reason": _exc_text(exc)})
            continue
        if res is None:
            skipped.append({"path": rel, "reason": "已存在（--force-readme 才覆盖）"})
        else:
            written.append(res)

    # ⑤ 90-系统 三页（凭证自检在 render_system_pages 内做）
    if want(f"{LAYOUT_SYSTEM_DIR}/"):
        inventory = list(written) + list(skipped)
        try:
            sys_results = render_system_pages(conn, data_root, home, as_of=as_of,
                                              written=inventory)
        except (OSError, sqlite3.Error) as exc:
            failed.append({"path": LAYOUT_SYSTEM_DIR, "reason": _exc_text(exc)})
            sys_results = []
        for res in sys_results:
            if res.get("blocked"):
                degraded.append({
                    "source": f"{LAYOUT_SYSTEM_DIR}/{Path(res['path']).name}",
                    "reason": "命中凭证形态（" + "、".join(res.get("patterns") or []) +
                              "），已拒绝落盘（HOME-8）"})
            else:
                written.append(res)

    return {"as_of": iso_local(as_of), "home_root": str(home),
            "written": written, "skipped": skipped, "degraded": degraded,
            "failed": failed, "changed_boards": changed_boards}


def _markdown_links(text: str) -> list:
    """提取 markdown 链接目标；跳过外链 / 锚点 / mailto（HOME-5 的链接检查用）。"""
    out = []
    for raw in _MD_LINK_RE.findall(text or ""):
        target = raw.strip().strip("<>").strip()
        if not target:
            continue
        if target.lower().startswith(("http://", "https://", "mailto:", "#")):
            continue
        out.append(target)
    return out


def _is_board_link(target) -> bool:
    """链接目标是不是一块看板（`看板-<pk>.md` / 分片 `看板-<pk>-<n>.md`）。

    状态表里的这类链接缺失属"今天没开工"，是 warning 不是 error——
    与普通文档链接（首页链接、正文链接）分级不同。
    """
    return Path(str(target).split("#", 1)[0]).name.startswith("看板-")


def _link_target_ok(src_file, target):
    """按"相对当前文件"解析链接；返回 `(ok, 说明)`。"""
    path_part = target.split("#", 1)[0]
    if not path_part:
        return True, "锚点"
    candidate = Path(path_part.replace("/", os.sep))
    if candidate.is_absolute():
        return False, "绝对路径链接不可移植"
    resolved = (Path(src_file).parent / candidate)
    if resolved.exists():
        return True, "ok"
    return False, "目标不存在"


def _iter_home_files(home):
    """遍历门面文件，返回 `[(相对路径, 绝对路径)]`（排序固定：确定性）。"""
    out = []
    for dirpath, dirnames, filenames in os.walk(home):
        dirnames.sort()
        for name in sorted(filenames):
            p = Path(dirpath) / name
            out.append((p.relative_to(home), p))
    return out


def _body_hashes(data_root) -> dict:
    """`data_root/projects/**/*.md` 的 sha256 → 相对路径（HOME-3 的第二重判据）。"""
    out = {}
    base = Path(data_root) / "projects"
    if not base.is_dir():
        return out
    for p in sorted(base.rglob("*.md")):
        try:
            out.setdefault(kb_sha256_file(p), p.relative_to(data_root).as_posix())
        except OSError:
            continue
    return out


def verify_layout(conn, data_root, *, home_root=None,
                  allow_external_home=False) -> dict:
    """门面体检（§4.7）。**只读**：不建目录、不改文件、不修数据。

    11 类检查各有独立 code（判据认 code 不认文案）：
      top_dir_missing / top_dir_extra / readme_missing / readme_element_missing /
      depth_exceeded / orphan_file / link_broken / memory_body_copy / credential_like /
      degraded_source / board_entry_missing / stale_render（后四类中前两类为 error、
      后两类为 warning；`stale_render` 与看板/事件时间有关，属 warning）。
    """
    data_root = Path(data_root)
    home = resolve_home_root(data_root, override=home_root, conn=conn,
                             allow_external_home=allow_external_home)
    items = []
    seen = set()

    def add(level, code, path, message, fix):
        key = (level, code, str(path))
        if key in seen:
            return
        seen.add(key)
        items.append({"level": level, "code": code, "path": str(path),
                      "message": message, "fix": fix})

    if not home.is_dir():
        for name in LAYOUT_TOP:
            add("error", "top_dir_missing", name,
                "门面根不存在（或该一级目录缺失）",
                "运行 `fish layout render --all` 重建门面")
        return _verify_result(home, items)

    # ① 一级目录集合（缺失 / 多余 / 越界条目）
    for name in LAYOUT_TOP:
        if not (home / name).is_dir():
            add("error", "top_dir_missing", name, "一级目录缺失",
                "运行 `fish layout render --all`（缺目录不会自动建）")
    for entry in sorted(home.iterdir(), key=lambda p: p.name):
        if entry.name in LAYOUT_TOP:
            continue
        if entry.is_dir():
            add("error", "top_dir_extra", entry.name,
                "多余的一级目录（只允许五个一级目录）",
                "删掉它，或把它移进五个目录之一")
        else:
            add("error", "orphan_file", entry.name,
                "一级目录下不允许直接放文件",
                "把文件移进五个一级目录之一，或删掉")

    # ② README 存在性 + 四要素
    for name in LAYOUT_TOP:
        rel = f"{name}/README.md"
        if not (home / name).is_dir():
            continue
        p = home / rel
        if not p.exists():
            add("error", "readme_missing", rel, "该一级目录没有 README.md",
                "重新渲染；若你删过，`fish layout render --all` 会补回")
            continue
        missing = _readme_missing_elements(_read_text(p))
        if missing:
            add("error", "readme_element_missing", rel,
                "README 缺要素：" + "、".join(missing),
                "补齐四要素：`## 这是什么` / `## 能做什么`（含“不能”）/ `## 谁在写` / "
                "`生成时刻：` + `手改会被覆盖`")

    files = _iter_home_files(home)

    # ③ 深度（>3 层即 error；`10-项目库/<项目>/看板-261026.md` 恰好 3 层，是上限不是超限）
    for rel, _p in files:
        if len(rel.parts) > LAYOUT_MAX_DEPTH:
            add("error", "depth_exceeded", rel.as_posix(),
                f"文件深度 {len(rel.parts)} > {LAYOUT_MAX_DEPTH}",
                "把文件上移；分支看板用一级 `_分支-<名>`，不要用 `_分支/<名>` 两级")

    # ④ 首页 / 状态表的相对链接
    # 状态表里的**看板入口**单独走 ⑨ 的 warning（board_entry_missing）：
    # "今天没人开工"是合法状态，不该把整份体检升级成 error。其余链接仍按 error 判。
    for rel in ("00-首页/README.md", LAYOUT_STATUS_TABLE):
        p = home / rel
        if not p.exists():
            continue
        for target in _markdown_links(_read_text(p)):
            if rel == LAYOUT_STATUS_TABLE and _is_board_link(target):
                continue
            ok, why = _link_target_ok(p, target)
            if not ok:
                add("error", "link_broken", rel,
                    f"链接目标不存在：{target}（{why}）",
                    "改链接，或先把目标页渲染出来")
        if rel == LAYOUT_STATUS_TABLE:
            # 状态表里的看板入口：今天没人开工是合法状态 → 只记 warning
            for target in _markdown_links(_read_text(p)):
                if not _is_board_link(target):
                    continue
                ok, _why = _link_target_ok(p, target)
                if not ok:
                    add("warning", "board_entry_missing", rel,
                        f"看板入口目标不存在：{target}",
                        "当天没开工属正常；否则跑 `fish board render --all`")

    # ⑤ 20-记忆库：**双重判据**（正文命名形态 + sha256 交集）
    mem_dir = home / LAYOUT_MEMORY_DIR
    if mem_dir.is_dir():
        body_hashes = _body_hashes(data_root)
        for p in sorted(mem_dir.rglob("*.md")):
            rel = p.relative_to(home).as_posix()
            if MEMORY_BODY_NAME_RE.match(p.name):
                add("error", "memory_body_copy", rel,
                    "文件名是正文形态（`编码__标题.md`）——门面禁止正文副本",
                    "删掉它；正文只在 `data_root/projects/**`")
                continue
            try:
                digest = kb_sha256_file(p)
            except OSError:
                continue
            if digest in body_hashes:
                add("error", "memory_body_copy", rel,
                    f"内容与正文 `{body_hashes[digest]}` 完全相同（sha256 交集）",
                    "删掉副本；要引用正文请用索引里的相对路径")

    # ⑥ 90-系统：凭证形态扫描（HOME-8）
    sys_dir = home / LAYOUT_SYSTEM_DIR
    if sys_dir.is_dir():
        for p in sorted(sys_dir.rglob("*")):
            if not p.is_file():
                continue
            hits = _scan_credentials(_read_text(p))
            if hits:
                add("error", "credential_like", p.relative_to(home).as_posix(),
                    "命中凭证形态：" + "、".join(hits),
                    "删掉凭证；凭证只放人的凭证文档与环境变量，永不进门面")

    # ⑦ 数据源降级段（HOME-9：降级要显式，不能看起来像"没有数据"）
    for rel, p in files:
        if p.suffix.lower() not in (".md", ".txt"):
            continue
        if "数据源不可用：" in _read_text(p):
            add("warning", "degraded_source", rel.as_posix(),
                "产物含『数据源不可用』段（是读不到，不是 0）",
                "修好数据源后重跑 `fish layout render --all`")

    # ⑧ as-of 新鲜度（产物早于数据最后变更 / 今天已有看板但门面还是旧的）
    as_of_values = []
    for rel, p in files:
        if p.suffix.lower() != ".md":
            continue
        m = _AS_OF_RE.search(_read_text(p))
        if m:
            try:
                as_of_values.append((rel, _parse_as_of(m.group(1))))
            except ValueError:
                continue
    if as_of_values:
        newest_rel, newest_dt = max(as_of_values, key=lambda t: t[1])
        try:
            row = conn.execute("SELECT MAX(ts) FROM events").fetchone()
            max_events = row[0] if row else None
        except sqlite3.Error as exc:
            max_events = None
            add("warning", "degraded_source", "(db)",
                f"读 `events.ts` 失败：{_exc_text(exc)}",
                "确认 `data_root/index.sqlite` 可读")
        if max_events:
            try:
                events_dt = datetime.datetime.strptime(
                    max_events, "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=datetime.timezone.utc)
                if newest_dt < events_dt:
                    add("warning", "stale_render", newest_rel,
                        f"门面 as-of（{iso_local(newest_dt)}）早于最后一件事（{max_events}）",
                        "重跑 `fish layout render --all`（不带 --as-of）")
            except ValueError as exc:
                warn_ignored('fish.py:13139 module', exc)
        today_key = datetime.datetime.now().strftime("%y%m%d")
        board_today = [rel for rel, p in files if p.name == f"看板-{today_key}.md"]
        if board_today and newest_dt.date() < datetime.date.today():
            add("warning", "stale_render", board_today[0],
                "今天已有看板文件，但门面的 as-of 早于今天",
                "重跑 `fish layout render --all`（不带 --as-of）")

    return _verify_result(home, items)


def _verify_result(home, items) -> dict:
    errors = sum(1 for it in items if it["level"] == "error")
    warnings = sum(1 for it in items if it["level"] == "warning")
    return {"home_root": str(home), "as_of": iso_local(),
            "errors": errors, "warnings": warnings, "items": items}


def cmd_layout(args):
    """B1 门面 CLI：`render --all` / `verify`（§3.2）。

    退出码：0 成功（含降级段）/ 1 域错误（verify 有 error、渲染有目录写失败）/ 2 用法错误。
    **新命令族用 `raise SystemExit(code)` 表达非零**——`main()` 丢弃返回值（既有缺陷），
    `return 1` 在这里等于退出 0（§3.1 / R9）。
    """
    action = getattr(args, "layout_action", None)
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    data_root = Path(args.root)
    try:
        if action == "render":
            if not getattr(args, "render_all", False):
                print("错误：`layout render` 必须带 `--all`（本批只支持全量渲染；"
                      "`--only` 是 P2）。", file=sys.stderr)
                raise SystemExit(2)
            as_of = _parse_as_of_arg(getattr(args, "as_of", None))
            try:
                res = render_layout(conn, data_root, as_of=as_of,
                                    home_root=getattr(args, "home", None),
                                    force_readme=getattr(args, "force_readme", False),
                                    allow_external_home=bool(
                                        getattr(args, "allow_external_home", False)))
            except RuntimeError as exc:          # 门面根非法 / 写盘越界
                print(f"错误：{exc}", file=sys.stderr)
                raise SystemExit(1)
            for it in res["written"]:
                print(f"{it['path']}\t{it['bytes']}\t{it['sha256'][:12]}")
            for it in res["skipped"]:
                print(f"跳过\t{it['path']}\t{it['reason']}")
            for it in res["degraded"]:
                print(f"降级\t{it['source']}\t{it['reason']}")
            for it in res["failed"]:
                print(f"失败\t{it['path']}\t{it['reason']}", file=sys.stderr)
            if res["changed_boards"]:
                print(f"看板落点已归一 {len(res['changed_boards'])} 行（boards.path）")
            print(f"改了 {len(res['written'])} 个文件 / 跳过 {len(res['skipped'])} 个已存在的 README")
            print(f"门面根：{res['home_root']}")
            print(f"重建命令：fish layout render --all --as-of {res['as_of']}")
            if res["failed"]:
                raise SystemExit(1)
            return 0

        if action == "verify":
            try:
                res = verify_layout(conn, data_root,
                                    home_root=getattr(args, "home", None),
                                    allow_external_home=bool(
                                        getattr(args, "allow_external_home", False)))
            except RuntimeError as exc:
                print(f"错误：{exc}", file=sys.stderr)
                raise SystemExit(1)
            if getattr(args, "json", False):
                print(json.dumps(res, ensure_ascii=False, indent=2))
            else:
                for it in res["items"]:
                    print(f"[{it['level']}] {it['code']} {it['path']} —— "
                          f"{it['message']}；修法：{it['fix']}")
                print(f"共 {res['errors']} 条 error / {res['warnings']} 条 warning")
            if res["errors"]:
                raise SystemExit(1)
            return 0

        print(f"错误：未知 layout 子命令：{action!r}", file=sys.stderr)
        raise SystemExit(2)
    finally:
        conn.close()


# ---------------------------------------------------------------- 5.3 项目状态表（B2）
# 依据：架构设计 §4.8 + 裁决 Q4/Q5/Q6/Q7。这一段的全部纪律可以压成三句：
#   ① **每一格都带来源**（`sources`），来源进 `--json`、进人读脚注——"为什么是 —" 必须能解释；
#   ② **一切时间走 `as_of`**（窗口、当日看板、最近 7 天），否则"同一 as-of 两次渲染全等"不成立；
#   ③ **读不到就说读不到**（`—（数据源不可用）` + degraded 段），绝不用 0 / 空串冒充。
# 数据源共五张表：code_registry（有哪些项目）/ code_index（最近活跃、未闭合、回退 owner）/
# boards（看板入口）/ project_stints（开档期 → 主责 AI）。


def _registry_rows(conn) -> list:
    """`code_registry` 的行（项目清单的**唯一**来源，裁决 Q6）。

    排序刻意不是按字母：人看状态表的第一眼要的是"还在跑的"，其次是"做完了"，最后是
    "退役的"。`status` 的未知取值排最后（不隐藏，脚注会说明"未登记状态"）。
    """
    rows = conn.execute(
        "SELECT prefix, namespace, status FROM code_registry "
        "ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'closed' THEN 1 "
        "WHEN 'retired' THEN 2 ELSE 3 END, prefix").fetchall()
    return [{"prefix": r[0], "namespace": r[1], "status": r[2]} for r in rows]


def _pst_local_date(ts, tzinfo):
    """"UTC+Z 时间戳" → `as-of` 时区里的 `YYYY-MM-DD`。

    为什么不直接 `str(ts)[:10]`：数据库时间戳是 UTC，`2026-10-25T00:00:00+08:00`
    落库是 `2026-10-24T16:00:00Z`——截前 10 位会把它显示成前一天。人读的"最近活跃"
    必须与 as-of 行用同一个时区，否则同一份表里两处时间互相打脸。
    """
    raw = str(ts or "")
    try:
        dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw[:10]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(tzinfo).strftime("%Y-%m-%d")


def _last_active_of(conn, prefix, *, as_of=None):
    """项目的"最近活跃"：`max(code_index.recorded_at) WHERE project=?` → `(YYYY-MM-DD, 来源)`。

    为什么加 `recorded_at <= as_of`：as-of 冻结的本意是"在那一刻看到的世界"。
    少了这个上界，冻结一个过去时间点却能看到之后写的记忆——幂等判据仍绿，
    但"历史快照"这个语义就是假的。
    """
    sql = ("SELECT MAX(recorded_at) FROM code_index WHERE project=? AND dormant=0")
    params = [prefix]
    if as_of is not None:
        sql += " AND recorded_at<=?"
        params.append(_as_of_utc_z(as_of))
    row = conn.execute(sql, params).fetchone()
    value = row[0] if row else None
    if not value:
        return PST_DASH_NO_MEMORY, "code_index.recorded_at(无记录)"
    tzinfo = as_of.tzinfo if as_of is not None else datetime.timezone.utc
    return _pst_local_date(value, tzinfo), "code_index.recorded_at(max)"


def _owners_of(conn, prefix, *, as_of):
    """主责 AI（裁决 Q4）→ `(显示名串, 来源)`。

    ① **开档期优先**：`project_stints WHERE project=? AND to_ts IS NULL` → display_name。
       多名时列前 2 + `等 N 人`（N = 该项目的**总人数**，"等 3 人"= 一共 3 人）。
    ② 无开档期 → 回退"最近 `PST_OWNER_FALLBACK_DAYS` 天内该项目记忆的 owner"，
       来源标 `recent_owner`（**标来源**是 Q4 的硬要求：回退值必须看得出来是回退的）。
    ③ 两者都无 → `—（无开档期）`。

    为什么显示名走 `_display_name_of` 而不在这里新写一条 citizens 查询：
    `test_identity_p1a::test_G_existing_queries_untouched` 规定"新增函数里的 citizens
    引用必须显式声明"，而本批不允许改那个测试文件。复用既有读点既守住纪律，
    又保证状态表与 whoami 对"谁是谁"给出同一个答案。
    """
    rows = conn.execute(
        "SELECT citizen_id, MIN(from_ts) AS first FROM project_stints "
        "WHERE project=? AND to_ts IS NULL GROUP BY citizen_id "
        "ORDER BY first ASC, citizen_id ASC", (prefix,)).fetchall()
    names = []
    for (cid, _first) in rows:
        try:
            display = _display_name_of(conn, cid)
        except sqlite3.Error:
            display = None
        names.append((display or "").strip() or _short_citizen_id(cid))
    if names:
        if len(names) == 1:
            return names[0], "project_stints(开档期)"
        return "、".join(names[:2]) + f"等 {len(names)} 人", "project_stints(开档期)"

    since = _as_of_utc_z(as_of - datetime.timedelta(days=PST_OWNER_FALLBACK_DAYS))
    rows = conn.execute(
        "SELECT owner, MAX(recorded_at) AS last FROM code_index "
        "WHERE project=? AND recorded_at>=? AND owner IS NOT NULL AND TRIM(owner)<>'' "
        "GROUP BY owner ORDER BY last DESC, owner ASC LIMIT 2", (prefix, since)).fetchall()
    names = []
    for (cid, _last) in rows:
        try:
            display = _display_name_of(conn, cid)
        except sqlite3.Error:
            display = None
        names.append((display or "").strip() or _short_citizen_id(cid))
    if names:
        return "、".join(names), "code_index.owner(recent_owner)"
    return PST_DASH_NO_OWNER, "none"


def _open_loops_of(conn, prefix, *, as_of):
    """未了事项（该项目在 `PST_OPEN_WINDOW_DAYS` 窗口内命中 8 词词表的条数）→ `(n, 来源)`。

    计数口径与注入包的 A 类信号**同源**（同一个 `_open_loop_rows`、同一个窗口常量），
    只是这里加 `project=` 过滤、把 limit 拉到计数上限。复制一份 SQL 就会出现
    "注入包说有 3 条、状态表说 2 条"这种最难解释的分歧。
    """
    since = _as_of_utc_z(as_of - datetime.timedelta(days=PST_OPEN_WINDOW_DAYS))
    # 探一次表：`_open_loop_rows` 对 `sqlite3.Error` 是**静默返回空列表**（注入包的容错，
    # 改它会动 467 条既有判据）。但状态表不能把"表坏了"显示成"未了事项 0 条"——
    # 那正是本节开头第③条纪律要防的。所以在这里显式探一次，让异常冒到按列降级。
    conn.execute("SELECT 1 FROM code_index LIMIT 1", ()).fetchall()
    rows = _open_loop_rows(conn, since, 10000, project=prefix)
    return len(rows), "code_index.title(8 词表)"


def _non_prefix_dirs(conn, data_root) -> dict:
    """正文目录里"不属于任何 registry 前缀"的一级目录（裁决 Q6/Q18：只计数、不列表）。

    为什么只计数：`_retired-ANYI-*` 这类目录是历史遗留，逐条列出来会把主表淹掉；
    但**静默吞掉**又违反"诚实"——所以脚注给一个数字，想看的人自己去目录里看。
    """
    base = Path(data_root) / "projects"
    if not base.is_dir():
        return {"count": 0, "names": []}
    try:
        prefixes = {r[0] for r in conn.execute("SELECT prefix FROM code_registry")}
    except sqlite3.Error:
        return {"count": 0, "names": []}
    extra = sorted(p.name for p in base.iterdir()
                   if p.is_dir() and p.name not in prefixes)
    return {"count": len(extra), "names": extra}


def _pst_status_label(status) -> str:
    """状态 → 人读标签；未知取值原样显示（脚注会说明"未登记状态"）。"""
    return {"active": "进行中", "closed": "已闭合", "retired": "已退役"}.get(
        str(status or ""), str(status or "") or "—")


def _pst_row_skeleton(prefix, namespace, status) -> dict:
    return {
        "prefix": prefix,
        "namespace": namespace,
        "status": status,
        "status_label": _pst_status_label(status),
        "last_active": None,
        "last_active_source": None,
        "board_entry": None,
        "board_entry_source": None,
        "owners": None,
        "owners_source": None,
        "open_loops": None,
        "open_loops_source": None,
        "sources": {},
    }


def project_status_rows(conn, data_root, *, as_of, home_root=None,
                        include_retired=False) -> dict:
    """六列 + 每列来源 + 降级清单（§3.4 / §4.8）。**只读**：不改任何表、不写盘。

    返回 `{"as_of","home_root","columns","rows","retired","degraded","footnotes",
    "retired_total","non_prefix_dirs"}`。`rows` 里每行的字段名与 `--json` 完全一致
    （K1：同一形状），渲染器只做字符串拼装，不再二次读库。

    降级的粒度是**列**而不是整表：某一张表的列坏了（比如旧库缺 `dormant`），
    只该让那一格变 `—（数据源不可用）`，其余五格照样有值——否则一次局部故障
    会让整张表看起来像"没有项目"。
    """
    data_root = Path(data_root)
    if home_root is None:
        home_root = resolve_home_root(data_root, conn=conn)
    degraded = []
    rows = []

    def _degrade(source, exc):
        degraded.append({"source": source, "reason": _exc_text(exc)})

    try:
        registry = _registry_rows(conn)
    except sqlite3.Error as exc:
        _degrade("code_registry", exc)
        registry = []

    for item in registry:
        prefix = str(item.get("prefix") or "")
        namespace = str(item.get("namespace") or "")
        status = str(item.get("status") or "")
        row = _pst_row_skeleton(prefix, namespace, status)
        if status not in ("active", "closed", "retired"):
            row["status_label"] = f"{status or '—'}（未登记状态）"

        for key, fn in (("last_active", lambda: _last_active_of(conn, prefix, as_of=as_of)),
                        ("board_entry", lambda: _board_entry_of(
                            conn, prefix, as_of, home_root=home_root)),
                        ("owners", lambda: _owners_of(conn, prefix, as_of=as_of)),
                        ("open_loops", lambda: _open_loops_of(
                            conn, prefix, as_of=as_of))):
            try:
                value, source = fn()
            except sqlite3.Error as exc:
                _degrade(f"{key}:{prefix}", exc)
                row[key] = PST_DASH_UNAVAILABLE
                row[f"{key}_source"] = "unavailable"
                row["sources"][key] = "unavailable"
                continue
            row[key] = value
            row[f"{key}_source"] = source
            row["sources"][key] = source
        row["sources"]["status"] = "code_registry.status"
        rows.append(row)

    retired_all = [r for r in rows if r["status"] == "retired"]
    if include_retired:
        visible = rows
        retired = []              # 已放进主表 → 折叠区不再重复列（否则同一行出现两次）
    else:
        visible = [r for r in rows if r["status"] != "retired"]
        retired = retired_all

    try:
        non_prefix = _non_prefix_dirs(conn, data_root)
    except OSError as exc:                 # 目录不可读不是 SQL 错，单独兜一次
        _degrade("data_root/projects", exc)
        non_prefix = {"count": 0, "names": []}

    keywords = "、".join(ATTENTION_OPEN_KEYWORDS)
    footnotes = [
        f"窗口：最近活跃与未了事项都只看 `as-of` 前 {PST_OPEN_WINDOW_DAYS} 天"
        f"（与注入包的 `ATTENTION_OPEN_WINDOW_DAYS` 同口径）。",
        "`—（无记忆）` = 该项目在 `code_index` 里没有任何 active 记忆；"
        "`—（今日无看板）` = 今日没有开过板（**不等于**没人干活，可能只是没签到）；"
        "`—（无开档期）` = 既无开档期、也没有近 7 天的记忆；`—` = 未了事项为 0。",
        f"主责 AI 口径（Q4）：① 该项目当前开档期（`project_stints.to_ts IS NULL`）的公民，"
        f"多名列前 2 + 「等 N 人」；② 无开档期时回退近 {PST_OWNER_FALLBACK_DAYS} 天内"
        f"该项目记忆的 owner，并在来源里标 `recent_owner`；③ 都无 → `—（无开档期）`。",
        f"未了事项词表（{len(ATTENTION_OPEN_KEYWORDS)} 个）：{keywords}。",
        "项目清单以 `code_registry` 为准（裁决 Q6）。",
        f"正文目录里不属于任何前缀的目录：{non_prefix['count']} 个"
        + (f"（{', '.join(non_prefix['names'][:5])}"
           + ("…" if non_prefix["count"] > 5 else "") + "）" if non_prefix["count"] else "")
        + "——只计数不列表（裁决 Q6/Q18）。",
        f"本页是投影：真相在 `data_root/index.sqlite`，重建命令 `fish project status`；"
        f"同一 `--as-of` 重渲染逐字节一致（as-of 行除外）。",
    ]

    return {"as_of": iso_local(as_of), "home_root": str(Path(home_root)),
            "columns": list(PST_COLUMNS), "rows": visible, "retired": retired,
            "retired_total": len(retired_all),
            "degraded": degraded, "footnotes": footnotes,
            "non_prefix_dirs": non_prefix}


def _pst_cell(value) -> str:
    """状态表单元格：`None`/空串 → `—`；0 条未了事项 → `—`（显式，不写 0）。"""
    if value is None or value == "":
        return PST_DASH_NONE
    return _cell(value)


def _pst_render_table(rows) -> list:
    lines = ["| " + " | ".join(PST_COLUMNS) + " |",
             "|" + "---|" * len(PST_COLUMNS)]
    if not rows:
        lines.append("| —（registry 里没有项目） | — | — | — | — | — |")
        return lines
    for row in rows:
        name = _cell(row["namespace"] or row["prefix"]) or "（未命名）"
        if row["namespace"]:
            name = f"{name}（`{_cell(row['prefix'])}`）"
        else:
            name = f"`{_cell(row['prefix'])}`"
        loops = row.get("open_loops")
        loops_cell = PST_DASH_NONE if not loops else str(loops)
        if loops is None:
            loops_cell = PST_DASH_UNAVAILABLE
        lines.append(
            f"| {name} | {_cell(row['status_label']) or PST_DASH_NONE} "
            f"| {_pst_cell(row.get('last_active'))} "
            f"| {row.get('board_entry') or PST_DASH_NO_BOARD} "
            f"| {_pst_cell(row.get('owners'))} "
            f"| {loops_cell} |")
    return lines


def render_project_status(conn, data_root, home_root, *, as_of,
                          include_retired=False) -> dict:
    """把状态表渲染到 `<home_root>/10-项目库/项目状态表.md`（B2 的唯一产物入口）。

    结构固定：标题 → as-of/生成器 → 六列表 → 降级段（有才写）→ 脚注 → retired 折叠区。
    **同一 as-of 两次渲染逐字节全等**：本函数的所有输入都来自 `as_of` 与库内容，
    不写 `now()`、不写 `ms`、不写随机数。
    """
    home = Path(home_root)
    res = project_status_rows(conn, data_root, as_of=as_of, home_root=home,
                              include_retired=include_retired)
    lines = [
        "# 项目状态表",
        "",
        f"> 生成器：`fish project status` ｜ as-of {res['as_of']}",
        "> 每格的取值来源见 `--json` 的 `sources`；本页是投影，真相在 "
        "`data_root/index.sqlite`。",
        "> 六列严格顺序：项目 / 状态 / 最近活跃 / 看板入口 / 主责 AI / 未了事项。",
        "",
    ]
    lines += _pst_render_table(res["rows"])
    lines += ["", f"主表 {len(res['rows'])} 个项目"
              + (f"（含 retired，`--include-retired`）" if include_retired else "")]
    if res["degraded"]:
        for item in res["degraded"]:
            lines += ["", f"## 数据源不可用：{item['source']}", "",
                      f"原因：`{item['reason']}`",
                      "该格显示 `—（数据源不可用）`，**不是 0**。修好数据源后重跑 "
                      "`fish project status`。"]
    lines += ["", "## 脚注", ""]
    lines += [f"- {note}" for note in res["footnotes"]]
    retired = res["retired"]
    if retired:
        lines += ["", f"<details><summary>已退役（{len(retired)}）</summary>", ""]
        lines += _pst_render_table(retired)
        lines += ["", "</details>", ""]
    lines += [""]
    out = _layout_write(home, LAYOUT_STATUS_TABLE, "\n".join(lines),
                        data_root=data_root)
    out["rows"] = len(res["rows"])
    out["retired"] = len(retired)
    out["degraded"] = res["degraded"]
    return out


def cmd_project_status(args):
    """B2 项目状态表 CLI（§3.2 `fish project status`）。

    契约要点：`--json` **不写文件**（只打印同一形状的 JSON）；`retired` 默认折进文末；
    退出码 0/1/2（数据库不可用=1、`--as-of` 解析失败=2）。

    为什么非零用 `raise SystemExit`：`main()` 结尾是 `args.func(args)`，返回值被丢弃——
    `return 1` 今天等于退出 0（§3.1 / R9）。这是既有缺陷，本批不修 `main()`。
    """
    data_root = Path(args.root)
    try:
        conn = connect(data_root)
        conn.executescript(SCHEMA)
        migrate(conn)
    except sqlite3.Error as exc:
        print(f"错误：数据库不可用：{exc}", file=sys.stderr)
        raise SystemExit(1)
    try:
        as_of = _parse_as_of_arg(getattr(args, "as_of", None))
        include_retired = bool(getattr(args, "include_retired", False))
        home_override = getattr(args, "home", None)
        try:
            home = resolve_home_root(
                data_root, override=home_override, conn=conn,
                allow_external_home=bool(getattr(args, "allow_external_home", False)))
        except RuntimeError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            raise SystemExit(1)
        if getattr(args, "json", False):
            res = project_status_rows(conn, data_root, as_of=as_of, home_root=home,
                                      include_retired=include_retired)
            payload = {"as_of": res["as_of"], "home_root": res["home_root"],
                       "columns": res["columns"], "rows": res["rows"],
                       "retired": res["retired"], "degraded": res["degraded"],
                       "footnotes": res["footnotes"],
                       "retired_total": res["retired_total"]}
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        out = render_project_status(conn, data_root, home, as_of=as_of,
                                    include_retired=include_retired)
        print(f"{out['path']}\t{out['bytes']}\t{out['sha256'][:12]}")
        print(f"主表 {out['rows']} 行 / 退役 {out['retired']} 行"
              f"（retired 默认折进文末折叠区；`--include-retired` 可放进主表）")
        for item in out["degraded"]:
            print(f"降级\t{item['source']}\t{item['reason']}")
        print(f"重建命令：fish project status --as-of {iso_local(as_of)}")
        return 0
    finally:
        conn.close()

def cmd_ui(args):
    """`fish ui serve|dump` —— 只读读口的 CLI 入口（2026-10-28 从生产树移植）。

    真正的实现在 `fish_ui.py`（独立模块，可单独 `python fish_ui.py` 跑，便于调试）。
    这里只做三件事：定位库、把 CLI 参数翻译成它的参数、调它。
    **不在这里写任何查询**——读口的形状只有一份，避免 CLI 与独立运行两条路漂移。
    """
    try:
        import fish_ui
    except ImportError as exc:  # pragma: no cover - 只在缺文件时触发
        print(f"[fish ui] 找不到 fish_ui.py（应与 fish.py 同目录）：{exc}", file=sys.stderr)
        return 2

    # root 指向**数据目录**（不是项目根）——fish 的 connect(Path(root)) 就是拿它当 data 目录用。
    db = os.path.join(str(Path(args.root)), "index.sqlite")
    action = getattr(args, "ui_action", "serve")
    argv = ["--db", db, "--minutes", str(getattr(args, "minutes", 30))]
    if action == "dump":
        argv.append("--once")
    else:
        argv += ["--host", getattr(args, "host", "127.0.0.1"),
                 "--port", str(getattr(args, "port", 8787))]
    return fish_ui.main(argv)


def cmd_kb(args):
    """B3 知识库 CLI（§3.2）：vault / tree / open / search / write / rollback / stats / doctor / backup。

    退出码（K9）：0 成功（含离线降级）/ 1 域错误（拒绝/越界/超限/编码/备份缺失）/ 2 用法错误。
    **新命令族用 `raise SystemExit(code)`**：`main()` 丢弃返回值，`return 1` 等于退出 0。
    """
    data_root = Path(args.root)
    try:
        conn = connect(data_root)
        conn.executescript(SCHEMA)
        migrate(conn)
    except sqlite3.Error as exc:
        print(f"错误：数据库不可用：{exc}", file=sys.stderr)
        raise SystemExit(1)
    try:
        act = getattr(args, "kb_action", None)
        try:
            if act == "vault":
                return _kb_cmd_vault(conn, args)
            if act == "tree":
                return _kb_cmd_tree(conn, args)
            if act == "toc":
                return _kb_cmd_toc(conn, args)
            if act == "open":
                return _kb_cmd_open(conn, args)
            if act == "search":
                return _kb_cmd_search(conn, args)
            if act == "write":
                return _kb_cmd_write(conn, data_root, args)
            if act == "rollback":
                return _kb_cmd_rollback(conn, data_root, args)
            if act == "stats":
                return _kb_cmd_stats(conn, args)
            if act == "doctor":
                return _kb_cmd_doctor(conn, data_root, args)
            if act == "backup":
                return _kb_cmd_backup(conn, data_root, args)
        except KBError as exc:
            print(f"错误[{exc.code}]：{exc}", file=sys.stderr)
            raise SystemExit(_kb_exit_code(exc))
        raise KBError("bad_usage", f"未知 kb 子命令：{act!r}")
    except KBError as exc:
        print(f"错误[{exc.code}]：{exc}", file=sys.stderr)
        raise SystemExit(_kb_exit_code(exc))
    except sqlite3.Error as exc:
        print(f"错误：数据库不可用：{exc}", file=sys.stderr)
        raise SystemExit(1)
    finally:
        conn.close()


# ------------------------------------------------------------------ CLI

def _archive_citizens_cmd(conn, args) -> int:
    """`fish archive --citizens [--days N] [--apply]`（FB-12）。

    默认 dry-run：归档是**搬行**（INSERT 冷表 + DELETE 热表），先看计划再执行。
    `--days` 缺省取 config（`session.archive_after_days`），而它的默认值就是 0
    ——"会话归档无限期"。0 不是"全归档"，是"不自动归档"，所以这里明说跳过。
    """
    days = args.days if getattr(args, "days", None) is not None \
        else int(get_config(conn, CITIZEN_ARCHIVE_DAYS_KEY))
    if int(days or 0) < 1:
        print("跳过（未配置）：session.archive_after_days = 0 —— 会话归档默认无限期。\n"
              "  要归档请给阈值：`fish archive --citizens --days N`"
              "（先演练，确认后加 --apply）；\n"
              "  或长期配置：`fish config set session.archive_after_days N`。")
        return 0
    res = archive_citizens(conn, days=int(days), dry_run=not args.apply)
    tag = "（演练，未真搬）" if res["dry_run"] else ""
    print(f"{tag}闲置 ≥{res['days']} 天的公民：可归档 {len(res['planned'])} 个"
          f"（截止 {res['cutoff']}；共检查 {res['checked']} 个）")
    for it in res["planned"]:
        print(f"  {it['display_name'] or '（无名）':<28} {it['citizen_id']}  "
              f"最后活跃 {it['last_seen_at']}")
    for it in res["skipped"][:10]:
        print(f"  跳过 {it['display_name'] or '（无名）'}（{it['citizen_id']}）：{it['why']}")
    if len(res["skipped"]) > 10:
        print(f"  … 共 {len(res['skipped'])} 个被跳过")
    if res["dry_run"]:
        print("（演练完毕：没有搬任何行。确认后加 --apply 执行。）")
    else:
        print(f"已归档 {res['archived_count']} 个："
              f"{', '.join(res['archived']) or '无'}")
    return 0


def cmd_archive(args):
    """归档 CLI：list / one / sweep。

    **它不改语义**：`status` 一个字不动，只搬文件 + 更新 `file_path`。
    所以推送、召回、注入包、邻近查询的行为完全不变（它们本来就看不见这些状态）。
    """
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    root = Path(args.root)
    try:
        act = args.action or "sweep"

        # 2026-10-26（FB-12）：`--citizens` 走**公民归档**（搬 citizens 行），
        # 与下面的正文归档（只搬文件、不删行）是两件事，用开关而不是新子命令：
        # 两者的默认行为都是"演练"，放同一个入口才不会有人以为 `archive` 只能归档正文。
        if getattr(args, "citizens", False):
            return _archive_citizens_cmd(conn, args)

        if act == "list":
            rows = conn.execute(
                "SELECT code, project, status, file_path FROM code_index "
                "WHERE status IN ('superseded','withdrawn') AND file_path IS NOT NULL "
                "ORDER BY project, code").fetchall()
            print(f"可归档 {len(rows)} 条（status=superseded/withdrawn，active 一条都不在其中）：")
            for code, proj, st, fp in rows:
                print(f"  {code:<26} {proj:<14} {st:<11} {fp}")
            return 0

        if act == "one":
            if not args.code:
                print("错误：one 需要给编码", file=sys.stderr)
                return 2
            r = archive_body(conn, root, args.code, year=args.year,
                             dry_run=args.dry_run)
            conn.commit()
            tag = "（演练）" if args.dry_run else "已归档"
            print(f"{tag}\t{r['code']}\n  {r['from']}\n→ {r['to']}")
            return 0

        res = archive_sweep(conn, root, include_withdrawn=args.withdrawn,
                            year=args.year, dry_run=args.dry_run,
                            limit=args.limit)
        tag = "（演练，未真移）" if args.dry_run else ""
        print(f"{tag}可归档 {res['total']} 条，移动 {res['moved']} 条"
              + (f"，已在归档区 {res['already']} 条" if res.get("already") else ""))
        for it in res["items"][:20]:
            print(f"  {it['code']:<26} → {it['to']}")
        if len(res["items"]) > 20:
            print(f"  … 共 {len(res['items'])} 条")
        for s in res["skipped"][:10]:
            print(f"  跳过 {s['code']}：{s['why']}")
        for e in res["errors"][:10]:
            print(f"  错误 {e['code']}：{e['why']}")
        return 0 if not res["errors"] else 1
    finally:
        conn.close()


# ====================================================== 主人画像 CLI（薄适配器）
#
# **实现全在 `fish_profile.run_cli`**（表现层跟着领域走——改画像的人只需要看一个文件）。
# 这里只做 `cmd_*` 都躲不开的三件事：建连接、落 SCHEMA、跑迁移。
#
# 写权分离（R1-2）体现在**接口面**：MCP 只有 `profile_show` / `profile_propose`，
# **没有写入口**；set/accept 这些只存在于 CLI。
def cmd_profile(args):
    conn = connect(Path(args.root))
    conn.executescript(SCHEMA)
    migrate(conn)
    try:
        return fish_profile.run_cli(args, conn, actor_fn=token_actor_citizen_id,
                                    config_fn=get_config)
    finally:
        conn.close()

# F-P1-10（2026-11-18 外部审查）：`main()` 与 argparse 装配已搬到 fish_cli.py。
# **必须在文件末尾 import**：fish_cli 里 `import fish`，而它需要的是一个属性
# 齐全的模块对象。放在这里，本文件的定义已全部完成，循环 import 因此成立。
import fish_cli as _fish_cli  # noqa: E402

_fish_cli.install(globals())   # 把自己的命名空间交给 CLI 层（dict，不传模块对象）
from fish_cli import main  # noqa: E402,F401

if __name__ == "__main__":
    main()
