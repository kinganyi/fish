import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { describeError } from '../../api/client'
import { fetchHost, fetchHosts, refreshHost } from '../../api/endpoints'
import type {
  FishPresence,
  HostCapState,
  HostConfigInfo,
  HostStatus,
  HostsData,
} from '../../api/types'
import { Block } from '../../components/AsyncBoundary'
import { codes } from '../../codes'
import { UNKNOWN, num, relativeOf, stampLongOf } from '../../format'
import type { LoadState } from '../../store/workbench'

/**
 * 设置页 —— **宿主接入层的唯一界面**（一期：只读探测）。
 *
 * 这一屏回答的是「这套东西装没装、能不能用」，时间尺度是天到月；它和现场那屏
 * （「此刻谁在动哪块」，秒到分钟）是**两个面**，用同一份真相 `GET /api/hosts`。
 *
 * 三条不能省的显示纪律（都是踩过的坑）：
 *  1. **协议与配置来源分开显示**：`protocol` 是 `session_key` 前缀（机器证据），
 *     `configPath` 是「fish 被写在哪份配置里」。二者不是一回事，二选一假装就等于撒谎。
 *  2. **三态分明**：已接 / 未接 / 未验证。`unverified` 不能渲染成「未接」——
 *     那是把「判不到」报成「红」（WorkBuddy 的 `--check` 假红就是这么来的）。
 *  3. **空心点也是宿主**：装了但没接 fish 的（本机的 CodeBuddy）要出现，否则用户
 *     永远看不到「这台机器上还有谁没接」。
 *
 * 动作面只有 `刷新`（POST 重探一次，不写盘）；`启用 hooks` / `启用 MCP` 是**禁用按钮**，
 * 原因由后端给（`blockedReasons`），前端不另写一套。
 */
const CAP_ORDER = ['hooks', 'mcp', 'events', 'identity', 'push'] as const
const CAP_NAME: Record<string, string> = {
  hooks: 'hooks',
  mcp: 'MCP',
  events: '事件流',
  identity: '身份',
  push: '推送',
}
const ACTION_NAME: Record<string, string> = {
  'toggle-hooks': '启用 hooks',
  'toggle-mcp': '启用 MCP',
}

function presenceLine(presence: FishPresence): string {
  if (presence === 'both') return 'hooks+MCP'
  if (presence === 'hooks') return 'hooks'
  if (presence === 'mcp') return '仅 MCP'
  return '未接入'
}

function capTone(state: HostCapState): string {
  if (state === 'yes') return 'text-ok'
  if (state === 'no') return 'text-n5'
  return 'text-warn'
}

function evidenceMark(ok: boolean | null): { text: string; tone: string } {
  if (ok === true) return { text: '通过', tone: 'text-ok' }
  if (ok === false) return { text: '未通过', tone: 'text-err' }
  return { text: '判不到', tone: 'text-warn' }
}

function stalledLine(host: HostStatus): string {
  if (host.stalled === null) return host.stalledNote ?? UNKNOWN
  return host.stalled ? `已停摆（${host.stalledNote ?? '超过 24 小时没动'}）` : '未停摆'
}

/** 左栏一行宿主：状态点（实心=有 fish 接入，空心=只探测到）+ 显示名 + 一行状态。 */
function HostRow({
  host,
  active,
  onPick,
}: {
  host: HostStatus
  active: boolean
  onPick: () => void
}) {
  const connected = host.fishPresence !== 'none'
  return (
    <li>
      <button
        type="button"
        aria-pressed={active}
        data-host={host.id}
        onClick={onPick}
        title={`${host.display} · ${presenceLine(host.fishPresence)}${host.configPath ? ` · ${host.configPath}` : ''}`}
        className={`flex w-full items-center gap-2 border-l-2 px-3 py-2 text-left active:bg-n3 ${
          active
            ? 'border-l-accent bg-accent-soft'
            : 'border-l-transparent hover:bg-n2'
        }`}
      >
        <span
          aria-hidden="true"
          className={`h-2 w-2 shrink-0 rounded-full ${
            connected ? 'bg-ok' : 'border border-n4 bg-n0'
          }`}
        />
        <span className="min-w-0 flex-1">
          <span className={`block truncate text-sub ${active ? 'font-name text-accent' : 'text-n7'}`}>
            {host.display}
          </span>
          <span className="block truncate font-mono text-meta text-n6">
            {presenceLine(host.fishPresence)}
            {host.installed ? '' : ' · 未安装'}
          </span>
        </span>
      </button>
    </li>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[96px_minmax(0,1fr)] items-baseline gap-2 border-b border-n3/60 py-1.5">
      <span className="text-meta text-n6">{label}</span>
      <div className="min-w-0 text-sub text-n7">{children}</div>
    </div>
  )
}

