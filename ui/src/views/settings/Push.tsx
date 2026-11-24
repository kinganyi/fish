import { useCallback, useEffect, useState } from 'react'
import { describeError } from '../../api/client'
import {
  fetchPushEvents,
  fetchPushOverview,
  postPushConfig,
  postPushPreset,
} from '../../api/endpoints'
import type {
  PushBreakdown,
  PushBreakdownRow,
  PushChannel,
  PushConfigItem,
  PushEvent,
  PushKbTopFile,
  PushOverviewData,
  PushPreset,
  PushTopCode,
} from '../../api/types'
import { Block } from '../../components/AsyncBoundary'
import { UNKNOWN, num, percent, stampLongOf, stampOf } from '../../format'
import type { LoadState } from '../../store/workbench'

/**
 * 设置 · 推送 —— **「推送到底有没有用、为什么没用、想调哪里」的唯一界面**。
 *
 * 这一屏把四件事分开摆，因为它们的错法不同：
 *
 *  1. **收益侧**（采纳率/盲区消除率）与**成本侧**（违规率）**分栏**：合成一个「总分」会让
 *     「不说话」看起来最安全（沉默永远没有误报）。所以四张指标卡各自带口径小字，
 *     分母写在卡上，不做任何跨卡换算。
 *  2. **诊断**回答「为什么没用」：收件人项目未知（不知道推给谁）、开放词表占比（推的都
 *     是「下一步/待办」这类没闭合的东西）、artifact 占比（推的是文件不是结论）、
 *     重复推送（同会话同一天同一编码推几百次）。
 *  3. **调整**只开放 7 个「口味」键（频率/开关/预算/两条下限）。`push.enabled` 与
 *     `factors/rrf/mmr/rerank/corpus` **只读**并在页面上说明为什么：`push.enabled=false`
 *     会连 `tool_activity` 采集一起停（`fish_push_hook.py:138`），那是「把仪表盘关掉」
 *     而不是「调安静」——想止血请用「止血预设」（它只动那三个口味键）。
 *  4. **二级拆解**回答「这些推送是什么东西」：左「内容源」= 记忆（类型/项目/通道/开放词表）
 *     → 知识库（目录/通道/top 文件）→ 语料（现场关闭说明）；右「态势源」= 看板（项目/看板）
 *     → 动作流（目录）→ git（仓库）。**每组最多 8 行 + 一行「其他（N 项）」**，
 *     合并行也带采纳/待判（后端算好，前端不重算）；组 total 为 0 时写一句「本窗口没有
 *     可拆的推送」而不是画一排 0——0 是读数，缺是状态，两者文案必须不同（96 §七）。
 *     `breakdown` 缺失（读口是旧版）时明说「该区数据未接：缺 breakdown」，不画空图。
 *     **相加纪律**：语料（`conv:` 前缀）按既有契约落在 `memory` 桶里（`classify_kind`
 *     把它归 memory，`byType` 显示成「未分类」18 条），所以汇总行写「记忆 X（其中语料
 *     历史 Y…）」、语料卡写「已计入记忆，不重复相加」——**不许**让三个数字看起来可以
 *     相加成总数（V-31 有一条判据钉 `memory+kb+board+activity+git == totals.total`）。
 *
 * 写口纪律：所有写操作都要 `window.confirm` 二次确认，成功后 **重新拉一次 overview**
 * （不拿本地值假装已生效）；失败把后端原文贴出来（`bad_key` / `bad_value` 的后半句
 * 就是范围与原因，翻译一遍只会丢信息）。白名单与范围判定**全在后端**，前端不复制一份。
 */
const WINDOWS = [7, 30] as const
const CHANNEL_NAMES: Record<string, string> = {
  inject: '开场注入',
  prompt: '用户消息边界',
  tool_after: '工具后',
}
const REACTION_NAMES: Record<string, string> = {
  '': '待判',
  accepted: '采纳',
  ignored: '忽略',
}
const KIND_NAMES: Record<string, string> = {
  memory: '记忆',
  artifact: '产物',
  episode: '流水',
  fact: '事实',
  rule: '规矩',
  lesson: '教训',
  board: '看板',
  kb: '知识库',
  activity: '工具动作',
  git: '提交',
}
const PRESET_TEXT: Record<PushPreset, { label: string; detail: string }> = {
  pause: {
    label: '止血预设',
    detail: '工具后每 9999 次最多一条（等于停）+ 关掉用户消息边界提示 + 开场推送预算 0',
  },
  resume: {
    label: '恢复默认',
    detail: '工具后每 10 次最多一条 + 打开用户消息边界提示 + 开场推送预算 450',
  },
}

function SectionTitle({ title, hint }: { title: string; hint: string }) {
  return (
    <div className="mb-2 flex flex-wrap items-baseline gap-2">
      <h3 className="text-sub font-name text-n7">{title}</h3>
      <span className="text-meta text-n6">{hint}</span>
    </div>
  )
}

function Metric({
  metric,
  label,
  value,
  note,
  tone = '',
}: {
  metric: string
  label: string
  value: string
  note: string
  tone?: string
}) {
  return (
    <div data-push-metric={metric} className="min-w-0 rounded border border-n3 bg-n0 px-3 py-2">
      <p className="text-meta text-n6">{label}</p>
      <p className={`mt-0.5 text-title font-head tabular-nums ${tone || 'text-n7'}`}>{value}</p>
      <p className="mt-0.5 text-meta leading-4 text-n6">{note}</p>
    </div>
  )
}

