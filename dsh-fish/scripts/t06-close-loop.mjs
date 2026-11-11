#!/usr/bin/env node
/**
 * T06 判据 6 闭合脚本（停机窗口内运行）
 *
 * 背景：判据 6 要求「装/卸/重装 dsh-fish 期间，真实 DSH 家目录零变化」。
 * 但 DSH 桌面端运行时自己会重写 profiles/desktop/cordis.patch.yml（LF→CRLF），
 * 造成「外部写入」污染证据。因此必须在桌面端完全退出后复跑。
 *
 * 本脚本只做三件事：
 *   1. 停机窗口内采集基线（真实 ~/.dsh 下 profiles/desktop 与 hooks.json 的 sha256 树）
 *   2. 在**隔离家目录**（.tmp/dsh-home-t06-closeloop）里完成 建 profile → 装 → 卸 → 重装
 *   3. 复采基线并对比；任何差异都判 FAIL 并逐条列出
 *
 * 安全边界：
 *   · 所有 dsh 调用都带 DSH_HOME=<隔离目录>，绝不触碰真实 profiles
 *   · 不修改真实 ~/.dsh 下任何文件（只读 sha256）
 *   · 不启用插件（disabled: true），只验证安装/卸载/幂等
 *
 * 用法：
 *   node t06-close-loop.mjs            # 真实执行（桌面端必须先完全退出）
 *   node t06-close-loop.mjs --dry-run  # 只打印计划，不做任何调用
 */
import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs'
import path from 'node:path'

const WORK = 'I:\\app\\fish-public\\dsh-fish'
const REAL_HOME = 'C:\\Users\\15596\\.dsh'
const ISO_HOME = path.join(WORK, '.tmp', 'dsh-home-t06-closeloop')
const DSH = 'I:\\app\\DSH-Home\\app\\resources\\runtime\\cli\\bin\\dsh.cmd'
const PNPM = 'C:\\Users\\15596\\.dsh\\dsh-runtimes\\dsh-primary-runtime\\dependencies\\pnpm\\bin\\pnpm.mjs'
const NODE = 'C:\\Users\\15596\\.dsh\\dsh-runtimes\\dsh-primary-runtime\\dependencies\\node\\bin\\node.exe'
const EVIDENCE = path.join(WORK, '.tmp', 't06-close-loop-evidence.json')
const PROFILE = 'dsh-fish-dev'
const DRY = process.argv.includes('--dry-run')

const log = (msg) => console.log(`[t06] ${msg}`)
const sha256 = (file) => createHash('sha256').update(readFileSync(file)).digest('hex')

/** 采集一棵树的 {相对路径: sha256}（不跟随目录符号链接之外的怪东西）。 */
function treeSha(root) {
  const out = {}
  if (!existsSync(root)) return out
  const walk = (dir) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name)
      if (entry.isDirectory()) walk(full)
      else if (entry.isFile()) out[path.relative(root, full).replaceAll('\\', '/')] = sha256(full)
    }
  }
  walk(root)
  return out
}

function snapshot() {
  const profilesRoot = path.join(REAL_HOME, 'profiles')
  return {
    desktop: treeSha(path.join(profilesRoot, 'desktop')),
    hooks: existsSync(path.join(REAL_HOME, 'hooks.json')) ? sha256(path.join(REAL_HOME, 'hooks.json')) : '(absent)',
    profilesEntries: existsSync(profilesRoot) ? readdirSync(profilesRoot).sort() : [],
  }
}

function diffTrees(before, after) {
  const findings = []
  for (const key of new Set([...Object.keys(before), ...Object.keys(after)])) {
    const a = before[key]
    const b = after[key]
    if (a === b) continue
    if (a === undefined) findings.push(`added: ${key}`)
    else if (b === undefined) findings.push(`removed: ${key}`)
    else findings.push(`changed: ${key} (${a.slice(0, 12)} -> ${b.slice(0, 12)})`)
  }
  return findings
}

function run(label, file, args, env = {}) {
  if (DRY) {
    log(`[dry-run] ${label}: ${file} ${args.join(' ')}`)
    return { status: 0, stdout: '', stderr: '' }
  }
  log(`> ${label}`)
  const res = spawnSync(file, args, {
    encoding: 'utf8',
    cwd: WORK,
    maxBuffer: 64 * 1024 * 1024,
    env: { ...process.env, ...env },
    shell: file.toLowerCase().endsWith('.cmd'),
  })
  if (res.error) throw new Error(`${label} 启动失败: ${res.error.message}`)
  const tail = (res.stdout || '').trim().split(/\r?\n/).slice(-2).join(' | ')
  log(`  exit=${res.status}${tail ? ` :: ${tail.slice(0, 160)}` : ''}`)
  if (res.status !== 0) throw new Error(`${label} 退出码 ${res.status}\n${(res.stderr || '').slice(0, 800)}`)
  return res
}

