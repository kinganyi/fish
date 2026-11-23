import type { ReactNode } from 'react'
import type { SourceStat } from '../../api/types'
import { Missing, StateTag } from '../../components/AsyncBoundary'
import { OriginTip } from '../../components/OriginTip'
import { ValueCell } from '../../components/ValueCell'
import { codes } from '../../codes'
import { num, percent, relativeOf, stampOf } from '../../format'
import { useWorkbench } from '../../store/workbench'
import { WIN_LABEL, STATS_WIN_LABEL } from '../../windows'

function Section({
  title,
  cap,
  detail,
  sources,
  children,
  state,
}: {
  title: string
  cap: string
  detail: string
  sources: SourceStat[]
  state: 'ok' | 'error' | 'loading' | 'idle'
  children: ReactNode
}) {
  return (
    <section className="border-b border-n3/60 px-4 py-3">
      <div className="mb-1 flex items-center justify-between gap-2">
        <h3 className="text-sub font-head text-n7">{title}</h3>
        <div className="flex items-center gap-1">
          <StateTag state={state} />
          <OriginTip sources={sources} cap={cap} detail={detail} />
        </div>
      </div>
      {children}
    </section>
  )
}

function Row({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-2 py-0.5 text-sub">
      <span className="min-w-0 truncate text-n6">{label}</span>
      <span className="shrink-0 tabular-nums text-n7">{value}</span>
    </div>
  )
}

function Bar({ name, count, max }: { name: string; count: number; max: number }) {
  const width = max > 0 ? Math.max(2, Math.round((count / max) * 100)) : 0
  return (
    <div className="py-0.5">
      <div className="flex items-baseline justify-between text-meta">
        <span className="font-mono text-n7">{name || '（未登记）'}</span>
        <span className="tabular-nums text-n6">{num(count)}</span>
      </div>
      <div className="mt-0.5 h-1 w-full rounded bg-n2">
        <div className="h-1 rounded bg-accent" style={{ width: `${width}%` }} />
      </div>
    </div>
  )
}

/**
 * 右栏页签一：先回答「现在需要我做什么」，再给读数（97 §A4）。
 *
 * 前三块是**真的需要人**的三件事：开了没关的档期、说了在做但没动手的会话、
 * 推了没人看的内容。读数（在跑/公民/工具/热推/热搜）排在后面——在跑 5 个会话
 * 不构成任何待办，而"308 个档期最老 29 天没关"构成待办。
 *
 * 每块标题右侧都有「来源 + 口径」（A-12），且口径里**必须带时间窗**（R-09）：
 * 只写表名不写窗口，读者无法判断这个数字覆盖哪段时间。窗口名从 `windows.ts` 取
 * （`W` = 跟随流水窗口的那三块；`H`/`M` = 后端按固定窗口算的那几块，取同一张表与
 * 载荷里的 `windowMinutes`，不写裸字符串）。
 *
 * 「缺」与「错」在这一屏分开（R-08）：`missing` 说"缺 /api/stats（不是读口没起）"、
 * 不给重试；`error` 才给"确认读口在跑，然后重试"。
 */
