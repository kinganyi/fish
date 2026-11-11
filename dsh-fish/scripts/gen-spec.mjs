#!/usr/bin/env node
/**
 * spec 生成器：把"p0 的事实"机械地抽成 `spec/` 契约层。
 *
 * 三条设计原则：
 *   1. **能 import 就不解析**：工具定义（每个都是一份嵌套很深的 inputSchema）与配置默认值直接从
 *      Python 侧 import 出来（`p0/mcp_server.py` 零第三方依赖），避免"正则抽出来的东西
 *      看起来对、其实丢了一个 required"。
 *   2. **纯函数只有一份**：schema 指纹、切词都来自**已构建的 `lib/`**，不在脚本里重写
 *      ——重写一份就等于给自己发了一张"两边都改对了吗"的试卷。
 *   3. **产物无时间戳**：spec 是要入库的契约，每次生成都变的时间戳只会制造假 diff；
 *      生成时刻记在 NOTES，不记在契约里。
 *
 * 外加一条纪律（T03.5 起）：**唯一需要人工判断的标注（writeClass / idempotent）先校验、后落盘**
 * ——标注不合规就一个文件都不写，别留下"schema.sql 已换、tools.json 还是旧的"这种半更新。
 *
 * 用法：pnpm run gen:spec   （= pnpm run build && node scripts/gen-spec.mjs）
 * 只读：生产库仅以 `mode=ro` 打开；本脚本不写 p0 下任何文件。
 */

import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { DatabaseSync } from 'node:sqlite'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const packageRoot = path.resolve(here, '..')
// 生产树不在本仓时用 FISH_HOME 显式指定（本仓 p0/ 是导出版，不是生成源）。
const p0Root = process.env.FISH_HOME
  ? path.resolve(process.env.FISH_HOME)
  : path.resolve(packageRoot, '..', 'p0')
const specDir = path.join(packageRoot, 'spec')
const libPath = path.join(packageRoot, 'lib', 'index.js')
const pythonBin = process.env.FISH_PYTHON || 'python'

/**
 * 生产库实测的事件类型数（`03-调研-fish全量能力.md` §4）。用于漂移检测。
 * 2026-10-30：21 → **22**，新增 `maintenance.pruned`（fish 侧 `fish_maintenance.py` 的任务台账事件）。
 * 这条常量**故意会红**：fish 每加一种事件，生成器就停下来要人确认，而不是静默把新事件漏进 spec。
 */
const EXPECTED_OBSERVED_EVENT_COUNT = 22

/** 冻结的切词向量输入（期望值由 Python 现场算，不由 TS 算）。 */
const CUT_VECTOR_INPUTS = [
  '中文重叠 bigram 切分',
  '协作约定',
  '记',
  'CR-12',
  'v1.0.0',
  'name_alias 表',
  'ANYI-261025-01',
  '「生活」是默认项目：用户没指定具体项目时的个人生活类对话落这里',
  '中文 English 混合 2026-10-27T13:37:00Z',
  '半角，标点。分组！问号？',
  'ｆｕｌｌｗｉｄｔｈ 全角拉丁不做切分',
  'emoji 🐟 与文本',
  '',
  '   ',
  'pure-latin-tokens-here',
  'abc123def',
  '一件事',
  '两件事',
  '三件事呢',
  '重叠滑窗必须逐位对齐',
  '记忆编码由系统分配',
  'kb:安逸知识库:10-漫剧/方法论/即梦图片4.5模型 提示词手册.md',
  'EVAL-261025-04 与 EVAL-261026-06',
  'tiktoken cl100k_base 中文≈1.2 token/字',
]

/**
 * 每个工具的**写入类别**标注：整份 spec 里**唯一需要人工判断**的两列。
 *
 * 为什么放在生成器而不是从 Python 侧抽：Python 的工具表里没有这两个字段（它们描述的是
 * "影子期 TS 侧敢不敢实现"，不是 Python 的行为）。所以这里必须有人逐条裁决，
 * 而裁决结果要被机器校验——取值域来自 `src/ports/spec.ts` 的 `WRITE_CLASSES` /
 * `IDEMPOTENT_VALUES`（唯一真源，生成器与 `check-spec` 都读同一份）。
 *
 * 影子期铁律：只有 `none`（读）与 `audit`（只追加审计日志）允许在 TS 侧实现。
 */
