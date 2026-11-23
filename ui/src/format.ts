/**
 * 时间与数字的显示工具。
 *
 * 两条纪律：
 *  · 库里所有时间戳都是 UTC（`...Z`）——**按 UTC 日期分组、按本地时区显示**（93 §6-3 的坑）；
 *  · `null` 一律渲染成「判不到」，**绝不显示 0**（0 是读数，判不到是证据不足）。
 */

export const UNKNOWN = '判不到'

export function parseUtc(ts: string | null | undefined): Date | null {
  if (!ts) return null
  const date = new Date(ts)
  return Number.isNaN(date.getTime()) ? null : date
}

/** 本地时区的 `HH:mm:ss`（悬停展开/核对同分钟内先后时用）。 */
export function clockOf(ts: string | null | undefined): string {
  const date = parseUtc(ts)
  if (!date) return '--:--:--'
  return date.toLocaleTimeString('zh-CN', { hour12: false })
}

/** 本地时区的 `HH:mm`（时间列的**默认**显示：秒悬停才展开，见 `components/Time.tsx`）。 */
export function clockShort(ts: string | null | undefined): string {
  const date = parseUtc(ts)
  if (!date) return '--:--'
  const hh = String(date.getHours()).padStart(2, '0')
  const mm = String(date.getMinutes()).padStart(2, '0')
  return `${hh}:${mm}`
}

/** 本地时区的 `MM-DD HH:mm`（跨天列表用）。 */
export function stampOf(ts: string | null | undefined): string {
  const date = parseUtc(ts)
  if (!date) return UNKNOWN
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  const hh = String(date.getHours()).padStart(2, '0')
  const mm = String(date.getMinutes()).padStart(2, '0')
  return `${month}-${day} ${hh}:${mm}`
}

/** 本地时区的 `MM-DD HH:mm:ss`（`stampOf` 的悬停展开版）。 */
export function stampLongOf(ts: string | null | undefined): string {
  const date = parseUtc(ts)
  if (!date) return UNKNOWN
  const clock = clockOf(ts)
  return `${stampOf(ts).slice(0, 5)} ${clock}`
}

/** 相对时间：给左栏「最后一次被交互」用。 */
export function relativeOf(ts: string | null | undefined): string {
  const date = parseUtc(ts)
  if (!date) return UNKNOWN
  const seconds = Math.round((Date.now() - date.getTime()) / 1000)
  if (seconds < 0) return '刚刚'
  if (seconds < 60) return `${seconds} 秒前`
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`
  return `${Math.floor(seconds / 86400)} 天前`
}

export function percent(rate: number | null | undefined): string {
  if (rate === null || rate === undefined) return UNKNOWN
  return `${(rate * 100).toFixed(1)}%`
}

/** 数字读数：`null` → 判不到；非空 → 千分位。 */
export function num(value: number | null | undefined): string {
  if (value === null || value === undefined) return UNKNOWN
  return value.toLocaleString('zh-CN')
}
