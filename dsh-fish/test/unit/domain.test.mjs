/**
 * 领域层单元测试（T02）：证据分级 / 检索过滤 / 自检渲染 / token 口径。
 *
 * 为什么这些用例必须存在（而有对拍还要它们）：
 *   对拍只回答"两侧一样"，不回答"这条规则还在不在"。
 *   例如"证据级别只降不升"——如果实现被改成"一律 machine"，只要 Python 侧也那样写，
 *   对拍照样绿；但语义已经错了。所以每一条**语义约束**都要有一条本侧断言。
 *
 * 2026-11-09 减重第二批删掉了 `domain/{inject,board,push,profile,activity,naming,surface,links,freshness}.ts`，
 * 因此测这 9 个模块的用例一并移除——它们断言的对象已不在仓里。
 * **保留**的是产品语义仍然成立的几组：证据分级（identity）、检索过滤（memory）、
 * 自检渲染（health），以及 token 口径（`token.ts`，`spec/injection.json.budgetRule` 的契约面）。
 *
 * 跑法：`pnpm test`（依赖 `lib/`，先 `pnpm run build`）
 */

import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { DatabaseSync } from 'node:sqlite'
import { after, describe, it } from 'node:test'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const packageRoot = path.resolve(here, '..', '..')
const lib = await import(pathToFileURL(path.join(packageRoot, 'lib', 'index.js')).href)
const schemaSql = readFileSync(path.join(packageRoot, 'spec', 'schema.sql'), 'utf8')

/** 造一个临时库（写只发生在临时目录）；返回目录，测试结束统一删。 */
const tempDirs = []
function makeDb(seedSql = []) {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'fish-unit-'))
  tempDirs.push(dir)
  const dbPath = path.join(dir, 'index.sqlite')
  const db = new DatabaseSync(dbPath)
  db.exec(schemaSql)
  for (const statement of seedSql) db.exec(statement)
  db.close()
  return { dir, dbPath, dataDir: dir }
}

/** 灌 FTS（生产实现算 token，不手抄）。 */
function seedFts(dbPath, entries) {
  const db = new DatabaseSync(dbPath)
  try {
    for (const entry of entries) {
      const titlePart = `${entry.code} ${entry.title ?? ''} ${(entry.tags ?? []).join(' ')}`
      db.prepare('INSERT INTO memory_fts (code, title_idx, body_idx) VALUES (?,?,?)').run(
        entry.code,
        lib.cutText(titlePart),
        lib.cutText(entry.body ?? ''),
      )
    }
  } finally {
    db.close()
  }
}

after(() => {
  for (const dir of tempDirs) rmSync(dir, { recursive: true, force: true })
})

const BASE_SEED = [
  "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at,is_owner,host_session_key) VALUES ('C-OWNER','主理人','resident','owner','cli','active','2026-10-01T00:00:00Z',1,'cli:owner'), ('C-ENG','工程师','role','engineer','cli','active','2026-10-02T00:00:00Z',0,'cli:eng')",
  "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) VALUES ('demo','演示','261027',2,'active')",
  "INSERT INTO code_index (code,title,project,owner,mem_type,visibility,status,tags,file_path,occurred_at,recorded_at,dormant) VALUES ('demo-261027-01','协作约定：先读后写','demo','C-ENG','fact','shared','active','[\"协作\"]','','2026-10-24T00:00:00Z','2026-10-24T00:00:00Z',0), ('demo-261027-02','旧结论：手工维护索引','demo','C-ENG','fact','shared','superseded','[]','','2026-10-18T00:00:00Z','2026-10-18T00:00:00Z',0), ('demo-261027-03','新结论：索引由事件重放','demo','C-ENG','fact','shared','active','[]','','2026-10-20T00:00:00Z','2026-10-20T00:00:00Z',0), ('demo-261027-04','待办：把 T02 做完','demo','C-ENG','fact','shared','active','[]','','2026-10-24T00:00:00Z','2026-10-24T00:00:00Z',1), ('demo-261027-05','规矩：先查再答','demo','C-ENG','rule','shared','active','[]','','2026-10-24T00:00:00Z','2026-10-24T00:00:00Z',0)",
]

const FTS_SEED = [
  { code: 'demo-261027-01', title: '协作约定：先读后写', tags: ['协作'], body: '写入之前先读相关条目。' },
  { code: 'demo-261027-02', title: '旧结论：手工维护索引', body: '手工维护索引的老办法。' },
  { code: 'demo-261027-03', title: '新结论：索引由事件重放', body: '索引改由事件重放生成，不再手工敲。' },
  { code: 'demo-261027-04', title: '待办：把 T02 做完', body: '剩余 fixtures。' },
  { code: 'demo-261027-05', title: '规矩：先查再答', body: '不确定就先查库。' },
]

