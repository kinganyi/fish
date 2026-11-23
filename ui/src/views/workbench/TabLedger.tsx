import type { ReactNode } from 'react'
import type { StreamItem } from '../../api/types'
import { Missing } from '../../components/AsyncBoundary'
import { Time } from '../../components/Time'
import { codes } from '../../codes'
import { num, relativeOf, UNKNOWN } from '../../format'
import { useWorkbench } from '../../store/workbench'

const TYPE_WORD: Record<string, string> = {
  tool: '工具动作',
  post: '看板登记',
  push: '推送',
  memory: '记忆事件',
  system: '系统事件',
  fold: '连续动作',
}

function Line({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-2 py-0.5 text-sub">
      <span className="shrink-0 text-n6">{label}</span>
      <span className="min-w-0 text-right text-n7">{children}</span>
    </div>
  )
}

/**
 * `payload` / `args_digest` 的展示（96 §六：只回键名，不回值）。
 *
 * 这里**不写"只给键名不给值"那句话**：键名清单本身加一个 `payload` 标签，读者一眼
 * 就看出这是字段名清单；用句子解释等于把纪律当文案念给用户听（评审 L1）。
 * `args_digest` 同理：只出现标签，是否摘要由名字自己说明。
 */
function Meta({ item }: { item: StreamItem }) {
  const keys = Array.isArray(item.meta.payloadKeys) ? (item.meta.payloadKeys as string[]) : []
  return (
    <>
      {item.meta.fullText ? (
        <div className="mt-2 whitespace-pre-wrap rounded border border-n3 bg-n1 p-2 text-sub leading-6 text-n7">
          {String(item.meta.fullText)}
        </div>
      ) : null}
      {keys.length ? (
        <p className="mt-1 flex flex-wrap items-baseline gap-1 font-mono text-meta text-n6">
          <span className="text-n5">payload</span>
          {keys.map((key) => (
            <span key={key} className="rounded bg-n2 px-1 text-n6">
              {key}
            </span>
          ))}
        </p>
      ) : null}
      {item.meta.argsDigestPresent ? (
        <p className="mt-1 font-mono text-meta text-n5" title="摘要，不回原值">
          args_digest
        </p>
      ) : null}
    </>
  )
}

/**
 * 右栏页签二：三段账本（97 §A5）。
 *
 * 不是八行属性表——属性表回答"有哪些字段"，账本回答"他干过什么"，后者才是点开的原因。
 * 三条写死的规矩：永不空白（未选中就说"还没选中"）；`payload`/`args_digest`/`token_hash`
 * 只给键名清单不给值；切页签不改选中。
 *
 * 时间列：默认到分钟，悬停展开到秒（`components/Time.tsx`）。原先把
 * `stampOf`(MM-DD HH:mm) 与 `clockOf`(HH:mm:ss) 并排渲染，屏幕上出现
 * `10-25 19:19 19:19:59` 这种同一分钟的重复（QA K3）——现在只有一条。
 */