const WRITE_CLASS = {
  remember: ['append', 'no'],
  recall: ['audit', 'yes'],
  neighbors: ['audit', 'yes'],
  resolve_code: ['none', 'yes'],
  read_body: ['audit', 'yes'],
  add_project: ['state', 'no'],
  supersede: ['transaction', 'no'],
  whoami: ['none', 'yes'],
  register: ['state', 'yes'],
  rename_citizen: ['state', 'yes'],
  close_project: ['state', 'yes'],
  reopen_project: ['state', 'yes'],
  inject: ['none', 'yes'],
  healthcheck: ['none', 'yes'],
  session_start: ['transaction', 'no'],
  board: ['state', 'mode-dependent'],
  kb: ['state', 'mode-dependent'],
  kb_write: ['transaction', 'yes'],
  // T03（2026-10-27）· 对话语料只读面。语义判据（读的是生产树，不是猜的）：
  //   · 动作面 = `CORPUS_TOOL_ACTIONS = ("search", "read", "stats")`（p0/mcp_server.py:125，
  //     入参 enum 与实现的分支判断都取自它）；`ingest/forget/rebuild` 只在 CLI
  //     （fish_corpus.py:1544-1592），MCP 面根本给不到 —— 所以模型侧没有写数据面；
  //   · `stats` 连语料库都只按 `readonly=True` 打开（fish_corpus.py:596-610），无写入；
  //   · `search` 只追加审计 `corpus_access_log`（fish_corpus.py:954-959 → `_log_access`
  //     859-889；写审计失败连主流程都不影响）；
  //   · `read` 另按**精确编码**回填推送采纳（p0/mcp_server.py:2056-2062），与 `read_body`
  //     走的是同一条 `_accept_push`（分类已标注 audit）——同一类"只留审计与度量痕迹"的副作用。
  // 故 writeClass=audit；幂等判 yes（同一入参重放不改变可观察状态：审计只追加、
  // 采纳回填有 `reaction IS NULL` 前置条件）。
  corpus: ['audit', 'yes'],
}

/**
 * 校验「标注表 ↔ 生产工具」：三条同时成立，否则**在写任何文件之前**失败。
 *
 *   ① 每个生产工具都有标注——少了就是"新工具悄悄进 spec，writeClass 是编造的"；
 *      T03 的 `corpus` 正是以这个形态把 `gen:spec` 卡住的（NOTES-T03 §5-D11）；
 *   ② 每条标注在生产侧都还存在——多了就是"登记表继续声明一个不存在的契约"；
 *   ③ 两个值都在取值域内——否则 spec 里会出现下游查不到的类别。
 */
function validateToolAnnotations(productionTools, libApi) {
  const productionNames = productionTools.map((tool) => tool.name)
  const annotatedNames = Object.keys(WRITE_CLASS)

  const undocumented = productionNames.filter((name) => !annotatedNames.includes(name))
  if (undocumented.length > 0) {
    throw new Error(
      `工具 ${undocumented.join(', ')} 没有 writeClass 标注：新增工具必须显式登记写入类别` +
        `（取值域 ${libApi.WRITE_CLASSES.join('/')}；影子期只允许 none/audit 在 TS 侧）`,
    )
  }
  const stale = annotatedNames.filter((name) => !productionNames.includes(name))
  if (stale.length > 0) {
    throw new Error(
      `writeClass 标注里有 p0 侧已不存在的工具：${stale.join(', ')}——删掉这条标注，` +
        '否则登记表会继续声明一个不存在的契约',
    )
  }
  for (const [name, [writeClass, idempotent]] of Object.entries(WRITE_CLASS)) {
    if (!libApi.WRITE_CLASSES.includes(writeClass)) {
      throw new Error(
        `工具 ${name} 的 writeClass=${JSON.stringify(writeClass)} 不在取值域 ` +
          `${libApi.WRITE_CLASSES.map((value) => JSON.stringify(value)).join(' | ')} 内`,
      )
    }
    if (!libApi.IDEMPOTENT_VALUES.includes(idempotent)) {
      throw new Error(
        `工具 ${name} 的 idempotent=${JSON.stringify(idempotent)} 不在取值域 ` +
          `${libApi.IDEMPOTENT_VALUES.map((value) => JSON.stringify(value)).join(' | ')} 内`,
      )
    }
  }
  return WRITE_CLASS
}

/** 固定缩进 + 结尾换行，保证重复生成字节一致。 */
function writeJson(relativePath, value) {
  const fullPath = path.join(specDir, relativePath)
  mkdirSync(path.dirname(fullPath), { recursive: true })
  writeFileSync(fullPath, `${JSON.stringify(value, null, 2)}\n`, 'utf8')
  return fullPath
}

function writeText(relativePath, content) {
  const fullPath = path.join(specDir, relativePath)
  mkdirSync(path.dirname(fullPath), { recursive: true })
  writeFileSync(fullPath, content, 'utf8')
  return fullPath
}

function sha256File(filePath) {
  return createHash('sha256').update(readFileSync(filePath)).digest('hex')
}

/** 键排序后序列化（让 spec 的字段顺序与生成顺序无关）。 */
function sortedRecord(entries) {
  const out = {}
  for (const key of Object.keys(entries).sort()) out[key] = entries[key]
  return out
}

// ---------------------------------------------------------------- 外部事实

/** 调用 Python 抽取 p0 事实，返回解析后的 JSON。 */
function dumpP0Facts() {
  const script = path.join(packageRoot, 'scripts', 'probe', 'dump-p0-facts.py')
  const child = spawnSync(pythonBin, [script], {
    encoding: 'utf8',
    cwd: packageRoot,
    maxBuffer: 64 * 1024 * 1024,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
  })
  if (child.error) throw new Error(`python 启动失败: ${child.error.message}`)
  if (child.status !== 0) throw new Error(`dump-p0-facts.py 退出码 ${child.status}: ${(child.stderr || '').trim()}`)
  try {
    return JSON.parse(child.stdout)
  } catch (error) {
    throw new Error(`dump-p0-facts.py 输出不是 JSON: ${error.message}`)
  }
}

