/**
 * 配置读取（复刻 `p0/fish.py` 的 `CONFIG_DEFAULTS` + `get_config`，fish.py:92 / fish.py:1870）。
 *
 * 为什么要把整张键表搬过来而不是"用到哪个读哪个"：
 *   `get_config` 对**未知键**是抛 `KeyError` 的（`_key_type`，fish.py:1864）。如果 TS 侧只认
 *   自己用到的几个键，那么"库里有人写了一个 TS 不认识的键"与"键拼错了"在行为上就会分叉——
 *   一边响亮报错、一边静默回落默认值。键表是**契约**，不是实现细节。
 *
 * 为什么类型也从这张表来：`config.value` 是 TEXT，`"3"` 与 `3`、`"false"` 与 `false`
 * 全靠 type 列解释。类型推断写在调用处，必然出现"这里当 int、那里当 string"的漂移，
 * 而漂移的表现是**数字静默变成字符串**（比较永远不等，且不报错）。
 *
 * @module dsh-fish/domain/config
 */

import type { DatabaseSync } from 'node:sqlite'

import { FishError } from '../core/errors.ts'

/** 配置值的类型（与 `CONFIG_DEFAULTS` 的第二列一一对应）。 */
export type ConfigType = 'int' | 'float' | 'bool' | 'string' | 'enum'

/** 一条配置键的定义：默认值 + 类型 + 枚举可选值。 */
export interface ConfigKeyDef {
  readonly defaultValue: number | boolean | string
  readonly type: ConfigType
  readonly allowed?: readonly string[]
}

/**
 * 配置键表（**逐条对齐** `fish.py:92` 的 `CONFIG_DEFAULTS`）。
 *
 * 只收键名/默认值/类型三件事：`enum` 的可选值只在写入侧校验，读侧不需要它，
 * 但为了让"这张表就是契约"这件事完整，枚举键的 allowed 一并记录。
 */
export const CONFIG_DEFAULTS: Readonly<Record<string, ConfigKeyDef>> = {
  'neighbors.strategy': { defaultValue: 'mixed', type: 'enum', allowed: ['code', 'time', 'tag', 'type', 'mixed'] },
  'neighbors.n': { defaultValue: 3, type: 'int' },
  'neighbors.cross_project': { defaultValue: false, type: 'bool' },
  'neighbors.include_dormant': { defaultValue: false, type: 'bool' },
  timezone: { defaultValue: 'Asia/Shanghai', type: 'string' },
  'layout.home_root': { defaultValue: '', type: 'string' },
  'board.sweep_days': { defaultValue: 7, type: 'int' },
  'board.overlap_window_min': { defaultValue: 90, type: 'int' },
  'inject.budget_tokens': { defaultValue: 1600, type: 'int' },
  'inject.style': { defaultValue: 'gentle', type: 'enum', allowed: ['gentle', 'strong'] },
  'inject.rules_per_source': { defaultValue: 3, type: 'int' },
  'inject.board_max_ratio': { defaultValue: 0.25, type: 'float' },
  owner_citizen: { defaultValue: '', type: 'string' },
  default_citizen: { defaultValue: '', type: 'string' },
  actor_name: { defaultValue: '', type: 'string' },
  'decay.episode_days': { defaultValue: 21, type: 'int' },
  'decay.fact_days': { defaultValue: 60, type: 'int' },
  'decay.lesson_days': { defaultValue: 365, type: 'int' },
  'decay.artifact_days': { defaultValue: 180, type: 'int' },
  'decay.rule_days': { defaultValue: 0, type: 'int' },
  'decay.identity_days': { defaultValue: 0, type: 'int' },
  'decay.dormant_threshold': { defaultValue: 0.5, type: 'float' },
  'decay.archive_threshold': { defaultValue: 0.1, type: 'float' },
  'push.enabled': { defaultValue: true, type: 'bool' },
  'push.serendipity_ratio': { defaultValue: 0.1, type: 'float' },
  'push.open_budget_tokens': { defaultValue: 450, type: 'int' },
  'push.after_tool_budget_tokens': { defaultValue: 100, type: 'int' },
  'push.situational_budget_tokens': { defaultValue: 80, type: 'int' },
  'push.after_tool_every_n_calls': { defaultValue: 10, type: 'int' },
  'push.gap_window_days': { defaultValue: 14, type: 'int' },
  'push.gap_others_read_min': { defaultValue: 3, type: 'int' },
  'push.situational_window_min': { defaultValue: 10, type: 'int' },
  'push.gap_early_phase_ratio': { defaultValue: 0.3, type: 'float' },
  'push.outcome_window_hours': { defaultValue: 24, type: 'int' },
  // ── 自动在场 / 会话维度 / 撞车（2026-10-26 · T03/T05）──────────────────
  'board.auto_presence': { defaultValue: true, type: 'bool' },
  'board.presence_session_rows': { defaultValue: true, type: 'bool' },
  'board.active_window_sec': { defaultValue: 300, type: 'int' },
  'board.collision_window_min': { defaultValue: 30, type: 'int' },
  'board.collision_ask': { defaultValue: 'warn', type: 'enum', allowed: ['off', 'warn', 'ask'] },
  'board.collision_ask_cooldown_sec': { defaultValue: 60, type: 'int' },
  'board.collision_min_segments': { defaultValue: 2, type: 'int' },
  // ── 提示词递记忆（2026-10-26 · T04）────────────────────────────────────
  'push.prompt_enabled': { defaultValue: true, type: 'bool' },
  'push.prompt_budget_tokens': { defaultValue: 120, type: 'int' },
  'push.prompt_max_items': { defaultValue: 2, type: 'int' },
  'push.prompt_min_interval_sec': { defaultValue: 90, type: 'int' },
  'push.prompt_min_relevance': { defaultValue: 0.6, type: 'float' },
  'push.prompt_stale_floor': { defaultValue: 0.35, type: 'float' },
  'push.prompt_exclude_hours': { defaultValue: 24, type: 'int' },
  // ── 动作采集（2026-10-26 · hooks 深度利用 T02）───────────────────────────
  'activity.enabled': { defaultValue: true, type: 'bool' },
  'activity.retention_days': { defaultValue: 14, type: 'int' },
  'activity.aggregate_window_min': { defaultValue: 30, type: 'int' },
  'activity.snapshot_min_interval_sec': { defaultValue: 20, type: 'int' },
  'activity.snapshot_max_age_sec': { defaultValue: 3600, type: 'int' },
  'activity.presence_cache_min_interval_sec': { defaultValue: 20, type: 'int' },
  'session.token_ttl_hours': { defaultValue: 0, type: 'int' },
  'session.archive_after_days': { defaultValue: 0, type: 'int' },
}

