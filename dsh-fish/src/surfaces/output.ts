/**
 * 命令面的**回显收尾**：截断 + 落盘 + 路径回显。
 *
 * 为什么截断必须回显路径：斜杠命令的返回值会整段进对话区，一条 `health` 在断链几百条时
 * 会把上下文吃掉一大块；但**"看不到"与"没有"必须能区分**，所以超限时一定要给出
 * "完整输出在哪"。
 *
 * 文件名用**内容哈希**而不是时间戳：同一份输出连跑两次要得到逐字节相同的文本
 * （`/fish hosts dry-run` 的幂等判据就靠这个），时间戳会把幂等变成"每次都不同"。
 *
 * @module dsh-fish/surfaces/output
 */

import { createHash } from 'node:crypto'
import { mkdirSync, writeFileSync } from 'node:fs'
import path from 'node:path'

/** 命令输出超过这个行数就落盘，只在回显里给路径（命令面不该刷屏）。 */
export const OUTPUT_LINE_LIMIT = 60

/** 落盘目录与文件名规则（内容哈希 → 同一份输出永远是同一个文件名）。 */
export function outputLogPath(packageRoot: string, label: string, text: string): string {
  const digest = createHash('sha256').update(text).digest('hex').slice(0, 8)
  return path.join(path.resolve(packageRoot), '.tmp', `fish-${label}-${digest}.log`)
}

/**
 * 截断过长的命令输出（超过 `OUTPUT_LINE_LIMIT` 行就落盘）。
 * @returns `text` 回显文本（超限时带截断说明与完整路径）、`logPath` 落盘路径或 `null`
 */
export function finalizeOutput(text: string, label: string, packageRoot: string): { text: string; logPath: string | null } {
  const lines = text.split('\n')
  if (lines.length <= OUTPUT_LINE_LIMIT) return { text, logPath: null }
  const logPath = outputLogPath(packageRoot, label, text)
  mkdirSync(path.dirname(logPath), { recursive: true })
  writeFileSync(logPath, text, 'utf8')
  const head = lines.slice(0, OUTPUT_LINE_LIMIT).join('\n')
  return {
    text: `${head}\n…（共 ${lines.length} 行，已截断到 ${OUTPUT_LINE_LIMIT} 行；完整输出：${logPath}）`,
    logPath,
  }
}

/** 给较长回显加一行来源说明（命令面必须自解释：这段是从哪个库、哪个口径读出来的）。 */
export function withHeader(header: string, body: string): string {
  return `${header}\n${body}`
}