/**
 * 枚举 `fish.py` 的命令树（T04）：**机器事实**，与人工裁决表交叉校验后进 `spec/cli-map.json`。
 *
 * 为什么单独一个探针脚本：`--help` 文本不是契约（换行宽度、排序、缩写都会变），
 * 真正的事实是 argparse 对象树。探针会拦截 `parse_args`，**不执行任何子命令**。
 */
function dumpCliEnumeration() {
  const script = path.join(packageRoot, 'scripts', 'probe', 'dump-cli.py')
  const child = spawnSync(pythonBin, [script], {
    encoding: 'utf8',
    cwd: packageRoot,
    maxBuffer: 32 * 1024 * 1024,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
  })
  if (child.error) throw new Error(`python 启动失败: ${child.error.message}`)
  if (child.status !== 0) throw new Error(`dump-cli.py 退出码 ${child.status}: ${(child.stderr || '').trim()}`)
  try {
    return JSON.parse(child.stdout)
  } catch (error) {
    throw new Error(`dump-cli.py 输出不是 JSON: ${error.message}`)
  }
}

/** 让 Python 算切词向量（oracle = 生产实现；TS 只是被验证方）。 */
function dumpCutVectors(inputs) {
  const script = path.join(packageRoot, 'scripts', 'probe', 'cut_py.py')
  const tmpFile = path.join(packageRoot, '.tmp', 'cut-vectors-input.json')
  mkdirSync(path.dirname(tmpFile), { recursive: true })
  writeFileSync(tmpFile, JSON.stringify(inputs, null, 2), 'utf8')
  const child = spawnSync(pythonBin, [script, tmpFile], {
    encoding: 'utf8',
    cwd: packageRoot,
    maxBuffer: 16 * 1024 * 1024,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
  })
  if (child.error) throw new Error(`python 启动失败: ${child.error.message}`)
  if (child.status !== 0) throw new Error(`cut_py.py 退出码 ${child.status}: ${(child.stderr || '').trim()}`)
  return JSON.parse(child.stdout)
}

/**
 * 导出语料库 DDL（T05）：语料是**独立库**，它的表结构不在 `schema.sql` 里。
 *
 * 为什么要进 spec 而不是让两侧各抄一份：两侧建库都照抄**同一份**契约，才谈得上
 * "对拍比的是实现差异"；抄两份的结局是"生产加了列（或改了约束），两份快照都还在旧结构"
 * ——两边一起错、对拍照样绿。
 */
function dumpCorpusDdl() {
  const script = path.join(packageRoot, 'scripts', 'probe', 'dump-corpus-ddl.py')
  const child = spawnSync(pythonBin, [script], {
    encoding: 'utf8',
    cwd: packageRoot,
    maxBuffer: 16 * 1024 * 1024,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
  })
  if (child.error) throw new Error(`python 启动失败: ${child.error.message}`)
  if (child.status !== 0) throw new Error(`dump-corpus-ddl.py 退出码 ${child.status}: ${(child.stderr || '').trim()}`)
  try {
    return JSON.parse(child.stdout)
  } catch (error) {
    throw new Error(`dump-corpus-ddl.py 输出不是 JSON: ${error.message}`)
  }
}

// ---------------------------------------------------------------- 事件抽取

/** 找到与 `openIndex`（指向 `(`）配对的右括号下标；跳过字符串与注释。 */
function findMatchingParen(source, openIndex) {
  let depth = 0
  for (let i = openIndex; i < source.length; i += 1) {
    const ch = source[i]
    if (ch === '"' || ch === "'") {
      const quote = ch
      i += 1
      while (i < source.length) {
        if (source[i] === '\\') i += 2
        else if (source[i] === quote) break
        else i += 1
      }
      continue
    }
    if (ch === '#') {
      while (i < source.length && source[i] !== '\n') i += 1
      continue
    }
    if (ch === '(' || ch === '[' || ch === '{') depth += 1
    else if (ch === ')' || ch === ']' || ch === '}') {
      depth -= 1
      if (depth === 0) return i
    }
  }
  return -1
}

/** 按顶层逗号切参数（跳过字符串/注释/嵌套括号）。 */
function splitTopLevel(inner) {
  const parts = []
  let depth = 0
  let start = 0
  for (let i = 0; i < inner.length; i += 1) {
    const ch = inner[i]
    if (ch === '"' || ch === "'") {
      const quote = ch
      i += 1
      while (i < inner.length) {
        if (inner[i] === '\\') i += 2
        else if (inner[i] === quote) break
        else i += 1
      }
      continue
    }
    if (ch === '#') {
      while (i < inner.length && inner[i] !== '\n') i += 1
      continue
    }
    if (ch === '(' || ch === '[' || ch === '{') depth += 1
    else if (ch === ')' || ch === ']' || ch === '}') depth -= 1
    else if (ch === ',' && depth === 0) {
      parts.push(inner.slice(start, i))
      start = i + 1
    }
  }
  const tail = inner.slice(start)
  if (tail.trim() !== '') parts.push(tail)
  return parts.map((part) => part.trim())
}