function ConfigLine({ config }: { config: HostConfigInfo }) {
  return (
    <li className="py-0.5">
      <span className="text-meta text-n6">
        {config.role === 'hooks' ? 'hooks 配置' : 'MCP 配置'} · {config.label} ·{' '}
        {config.exists ? (config.fishHit ? 'fish 命中' : '无 fish') : '不存在'}
      </span>
      <span className="block truncate font-mono text-meta text-n7" title={config.path}>
        {config.path}
      </span>
      {config.detail ? (
        <span className="block truncate text-meta text-n6" title={config.detail}>
          {config.detail}
        </span>
      ) : null}
    </li>
  )
}

function CapabilityChip({ name, cap }: { name: string; cap: HostStatus['capabilities'][string] }) {
  return (
    <span
      title={cap.evidence}
      className={`rounded border border-n3 bg-n1 px-2 py-1 text-meta ${capTone(cap.state)}`}
    >
      {CAP_NAME[name] ?? name} · {cap.label}
    </span>
  )
}

function Detail({
  host,
  state,
  error,
  busy,
  onRefresh,
}: {
  host: HostStatus | null
  state: LoadState
  error: string | null
  busy: boolean
  onRefresh: () => void
}) {
  if (state === 'loading' || state === 'idle') {
    return (
      <div className="animate-pulse space-y-2 p-4" aria-busy="true" aria-label={codes.loading}>
        <div className="h-3 w-40 rounded bg-n2" />
        <div className="h-3 w-64 rounded bg-n2" />
        <div className="h-3 w-56 rounded bg-n2" />
      </div>
    )
  }
  if (state === 'error' || !host) {
    return (
      <div className="space-y-1 p-4 text-sub text-n6">
        <p className="text-n7">这一块没读出来。</p>
        {error ? <p className="break-words font-mono text-meta">{error}</p> : null}
        <button
          type="button"
          onClick={onRefresh}
          className="mt-1 rounded border border-n4 bg-n0 px-2 py-1 text-meta text-n7 hover:bg-n2 active:bg-n3"
        >
          {codes.retry}
        </button>
      </div>
    )
  }

  return (
    <div className="max-w-[860px]">
      <div className="flex flex-wrap items-baseline gap-2">
        <h2 className="text-section font-head text-n7">{host.display}</h2>
        <span className="font-mono text-meta text-n6">{host.id}</span>
        <span
          className={`rounded px-1.5 py-0.5 text-meta ${
            host.fishPresence === 'none' ? 'bg-n2 text-n6' : 'bg-accent-soft text-accent'
          }`}
        >
          {presenceLine(host.fishPresence)}
        </span>
        {host.installed ? null : <span className="text-meta text-warn">未安装</span>}
      </div>

      <Field label="安装">
        {host.installed ? '已探测到安装痕迹' : '没有探测到安装痕迹'}
        {host.home ? (
          <span className="ml-2 truncate font-mono text-meta text-n6" title={host.home}>
            {host.home}
          </span>
        ) : null}
      </Field>

      <Field label="配置来源">
        {host.configs.length === 0 ? (
          <span className="text-n6">无配置文件（不是安装目标）</span>
        ) : (
          <ul>
            {host.configs.map((config) => (
              <ConfigLine key={`${config.role}:${config.label}`} config={config} />
            ))}
          </ul>
        )}
        {host.configPath === null && host.configReason ? (
          <span className="block text-meta text-warn">{host.configReason}</span>
        ) : null}
      </Field>

      <Field label="版本">
        <span className="font-mono">{host.version ?? UNKNOWN}</span>
        {host.versionSource ? (
          <span className="ml-2 truncate font-mono text-meta text-n6" title={host.versionSource}>
            {host.versionSource}
          </span>
        ) : host.versionNote ? (
          <span className="ml-2 text-meta text-n6">{host.versionNote}</span>
        ) : null}
        {/*
          Electron 版本**不是**宿主版本（2026-11-24 修的真错：它曾被当成 DSH 44.7.0 显示）。
          单独一行、明确标注，既保留「我们知道这个数」的信息，又不让它冒充应用版本。
        */}
        {host.electronVersion ? (
          <span
            className="block truncate font-mono text-meta text-n6"
            title={host.electronSource ?? undefined}
          >
            Electron {host.electronVersion}（非应用版本）
          </span>
        ) : null}
      </Field>

      <Field label="协议">
        <span className="font-mono text-n7">{host.protocol ? `${host.protocol}:` : '未登记'}</span>
        <span className="ml-2 text-meta text-n6">
          {host.protocol ? '会话键前缀' : '这台宿主不产生 fish 会话键'}
        </span>
        {host.protocolNote ? (
          <span className="block text-meta text-warn">{host.protocolNote}</span>
        ) : null}
      </Field>

      <Field label="最后活跃">
        {host.lastSeenAt ? (
          <>
            <span className="font-mono">{host.lastSeenAt}</span>
            <span className="ml-2 text-meta text-n6">{relativeOf(host.lastSeenAt)}</span>
            <span className={`ml-2 text-meta ${host.stalled ? 'text-warn' : 'text-n6'}`}>
              {stalledLine(host)}
            </span>
            {host.lastSeenSources.length > 0 ? (
              <span className="block truncate text-meta text-n6">
                {host.lastSeenSources.map((source) => source.source).join(' · ')}
              </span>
            ) : null}
          </>
        ) : (
          <span className="text-n6">{stalledLine(host)}</span>
        )}
      </Field>

      <Field label="能力">
        <div className="flex flex-wrap gap-2">
          {CAP_ORDER.map((name) => {
            const cap = host.capabilities[name]
            if (!cap) return null
            return <CapabilityChip key={name} name={name} cap={cap} />
          })}
        </div>
      </Field>

      <Field label="接入证据">
        <ul className="space-y-1">
          {host.evidence.map((item) => {
            const mark = evidenceMark(item.ok)
            return (
              <li key={item.id} className="flex items-baseline gap-2">
                <span className={`w-10 shrink-0 text-meta ${mark.tone}`}>{mark.text}</span>
                <span className="min-w-0 flex-1">
                  <span className="block text-sub text-n7">{item.label}</span>
                  <span
                    className="block truncate font-mono text-meta text-n6"
                    title={item.detail}
                  >
                    {item.detail}
                  </span>
                </span>
              </li>
            )
          })}
        </ul>
      </Field>

      <div className="mt-3 flex flex-wrap items-center gap-3 border-b border-n3/60 pb-3">
        <button
          type="button"
          onClick={onRefresh}
          disabled={busy}
          className="rounded border border-n4 bg-n0 px-3 py-1.5 text-sub text-n7 hover:bg-n2 active:bg-n3 disabled:text-n5"
        >
          {busy ? '重探中…' : '刷新'}
        </button>
        {host.blockedActions.map((action) => (
          <span key={action} className="inline-flex items-center gap-2">
            <button
              type="button"
              disabled
              title={`${host.blockedReasons[action] ?? '待二期'}（本页不写任何宿主配置）`}
              className="cursor-not-allowed rounded border border-n3 bg-n1 px-3 py-1.5 text-sub text-n5"
            >
              {ACTION_NAME[action] ?? action}
            </button>
            <span className="text-meta text-n6">{host.blockedReasons[action] ?? '待二期'}</span>
          </span>
        ))}
      </div>

      {host.notes.length > 0 ? (
        <ul className="mt-2 space-y-1">
          {host.notes.map((note) => (
            <li key={note} className="text-meta leading-5 text-n6">
              {note}
            </li>
          ))}
        </ul>
      ) : null}

      <p className="mt-3 text-meta text-n6">
        机制：
        <span className="ml-1 font-mono">
          宿主支持 hooks {host.hooksSupported ? '是' : '否'} · 事件 {num(host.events.length)} 个 ·
          matcher {host.matcherDialect?.kind ?? '无'} · 采集器 {num(host.collector.length)} 个
        </span>
      </p>
    </div>
  )
}

