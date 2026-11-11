#!/usr/bin/env node
/**
 * T04 变异自证（3 条）：**每一条都必须让对应判据变红，还原后必须复绿**。
 *
 * | # | 改坏什么 | 应红的判据 | 为什么这条值得证 |
 * |---|---|---|---|
 * | M01 | `spec/cli-map.json` 少一条叶子（并同步 counts 与摘要，避免摘要先红挡住判据） | `check-spec` 的 `spec.cli_map_covers_production` | "覆盖 100%"只有对着生产命令树才发现漏；产物自洽永远自洽 |
 * | M02 | 把写命令 `board checkin` 标成 `impl: ts` | `check-cli-surface` 的 `cli.write_commands_refused` | 写命令被标成 ts = 偷偷授予写权限，判据必须当场红 |
 * | M03 | dry-run 之后**顺手把计划写进目标文件** | `check-hosts-dryrun` 的 `hosts.*.zero_write` | 零写入承诺必须可证伪；M03 只在**夹具家目录**里跑，绝不碰真配置 |
 *
 * 三条纪律（T035 的教训）：
 *   1. **改坏 → 跑判据 → 还原 → 复跑** 四步齐全，缺"复绿"这一步的变异等于没做；
 *   2. 每段都在 `finally` 里还原，并在收尾**逐字节**核对文件已回原样；
 *   3. M03 动的是"会写文件"的代码——所以它把 `DSH_HOME`/`WORKBUDDY_HOME` 指到临时夹具，
 *      真配置从头到尾只被读过。
 *
 * 用法：`FISH_HOME=… node test/mutation/run-t04-mutations.mjs`
 */

import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const packageRoot = path.resolve(here, '..', '..')
const nodeBin = process.execPath
const tsdownBin = path.join(packageRoot, 'node_modules', 'tsdown', 'dist', 'run.mjs')