/** 取字典字面量的顶层键（`{ "a": 1, "b": {"c": 2} }` → ['a','b']）。 */
function dictTopLevelKeys(literal) {
  const text = literal.trim()
  if (!text.startsWith('{')) return { keys: [], isLiteral: false }
  const end = findMatchingParen(text, 0)
  const inner = text.slice(1, end === -1 ? text.length : end)
  const keys = []
  let depth = 0
  let i = 0
  let expectKey = true
  while (i < inner.length) {
    const ch = inner[i]
    if (ch === '"' || ch === "'") {
      const quote = ch
      const startIdx = i
      i += 1
      while (i < inner.length) {
        if (inner[i] === '\\') i += 2
        else if (inner[i] === quote) break
        else i += 1
      }
      const raw = inner.slice(startIdx + 1, i)
      // 深度 0 且此时期望一个键 → 这个字符串就是键名。
      if (depth === 0 && expectKey) {
        keys.push(raw)
        expectKey = false
      }
      i += 1
      continue
    }
    if (ch === '{' || ch === '(' || ch === '[') depth += 1
    else if (ch === '}' || ch === ')' || ch === ']') depth -= 1
    else if (ch === ':' && depth === 0) expectKey = false
    else if (ch === ',' && depth === 0) expectKey = true
    i += 1
  }
  return { keys, isLiteral: true }
}

/** 解析 `_EVENT_BY_PROJECT_STATUS = {...}` 这类的"事件名映射"。 */
function parseEventMapping(source, varName) {
  const marker = `${varName} = {`
  const at = source.indexOf(marker)
  if (at === -1) return {}
  const openBrace = source.indexOf('{', at)
  const closeBrace = findMatchingParen(source, openBrace)
  const literal = source.slice(openBrace, closeBrace + 1)
  const mapping = {}
  const re = /"([A-Za-z0-9_.]+)"\s*:\s*"([A-Za-z0-9_.]+)"/g
  let m
  while ((m = re.exec(literal)) !== null) mapping[m[1]] = m[2]
  return mapping
}

/** 从源码里抽出所有 `append_event(...)` 调用点。 */
function extractEventSites(source, fileName, extraMappings = {}) {
  const sites = []
  const marker = 'append_event('
  let index = source.indexOf(marker)
  while (index !== -1) {
    const openParen = index + marker.length - 1
    const closeParen = findMatchingParen(source, openParen)
    if (closeParen === -1) break
    const args = splitTopLevel(source.slice(openParen + 1, closeParen))
    const typeExpr = (args[1] ?? '').trim()
    const payloadLiteral = (args[4] ?? '').trim()
    const line = source.slice(0, index).split('\n').length
    const payload = dictTopLevelKeys(payloadLiteral)

    let types = []
    const literalMatch = /^"([A-Za-z0-9_.]+)"$/.exec(typeExpr)
    if (literalMatch) {
      types = [literalMatch[1]]
    } else {
      // `_EVENT_BY_PROJECT_STATUS[target]` 这类：展开成全部可能值。
      for (const [alias, name] of Object.entries(extraMappings)) {
        if (typeExpr.includes(alias)) types.push(name)
      }
    }
    for (const type of types) {
      sites.push({
        type,
        requiredPayload: payload.keys,
        payloadKeys: payload.keys,
        sourceFile: fileName,
        sourceLine: line,
        payloadIsLiteral: payload.isLiteral,
      })
    }
    index = source.indexOf(marker, closeParen)
  }
  return sites
}

/** 合并同一事件类型的多个调用点（不同分支写不同字段）。 */
function mergeEventSites(sites) {
  const byType = new Map()
  for (const site of sites) {
    const existing = byType.get(site.type)
    if (existing === undefined) {
      byType.set(site.type, { ...site, payloadKeys: [...site.payloadKeys], sourceLines: [`${site.sourceFile}:${site.sourceLine}`] })
      continue
    }
    for (const key of site.payloadKeys) if (!existing.payloadKeys.includes(key)) existing.payloadKeys.push(key)
    existing.sourceLines.push(`${site.sourceFile}:${site.sourceLine}`)
  }
  return byType
}

// ---------------------------------------------------------------- 主流程

