import type { BoardFacet } from '../api/types'

/**
 * 切片器状态机（97 §B1）。
 *
 * 写成**一个枚举**而不是「项目 + 看板」两个独立开关：两者是两条不同的切片轴，
 * 组合后的语义没人要（"大肥鱼项目里的 CZHY 板"），却要多一层状态与一组空集分支。
 * 判据：代码里出现 `if (scope.kind === 'project' && scope.kind === 'board')` 即红。
 */
export type Scope =
  | { kind: 'all' }
  | { kind: 'project'; name: string; label: string }
  | { kind: 'project-none'; label: string }
  | { kind: 'board'; id: number; label: string }

export type Tab = 'need' | 'ledger'

export const SCOPE_ALL: Scope = { kind: 'all' }
export const UNATTRIBUTED_LABEL = '未归属'

export function scopeLabel(scope: Scope): string {
  switch (scope.kind) {
    case 'all':
      return '全部切片'
    case 'project':
      return scope.label
    case 'project-none':
      return UNATTRIBUTED_LABEL
    case 'board':
      return scope.label
  }
}

export function isFiltered(scope: Scope): boolean {
  return scope.kind !== 'all'
}

/** 深链用的紧凑写法：`all` / `project:<名>` / `project:__none__` / `board:<id>`。 */
export function scopeToParam(scope: Scope): string {
  switch (scope.kind) {
    case 'all':
      return 'all'
    case 'project':
      return `project:${scope.name}`
    case 'project-none':
      return 'project:__none__'
    case 'board':
      return `board:${scope.id}`
  }
}

/** 参数 → 状态。板名需要从 facets 里回查（深链刷新时 facets 还没到就先用 id）。 */
export function scopeFromParam(param: string, boards: BoardFacet[]): Scope {
  if (!param || param === 'all') return SCOPE_ALL
  if (param.startsWith('project:')) {
    const name = param.slice('project:'.length)
    if (name === '__none__') return { kind: 'project-none', label: UNATTRIBUTED_LABEL }
    return { kind: 'project', name, label: name }
  }
  if (param.startsWith('board:')) {
    const id = Number(param.slice('board:'.length))
    const board = boards.find((b) => b.id === id)
    return { kind: 'board', id, label: board?.label || `看板 ${id}` }
  }
  return SCOPE_ALL
}

/** 左栏点击：同一刀再点一次 = 取消（97 §B1）。 */
export function toggleScope(current: Scope, next: Scope): Scope {
  if (current.kind !== next.kind) return next
  if (current.kind === 'project' && next.kind === 'project' && current.name === next.name) {
    return SCOPE_ALL
  }
  if (current.kind === 'project-none' && next.kind === 'project-none') return SCOPE_ALL
  if (current.kind === 'board' && next.kind === 'board' && current.id === next.id) return SCOPE_ALL
  return next
}
