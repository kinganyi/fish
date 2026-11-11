/**
 * T04 单元测试：命令等价层（`domain/cli`）+ 宿主管理面的纯函数部分。
 *
 * 三条覆盖重点（都是"改坏了判据才会红"的那类）：
 *   ① `buildCliMap` 的双向覆盖（漏一条 / 树里多了没裁决的）/ `--apply` 与只读的矛盾；
 *      以及 `validateCliAnnotation` 对**合成裁决**的逐条拒绝（写标 ts / proxy 标写 / 缺替代）；
 *   ② 命令解析：最长前缀（`kb vault list` 不能被当成 `kb vault` + 位置参数）+ `--k=v` 还原；
 *   ③ 宿主侧纯函数：matcher 方言、心跳新鲜度（**注入 now**，不依赖真实时钟）、统一 diff。
 */

import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { test } from 'node:test'

import {
  buildCliMap,
  checkHostHealth,
  CLI_IMPLS,
  CLI_KINDS,
  cliGroupPaths,
  cliLeafPaths,
  cliRootPaths,
  findCliAnnotation,
  flagsToArgv,
  HEARTBEAT_STALE_MS,
  matcherCoversWriteTools,
  matcherDialect,
  parseCommandInput,
  readHeartbeat,
  renderCliGuidance,
  renderCliHelpText,
  renderCliRefusal,
  resolveCliRoute,
  resolveHostSpecs,
  runFishCliOnSnapshot,
  scriptPathOf,
  unifiedDiff,
  validateCliAnnotation,
} from '../../lib/index.js'

/** 生产工具名（裁决表里用到的那些；校验 `mcpTool` 需要）。 */
const KNOWN_TOOLS = ['remember', 'recall', 'board', 'kb', 'kb_write', 'supersede', 'add_project', 'close_project', 'reopen_project', 'corpus']

/** 按 76 条真实叶子造一棵合成命令树（`applyPaths` 用来伪造 `--apply` 开关）。 */
function syntheticEnumeration(paths, options = {}) {
  const commands = []
  const roots = [...new Set(paths.map((p) => p.split(' ')[0]))]
  for (const root of roots) {
    const rest = paths.filter((p) => p.startsWith(`${root} `)).map((p) => p.slice(root.length + 1))
    if (rest.length === 0) {
      commands.push({
        path: root,
        depth: 1,
        kind: 'leaf',
        help: '',
        children: [],
        hasApplyFlag: options.applyPaths?.includes(root) ?? false,
        func: 'cmd_x',
      })
      continue
    }
    const direct = rest.filter((child) => !child.includes(' '))
    commands.push({ path: root, depth: 1, kind: 'group', help: '', children: direct, hasApplyFlag: false, func: null })
    for (const child of rest) {
      const full = `${root} ${child}`
      if (child.includes(' ') && rest.some((other) => child.startsWith(`${other} `))) continue
      commands.push({
        path: full,
        depth: full.split(' ').length,
        kind: 'leaf',
        help: '',
        children: [],
        hasApplyFlag: options.applyPaths?.includes(full) ?? false,
        func: 'cmd_x',
      })
    }
  }
  return {
    python: '3.11.9',
    fishPy: 'p0/fish.py',
    fishPySha256_12: 'deadbeef1234',
    rootCommands: roots,
    commands,
  }
}

// 实测口径（2026-11-09 复核）：`spec/cli-map.json` 与 `CLI_ANNOTATIONS` 都是 80 条，
// 一级 37 / 容器 13 / 叶子 80。原来的 76/35/12 是 T04 交付时的读数，fish 命令树后来长大了
// 而这三处断言没跟上——**测试过时**，不是源码漂移（差集为空可以证明）。
test('cli-map：真实裁决表覆盖 80 条叶子 / 37 一级 / 13 容器，四个 impl 都出现过', () => {
  const leaves = cliLeafPaths()
  assert.equal(leaves.length, 80, `叶子应为 80 条，实得 ${leaves.length}`)
  assert.equal(new Set(leaves).size, leaves.length, '裁决表里有重复路径')
  assert.equal(cliRootPaths().length, 37)
  assert.equal(cliGroupPaths().length, 13)
  for (const impl of CLI_IMPLS) {
    assert.ok(
      leaves.some((leaf) => findCliAnnotation(leaf)?.impl === impl),
      `取值域里的 ${impl} 没有任何一条命令使用（表与域脱节）`,
    )
  }
  for (const leaf of leaves) {
    const annotation = findCliAnnotation(leaf)
    if (annotation.impl === 'ts') {
      assert.equal(annotation.kind, 'read', `${leaf} 是 ts 但不是只读`)
      continue
    }
    assert.ok(annotation.replacement !== undefined, `${leaf} 缺替代命令（未实现项不许是终点）`)
  }
})

test('cli-map：合成树里有未裁决的命令 → 抛错并点名', () => {
  const enumeration = syntheticEnumeration([...cliLeafPaths(), 'zzz-unannotated'])
  assert.throws(() => buildCliMap(enumeration, { knownTools: KNOWN_TOOLS }), /没有裁决|zzz-unannotated/)
})

