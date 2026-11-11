/**
 * T05 单元测试：知识库 / 语料 / 项目坐标三族只读面的**纯函数与零写入纪律**。
 *
 * 覆盖重点（都是"改坏了才会红、而且变化肉眼看不出来"的那类）：
 *   ① 知识库的**硬边界**：忽略规则（强制项删不掉）、逐段敏感名单（`密码学` 不算密码）、
 *      八类路径拒绝（`..` / 绝对 / 盘符 / ADS / 设备名 / 越界 / 链接 / 不存在）；
 *   ② 语料层的**门牌三档**与**出口二次脱敏**（`token=` 这类键+值整体消费）；
 *   ③ 项目坐标：别名多跳展开、六列表的来源、retired 折叠；
 *   ④ 影子期纪律：读命令**一个字节都不写**（kb_access / corpus_access_log / 状态表文件）。
 *
 * 为什么零写入要单独断言：这不是"顺手检查一下"，而是影子期的**定义**。
 * 生产侧每次读都追一条审计（`kb_access` / `corpus_access_log`），TS 侧刻意不写——
 * 没有这条断言，"我们没写"就只是一句承诺。
 */

import assert from 'node:assert/strict'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { DatabaseSync } from 'node:sqlite'
import { test } from 'node:test'

import {
  ageText,
  buildFilterSql,
  canonicalPointer,
  corpusRead,
  corpusSearch,
  corpusStats,
  cutText,
  effectiveGlobs,
  fnmatchCase,
  judgeComponent,
  kbDetectText,
  kbFitText,
  kbIgnored,
  kbIsSensitiveName,
  kbOpen,
  kbResolveTarget,
  kbSearch,
  kbTree,
  kbVaultGet,
  kbVaultList,
  KbError,
  matchExpression,
  parseAsOf,
  parsePointer,
  parseStrictBool,
  PointerError,
  projectStatusPayload,
  projectStatusRows,
  pushPointer,
  rankNorm,
  redact,
  renderAliasList,
  renderProjectStatus,
  resolveName,
  resolvePointer,
  sid8,
} from '../../lib/index.js'

/** 包根（`test/unit` 往上两层）。 */
const packageRoot = path.resolve(import.meta.dirname, '..', '..')

/** 造一个临时目录，返回 `{dir, cleanup}`。 */
function tempDir(prefix) {
  const dir = mkdtempSync(path.join(os.tmpdir(), prefix))
  return { dir, cleanup: () => rmSync(dir, { recursive: true, force: true }) }
}

/** 用 `spec/schema.sql` 建一个可写库（测试里用它造夹具；只读由 `openReadOnly` 保证）。 */
function makeMainDb(dir) {
  const dbPath = path.join(dir, 'data', 'index.sqlite')
  mkdirSync(path.dirname(dbPath), { recursive: true })
  const db = new DatabaseSync(dbPath)
  db.exec(readFileSync(path.join(packageRoot, 'spec', 'schema.sql'), 'utf8'))
  return { db, dbPath }
}

/** 建一个知识库 vault（磁盘文件 + `kb_vault` 行）。 */
function seedVault(db, root, options = {}) {
  const vaultDir = path.join(root, 'vault')
  const files = options.files ?? {}
  for (const [rel, text] of Object.entries(files)) {
    const abs = path.join(vaultDir, rel)
    mkdirSync(path.dirname(abs), { recursive: true })
    writeFileSync(abs, text, 'utf8')
  }
  mkdirSync(vaultDir, { recursive: true })
  db.prepare(
    'INSERT INTO kb_vault (vault_id,name,path,path_key,mode,ignore_globs,max_open_bytes,is_default,status,created_at,updated_at) ' +
      'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
  ).run('01VAULT00000000000000000001', '测试库', vaultDir, vaultDir.toLowerCase(), 'ro', JSON.stringify(options.ignore ?? []), 2097152, 1, 'active', '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z')
  return vaultDir
}

// ---------------------------------------------------------------- 知识库：忽略与敏感

