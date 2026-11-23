import { clockOf, clockShort, stampLongOf, stampOf } from '../format'

/**
 * 时间列：**默认到分钟，悬停展开到秒**（评审 L1 的「按下态/悬停」那一条）。
 *
 * 为什么不常显秒：等宽 11–12px 下 `HH:mm:ss` 比 `HH:mm` 宽三分之一，会把同一行的
 * 类型/项目挤成省略号；而扫读要的是"几点几分"，秒只在核对同一分钟内的先后时才需要。
 * 为什么用 CSS 而不是 state：悬停是纯视觉，进 state 会让虚拟列表每行都参与重渲染。
 */
export function Time({
  ts,
  withDate = false,
  className = '',
}: {
  ts: string | null | undefined
  /** 跨天列表（页签二的「时间」行）要带日期：`MM-DD HH:mm` → `MM-DD HH:mm:ss`。 */
  withDate?: boolean
  className?: string
}) {
  const short = withDate ? stampOf(ts) : clockShort(ts)
  const long = withDate ? stampLongOf(ts) : clockOf(ts)
  return (
    <span className={`group/time inline-flex ${className}`} data-clock="">
      <span className="tabular-nums group-hover/time:hidden">{short}</span>
      <span className="hidden tabular-nums group-hover/time:inline">{long}</span>
    </span>
  )
}