function main() {
  if (!existsSync(DSH)) throw new Error(`找不到 dsh CLI：${DSH}`)
  const tarballs = existsSync(path.join(WORK, '.tmp'))
    ? readdirSync(path.join(WORK, '.tmp')).filter((n) => n.endsWith('.tgz')).sort()
    : []

  log(`模式=${DRY ? 'dry-run' : '真实执行'}`)
  log(`隔离家目录=${ISO_HOME}`)
  log(`已有 tarball=${tarballs.join(',') || '(无，将现场 pack)'}`)

  const before = DRY ? { desktop: {}, hooks: '(dry)', profilesEntries: [] } : snapshot()
  log(`基线：desktop 文件 ${Object.keys(before.desktop).length} 个，hooks=${String(before.hooks).slice(0, 12)}`)

  mkdirSync(ISO_HOME, { recursive: true })
  const env = { DSH_HOME: ISO_HOME, FISH_HOME: 'I:\\app\\DSH-Home\\workspace\\learn\\大肥鱼生命系统\\p0' }
  const dsh = (label, args) => run(label, DSH, args, env)

  // 1) 建隔离 profile
  dsh('create-profile', [PROFILE, '--from-default-profile', 'web', '--dump-config'])

  // 2) pack（无 tarball 时现场打）
  let tarball
  if (tarballs.length > 0) {
    tarball = path.join(WORK, '.tmp', tarballs[tarballs.length - 1])
  } else {
    run('pack', NODE, [PNPM, 'pack', '--pack-destination', '.tmp'], env)
    const fresh = readdirSync(path.join(WORK, '.tmp')).filter((n) => n.endsWith('.tgz')).sort()
    if (fresh.length === 0) throw new Error('pack 未产出 tarball')
    tarball = path.join(WORK, '.tmp', fresh[fresh.length - 1])
  }
  log(`tarball=${tarball}`)

  // 3) 装 → 卸 → 重装（幂等）
  dsh('install-1', ['plugin', '--profile', PROFILE, 'add', tarball])
  const manifest1 = path.join(ISO_HOME, 'profiles', PROFILE, 'package.json')
  const lock1 = path.join(ISO_HOME, 'profiles', PROFILE, 'pnpm-lock.yaml')
  const snap1 = DRY ? null : { manifest: sha256(manifest1), lock: existsSync(lock1) ? sha256(lock1) : '(none)' }

  dsh('install-1-dump', ['--profile', PROFILE, '--dump-config'])
  dsh('remove', ['plugin', '--profile', PROFILE, 'remove', 'dsh-fish'])
  dsh('install-2', ['plugin', '--profile', PROFILE, 'add', tarball])
  const snap2 = DRY ? null : { manifest: sha256(manifest1), lock: existsSync(lock1) ? sha256(lock1) : '(none)' }

  const after = DRY ? { desktop: {}, hooks: '(dry)', profilesEntries: [] } : snapshot()
  const treeFindings = diffTrees(before.desktop, after.desktop)
  const hooksChanged = before.hooks !== after.hooks
  const profilesChanged =
    JSON.stringify(before.profilesEntries) !== JSON.stringify(after.profilesEntries)
  const idempotent = snap1 && snap2 ? snap1.manifest === snap2.manifest && snap1.lock === snap2.lock : null

  const result = {
    ranAt: new Date().toISOString(),
    mode: DRY ? 'dry-run' : 'real',
    isoHome: ISO_HOME,
    tarball,
    criterion6: {
      desktopTreeFindings: treeFindings,
      hooksChanged,
      profilesEntriesChanged: profilesChanged,
      pass: treeFindings.length === 0 && !hooksChanged && !profilesChanged,
    },
    idempotent,
  }

  if (!DRY) writeFileSync(EVIDENCE, `${JSON.stringify(result, null, 2)}\n`, 'utf8')

  console.log('\n================ 判据 6（停机窗口） ================')
  console.log(`desktop 配置树差异：${treeFindings.length === 0 ? '无' : treeFindings.join('; ')}`)
  console.log(`hooks.json：${hooksChanged ? '变了（FAIL）' : '未变（OK）'}`)
  console.log(`profiles 目录项：${profilesChanged ? '变了（FAIL）' : '未变（OK）'}`)
  console.log(`装→卸→重装幂等：${idempotent === null ? '(dry-run)' : idempotent ? '是（OK）' : '否（FAIL）'}`)
  console.log(`\n判据 6：${result.criterion6.pass ? 'PASS' : 'FAIL'}`)
  if (!DRY) console.log(`证据：${EVIDENCE}`)
  console.log('===================================================')
  return result.criterion6.pass ? 0 : 1
}

try {
  process.exitCode = main()
} catch (error) {
  console.error(`[t06] 失败：${error.message}`)
  process.exitCode = 1
}