/** 取键定义；未知键**响亮失败**（与 Python `KeyError` 同义，但带下一步）。 */
export function configKeyDef(key: string): ConfigKeyDef {
  const def = CONFIG_DEFAULTS[key]
  if (def === undefined) {
    throw new FishError('CONTRACT_INVALID', `未知配置键：${key}`, {
      hint: `可用键见 src/domain/config.ts 的 CONFIG_DEFAULTS（与 p0/fish.py 的 CONFIG_DEFAULTS 同表）`,
      details: { key },
    })
  }
  return def
}

/**
 * 读一个配置键（复刻 `get_config`，fish.py:1870）。
 *
 * 口径逐条对齐 Python：库里没有该行 → 默认值；`int` 走 `parseInt`、`float` 走 `parseFloat`、
 * `bool` 只在原值为字符串 `"true"` 时为真（Python 是 `raw == "true"`，**不是**真值判断）。
 */
export function getConfig(db: DatabaseSync, key: string): number | boolean | string {
  const def = configKeyDef(key)
  const row = db.prepare('SELECT value FROM config WHERE key=?').get(key) as { value: string } | undefined
  if (row === undefined) return def.defaultValue
  const raw = row.value
  switch (def.type) {
    case 'int': {
      const parsed = Number.parseInt(raw, 10)
      if (Number.isNaN(parsed)) {
        throw new FishError('CONTRACT_INVALID', `配置 ${key} 不是整数：${JSON.stringify(raw)}`, {
          hint: `fish config set ${key} <整数>`,
        })
      }
      return parsed
    }
    case 'float': {
      const parsed = Number.parseFloat(raw)
      if (Number.isNaN(parsed)) {
        throw new FishError('CONTRACT_INVALID', `配置 ${key} 不是浮点数：${JSON.stringify(raw)}`, {
          hint: `fish config set ${key} <小数>`,
        })
      }
      return parsed
    }
    case 'bool':
      return raw === 'true'
    default:
      return raw
  }
}

/** 读整数配置（带范围夹紧的调用方自己夹，这里只保证类型）。 */
export function getConfigInt(db: DatabaseSync, key: string): number {
  const value = getConfig(db, key)
  return typeof value === 'number' ? Math.trunc(value) : Number.parseInt(String(value), 10)
}

/** 读浮点配置。 */
export function getConfigFloat(db: DatabaseSync, key: string): number {
  const value = getConfig(db, key)
  return typeof value === 'number' ? value : Number.parseFloat(String(value))
}

/** 读布尔配置。 */
export function getConfigBool(db: DatabaseSync, key: string): boolean {
  return getConfig(db, key) === true
}

/** 读字符串配置。 */
export function getConfigString(db: DatabaseSync, key: string): string {
  const value = getConfig(db, key)
  return typeof value === 'string' ? value : String(value)
}

/** 全部 `decay.*` 键的现值（复刻 `load_decay_cfg`，fish.py:6198）。 */
export function loadDecayConfig(db: DatabaseSync): Record<string, number> {
  const out: Record<string, number> = {}
  for (const key of Object.keys(CONFIG_DEFAULTS)) {
    if (!key.startsWith('decay.')) continue
    out[key] = getConfigFloat(db, key)
  }
  return out
}
