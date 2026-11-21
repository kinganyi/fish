#!/usr/bin/env node
// 一次性：取出 whoami fixture 要用的真实公民（主人优先），只读打开生产库。
import { DatabaseSync } from 'node:sqlite'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const repoRoot = path.resolve(here, '..', '..')
const db = new DatabaseSync(path.resolve(repoRoot, '..', 'p0', 'data', 'index.sqlite'), { readOnly: true })

const owner = db.prepare('SELECT citizen_id, display_name, kind, host, status FROM citizens WHERE is_owner=1 LIMIT 3').all()
const active = db.prepare("SELECT citizen_id, display_name, kind, host, status FROM citizens WHERE status='active' ORDER BY created_at DESC LIMIT 3").all()
console.log('owner:', JSON.stringify(owner))
console.log('active:', JSON.stringify(active))
console.log('ownerCitizenConfig:', JSON.stringify(db.prepare("SELECT key,value FROM config WHERE key IN ('owner_citizen','actor_name')").all()))
db.close()