function DiagLine({ label, value, note }: { label: string; value: string; note: string }) {
  return (
    <li className="flex flex-wrap items-baseline gap-2 border-b border-n3/60 py-1">
      <span className="w-40 shrink-0 text-meta text-n6">{label}</span>
      <span className="font-mono text-sub text-n7 tabular-nums">{value}</span>
      <span className="min-w-0 flex-1 truncate text-meta text-n6" title={note}>
        {note}
      </span>
    </li>
  )
}

function ChannelRow({ item, max }: { item: PushChannel; max: number }) {
  const width = max > 0 ? Math.round((item.total / max) * 100) : 0
  return (
    <li data-push-channel={item.channel} className="border-b border-n3/60 py-1.5">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="w-28 shrink-0 truncate font-mono text-sub text-n7">
          {item.channel}
        </span>
        <span className="w-24 shrink-0 truncate text-meta text-n6">
          {CHANNEL_NAMES[item.channel] ?? ''}
        </span>
        <span className="w-16 shrink-0 text-right font-mono text-sub tabular-nums text-n7">
          {num(item.total)}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block h-1.5 w-full overflow-hidden rounded bg-n2">
            <span
              className="block h-full rounded bg-accent"
              style={{ width: `${width}%` }}
              aria-hidden="true"
            />
          </span>
        </span>
        <span className="w-52 shrink-0 text-right font-mono text-meta tabular-nums text-n6">
          采纳 {num(item.accepted)} · 忽略 {num(item.ignored)} · 待判 {num(item.pending)}
        </span>
        <span className="w-16 shrink-0 text-right font-mono text-sub tabular-nums text-n7">
          {percent(item.acceptRate)}
        </span>
      </div>
    </li>
  )
}

function TopRow({ item, index }: { item: PushTopCode; index: number }) {
  return (
    <li data-push-top-code={item.code} className="border-b border-n3/60 py-1.5">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="w-6 shrink-0 text-right font-mono text-meta text-n6">{index + 1}</span>
        <span className="w-16 shrink-0 text-right font-mono text-sub tabular-nums text-n7">
          {num(item.count)}
        </span>
        <span className="min-w-0 flex-1 truncate font-mono text-sub text-n7" title={item.code}>
          {item.code}
        </span>
        <span className="w-20 shrink-0 text-meta text-n6">{KIND_NAMES[item.kind] ?? item.kind}</span>
        <span className="w-24 shrink-0 truncate text-meta text-n6" title={item.project ?? ''}>
          {item.project ?? '未归属'}
        </span>
        <span className="w-20 shrink-0 text-right font-mono text-meta tabular-nums text-n6">
          采纳 {num(item.accepted)}
        </span>
        <span className="w-24 shrink-0 text-right font-mono text-meta tabular-nums text-n6">
          {item.lastAt ? stampOf(item.lastAt) : UNKNOWN}
        </span>
      </div>
      {item.title ? (
        <p className="mt-0.5 truncate pl-8 text-meta text-n6" title={item.title}>
          {item.title}
        </p>
      ) : null}
    </li>
  )
}

function BreakdownRow({ item }: { item: PushBreakdownRow }) {
  // 条宽按**组内占比**（`share`）而不是「本组最大行」：`share` 是后端给的同一把尺子，
  // 前端不再算一遍（算两遍就会漂）。`null` = 组 total 为 0，那时压根没有行。
  const width = item.share === null ? 0 : Math.round(Math.max(0, Math.min(1, item.share)) * 100)
  return (
    <li data-breakdown-key={item.key} className="border-b border-n3/40 py-1">
      <div className="flex min-w-0 items-baseline gap-2">
        <span className="min-w-0 flex-1 truncate text-meta text-n7" title={item.label}>
          {item.label}
        </span>
        <span className="w-14 shrink-0 text-right font-mono text-meta tabular-nums text-n7">
          {num(item.total)}
        </span>
        <span className="w-12 shrink-0 text-right font-mono text-meta tabular-nums text-n6">
          {percent(item.share)}
        </span>
      </div>
      <div className="mt-1 flex min-w-0 items-center gap-2">
        <span className="block h-1 min-w-0 flex-1 overflow-hidden rounded bg-n2">
          <span
            className="block h-full rounded bg-accent"
            style={{ width: `${width}%` }}
            aria-hidden="true"
          />
        </span>
        <span className="shrink-0 font-mono text-meta tabular-nums text-n6">
          采纳 {num(item.accepted)} · 待判 {num(item.pending)}
        </span>
      </div>
    </li>
  )
}

/** 一个子组：一行小标题 + 若干 `BreakdownRow`；空组写一句话而不是画 0。 */
function BreakdownGroup({
  group,
  title,
  hint,
  rows,
  empty = '本窗口没有可拆的推送',
}: {
  group: string
  title: string
  hint: string
  rows: PushBreakdownRow[]
  empty?: string
}) {
  return (
    <div data-breakdown-group={group} className="mt-2 min-w-0 first:mt-0">
      <div className="flex flex-wrap items-baseline gap-2">
        <p className="font-name text-meta text-n7">{title}</p>
        <span className="min-w-0 truncate text-meta text-n6" title={hint}>
          {hint}
        </span>
      </div>
      {rows.length === 0 ? (
        <p className="py-0.5 text-meta text-n6">{empty}</p>
      ) : (
        <ul className="mt-0.5">
          {rows.map((item) => (
            <BreakdownRow key={item.key} item={item} />
          ))}
        </ul>
      )}
    </div>
  )
}

