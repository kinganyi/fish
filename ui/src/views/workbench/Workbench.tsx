import { NavRail } from '../../components/NavRail'
import type { PageId } from '../../pages'
import { Settings } from '../settings/Settings'
import { Panel } from './Panel'
import { Rail } from './Rail'
import { Stream } from './Stream'

/**
 * 四栏骨架（用户 2026-11-23 裁决：**是四栏，不是三栏**）。
 *
 *   52px 顶级页签窄条 ｜ 200px 切片器 ｜ 中 ≥55% 流水 ｜ 320px 判断面
 *
 * 宽度重算（加窄条后必须重算，不能沿用三栏时代的 200/·/360）：
 *
 * | 视口 | 固定列 52+200+320=572 | 中栏 | 中栏占比 |
 * |---|---|---|---|
 * | 1280 | 572 | 708 | 55.3% |
 * | 1440 | 572 | 868 | 60.3% |
 *
 * 中栏仍是三列里最大的（708 > 320 > 200），且 55% 这条硬线在 1280 下仍成立——
 * 右栏取 320（设计允许 320–400 的下限），把省下的 40px 全部让给正文区。
 *
 * 更窄时不缩栏宽、不折栏（`min-w-[1280px]` + 横向滚动）：折栏等于砍掉"左筛中、中改右"
 * 这个三角。1280×720 与 1440×900 两个基准视口都**不得横向溢出**，机检见
 * `p0/tools/shoot_workbench.py`（每条读数里带 `scrollWidth == clientWidth`）。
 *
 * 窄条不是装饰：它是**产品一级导航**（97 §A6-1 曾把它列进删除项，该裁决已被用户作废）。
 *
 * **设置页**是第二个面（用户 2026-11-23 新增）：它不属于现场那套四栏——
 * 宿主接入回答的是「这套东西装没装、能不能用」（时间尺度天到月），塞进 320px 的判断面
 * 会把「此刻谁在动哪块」和「哪个宿主没接 fish」混成一屏。所以它就是 `52px + 1fr` 两栏，
 * 页面内部的「左列表 / 右详情」由 `Settings` 自己排。
 */
export function Workbench({ page, onPick }: { page: PageId; onPick: (id: PageId) => void }) {
  if (page === 'settings') {
    return (
      <div
        className="grid h-full min-w-[1280px] grid-rows-1 bg-n1"
        style={{ gridTemplateColumns: '52px minmax(0, 1fr)' }}
      >
        <NavRail current={page} onPick={onPick} />
        <Settings />
      </div>
    )
  }
  return (
    <div
      className="grid h-full min-w-[1280px] grid-rows-1 bg-n1"
      style={{ gridTemplateColumns: '52px 200px minmax(0, 1fr) 320px' }}
    >
      <NavRail current={page} onPick={onPick} />
      <Rail />
      <Stream />
      <Panel />
    </div>
  )
}
