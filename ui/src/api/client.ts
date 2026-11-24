import type { Envelope } from './types'

/**
 * 读口与写口的 HTTP 客户端。
 *
 * **基址可配置**（F-2：交付拓扑是「UI 静态托管 + API 独立进程」，前端不能假设同源）。
 * 优先级从高到低：
 *   1. 地址栏 `?api=http://127.0.0.1:8790`——静态托管（如 `python -m http.server`）时用它；
 *   2. 构建期 `VITE_FISH_API_BASE`（`npm run build` 时定死，适合打包给宿主壳层）；
 *   3. 同源相对路径——读口 `--dist` 同源发静态、或 Vite dev 的 `/api` proxy（天然满足）。
 * 空串 = 同源。**非 http(s) 的写法一律忽略**（不把地址栏里随便一段字符串当 URL 用）。
 *
 * **读口默认只读；唯一的写口是 `/api/push/*`**（白名单 + 类型/范围 + 审计，见
 * `p0/fish_push_audit.py`），以及不写盘的 `POST /api/hosts/<id>/refresh`。所以这里只有
 * 三个出口：`getJson` / `requestJson`（读）、`postJson`（重探宿主）、`postJsonBody`
 * （写一个白名单配置项 / 应用预设）。**没有通用 `request(method, body)`**——铺开成通用方法，
 * 下一个人就会顺手加一个「什么都能写」的 POST。
 *
 * 两类失败必须分开（97 §D5：前端**不推断**状态，但"读口没起来"只能由网络层判）：
 *   · `NetworkError` —— 连接都没建立（读口未起 / 端口不对）→ 整屏「读口未起」；
 *   · `ApiError`    —— 读口回了错误信封（400/404/405/503/500）→ 按三段文案展示。
 */
const BASE_INFO = resolveApiBase()
const BASE = BASE_INFO.base

export type ApiBaseSource = 'query' | 'build' | 'same-origin'

export function apiBaseInfo(): { base: string; source: ApiBaseSource } {
  return BASE_INFO
}

function resolveApiBase(): { base: string; source: ApiBaseSource } {
  const candidates: [string, ApiBaseSource][] = [
    [new URLSearchParams(window.location.search).get('api') || '', 'query'],
    [(import.meta.env.VITE_FISH_API_BASE as string | undefined) || '', 'build'],
  ]
  for (const [raw, source] of candidates) {
    const text = raw.trim()
    if (!text) continue
    try {
      const url = new URL(text)
      if (url.protocol === 'http:' || url.protocol === 'https:') {
        return { base: url.origin + url.pathname.replace(/\/+$/, ''), source }
      }
    } catch {
      // 不是绝对 URL：忽略（`?api=javascript:…` 这类不能进 fetch）
    }
  }
  return { base: '', source: 'same-origin' }
}

export class NetworkError extends Error {
  readonly kind = 'unreachable' as const
  constructor(message: string) {
    super(message)
    this.name = 'NetworkError'
  }
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly hint: string
  readonly action: string
  constructor(status: number, code: string, hint: string, action: string) {
    super(hint)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.hint = hint
    this.action = action
  }
}

export type Query = Record<string, string | number | boolean | null | undefined>

/**
 * 错误 → 一句话（三段式的前两段：出了什么 + 为什么）。
 *
 * 放在 client 里是因为它读的是 `ApiError`/`NetworkError` 的字段（同一份来源）；
 * 视图不自己拼错误文案，免得两处写法慢慢漂开（「连不上」与「读失败」必须分得清）。
 */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) return `${error.code}：${error.hint}`
  if (error instanceof NetworkError) return `连不上读口（${error.message}）`
  return (error as Error)?.message || '未知错误'
}

