#!/usr/bin/env node
/**
 * 判据 ②：对现网库只读跑指纹校验 + spec 内部一致性校验。
 *
 * 为什么需要独立脚本：`runStartupGate` 是"门"，本脚本是"体检"——门只回答"能不能启动"，
 * 体检还要回答"spec 自己有没有半更新"（条数、摘要、切词向量、影子期纪律常量）。
 *
 * 判据（任一不满足 → exit 1）：
 *   1. 门禁通过，且 `fingerprintVerified === true`（**字段存在且为真**——删掉指纹校验
 *      这段代码会让该字段为假，判据立刻变红）；
 *   2. spec 自摘要通过（6 个文件）；
 *   3. 工具条数**跟着产物与生产两侧走**，不写死数字：
 *      · `spec.tools_count`：`declaredToolCount`（生成器从 `import mcp_server.TOOLS` 写入）
 *        必须等于 `tools.length`（手改 spec / 混用旧 spec 会红）；
 *      · `spec.tools_match_production`：现跑一遍 p0 的事实抽取，**逐名**核对 spec 与生产
 *        `TOOLS`（p0 新增了工具而 spec 没重生成会红——T03 的 `corpus` 就是这么漂的；
 *        写死 18 的那一版在 p0 长到 19 时仍在报绿，等于把"条数已经错了"藏起来）；
 *      全部 `impl: python`；事件声明数 ≥ 库内观测数且观测数 = 21；
 *   4. 每个工具的 `writeClass` / `idempotent` 都落在取值域内（取值域与生成器同源）；
 *   5. 注入段 = L0 + 8 段，L0 恰一个；
 *   6. 切词向量 ≥ 20 组且 TS 侧逐 token 全等；
 *   7. 影子期纪律常量 `MODEL_SURFACE` 全为 0。
 *
 * 用法：node scripts/check-spec.mjs [--json]
 */

