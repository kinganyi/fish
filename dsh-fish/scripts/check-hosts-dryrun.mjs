#!/usr/bin/env node
/**
 * 判据⑤：**hooks dry-run 幂等 + 零写入**（外加与 Python 安装器读数的对拍）。
 *
 * 三件事必须同时成立，缺一件这条判据就是装饰：
 *
 *   1. **幂等**：同一份配置连跑两次，输出**逐字节相同**（sha256 相等）。
 *      输出里若混进时间戳，这条永远红——所以渲染层刻意不含时刻（见 `hosts/dryrun.ts`）。
 *   2. **零写入**：两次都跑完，目标配置文件 `sha256` 与开始时相同。
 *      这一段跑的是**真配置**（用户正在用的 `hooks.json` / `settings.json`），只读。
 *   3. **diff 不是空转**：在 `--fixtures` 模式下用临时家目录伪造两份"配置与目标不一致"
 *      的配置（少了 PostToolUse、多了别人的 `Stop`），断言 diff 非空、并且
 *      **别人的事件在计划里被原样保留**。真实配置恰好已与目标一致（diff 为空），
 *      只跑真实配置就永远证明了"diff 引擎能工作"。
 *
 * 另外与生产树对拍（否则 TS 的 matcher 判据可能与 `install_hooks.py` 悄悄漂开）：
 * 拿 `scripts/probe/dump-hooks-plan.py` 输出的**结构化事实**逐项比对，不比文案。
 *
 * 用法：
 *   FISH_HOME=… node scripts/check-hosts-dryrun.mjs            # 只跑只读判据（真实配置）
 *   FISH_HOME=… node scripts/check-hosts-dryrun.mjs --fixtures # 额外跑"有差异"的夹具
 */

import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const packageRoot = path.resolve(here, '..')
const libPath = path.join(packageRoot, 'lib', 'index.js')
const envHome = (process.env.FISH_HOME ?? '').trim()
const fishHome = envHome !== '' ? path.resolve(envHome) : path.resolve(packageRoot, '..', 'p0')
const pythonBin = process.env.FISH_PYTHON || 'python'
const withFixtures = process.argv.includes('--fixtures')

if (!existsSync(libPath)) {
  console.error(`找不到 ${libPath}：先跑 pnpm run build`)
  process.exit(1)
}

const lib = await import(pathToFileURL(libPath).href)
const checks = []
const record = (id, ok, detail) => {
  checks.push({ id, ok, detail })
  return ok
}

const sha256 = (text) => createHash('sha256').update(text).digest('hex')
const sha256File = (filePath) => {
  try {
    return createHash('sha256').update(readFileSync(filePath)).digest('hex')
  } catch {
    return null
  }
}