export function TabNeedYou() {
  const stats = useWorkbench((state) => state.stats)
  const statsState = useWorkbench((state) => state.statsState)
  const statsAvailability = useWorkbench((state) => state.statsAvailability)
  const sources = useWorkbench((state) => state.statsSources)
  const statsError = useWorkbench((state) => state.lastError)
  const refreshStats = useWorkbench((state) => state.refreshStats)
  const win = useWorkbench((state) => state.win)

  if (statsAvailability === 'missing') {
    return <Missing cap="/api/stats" detail={statsError} />
  }
  if (statsState === 'error') {
    return (
      <div className="p-4 text-sub text-n6">
        <p className="text-n7">判断面没读出来。</p>
        {statsError ? <p className="mt-1 break-words font-mono text-meta">{statsError}</p> : null}
        <p className="mt-1">能做的：确认读口在跑，然后重试。</p>
        <button
          type="button"
          onClick={() => void refreshStats()}
          className="mt-2 rounded border border-n4 bg-n0 px-2 py-1 text-meta text-n7 hover:bg-n2 active:bg-n3"
        >
          {codes.retry}
        </button>
      </div>
    )
  }
  if (statsState !== 'ok' || !stats) {
    return (
      <div className="space-y-2 p-4" aria-busy="true" aria-label={codes.loading}>
        <div className="h-3 w-2/3 animate-pulse rounded bg-n2" />
        <div className="h-3 w-1/2 animate-pulse rounded bg-n2" />
        <div className="h-3 w-3/4 animate-pulse rounded bg-n2" />
      </div>
    )
  }

  const d = stats
  const pick = (names: string[]) =>
    sources.filter((source) => names.some((name) => source.source.includes(name)))
  const toolsMax = Math.max(0, ...d.tools.in24hByName.map((item) => item.count))
  const state = statsState === 'ok' ? 'ok' : statsState

  // 时间窗文案：`W` 跟随流水窗口（推送/热推/热搜三块的后端窗口就是 days），
  // `H` 是后端固定按 24 小时算的两块，`M` 是后端给的「在跑」窗口（30 分钟，取载荷不写死）。
  const W = STATS_WIN_LABEL[win]
  const H = WIN_LABEL['24h']
  const M = `近 ${d.motion.windowMinutes} 分钟`

  return (
    <div>
      <Section
        title="要你收尾的"
        cap="/api/stats · openStints"
        detail={`全期 · project_stints 里 to_ts 为空的行；天数按 from_ts 起算`}
        state={state}
        sources={pick(['project_stints'])}
      >
        {d.openStints.total === 0 ? (
          <p className="text-sub text-n6">没有开了没关的档期。</p>
        ) : (
          <>
            <p className="text-sub text-n7">
              <ValueCell value={d.openStints.total} suffix="个档期开了没关" />
              <span className="ml-2 text-meta text-n6">
                最老 {d.openStints.oldestDays === null ? '判不到' : `${d.openStints.oldestDays} 天`}
              </span>
            </p>
            <div className="mt-1">
              {d.openStints.projects.slice(0, 3).map((project) => (
                <Row
                  key={project.name || '(未命名)'}
                  label={project.name || '（空项目名）'}
                  value={`${num(project.count)} 个 · ${
                    project.oldestDays === null ? '判不到' : `${project.oldestDays} 天`
                  }`}
                />
              ))}
            </div>
          </>
        )}
      </Section>

      <Section
        title="要你确认的"
        cap="/api/stats · idleDeclared"
        detail="全表 · presence.doing：是否含占位模板 + auto_source 是否为空（两者联合判定）"
        state={state}
        sources={pick(['presence'])}
      >
        <Row label="系统代登记（未说明在做什么）" value={<ValueCell value={d.idleDeclared.auto} />} />
        <Row label="本人声明了在做什么" value={<ValueCell value={d.idleDeclared.declared} />} />
        {d.idleDeclared.unclear > 0 ? (
          <Row label="判定不了的声明" value={<ValueCell value={d.idleDeclared.unclear} />} />
        ) : null}
        {d.idleDeclared.items.length ? (
          <ul className="mt-1 space-y-1">
            {d.idleDeclared.items.slice(0, 3).map((item) => (
              <li key={`${item.sessionTag}-${item.lastSeenAt}`} className="text-meta leading-5 text-n6">
                <span className="font-name text-n7">{item.label}</span>
                <span className="ml-1 font-mono">{item.sessionTag}</span>
                <span className="ml-1">：{item.doing || '（空）'}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-1 text-meta text-n6">所有在跑的会话都说清了在做什么。</p>
        )}
      </Section>

      <Section
        title="要你决定的"
        cap="/api/stats · push"
        detail={`${W} · push_stats(acceptRate)：分母 = accepted + ignored（已判），待回填单列`}
        state={state}
        sources={pick(['push_log'])}
      >
        {d.push.total === null ? (
          <p className="text-sub text-n6">推送读数判不到（后端没给读数，不是"没有推送"）。</p>
        ) : d.push.total === 0 ? (
          <p className="text-sub text-n6">这一窗里还没推过内容。</p>
        ) : (
          <>
            <Row label="推送" value={<ValueCell value={d.push.total} suffix="次" />} />
            <Row label="已判（accepted + ignored）" value={<ValueCell value={d.push.decided} />} />
            <Row label="待回填（pending）" value={<ValueCell value={d.push.pending} />} />
            <Row
              label="采纳率"
              value={
                d.push.acceptRate === null ? (
                  <span className="text-n6" title="分母为 0，判不到，不是 0%">
                    判不到（今日推送都待回填）
                  </span>
                ) : (
                  percent(d.push.acceptRate)
                )
              }
            />
            <p className="mt-1 text-meta text-n6">{d.push.rateDenominator}</p>
          </>
        )}
      </Section>

      <Section
        title="在跑 / 最近来过"
        cap="/api/stats · motion"
        detail={`${M} / ${H} · 在跑 = 窗口内有工具动作的去重会话；最近来过 = 有过心跳或动作的会话`}
        state={state}
        sources={pick(['tool_activity', 'presence'])}
      >
        {d.motion.inMotion === 0 ? (
          <p className="text-sub text-n6">
            现在没有会话在动（最近一次动作：{stampOf(d.motion.lastActionAt)}）。
          </p>
        ) : (
          <Row
            label={`在跑（${M}）`}
            value={<ValueCell value={d.motion.inMotion} suffix={d.motion.quantifier} />}
          />
        )}
        <Row
          label={`最近来过（${H}内）`}
          value={<ValueCell value={d.motion.seen24h} suffix={d.motion.quantifier} />}
        />
        <p className="mt-1 text-meta text-n6">
          最近一次动作：<span className="font-mono">{stampOf(d.motion.lastActionAt)}</span>
        </p>
      </Section>

      <Section
        title="公民综合"
        cap="/api/stats · citizens"
        detail={`全期 + ${H} · citizens_all 按 kind / host 计数；宿主大小写已归一显示（不改库）`}
        state={state}
        sources={pick(['citizens_all'])}
      >
        {d.citizens.total === 0 ? (
          <p className="text-sub text-n6">还没有公民。</p>
        ) : (
          <>
            <Row label="公民总数" value={<ValueCell value={d.citizens.total} />} />
            <Row label={`${H}内有动作的公民`} value={<ValueCell value={d.citizens.active24h} />} />
            <div className="mt-1 flex flex-wrap gap-1">
              {d.citizens.byKind.map((item) => (
                <span key={item.kind} className="rounded bg-n2 px-1.5 py-0.5 text-meta text-n7">
                  {item.kind || '（空）'} <span className="font-mono">{num(item.count)}</span>
                </span>
              ))}
            </div>
            <div className="mt-1 flex flex-wrap gap-1">
              {d.citizens.byHost.map((item) => (
                <span
                  key={`host-${item.host ?? 'null'}`}
                  className="rounded border border-n3 px-1.5 py-0.5 font-mono text-meta text-n6"
                >
                  {item.host ?? '（空）'} {num(item.count)}
                </span>
              ))}
            </div>
          </>
        )}
      </Section>

      <Section
        title="工具使用"
        cap="/api/stats · tools"
        detail={`${H} + 累计全期 · tool_activity 按 tool_name 计数；两档分开两个读数，不混一格`}
        state={state}
        sources={pick(['tool_activity'])}
      >
        <Row label={`${H}合计`} value={<ValueCell value={d.tools.in24h} suffix="条" />} />
        <div className="mt-1">
          {d.tools.in24hByName.map((item) => (
            <Bar key={item.name} name={item.name} count={item.count} max={toolsMax} />
          ))}
        </div>
        {/* 「今日 / 累计」是两档不同口径，用标签分行表达，不写成一句话解释。 */}
        <p className="mt-2 flex items-baseline gap-1 text-meta text-n6">
          <span className="rounded bg-n2 px-1 text-n6">累计</span>
          <span className="font-mono text-n7">{num(d.tools.cumulative)}</span>
          <span>条</span>
        </p>
      </Section>

      <Section
        title="热推内容"
        cap="/api/stats · push.top"
        detail={`${W} · 按 code 取前几（次数降序、同次数按最近推送降序）；率的分母与 acceptRate 同一口径`}
        state={state}
        sources={pick(['push_log'])}
      >
        {d.push.top.length === 0 ? (
          <p className="text-sub text-n6">这一窗里还没有推送记录。</p>
        ) : (
          <ul className="space-y-1">
            {d.push.top.map((item) => (
              <li key={item.code} className="text-meta leading-5">
                <div className="truncate font-mono text-n7" title={item.code}>
                  {item.code || '（无编码）'}
                </div>
                <div className="text-n6">
                  推 {num(item.pushed)} · 采纳 {num(item.accepted)} · 忽略 {num(item.ignored)} · 待判{' '}
                  {num(item.pending)} · 率 {percent(item.rate)}
                </div>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section
        title="热搜内容"
        cap="/api/stats · hotAccess"
        detail={`${W} + 全期 · memory_access_log 按 code 计数；每行带最后取用时间，另有全期一档`}
        state={state}
        sources={pick(['memory_access_log', 'code_index'])}
      >
        {d.hotAccess.window.length === 0 && d.hotAccess.allTime.length === 0 ? (
          <p className="text-sub text-n6">还没有记忆被取用过。</p>
        ) : (
          <>
            <p className="text-meta text-n6">{W}</p>
            <ul className="mb-2 space-y-0.5">
              {(d.hotAccess.window.length ? d.hotAccess.window : []).map((item) => (
                <li key={`w-${item.code}`} className="text-meta leading-5">
                  <span className="font-mono text-n7">{item.code}</span>
                  <span className="ml-1 text-n6">{item.title ?? '编码已不在索引里'}</span>
                  <span className="ml-1 text-n6">
                    · {num(item.count)} 次 · 最后 {relativeOf(item.lastAccessedAt)}
                  </span>
                </li>
              ))}
              {d.hotAccess.window.length === 0 ? (
                <li className="text-meta text-n6">这一窗里没有取用记录。</li>
              ) : null}
            </ul>
            <p className="text-meta text-n6">全期（老记忆常驻榜首，所以两档都给）</p>
            <ul className="space-y-0.5">
              {d.hotAccess.allTime.map((item) => (
                <li key={`a-${item.code}`} className="text-meta leading-5">
                  <span className="font-mono text-n7">{item.code}</span>
                  <span className="ml-1 text-n6">{item.title ?? '编码已不在索引里'}</span>
                  <span className="ml-1 text-n6">
                    · {num(item.count)} 次 · 最后 {relativeOf(item.lastAccessedAt)}
                  </span>
                </li>
              ))}
            </ul>
          </>
        )}
      </Section>
    </div>
  )
}
