/**
 * 单元测试（`node --test`）：只测**纯逻辑**与"不需要真实库"的那部分。
 *
 * 边界：凡是需要真实 fish 库的判据都放在 `scripts/check-spec.mjs`（对现网库只读）里——
 * 单元测试不该偷偷连生产库，否则"跑一下测试"就变成了"读一次生产数据"。
 *
 * 2026-11-09 减重第一批删掉了 `test/conformance/`（双跑器与比较器一并作废），
 * 于是这里原先那两条「规范化规则」用例连同 `normalize.mjs` 一起失效并移除——
 * 它们测的那个文件已经不在仓里，留着只会把 `pnpm test` 变成一条断 import。
 *
 * 用法：pnpm test
 */

import assert from 'node:assert/strict'
import path from 'node:path'
import { existsSync } from 'node:fs'
import { fileURLToPath, pathToFileURL } from 'node:url'
import test from 'node:test'

const here = path.dirname(fileURLToPath(import.meta.url))
const packageRoot = path.resolve(here, '..', '..')
const libPath = path.join(packageRoot, 'lib', 'index.js')

if (!existsSync(libPath)) {
  throw new Error(`找不到 ${libPath}：先跑 pnpm run build`)
}
const lib = await import(pathToFileURL(libPath).href)
const spec = lib.loadSpec(path.join(packageRoot, 'spec'))

test('cut：spec 里的全部向量逐 token 相等（oracle = Python 冻结值）', () => {
  const failures = lib.verifyCutVectors(spec.cut)
  assert.deepEqual(failures, [], `切词向量不等：${JSON.stringify(failures.slice(0, 3))}`)
})

test('cut：中文连续段切成重叠 bigram，单字原样，拉丁小写', () => {
  assert.deepEqual(lib.cutTokens('协作约定'), ['协作', '作约', '约定'])
  assert.deepEqual(lib.cutTokens('记'), ['记'])
  assert.deepEqual(lib.cutTokens('ABC def'), ['abc', 'def'])
  assert.deepEqual(lib.cutTokens(''), [])
})

test('cut：MATCH 表达式每个 token 都加双引号（否则 CR-12 会被当成列名）', () => {
  const expr = lib.ftsMatchExpression('CR-12')
  assert.equal(expr, '"cr-12"')
  const composite = lib.ftsMatchExpression('协作约定')
  assert.equal(composite, '"协作" AND "作约" AND "约定"')
  assert.equal(lib.ftsMatchExpression('   '), null)
})

test('契约版本比较：预发布低于同版本正式版，rc 序号按数值比较', () => {
  const cases = [
    ['0.2.0-rc.2', '0.2.0-rc.2', 0],
    ['0.2.0-rc.2', '0.2.0', -1],
    ['0.2.0', '0.2.0-rc.2', 1],
    ['0.2.0-rc.3', '0.2.0-rc.2', 1],
    ['0.1.7-rc.2', '0.2.0-rc.2', -1],
    ['0.2.1', '0.2.0', 1],
    ['1.0', '0.9.9', 1],
  ]
  for (const [left, right, expected] of cases) {
    assert.equal(lib.compareVersions(left, right), expected, `${left} vs ${right}`)
  }
})

test('命令路由：help 成功、未知子命令带用法报错、回显含影子期三条纪律', () => {
  const deps = {
    paths: { packageRoot, specDir: spec.specDir, fishHome: '', dataDir: '', dbPath: '' },
    spec,
    gate: {
      ok: true,
      contractVersion: 'v0',
      supportedContractVersion: 'v0',
      minHostVersion: '0.2.0-rc.2',
      hostVersion: '0.2.0-rc.2',
      hostVersionSatisfied: true,
      fingerprintVerified: true,
      fingerprint: 'a'.repeat(64),
      businessFingerprint: 'b'.repeat(64),
      specFingerprint: 'a'.repeat(64),
      objectCounts: { table: 28 },
      businessTableCount: 21,
      specDigestsVerified: true,
      specDigestCount: 6,
    },
    logger: lib.SILENT_LOGGER,
    config: { mode: 'shadow', budgetTokens: 1600, commandEnabled: true },
  }
  const help = lib.handleFishCommand('', deps)
  assert.equal(help.kind, 'success')
  assert.match(help.text, /shadow status/)

  const unknown = lib.handleFishCommand('write memory', deps)
  assert.equal(unknown.kind, 'error')
  assert.match(unknown.text, /未知子命令/)

  const contract = lib.handleFishCommand('shadow contract', deps)
  assert.equal(contract.kind, 'success')
  assert.match(contract.text, /contractVersion：v0/)
  assert.match(contract.text, /impl python=18/)
})

test('配置校验：非法 logLevel / budgetTokens 必须抛错，缺省值落到影子期安全值', () => {
  assert.throws(() => lib.normalizeConfig({ logLevel: 'verbose' }), /logLevel 非法/)
  assert.throws(() => lib.normalizeConfig({ budgetTokens: 0 }), /budgetTokens 必须为正整数/)
  const defaults = lib.normalizeConfig({})
  assert.equal(defaults.mode, 'shadow')
  assert.equal(defaults.commandEnabled, true)
  assert.equal(defaults.strictHostVersion, false)
})

test('影子纪律常量：模型面登记数必须为 0（影子期不抢注册 / 不注入）', () => {
  assert.equal(lib.MODEL_SURFACE.toolRegistrations, 0)
  assert.equal(lib.MODEL_SURFACE.promptInjections, 0)
})