function main() {
  if (!existsSync(libPath)) {
    throw new Error(`找不到构建产物 ${libPath}：先跑 pnpm run build（本脚本复用 lib/ 里的唯一实现）`)
  }
  if (!existsSync(path.join(p0Root, 'fish.py'))) {
    throw new Error(`找不到 ${p0Root}/fish.py：确认 dsh-fish 与 p0 是兄弟目录`)
  }
  mkdirSync(specDir, { recursive: true })

  const facts = dumpP0Facts()
  const fishSource = readFileSync(path.join(p0Root, 'fish.py'), 'utf8')
  const mcpSource = readFileSync(path.join(p0Root, 'mcp_server.py'), 'utf8')

  // ---------- 0) 人工标注先校验（**落盘之前**）
  // 为什么提到最前面：标注错了就一个文件都不该写。若等到第 3 步才抛，schema.sql 与
  // events.json 已经落盘而 contract.json 还是上一版摘要——仓库会停在"摘要不一致"的半更新态，
  // 下一个人看到的第一个红灯就不再是"标注错了"。
  validateToolAnnotations(facts.tools, libModule)

  // ---------- 0b) CLI 裁决 × 命令树交叉校验（T04，同样在**落盘之前**）
  // `buildCliMap` 自己会抛：漏一条命令 / 多一条不存在的命令 / 取值域越界 /
  // proxy 标的不是只读 / 替代命令解析不到。它抛了，下面一个文件都不会写。
  const cliEnumeration = dumpCliEnumeration()
  const cliMap = libModule.buildCliMap(cliEnumeration, {
    knownTools: facts.tools.map((tool) => tool.name),
  })

  // ---------- 1) schema.sql + 指纹（用构建产物里的唯一实现）
  const lib = libModule
  const dbPath = path.join(p0Root, 'data', 'index.sqlite')
  const db = lib.openReadOnly(dbPath)
  let schemaSql
  let fingerprint
  try {
    const objects = db
      .prepare("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name")
      .all()
    fingerprint = lib.computeSchemaFingerprint(db)
    schemaSql = renderSchemaSql(objects, {
      dbPath,
      python: facts.python,
      fingerprint: fingerprint.fingerprint,
    })
  } finally {
    db.close()
  }
  writeText('schema.sql', schemaSql)
  writeJson('schema.fingerprint.json', fingerprint)

  // ---------- 2) events.json
  const projectStatusMapping = parseEventMapping(fishSource, '_EVENT_BY_PROJECT_STATUS')
  const sites = [
    ...extractEventSites(fishSource, 'fish.py', projectStatusMapping),
    ...extractEventSites(mcpSource, 'mcp_server.py', projectStatusMapping),
  ]
  const merged = mergeEventSites(sites)

  // 库内实测的事件类型是"这份清单是否漏了东西"的唯一独立证据。
  const observedDb = lib.openReadOnly(dbPath)
  let observed
  try {
    observed = observedDb.prepare('SELECT type, COUNT(*) AS n FROM events GROUP BY type ORDER BY type').all()
  } finally {
    observedDb.close()
  }
  const observedTypes = observed.map((row) => row.type)
  if (observedTypes.length !== EXPECTED_OBSERVED_EVENT_COUNT) {
    throw new Error(
      `事件类型漂移：库内实测 ${observedTypes.length} 种（冻结值 ${EXPECTED_OBSERVED_EVENT_COUNT}）。` +
        `清单：${observedTypes.join(', ')}。请人工复核后更新 EXPECTED_OBSERVED_EVENT_COUNT 与 03 号调研。`,
    )
  }
  for (const type of observedTypes) {
    if (!merged.has(type)) {
      // 由运维脚本裸 INSERT 写入（gov_*/merge_cdql.py），没有 append_event 调用点。
      merged.set(type, {
        type,
        requiredPayload: [],
        payloadKeys: [],
        sourceFile: 'p0/_audit（由 gov_*/merge_cdql.py 以裸 INSERT 写入，无 append_event 调用点）',
        sourceLine: 0,
        sourceLines: [],
      })
    }
  }
  const eventList = [...merged.values()]
    .map((entry) => ({
      type: entry.type,
      requiredPayload: entry.requiredPayload,
      payloadKeys: entry.payloadKeys.filter((key, i, arr) => arr.indexOf(key) === i),
      sourceLines: entry.sourceLines.length > 0 ? entry.sourceLines : [`${entry.sourceFile}:${entry.sourceLine}`],
      observedInDb: observedTypes.includes(entry.type),
    }))
    .sort((a, b) => (a.type < b.type ? -1 : a.type > b.type ? 1 : 0))
  writeJson('events.json', {
    specVersion: 'v0',
    source: {
      files: ['p0/fish.py', 'p0/mcp_server.py'],
      method: '静态解析 append_event(...) 调用点（含 _EVENT_BY_PROJECT_STATUS 展开）+ 库内 SELECT DISTINCT type 交叉核对',
      observedTypeCount: observedTypes.length,
      observedTypes,
      python: facts.python,
    },
    declaredTypeCount: eventList.length,
    types: eventList,
  })

  // ---------- 3) tools.json
  // 标注表在模块顶部（`WRITE_CLASS`），第 0 步已校验过"标注 ↔ 生产工具"的双向覆盖与取值域，
  // 所以这里取到的每一对都必然存在且合法。
  const tools = facts.tools.map((tool) => {
    const [writeClass, idempotent] = WRITE_CLASS[tool.name]
    return {
      name: tool.name,
      title: tool.title,
      description: tool.description,
      inputSchema: tool.inputSchema,
      writeClass,
      idempotent,
      impl: 'python',
      pythonSymbol: `FishServer.tool_${tool.name}`,
    }
  })
  writeJson('tools.json', {
    specVersion: 'v0',
    source: {
      files: ['p0/mcp_server.py'],
      method: 'import mcp_server.TOOLS（Python 侧原样导出，不解析字面量）',
      serverInfo: facts.serverInfo,
      tokenArg: facts.tokenArg,
      resourceCount: facts.resources.length,
      python: facts.python,
    },
    declaredToolCount: tools.length,
    tools,
  })

  // ---------- 4) injection.json
  const config = facts.config
  const budgetKeys = ['inject.budget_tokens', 'inject.board_max_ratio', 'inject.rules_per_source', 'push.open_budget_tokens']
  for (const key of budgetKeys) {
    if (config[key] === undefined) throw new Error(`缺少配置键 ${key}：injection spec 的预算来源变了，请复核`)
  }
  const bodyOrder = ['status', 'recent', 'board', 'rules', 'attention', 'lessons', 'push', 'index']
  // 裁剪顺序（`build_injection_pack` 的实际 while 链）：数字越大越先被丢。
  const TRIM_RANK = {
    push: 9,
    index: 8,
    attention: 7,
    lessons: 6,
    rules: 5,
    board: 4,
    recent: 3,
    status: 1,
  }
  const SECTION_SOURCE = {
    status: 'code_index(active) + project_stints/_resolve_injection_project + citizens_archive（休眠计数）',
    recent: "code_index WHERE project=? AND dormant=0 AND status='active' ORDER BY code DESC LIMIT 3",
    board: 'board_block(boards/board_posts/presence/tool_activity)',
    rules: 'build_short_rules_block(code_index mem_type=rule)',
    attention: 'build_attention_block(code_index + memory_access_log)',
    lessons: 'build_lesson_block(code_index mem_type=lesson)',
    push: 'build_push_block(push_log/links/memory_access_log)',
    index: 'code_registry JOIN code_index + decay.*(config) + 操作指引常量',
  }
  const sections = [
    {
      name: 'l0',
      kind: 'l0',
      order: 0,
      source: 'citizens + resolve_owner + 记忆根目录 + 三条铁律（永不裁剪到消失，只降级为 l0_min）',
      budgetKeys: [],
      trimRank: 2,
      declared: false,
    },
    ...bodyOrder.map((sectionName, i) => ({
      name: sectionName,
      kind: 'section',
      order: i + 1,
      source: SECTION_SOURCE[sectionName] ?? '（未登记来源，需补 spec）',
      budgetKeys: sectionName === 'board' ? ['inject.board_max_ratio'] : sectionName === 'rules' ? ['inject.rules_per_source'] : [],
      trimRank: TRIM_RANK[sectionName] ?? 1,
      declared: facts.injectionSections.includes(sectionName),
    })),
  ]
  for (const declaredSection of facts.injectionSections) {
    if (!sections.some((section) => section.name === declaredSection)) {
      throw new Error(`INJECTION_SECTIONS 里的 ${declaredSection} 没有在 spec 装配顺序中登记`)
    }
  }
  // 预算**计数口径**（T03 · D3 裁决）：写进 spec 而不是留在两份实现的注释里。
  //
  // 为什么必须由生成器产出：手写进 spec 会被下一次生成覆盖掉（"改了但没人知道"），
  // 而这条口径是**契约**——它决定预算裁剪在哪儿触发，两侧必须一致。
  // `scripts/check-spec.mjs` 拿它与实现逐位比对：把这里改成 tiktoken 而不改实现 → 判据红。
  const budgetRule = {
    rule: 'fallback',
    formula: 'int(len(text) * 1.2)',
    countUnit: 'unicode-code-point',
    tokenizer: 'none',
    rationale:
      '影子期 TS 侧没有 cl100k_base，无法与生产 tiktoken 精确计数对等；' +
      '所以两侧统一走 Python count_tokens 的 fallback 分支，让"预算裁剪在哪儿触发"也变成可对拍的事实',
    contractBreach:
      '换心期若引入真实 tokenizer，**必须同时改 spec 的 budgetRule 与两侧实现**（Python 的 count_tokens 与 TS 的 countTokens）；' +
      '只改一侧或只改 spec 都视为契约破坏，`scripts/check-spec.mjs` 会红',
    knownCost: '与生产 tiktoken 精确计数在边界上可能差 1 条候选——只影响裁剪触发点，不影响未触发裁剪时的包正文',
  }
  writeJson('injection.json', {
    specVersion: 'v0',
    source: {
      files: ['p0/fish.py'],
      function: 'build_injection_pack',
      declaredSections: facts.injectionSections,
      declaredSectionNote: facts.injectionSectionNote,
      sectionAliases: sortedRecord(facts.injectionSectionAliases),
      python: facts.python,
    },
    budget: sortedRecord(
      Object.fromEntries(budgetKeys.map((key) => [key, { default: config[key].default, type: config[key].type, key }])),
    ),
    budgetRule,
    tail: {
      lines: 2,
      truncationKeys: facts.truncationCountKeys,
      labels: sortedRecord(facts.injectionTailLabels),
      fullContainsPhrase: facts.injectionTailFullContains,
      worstAsOf: facts.injectionTailWorstAsOf,
      note: '包尾两行 = 截断声明 + as-of；两者都必须算进预算（`injection_tail_worst()` 现算，不写常量）',
    },
    trimOrder: ['push', 'index', 'attention', 'lessons', 'rules', 'board', 'recent', 'l0-iron-rules', 'status'],
    sections,
    // 候选段里各类的**实现归属**（T03 边界登记）：这是唯一仍未在 TS 侧实现的类，
    // 写明在这里是为了让它可被机器检查（"少了一个类"不该只写在 NOTES 里）。
    pushClasses: [
      { kind: 'rule', source: 'code_index(未闭合) + _open_loop_rows', impl: 'ts' },
      { kind: 'association', source: 'links + code_index(同项目)', impl: 'ts' },
      { kind: 'serendipity', source: 'links + code_index(跨项目)', impl: 'ts' },
      { kind: 'situational', source: 'memory_access_log(10 分钟) + board_seen', impl: 'ts' },
      { kind: 'activity', source: 'tool_activity + presence(声明面)', impl: 'ts' },
      { kind: 'kb', source: 'kb_vault + 文件系统检索（fish_kb._kb_search_multi）', impl: 'python' },
    ],
  })

  // ---------- 4b) cli-map.json（T04：命令等价表，**枚举 × 人工裁决**的产物）
  // 三条纪律都落在产物里，不只写在文档里：
  //   · `counts.leaves` 必须等于 `entries.length`（加载时校验，手改条数会红）；
  //   · 每条非 `ts` 命令都带 `replacement`，且 `replacement.cli` 必须在命令树里解析得到；
  //   · `impl=proxy-readonly` 必须 `kind=read` 且带 `evidence`（源码位置）。
  writeJson('cli-map.json', cliMap)

  // ---------- 4c) corpus.schema.sql（T05：语料库是独立库，它的 DDL 也是契约）
  //
  // 两侧建库都读**这一份**（`test/conformance/run.mjs` / `run.py`），所以"生产改了 DDL"
  // 必须能被机器看见：`check-spec` 的 `spec.corpus_schema_matches_production` 每次重跑
  // 都对着 `p0/fish_corpus_store.DDL` 再比一次，差异即红（补救动作 = 重跑 gen:spec）。
  const corpusDdl = dumpCorpusDdl()
  const corpusHeader = [
    '-- 语料库（corpus.sqlite）DDL 快照（只读导出，勿手工编辑）',
    '-- 来源：p0/fish_corpus_store.py 的 DDL 常量（import 后原样导出，不解析字面量）',
    `-- 生成：scripts/gen-spec.mjs ｜ Python ${corpusDdl.python} ｜ schema_version=${corpusDdl.schemaVersion}`,
    `-- 表：${corpusDdl.tables.join(', ')}`,
    '-- 口径：语料库与主库零耦合（主库 DDL 在 schema.sql）；本文件只描述 corpus.sqlite。',
    '',
  ].join('\n')
  writeText('corpus.schema.sql', `${corpusHeader}${String(corpusDdl.ddl).trimEnd()}\n`)

  // ---------- 5) layout/cut.json（规则 + Python 冻结的向量）
  const cutPython = dumpCutVectors(CUT_VECTOR_INPUTS)
  const vectors = CUT_VECTOR_INPUTS.map((input, i) => ({
    id: `v${String(i + 1).padStart(2, '0')}`,
    input,
    tokens: cutPython.tokens[i],
  }))
  writeJson('layout/cut.json', {
    specVersion: 'v0',
    rules: lib.DEFAULT_CUT_RULES,
    match: {
      quoteEveryToken: true,
      joiner: ' AND ',
      emptyResult: null,
    },
    vectors,
    source: {
      files: ['p0/fish.py'],
      function: 'index_tokens / fts_text / fts_match_expr（ADR-005）',
      oracle: `python ${cutPython.python} 现场计算（${cutPython.module}）`,
      note: '向量期望值由 Python 冻结；TS 侧只许被验证，不许自证',
    },
  })

  // ---------- 6) contract.json（最后写：它登记前面所有文件的摘要）
  const capabilityBlocks = [
    ['memory-write', '记忆写（remember/withdraw/supersede/owner-fix）'],
    ['memory-read', '记忆读（recall/read_body/resolve_code/list）'],
    ['links', '关联（写时建边 + neighbors 五策略）'],
    ['decay', '衰减与归档（forgetting/archive）'],
    ['search', '检索（FTS5 中文 bigram + 过滤下推）'],
    ['injection', '注入包（L0 + 8 段 + 预算裁剪 + 包尾两行）'],
    ['identity', '身份与凭证（register/whoami/session_start）'],
    ['board', '看板与撞车（board/presence/collision）'],
    ['push', '推送（候选 + 四时机 + 节流）'],
    ['kb', '知识库（kb/kb_write）'],
    // T03.5（2026-10-27）：`corpus` 工具已进 MCP 面（工具数 18 → 19），语料层不再是"分支未合并"
    // ——生产树里 `fish_corpus.py` 与独立库 `corpus.sqlite` 都在跑。措辞跟着事实走，别让契约说谎。
    ['corpus', '语料层（corpus 只读面 + fish_corpus 独立库 corpus.sqlite）'],
    ['layout', '布局渲染（layout render/verify）'],
    ['cli', 'CLI 运维（fish.py 全部叶子 → /fish 命令面）'],
    ['hosts', '多宿主 hooks（probe/install/health）'],
    ['project-alias', '项目与别名（project/alias/workspace）'],
    ['config', '配置开关（config 表 + 56 键）'],
    ['backup', '备份恢复（backup/restore/snapshot）'],
    ['healthcheck', '健康检查（healthcheck/doctor/verify）'],
  ]
  const digests = {
    'schema.sql': sha256File(path.join(specDir, 'schema.sql')),
    'schema.fingerprint.json': sha256File(path.join(specDir, 'schema.fingerprint.json')),
    'events.json': sha256File(path.join(specDir, 'events.json')),
    'tools.json': sha256File(path.join(specDir, 'tools.json')),
    'injection.json': sha256File(path.join(specDir, 'injection.json')),
    'layout/cut.json': sha256File(path.join(specDir, 'layout', 'cut.json')),
    'cli-map.json': sha256File(path.join(specDir, 'cli-map.json')),
    'corpus.schema.sql': sha256File(path.join(specDir, 'corpus.schema.sql')),
  }
  const sourceRevision = sha256File(path.join(p0Root, 'fish.py')).slice(0, 12)
  writeJson('contract.json', {
    contractVersion: 'v0',
    minHostVersion: '0.2.0-rc.2',
    generatedBy: 'scripts/gen-spec.mjs',
    source: {
      fishPy: 'p0/fish.py',
      schemaDb: 'p0/data/index.sqlite',
      fishPySha256_12: sourceRevision,
      mcpServerInfo: facts.serverInfo,
      python: facts.python,
    },
    implMatrix: capabilityBlocks.map(([id, name]) => ({ id, name, impl: 'python', note: '影子期以 Python 为准；换心期逐块升 shadow → ts' })),
    capabilities: capabilityBlocks.map(([id]) => id),
    concurrency: {
      journalMode: 'wal',
      busyTimeoutMs: 5000,
      beginMode: 'IMMEDIATE',
      longTransactionLimitMs: 50,
      retryBackoffMs: [50, 150, 400],
    },
    shadowPolicy: {
      toolRegistrations: 0,
      promptInjections: 0,
      dbOpenMode: 'ro',
      writePaths: '全部禁用（生成命令 + 人工执行）',
      reason: '与 mcp__fish__* 和 hooks.json 4 条并存：同层同名工具会抛错、双份注入无法归因、双写者会互相覆盖',
    },
    specFiles: digests,
  })

  // ---------- 汇总
  const lines = [
    `spec 生成完成：${specDir}`,
    `  对象计数：${JSON.stringify(fingerprint.counts)}`,
    `  fingerprint=${fingerprint.fingerprint.slice(0, 16)}… business=${fingerprint.businessFingerprint.slice(0, 12)}…`,
    `  事件：声明 ${eventList.length} 种（库内观测 ${observedTypes.length} 种）`,
    `  工具：${tools.length} 个`,
    `  注入段：${sections.length} 段（L0 + ${sections.length - 1}）`,
    `  切词向量：${vectors.length} 组`,
    `  CLI：一级 ${cliMap.counts.level1AsCommands} 个 / 叶子 ${cliMap.counts.leaves} 条 / 容器 ${cliMap.counts.groups} 个` +
      `（${Object.entries(cliMap.implCounts).map(([impl, n]) => `${impl}=${n}`).join(' ')}）`,
    `  schema.sql：${schemaSql.split('\n').length} 行`,
    `  corpus.schema.sql：${String(corpusDdl.ddl).trimEnd().split('\n').length} 行（${corpusDdl.tables.length} 张表 / schema_version=${corpusDdl.schemaVersion}）`,
  ]
  console.log(lines.join('\n'))
}