/** 跑一次 Python 计划探针（返回原始 payload；失败返回 `{error}`）。 */
function runPythonPlan() {
  const script = path.join(packageRoot, 'scripts', 'probe', 'dump-hooks-plan.py')
  const child = spawnSync(pythonBin, [script], {
    encoding: 'utf8',
    timeout: 20000,
    maxBuffer: 32 * 1024 * 1024,
    windowsHide: true,
    env: { ...process.env, FISH_HOME: fishHome, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
  })
  if (child.error !== undefined) return { error: `python 启动失败：${child.error.message}` }
  if (child.status !== 0) return { error: `dump-hooks-plan.py 退出码 ${child.status}：${(child.stderr || '').slice(0, 300)}` }
  try {
    return { payload: JSON.parse(child.stdout) }
  } catch (error) {
    return { error: `输出不是 JSON：${error.message}` }
  }
}

/**
 * 与 Python 读数对拍（**结构化字段**，不比自然语言）。
 * 比的是：声明了哪些事件 / fish 条目数 / 采集器数 / PreToolUse 组数 / 每条 matcher 的方言判定。
 */
function compareWithPython(label) {
  const python = runPythonPlan()
  if (python.error !== undefined) {
    record(`hosts.${label}.conformance`, false, `无法对拍：${python.error}`)
    return
  }
  const specs = lib.resolveHostSpecs()
  const probes = lib.probeAllHosts(specs, fishHome)
  const problems = []
  for (const host of python.payload.hosts) {
    const probe = probes.find((item) => item.id === host.host)
    if (probe === undefined) {
      problems.push(`${host.host}: TS 侧没有这个宿主`)
      continue
    }
    const facts = host.facts ?? {}
    const tsEvents = [...probe.requiredEventsPresent].sort().join(',')
    const pyEvents = [...(facts.requiredEventsPresent ?? [])].sort().join(',')
    if (tsEvents !== pyEvents) problems.push(`${host.host}: 事件集 ${tsEvents} vs ${pyEvents}`)
    if (probe.fishEntries.length !== facts.fishEntryCount) {
      problems.push(`${host.host}: fish 条目 ${probe.fishEntries.length} vs ${facts.fishEntryCount}`)
    }
    const tsCollectors = probe.fishEntries.filter((entry) => entry.collector).length
    if (tsCollectors !== facts.collectorCount) problems.push(`${host.host}: 采集器 ${tsCollectors} vs ${facts.collectorCount}`)
    const tsPre = probe.fishEntries.filter((entry) => entry.event === 'PreToolUse').length
    if (tsPre !== facts.preToolGroups) problems.push(`${host.host}: PreToolUse 组 ${tsPre} vs ${facts.preToolGroups}`)
    for (const matcher of host.matchers ?? []) {
      const raw = matcher.matcher === null || matcher.matcher === undefined ? null : String(matcher.matcher)
      const ts = lib.matcherDialect(raw)
      if (ts.kind !== matcher.kind) {
        problems.push(
          `${host.host}: matcher 方言判定不一致（${String(raw).slice(0, 30)}…）TS=${ts.kind || '非法'} Python=${matcher.kind || '非法'}`,
        )
      }
    }
  }
  record(
    `hosts.${label}.conformance`,
    problems.length === 0,
    problems.length === 0
      ? `与 p0/install_hooks.py 逐项一致（${python.payload.hosts.length} 宿主：事件集 / fish 条目 / 采集器 / PreToolUse 组 / matcher 方言）`
      : problems.join(' | '),
  )
}

/** 跑两次 dry-run，返回 {first, second, textSha, idempotent, unchanged, hosts}. */
function runTwice(label) {
  const specs = lib.resolveHostSpecs()
  const configPaths = specs.map((spec) => spec.configPath)
  const before = configPaths.map((configPath) => [configPath, sha256File(configPath)])
  const first = lib.runHooksDryRun(fishHome, { packageRoot, hostConfigPaths: configPaths })
  const text1 = lib.renderHostsDryRun(first)
  const second = lib.runHooksDryRun(fishHome, { packageRoot, hostConfigPaths: configPaths })
  const text2 = lib.renderHostsDryRun(second)
  const after = configPaths.map((configPath) => [configPath, sha256File(configPath)])
  const unchanged = before.every(([configPath, digest]) => digest === after.find(([p]) => p === configPath)?.[1])
  record(
    `hosts.${label}.idempotent`,
    sha256(text1) === sha256(text2),
    `两次输出 sha256 ${sha256(text1).slice(0, 12)} / ${sha256(text2).slice(0, 12)}（planSha=${first.planSha256.slice(0, 12)}）`,
  )
  record(
    `hosts.${label}.zero_write`,
    unchanged && first.writeAttempts === 0 && first.hosts.every((host) => host.targetUnchanged),
    unchanged
      ? `目标文件 sha256 前后相同（${configPaths.map((p) => `${path.basename(p)}=${(sha256File(p) ?? '').slice(0, 12)}`).join(' ')}）；writeAttempts=${first.writeAttempts}`
      : `目标文件被改动：${before.map(([p, d]) => `${p} ${d?.slice(0, 12)}→${sha256File(p)?.slice(0, 12)}`).join('; ')}`,
  )
  return { first, text1 }
}

try {
  // ---- 1) 真实配置：只读 + 幂等 ----
  const real = runTwice('real')
  compareWithPython('real')

  // ---- 2) 夹具：配置与目标不一致，证明 diff 引擎不是空转 ----
  if (withFixtures) {
    const fixtureRoot = mkdtempSync(path.join(os.tmpdir(), 'fish-hooks-fixture-'))
    const originalEnv = { DSH_HOME: process.env.DSH_HOME, WORKBUDDY_HOME: process.env.WORKBUDDY_HOME }
    try {
      const dshHome = path.join(fixtureRoot, 'dsh')
      const wbHome = path.join(fixtureRoot, 'workbuddy')
      mkdirSync(dshHome, { recursive: true })
      mkdirSync(wbHome, { recursive: true })
      // DSH：少了 PostToolUse、多了别人的 Stop / Notification，且 PreToolUse matcher 是坏的（锚定正则）。
      const dshDoc = {
        _comment: 'fixture',
        hooks: {
          SessionStart: [{ hooks: [{ type: 'command', command: `python "${path.join(fishHome, 'fish_dsh_hook.py')}"`, timeout: 25 }] }],
          UserPromptSubmit: [{ hooks: [{ type: 'command', command: `python "${path.join(fishHome, 'fish_prompt_hook.py')}" --host dsh`, timeout: 10 }] }],
          PreToolUse: [{ matcher: '^(Write|write|Edit|edit)$', hooks: [{ type: 'command', command: `python "${path.join(fishHome, 'fish_pretool_hook.py')}" --host dsh`, timeout: 10 }] }],
          Stop: [{ hooks: [{ type: 'command', command: 'node "C:/other/plugin/stop.js"' }] }],
          Notification: [{ hooks: [{ type: 'command', command: 'echo notified' }] }],
        },
      }
      writeFileSync(path.join(dshHome, 'hooks.json'), `${JSON.stringify(dshDoc, null, 2)}\r\n`, 'utf8')
      // WorkBuddy：空配置（会新建），且带一个非 hooks 的顶层键（必须保留）。
      const wbDoc = { sandbox: { mode: 'workspace-write' }, hooks: {} }
      writeFileSync(path.join(wbHome, 'settings.json'), `${JSON.stringify(wbDoc, null, 2)}\n`, 'utf8')
      process.env.DSH_HOME = dshHome
      process.env.WORKBUDDY_HOME = wbHome
      // 夹具文件的"跑之前"摘要（跑完必须逐字节不变——夹具是可控的，这条比真实配置更硬）。
      const dshShaBefore = sha256File(path.join(dshHome, 'hooks.json'))
      const wbShaBefore = sha256File(path.join(wbHome, 'settings.json'))

      const fixture = runTwice('fixtures')
      compareWithPython('fixtures')
      const dshPlan = fixture.first.hosts.find((host) => host.host === 'dsh')
      const wbPlan = fixture.first.hosts.find((host) => host.host === 'workbuddy')
      record(
        'hosts.fixtures.diff_not_vacuous',
        (dshPlan?.diffLines ?? 0) > 0 && (wbPlan?.diffLines ?? 0) > 0,
        `dsh diff ${dshPlan?.diffLines ?? 0} 行 / workbuddy diff ${wbPlan?.diffLines ?? 0} 行（真实配置那轮 diff 为 ${real.first.hosts.map((h) => h.diffLines).join('/')} 行）`,
      )
      record(
        'hosts.fixtures.others_preserved',
        (dshPlan?.preserved ?? []).includes('Stop') && (dshPlan?.preserved ?? []).includes('Notification') && (dshPlan?.dropped ?? []).length === 0,
        `dsh 计划里保留其它事件=${(dshPlan?.preserved ?? []).join(',') || '（无）'}；将被删除=${(dshPlan?.dropped ?? []).join(',') || '（无）'}`,
      )
      record(
        'hosts.fixtures.line_endings_kept',
        dshPlan?.lineEndings === 'CRLF' && wbPlan?.lineEndings === 'LF',
        `行尾：dsh=${dshPlan?.lineEndings} / workbuddy=${wbPlan?.lineEndings}（序列化必须按原文风格，否则 diff 变整文件重写）`,
      )
      // 夹具的写入自证：两个夹具文件字节没变。
      const dshShaAfter = sha256File(path.join(dshHome, 'hooks.json'))
      const wbShaAfter = sha256File(path.join(wbHome, 'settings.json'))
      record(
        'hosts.fixtures.zero_write_bytes',
        fixture.first.writeAttempts === 0 && dshShaBefore === dshShaAfter && wbShaBefore === wbShaAfter,
        `writeAttempts=${fixture.first.writeAttempts}；hooks.json ${dshShaBefore?.slice(0, 12)}→${dshShaAfter?.slice(0, 12)}；` +
          `settings.json ${wbShaBefore?.slice(0, 12)}→${wbShaAfter?.slice(0, 12)}`,
      )
    } finally {
      if (originalEnv.DSH_HOME === undefined) delete process.env.DSH_HOME
      else process.env.DSH_HOME = originalEnv.DSH_HOME
      if (originalEnv.WORKBUDDY_HOME === undefined) delete process.env.WORKBUDDY_HOME
      else process.env.WORKBUDDY_HOME = originalEnv.WORKBUDDY_HOME
      rmSync(fixtureRoot, { recursive: true, force: true })
    }
  }

  // ---- 3) 真实配置里的"写入次数"文案必须是 0（命令面回显自证）----
  record(
    'hosts.render_selfproof',
    real.text1.includes('写入次数：0') && real.text1.includes('不实现 apply'),
    `回显含"写入次数：0"与"不实现 apply"（${real.text1.split('\n').length} 行）`,
  )
} catch (error) {
  record('x.unexpected', false, error instanceof Error ? `${error.name}: ${error.message}` : String(error))
}

const failed = checks.filter((check) => !check.ok)
console.log('# check-hosts-dryrun')
for (const check of checks) console.log(`${check.ok ? 'PASS' : 'FAIL'}  ${check.id}  ${check.detail}`)
console.log(`# summary: ${checks.length - failed.length}/${checks.length} pass`)
process.exitCode = failed.length === 0 ? 0 : 1