const envHome = (process.env.FISH_HOME ?? '').trim()
const fishHome = envHome !== '' ? path.resolve(envHome) : path.resolve(packageRoot, '..', 'p0')
const baseEnv = { ...process.env, FISH_HOME: fishHome, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' }

const sha = (buffer) => createHash('sha256').update(buffer).digest('hex')
const results = []

/** 跑一条命令，返回 {code, out}。 */
function run(script, args = [], env = baseEnv) {
  const child = spawnSync(nodeBin, [script, ...args], {
    cwd: packageRoot,
    encoding: 'utf8',
    env,
    maxBuffer: 32 * 1024 * 1024,
    timeout: 180000,
  })
  return { code: child.status ?? 1, out: `${child.stdout ?? ''}${child.stderr ?? ''}` }
}

/** 构建 lib（变异后必须重建，否则判据跑的还是旧产物）。 */
function build() {
  const child = spawnSync(nodeBin, [tsdownBin], { cwd: packageRoot, encoding: 'utf8', env: baseEnv, maxBuffer: 8 * 1024 * 1024 })
  if (child.status !== 0) throw new Error(`构建失败：${(child.stderr ?? '').slice(0, 500)}`)
}

/** 从 check 输出里取失败项 id。 */
function failedIds(output) {
  return output
    .split('\n')
    .filter((line) => line.startsWith('FAIL'))
    .map((line) => line.trim().split(/\s+/)[1])
}

/** 一条变异：改坏 → 判据红 → 还原 → 判据绿。 */
function mutate({ id, title, file, patch, restore, check, checkArgs = [], env = baseEnv, expectRed }) {
  const target = path.join(packageRoot, file)
  const original = readFileSync(target)
  let outcome = { id, title, ok: false, detail: '' }
  try {
    writeFileSync(target, patch(original.toString('utf8')), 'utf8')
    if (file.startsWith('src/')) build()
    const red = run(check, checkArgs, env)
    const redIds = failedIds(red.out)
    const hit = expectRed.every((expected) => redIds.some((failed) => failed.startsWith(expected)))
    outcome = {
      ...outcome,
      redExit: red.code,
      redIds,
      hit,
      detail: `变异后 exit=${red.code} 红项=[${redIds.join(', ') || '（无）'}]`,
    }
    outcome.ok = red.code !== 0 && hit
    if (!outcome.ok) throw new Error(`变异没有命中预期判据 ${expectRed.join(',')}（实际红项：${redIds.join(',') || '无'}）`)
  } finally {
    writeFileSync(target, restore ? restore(original) : original)
    if (file.startsWith('src/')) build()
  }
  const green = run(check, checkArgs, env)
  outcome.greenExit = green.code
  outcome.greenDetail = `还原后 exit=${green.code}`
  // 还原必须逐字节一致，否则"复绿"可能是靠别的改动凑出来的。
  outcome.restored = sha(readFileSync(target)) === sha(original)
  outcome.ok = outcome.ok && green.code === 0 && outcome.restored
  results.push(outcome)
  return outcome
}

// ---------------------------------------------------------------- M01
const specFile = path.join(packageRoot, 'spec', 'cli-map.json')
const contractFile = path.join(packageRoot, 'spec', 'contract.json')
const specOriginal = readFileSync(specFile)
const contractOriginal = readFileSync(contractFile)
try {
  const spec = JSON.parse(specOriginal.toString('utf8'))
  const dropped = spec.entries.find((entry) => entry.path === 'citizen-list')
  spec.entries = spec.entries.filter((entry) => entry.path !== 'citizen-list')
  spec.counts.leaves = spec.entries.length
  spec.implCounts['proxy-readonly'] = spec.entries.filter((entry) => entry.impl === 'proxy-readonly').length
  const specText = `${JSON.stringify(spec, null, 2)}\n`
  writeFileSync(specFile, specText, 'utf8')
  // 同步摘要：否则 `gate.spec_digests` 会先红，覆盖判据被挡在门外（T035 M03 的同一教训）。
  const contract = JSON.parse(contractOriginal.toString('utf8'))
  contract.specFiles['cli-map.json'] = sha(Buffer.from(specText, 'utf8'))
  writeFileSync(contractFile, `${JSON.stringify(contract, null, 2)}\n`, 'utf8')

  const red = run('scripts/check-spec.mjs')
  const redIds = failedIds(red.out)
  const hit = redIds.includes('spec.cli_map_covers_production')
  writeFileSync(specFile, specOriginal)
  writeFileSync(contractFile, contractOriginal)
  const green = run('scripts/check-spec.mjs')
  results.push({
    id: 'M01',
    title: `cli-map 漏一条（${dropped?.path ?? 'citizen-list'}）`,
    ok: red.code !== 0 && hit && green.code === 0,
    detail: `变异后 exit=${red.code} 红项=[${redIds.join(', ')}]；还原后 exit=${green.code}`,
    restored: sha(readFileSync(specFile)) === sha(specOriginal) && sha(readFileSync(contractFile)) === sha(contractOriginal),
    greenExit: green.code,
  })
} finally {
  writeFileSync(specFile, specOriginal)
  writeFileSync(contractFile, contractOriginal)
}

// ---------------------------------------------------------------- M02
mutate({
  id: 'M02',
  title: '把写命令 board checkin 标成 impl: ts',
  file: 'src/domain/cli.ts',
  patch: (text) =>
    text.replace(
      `    path: 'board checkin',\n    kind: 'write',\n    impl: 'unsupported-shadow',`,
      `    path: 'board checkin',\n    kind: 'write',\n    impl: 'ts',`,
    ),
  check: 'scripts/check-cli-surface.mjs',
  expectRed: ['cli.write_commands_refused'],
})

// ---------------------------------------------------------------- M03（只在夹具家目录里跑）
{
  const fixtureRoot = mkdtempSync(path.join(os.tmpdir(), 'fish-t04-mut-'))
  const dshHome = path.join(fixtureRoot, 'dsh')
  const wbHome = path.join(fixtureRoot, 'workbuddy')
  mkdirSync(dshHome, { recursive: true })
  mkdirSync(wbHome, { recursive: true })
  // 夹具：DSH 少了 PostToolUse（会改），WorkBuddy 空 hooks（会新建）——两条都必然进入"写计划"分支。
  writeFileSync(
    path.join(dshHome, 'hooks.json'),
    `${JSON.stringify(
      {
        hooks: {
          SessionStart: [{ hooks: [{ type: 'command', command: `python "${path.join(fishHome, 'fish_dsh_hook.py')}"`, timeout: 25 }] }],
          PreToolUse: [{ matcher: 'Write|write|Edit|edit', hooks: [{ type: 'command', command: `python "${path.join(fishHome, 'fish_pretool_hook.py')}" --host dsh`, timeout: 10 }] }],
          Stop: [{ hooks: [{ type: 'command', command: 'node "C:/other/stop.js"' }] }],
        },
      },
      null,
      2,
    )}\r\n`,
    'utf8',
  )
  writeFileSync(path.join(wbHome, 'settings.json'), `${JSON.stringify({ hooks: {} }, null, 2)}\n`, 'utf8')
  const fixtureEnv = { ...baseEnv, DSH_HOME: dshHome, WORKBUDDY_HOME: wbHome }
  try {
    mutate({
      id: 'M03',
      title: 'dry-run 顺手把计划写进目标文件',
      file: 'src/hosts/dryrun.ts',
      patch: (text) =>
        text
          .replace(
            `import { readFileSync } from 'node:fs'`,
            `import { readFileSync, writeFileSync } from 'node:fs'`,
          )
          .replace(
            `  const touched = hosts.filter((host) => !host.targetUnchanged)`,
            `  // MUTATION: 故意写盘（判据必须红）\n  for (const host of payload.hosts) writeFileSync(host.path, host.plannedText)\n  const touched = hosts.filter((host) => !host.targetUnchanged)`,
          ),
      check: 'scripts/check-hosts-dryrun.mjs',
      checkArgs: ['--fixtures'],
      env: fixtureEnv,
      expectRed: ['hosts.'],
    })
  } finally {
    rmSync(fixtureRoot, { recursive: true, force: true })
  }
}

// ---------------------------------------------------------------- 汇总
const failed = results.filter((result) => !result.ok)
console.log('# T04 变异自证')
for (const result of results) {
  console.log(`${result.ok ? 'PASS' : 'FAIL'}  ${result.id}  ${result.title}  ${result.detail}；还原=${result.restored ? '逐字节一致' : '**不一致**'}；${result.greenDetail ?? ''}`)
}
console.log(`# summary: ${results.length - failed.length}/${results.length} 符合预期`)
process.exitCode = failed.length === 0 ? 0 : 1
