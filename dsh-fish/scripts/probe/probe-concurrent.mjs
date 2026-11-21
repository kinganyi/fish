#!/usr/bin/env node
/**
 * 先验假设 #3：两路进程并发写同一 SQLite（`BEGIN IMMEDIATE` + `busy_timeout`）时
 * 不出现**未处理**的 `SQLITE_BUSY`，且计数守恒。
 *
 * 硬约束：**绝不写真库**。生产库只以 `mode=ro` 打开做一次 `backup` 拷到临时目录，
 * 之后所有写都发生在临时副本上；退出前删除副本。
 *
 * 判据（全部必须通过）：
 *   A. 两路各写 100 行，`busyErrors === 0`、`written === 100`（不许靠重试掩盖锁冲突）。
 *   B. 临时库总行数守恒：`SELECT COUNT(*) === 200`，且两标签各 100。
 *   C. 反向对照：`busy_timeout=0` 时同样并发必出 `SQLITE_BUSY`——证明 A 测的是真并发，
 *      而不是"两个进程其实串行跑了"。
 *
 * 用法：node scripts/probe/probe-concurrent.mjs [--json]
 */

import { DatabaseSync, backup } from 'node:sqlite';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { mkdtempSync, rmSync, readFileSync } from 'node:fs';
import os from 'node:os';

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '..', '..');
const dbPath = process.env.FISH_DB
  ? path.resolve(process.env.FISH_DB)
  : path.resolve(repoRoot, '..', 'p0', 'data', 'index.sqlite');

const ROWS_PER_WORKER = 100;
const BUSY_TIMEOUT_MS = 5000;

/** 生产库 → 临时副本（只读打开源库，走 SQLite 自己的 backup 通道）。 */
async function snapshotTo(tmpDir) {
  const target = path.join(tmpDir, 'probe-index.sqlite');
  const src = new DatabaseSync(dbPath, { readOnly: true });
  // `backup()` 只读源库、写目标文件；不解析、不改写源库任何内容。
  await backup(src, target);
  src.close();
  return target;
}

/** 起一个工人进程并等它结束；结果经文件回传（避免管道依赖）。 */
function runWorker(workerDb, tag, rows, busyTimeout, resultFile) {
  return new Promise((resolve) => {
    const child = spawn(
      process.execPath,
      [path.join(here, 'concurrent-write-child.mjs'), workerDb, tag, String(rows), String(busyTimeout), resultFile],
      { cwd: repoRoot, stdio: 'inherit' },
    );
    child.on('exit', (code) => resolve(code));
    child.on('error', (err) => resolve({ error: err.message }));
  });
}

async function runRound(workerDb, tagSuffix, busyTimeout, rows) {
  const results = [];
  const exits = await Promise.all(
    ['alpha', 'beta'].map(async (tag) => {
      const resultFile = path.join(path.dirname(workerDb), `result-${tag}${tagSuffix}.json`);
      const code = await runWorker(workerDb, tag, rows, busyTimeout, resultFile);
      let payload = null;
      try {
        payload = JSON.parse(readFileSync(resultFile, 'utf8'));
      } catch {
        /* 工人没写出结果文件 = 立刻崩，按失败处理。 */
      }
      results.push({ exit: code, payload });
      return code;
    }),
  );
  return { results, exits };
}

async function main() {
  const checks = [];
  const record = (id, ok, detail) => checks.push({ id, ok, detail });

  const tmpDir = mkdtempSync(path.join(os.tmpdir(), 'fish-concurrent-'));
  let workerDb;
  try {
    workerDb = await snapshotTo(tmpDir);
    record('A.snapshot', true, `副本=${workerDb}`);

    // 临时表只存在于副本里。
    const setup = new DatabaseSync(workerDb);
    setup.exec('PRAGMA journal_mode=WAL');
    setup.exec('CREATE TABLE IF NOT EXISTS probe_w (id INTEGER PRIMARY KEY AUTOINCREMENT, tag TEXT NOT NULL, seq INTEGER NOT NULL, payload TEXT NOT NULL)');
    setup.exec('DELETE FROM probe_w');
    setup.close();

    // 判据 A/B：busy_timeout=5000，两路各 100 行
    const round1 = await runRound(workerDb, '-bt', BUSY_TIMEOUT_MS, ROWS_PER_WORKER);
    const busy1 = round1.results.reduce((n, r) => n + ((r.payload && r.payload.busyErrors) || 0), 0);
    const written1 = round1.results.reduce((n, r) => n + ((r.payload && r.payload.written) || 0), 0);
    const other1 = round1.results.flatMap((r) => (r.payload && r.payload.otherErrors) || []);
    record(
      'A.no_unhandled_busy',
      busy1 === 0 && other1.length === 0 && written1 === ROWS_PER_WORKER * 2,
      `busyErrors=${busy1} otherErrors=${other1.length} written=${written1}/${ROWS_PER_WORKER * 2} exits=${JSON.stringify(round1.exits)}`,
    );

    const verify = new DatabaseSync(workerDb, { readOnly: true });
    const total = verify.prepare('SELECT COUNT(*) AS n FROM probe_w').get().n;
    const perTag = verify
      .prepare('SELECT tag, COUNT(*) AS n FROM probe_w GROUP BY tag ORDER BY tag')
      .all()
      .map((r) => `${r.tag}:${r.n}`)
      .join(',');
    const seqOk = verify
      .prepare('SELECT tag, MIN(seq) AS lo, MAX(seq) AS hi, COUNT(DISTINCT seq) AS distinct_n FROM probe_w GROUP BY tag')
      .all()
      .every((r) => r.lo === 1 && r.hi === ROWS_PER_WORKER && r.distinct_n === ROWS_PER_WORKER);
    verify.close();
    record(
      'B.count_conserved',
      total === ROWS_PER_WORKER * 2 && seqOk,
      `total=${total} perTag=${perTag} seqComplete=${seqOk}`,
    );

    // 判据 C：反向对照（busy_timeout=0）必须真的出现 BUSY
    const setup2 = new DatabaseSync(workerDb);
    setup2.exec('DELETE FROM probe_w');
    setup2.close();
    const round2 = await runRound(workerDb, '-bt0', 0, ROWS_PER_WORKER);
    const busy2 = round2.results.reduce((n, r) => n + ((r.payload && r.payload.busyErrors) || 0), 0);
    record(
      'C.busy_visible_when_no_timeout',
      busy2 > 0,
      `busy_timeout=0 时 busyErrors=${busy2}（>0 才证明并发是真的在争锁）`,
    );

    const failed = checks.filter((c) => !c.ok);
    const report = {
      dbPath,
      tempDb: workerDb,
      rowsPerWorker: ROWS_PER_WORKER,
      busyTimeoutMs: BUSY_TIMEOUT_MS,
      pass: failed.length === 0,
      checks,
    };
    if (process.argv.includes('--json')) {
      process.stdout.write(JSON.stringify(report, null, 2));
    } else {
      console.log(`# probe-concurrent  src=${dbPath}  rows/worker=${ROWS_PER_WORKER}`);
      for (const c of checks) console.log(`${c.ok ? 'PASS' : 'FAIL'}  ${c.id}  ${c.detail}`);
      console.log(`# summary: ${checks.length - failed.length}/${checks.length} pass`);
    }
    process.exitCode = failed.length === 0 ? 0 : 1;
  } finally {
    rmSync(tmpDir, { recursive: true, force: true });
  }
}

main().catch((err) => {
  console.error(`probe-concurrent 失败: ${(err && err.stack) || err}`);
  process.exitCode = 1;
});