test('kb: 生效忽略规则 = 强制项 ∪ 行内规则（强制项删不掉）', () => {
  assert.deepEqual(effectiveGlobs([]), ['.obsidian/**', '.trash/**', '90-模板/**'])
  assert.deepEqual(effectiveGlobs(['20-附件/**', '.obsidian/**']), ['.obsidian/**', '.trash/**', '90-模板/**', '20-附件/**'])
  assert.equal(kbIgnored('.obsidian/app.json', []), '.obsidian/**')
  assert.equal(kbIgnored('.trash/a/b.md', []), '.trash/**')
  assert.equal(kbIgnored('90-模板/模板.md', []), '90-模板/**')
  // 目录本身也要被挡住（否则 tree 会列出空节点）
  assert.equal(kbIgnored('90-模板', []), '90-模板/**')
  assert.equal(kbIgnored('20-附件/说明.md', ['20-附件/**']), '20-附件/**')
  assert.equal(kbIgnored('10-漫剧/项目.md', []), null)
})

test('kb: 忽略判定先做逐段归一（大小写 + 尾随点/空格）', () => {
  assert.equal(judgeComponent('.OBSIDIAN. '), '.obsidian')
  assert.equal(kbIgnored('.OBSIDIAN/app.json', []), '.obsidian/**')
  assert.equal(kbIgnored('.obsidian /app.json', []), '.obsidian/**')
})

test('kb: 敏感名单逐段判定 + 密码学的负向断言', () => {
  assert.equal(kbIsSensitiveName('20-学习/密码本.md'), true)
  assert.equal(kbIsSensitiveName('20-学习/密码学笔记.md'), false)
  assert.equal(kbIsSensitiveName('凭证目录/账号清单.md'), true)
  assert.equal(kbIsSensitiveName('config/.env.local'), true)
  assert.equal(kbIsSensitiveName('keys/id_rsa'), true)
  assert.equal(kbIsSensitiveName('普通/笔记.md'), false)
})

test('kb: fnmatch 的 * / ? / [seq] 与 Python 同口径（* 跨 /）', () => {
  assert.equal(fnmatchCase('a/b/c.md', '*'), true)
  assert.equal(fnmatchCase('a/b.md', 'a/?.md'), true)
  assert.equal(fnmatchCase('a/b.md', 'a/??.md'), false)
  assert.equal(fnmatchCase('x1.md', 'x[12].md'), true)
  assert.equal(fnmatchCase('x3.md', 'x[!12].md'), true)
})

// ---------------------------------------------------------------- 知识库：路径拒绝

test('kb: 路径解析的八类拒绝（code 不认文案）', () => {
  const { dir, cleanup } = tempDir('fish-t05-kb-')
  try {
    const { db } = makeMainDb(dir)
    const vaultDir = seedVault(db, dir, { files: { '10-漫剧/a.md': '# a\n', '.obsidian/app.json': '{}' } })
    db.close()
    const probe = new DatabaseSync(path.join(dir, 'data', 'index.sqlite'), { readOnly: true })
    let vault
    try {
      vault = kbVaultGet(probe, null)
    } finally {
      probe.close()
    }
    const codeOf = (rel) => {
      try {
        kbResolveTarget(vault, rel)
        return 'ok'
      } catch (error) {
        assert.ok(error instanceof KbError)
        return error.code
      }
    }
    assert.equal(codeOf('..'), 'path_invalid')
    assert.equal(codeOf('../别处.md'), 'path_invalid')
    assert.equal(codeOf('/etc/passwd'), 'path_invalid')
    assert.equal(codeOf('C:\\Windows\\x.md'), 'path_invalid')
    assert.equal(codeOf('10-漫剧/a.md:hidden'), 'path_invalid')
    assert.equal(codeOf('CON'), 'path_invalid')
    assert.equal(codeOf('.obsidian/app.json'), 'ignored')
    assert.equal(codeOf('10-漫剧/不存在.md'), 'not_found')
    assert.equal(codeOf('10-漫剧/a.md'), 'ok')
    assert.ok(existsSync(path.join(vaultDir, '10-漫剧/a.md')))
  } finally {
    cleanup()
  }
})

