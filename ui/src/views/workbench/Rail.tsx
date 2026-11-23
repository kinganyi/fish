import { useState } from 'react'
import type { BoardFacet, ProjectFacet } from '../../api/types'
import { Block } from '../../components/AsyncBoundary'
import { StatusLine } from '../../components/StatusLine'
import { codes } from '../../codes'
import { num, relativeOf } from '../../format'
import { toggleScope, UNATTRIBUTED_LABEL, type Scope } from '../../store/scope'
import { isEmptyDb, useWorkbench } from '../../store/workbench'

function ProjectButton({
  facet,
  active,
  onPick,
}: {
  facet: ProjectFacet
  active: boolean
  onPick: () => void
}) {
  return (
    <button
      type="button"
      data-rail=""
      aria-pressed={active}
      onClick={onPick}
      className={`flex w-full items-center gap-1 rounded py-1 pr-1 pl-2 text-left text-sub active:bg-n3 ${
        active
          ? 'border-l-2 border-l-accent bg-accent-soft pl-1.5 font-name text-accent'
          : 'border-l-2 border-l-transparent text-n7 hover:bg-n2'
      }`}
      title={`${facet.label} · ${num(facet.count)} 条行为 · ${relativeOf(facet.lastTouchedAt)}`}
    >
      <span className="min-w-0 flex-1 truncate">{facet.label}</span>
      <span className="shrink-0 font-mono text-meta text-n6">{num(facet.count)}</span>
    </button>
  )
}

function BoardButton({
  board,
  active,
  onPick,
}: {
  board: BoardFacet
  active: boolean
  onPick: () => void
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onPick}
      className={`flex w-full flex-col gap-0.5 rounded py-1 pr-1 pl-2 text-left text-sub active:bg-n3 ${
        active
          ? 'border-l-2 border-l-accent bg-accent-soft pl-1.5 font-name text-accent'
          : 'border-l-2 border-l-transparent text-n7 hover:bg-n2'
      }`}
      title={`${board.label} · 在场 ${board.memberCount} 个会话 · 登记 ${board.postCount} 条`}
    >
      <span className="flex items-center gap-1">
        <span className="min-w-0 flex-1 truncate">{board.label}</span>
        {board.runningCount > 0 ? (
          <span className="shrink-0 rounded bg-n2 px-1 font-mono text-meta text-n7">
            在跑 {board.runningCount}
          </span>
        ) : null}
      </span>
      <span className="truncate font-mono text-meta text-n6">
        {board.memberCount} 会话 · {board.postCount} 条登记
      </span>
    </button>
  )
}

/**
 * 左栏 = 切片器（97 §A2）。
 *
 * 它不是"项目列表"也不是"看板列表"，是**当前视图的作用域**：同一时刻只有一刀生效，
 * 点一次筛、再点一次取消。项目块按 `lastTouchedAt` 降序取前 3，另加「未归属」
 * （24h 里 91.7% 的工具动作不带项目，不加这一格用户永远点不到那部分）。
 * 看板块的作用域是**板上会话的行为**，不是板上登记的条数（差 500 倍，97 §D2）。
 *
 * 「空库」判据与中栏**同一份**（QA K1）：`isEmptyDb(facets, stats)`。原来这里只看
 * `facets.window.total === 0`，于是"停了一整天再打开"时左栏说"这个库还是空的"、
 * 中栏说"这个时间窗里没有行为"——同一屏两句话互相打脸。
 */
