#!/usr/bin/env node
/**
 * 双跑器 · 比对器：两侧产物规范化后逐字段 diff。
 *
 * 三种结果，一种都不许静默：
 *   · `MATCH`   —— 规范化后逐字段相等；
 *   · `DIFF`    —— 不相等；打印首个差异路径 + 两侧片段（超过 20 条截断）；
 *   · `PENDING` —— 某侧未实现；**只有** allowlist 里登记了 owner + 未过期才放过。
 *
 * `--self-test`：人为扰动一侧的规范化产物，断言比较器**能报红**。
 * 为什么必须有它：一个永远说"diff=0"的比较器与"真的相等"在外观上无法区分，
 * 而影子期的全部结论都建立在"diff 可信"之上。
 *
 * 用法：
 *   node test/conformance/diff.mjs               # 正常对拍
 *   node test/conformance/diff.mjs --self-test   # 比较器自证（必须报红）
 *   node test/conformance/diff.mjs --json
 */

import { existsSync, readdirSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { deepDiff, normalizeOutput } from './normalize.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const defaultTs = path.join(here, 'out', 'ts.json')
const defaultPy = path.join(here, 'out', 'py.json')
const allowlistPath = path.join(here, 'allowlist.json')

const argv = process.argv.slice(2)
const asJson = argv.includes('--json')
const selfTest = argv.includes('--self-test')
const pick = (flag) => {
  const index = argv.indexOf(flag)
  return index >= 0 ? path.resolve(argv[index + 1]) : null
}
const tsPath = pick('--ts') ?? defaultTs
const pyPath = pick('--py') ?? defaultPy

/** 读 JSON；缺文件 → 明确报错（"没跑过"与"跑失败"必须区分）。 */
function readJson(filePath, label) {
  if (!existsSync(filePath)) {
    throw new Error(`${label}产物不存在：${filePath}（先跑 run.mjs / run.py）`)
  }
  return JSON.parse(readFileSync(filePath, 'utf8'))
}

/** 读 allowlist；结构不合法直接抛（豁免表坏了比没有更危险）。 */
function loadAllowlist() {
  if (!existsSync(allowlistPath)) return { entries: [] }
  const parsed = JSON.parse(readFileSync(allowlistPath, 'utf8'))
  if (!Array.isArray(parsed.entries)) throw new Error('allowlist.json 缺少 entries[]')
  for (const entry of parsed.entries) {
    for (const key of ['fixture', 'op', 'side', 'kind', 'owner', 'expires']) {
      if (typeof entry[key] !== 'string' || entry[key] === '') {
        throw new Error(`allowlist 条目缺少 ${key}：${JSON.stringify(entry)}`)
      }
    }
  }
  return parsed
}

/** 按 fixture + side 找豁免条目；过期视为不存在（并回报过期这件事）。 */
function findAllowance(allowlist, fixtureId, side, today) {
  const entry = allowlist.entries.find((item) => item.fixture === fixtureId && item.side === side)
  if (entry === undefined) return { entry: null, expired: false }
  return { entry, expired: entry.expires < today }
}

const today = new Date().toISOString().slice(0, 10)

/** 依次比对两侧产物。 */
function compareSides(tsPayload, pyPayload, allowlist) {
  const byId = new Map()
  for (const item of tsPayload.fixtures) byId.set(item.id, { ts: item })
  for (const item of pyPayload.fixtures) {
    const entry = byId.get(item.id) ?? {}
    entry.py = item
    byId.set(item.id, entry)
  }

  const results = []
  for (const [id, sides] of byId) {
    const ts = sides.ts
    const py = sides.py
    if (ts === undefined || py === undefined) {
      results.push({ id, verdict: 'MISSING', detail: ts === undefined ? 'TS 侧缺该 fixture' : 'Python 侧缺该 fixture' })
      continue
    }
    if (ts.status === 'error' || py.status === 'error') {
      results.push({
        id,
        op: ts.op,
        verdict: 'ERROR',
        detail: `ts=${ts.status}${ts.reason ? `(${ts.reason})` : ''} py=${py.status}${py.reason ? `(${py.reason})` : ''}`,
      })
      continue
    }
    if (ts.status === 'pending' || py.status === 'pending') {
      const side = ts.status === 'pending' ? 'ts' : 'py'
      const { entry, expired } = findAllowance(allowlist, id, side, today)
      if (entry === null) {
        results.push({ id, op: ts.op, verdict: 'PENDING_UNALLOWED', detail: `${side} 侧未实现，且 allowlist 未登记` })
      } else if (expired) {
        results.push({
          id,
          op: ts.op,
          verdict: 'PENDING_EXPIRED',
          detail: `${side} 侧未实现，豁免已过期（${entry.expires}，owner=${entry.owner}）`,
        })
      } else {
        results.push({
          id,
          op: ts.op,
          verdict: 'PENDING',
          detail: `${side} 侧未实现（owner=${entry.owner}，到期=${entry.expires}）`,
        })
      }
      continue
    }

    const rules = fixtureRules(id)
    const tsNorm = normalizeOutput(ts.output, rules)
    const pyNorm = normalizeOutput(py.output, rules)
    const diff = deepDiff(pyNorm.value, tsNorm.value)
    results.push({
      id,
      op: ts.op,
      verdict: diff.equal ? 'MATCH' : 'DIFF',
      rules: rules,
      ruleHits: { ts: tsNorm.hits, py: pyNorm.hits },
      diffs: diff.diffs,
    })
  }
  return results
}

/** fixtures 里声明的规范化规则（两侧共用同一份 → 从 fixtures 读，不各写一份）。 */
const fixtureRulesCache = new Map()
function fixtureRules(fixtureId) {
  if (fixtureRulesCache.size === 0) {
    const dir = path.join(here, 'fixtures')
    if (existsSync(dir)) {
      for (const name of readdirSync(dir).filter((entry) => entry.endsWith('.json')).sort()) {
        try {
          const parsed = JSON.parse(readFileSync(path.join(dir, name), 'utf8'))
          if (typeof parsed.id === 'string' && Array.isArray(parsed.normalize)) {
            fixtureRulesCache.set(parsed.id, parsed.normalize)
          }
        } catch {
          /* fixture 坏了会在 run 阶段先报，这里静默跳过。 */
        }
      }
    }
  }
  return fixtureRulesCache.get(fixtureId) ?? []
}

/**
 * 比较器自证：把 Python 侧第一个 MATCH 的产物改坏一个字符，必须报 DIFF。
 * 没有可比较的 MATCH 时退回"用两份人造产物证明比较器能报红"。
 */
function runSelfTest(tsPayload, pyPayload, allowlist) {
  const checks = []
  const record = (id, ok, detail) => checks.push({ id, ok, detail })

  // 场景 1：真实对拍里是否存在可比的 fixture
  const comparable = tsPayload.fixtures.filter((item) => item.status === 'ok')
  record('selftest.has_comparable', comparable.length > 0, `TS 侧 ok 的 fixture=${comparable.length}`)

  // 场景 2：人造产物——完全相等时必须 MATCH
  const sample = { id: 'selftest-equal', op: 'synthetic', status: 'ok', output: { text: '甲乙丙' } }
  const equal = compareSides({ fixtures: [sample] }, { fixtures: [structuredClone(sample)] }, allowlist)
  record('selftest.equal_detected', equal[0]?.verdict === 'MATCH', `verdict=${equal[0]?.verdict}`)

  // 场景 3：人造产物——改一个字符必须 DIFF
  const perturbed = structuredClone(sample)
  perturbed.output = { text: '甲乙丁' }
  const differing = compareSides({ fixtures: [sample] }, { fixtures: [perturbed] }, allowlist)
  record('selftest.mutation_detected', differing[0]?.verdict === 'DIFF', `verdict=${differing[0]?.verdict}`)
  record(
    'selftest.mutation_path',
    differing[0]?.diffs?.[0]?.path === '$.text',
    `firstPath=${differing[0]?.diffs?.[0]?.path ?? '(none)'}`,
  )

  // 场景 4：规范化真的在起作用（时间戳差异必须被抹平 → MATCH）
  const tsText = { id: 'selftest-normalize', op: 'synthetic', status: 'ok', output: { text: 'as-of 2026-10-27T01:02:03Z' } }
  const pyText = { id: 'selftest-normalize', op: 'synthetic', status: 'ok', output: { text: 'as-of 2026-01-01T00:00:00Z' } }
  const rules = ['strip-iso-timestamp']
  const normTs = normalizeOutput(tsText.output, rules)
  const normPy = normalizeOutput(pyText.output, rules)
  const normDiff = deepDiff(normPy.value, normTs.value)
  record('selftest.normalize_masks_timestamp', normDiff.equal, `equal=${normDiff.equal} ts=${normTs.value.text}`)

  // 场景 5：未登记 pending 必须报 PENDING_UNALLOWED（豁免不是默认）
  const pending = { id: 'selftest-pending', op: 'synthetic', status: 'pending', reason: 'x' }
  const pendingVerdict = compareSides({ fixtures: [pending] }, { fixtures: [structuredClone(pending)] }, { entries: [] })
  record('selftest.pending_requires_allowlist', pendingVerdict[0]?.verdict === 'PENDING_UNALLOWED', `verdict=${pendingVerdict[0]?.verdict}`)

  return checks
}

let tsPayload
let pyPayload
let allowlist
try {
  tsPayload = readJson(tsPath, 'TS 侧')
  pyPayload = readJson(pyPath, 'Python 侧')
  allowlist = loadAllowlist()
} catch (error) {
  console.error(`diff 无法开始：${error.message}`)
  process.exit(1)
}

if (selfTest) {
  const checks = runSelfTest(tsPayload, pyPayload, allowlist)
  const failed = checks.filter((check) => !check.ok)
  if (asJson) {
    process.stdout.write(`${JSON.stringify({ selfTest: true, pass: failed.length === 0, checks }, null, 2)}\n`)
  } else {
    console.log('# diff --self-test（比较器自证：必须能报红）')
    for (const check of checks) console.log(`${check.ok ? 'PASS' : 'FAIL'}  ${check.id}  ${check.detail}`)
    console.log(`# summary: ${checks.length - failed.length}/${checks.length} pass`)
  }
  process.exitCode = failed.length === 0 ? 0 : 1
} else {
  const results = compareSides(tsPayload, pyPayload, allowlist)
  const counts = {}
  for (const result of results) counts[result.verdict] = (counts[result.verdict] ?? 0) + 1
  const bad = results.filter((r) => r.verdict !== 'MATCH' && r.verdict !== 'PENDING')

  if (asJson) {
    process.stdout.write(`${JSON.stringify({ counts, results }, null, 2)}\n`)
  } else {
    console.log('# diff（两侧规范化后逐字段比对）')
    for (const result of results) {
      const head = `${result.verdict.padEnd(17)} ${result.id}`
      console.log(`${head}  ${result.detail ?? ''}`)
      if (result.verdict === 'DIFF') {
        for (const item of result.diffs ?? []) {
          console.log(`      ${item.path}:  py=${JSON.stringify(item.expected)}  ts=${JSON.stringify(item.actual)}`)
        }
      }
    }
    const summary = Object.entries(counts)
      .map(([verdict, n]) => `${verdict}=${n}`)
      .join(' ')
    console.log(`# summary: ${summary}`)
    console.log(`# 结论：diff=${counts.DIFF ?? 0}  matched=${counts.MATCH ?? 0}  pending=${counts.PENDING ?? 0}`)
  }
  process.exitCode = bad.length === 0 ? 0 : 1
}