test('kb: 文本探测（扩展名 + 魔数 + NUL + 编码）', () => {
  assert.equal(kbDetectText(Buffer.from('hello', 'utf8'), '.png').reason, 'extension:.png')
  const png = Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), Buffer.from('x')])
  assert.equal(kbDetectText(png, '.md').reason, 'magic:png')
  assert.equal(kbDetectText(Buffer.from([0x61, 0x00, 0x62]), '.md').reason, 'nul_byte')
  const utf8 = kbDetectText(Buffer.from('# 中文标题\n', 'utf8'), '.md')
  assert.equal(utf8.isText, true)
  assert.equal(utf8.encoding, 'utf-8')
  const bom = kbDetectText(Buffer.from('\uFEFF标题', 'utf8'), '.md')
  assert.equal(bom.encoding, 'utf-8-sig')
  // 非法 UTF-8 序列：既不是文本也不是媒体 → bad_encoding
  assert.equal(kbDetectText(Buffer.from([0xff, 0xfe, 0xfd]), '.md').reason, 'bad_encoding')
})

test('kb: 双预算截断（先字符、后 token，并说明原因）', () => {
  const lines = Array.from({ length: 50 }, (_v, i) => `第 ${i} 行：${'字'.repeat(20)}`)
  const fitted = kbFitText(lines, 200, 50)
  assert.equal(fitted.truncated, true)
  assert.ok(fitted.chars <= 200)
  assert.ok(fitted.truncatedReasons.length > 0)
  const plain = kbFitText(['一行'], 8000, 1200)
  assert.equal(plain.truncated, false)
})

test('kb: 严格布尔解析（"false" 是拒绝，模糊写法报错）', () => {
  assert.equal(parseStrictBool(undefined, 'allow_sensitive'), false)
  assert.equal(parseStrictBool('false', 'allow_sensitive'), false)
  assert.equal(parseStrictBool('TRUE', 'allow_sensitive'), true)
  assert.equal(parseStrictBool(1, 'allow_sensitive'), true)
  assert.throws(() => parseStrictBool('yes', 'allow_sensitive'), (error) => error instanceof KbError && error.code === 'bad_usage')
})

// ---------------------------------------------------------------- 知识库：读路径 + 零写入

test('kb: tree/open/search/stats 能读出内容，且**不写 kb_access**', () => {
  const { dir, cleanup } = tempDir('fish-t05-kbread-')
  try {
    const { db, dbPath } = makeMainDb(dir)
    seedVault(db, dir, {
      files: {
        '10-漫剧/方法论/手册.md': '# 手册\n\n协作约定：先对齐再动手。\n',
        '10-漫剧/项目.md': '# 项目\n\n下一步：把模板化。\n',
        '20-学习/密码本.md': '# 密码本\n\n口令在这里。\n',
      },
    })
    db.close()

    const readOnly = new DatabaseSync(dbPath, { readOnly: true })
    try {
      const vault = kbVaultGet(readOnly, null)
      assert.equal(kbVaultList(readOnly).length, 1)
      const tree = kbTree(vault, { depth: 2 })
      assert.match(tree.text, /知识库目录/)
      assert.match(tree.text, /10-漫剧\/方法论\/手册\.md/)
      assert.equal(tree.scan.files, 3)

      const opened = kbOpen(vault, '10-漫剧/方法论/手册.md')
      assert.match(opened.text, /协作约定/)
      assert.equal(opened.encoding, 'utf-8')
      assert.throws(
        () => kbOpen(vault, '20-学习/密码本.md'),
        (error) => error instanceof KbError && error.code === 'sensitive',
      )
      assert.match(kbOpen(vault, '20-学习/密码本.md', { allowSensitive: true }).text, /口令/)

      const found = kbSearch(vault, '协作约定')
      assert.equal(found.total, 1)
      assert.equal(found.hits[0]?.rel, '10-漫剧/方法论/手册.md')
      assert.equal(found.scanned.skippedSensitive, 1)
    } finally {
      readOnly.close()
    }

    // 影子期纪律：读命令一条审计都不该留下。
    const after = new DatabaseSync(dbPath, { readOnly: true })
    try {
      const row = after.prepare('SELECT COUNT(*) AS n FROM kb_access').get()
      assert.equal(Number(row.n), 0, 'kb 读路径不能写 kb_access（影子期零写入）')
    } finally {
      after.close()
    }
  } finally {
    cleanup()
  }
})

