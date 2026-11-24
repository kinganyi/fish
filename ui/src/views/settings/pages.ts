/**
 * 设置页的二级页清单 —— **设置内部导航的唯一数据源**。
 *
 * 为什么单独一个文件而不是写在 `Settings.tsx` 里：二级导航与「推送页的判据脚本」要读
 * 同一份 id/label（`data-settings-page` 的取值必须是契约，不能靠两边各写一遍）。
 *
 * 三条纪律：
 *  1. **只放已建好的子页**：没做完的子页不要提前挂进来（画一个点不动的入口，等于给用户
 *     一个"够不着"的承诺）。
 *  2. 深链形状是 `?page=settings&sub=push`（查询串，不是 hash）：顶级页与二级页都是
 *     「看哪一屏」，与现场那屏的 `#/w?scope=…`（看哪一段）语义不同，混进一个 query 会让
 *     两种语义互相污染。
 *  3. 回写 `sub` 时**逐字节保留其它参数**：`?api=http://127.0.0.1:8790` 里的冒号若被
 *     `URLSearchParams` 重新编码成 `%3A`，地址栏就变一个样（虽然还能读）——那不是
 *     「回写一个键」，那是改了用户的 URL。与 `router.ts` 的 `writePage` 同一手法。
 */
export type SettingsPageId = 'hosts' | 'push'

export interface SettingsPageDef {
  /** 深链与 `data-settings-page` 用的稳定 id（不进中文，免得 %-编码）。 */
  id: SettingsPageId
  /** 二级导航里显示的名字。 */
  label: string
  /** 一句话说明（导航项第二行 + 顶部副标题）。 */
  hint: string
}

export const SETTINGS_PAGES: SettingsPageDef[] = [
  { id: 'hosts', label: 'Agent 接入', hint: '宿主：装没装、接没接、接的哪条通道' },
  { id: 'push', label: '推送', hint: '推送：有没有用、为什么没用、想调哪里' },
]

/** 缺省子页 = Agent 接入（与改造前的设置页一致：进来先看宿主）。 */
export const DEFAULT_SETTINGS_PAGE: SettingsPageId = 'hosts'

export function isSettingsPageId(raw: string | null | undefined): raw is SettingsPageId {
  return Boolean(raw) && SETTINGS_PAGES.some((page) => page.id === raw)
}

export function settingsPageOf(id: SettingsPageId): SettingsPageDef {
  return SETTINGS_PAGES.find((page) => page.id === id) ?? SETTINGS_PAGES[0]
}

/** 当前子页：只认 `sub`；不认识的值回落缺省页（不猜「最像」的那个）。 */
export function readSettingsPage(): SettingsPageId {
  const raw = new URLSearchParams(window.location.search).get('sub')
  return isSettingsPageId(raw) ? raw : DEFAULT_SETTINGS_PAGE
}

/** 回写子页（`replaceState`，不进历史栈；只增删 `sub=`，其它参数原样保留）。 */
export function writeSettingsPage(id: SettingsPageId): void {
  const raw = window.location.search.replace(/^\?/, '')
  const kept = raw
    .split('&')
    .filter(Boolean)
    .filter((part) => part.split('=')[0] !== 'sub')
  if (id !== DEFAULT_SETTINGS_PAGE) kept.push(`sub=${id}`)
  const search = kept.length > 0 ? `?${kept.join('&')}` : ''
  const next = window.location.pathname + search + window.location.hash
  const current = window.location.pathname + window.location.search + window.location.hash
  if (next !== current) {
    window.history.replaceState(null, '', next)
  }
}
