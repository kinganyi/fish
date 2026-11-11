/**
 * 宿主登记表：**"哪份文件是哪个宿主的 hooks 配置"只在这里回答一次**。
 *
 * 事实源是生产树的 `p0/install_hooks.py`（`TARGETS` + `desired_hooks`）。本模块只复刻
 * **读侧**需要的定位信息（家目录 / 文件路径 / 四个必装事件 / 每宿主的唯一采集器），
 * 不复刻"目标配置长什么样"——那份事实在 dry-run 时由 Python 现场算出（见 `dryrun.ts`）。
 *
 * 为什么路径不许硬编码成 `C:\Users\15596\...`：那样换一台机器（或换个用户名）就静默
 * 读到"没有配置"，而"没配"和"读了别处"在报告里长得一模一样。所以默认值走
 * `os.homedir()`，且允许 `DSH_HOME` / `WORKBUDDY_HOME` 覆盖（与 install_hooks 同名）。
 *
 * @module dsh-fish/hosts/registry
 */

import { homedir } from 'node:os'
import path from 'node:path'

/** 四个必须安装的事件（`install_hooks.REQUIRED_EVENTS`，两宿主一致）。 */
export const REQUIRED_EVENTS = ['SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse'] as const

/** 事件名。 */
export type HostEvent = (typeof REQUIRED_EVENTS)[number]

/** 每宿主允许出现**恰好一个**采集器（两个都注册 = 双份行 + 双倍延迟）。 */
export const COLLECTOR_SCRIPTS = ['fish_push_hook.py', 'fish_posttool_hook.py'] as const

/** fish 的六个 hook 脚本（缺任何一个，安装器的 `--check` 就会红）。 */
export const FISH_HOOK_SCRIPTS = [
  'fish_dsh_hook.py',
  'fish_session_hook.py',
  'fish_prompt_hook.py',
  'fish_pretool_hook.py',
  'fish_push_hook.py',
  'fish_posttool_hook.py',
] as const

/** 写类工具名的**两种大小写**（DSH 小写 / WorkBuddy 首字母大写）。 */
export const WRITE_TOOL_NAMES_LOWER = ['write', 'edit', 'multiedit', 'notebookedit', 'apply_patch'] as const

/** 写类 matcher 必须覆盖的最小字面量集合。 */
export const WRITE_MATCHER_REQUIRED = ['write', 'Write', 'edit', 'Edit'] as const

/** 一个宿主。
 *
 * `host` 是 fish 侧的口径（`citizens.host` 的取值、心跳文件名里的 `host`），
 * 与安装器的 `TARGETS` 键一致（`dsh` / `workbuddy`）。
 */
export interface HostSpec {
  readonly id: string
  readonly label: string
  /** hooks 配置文件绝对路径。 */
  readonly configPath: string
  /** 家目录（配置所在的那一层）。 */
  readonly home: string
  /** 覆盖用的环境变量名（与 install_hooks 同名）。 */
  readonly homeEnv: string
  /** 该宿主的采集器脚本名（心跳新鲜度按它判活）。 */
  readonly collectorScript: string
  /** 一句人话：这个宿主的配置有什么特殊性。 */
  readonly note: string
}

/** 环境变量读取（空串视为未设置）。 */
function envHome(name: string): string {
  const raw = (process.env[name] ?? '').trim()
  return raw === '' ? '' : path.resolve(raw)
}

/**
 * 解析两个宿主的位置（**只读**：不创建目录、不写文件）。
 *
 * 顺序：`DSH_HOME` / `WORKBUDDY_HOME` > `os.homedir()` 下的默认位置。
 * 生产树 `install_hooks.py` 把默认值写死成 `C:\Users\15596\...`；本机两者等价，
 * 但换机器时硬编码会静默指错——这是与生产的一处**有意偏差**（NOTES 里登记）。
 */
export function resolveHostSpecs(): readonly HostSpec[] {
  const dshHome = envHome('DSH_HOME') || path.join(homedir(), '.dsh')
  const workbuddyHome = envHome('WORKBUDDY_HOME') || path.join(homedir(), '.workbuddy')
  return [
    {
      id: 'dsh',
      label: 'DSH（DeepSeek Harness）',
      home: dshHome,
      homeEnv: 'DSH_HOME',
      configPath: path.join(dshHome, 'hooks.json'),
      collectorScript: 'fish_push_hook.py',
      note: 'hooks 是启动读一次；matcher 非法会让**整份配置被拒**（四个事件全部不注册）',
    },
    {
      id: 'workbuddy',
      label: 'WorkBuddy',
      home: workbuddyHome,
      homeEnv: 'WORKBUDDY_HOME',
      configPath: path.join(workbuddyHome, 'settings.json'),
      collectorScript: 'fish_posttool_hook.py',
      note: '与别的插件**共用** settings.json：写入必须合并，不许整层替换',
    },
  ]
}

/** 按 id 取宿主（未知 id 抛错——静默返回空会让"没有这个宿主"变成"这个宿主没配置"）。 */
export function findHostSpec(id: string, specs: readonly HostSpec[] = resolveHostSpecs()): HostSpec {
  const spec = specs.find((item) => item.id === id)
  if (spec === undefined) {
    throw new Error(`未知宿主 ${JSON.stringify(id)}（可用：${specs.map((item) => item.id).join(' / ')}）`)
  }
  return spec
}
