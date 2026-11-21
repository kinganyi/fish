#!/usr/bin/env node
// 一次性事实采集：事件类型分布 / 可当"真实查询词"的语料 / 21 事件来源对照。
// 只读打开生产库（mode=ro）。
import { DatabaseSync } from 'node:sqlite';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '..', '..');
const dbPath = process.env.FISH_DB
  ? path.resolve(process.env.FISH_DB)
  : path.resolve(repoRoot, '..', 'p0', 'data', 'index.sqlite');

const db = new DatabaseSync(dbPath, { readOnly: true });
const q = (sql, ...args) => db.prepare(sql).all(...args);

console.log('== events by type ==');
for (const r of q('SELECT type, COUNT(*) n FROM events GROUP BY type ORDER BY n DESC')) {
  console.log(`${r.type}\t${r.n}`);
}
console.log('distinct types =', q('SELECT COUNT(DISTINCT type) n FROM events')[0].n);

console.log('\n== memory_access_log sample payload (via) ==');
for (const r of q('SELECT via, substr(payload,1,180) p FROM memory_access_log ORDER BY id DESC LIMIT 8')) {
  console.log(`[${r.via}] ${r.p}`);
}

console.log('\n== code_index sample (code/title/project) ==');
for (const r of q('SELECT code, substr(title,1,60) t, project FROM code_index WHERE status=\'active\' ORDER BY code DESC LIMIT 12')) {
  console.log(`${r.code}\t${r.project}\t${r.t}`);
}

console.log('\n== counts ==');
for (const t of ['code_index', 'memory_fts', 'links', 'citizens', 'session_tokens', 'tool_activity']) {
  console.log(t, q(`SELECT COUNT(*) n FROM ${t}`)[0].n);
}
db.close();