test('cli-map：树里少一条被裁决的命令 → 抛错（登记表不许声明不存在的契约）', () => {
  const enumeration = syntheticEnumeration(cliLeafPaths().filter((leaf) => leaf !== 'board checkin'))
  assert.throws(() => buildCliMap(enumeration, { knownTools: KNOWN_TOOLS }), /在命令树里不存在/)
})

test('cli-map：只读命令带 --apply → 抛错（--apply 是写的强信号）', () => {
  const enumeration = syntheticEnumeration(cliLeafPaths(), { applyPaths: ['list'] })
  assert.throws(() => buildCliMap(enumeration, { knownTools: KNOWN_TOOLS }), /--apply/)
})

test('cli-map：合成裁决逐条拒绝（写标 ts / proxy 标写 / 缺替代 / 替代悬空）', () => {
  const base = { path: 'fake cmd', kind: 'write', impl: 'ts', reason: '这是一条足够长的理由' }
  assert.throws(() => validateCliAnnotation(base, KNOWN_TOOLS), /不许标成 ts/)
  assert.throws(
    () => validateCliAnnotation({ ...base, kind: 'write', impl: 'proxy-readonly', evidence: 'x' }, KNOWN_TOOLS),
    /只允许 kind=read/,
  )
  assert.throws(
    () => validateCliAnnotation({ ...base, kind: 'read', impl: 'proxy-readonly' }, KNOWN_TOOLS),
    /只读依据/,
  )
  assert.throws(() => validateCliAnnotation({ ...base, kind: 'write', impl: 'unsupported-shadow' }, KNOWN_TOOLS), /必须给替代/)
  assert.throws(
    () =>
      validateCliAnnotation(
        { ...base, kind: 'write', impl: 'unsupported-shadow', replacement: { cli: 'remember', mcpTool: 'no_such_tool' } },
        KNOWN_TOOLS,
      ),
    /不在生产工具表/,
  )
  assert.throws(() => validateCliAnnotation({ ...base, kind: 'read', impl: 'no-such-impl' }, KNOWN_TOOLS), /取值域/)
  assert.throws(() => validateCliAnnotation({ ...base, kind: 'read', impl: 'ts', reason: '太短' }, KNOWN_TOOLS), /reason/)
  // 合法的一条必须通过（否则上面的断言可能是"怎么都抛"）。
  assert.doesNotThrow(() =>
    validateCliAnnotation({ path: 'fake cmd', kind: 'read', impl: 'ts', reason: '这是一条足够长的理由' }, KNOWN_TOOLS),
  )
})

test('cli-route：最长前缀匹配（kb vault list 不是 kb vault + 位置参数）', () => {
  const route = resolveCliRoute(['kb', 'vault', 'list'])
  assert.equal(route?.path, 'kb vault list')
  assert.deepEqual(route?.rest, [])
  const withRest = resolveCliRoute(['recall', '协作', '约定'])
  assert.equal(withRest?.path, 'recall')
  assert.deepEqual(withRest?.rest, ['协作', '约定'])
  const group = resolveCliRoute(['kb', 'vault'])
  assert.equal(group?.path, 'kb vault')
  assert.equal(group?.annotation, null, '容器不该带裁决')
  assert.equal(resolveCliRoute(['no-such-command']), null)
})

test('cli-help：顶层与单条都能渲染出实现归属与替代', () => {
  const top = renderCliHelpText()
  assert.ok(top.includes('一级命令 37 个'))
  assert.ok(top.includes('shadow / hosts'))
  const one = renderCliHelpText('board checkin')
  assert.ok(one.includes('实现：unsupported-shadow'))
  assert.ok(one.includes('替代：') && one.includes('fish board checkin'))
})

test('cli-refusal：拒绝/指引文案必须含"没有"与替代（不许静默成功）', () => {
  const annotation = findCliAnnotation('remember')
  const refusal = renderCliRefusal(annotation, annotation.reason)
  assert.ok(refusal.includes('影子期不执行写操作'))
  assert.ok(refusal.includes('没有'))
  assert.ok(refusal.includes('fish remember'))
  const start = findCliAnnotation('session start')
  const guidance = renderCliGuidance(start, start.reason)
  assert.ok(guidance.includes('只给指引'))
  assert.ok(guidance.includes('没有'))
})

test('parse：--k=v / --k v / --k 三种形态都能还原成 argv', () => {
  const parsed = parseCommandInput('events --limit=20 --scope all --json')
  assert.deepEqual(parsed.positional, ['events'])
  assert.equal(parsed.flags.limit, '20')
  assert.equal(parsed.flags.scope, 'all')
  assert.equal(parsed.flags.json, true)
  assert.deepEqual(flagsToArgv(parsed.flags), ['--json', '--limit=20', '--scope=all'])
  assert.equal(parseCommandInput('x --note=a=b').flags.note, 'a=b')
})