export function Settings() {
  const [hosts, setHosts] = useState<HostStatus[]>([])
  const [generatedAt, setGeneratedAt] = useState<string | null>(null)
  const [consistency, setConsistency] = useState<HostsData['consistency'] | null>(null)
  const [listState, setListState] = useState<LoadState>('idle')
  const [listError, setListError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<HostStatus | null>(null)
  const [detailState, setDetailState] = useState<LoadState>('idle')
  const [detailError, setDetailError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    setListState('loading')
    setListError(null)
    try {
      const envelope = await fetchHosts()
      const data = envelope.data
      setHosts(data.items)
      setGeneratedAt(data.generatedAt)
      setConsistency(data.consistency)
      setListState('ok')
      setSelectedId((current) =>
        current && data.items.some((item) => item.id === current)
          ? current
          : data.items[0]?.id ?? null,
      )
    } catch (error) {
      setListState('error')
      setListError(describeError(error))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    if (!selectedId) {
      setDetail(null)
      setDetailState('idle')
      return
    }
    let alive = true
    setDetailState('loading')
    setDetailError(null)
    fetchHost(selectedId)
      .then((envelope) => {
        if (!alive) return
        setDetail(envelope.data)
        setDetailState('ok')
      })
      .catch((error: unknown) => {
        if (!alive) return
        setDetailState('error')
        setDetailError(describeError(error))
      })
    return () => {
      alive = false
    }
  }, [selectedId])

  const refresh = useCallback(async () => {
    if (!selectedId) return
    setBusy(true)
    setDetailError(null)
    try {
      const envelope = await refreshHost(selectedId)
      setDetail(envelope.data)
      setDetailState('ok')
      setGeneratedAt(envelope.asOf)
      setHosts((list) => list.map((item) => (item.id === selectedId ? envelope.data : item)))
    } catch (error) {
      setDetailState('error')
      setDetailError(describeError(error))
    } finally {
      setBusy(false)
    }
  }, [selectedId])

  const warnings = useMemo(() => consistency?.warnings ?? [], [consistency])

  return (
    <main className="flex min-h-0 flex-col overflow-hidden bg-n1" aria-label="设置">
      <header className="flex flex-wrap items-baseline justify-between gap-4 border-b border-n3 bg-n0 px-4 py-3">
        <div>
          <h1 className="text-section font-head text-n7">设置</h1>
          <p className="mt-0.5 text-meta text-n6">宿主接入：装没装、接没接、接的哪条通道</p>
        </div>
        <p className="font-mono text-meta text-n6" title="探测时刻（每次现探，不落库）">
          探测于 {generatedAt ? stampLongOf(generatedAt) : UNKNOWN}
        </p>
      </header>

      {warnings.length > 0 ? (
        <p className="border-b border-n3 bg-n1 px-4 py-2 text-meta text-warn">
          词表一致性提示（不阻断）：{warnings.join('；')}
        </p>
      ) : null}

      <div className="grid min-h-0 flex-1" style={{ gridTemplateColumns: '260px minmax(0, 1fr)' }}>
        <aside
          className="min-h-0 overflow-y-auto border-r border-n3 bg-n1 px-1 py-2"
          aria-label="宿主列表"
        >
          <Block
            state={listState}
            cap="hosts"
            errorText={listError}
            onRetry={() => void load()}
            emptyText="没有探测到已安装的宿主"
          >
            <ul className="space-y-0.5">
              {hosts.map((host) => (
                <HostRow
                  key={host.id}
                  host={host}
                  active={host.id === selectedId}
                  onPick={() => setSelectedId(host.id)}
                />
              ))}
            </ul>
          </Block>
        </aside>

        <section className="min-h-0 overflow-y-auto bg-n0 px-6 py-4" aria-label="宿主详情">
          <Detail
            host={detail}
            state={detailState}
            error={detailError}
            busy={busy}
            onRefresh={() => void refresh()}
          />
        </section>
      </div>
    </main>
  )
}