// ---------------------------------------------------------------- 语料

test('corpus: 门牌三档（bad_pointer / not_found / ambiguous_pointer）', () => {
  assert.throws(() => parsePointer('不是门牌'), (error) => error instanceof PointerError && error.code === 'bad_pointer')
  assert.throws(() => parsePointer(''), (error) => error instanceof PointerError && error.code === 'bad_pointer')
  const parsed = parsePointer('log:dsh:session-abc#seq=12.1')
  assert.deepEqual(parsed, { kind: 'log', host: 'dsh', sid: 'session-abc', loc: 'seq=12', part: 1 })
  assert.equal(canonicalPointer('dsh', 'session-abc', 'seq=12', 1), 'log:dsh:session-abc#seq=12.1')
  assert.equal(pushPointer('dsh', 'session-abc', 'seq=12', 0), 'conv:dsh:abc#seq=12')
  assert.equal(sid8('session-3931d23f-17c9-4de8'), '3931d23f')
  assert.equal(sid8('3931d23f-17c9-4de8'), '3931d23f')
})

test('corpus: 出口脱敏（键+值整体消费、占位符幂等）', () => {
  const [text, count] = redact('MY_TOKEN=abc123 和 fsh_abcdefghijklmnopqrstuvwx 与 Bearer abcdefgh')
  assert.ok(count >= 2)
  assert.ok(!text.includes('abc123'))
  assert.ok(!text.includes('fsh_abcdefghijklmnopqrstuvwx'))
  const [again, second] = redact(text)
  assert.equal(again, text)
  assert.equal(second, 0)
})

test('corpus: MATCH 表达式与过滤下推（project 的空串是有效值）', () => {
  assert.equal(matchExpression(['协作', 'cr-12']), '"协作" OR "cr-12"')
  assert.equal(matchExpression([]), null)
  const [where, args] = buildFilterSql({ project: [''] }, {})
  assert.match(where, /ss\.project IN \(\?\)/)
  assert.deepEqual(args, [''])
  assert.match(where, /s\.shadowed=0/)
  assert.throws(() => buildFilterSql({ nope: ['x'] }, {}), /未知过滤键/)
  assert.throws(() => buildFilterSql({}, { timeFrom: '2026-10-27 00:00:00' }), /UTC/)
})

test('corpus: 排序口径（批内归一 + 时间衰减）', () => {
  assert.deepEqual(rankNorm([1, 3]), [1, 0])
  assert.deepEqual(rankNorm([2, 2]), [1, 1])
  assert.equal(ageText(0.2), '今天')
  assert.equal(ageText(3.4), '3 天前')
  assert.equal(ageText(45), '1 个月前')
  assert.equal(ageText(400), '1.1 年前')
})