function buildUrl(path: string, params?: Query): string {
  const url = new URL(path, window.location.origin)
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      // 空串 = 不传（`?channel=` 与「没有这个参数」在后端是两种语义，但这一层只表达「不传」）。
      // 需要有语义的「空」时用**显式哨兵**（如 `reaction=pending`），别指望空串能过网关：
      // 后端 `parse_qs` 默认也丢空值，两头都到不了（评审 🟠-2）。
      if (value === null || value === undefined || value === '') continue
      url.searchParams.set(key, String(value))
    }
  }
  return BASE + url.pathname + url.search
}

export async function getJson<T>(path: string, params?: Query, signal?: AbortSignal): Promise<Envelope<T>> {
  return (await requestPayload(path, params, signal)) as Envelope<T>
}

/**
 * POST **重探一次**：`POST /api/hosts/<id>/refresh`（不写盘）。
 *
 * 为什么单独一个函数而不是给 `getJson` 加 `method` 参数：读与动作是两种语义，
 * 混成一个函数之后没人分得清哪次调用会改东西。写配置请用 `postJsonBody`。
 */
export async function postJson<T>(path: string, signal?: AbortSignal): Promise<Envelope<T>> {
  return (await requestPayload(path, undefined, signal, 'POST')) as Envelope<T>
}

/**
 * POST **带 JSON body**：`/api/push/config`（单键）与 `/api/push/preset`（止血/恢复）。
 *
 * 这是全前端**唯一**写配置的出口。白名单/类型/范围/审计都在后端判：
 * 前端把 `key` 与用户填的 `value` 原样发过去，收到 400 就贴后端原文（`ApiError.hint`），
 * **不在前端自己判可写**——两处判据必然漂开，而写错配置是会立刻影响行为的。
 */
export async function postJsonBody<T>(
  path: string,
  body: unknown,
  signal?: AbortSignal,
): Promise<Envelope<T>> {
  return (await requestPayload(path, undefined, signal, 'POST', body)) as Envelope<T>
}

/**
 * 非信封端点（`/api/meta`）：同样的错误判别，但载荷**原样**返回。
 *
 * 为什么不让 meta 也套信封：它是读口自述（schema 名/能力/库路径/进程号），
 * 套一层 `data` 只会让外部脚本多剥一层，且 `availability`/`sources` 对它没有意义。
 */
export async function requestJson<T>(path: string, params?: Query, signal?: AbortSignal): Promise<T> {
  return (await requestPayload(path, params, signal)) as T
}

async function requestPayload(
  path: string,
  params?: Query,
  signal?: AbortSignal,
  method: 'GET' | 'POST' = 'GET',
  body?: unknown,
): Promise<unknown> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  let response: Response
  try {
    response = await fetch(buildUrl(path, params), {
      method,
      headers,
      signal,
      body: body === undefined ? undefined : JSON.stringify(body),
      // 只读快照面：不缓存，避免 2 秒轮询读到上一轮
      cache: 'no-store',
    })
  } catch (error) {
    if ((error as Error)?.name === 'AbortError') throw error
    throw new NetworkError((error as Error)?.message || '连接失败')
  }

  let payload: unknown = null
  try {
    payload = await response.json()
  } catch {
    payload = null
  }

  if (!response.ok) {
    const envelope = payload as Envelope<unknown> | null
    // 关键判别：**本读口的错误一定带 `schema` 信封**。开发代理在后端没起时回的是
    // Vite 自己的 500（没有 schema）、网关错误同理；静态托管把 `/api` 当普通文件 404
    // 回的是 HTML——那都属于"读口未起"，不是"这一块缺"。
    // 不这么分，用户会看到"没读出来"而不知道是读口根本没跑。
    const isOurs = Boolean(
      envelope && typeof envelope === 'object' && typeof envelope.schema === 'string',
    )
    if (!isOurs) {
      throw new NetworkError(`HTTP ${response.status}，且响应不是本读口的错误信封`)
    }
    throw new ApiError(
      response.status,
      envelope?.code || `http_${response.status}`,
      envelope?.hint || `读口回了 ${response.status}`,
      envelope?.action || '看服务端日志；这多半是读口的缺陷',
    )
  }
  return payload
}
