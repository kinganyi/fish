# 交接：生成防重 P0 已合 main 但生产未生效（dist 未重建）+ P1 定稿位置 + G-1 待办

> 编码：anyi-studio-261022-30 ｜ 类型：artifact ｜ 记录于 2026-10-21T22:38:22Z

> 交接对象：主AI（anyi-studio-主AI）。交接人：开发AI-2（`8a83d218`）。时间：2026-10-22 06:38。

## 一句话
**P0 前端止血已合入 main（`d33763e`），但生产未生效** —— 用户报告的「连点生成 → 重复扣费」在线上仍然存在。

---

## 一、代码现状（可复跑）

| 项 | 值 | 验证命令 |
|---|---|---|
| 提交 | `d33763e`（10-21 21:11） | `git log --oneline -1 d33763e` |
| 所在分支 | `main` + `origin/main` + `ssh-origin/main` | `git branch -a --contains d33763e` |
| 规模 | 14 文件，+1256 / −89 | `git show --stat d33763e` |
| 合并者 | 主AI（我未 push、未合并） | — |
| main 当前 | `2b7f80c`（10-22 06:12） | `git log --oneline -1 main` |

---

## 二、生产状态（关键：P0 没上线）

| 项 | 实测 |
|---|---|
| 生产 PID | `995825`，启动 **10-21 23:02:12** |
| `frontend/src/hooks/useSubmitGuard.ts` | **不存在** |
| `dist/assets/*.js` 含「正在提交」 | **0 个文件** |
| dist 入口 chunk | `index-D_B7pKfk.js`（与 10-20 的 `index-WtZaalxl.js` 不同，说明前端重建过，但**不含 P0**） |
| 三域 health | 200（服务本身正常） |

### 时序推断
```
10-21 21:11  我的 P0 提交 d33763e（在 feature/gen-idempotency）
10-21 23:02  主AI 部署生产（PID 995825）
             ↑ 此时若 main 已含 d33763e，部署仍用了旧 dist ⇒ 前端未重建
```
**结论：23:02 那次部署漏了前端构建（或只换后端）。代码在仓库里，生产跑的还是旧逻辑。**

---

## 三、需要主AI 做的（3 步，当天可完成）

1. **确认 23:02 部署的物料来源**：是没 `npm run build`，还是 build 了但拷错目录（`dist.bak_*` 多个备份容易拿错）
2. **重新构建并部署前端**：`cd frontend && npm run build` → 换 `/opt/anyi-studio/frontend/dist` → 刷新（nginx 静态，无需重启后端）
3. **验证（必做）**：
   ```bash
   grep -rl '正在提交' /opt/anyi-studio/frontend/dist/assets/*.js | wc -l   # 必须 > 0
   ```
   并通知用户强刷（Ctrl+F5）后手测：点生成 → 按钮变「正在提交…」且置灰。

---

## 四、P0 到底改了什么（供 review）

**新增**
- `frontend/src/hooks/useSubmitGuard.ts` —— ref 同步锁 + state 驱动 UI，**置位在任何 await 之前**（只写 state 等于没修，React 批处理会穿透）
- `frontend/src/utils/submitResult.ts` —— `isUnknownSubmitCode(code)`：`code === -1 || (code >= 500 && code < 1000)`

**修改**
- `pages/canvas/view/GenerationPanel.tsx` —— `disabled={!isGenModelValid || submitting}` + `aria-busy` + Tooltip「正在提交…」
- `pages/WorkspacePage.tsx` —— 套守卫（删掉本地 `useState(isSubmitting)`），并修 `noChannel` 早退未复位导致按钮永久置灰
- `components/AssetCard.tsx` —— 两个菜单项补 `busyRef`（原先只有 IconButton 有 disabled）
- `pages/canvas/controller/useGenerationFlow.ts` —— `handleGenerate` 包 `tryRun`；**删掉冗余预检 `fetchRefValidation`**（后端建单前已跑同一校验，1504 在扣费之前）
- `hooks/useTaskList.ts` —— `1702` 文案改为「积分不足：本次需 X，当前 Y」；`code:-1` 中文文案 + 工作台 `refresh()` 弱确认

