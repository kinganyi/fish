import type { Win } from './api/types'

/**
 * 三个时间窗的显示名与查询口径 —— **唯一一张表**。
 *
 * 为什么要集中：窗口名会同时出现在状态行、流水页签、以及页签一每块 ⓘ 的「口径」里
 * （评审 R-09：ⓘ 只说表名不说时间窗，读者无法判断这个数字覆盖哪段时间）。
 * 三处各写一遍 `'近 24 小时'`，改窗口文案时必定漏一处。
 */
export const WIN_LABEL: Record<Win, string> = {
  '24h': '近 24 小时',
  '7d': '近 7 天',
  all: '全部时间',
}

/**
 * 窗口 → `/api/stats?days=`。
 *
 * 与流水窗口**刻意同步**：推送、热推、热搜三块的时间窗就是 `days`。不同步的话，
 * 用户把流水切到「近 7 天」，右栏 ⓘ 却还写着「近 24 小时」——那正是 R-09 说的问题。
 * `all` 取后端上限 30 天（`parse_int(...days, 1, 30)`），不假装真的是"从有数据起"。
 */
export const STATS_DAYS: Record<Win, number> = { '24h': 1, '7d': 7, all: 30 }

/** 页签一 ⓘ 的窗口名：前两档与流水同字，`all` 如实写「近 30 天」（days 的上限）。 */
export const STATS_WIN_LABEL: Record<Win, string> = {
  '24h': WIN_LABEL['24h'],
  '7d': WIN_LABEL['7d'],
  all: '近 30 天',
}
