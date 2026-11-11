// 清理 package.json 里指向已删目录的 scripts（第一批减重）
import { readFileSync, writeFileSync } from 'node:fs';

const p = 'I:/app/fish-public/dsh-fish/package.json';
const j = JSON.parse(readFileSync(p, 'utf8'));

const DROP = ['probe', 'gen:snapshot', 'conformance', 'conformance:self-test',
  'conformance:mutation', 'conformance:mutation:t03', 'conformance:mutation:t035',
  'conformance:mutation:t04', 'conformance:mutation:t05', 'check:cli', 'check:hosts'];

const dropped = [];
for (const k of DROP) {
  if (k in j.scripts) { delete j.scripts[k]; dropped.push(k); }
}

// verify 脚本也要同步收窄（它串了一堆已删的）
if (j.scripts.verify) {
  j.scripts.verify = 'pnpm run typecheck && pnpm run build && pnpm run check:spec && pnpm run check:contract && pnpm run check:readonly && pnpm test';
}

writeFileSync(p, JSON.stringify(j, null, 2) + '\n', 'utf8');
console.log('已删 scripts:', dropped.join(', '));
console.log('剩余 scripts:', Object.keys(j.scripts).join(', '));