// ---------- schema.sql 渲染

function renderSchemaSql(objects, meta) {
  // FTS5 的影子表由 `CREATE VIRTUAL TABLE` 自动创建，**不能**出现在可重建 DDL 里：
  // 否则按 schema.sql 造库时会撞 `object name reserved for internal use` /
  // `table 'xxx_config' already exists`（2026-10-30 对拍暴露，fish 新增 kb_fts 后触发）。
  const SHADOW_PREFIXES = ['memory_fts_', 'kb_fts_']
  const isShadow = (name) => SHADOW_PREFIXES.some((prefix) => name.startsWith(prefix)) || name === 'sqlite_sequence'
  const order = ['table', 'view', 'index', 'trigger']
  const header = [
    '-- fish 主库 DDL 快照（只读导出，勿手工编辑）',
    `-- 来源：${meta.dbPath}`,
    `-- 生成：scripts/gen-spec.mjs ｜ Python ${meta.python}`,
    '-- 指纹：${meta.fingerprint}'.replace('${meta.fingerprint}', meta.fingerprint),
    '-- 口径：排除 FTS5 影子表（memory_fts_* 与 kb_fts_* 的 config/content/data/docsize/idx）与 sqlite_sequence；',
    '--       保留 memory_fts / kb_fts 本体定义（影子期只读校验依赖它们）。',
    '',
  ]
  const body = []
  for (const type of order) {
    const group = objects.filter((object) => object.type === type && !isShadow(object.name))
    if (group.length === 0) continue
    body.push(`-- ===== ${type} (${group.length}) =====`)
    for (const object of group) {
      body.push(`-- ${object.name}${object.tbl_name !== object.name ? ` (on ${object.tbl_name})` : ''}`)
      body.push(`${object.sql.trim()};`)
      body.push('')
    }
  }
  return `${header.join('\n')}${body.join('\n')}`.trimEnd() + '\n'
}

// 顶层 await：先把构建产物加载进来，再跑主流程。
let libModule
try {
  libModule = await import(pathToFileURL(libPath).href)
} catch (error) {
  console.error(`加载 ${libPath} 失败：先跑 pnpm run build\n${error.message}`)
  process.exitCode = 1
  process.exit(1)
}

try {
  main()
} catch (error) {
  console.error(`gen-spec 失败：${(error && error.stack) || error}`)
  process.exitCode = 1
}