test('corpus: search/read/stats 跑通且**不写 corpus_access_log**', () => {
  const { dir, cleanup } = tempDir('fish-t05-corpus-')
  try {
    const dataDir = path.join(dir, 'data')
    mkdirSync(dataDir, { recursive: true })
    const corpusPath = path.join(dataDir, 'corpus.sqlite')
    const db = new DatabaseSync(corpusPath)
    try {
      db.exec(readFileSync(path.join(packageRoot, 'spec', 'corpus.schema.sql'), 'utf8'))
      db.prepare(
        "INSERT INTO corpus_session (host,session_id,sid8,title,workspace,project,project_source,origin,segments) VALUES ('dsh','session-aaaa1111','aaaa1111','夹具会话','','ANYI','cwd','main',1)",
      ).run()
      db.prepare(
        "INSERT INTO corpus_source (host,session_id,path,format,bytes_total,mtime_ns,events_seen,segments_kept,status,first_ingested_at,last_ingested_at) VALUES ('dsh','session-aaaa1111','C:\\logs\\a.jsonl','dsh.v4',128,0,2,1,'ok','2026-10-26T00:00:00Z','2026-10-26T00:00:00Z')",
      ).run()
      db.prepare(
        "INSERT INTO corpus_segment (id,host,session_id,loc,part,role,occurred_at,text,chars,redacted_count,raw_sha256,byte_from,byte_to,offset_space,shadowed,ingested_at) VALUES (1,'dsh','session-aaaa1111','seq=1',0,'user','2026-10-20T00:00:00Z','协作约定：先对齐再动手。token=abcdef123456',30,1,'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',0,64,'byte',0,'2026-10-26T00:00:00Z')",
      ).run()
      db.prepare('INSERT INTO corpus_fts (seg_id, text_idx) VALUES (1, ?)').run(cutText('协作约定：先对齐再动手。token=abcdef123456'))
    } finally {
      db.close()
    }

    const res = corpusSearch(dataDir, '协作')
    assert.equal(res.meta.total_matched, 1)
    assert.equal(res.hits.length, 1)
    assert.ok(!(res.hits[0]?.snippet ?? '').includes('abcdef123456'), '片段必须脱敏')

    const read = corpusRead(dataDir, 'conv:dsh:aaaa1111#seq=1')
    assert.equal(read.role, 'user')
    // 夹具的源路径是 `C:\logs\a.jsonl`（不存在）：必须**声明**来源缺失，正文照常返回。
    assert.equal(read.source.missing, true)
    assert.match(read.source.note, /源文件已不在/)
    assert.ok(!read.text.includes('abcdef123456'))

    const stats = corpusStats(dataDir)
    assert.equal(stats.segments, 1)
    assert.equal(stats.sessions, 1)
    assert.equal(JSON.parse(JSON.stringify(stats)).db_missing, undefined)

    const check = new DatabaseSync(corpusPath, { readOnly: true })
    try {
      assert.equal(Number(check.prepare('SELECT COUNT(*) AS n FROM corpus_access_log').get().n), 0, '语料读路径不能写审计（影子期零写入）')
    } finally {
      check.close()
    }
  } finally {
    cleanup()
  }
})

// ---------------------------------------------------------------- 项目坐标

test('project: 别名多跳展开 + 分组折叠', () => {
  const { dir, cleanup } = tempDir('fish-t05-alias-')
  try {
    const { db } = makeMainDb(dir)
    db.prepare("INSERT INTO code_registry (prefix,namespace,status) VALUES ('ANYI','安逸知识库','active')").run()
    db.prepare("INSERT INTO name_aliases (kind,from_name,to_name,created_at) VALUES ('project','anyi','ANYI','2026-10-01T00:00:00Z')").run()
    db.prepare("INSERT INTO name_aliases (kind,from_name,to_name,created_at) VALUES ('project','anyi-kb','anyi','2026-10-01T00:00:00Z')").run()
    const resolved = resolveName(db, 'project', 'anyi-kb')
    assert.equal(resolved.canonical, 'ANYI')
    assert.deepEqual(resolved.chain, ['anyi-kb', 'anyi', 'ANYI'])
    assert.deepEqual(resolved.aliases, ['anyi', 'anyi-kb'])
    assert.equal(resolved.found, true)
    assert.equal(resolved.status, 'active')
    const text = renderAliasList(db)
    assert.match(text, /project\tanyi → ANYI/)
    assert.match(text, /\[project\] ANYI ← anyi, anyi-kb/)
    db.close()
  } finally {
    cleanup()
  }
})

