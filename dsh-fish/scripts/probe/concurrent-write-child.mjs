#!/usr/bin/env node
/**
 * 先验假设 #3 的写侧工人：一个独立进程，用自己的连接往临时库写 N 行。
 *
 * 只用 `BEGIN IMMEDIATE` + `busy_timeout`，不重试——重试会把"锁竞争到底有没有被
 * 处理掉"这件事抹平，于是压测变成自证成功。
 *
 * 用法：node concurrent-write-child.mjs <dbPath> <tag> <rows> <busyTimeoutMs> <resultFile>
 * 退出码：0 = 全部写入成功且无未处理 BUSY；其它 = 失败（结果文件里写明原因）。
 */

import { DatabaseSync } from 'node:sqlite';
import { writeFileSync } from 'node:fs';

const [, , dbPath, tag, rowsArg, busyArg, resultFile] = process.argv;
const rows = Number(rowsArg);
const busyTimeout = Number(busyArg);

/** @type {{tag: string, attempted: number, written: number, busyErrors: number, otherErrors: string[]}} */
const result = { tag, attempted: rows, written: 0, busyErrors: 0, otherErrors: [] };

try {
  if (!dbPath || !tag || !Number.isInteger(rows) || rows <= 0) {
    throw new Error(`参数非法: dbPath=${dbPath} tag=${tag} rows=${rowsArg}`);
  }
  const db = new DatabaseSync(dbPath);
  db.exec(`PRAGMA busy_timeout=${Number.isFinite(busyTimeout) ? busyTimeout : 5000}`);
  const insert = db.prepare('INSERT INTO probe_w (tag, seq, payload) VALUES (?, ?, ?)');

  for (let i = 1; i <= rows; i += 1) {
    try {
      db.exec('BEGIN IMMEDIATE');
      insert.run(tag, i, `${tag}#${i}`);
      db.exec('COMMIT');
      result.written += 1;
    } catch (err) {
      try {
        db.exec('ROLLBACK');
      } catch {
        /* 事务已经不在活动状态，回滚失败不影响判定。 */
      }
      const message = String((err && err.message) || err);
      if (/BUSY|LOCKED/i.test(message)) result.busyErrors += 1;
      else result.otherErrors.push(`${i}: ${message}`);
    }
  }
  db.close();
} catch (err) {
  result.otherErrors.push(`fatal: ${(err && err.stack) || err}`);
}

writeFileSync(resultFile, JSON.stringify(result), 'utf8');
process.exitCode =
  result.busyErrors === 0 && result.otherErrors.length === 0 && result.written === rows ? 0 : 1;