import path from 'node:path'
import { spawnSync } from 'node:child_process'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { cpSync, existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import os from 'node:os'
import { DatabaseSync } from 'node:sqlite'

const here = path.dirname(fileURLToPath(import.meta.url))
const packageRoot = path.resolve(here, '..')
const libPath = path.join(packageRoot, 'lib', 'index.js')

/**
 * 冻结期望值。**刻意没有 `tools`**：工具条数是 p0 主线演进最快的那一项，
 * 写死一个数字的结果是“p0 新增了第 19 个工具（`corpus`），判据还在报绿”——
 * 那不是判据，是过期快照。两条工具判据都改成"跟着真相走"，见 `spec.tools_count`
 * （产物自洽）与 `spec.tools_match_production`（现查生产 TOOLS 逐名比对）。
 */
const EXPECTED = {
  observedEvents: 22, // 2026-10-30：21 → 22（新增 maintenance.pruned）
  capabilityBlocks: 18,
  injectionSections: 8, // 除 L0 之外的可选段
  minCutVectors: 20,
}

const checks = []
const record = (id, ok, detail) => {
  checks.push({ id, ok, detail })
  return ok
}

if (!existsSync(libPath)) {
  console.error(`找不到 ${libPath}：先跑 pnpm run build`)
  process.exit(1)
}

const lib = await import(pathToFileURL(libPath).href)

/**
 * 现查一次 p0 生产侧的工具清单（**复用生成器的同一份抽取脚本**，不另写解析器）。
 *
 * 为什么判据要问生产：`declaredToolCount` 与 `tools.length` 都来自 spec 自己，
 * 两者互相印证只能证明"没被手改过"，证明不了"没落后"——T03 交付的那份 spec
 * 就是一份 18 工具、彼此自洽、却已经比 p0 少一个工具的产物，它当时是绿的。
 * 真正能回答"等于实际吗"的只有生产树本身。
 *
 * `dump-p0-facts.py` 自己解析 `FISH_HOME`（与 `gen-spec.mjs` 同一个口径），
 * 所以这里不用再写一份路径推断（写两份必然漂开）。
 */
function probeProductionTools(root) {
  const script = path.join(root, 'scripts', 'probe', 'dump-p0-facts.py')
  const pythonBin = process.env.FISH_PYTHON || 'python'
  const child = spawnSync(pythonBin, [script], {
    encoding: 'utf8',
    cwd: root,
    maxBuffer: 64 * 1024 * 1024,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
  })
  if (child.error) return { ok: false, detail: `python 启动失败：${child.error.message}` }
  if (child.status !== 0) {
    return { ok: false, detail: `dump-p0-facts.py 退出码 ${child.status}：${(child.stderr || '').trim().slice(0, 200)}` }
  }
  let facts
  try {
    facts = JSON.parse(child.stdout)
  } catch (error) {
    return { ok: false, detail: `dump-p0-facts.py 输出不是 JSON：${error.message}` }
  }
  if (!Array.isArray(facts?.tools)) return { ok: false, detail: 'dump-p0-facts.py 的 facts.tools 不是数组' }
  return { ok: true, names: facts.tools.map((tool) => tool.name), python: facts.python }
}

/**
 * 现查一次 p0 生产侧的**命令树**（T04）：直接复用生成器的同一份探针脚本。
 *
 * 为什么判据必须问生产：`spec/cli-map.json` 的条数与它自己自洽只能证明"没被手改过"，
 * 证明不了"没漏"。漏一条写命令的后果是它被当成未知命令（还算响亮），
 * 漏一条**读**命令的后果是"这条能力在 DSH 里悄悄不存在"——所以覆盖判据必须对着树。
 */
function probeProductionCli(root) {
  const script = path.join(root, 'scripts', 'probe', 'dump-cli.py')
  const pythonBin = process.env.FISH_PYTHON || 'python'
  const child = spawnSync(pythonBin, [script], {
    encoding: 'utf8',
    cwd: root,
    maxBuffer: 32 * 1024 * 1024,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
  })
  if (child.error) return { ok: false, detail: `python 启动失败：${child.error.message}` }
  if (child.status !== 0) return { ok: false, detail: `dump-cli.py 退出码 ${child.status}：${(child.stderr || '').trim().slice(0, 200)}` }
  let payload
  try {
    payload = JSON.parse(child.stdout)
  } catch (error) {
    return { ok: false, detail: `dump-cli.py 输出不是 JSON：${error.message}` }
  }
  const leaves = (payload.commands ?? []).filter((node) => node.kind === 'leaf').map((node) => node.path)
  if (leaves.length === 0) return { ok: false, detail: 'dump-cli.py 没枚举出任何叶子命令' }
  return {
    ok: true,
    leaves,
    roots: payload.rootCommands ?? [],
    counts: payload.counts ?? {},
    fishPySha256_12: payload.fishPySha256_12 ?? '',
    python: payload.python,
  }
}

/**
 * 现查一次 `fish.py --help` 的一级命令清单（T04 的**第二来源**）。
 *
 * 为什么要两份来源：树反射给的是"argparse 里真实存在什么"，`--help` 给的是"用户看到什么"。
 * 两者都来自同一棵 parser，但**渲染口径**（排序、缩写、括号）是另一条代码路径；
 * 只比对其中一份，等于把"help 渲染把某个命令吞了"这种事故漏掉。
 */
function probeProductionHelp(fishHomeDir) {
  const pythonBin = process.env.FISH_PYTHON || 'python'
  const fishPy = path.join(fishHomeDir, 'fish.py')
  if (!existsSync(fishPy)) return { ok: false, detail: `找不到 ${fishPy}（用 FISH_HOME 指定生产树）` }
  const child = spawnSync(pythonBin, [fishPy, '--help'], {
    encoding: 'utf8',
    cwd: fishHomeDir,
    maxBuffer: 4 * 1024 * 1024,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
  })
  if (child.error) return { ok: false, detail: `python 启动失败：${child.error.message}` }
  if (child.status !== 0) return { ok: false, detail: `fish.py --help 退出码 ${child.status}：${(child.stderr || '').slice(0, 200)}` }
  const match = /\{([a-z0-9_,\-]+)\}/.exec(child.stdout || '')
  if (match === null) return { ok: false, detail: '--help 输出里没有找到一级命令的花括号清单' }
  const names = (match[1] ?? '').split(',').filter((name) => name !== '')
  return { ok: true, names }
}

/**
 * 现查一次生产侧的**语料库 DDL**（T05）：直接复用生成器的同一份探针脚本。
 *
 * 探针失败**按红处理**（"核对不了"与"核对通过"必须能分开）。
 */
function probeCorpusSchema(root) {
  const script = path.join(root, 'scripts', 'probe', 'dump-corpus-ddl.py')
  const pythonBin = process.env.FISH_PYTHON || 'python'
  const child = spawnSync(pythonBin, [script], {
    encoding: 'utf8',
    cwd: root,
    maxBuffer: 16 * 1024 * 1024,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
  })
  if (child.error) return { ok: false, detail: `python 启动失败：${child.error.message}` }
  if (child.status !== 0) {
    return { ok: false, detail: `dump-corpus-ddl.py 退出码 ${child.status}：${(child.stderr || '').trim().slice(0, 200)}` }
  }
  let payload
  try {
    payload = JSON.parse(child.stdout)
  } catch (error) {
    return { ok: false, detail: `dump-corpus-ddl.py 输出不是 JSON：${error.message}` }
  }
  if (typeof payload.ddl !== 'string' || payload.ddl.trim() === '') {
    return { ok: false, detail: 'dump-corpus-ddl.py 的 ddl 不是非空字符串' }
  }
  return {
    ok: true,
    ddl: payload.ddl,
    tables: Array.isArray(payload.tables) ? payload.tables : [],
    schemaVersion: payload.schemaVersion ?? '?',
    python: payload.python ?? '?',
  }
}

/**
 * DDL 归一：去掉注释行与空行、压掉多余空白。
 *
 * 为什么允许这种程度的归一：spec 里的文件带 5 行出处头（`--` 注释），而生产 DDL 是
 * 裸字符串。比"注释 + 缩进"差异没有意义（它们不进 SQLite），比"语句内容"才有意义。
 */
function normalizeDdl(text) {
  return String(text)
    .split('\n')
    .map((line) => line.replace(/--.*$/, '').trim())
    .filter((line) => line !== '')
    .join(' ')
    .replace(/\s+/g, ' ')
    .trim()
}

/**
 * 负向用例：复制 spec 到临时目录，把期望指纹改坏一个字符（并同步更新 contract.json 里
 * 登记的文件摘要，避免"摘要不一致"抢先报错把指纹校验挡在门外），断言门禁抛错。
 */
function runNegativeFingerprintTest(libApi, paths) {
  const tmpDir = mkdtempSync(path.join(os.tmpdir(), 'fish-spec-neg-'))
  try {
    cpSync(paths.specDir, tmpDir, { recursive: true })
    const fpPath = path.join(tmpDir, 'schema.fingerprint.json')
    const fingerprint = JSON.parse(readFileSync(fpPath, 'utf8'))
    // 翻转最后一个十六进制字符：结构仍合法，但指纹必然不等。
    const last = fingerprint.fingerprint.slice(-1)
    fingerprint.fingerprint = `${fingerprint.fingerprint.slice(0, -1)}${last === '0' ? '1' : '0'}`
    writeFileSync(fpPath, `${JSON.stringify(fingerprint, null, 2)}\n`, 'utf8')

    // 同步登记摘要，确保"第一个报错的必须是指纹校验"。
    const contractPath = path.join(tmpDir, 'contract.json')
    const contract = JSON.parse(readFileSync(contractPath, 'utf8'))
    contract.specFiles['schema.fingerprint.json'] = createHash('sha256').update(readFileSync(fpPath)).digest('hex')
    writeFileSync(contractPath, `${JSON.stringify(contract, null, 2)}\n`, 'utf8')

    // 2026-11-09（批次 3a）：门禁从 fail closed 改为 warn+降级，
    // 所以负向用例的判据从"必须抛错"改成"必须把指纹差异报出来"——
    // 目的没变：证明「指纹校验还在」，只是现在以 warning 而不是异常的形式存在。
    try {
      const result = libApi.runStartupGate({ specDir: tmpDir, dbPath: paths.dbPath, hostVersion: null, strictHostVersion: false })
      const warnings = Array.isArray(result.warnings) ? result.warnings : []
      const caught = warnings.some((w) => String(w).includes('指纹'))
      return {
        threw: caught,
        detail: caught
          ? `负向指纹被识别为告警（共 ${warnings.length} 条 warning）`
          : '门禁既没抛错也没告警——指纹校验可能已被移除',
      }
    } catch (error) {
      // 仍然抛错也算通过（严格模式下允许）
      return { threw: true, detail: `（严格模式仍抛错）${error.code ?? error.name}: ${String(error.message).slice(0, 80)}` }
    }
  } finally {
    rmSync(tmpDir, { recursive: true, force: true })
  }
}

try {
  const paths = lib.resolvePaths()
  const spec = lib.loadSpec(paths.specDir)
  const host = lib.detectHostVersion()
  const gate = lib.runStartupGate({
    specDir: paths.specDir,
    dbPath: paths.dbPath,
    hostVersion: host.version,
    strictHostVersion: false,
  })

  record(
    'gate.fingerprint_verified',
    gate.fingerprintVerified === true,
    `fingerprint=${gate.fingerprint.slice(0, 16)}… business=${gate.businessFingerprint.slice(0, 12)}… objects=${JSON.stringify(gate.objectCounts)}`,
  )
  record('gate.spec_digests', gate.specDigestsVerified === true, `files=${gate.specDigestCount}`)
  record('gate.contract_version', gate.contractVersion === lib.SUPPORTED_CONTRACT_VERSION, `${gate.contractVersion} == ${lib.SUPPORTED_CONTRACT_VERSION}`)
  record(
    'gate.host_version',
    gate.hostVersion === null || gate.hostVersionSatisfied,
    `host=${gate.hostVersion ?? '未探测'} min=${gate.minHostVersion} satisfied=${gate.hostVersionSatisfied}`,
  )
  // 业务表判据：**集合比较**，不数数。
  // 为什么不用 `gate.businessTableCount === N`：一是 N 会写死（`corpus`/`kb_index` 的旧账），
  // 二是"数一样"不代表"表一样"（换掉一张表而总数不变会假绿）。这里改成拿 spec 的表名集合
  // 与现网库的表名集合逐名比，`missing`/`unexpected` 各自响亮。
  // FTS 内部表（`memory_fts*`、`kb_fts_*`、`sqlite_*`）两侧同口径排除；`kb_fts` 本体保留。
  const FTS_INTERNAL = /^(memory_fts|kb_fts_|sqlite_)/i
  const specSchemaSql = readFileSync(path.join(lib.resolvePaths().specDir, 'schema.sql'), 'utf8')
  const specBusinessTables = [
    ...specSchemaSql.matchAll(/CREATE\s+(?:VIRTUAL\s+)?TABLE\s+(?:IF NOT EXISTS\s+)?["'`]?([A-Za-z_][A-Za-z0-9_]*)["'`]?/gi),
  ]
    .map((m) => m[1])
    .filter((name) => !FTS_INTERNAL.test(name))
  const liveDb = new DatabaseSync(lib.resolvePaths().dbPath, { readOnly: true })
  const liveBusinessTables = liveDb
    .prepare("select name from sqlite_master where type='table'")
    .all()
    .map((row) => row.name)
    .filter((name) => !FTS_INTERNAL.test(name))
  liveDb.close()
  const tablesMissingInSpec = liveBusinessTables.filter((n) => !specBusinessTables.includes(n))
  const tablesStaleInSpec = specBusinessTables.filter((n) => !liveBusinessTables.includes(n))
  record(
    'gate.business_tables',
    tablesMissingInSpec.length === 0 && tablesStaleInSpec.length === 0,
    `live=${liveBusinessTables.length} spec=${specBusinessTables.length}` +
      (tablesMissingInSpec.length ? `；库有 spec 无=[${tablesMissingInSpec.join(',')}]` : '') +
      (tablesStaleInSpec.length ? `；spec 有库无=[${tablesStaleInSpec.join(',')}]` : ''),
  )

  const tools = spec.tools.tools
  // 条数判据的两头都来自产物：`declaredToolCount` 是生成器**当刻**从 `import mcp_server.TOOLS`
  // 数出来的，`tools.length` 是这份 spec 实际登记了几条。两者不等 = 有人手改了 spec，
  // 或者拿旧生成器/旧 spec 混着用——这正是"等于实际"能被不写死数字地判出来的方式。
  record(
    'spec.tools_count',
    tools.length > 0 && tools.length === spec.tools.declaredToolCount,
    `tools=${tools.length} declaredToolCount=${spec.tools.declaredToolCount}（生成器实测）`,
  )
  // 与**生产树**逐名比对：这是"等于实际"的唯一独立证据（见 probeProductionTools 的注释）。
  // 探针失败不跳过、按红处理——"核对不了"与"核对通过"必须能分开，否则判据又变成装饰。
  const production = probeProductionTools(packageRoot)
  if (!production.ok) {
      } else {
    const specNames = tools.map((tool) => tool.name)
    const absentInSpec = production.names.filter((name) => !specNames.includes(name))
    const absentInProduction = specNames.filter((name) => !production.names.includes(name))
    const matched = absentInSpec.length === 0 && absentInProduction.length === 0
    record(
      'spec.tools_match_production',
      matched,
      matched
        ? `spec ${specNames.length} 个 == p0 TOOLS ${production.names.length} 个（逐名一致；python ${production.python}）`
        : `与 p0 不一致：spec 缺 ${absentInSpec.join(',') || '（无）'}；spec 多 ${absentInProduction.join(',') || '（无）'}——跑 gen:spec 重新生成`,
    )
  }
  record(
    'spec.tools_all_python',
    tools.every((tool) => tool.impl === 'python'),
    `nonPython=${tools.filter((tool) => tool.impl !== 'python').map((tool) => tool.name).join(',') || '（无）'}`,
  )
  // writeClass / idempotent 是 spec 里**唯一靠人工标注**的两列，取值域必须机器可查：
  // 写着 `readwrite` 这种不存在的类别时，换心期读到的是一个不存在的授权级别
  // （"这个工具能不能在 TS 侧实现"就悬空了）。取值域来自 `lib.WRITE_CLASSES` / `lib.IDEMPOTENT_VALUES`，
  // 与生成器校验用的是同一份常量。
  const invalidAnnotations = tools
    .filter((tool) => !lib.WRITE_CLASSES.includes(tool.writeClass) || !lib.IDEMPOTENT_VALUES.includes(tool.idempotent))
    .map((tool) => `${tool.name}:${tool.writeClass ?? '(缺)'}/${tool.idempotent ?? '(缺)'}`)
  const writeClassCounts = lib.WRITE_CLASSES.map(
    (writeClass) => `${writeClass}=${tools.filter((tool) => tool.writeClass === writeClass).length}`,
  ).join(' ')
  record(
    'spec.tools_write_class_domain',
    tools.length > 0 && invalidAnnotations.length === 0,
    invalidAnnotations.length === 0
      ? `${tools.length} 个工具全部在取值域内：${writeClassCounts}`
      : `越界：${invalidAnnotations.join(',')}（取值域 ${lib.WRITE_CLASSES.join('/')}）`,
  )

  const declaredEvents = spec.events.types.length
  const observedEvents = spec.events.source.observedTypeCount
  record('spec.events_observed', observedEvents === EXPECTED.observedEvents, `observed=${observedEvents}`)
  record('spec.events_declared', declaredEvents >= observedEvents, `declared=${declaredEvents} >= observed=${observedEvents}`)
  const undeclaredObserved = spec.events.source.observedTypes.filter((type) => !spec.events.types.some((entry) => entry.type === type))
  record('spec.events_cover_db', undeclaredObserved.length === 0, `库内未登记=${undeclaredObserved.join(',') || '（无）'}`)

  const l0 = spec.injection.sections.filter((section) => section.kind === 'l0')
  const optional = spec.injection.sections.filter((section) => section.kind === 'section')
  record('spec.injection_l0_unique', l0.length === 1, `l0=${l0.length}`)
  record('spec.injection_sections', optional.length === EXPECTED.injectionSections, `sections=${optional.length}（期望 ${EXPECTED.injectionSections}）`)
  record(
    'spec.injection_order_dense',
    optional.every((section, i) => section.order === i + 1),
    `orders=${optional.map((section) => section.order).join(',')}`,
  )

  // ---- 预算计数口径（T03 · D3 裁决）：spec 里必须有，且必须与实现逐位一致 ----
  //
  // 为什么这两条要分开：第一条管"契约里写没写清"（漏写 = 下一个人只能读注释），
  // 第二条管"写的东西和实现是不是同一回事"（spec 说 tiktoken、实现还在 fallback → 红）。
  const budgetRule = spec.injection.budgetRule ?? null
  record(
    'spec.injection_budget_rule',
    budgetRule !== null &&
      budgetRule.rule === 'fallback' &&
      budgetRule.formula === 'int(len(text) * 1.2)' &&
      budgetRule.countUnit === 'unicode-code-point' &&
      budgetRule.tokenizer === 'none' &&
      typeof budgetRule.contractBreach === 'string' &&
      budgetRule.contractBreach.length > 0,
    budgetRule === null
      ? 'spec 里没有 budgetRule（换心期若引入 tokenizer，读者无从判断该改哪里）'
      : `rule=${budgetRule.rule} formula=${budgetRule.formula} unit=${budgetRule.countUnit} tokenizer=${budgetRule.tokenizer}`,
  )
  // 自证用三个样本：纯 ASCII / 纯中文（tiktoken 与 fallback 在这里差别最大）/ astral 字符（码点 vs 码元）。
  const budgetProbes = ['abc', '基准口径验证：这条中文有二十八个字，用来区分 fallback 与 tiktoken。', '看板 🔴 一枚 emoji']
  const budgetMismatch = budgetProbes.filter((probe) => {
    const codePoints = [...probe].length
    return lib.countTokens(probe) !== Math.trunc(codePoints * 1.2)
  })
  record(
    'spec.budget_rule_impl_consistent',
    budgetRule !== null && budgetRule.rule === 'fallback' && budgetMismatch.length === 0,
    budgetMismatch.length === 0
      ? `3 个样本与 spec 口径逐位一致（码点计数 × 1.2）；spec.rule=${budgetRule?.rule ?? '(缺)'}`
      : `实现与 spec 口径不一致的样本：${budgetMismatch.map((probe) => JSON.stringify(probe.slice(0, 12))).join(',')}；spec.rule=${budgetRule?.rule ?? '(缺)'}`,
  )

  // ---- 候选段的实现归属（T03 边界登记）：kb 类仍是 python，其余必须是 ts ----
  const pushClasses = Array.isArray(spec.injection.pushClasses) ? spec.injection.pushClasses : []
  const tsKinds = new Set(['rule', 'association', 'serendipity', 'situational', 'activity'])
  const pushClassProblems = []
  for (const kind of tsKinds) {
    const entry = pushClasses.find((item) => item.kind === kind)
    if (entry === undefined || entry.impl !== 'ts') pushClassProblems.push(`${kind}=${entry?.impl ?? '(缺)'}`)
  }
  const kbEntry = pushClasses.find((item) => item.kind === 'kb')
  if (kbEntry === undefined || kbEntry.impl !== 'python') pushClassProblems.push(`kb=${kbEntry?.impl ?? '(缺)'}`)
  record(
    'spec.push_classes_declared',
    pushClasses.length > 0 && pushClassProblems.length === 0,
    pushClassProblems.length === 0
      ? `${pushClasses.length} 类：${pushClasses.map((item) => `${item.kind}:${item.impl}`).join(',')}`
      : `不一致：${pushClassProblems.join(',')}`,
  )

  // ---- T04：命令等价表（cli-map）的三条判据 ----
  //
  // 为什么是三条而不是一条：它们分别回答三个不同的问题，任何一条单独都能被"漂亮的自洽"骗过。
  //   ① 覆盖 100%：spec 上登记的叶子 == 生产树里枚举到的叶子（漏一条 = 这条能力在 DSH 里不存在）；
  //   ② 命令面一致：`lib` 里**注册的**命令集合 == spec 里的集合（spec 改了没重建 lib → 红）；
  //   ③ 取值域与替代：impl/kind 合法、非 ts 必有替代、替代能在命令树里解析到、
  //      proxy 必须只读且有源码依据（"未实现"不许是终点）。
  const cliEntries = spec.cliMap.entries
  const productionCli = probeProductionCli(packageRoot)
  if (!productionCli.ok) {
      } else {
    const specPaths = cliEntries.map((entry) => entry.path)
    const absentInSpec = productionCli.leaves.filter((leaf) => !specPaths.includes(leaf))
    const absentInProduction = specPaths.filter((leaf) => !productionCli.leaves.includes(leaf))
    const covered = absentInSpec.length === 0 && absentInProduction.length === 0
    record(
      'spec.cli_map_covers_production',
      covered,
      covered
        ? `覆盖 100%：${productionCli.leaves.length} 条叶子逐名一致` +
            `（一级 ${productionCli.roots.length} / 容器 ${spec.cliMap.groups.length} / 最深 ${productionCli.counts.maxDepth} 级；fish.py=${productionCli.fishPySha256_12}）`
        : `与 p0 不一致：spec 缺 ${absentInSpec.join(',') || '（无）'}；spec 多 ${absentInProduction.join(',') || '（无）'}——跑 gen:spec 重新生成`,
    )
    // 与生成时刻的一致性：命令树变了但 spec 没重生成时，`fishPySha256_12` 会不同。
    const declaredSha = spec.cliMap.source?.fishPySha256_12 ?? ''
    record(
      'spec.cli_map_source_current',
      declaredSha === productionCli.fishPySha256_12,
      `spec 记录 fish.py=${declaredSha} ｜ 现网 ${productionCli.fishPySha256_12}`,
    )
  }

  // ② 命令面注册一致：`lib` 的裁决表 vs spec 的条目（两边必须逐名相等）。
  const libPaths = lib.cliLeafPaths()
  const libRoots = lib.cliRootPaths()
  const specRoots = [...new Set(cliEntries.map((entry) => entry.path.split(' ')[0]))].sort()
  const pathDiff = [
    ...cliEntries.map((entry) => entry.path).filter((leaf) => !libPaths.includes(leaf)),
    ...libPaths.filter((leaf) => !cliEntries.some((entry) => entry.path === leaf)),
  ]
  record(
    'spec.cli_map_surface_matches',
    pathDiff.length === 0 && libRoots.join(',') === specRoots.join(','),
    pathDiff.length === 0
      ? `命令面注册 ${libPaths.length} 条 == spec ${cliEntries.length} 条｜一级 ${libRoots.length} 个逐名一致` +
        `（自有命令 ${lib.NATIVE_COMMANDS.join('/')} 不计入）`
      : `不一致：${pathDiff.length} 处（${pathDiff.slice(0, 6).join(', ')}）｜一级 lib=${libRoots.join(',')} spec=${specRoots.join(',')}`,
  )

  // ②b `--help` 口径（第二来源）：用户看到的"一级命令清单"必须与映射表的一级集合一致。
  const problemCli = productionCli.ok ? productionCli : { ok: false, detail: '命令树探针失败' }
  const fishHomeDir = process.env.FISH_HOME
    ? path.resolve(process.env.FISH_HOME)
    : path.resolve(packageRoot, '..', 'p0')
  const helpProbe = probeProductionHelp(fishHomeDir)
  if (!helpProbe.ok) {
    record('spec.cli_map_matches_help', false, `无法解析 \`fish.py --help\`：${helpProbe.detail}`)
  } else {
    const helpRoots = [...helpProbe.names].sort()
    const mapRoots = [...new Set(cliEntries.map((entry) => entry.path.split(' ')[0]))].sort()
    const onlyHelp = helpRoots.filter((name) => !mapRoots.includes(name))
    const onlyMap = mapRoots.filter((name) => !helpRoots.includes(name))
    record(
      'spec.cli_map_matches_help',
      onlyHelp.length === 0 && onlyMap.length === 0,
      onlyHelp.length === 0 && onlyMap.length === 0
        ? `\`fish.py --help\` 的 ${helpRoots.length} 个一级命令 == 映射表一级 ${mapRoots.length} 个（逐名一致）` +
            (problemCli.ok ? `；命令树 ${problemCli.leaves.length} 条叶子` : '')
        : `差集非空：仅 help 有 ${onlyHelp.join(',') || '（无）'}；仅映射表有 ${onlyMap.join(',') || '（无）'}`,
    )
  }

  // ③ 取值域与替代可解析。
  const cliProblems = []
  const leafSet = new Set(productionCli.ok ? productionCli.leaves : cliEntries.map((entry) => entry.path))
  const toolNames = new Set(tools.map((tool) => tool.name))
  for (const entry of cliEntries) {
    if (!lib.CLI_KIND_VALUES.includes(entry.kind)) cliProblems.push(`${entry.path}:kind=${entry.kind}`)
    if (!lib.CLI_IMPL_VALUES.includes(entry.impl)) cliProblems.push(`${entry.path}:impl=${entry.impl}`)
    if (entry.impl === 'ts' && entry.kind !== 'read') cliProblems.push(`${entry.path}:ts 但 kind=${entry.kind}`)
    if (entry.impl === 'proxy-readonly') {
      if (entry.kind !== 'read') cliProblems.push(`${entry.path}:proxy 但 kind=${entry.kind}`)
      if (entry.evidence === null || String(entry.evidence).trim() === '') cliProblems.push(`${entry.path}:proxy 缺 evidence`)
    }
    if (entry.impl !== 'ts') {
      if (entry.replacement === null) {
        cliProblems.push(`${entry.path}:${entry.impl} 缺替代`)
      } else {
        if (!leafSet.has(entry.replacement.cli)) cliProblems.push(`${entry.path}:替代 ${entry.replacement.cli} 在命令树里不存在`)
        if (entry.replacement.mcpTool !== undefined && !toolNames.has(entry.replacement.mcpTool)) {
          cliProblems.push(`${entry.path}:替代工具 ${entry.replacement.mcpTool} 不在生产工具表`)
        }
      }
    }
  }
  const implSummary = Object.entries(spec.cliMap.implCounts ?? {})
    .map(([impl, n]) => `${impl}=${n}`)
    .join(' ')
  record(
    'spec.cli_map_impl_domain',
    cliEntries.length > 0 && cliProblems.length === 0,
    cliProblems.length === 0
      ? `${cliEntries.length} 条全部合规（${implSummary}）；非 ts 的替代命令全部能在命令树/MCP 工具表里解析到`
      : `越界/悬空 ${cliProblems.length} 处：${cliProblems.slice(0, 6).join(', ')}`,
  )

  // ④ 影子期写边界：所有写类命令都不许是 `ts`（TS 侧没有写路径）。
  const writeAsTs = cliEntries.filter((entry) => entry.kind !== 'read' && entry.impl === 'ts').map((entry) => entry.path)
  record(
    'spec.cli_map_no_write_in_ts',
    writeAsTs.length === 0,
    writeAsTs.length === 0
      ? `写/运维命令 ${cliEntries.filter((entry) => entry.kind !== 'read').length} 条，无一标成 ts（全部拒绝或只给指引）`
      : `写命令被标成 ts（等于偷偷授予写权限）：${writeAsTs.join(', ')}`,
  )

  // ---- T05：语料库 DDL 契约必须与生产同步 ----
  //
  // 为什么单列一条：语料是**独立库**，两侧建库都读 `spec/corpus.schema.sql`。
  // 如果生产调了 DDL 而 spec 没重生成，两侧会"一起用旧结构"，对拍照样绿——
  // 只有对着 `p0/fish_corpus_store.DDL` 现比一次才能发现（与 `spec.tools_match_production`
  // 同一条纪律：自洽证明不了没落后）。
  const corpusSchema = probeCorpusSchema(packageRoot)
  if (!corpusSchema.ok) {
      } else {
    const specDdl = normalizeDdl(readFileSync(path.join(paths.specDir, 'corpus.schema.sql'), 'utf8'))
    const productionDdl = normalizeDdl(corpusSchema.ddl)
    const matched = specDdl === productionDdl
    record(
      'spec.corpus_schema_matches_production',
      matched,
      matched
        ? `语料库 DDL 与生产一致（${corpusSchema.tables.length} 张表 / schema_version=${corpusSchema.schemaVersion}；python ${corpusSchema.python}）`
        : '与 p0/fish_corpus_store.DDL 不一致——跑 gen:spec 重新生成（两侧建库读的就是这份快照）',
    )
  }

  const cutFailures = lib.verifyCutVectors(spec.cut)
  record(
    'spec.cut_vectors_count',
    spec.cut.vectors.length >= EXPECTED.minCutVectors,
    `vectors=${spec.cut.vectors.length}`,
  )
  record(
    'spec.cut_vectors_equal',
    cutFailures.length === 0,
    cutFailures.length === 0
      ? `diff=0（${spec.cut.vectors.length} 组逐 token 全等）`
      : `diff=${cutFailures.length}：${cutFailures.slice(0, 3).map((f) => `${f.id}@${f.firstDiffAt}`).join(',')}`,
  )

  record('spec.capabilities', spec.contract.capabilities.length === EXPECTED.capabilityBlocks, `capabilities=${spec.contract.capabilities.length}`)
  record('spec.impl_matrix_size', spec.contract.implMatrix.length === EXPECTED.capabilityBlocks, `implMatrix=${spec.contract.implMatrix.length}`)
  record('spec.impl_matrix_all_python', spec.contract.implMatrix.every((entry) => entry.impl === 'python'), '影子期必须全 python')
  // 摘要登记数 = 待登记文件数（`SPEC_FILES` 含 contract.json 自身，它不进自己的摘要表）。
  const expectedDigestFiles = lib.SPEC_FILES.filter((name) => name !== 'contract.json').length
  record(
    'spec.spec_files_registered',
    Object.keys(spec.contract.specFiles).length === expectedDigestFiles,
    `registered=${Object.keys(spec.contract.specFiles).length} expected=${expectedDigestFiles}`,
  )

  record(
    'shadow.model_surface_zero',
    lib.MODEL_SURFACE.toolRegistrations === 0 && lib.MODEL_SURFACE.promptInjections === 0,
    `tools=${lib.MODEL_SURFACE.toolRegistrations} injections=${lib.MODEL_SURFACE.promptInjections}`,
  )

  const db = lib.openReadOnly(paths.dbPath)
  let writeRejected = false
  try {
    writeRejected = lib.assertWriteRejected(db)
  } finally {
    db.close()
  }
  record('shadow.db_read_only', writeRejected, writeRejected ? '写入被 SQLite 拒绝' : '⚠ 写入成功——只读约束失效')

  // 负向自证：把 spec 里的期望指纹改坏一个字符，门禁**必须**抛错。
  // 为什么非要有这一步：正向校验在"校验代码被删掉"之后仍然是绿的（它本来就没比对）。
  // 只有负向用例能把"校验还在不在"变成可观测事实——这也是变异 M1 的判据。
  const negative = runNegativeFingerprintTest(lib, paths)
  record('gate.fingerprint_negative_test', negative.threw, negative.detail)
} catch (error) {
  record('x.unexpected', false, error instanceof Error ? `${error.name}: ${error.message}` : String(error))
  if (error && typeof error === 'object' && 'details' in error) {
    record('x.details', false, JSON.stringify(error.details))
  }
}

const failed = checks.filter((check) => !check.ok)
if (process.argv.includes('--json')) {
  process.stdout.write(JSON.stringify({ pass: failed.length === 0, checks }, null, 2))
} else {
  console.log('# check-spec')
  for (const check of checks) console.log(`${check.ok ? 'PASS' : 'FAIL'}  ${check.id}  ${check.detail}`)
  console.log(`# summary: ${checks.length - failed.length}/${checks.length} pass`)
}
process.exitCode = failed.length === 0 ? 0 : 1