test('matcher：只认纯字面备选（锚定/内联 flag/首尾空白都非法）', () => {
  assert.equal(matcherDialect('Write|write|Edit|edit').kind, 'literal')
  assert.equal(matcherDialect('^(Write|write|Edit|edit)$').kind, '')
  assert.equal(matcherDialect('(?i)write|edit').kind, '')
  assert.equal(matcherDialect(' Write|write').kind, '')
  assert.equal(matcherDialect('').kind, '')
  assert.equal(matcherDialect(null).kind, '')
  assert.equal(matcherCoversWriteTools('Write|write|Edit|edit'), true)
  assert.equal(matcherCoversWriteTools('Write|Edit'), false, '大小写只覆盖一半不算通过')
  assert.equal(scriptPathOf('python "I:/a/b/fish_pretool_hook.py" --host dsh'), 'I:/a/b/fish_pretool_hook.py')
  assert.equal(scriptPathOf('python C:/x/fish_push_hook.py'), 'C:/x/fish_push_hook.py')
  assert.equal(scriptPathOf('node stop.js'), null)
})

test('unifiedDiff：无变化给空串，有变化给 @@ 块', () => {
  assert.equal(unifiedDiff('a\nb\n', 'a\nb\n'), '')
  const diff = unifiedDiff('a\nb\nc\n', 'a\nB\nc\n')
  assert.ok(diff.startsWith('@@'), diff)
  assert.ok(diff.includes('-b') && diff.includes('+B'))
})

test('heartbeat：新鲜度按注入时刻算，bad_payload 不算真实触发', () => {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'fish-hb-'))
  try {
    const hooks = path.join(dir, '_audit', 'hooks')
    mkdirSync(hooks, { recursive: true })
    const now = Date.parse('2026-10-27T12:00:00Z')
    writeFileSync(
      path.join(hooks, 'heartbeat-dsh-PostToolUse.json'),
      JSON.stringify({ ts_utc: '2026-10-27T11:59:00Z', event: 'PostToolUse', host: 'dsh', tool_name: 'pwsh' }),
    )
    writeFileSync(
      path.join(hooks, 'heartbeat-dsh-PreToolUse.json'),
      JSON.stringify({ ts_utc: '2026-10-27T09:00:00Z', event: 'PreToolUse', host: 'dsh' }),
    )
    writeFileSync(
      path.join(hooks, 'heartbeat-dsh-UserPromptSubmit.json'),
      JSON.stringify({ ts_utc: '2026-10-27T11:59:30Z', event: 'UserPromptSubmit', host: 'dsh', bad_payload: true }),
    )
    const fresh = readHeartbeat(dir, 'dsh', 'PostToolUse', now)
    assert.equal(fresh.exists, true)
    assert.equal(fresh.stale, false)
    assert.equal(fresh.ageMs, 60000)
    const stale = readHeartbeat(dir, 'dsh', 'PreToolUse', now)
    assert.equal(stale.stale, true)
    assert.ok(stale.ageMs > HEARTBEAT_STALE_MS)
    const drill = readHeartbeat(dir, 'dsh', 'UserPromptSubmit', now)
    assert.equal(drill.badPayload, true)
    assert.equal(drill.stale, true)
    const missing = readHeartbeat(dir, 'dsh', 'SessionStart', now)
    assert.equal(missing.exists, false)
    const health = checkHostHealth(
      dir,
      resolveHostSpecs(),
      { dsh: ['SessionStart', 'PreToolUse', 'PostToolUse', 'UserPromptSubmit'] },
      now,
    )
    assert.ok(
      health.alerts.some((alert) => alert.includes('dsh/SessionStart') && alert.includes('从未出现')),
      `告警里必须有"配置有但心跳缺失"：${health.alerts.join(' | ')}`,
    )
    assert.ok(health.alerts.some((alert) => alert.includes('dsh/PreToolUse') && alert.includes('陈旧')))
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('取值域：CLI_KINDS / CLI_IMPLS 与 spec 校验读同一份', () => {
  assert.deepEqual([...CLI_KINDS], ['read', 'write', 'ops'])
  assert.deepEqual([...CLI_IMPLS], ['ts', 'proxy-readonly', 'unsupported-shadow', 'python-only'])
})

test('工程口径：每条 proxy 都必须是只读且带源码依据', () => {
  for (const leaf of cliLeafPaths()) {
    const annotation = findCliAnnotation(leaf)
    if (annotation.impl !== 'proxy-readonly') continue
    assert.equal(annotation.kind, 'read', `${leaf} 标了 proxy 但不是只读`)
    assert.ok((annotation.evidence ?? '').includes('fish.py'), `${leaf} 的只读依据里没有源码位置`)
  }
})

test('proxy：带写开关一律拒（分类标错与参数拼错是两种事故，双保险）', () => {
  for (const flag of ['--apply', '--write', '--force']) {
    assert.throws(
      () => runFishCliOnSnapshot({ fishHome: 'unused', dbPath: 'unused.sqlite', args: ['board', 'checkin', flag] }),
      /只读转发不许带写开关/,
      `${flag} 必须被拒绝`,
    )
  }
})