export function TabLedger() {
  const selectedKey = useWorkbench((state) => state.selectedKey)
  const entry = useWorkbench((state) => state.entry)
  const entryState = useWorkbench((state) => state.entryState)
  const entryAvailability = useWorkbench((state) => state.entryAvailability)
  const entryError = useWorkbench((state) => state.entryError)
  const items = useWorkbench((state) => state.items)
  const select = useWorkbench((state) => state.select)

  if (!selectedKey) {
    return <p className="p-4 text-sub text-n6">{codes.panelNoSelection}</p>
  }
  if (entryAvailability === 'missing') {
    return <Missing cap="/api/entry" detail={entryError} />
  }
  if (entryState === 'loading' || entryState === 'idle') {
    return (
      <div className="space-y-2 p-4" aria-busy="true" aria-label={codes.loading}>
        <div className="h-3 w-2/3 animate-pulse rounded bg-n2" />
        <div className="h-3 w-1/2 animate-pulse rounded bg-n2" />
        <div className="h-3 w-3/4 animate-pulse rounded bg-n2" />
      </div>
    )
  }
  if (entryState === 'error' || !entry) {
    const gone = Boolean(entryError && entryError.startsWith('not_found'))
    return (
      <div className="space-y-2 p-4 text-sub text-n6">
        <p className="text-n7">{gone ? codes.selectUnknown : '这一条没读出来。'}</p>
        {!gone && entryError ? <p className="font-mono text-meta">{entryError}</p> : null}
        <button
          type="button"
          onClick={() => void select(selectedKey)}
          className="rounded border border-n4 bg-n0 px-2 py-1 text-meta text-n7 hover:bg-n2 active:bg-n3"
        >
          {codes.retry}
        </button>
      </div>
    )
  }

  const { entry: row, actor, ledger, related } = entry
  // 选中项可能已被折叠进某一组：从当前页里把它捞出来做「这一条」的兜底展示。
  const fromList = items.find((item) => item.key === selectedKey)
  const pathText = row.path?.label || codes.noPath
  const okText =
    row.tool === null
      ? row.src === 'post'
        ? '登记'
        : row.src === 'push'
          ? '已发出'
          : '事件'
      : row.tool.ok === null
        ? UNKNOWN
        : row.tool.ok
          ? '成功'
          : '未成功'

  return (
    <div className="text-body">
      <section className="border-b border-n3/60 px-4 py-3">
        <h3 className="mb-1 text-sub font-head text-n7">这一条</h3>
        <Line label="类型">{TYPE_WORD[row.cls] || row.src}</Line>
        <p className="mt-1 text-sub leading-6 text-n7">{row.text || fromList?.text || UNKNOWN}</p>
        <Line label="操作面 / 目标">
          <span className="break-all font-mono text-meta">{pathText}</span>
        </Line>
        <Line label="结果">{okText}</Line>
        <Line label="时间">
          <span className="font-mono text-meta" title="库内是 UTC 时间，这里按本地时区显示">
            <Time ts={row.ts} withDate />
          </span>
        </Line>
        {related.project ? <Line label="项目">{related.project}</Line> : null}
        {related.board ? (
          <Line label="所属板">
            {related.board.label}
            {row.meta.boardScope === 'root' ? (
              <span className="ml-1 text-meta text-n6">（根看板，没有项目归属）</span>
            ) : null}
          </Line>
        ) : null}
        {related.code ? (
          <Line label="记忆编码">
            <span className="font-mono text-meta">{related.code}</span>
          </Line>
        ) : null}
        <Meta item={row} />
      </section>

      <section className="border-b border-n3/60 px-4 py-3">
        <h3 className="mb-1 text-sub font-head text-n7">这个人</h3>
        <Line label="名称">
          <span className="font-name">{actor.label}</span>
          {actor.labelKind === 'session' ? (
            <span className="ml-1 rounded bg-n2 px-1 text-meta text-n6">会话</span>
          ) : null}
          {actor.labelKind === 'none' ? (
            <span className="ml-1 rounded bg-n2 px-1 text-meta text-n6">无公民归属</span>
          ) : null}
        </Line>
        <Line label="会话">
          <span className="font-mono text-meta">{actor.sessionTag || UNKNOWN}</span>
        </Line>
        <Line label="宿主">
          <span className="font-mono text-meta">{actor.host || codes.noHost}</span>
          <span
            className="ml-1 cursor-help text-meta text-n6"
            title={actor.hostBasis}
            aria-label={actor.hostBasis}
          >
            ⓘ
          </span>
        </Line>

        <Line label="公民档案">
          {actor.citizen ? (
            <span>
              {actor.citizen.displayName || '未命名'}
              <span className="ml-1 text-meta text-n6">
                {[actor.citizen.kind, actor.citizen.status, actor.citizen.host]
                  .filter(Boolean)
                  .join(' · ')}
              </span>
            </span>
          ) : (
            <span className="text-n6">{codes.noCitizen}</span>
          )}
        </Line>

        <Line label="在不在跑">
          {actor.session.present ? (
            actor.session.running ? (
              <span className="text-ok">在跑（近 30 分钟有动作）</span>
            ) : (
              <span className="text-n6">不在跑（最近一次 {relativeOf(actor.session.lastSeenAt || null)}）</span>
            )
          ) : (
            <span className="text-n6">这个会话没有在场记录</span>
          )}
        </Line>

        <Line label="正在干什么">
          {actor.session.present ? (
            actor.session.autoSource ? (
              <span className="text-n6">已开始，还没说明在做什么</span>
            ) : (
              <span>{actor.session.doing || codes.dormant}</span>
            )
          ) : (
            <span className="text-n6">{codes.dormant}</span>
          )}
        </Line>

        <Line label="声明面">
          <span className="break-all font-mono text-meta">
            {actor.session.surface || codes.noSurface}
          </span>
        </Line>
        <Line label="实际面">
          <span className="break-all font-mono text-meta">
            {actor.session.autoSurface || '（没有自动登记的面）'}
          </span>
        </Line>
        {actor.session.surfaceMismatch ? (
          <p className="text-meta text-warn">{codes.surfaceMismatch}</p>
        ) : null}
      </section>

      <section className="px-4 py-3">
        <h3 className="mb-1 text-sub font-head text-n7">他的账本</h3>

        <h4 className="mt-2 text-meta font-head text-n6">最近工具动作</h4>
        {ledger.tools.length === 0 ? (
          <p className="text-meta text-n6">没有工具动作记录。</p>
        ) : (
          <ul className="space-y-0.5">
            {ledger.tools.map((tool) => (
              <li key={tool.key} className="flex items-baseline gap-2 text-meta leading-5">
                <span className="shrink-0 font-mono text-n6">
                  <Time ts={tool.ts} />
                </span>
                <span className="min-w-0 flex-1 truncate text-n7">
                  {tool.toolName || UNKNOWN}
                  {tool.pathLabel ? <span className="font-mono text-n6"> {tool.pathLabel}</span> : null}
                </span>
                <span className={`shrink-0 ${tool.ok === false ? 'text-err' : 'text-n6'}`}>
                  {tool.ok === null ? UNKNOWN : tool.ok ? '成功' : '未成功'}
                </span>
              </li>
            ))}
          </ul>
        )}

        <h4 className="mt-2 text-meta font-head text-n6">最近事件</h4>
        {ledger.events.length === 0 ? (
          <p className="text-meta text-n6">没有事件记录。</p>
        ) : (
          <ul className="space-y-0.5">
            {ledger.events.map((event) => (
              <li key={event.key} className="flex items-baseline gap-2 text-meta leading-5">
                <span className="shrink-0 font-mono text-n6">
                  <Time ts={event.ts} />
                </span>
                <span className="min-w-0 flex-1 truncate font-mono text-n7" title={event.type}>
                  {event.type}
                </span>
                <span className="shrink-0 font-mono text-n6">{event.code}</span>
              </li>
            ))}
          </ul>
        )}

        <h4 className="mt-2 text-meta font-head text-n6">按天摘要（最近几天）</h4>
        {ledger.days.length === 0 ? (
          <p className="text-meta text-n6">最近几天没有动作。</p>
        ) : (
          <ul className="space-y-0.5">
            {ledger.days.map((day) => (
              <li key={day.date} className="text-meta leading-5 text-n6">
                <span className="font-mono text-n7">{day.date}</span> · {num(day.count)} 处动作
              </li>
            ))}
          </ul>
        )}
        <p className="mt-2 text-meta text-n6">
          登记：{num(ledger.postCount)} 条（该公民在板上留下的正文）
        </p>
      </section>
    </div>
  )
}