/** 知识库 top 文件：`rel`（vault 内相对路径）与 `title` 至少一个可读。 */
function TopFileRow({ item }: { item: PushKbTopFile }) {
  const label = item.rel || item.title
  return (
    <li data-breakdown-key={item.code} className="border-b border-n3/40 py-1">
      <div className="flex min-w-0 items-baseline gap-2">
        <span className="min-w-0 flex-1 truncate font-mono text-meta text-n7" title={item.code}>
          {label}
        </span>
        <span className="w-14 shrink-0 text-right font-mono text-meta tabular-nums text-n7">
          {num(item.total)}
        </span>
      </div>
      <p className="mt-0.5 font-mono text-meta tabular-nums text-n6">
        采纳 {num(item.accepted)} · 待判 {num(item.pending)}
        {item.title ? ` · ${item.title}` : ''}
      </p>
    </li>
  )
}

function ConfigRow({
  item,
  draft,
  busy,
  onDraft,
  onSave,
}: {
  item: PushConfigItem
  draft: string
  busy: boolean
  onDraft: (value: string) => void
  onSave: () => void
}) {
  const sourceNote = item.source === 'db' ? '库里有这一行' : '未设置（吃默认值）'
  return (
    <li data-push-config={item.key} className="border-b border-n3/60 py-2">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="min-w-0 text-sub text-n7">{item.label}</span>
        <span className="font-mono text-meta text-n6">{item.key}</span>
        <span className="ml-auto flex items-center gap-2">
          {item.type === 'bool' ? (
            <select
              aria-label={`${item.label}（当前 ${String(item.value)}）`}
              value={draft}
              disabled={busy}
              onChange={(event) => onDraft(event.target.value)}
              className="rounded border border-n4 bg-n0 px-2 py-1 text-meta text-n7"
            >
              <option value="true">开</option>
              <option value="false">关</option>
            </select>
          ) : (
            <input
              aria-label={`${item.label}（当前 ${String(item.value)}）`}
              type="number"
              inputMode="decimal"
              value={draft}
              min={item.min ?? undefined}
              max={item.max ?? undefined}
              step={item.type === 'float' ? 0.01 : 1}
              disabled={busy}
              onChange={(event) => onDraft(event.target.value)}
              className="w-24 rounded border border-n4 bg-n0 px-2 py-1 font-mono text-meta text-n7"
            />
          )}
          <button
            type="button"
            disabled={busy}
            onClick={onSave}
            className="rounded border border-n4 bg-n0 px-2 py-1 text-meta text-n7 hover:bg-n2 active:bg-n3 disabled:text-n5"
          >
            {busy ? '写入中…' : '保存'}
          </button>
        </span>
      </div>
      <p className="mt-0.5 text-meta text-n6">{item.help}</p>
      <p className="mt-0.5 font-mono text-meta tabular-nums text-n6">
        现场值 {String(item.value)} · 默认 {String(item.default)} · 来源 {sourceNote}
        {item.min !== null || item.max !== null ? ` · 范围 ${item.min}–${item.max}` : ''}
        {item.updatedAt ? ` · 上次写 ${stampOf(item.updatedAt)}` : ''}
      </p>
    </li>
  )
}

function EventRow({ item }: { item: PushEvent }) {
  return (
    <li data-push-event={item.id} className="flex flex-wrap items-baseline gap-2 border-b border-n3/60 py-1">
      <span className="w-20 shrink-0 font-mono text-meta text-n6">{stampOf(item.pushedAt)}</span>
      <span className="w-24 shrink-0 truncate font-mono text-meta text-n7">{item.channel}</span>
      <span className="w-44 shrink-0 truncate font-mono text-meta text-n7" title={item.code}>
        {item.code}
      </span>
      <span className="min-w-0 flex-1 truncate text-meta text-n6" title={item.title || item.code}>
        {item.title || '（没有标题：这条编码不在 code_index 里）'}
      </span>
      <span className="w-20 shrink-0 truncate text-meta text-n6">{item.project ?? '未归属'}</span>
      <span
        className={`w-12 shrink-0 text-right text-meta ${
          item.reaction === 'accepted'
            ? 'text-ok'
            : item.reaction === 'ignored'
              ? 'text-n5'
              : 'text-warn'
        }`}
      >
        {REACTION_NAMES[item.reaction] ?? item.reaction}
      </span>
    </li>
  )
}

function draftOf(item: PushConfigItem): string {
  if (item.type === 'bool') return item.value ? 'true' : 'false'
  return String(item.value)
}

