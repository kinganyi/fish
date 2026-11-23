import type { ReactNode } from 'react'
import type { AvailState } from '../api/types'
import type { LoadState } from '../store/workbench'
import { codes, fill } from '../codes'

/**
 * 区块级的三态/空态壳（97 §D5：服务端给 `availability.state`，前端**不推断**）。
 *
 * 五句话必须分得清：0 是读数、「空」是状态、「缺」是能力、「判不到」是证据不足、
 * 「连不上」是读口没起来。所以这里不给"加载失败"这种模糊措辞，也不给骨架数字。
 *
 * 关于死分支（QA F-7）：本组件原来还有一条 `availability === 'partial'`，但
 * ① 后端没有任何路径能产出 `partial`（F-8 已把它整条删掉）；② 调用方从来不传
 * `availability`，于是 `missing`/`empty` 两条也是死的。现在 `availability` 由
 * store 逐块落盘、由 `Rail` 真正传进来（`facetsAvailability`），三条分支都至少被
 * 构造一次：空库 → `empty`、缺表副本库 → `missing`、正常库 → `ok`、
 * 首帧 → `idle`/`loading`、接口报错 → `error`。
 */
export function Block({
  state,
  availability,
  availabilityDetail,
  cap,
  emptyText,
  errorText,
  onRetry,
  children,
}: {
  state: LoadState
  availability?: AvailState
  availabilityDetail?: string | null
  cap: string
  emptyText?: string
  errorText?: string | null
  onRetry?: () => void
  children: ReactNode
}) {
  if (state === 'idle' || state === 'loading') {
    // 只占位不填假数字（A-2）：一条中性占位条，没有数字、没有示例值。
    return (
      <div className="animate-pulse space-y-2" aria-busy="true" aria-label={codes.loading}>
        <div className="h-3 w-2/3 rounded bg-n2" />
        <div className="h-3 w-1/2 rounded bg-n2" />
      </div>
    )
  }
  if (state === 'error') {
    return (
      <div className="space-y-1 text-sub text-n6">
        <p className="text-n7">这一块没读出来。</p>
        {errorText ? <p className="break-words font-mono text-meta">{errorText}</p> : null}
        <p>能做的：确认读口在跑，然后点重试。</p>
        {onRetry ? (
          <button
            type="button"
            onClick={onRetry}
            className="mt-1 rounded border border-n4 bg-n0 px-2 py-1 text-meta text-n7 hover:bg-n2 active:bg-n3"
          >
            {codes.retry}
          </button>
        ) : null}
      </div>
    )
  }
  if (availability === 'missing') {
    // 「缺」是能力缺失，不是空：说清缺哪个 cap，不冒充成 0。
    return (
      <div className="space-y-0.5 text-sub text-n6">
        <p>{fill(codes.sourceMissing, { cap })}</p>
        {availabilityDetail ? (
          <p className="break-words font-mono text-meta">{availabilityDetail}</p>
        ) : null}
      </div>
    )
  }
  if (availability === 'empty' && emptyText) {
    return <p className="text-sub text-n6">{emptyText}</p>
  }
  return <>{children}</>
}

/**
 * 「缺」的独立表态（评审 R-08）：**缺哪个能力**，且明确**不是读口没起来**。
 *
 * 为什么不能复用 `error` 那一段：`error` 的补救动作是"确认读口在跑，然后重试"，
 * 而 `missing` 是缺表/缺库——重试一万次也不会好。把两者混成一句，用户会一直点重试。
 * 所以这里**不给重试按钮**，只给原因（后端 `availability.detail` 原样透出）。
 */
export function Missing({ cap, detail }: { cap: string; detail?: string | null }) {
  return (
    <div className="m-4 space-y-1 rounded border border-n3 bg-n1 p-4 text-sub text-n6">
      <p className="text-n7">{fill(codes.missingCap, { cap })}</p>
      {detail ? <p className="break-words font-mono text-meta text-n6">{detail}</p> : null}
    </div>
  )
}

/** 加载中/出错时给标题行用的小徽标（不是数字，不参与读数）。 */
export function StateTag({ state }: { state: LoadState }) {
  if (state === 'loading' || state === 'idle') {
    return <span className="text-meta text-n6">正在读…</span>
  }
  if (state === 'error') {
    return <span className="text-meta text-err">没读出来</span>
  }
  return null
}
