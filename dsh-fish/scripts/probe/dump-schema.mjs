#!/usr/bin/env node
// 一次性事实采集脚本（T01 步骤 2 的输入）：只读导出 sqlite_master 全量 DDL。
// 生产库只经 mode=ro 打开；本脚本不写任何东西到库。
// 用法: node scripts/probe/dump-schema.mjs [--json]

import { DatabaseSync } from 'node:sqlite';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '..', '..');
const dbPath = process.env.FISH_DB
  ? path.resolve(process.env.FISH_DB)
  : path.resolve(repoRoot, '..', 'p0', 'data', 'index.sqlite');

const db = new DatabaseSync(dbPath, { readOnly: true });
const rows = db
  .prepare("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name")
  .all();

if (process.argv.includes('--json')) {
  process.stdout.write(JSON.stringify({ dbPath, rows }, null, 2));
} else {
  const counts = {};
  for (const r of rows) counts[r.type] = (counts[r.type] || 0) + 1;
  console.log(`db=${dbPath}`);
  console.log(`objects=${rows.length} counts=${JSON.stringify(counts)}`);
  for (const r of rows) {
    console.log(`--- ${r.type} ${r.name}`);
    console.log(r.sql);
  }
}
db.close();