test('project: 六列表 + 每列来源 + retired 折叠 + 脚注', () => {
  const { dir, cleanup } = tempDir('fish-t05-pst-')
  try {
    const { db } = makeMainDb(dir)
    db.prepare("INSERT INTO code_registry (prefix,namespace,status) VALUES ('ANYI','安逸知识库','active')").run()
    db.prepare("INSERT INTO code_registry (prefix,namespace,status) VALUES ('OLD','老项目','retired')").run()
    db.prepare("INSERT INTO citizens (citizen_id,display_name,kind,status,created_at) VALUES ('01CITIZEN00000000000000001','安逸AI','resident','active','2026-09-01T00:00:00Z')").run()
    db.prepare("INSERT INTO project_stints (citizen_id,project,from_ts) VALUES ('01CITIZEN00000000000000001','ANYI','2026-10-10T00:00:00Z')").run()
    db.prepare(
      "INSERT INTO code_index (code,title,project,owner,mem_type,status,recorded_at,dormant) VALUES ('ANYI-261016-02','下一步：写文档','ANYI','01CITIZEN00000000000000001','fact','active','2026-10-16T02:00:00Z',0)",
    ).run()
    db.prepare("INSERT INTO boards (scope,scope_key,period_key,path,status,created_at) VALUES ('project','ANYI','261020','10-项目库/ANYI/看板-261020.md','open','2026-10-20T00:00:00Z')").run()

    const asOf = parseAsOf('2026-10-20T12:00:00+08:00')
    const result = projectStatusRows(db, { dataRoot: path.join(dir, 'data'), asOf })
    assert.equal(result.rows.length, 1)
    assert.equal(result.retired_total, 1)
    assert.equal(result.retired.length, 1)
    const row = result.rows[0]
    assert.equal(row.last_active, '2026-10-16')
    assert.equal(row.last_active_source, 'code_index.recorded_at(max)')
    assert.equal(row.board_entry, '[看板-261020.md](ANYI/看板-261020.md)')
    assert.equal(row.owners, '安逸AI')
    assert.equal(row.owners_source, 'project_stints(开档期)')
    assert.equal(row.open_loops, 1)
    assert.equal(row.sources.status, 'code_registry.status')
    assert.equal(result.as_of, '2026-10-20T12:00:00+08:00')
    assert.equal(result.footnotes.length, 7)

    const payload = projectStatusPayload(result)
    assert.deepEqual(Object.keys(payload), ['as_of', 'home_root', 'columns', 'rows', 'retired', 'degraded', 'footnotes', 'retired_total'])
    assert.deepEqual(Object.keys(payload.rows[0]), [
      'prefix',
      'namespace',
      'status',
      'status_label',
      'last_active',
      'last_active_source',
      'board_entry',
      'board_entry_source',
      'owners',
      'owners_source',
      'open_loops',
      'open_loops_source',
      'sources',
    ])

    // retired 进主表的逃生口
    const inclusive = projectStatusRows(db, { dataRoot: path.join(dir, 'data'), asOf, includeRetired: true })
    assert.equal(inclusive.rows.length, 2)
    assert.equal(inclusive.retired.length, 0)
    db.close()
  } finally {
    cleanup()
  }
})

test('project: `project status` 的文本形态**不落盘**（影子期纪律）', () => {
  const { dir, cleanup } = tempDir('fish-t05-pst-write-')
  try {
    const { db } = makeMainDb(dir)
    db.prepare("INSERT INTO code_registry (prefix,namespace,status) VALUES ('ANYI','安逸知识库','active')").run()
    const asOf = parseAsOf('2026-10-20T12:00:00+08:00')
    const result = projectStatusRows(db, { dataRoot: path.join(dir, 'data'), asOf })
    const text = renderProjectStatus(result)
    assert.match(text, /# 项目状态表/)
    assert.match(text, /影子期不落盘/)
    assert.equal(existsSync(path.join(result.home_root, '10-项目库', '项目状态表.md')), false, '影子期不写渲染文件')
    db.close()
  } finally {
    cleanup()
  }
})
