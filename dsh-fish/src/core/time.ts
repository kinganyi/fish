/**
 * 时间原语：**库里用的 UTC 与输出层用的本地时区，是两个口径**。
 *
 * 从 `fish_common` 抄过来的三条规则（不是自己发明的）：
 *   · 库里的时间戳一律 `2026-10-27T04:06:43Z`（`nowUtc` / `utcStamp`）——事实层；
 *   · 给人看的 as-of 一律本地时区带偏移（`isoLocal`）——输出层；
 *   · 无时区的入参按**本地时区**补（不是 UTC）：`--as-of 2026-10-26T00:00:00` 是人手写
 *     的最常见形态，把它当 UTC 会让"今天"整体偏 8 小时。
 *
 * 为什么单独成模块：知识库 / 项目状态表 / 语料层三处都要这两条格式化，各写一份的
 * 结局是"同一份回执里两个时间互相打脸"（T05 起三处共用这一份）。
 *
 * @module dsh-fish/core/time
 */

/** 两位补零。 */
function pad(value: number, width = 2): string {
  return String(value).padStart(width, '0')
}

/** 库内时间戳（UTC + `Z`，与 Python `now_utc()` 同形）。 */
export function utcStamp(date: Date = new Date()): string {
  return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}T${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}Z`
}

/**
 * 本地时区的 ISO8601（`2026-10-26T02:05:11+08:00`），与 Python `iso_local()` 同形。
 *
 * 为什么不用 `Date.toISOString()`：那个是 UTC + 毫秒。as-of 是**输出层**的东西，
 * 读者看到的必须是自己的时区；而毫秒在两侧一定会差，逐字节对拍永远过不去。
 */
export function isoLocal(date: Date = new Date()): string {
  const offsetMinutes = -date.getTimezoneOffset()
  const sign = offsetMinutes >= 0 ? '+' : '-'
  const absolute = Math.abs(offsetMinutes)
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}` +
    `${sign}${pad(Math.trunc(absolute / 60))}:${pad(absolute % 60)}`
  )
}

/**
 * 解析一个时间入参：接受 `…Z` / `…+08:00` / 无时区（按本地时区补）。
 * @returns 解析成功返回 `Date`；失败返回 `null`（调用方决定怎么报错，不在这里猜）
 */
export function parseStamp(raw: string | null | undefined): Date | null {
  const text = (raw ?? '').trim()
  if (text === '') return null
  const normalized = text.endsWith('Z') ? `${text.slice(0, -1)}+00:00` : text
  // 无时区（`2026-10-26T00:00:00`）：JS 的 Date 会按**本地**时区解释，正好是 Python 的口径。
  const parsed = new Date(normalized)
  return Number.isNaN(parsed.getTime()) ? null : parsed
}

/**
 * 从时间串里取出**它自己声明的**时区偏移（分钟，东为正）；无偏移 → `null`。
 *
 * 为什么要它：`--as-of 2026-10-26T00:00:00+08:00` 冻结的是"北京时间的零点"，
 * 而 `Date` 只保留瞬时值、丢掉偏移。项目状态表的"最近活跃"与"看板周期键"都要在
 * **as-of 自己的时区**里算（Python 的 `astimezone(tzinfo)` 就是这个口径），
 * 丢掉偏移会让同一份表里两处时间互相打脸（差 8 小时正好跨天）。
 */
export function offsetMinutesOf(raw: string | null | undefined): number | null {
  const text = (raw ?? '').trim()
  if (text === '') return null
  if (text.endsWith('Z') || text.endsWith('z')) return 0
  const match = /([+-])(\d{2}):?(\d{2})$/u.exec(text)
  if (match === null) return null
  const sign = match[1] === '-' ? -1 : 1
  return sign * (Number(match[2]) * 60 + Number(match[3]))
}

/** 按**给定偏移**渲染 ISO8601（`2026-10-26T00:00:00+08:00`）。 */
export function isoWithOffset(date: Date, offsetMinutes: number): string {
  const shifted = new Date(date.getTime() + offsetMinutes * 60000)
  const sign = offsetMinutes >= 0 ? '+' : '-'
  const absolute = Math.abs(offsetMinutes)
  return (
    `${shifted.getUTCFullYear()}-${pad(shifted.getUTCMonth() + 1)}-${pad(shifted.getUTCDate())}` +
    `T${pad(shifted.getUTCHours())}:${pad(shifted.getUTCMinutes())}:${pad(shifted.getUTCSeconds())}` +
    `${sign}${pad(Math.trunc(absolute / 60))}:${pad(absolute % 60)}`
  )
}

/** 按**给定偏移**取日期部分（`YYYY-MM-DD`）与周期键（`YYMMDD`）。 */
export function datePartsInOffset(
  date: Date,
  offsetMinutes: number,
): { readonly year: number; readonly month: number; readonly day: number; readonly ymd: string; readonly period: string } {
  const shifted = new Date(date.getTime() + offsetMinutes * 60000)
  const year = shifted.getUTCFullYear()
  const month = shifted.getUTCMonth() + 1
  const day = shifted.getUTCDate()
  return {
    year,
    month,
    day,
    ymd: `${year}-${pad(month)}-${pad(day)}`,
    period: `${pad(year % 100)}${pad(month)}${pad(day)}`,
  }
}

