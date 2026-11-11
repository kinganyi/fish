// 更新 dsh-fish README：加"减重状态"段 + 修正过时读数
import { readFileSync, writeFileSync } from 'node:fs';

const p = 'I:/app/fish-public/dsh-fish/README.md';
let t = readFileSync(p, 'utf8');

const HEADER = `# dsh-fish

> ## ⚠️ 2026-11-09 方向变更 + 减重状态（**先读这段，下面正文有部分已过时**）
>
> 用户拍板：「后续不准备在 DSH 进行全量实现了，DSH 侧的插件作为**只读和本体能力的补充**就行」+「DSH 更新频率高、破坏性大，插件侧要轻」。
>
> **减重已完成两批**（commit \`82558b7\` / \`674db78\`）：
> - 批次 1：删 \`test/conformance/\`（117 用例 + 8.2 MB 快照）、\`.tmp/\`、\`scripts/probe/\`、\`NOTES-T0*.md\` → **16.2 MB → 3.2 MB**
> - 批次 2：删 9 个领域模块（\`inject\` / \`board\` / \`push\` / \`profile\` / \`activity\` / \`naming\` / \`surface\` / \`links\` / \`freshness\`，共 4,238 行），新增极简 \`domain/presence.ts\` → **\`lib/index.js\` 458 KB → 328 KB**
>
> **当前能力面**（真跑过的）：
> - \`/fish shadow presence\`（在场 + 最近动态，面板数据源）、\`recall\`、\`list\`、\`whoami\`、\`health\`
> - \`inject\` 已降为 \`python-only\`（只给指引，不执行）——用终端 \`fish inject\`
> - 面板（\`conversation.view\` 槽位的 canvas）仍在，产物 \`lib/client.js\` 9.33 KB
>
> **判据现状**：\`typecheck 0\` / \`build 0\` / \`unit 54/54\` / \`check-spec 30/33\`（3 条红是"probe 脚本已删"，不是本批引入）。
>
> **下面正文的 §2/§3/§4 描述的是减重前的状态**（T01-T06 批次记录），保留作历史；**新增能力以本段为准**。完整裁决见 [\`docs/减重清单-20261109.md\`](docs/减重清单-20261109.md)。

`;

if (!t.includes('2026-11-09 方向变更 + 减重状态')) {
  t = t.replace(/^# dsh-fish\n+/, HEADER);
}

// 修正过时读数：命令树叶子数
t = t.replace('命令树实测叶子是 76。', '命令树实测叶子是 80（2026-11-09 实测；旧文写 76 是过时读数）。');

// 给"新增领域层"那段加删除标注
t = t.replace(
  '- **新增领域层**：`board`（看板段与其取数链',
  '- **新增领域层（⚠️ 2026-11-09 已删，见顶部减重段）**：`board`（看板段与其取数链');

writeFileSync(p, t, 'utf8');
console.log('README 已更新');
console.log('  含减重段:', t.includes('2026-11-09 方向变更 + 减重状态'));
console.log('  76→80:', !t.includes('命令树实测叶子是 76。'));