export function Push() {
  const [days, setDays] = useState<number>(30)
  const [overview, setOverview] = useState<PushOverviewData | null>(null)
  const [state, setState] = useState<LoadState>('idle')
  const [error, setError] = useState<string | null>(null)

  const [events, setEvents] = useState<PushEvent[]>([])
  const [eventsState, setEventsState] = useState<LoadState>('idle')
  const [eventsError, setEventsError] = useState<string | null>(null)

  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [busyKey, setBusyKey] = useState<string | null>(null)
  const [busyPreset, setBusyPreset] = useState<PushPreset | null>(null)
  const [writeError, setWriteError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const loadOverview = useCallback(async () => {
    setState('loading')
    setError(null)
    try {
      const envelope = await fetchPushOverview(days)
      const data = envelope.data
      setOverview(data)
      setDrafts(
        Object.fromEntries(
          data.config.filter((item) => item.writable).map((item) => [item.key, draftOf(item)]),
        ),
      )
      setState('ok')
    } catch (err) {
      setState('error')
      setError(describeError(err))
    }
  }, [days])

  const loadEvents = useCallback(async () => {
    setEventsState('loading')
    setEventsError(null)
    try {
      const envelope = await fetchPushEvents({ limit: 20 })
      setEvents(envelope.data.items)
      setEventsState('ok')
    } catch (err) {
      setEventsState('error')
      setEventsError(describeError(err))
    }
  }, [])

  useEffect(() => {
    void loadOverview()
  }, [loadOverview])

  useEffect(() => {
    void loadEvents()
  }, [loadEvents])

  const saveKey = useCallback(
    async (item: PushConfigItem) => {
      const raw = drafts[item.key] ?? ''
      let value: string | number | boolean
      if (item.type === 'bool') {
        value = raw === 'true'
      } else {
        const parsed = Number(raw)
        if (raw.trim() === '' || !Number.isFinite(parsed)) {
          setWriteError(`${item.key} 要一个数字，收到 ${JSON.stringify(raw)}（没有发出去）`)
          setNotice(null)
          return
        }
        value = parsed
      }
      const confirmed = window.confirm(
        `把 ${item.key} 从「${String(item.value)}」改成「${String(value)}」？\n` +
          '这会立刻写库，下一次推送就按新值走。',
      )
      if (!confirmed) return
      setBusyKey(item.key)
      setWriteError(null)
      setNotice(null)
      try {
        const envelope = await postPushConfig(item.key, value)
        const result = envelope.data
        // 三种结果的文案必须分开（S6/评审都盯这一处）：
        //  ① 真写了 → 成功；② 值没变 → **什么都没写**，不是错误；③ 真失败 → catch 里贴后端原文。
        if (!result.changed) {
          setNotice(
            `${result.key} 值未变化（现场值就是 ${String(result.new)}）：` +
              '没写库，也没有写审计——这不代表失败。',
          )
        } else {
          setNotice(
            `已写入 ${result.key}：${String(result.old)} → ${String(result.new)}` +
              (result.auditAt
                ? `（审计 ${stampLongOf(result.auditAt)}）`
                : '（写入已生效，但审计那一行没落盘，看服务端日志）'),
          )
        }
        await loadOverview()
      } catch (err) {
        // 后端原文直出：`bad_key` / `bad_value` 的后半句就是原因与范围，翻译一遍会丢信息。
        setWriteError(describeError(err))
      } finally {
        setBusyKey(null)
      }
    },
    [drafts, loadOverview],
  )

  const applyPreset = useCallback(
    async (preset: PushPreset) => {
      const text = PRESET_TEXT[preset]
      const confirmed = window.confirm(`${text.label}：${text.detail}。\n这会立刻写库，继续？`)
      if (!confirmed) return
      setBusyPreset(preset)
      setWriteError(null)
      setNotice(null)
      try {
        const envelope = await postPushPreset(preset)
        const result = envelope.data
        // 重复应用同一个预设（`changedCount=0`）＝「现场值已经就是这样」，
        // 不是审计失败：它压根没写库，也就没有审计行可写。
        if (result.changedCount === 0) {
          setNotice(
            `${result.label}：值未变化（${result.changes.length} 个键现场值已经就是这样）：` +
              '没写库，也没有写审计——这不代表失败。',
          )
        } else {
          setNotice(
            `${result.label}：改了 ${result.changedCount} 项` +
              (result.auditAt
                ? `（审计 ${stampLongOf(result.auditAt)}）`
                : '（写入已生效，但审计那几行没落盘，看服务端日志）'),
          )
        }
        await loadOverview()
      } catch (err) {
        setWriteError(describeError(err))
      } finally {
        setBusyPreset(null)
      }
    },
    [loadOverview],
  )

  const totals = overview?.totals
  const diag = overview?.diagnostics
  // `breakdown` 是**可选块**（旧读口没有）：`undefined` 时画「数据未接」，不画空图。
  const breakdown: PushBreakdown | null = overview?.breakdown ?? null
  const writable = overview?.config.filter((item) => item.writable) ?? []
  const readonly = overview?.config.filter((item) => !item.writable) ?? []
  const maxChannel = Math.max(0, ...(overview?.channels ?? []).map((item) => item.total))
  const enabled = readonly.find((item) => item.key === 'push.enabled')
  // 缓存年龄与构建时刻单独取出来：`asOf` 是**数据时刻**，命中缓存时它就等于 `cachedAt`，
  // 年龄单列（不把「现在」写成「数据截至」——评审 🟡-2）。
  const cacheAgeSec = overview?.cacheAgeSec ?? 0
  const cachedAt = overview?.cachedAt ?? null

  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col overflow-hidden bg-n1" data-push-page>
      <div
        data-push-block="toolbar"
        className="flex flex-wrap items-center gap-3 border-b border-n3 bg-n0 px-4 py-2"
      >
        <h2 className="text-section font-head text-n7">推送</h2>
        <span className="min-w-0 truncate text-meta text-n6">有没有用 · 为什么没用 · 想调哪里</span>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <div className="flex items-center gap-1" role="group" aria-label="时间窗">
            {WINDOWS.map((option) => (
              <button
                key={option}
                type="button"
                aria-pressed={days === option}
                data-push-window={option}
                onClick={() => setDays(option)}
                className={`rounded border px-2 py-1 text-meta ${
                  days === option
                    ? 'border-n4 bg-accent-soft font-name text-accent'
                    : 'border-n3 bg-n0 text-n6 hover:bg-n2'
                }`}
              >
                {option} 天
              </button>
            ))}
          </div>
          <button
            type="button"
            data-push-action="refresh"
            disabled={state === 'loading'}
            onClick={() => {
              void loadOverview()
              void loadEvents()
            }}
            className="rounded border border-n4 bg-n0 px-2 py-1 text-meta text-n7 hover:bg-n2 active:bg-n3 disabled:text-n5"
          >
            {state === 'loading' ? '读…' : '刷新'}
          </button>
          {/* 「数据截至」= 这份数据**算出来的时刻**，不是「此刻」。
              读口有 15 s 缓存（`fish_push_audit.CACHE_TTL_S`），命中缓存时 `asOf` 仍是
              缓存构建时刻、`cachedAt` 同一个值、`cacheAgeSec` 是这份数据已经放了多久。
              拿当前时刻冒充数据时刻，就是让一屏旧数字自称「刚刚」（评审 🟡-2）。 */}
          <span
            className="font-mono text-meta text-n6"
            data-push-asof={overview?.asOf ?? ''}
            title={cachedAt ? `缓存构建于 ${cachedAt}` : undefined}
          >
            数据截至 {overview ? stampLongOf(overview.asOf) : UNKNOWN}
            {cacheAgeSec >= 1 && cachedAt
              ? `（缓存于 ${stampLongOf(cachedAt)}，${Math.round(cacheAgeSec)} 秒前）`
              : ''}
          </span>
          <span className="rounded border border-n3 bg-n1 px-2 py-1 text-meta text-n6">
            诊断只读 · 可写 {writable.length} 项（白名单 + 审计）
          </span>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        <Block
          state={state}
          cap="push"
          errorText={error}
          onRetry={() => void loadOverview()}
        >
          {totals && diag ? (
            <div className="space-y-4 p-4">
              {/* ① 诊断提要：四个率各有分母，不做跨卡换算 */}
              <section data-push-block="diag" aria-label="诊断提要">
                <SectionTitle
                  title="诊断提要"
                  hint="四个率的分母各不相同；成本侧单独一栏，不与收益合并"
                />
                <div
                  className="grid gap-3"
                  style={{ gridTemplateColumns: 'repeat(4, minmax(0, 1fr))' }}
                >
                  <Metric
                    metric="total"
                    label="推送总量"
                    value={num(totals.total)}
                    note={`近 ${overview?.window.days ?? days} 天 · 采纳 ${num(totals.accepted)} / 忽略 ${num(totals.ignored)} / 待判 ${num(totals.pending)}`}
                  />
                  <Metric
                    metric="accept"
                    label="采纳率"
                    value={percent(totals.acceptRate)}
                    note={`分母 = 已判 ${num(totals.decided)}（待判不算；算不出时显示判不到而不是 0）`}
                  />
                  <Metric
                    metric="gap"
                    label="盲区消除率"
                    value={percent(totals.gapClearRate)}
                    note={`推后 7 天内真的读了正文：${num(totals.gapCleared)} / ${num(totals.total)}`}
                  />
                  <Metric
                    metric="violation"
                    label="违规率（成本侧）"
                    value={percent(totals.violationRate)}
                    tone="text-warn"
                    note={`工具后推送 ${num(totals.toolAfterTotal)} 次里，5 分钟内该会话毫无动静的比例`}
                  />
                </div>
                <ul className="mt-2">
                  <DiagLine
                    label="收件人项目未知率"
                    value={percent(diag.receiver.unknownRate)}
                    note={`会话 ${num(diag.receiver.sessions)} 个里 ${num(diag.receiver.unknownSessions)} 个没有项目归属；工具动作里 ${percent(diag.receiver.activityUnknownRate)} 没有项目；workspace_map ${diag.receiver.workspaceMapRows === null ? UNKNOWN : `${num(diag.receiver.workspaceMapRows)} 条`}`}
                  />
                  <DiagLine
                    label="开放词表占比"
                    value={percent(diag.openLoop.share)}
                    note={`${num(diag.openLoop.pushes)} 次推送来自 ${num(diag.openLoop.codes)} 个标题命中「${diag.openLoop.keywords.join('、')}」的编码`}
                  />
                  <DiagLine
                    label="产物类推送"
                    value={`${num(diag.artifact.pushes)} / ${num(diag.artifact.accepted)}`}
                    note="推送 / 采纳（mem_type=artifact）：产物占大头说明推的是文件，不是结论"
                  />
                  <DiagLine
                    label="重复推送"
                    value={`${num(diag.duplicates.sessionCodeDayGroups)} 组`}
                    note={
                      diag.duplicates.maxDetail
                        ? `同会话 + 同编码 + 同一天；最大一组 ${num(diag.duplicates.maxPerSessionDay)} 次（${diag.duplicates.maxDetail}）`
                        : '同会话 + 同编码 + 同一天，没有 2 次以上的组'
                    }
                  />
                </ul>
              </section>

              {/* ② 通道 */}
              <section data-push-block="channels" aria-label="通道">
                <SectionTitle title="通道" hint="同一条推送走哪条通道；采纳率分母 = 已判" />
                <ul>
                  {(overview?.channels ?? []).map((item) => (
                    <ChannelRow key={item.channel} item={item} max={maxChannel} />
                  ))}
                </ul>
              </section>

              {/* ③ 二级拆解：内容源（记忆/知识库/语料）+ 态势源（看板/动作流/git）。
                  两组「同一个窗口、同一批推送行」，左右分开是为了不让 5183 条记忆的
                  类型分布淹掉那 2 条 git 推送——它们的量级差三个数量级，但回答的是
                  两个不同的问题（推了什么内容 / 推送被哪些态势面触发的）。 */}
              <section data-push-block="breakdown" aria-label="二级拆解">
                <SectionTitle
                  title="二级拆解"
                  hint="内容源按类型/项目/通道，态势源按作用域/目录/仓库"
                />
                {breakdown ? (
                  <div
                    className="grid gap-4"
                    style={{ gridTemplateColumns: 'repeat(2, minmax(0, 1fr))' }}
                  >
                    {/* 左：内容源 */}
                    <div data-breakdown-side="content" className="min-w-0 space-y-3">
                      <p className="flex flex-wrap items-baseline gap-2">
                        <span className="font-name text-sub text-n7">内容源</span>
                        {/* 语料（`conv:`）按既有契约落在 memory 桶里，**已含在**记忆那个数里
                            （byType 显示成「未分类」），所以这里写「其中」+「已计入记忆」，
                            而不是把三个数字并列——并列会让人相加成总数（V-31 钉了相加关系）。 */}
                        <span className="text-meta text-n6">
                          记忆 {num(breakdown.memory.total)}（其中语料历史{' '}
                          {num(breakdown.corpus.total)}）· 知识库 {num(breakdown.kb.total)} ·
                          语料：{breakdown.corpus.enabled ? '现场开着' : '关闭'}（
                          {num(breakdown.corpus.total)} 条历史行，已计入记忆）
                        </span>
                      </p>

                      <div className="min-w-0 rounded border border-n3 bg-n0 px-3 py-2">
                        <p className="mb-1 flex flex-wrap items-baseline gap-2">
                          <span className="font-name text-sub text-n7">记忆</span>
                          <span className="text-meta text-n6">
                            {num(breakdown.memory.total)} 次 · 类型 / 项目 / 通道 / 开放词表
                          </span>
                        </p>
                        <BreakdownGroup
                          group="memory.byType"
                          title="类型"
                          hint="ci.mem_type（认不出 → 未分类）"
                          rows={breakdown.memory.byType}
                        />
                        <BreakdownGroup
                          group="memory.byProject"
                          title="项目"
                          hint="ci.project（空 → 未归属）"
                          rows={breakdown.memory.byProject}
                        />
                        <BreakdownGroup
                          group="memory.byChannel"
                          title="通道"
                          hint="inject / prompt / tool_after"
                          rows={breakdown.memory.byChannel}
                        />
                        <div data-breakdown-group="memory.openLoop" className="mt-2 min-w-0">
                          <div className="flex flex-wrap items-baseline gap-2">
                            <p className="font-name text-meta text-n7">开放词表</p>
                            <span className="min-w-0 truncate text-meta text-n6">
                              标题命中未闭合词（下一步/待办/待确认…）
                            </span>
                          </div>
                          <p className="mt-1 flex min-w-0 items-baseline gap-2">
                            <span className="w-14 shrink-0 text-right font-mono text-meta tabular-nums text-n7">
                              {num(breakdown.memory.openLoop.pushes)}
                            </span>
                            <span className="w-12 shrink-0 text-right font-mono text-meta tabular-nums text-n6">
                              {percent(breakdown.memory.openLoop.share)}
                            </span>
                            <span className="min-w-0 flex-1 truncate text-meta text-n6">
                              来自 {num(breakdown.memory.openLoop.codes)} 个编码 · 占记忆推送
                            </span>
                          </p>
                        </div>
                      </div>

                      <div className="min-w-0 rounded border border-n3 bg-n0 px-3 py-2">
                        <p className="mb-1 flex flex-wrap items-baseline gap-2">
                          <span className="font-name text-sub text-n7">知识库</span>
                          <span className="text-meta text-n6">
                            {num(breakdown.kb.total)} 次 · 目录 / 通道 / top 文件
                          </span>
                        </p>
                        <BreakdownGroup
                          group="kb.byFolder"
                          title="目录"
                          hint="vault 内顶层目录（无斜杠 = 根）"
                          rows={breakdown.kb.byFolder}
                        />
                        <BreakdownGroup
                          group="kb.byChannel"
                          title="通道"
                          hint="inject / prompt / tool_after"
                          rows={breakdown.kb.byChannel}
                        />
                        <div data-breakdown-group="kb.topFiles" className="mt-2 min-w-0">
                          <div className="flex flex-wrap items-baseline gap-2">
                            <p className="font-name text-meta text-n7">Top 文件</p>
                            <span className="text-meta text-n6">按次数前 5</span>
                          </div>
                          {breakdown.kb.topFiles.length === 0 ? (
                            <p className="py-0.5 text-meta text-n6">本窗口没有可拆的推送</p>
                          ) : (
                            <ul className="mt-0.5">
                              {breakdown.kb.topFiles.map((item) => (
                                <TopFileRow key={item.code} item={item} />
                              ))}
                            </ul>
                          )}
                        </div>
                      </div>

                      <div
                        data-breakdown-group="corpus"
                        className="min-w-0 rounded border border-n3 bg-n0 px-3 py-2"
                      >
                        <p className="flex flex-wrap items-baseline gap-2">
                          <span className="font-name text-sub text-n7">语料</span>
                          <span className="text-meta text-n6">
                            {breakdown.corpus.enabled ? '现场开着' : '现场关闭'}
                          </span>
                        </p>
                        {/* 「关闭」是状态，「窗口内 18 次」是关闭前的历史读数——两件事都写出来 */}
                        <p className="mt-0.5 break-words text-meta leading-4 text-n6">
                          {breakdown.corpus.note}
                        </p>
                        <p className="mt-0.5 font-mono text-meta tabular-nums text-n6">
                          窗口内 {num(breakdown.corpus.total)} 次语料推送
                        </p>
                        {/* 这三行在「记忆」那个数里已经有了（`conv:` 落 memory 的「未分类」），
                            写清楚是为了不让读者把语料再加一遍（QA 🟡：相加不等于总数）。 */}
                        <p className="mt-0.5 break-words text-meta leading-4 text-n6">
                          这 {num(breakdown.corpus.total)} 条已计入上面「记忆」的未分类，不与记忆/知识库重复相加
                        </p>
                      </div>
                    </div>

                    {/* 右：态势源 */}
                    <div data-breakdown-side="situation" className="min-w-0 space-y-3">
                      <p className="flex flex-wrap items-baseline gap-2">
                        <span className="font-name text-sub text-n7">态势源</span>
                        <span className="text-meta text-n6">
                          看板 {num(breakdown.board.total)} · 动作流 {num(breakdown.activity.total)} ·
                          git {num(breakdown.git.total)}
                        </span>
                      </p>

                      <div className="min-w-0 rounded border border-n3 bg-n0 px-3 py-2">
                        <p className="mb-1 flex flex-wrap items-baseline gap-2">
                          <span className="font-name text-sub text-n7">看板</span>
                          <span className="text-meta text-n6">
                            {num(breakdown.board.total)} 次 · 项目 / 看板
                          </span>
                        </p>
                        <BreakdownGroup
                          group="board.byProject"
                          title="项目"
                          hint="推送行自己的项目（空 → 未归属）；本库看板行全为空，区分度在「看板」那组"
                          rows={breakdown.board.byProject}
                        />
                        <BreakdownGroup
                          group="board.byLabel"
                          title="看板"
                          hint="去 board: 前缀（空 → 根看板）"
                          rows={breakdown.board.byLabel}
                        />
                      </div>

                      <div className="min-w-0 rounded border border-n3 bg-n0 px-3 py-2">
                        <p className="mb-1 flex flex-wrap items-baseline gap-2">
                          <span className="font-name text-sub text-n7">动作流</span>
                          <span className="text-meta text-n6">
                            {num(breakdown.activity.total)} 次 · 按目录
                          </span>
                        </p>
                        <BreakdownGroup
                          group="activity.byDir"
                          title="目录"
                          hint="去 activity: 前缀"
                          rows={breakdown.activity.byDir}
                        />
                      </div>

                      <div className="min-w-0 rounded border border-n3 bg-n0 px-3 py-2">
                        <p className="mb-1 flex flex-wrap items-baseline gap-2">
                          <span className="font-name text-sub text-n7">git</span>
                          <span className="text-meta text-n6">
                            {num(breakdown.git.total)} 次 · 按仓库
                          </span>
                        </p>
                        <BreakdownGroup
                          group="git.byRepo"
                          title="仓库"
                          hint="仓库名取 git_repo（缺就退回编码）"
                          rows={breakdown.git.byRepo}
                        />
                      </div>
                    </div>
                  </div>
                ) : (
                  <p className="text-sub text-n6">
                    该区数据未接：缺 breakdown（读口是旧版，或这次读回的载荷里没有这一块）。
                  </p>
                )}
              </section>

              {/* ④ Top offenders */}
              <section data-push-block="top" aria-label="Top offenders">
                <SectionTitle
                  title="Top offenders"
                  hint={`近 ${overview?.window.days ?? days} 天按次数降序（最多 12 条）`}
                />
                {(overview?.topCodes.length ?? 0) === 0 ? (
                  <p className="text-sub text-n6">这个时间窗里没有推送记录。</p>
                ) : (
                  <ol>
                    {(overview?.topCodes ?? []).map((item, index) => (
                      <TopRow key={item.code} item={item} index={index} />
                    ))}
                  </ol>
                )}
              </section>

              {/* ⑤ 调整：只开放白名单 7 项 + 两个预设 */}
              <section data-push-block="adjust" aria-label="调整">
                <SectionTitle
                  title="调整"
                  hint="7 个可写项（白名单 + 类型/范围校验 + 审计）；保存前二次确认，成功会重新读一遍"
                />
                {writeError ? (
                  <p className="mb-2 break-words rounded border border-err/40 bg-n0 px-3 py-2 text-meta text-err">
                    写入失败：{writeError}
                  </p>
                ) : null}
                {notice ? (
                  <p className="mb-2 break-words rounded border border-ok/40 bg-n0 px-3 py-2 text-meta text-ok">
                    {notice}
                  </p>
                ) : null}
                <ul>
                  {writable.map((item) => (
                    <ConfigRow
                      key={item.key}
                      item={item}
                      draft={drafts[item.key] ?? draftOf(item)}
                      busy={busyKey === item.key}
                      onDraft={(value) =>
                        setDrafts((current) => ({ ...current, [item.key]: value }))
                      }
                      onSave={() => void saveKey(item)}
                    />
                  ))}
                </ul>

                <div className="mt-3 rounded border border-n3 bg-n0 px-3 py-2">
                  <p className="text-sub text-n7">只读项（不在本页写）</p>
                  <ul className="mt-1 space-y-1">
                    {readonly.map((item) => (
                      <li key={item.key} data-push-readonly={item.key} className="text-meta">
                        <span className="text-n7">{item.label}</span>
                        <span className="ml-2 font-mono text-n6">{item.key}</span>
                        <span className="ml-2 font-mono text-n7">{String(item.value)}</span>
                      </li>
                    ))}
                  </ul>
                  {enabled ? (
                    <p className="mt-2 rounded border border-err/40 bg-n1 px-2 py-1.5 text-meta text-err">
                      ⚠ push.enabled 现在是 {String(enabled.value)}：在 DSH 上设 false 会连
                      tool_activity 采集一起停（fish_push_hook.py:138），所以本页不提供一键关。
                      想让它安静下来，用下面的「止血预设」。
                    </p>
                  ) : null}
                </div>

                <div className="mt-3 flex flex-wrap items-center gap-2">
                  {(Object.keys(PRESET_TEXT) as PushPreset[]).map((preset) => (
                    <button
                      key={preset}
                      type="button"
                      data-push-preset={preset}
                      disabled={busyPreset !== null}
                      onClick={() => void applyPreset(preset)}
                      className="rounded border border-n4 bg-n0 px-3 py-1.5 text-sub text-n7 hover:bg-n2 active:bg-n3 disabled:text-n5"
                    >
                      {busyPreset === preset ? '写入中…' : PRESET_TEXT[preset].label}
                    </button>
                  ))}
                  <span className="min-w-0 flex-1 text-meta text-n6">
                    止血 = {PRESET_TEXT.pause.detail}；恢复 = {PRESET_TEXT.resume.detail}
                  </span>
                </div>
              </section>

              {/* ⑥ 最近推送 */}
              <section data-push-block="events" aria-label="最近推送">
                <div className="mb-2 flex flex-wrap items-baseline gap-2">
                  <h3 className="text-sub font-name text-n7">最近推送</h3>
                  <span className="text-meta text-n6">
                    按写入顺序倒序，最多 20 条；reaction 空 = 待判（不是拒绝）
                  </span>
                  <button
                    type="button"
                    data-push-action="refresh-events"
                    disabled={eventsState === 'loading'}
                    onClick={() => void loadEvents()}
                    className="ml-auto rounded border border-n4 bg-n0 px-2 py-0.5 text-meta text-n7 hover:bg-n2 active:bg-n3 disabled:text-n5"
                  >
                    {eventsState === 'loading' ? '读…' : '刷新'}
                  </button>
                </div>
                <Block
                  state={eventsState}
                  cap="push.events"
                  errorText={eventsError}
                  onRetry={() => void loadEvents()}
                >
                  {events.length === 0 ? (
                    <p className="text-sub text-n6">还没有任何推送事件（这个库还没推过东西）。</p>
                  ) : (
                    <ul>
                      {events.map((item) => (
                        <EventRow key={item.id} item={item} />
                      ))}
                    </ul>
                  )}
                </Block>
              </section>

              {/* ⑦ 口径 */}
              <section data-push-block="notes" aria-label="口径">
                <SectionTitle title="口径" hint="这几个数各自回答什么、不回答什么" />
                <ul className="space-y-1 text-meta leading-5 text-n6">
                  <li>
                    采纳率 = accepted /（accepted + ignored）。待判不算分母（结果窗口没到就
                    拉低采纳率是错的），待判也不等于拒绝。
                  </li>
                  <li>
                    盲区消除率 = 推送后 7 天内真的读了正文的次数 / 推送总数：它回答「推完有没有
                    真的被看」，不是「被采纳」。
                  </li>
                  <li>
                    违规率 = 工具后推送里，5 分钟内该会话毫无动静的比例。成本侧单列：它不参与
                    收益，也不与采纳率合成总分。
                  </li>
                  <li>
                    未知项目 = session_context 里没有项目归属的会话。跨项目的推送算不出「该不该
                    推」，所以它单列成诊断，不混进采纳率。
                  </li>
                  <li>
                    重复推送 = 同会话 + 同编码 + 同一天（UTC 日期）出现 2 次以上的组：一个编码在
                    一天内被推几百次，说明去重闸没兜住。
                  </li>
                </ul>
              </section>
            </div>
          ) : null}
        </Block>
      </div>
    </div>
  )
}
