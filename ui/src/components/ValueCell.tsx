import { UNKNOWN, num } from '../format'

/**
 * 数字渲染的唯一出口。
 *
 * 为什么必须是一个组件：`null` 显示「判不到」这条纪律如果靠每个调用点自觉，
 * 迟早有人写 `value ?? 0`——那会把"证据不足"渲染成"读数是零"（96 §七共同纪律）。
 */
export function ValueCell({
  value,
  suffix,
  className = '',
}: {
  value: number | null | undefined
  suffix?: string
  className?: string
}) {
  const known = value !== null && value !== undefined
  return (
    <span
      className={`font-name tabular-nums ${known ? 'text-n7' : 'text-n6'} ${className}`}
      title={known ? undefined : UNKNOWN}
    >
      {known ? num(value) : UNKNOWN}
      {known && suffix ? <span className="ml-0.5 font-body text-meta text-n6">{suffix}</span> : null}
    </span>
  )
}
