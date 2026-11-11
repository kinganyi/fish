// 第三批-a：启动门禁 fail closed → warn + 降级（2026-11-09）
// 目的：库/宿主结构漂移时不再拒绝启动（"只读补充"定位下，起不来比读不全新更糟）
import { readFileSync, writeFileSync } from 'node:fs';

const p = 'I:/app/fish-public/dsh-fish/src/core/contract.ts';
let t = readFileSync(p, 'utf8');
const before = t;
const miss = [];

function rep(oldStr, newStr, label) {
  if (!t.includes(oldStr)) { miss.push(label); return; }
  t = t.replace(oldStr, newStr);
}

// 1) 类型加 warnings
rep(
  '  readonly specDigestCount: number\n}',
  '  readonly specDigestCount: number\n  /** 降级告警：2026-11-09 起门禁不匹配只警告，不再拒绝启动。 */\n  readonly warnings: readonly string[]\n}',
  'type-warnings',
);

// 2) 版本门
rep(
  `  if (contract.contractVersion !== SUPPORTED_CONTRACT_VERSION) {
    throw new ContractMismatchError(
      \`契约版本不匹配：spec=\${contract.contractVersion} / 插件支持=\${SUPPORTED_CONTRACT_VERSION}\`,
      {
        hint: '三者必须同代：spec、插件、库。先跑 \`fish migrate\` 升级库，再对齐插件与 spec（回滚时同样三件一起回）',
        details: { specDir, specVersion: contract.contractVersion, supported: SUPPORTED_CONTRACT_VERSION },
      },
    )
  }`,
  `  const warnings: string[] = []
  if (contract.contractVersion !== SUPPORTED_CONTRACT_VERSION) {
    // 2026-11-09：fail closed → warn。只读期版本漂移不该让插件起不来。
    warnings.push(
      \`契约版本不匹配：spec=\${contract.contractVersion} / 插件支持=\${SUPPORTED_CONTRACT_VERSION}\`,
    )
  }`,
  'gate-version',
);

// 3) spec 摘要门
rep(
  `  if (!digests.verified) {
    throw new ContractMismatchError(\`spec 自校验失败（\${digests.problems.length} 个文件）\`, {
      hint: '跑 \`node scripts/gen-spec.mjs\` 重新生成 spec/ 与摘要，不要手工改 spec',
      details: { problems: digests.problems },
    })
  }`,
  `  if (!digests.verified) {
    warnings.push(\`spec 自校验失败（\${digests.problems.length} 个文件）：\${digests.problems.slice(0, 3).join('; ')}\`)
  }`,
  'gate-digest',
);

// 4) 宿主版本门
rep(
  `    if (order < 0) {
      throw new ContractMismatchError(\`宿主版本过低：\${hostVersion} < \${minHostVersion}\`, {
        hint: '升级 DSH 宿主，或使用与宿主同代的 dsh-fish 版本',
        details: { hostVersion, minHostVersion },
      })
    }
    hostVersionSatisfied = true`,
  `    if (order < 0) {
      warnings.push(\`宿主版本过低：\${hostVersion} < \${minHostVersion}（继续运行，可能遇到 API 差异）\`)
    } else {
      hostVersionSatisfied = true
    }`,
  'gate-host',
);

// 5) schema 指纹门
rep(
  `  if (!delta.fingerprintMatched) {
    throw new ContractMismatchError('schema 指纹不匹配：现网库结构与 spec 不同代', {
      hint: '先确认库版本（\`fish migrate\` 是否已跑），再重新 \`node scripts/gen-spec.mjs\` 生成 spec',
      details: {
        fingerprintMatched: delta.fingerprintMatched,
        businessMatched: delta.businessMatched,
        missing: delta.missing.slice(0, 12),
        unexpected: delta.unexpected.slice(0, 12),
        changed: delta.changed.slice(0, 12),
      },
    })
  }`,
  `  if (!delta.fingerprintMatched) {
    warnings.push(
      \`schema 指纹不匹配（missing \${delta.missing.length} / unexpected \${delta.unexpected.length} / changed \${delta.changed.length}）——继续运行，只读字段可能失效\`,
    )
  }`,
  'gate-fingerprint',
);

// 6) 返回值加 warnings
rep('    specDigestCount: digests.count,\n  }', '    specDigestCount: digests.count,\n    warnings,\n  }', 'return-warnings');

if (miss.length) {
  console.error('未命中:', miss.join(', '));
  process.exit(2);
}
if (t === before) { console.log('无改动'); process.exit(0); }
writeFileSync(p, t, 'utf8');
const throws = (t.match(/throw new ContractMismatchError/g) || []).length;
console.log('已写盘。剩余 ContractMismatchError throw 数:', throws);
console.log('warnings.push 数:', (t.match(/warnings\.push\(/g) || []).length);