describe('证据分级（只降不升）', () => {
  it('声称高于天花板时被降级，并标记 downgraded', () => {
    const result = lib.clampEvidence('machine', 'self-reported')
    assert.equal(result.effective, 'self-reported')
    assert.equal(result.downgraded, true)
  })

  it('声称低于天花板时保留声称值（降级是允许的方向）', () => {
    const result = lib.clampEvidence('self-reported', 'machine')
    assert.equal(result.effective, 'self-reported')
    assert.equal(result.downgraded, false)
  })

  it('没有天花板时不给任何级别（不许"没证据也算 machine"）', () => {
    assert.deepEqual(lib.clampEvidence('machine', null), { effective: null, downgraded: false })
  })

  it('session_key 单独使用时的天花板是 self-reported', () => {
    const { dbPath } = makeDb([
      ...BASE_SEED,
      "INSERT INTO session_tokens (token_hash,citizen_id,session_key,evidence,channel,issued_at) VALUES ('x','C-ENG','cli:eng','machine','cli','2026-10-26T00:00:00Z')",
    ])
    const db = lib.openReadOnly(dbPath)
    try {
      const report = lib.whoami(db, { sessionKey: 'cli:eng' })
      assert.equal(report.citizen_source, 'session_key')
      assert.equal(report.evidence_effective, 'self-reported')
      assert.equal(report.evidence_ceiling, 'self-reported')
    } finally {
      db.close()
    }
  })
})

describe('token 口径：int(len*1.2)，且 len 数的是码点（契约 budgetRule）', () => {
  it('token 计数走的是 Python fallback 公式 int(len*1.2)，且 len 数的是**码点**', () => {
    assert.equal(lib.countTokens(''), 0)
    assert.equal(lib.countTokens('abcdefghij'), 12)
    assert.equal(lib.countTokens('中文五个字'), 6)
    // astral 字符（看板里真出现过 🔴）：Python `len` = 1，JS `.length` = 2。
    // 这条用例钉的是 T03 快照对拍抓到的真 bug（`<!-- 930 -->` vs `<!-- 931 -->`）。
    assert.equal(lib.codePointLength('🔴'), 1)
    assert.equal('🔴'.length, 2)
    assert.equal(lib.countTokens('🔴'), 1)
    assert.equal(lib.countTokens('看板 🔴 一枚 emoji'), Math.trunc([...'看板 🔴 一枚 emoji'].length * 1.2))
  })

  it('按字符截断也走码点（不许把代理对切成两半）', () => {
    assert.equal(lib.clipChars('🔴🔴🔴🔴', 2), '🔴…')
    assert.equal(lib.clipChars('abc', 0), 'abc')
  })
})

describe('检索：过滤语义', () => {
  it('默认过滤掉 superseded；显式放开后才出现', () => {
    const { dbPath } = makeDb(BASE_SEED)
    seedFts(dbPath, FTS_SEED)
    const db = lib.openReadOnly(dbPath)
    try {
      const filtered = lib.searchMemories(db, { query: '手工维护' })
      assert.equal(filtered.rows.length, 0, 'superseded 默认不该命中')
      const opened = lib.searchMemories(db, { query: '手工维护', includeSuperseded: true })
      assert.equal(opened.rows.length, 1)
      assert.equal(opened.rows[0][0], 'demo-261027-02')
      assert.equal(opened.rows[0][4], 'superseded')
    } finally {
      db.close()
    }
  })

  it('FTS 是 AND 不是 OR：只含一个 bigram 的 active 条目不算命中', () => {
    const { dbPath } = makeDb(BASE_SEED)
    seedFts(dbPath, FTS_SEED)
    const db = lib.openReadOnly(dbPath)
    try {
      const { rows } = lib.searchMemories(db, { query: '手工维护' })
      assert.equal(rows.length, 0, 'demo-261027-03 只有「手工」两字，不该命中')
    } finally {
      db.close()
    }
  })

  it('未命中回退遵守 project 过滤（不跨项目串台）', () => {
    const { dbPath } = makeDb(BASE_SEED)
    seedFts(dbPath, FTS_SEED)
    const db = lib.openReadOnly(dbPath)
    try {
      const rows = lib.recentFallback(db, { project: 'demo', limit: 3 })
      assert.ok(rows.length > 0)
      assert.ok(rows.every((row) => row.code.startsWith('demo-')), '回退列表混入了别的项目')
    } finally {
      db.close()
    }
  })

  it('状态标签把 dormant 合并进同一个方括弧', () => {
    assert.equal(lib.formatStatusTag('fact', 'active', 1), '[fact/active+dormant]')
    assert.equal(lib.formatStatusTag('fact', 'active', 0), '[fact/active]')
  })
})

describe('自检渲染', () => {
  it('索引落后 / 同步两种措辞各自出现（不许把落后念成同步）', () => {
    const synced = lib.renderHealthcheck({
      events: 0,
      activeMemories: 2,
      dormant: 0,
      withdrawn: 0,
      brokenLinks: [],
      fts: 2,
      ftsGap: 0,
      ownerInvalid: [],
    })
    assert.match(synced, /全文索引 2 条（与记忆同步）/)
    const lagged = lib.renderHealthcheck({
      events: 1,
      activeMemories: 3,
      dormant: 1,
      withdrawn: 1,
      brokenLinks: ['demo-261027-01'],
      fts: 2,
      ftsGap: 1,
      ownerInvalid: ['demo-261027-02'],
    })
    assert.match(lagged, /断链 1 条/)
    assert.match(lagged, /断链：demo-261027-01/)
    assert.match(lagged, /落后 1 条/)
    assert.match(lagged, /owner 不是有效公民 1 条/)
  })
})