**条件释放语义**（核心）
| 结果 | 锁 | 理由 |
|---|---|---|
| 成功 | 释放 | — |
| 业务码拒绝（1502/1504/1610/1701/1702/1703…） | 释放 | 后端明确拒绝，扣费前拦下 |
| `code:-1` / 5xx / throw | **不释放** | 结果未知，可能已扣费 |

不释放时给两个不产生新请求的出口：「查看任务列表」「刷新页面」，**无重试按钮**。

**验证数据**：`tsc -b --force` EXIT=0；`vitest run` **177 files / 1938 tests / 0 failed**（基线 172/1912）；门禁 audit 568=568、ACL 全零；反证：摘掉守卫新用例必红（3 次/2 次调用）。

---

## 五、P1 未做（设计已定稿，待放行）

| 产物 | 路径 |
|---|---|
| 验收清单 | `I:/app/anyi-studio-AI工作空间/docs/t03-t04-acceptance.md`（604 行，A-1~A-20 / 23 颗雷 / 9 条不可豁免） |
| 系统设计 | 同目录 `system_design.md`（1514 行） |
| 变更清单 | 同目录 `change-log-v1.0-to-v1.5.md`（313 行） |

**核心设计（四条，均经过对抗修正）**
1. **删 `claimed` 中间态**，改为同一 `db_write_tx` 段内四步原子提交（查完成记录 → check_and_hold_no_commit → INSERT generations → INSERT submit_idempotency）
2. **不动 `hold_` 锚格式**（原方案改 cid 锚会让 `_latest_hold` 等 5 处查询失效 → **退款一分不退且不报错**）
3. **双维度状态** `task_state × billing_state` 正交（单枚举五态必漏组合）
4. **三条腿**：同 cid 同参→回带；**同 cid 异参→409 不得回带**（否则用户改了 prompt 重试会静默拿到旧结果）；不同 cid→新建

**T03 九条不可豁免**：A-1 5bis（释放按钮不得换 cid）/ A-3（退款净额 + 失败时不得说「正在生成」）/ A-10（组合矩阵 + 文案溯源列）/ A-14（锚格式 round-trip）/ A-15（退款超时承诺）/ A-16（cid 与幂等表必须同批上线）/ A-17（空串 cid 旁路，否则上线即全站提交中断）/ A-19（三条腿第三条）/ A-20（建唯一索引前的脏数据检测）

---

## 六、范围外但已记录（不要漏）

**G-1 结算补扣失控**：`settle_task` 多退少补，`settle_*` 分支会在实付 > 预扣时**补扣**，且：
- **无上限**（grep `cap/limit/max_charge/threshold` 零命中）
- **余额不足时转 `adjust(key, -delta)` → 用户余额直接变负** = 系统单方面授信
- force 弹窗那句「将扣除 {N} 积分」是**事前承诺**，与实际「可能更多」不符

四个必须定的口径：补扣上限 / 告知时点（必须结算那一刻）/ 负余额下的功能（只读降级）/ 处置方向（**优先事前截断，而非事后欠账**）。

---

## 七、协作约束（避免返工）

1. **不要重复实现 P0**（已在 main）
2. **不要用参数哈希做幂等键**（AWS 明确否决：两次相同 prompt 是合法需求，会被误吞）
3. **不要动 `hold_` 锚格式**（5 处按 task_id 解析，改则退款链全断）
4. **不要用 `code >= 500` 判「结果未知」**（本项目业务码是四位数 1001/1702… 全部 > 500，会把「积分不足」判成永久锁死）
5. **工作树**：我的 `I:/app/anyi-studio-genfix` 已被清理，现只剩 `I:/app/anyi-studio`（main @ 2b7f80c）。P1 要动 `task_manager` 内部结构，**需独立工作树**，不要在主树直接改

---

## 八、用户已定的口径

- **同节点生成中，按钮禁用直到成功或失败**（复制/创建副本不受影响）—— 注意：P0 只覆盖「请求在途」，**任务在途仍可点**，这属 P1 的节点互斥
- **历史重复扣费不退**
- **cid 生命周期**：一次意图一个 cid，重试复用，改参数换新，只有用户显式「再来一次」才换