export function Rail() {
  const facets = useWorkbench((state) => state.facets)
  const facetsState = useWorkbench((state) => state.facetsState)
  const facetsAvailability = useWorkbench((state) => state.facetsAvailability)
  const lastError = useWorkbench((state) => state.lastError)
  const scope = useWorkbench((state) => state.scope)
  const win = useWorkbench((state) => state.win)
  const totalKnown = useWorkbench((state) => state.totalKnown)
  const stats = useWorkbench((state) => state.stats)
  const setScope = useWorkbench((state) => state.setScope)
  const refreshFacets = useWorkbench((state) => state.refreshFacets)

  const [allProjects, setAllProjects] = useState(false)
  const [allBoards, setAllBoards] = useState(false)

  const pick = (next: Scope) => setScope(toggleScope(scope, next))
  const projects = facets?.projects ?? []
  const shownProjects = allProjects ? projects : projects.slice(0, 3)
  const hiddenProjects = Math.max(0, projects.length - shownProjects.length)
  const boards = facets?.boards ?? []
  const shownBoards = allBoards ? boards : boards.slice(0, 3)
  const hiddenBoards = Math.max(0, boards.length - shownBoards.length)

  // 「库空」= 窗口里没有 **且** 累计也没有（与中栏同一个函数，不许各自推导）。
  const dbEmpty = isEmptyDb(facets, stats)

  return (
    <aside
      className="flex min-w-0 flex-col overflow-y-auto border-r border-n3 bg-n1"
      aria-label="切片器"
    >
      <StatusLine scope={scope} win={win} total={totalKnown} />

      <div className="border-b border-n3/60 px-2 py-3">
        <h2 className="mb-1 px-1 text-meta font-head tracking-wide text-n6">项目</h2>
        <Block
          state={facetsState}
          availability={facetsAvailability}
          availabilityDetail={facetsAvailability === 'missing' ? lastError : null}
          cap="facets"
          errorText={lastError}
          onRetry={() => void refreshFacets()}
          emptyText={dbEmpty ? codes.emptyDb : codes.projectEmptyWindow}
        >
          {shownProjects.length === 0 && !facets?.unattributed ? (
            <p className="px-1 text-meta text-n6">
              {dbEmpty ? codes.emptyDb : codes.projectEmptyWindow}
            </p>
          ) : (
            <div className="space-y-0.5">
              {shownProjects.map((facet) => (
                <ProjectButton
                  key={facet.name}
                  facet={facet}
                  active={scope.kind === 'project' && scope.name === facet.name}
                  onPick={() => pick({ kind: 'project', name: facet.name, label: facet.label })}
                />
              ))}
              {facets?.unattributed ? (
                <ProjectButton
                  facet={facets.unattributed}
                  active={scope.kind === 'project-none'}
                  onPick={() => pick({ kind: 'project-none', label: UNATTRIBUTED_LABEL })}
                />
              ) : null}
              {hiddenProjects > 0 ? (
                <button
                  type="button"
                  onClick={() => setAllProjects(true)}
                  className="w-full rounded px-2 py-1 text-left text-meta text-n6 hover:bg-n2 hover:text-n7 active:bg-n3"
                >
                  其余 {hiddenProjects} 个项目（展开）
                </button>
              ) : null}
            </div>
          )}
        </Block>
      </div>

      <div className="px-2 py-3">
        <h2 className="mb-1 px-1 text-meta font-head tracking-wide text-n6">今日板</h2>
        <Block
          state={facetsState}
          availability={facetsAvailability}
          availabilityDetail={facetsAvailability === 'missing' ? lastError : null}
          cap="facets"
          errorText={lastError}
          onRetry={() => void refreshFacets()}
          emptyText={dbEmpty ? codes.emptyDb : codes.boardEmptyWindow}
        >
          {shownBoards.length === 0 ? (
            <p className="px-1 text-meta text-n6">
              {dbEmpty ? codes.emptyDb : codes.boardEmptyWindow}
            </p>
          ) : (
            <div className="space-y-0.5">
              {shownBoards.map((board) => (
                <BoardButton
                  key={board.id}
                  board={board}
                  active={scope.kind === 'board' && scope.id === board.id}
                  onPick={() => pick({ kind: 'board', id: board.id, label: board.label })}
                />
              ))}
              {hiddenBoards > 0 ? (
                <button
                  type="button"
                  onClick={() => setAllBoards(true)}
                  className="w-full rounded px-2 py-1 text-left text-meta text-n6 hover:bg-n2 hover:text-n7 active:bg-n3"
                >
                  其余 {hiddenBoards} 块（展开）
                </button>
              ) : null}
            </div>
          )}
        </Block>
      </div>
    </aside>
  )
}
