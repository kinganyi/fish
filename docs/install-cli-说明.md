# 安装 `fish` CLI 说明（只写步骤，**不执行**）

> 裁决 **Q17**：本批**不动系统 PATH**、不做机器级动作。
> 本文是给"以后想在任何目录直接敲 `fish`"的人看的步骤清单；照着做之前请先自己确认，
> 本仓库的测试与判据**不依赖**本页任何一步（全部命令都能用 `python p0/fish.py ...` 跑）。
> as-of 2026-10-26（`fish-devA` · `dev/fish-devA`）。

## 0. 先确认现在就能用（不改任何系统设置）

```powershell
cd <仓库根>\p0
python fish.py --help
```

能出帮助就说明入口没问题，只是"要从任意目录敲 `fish`"才需要下面的步骤。

## 1. 准备一个稳定的入口路径

目标：一条**绝对路径**能在任何工作目录下被调用。两种都可，选一种：

- 方案 A（零文件）：直接用 `python <仓库根>\p0\fish.py`
- 方案 B（推荐）：建一个包装脚本 `fish.cmd`，内容一行：

```bat
@echo off
python "I:\app\DSH-Home\workspace\learn\fish-devA\p0\fish.py" %*
```

把 `fish.cmd` 放在一个**你自己**管理的目录里（例如 `%USERPROFILE%\bin`）。

## 2. 把那个目录加进用户 PATH（可选，机器级动作）

1. 打开「系统属性 → 高级 → 环境变量」；
2. 在**用户变量**里选 `Path` → 编辑 → 新建 → 填入第 1 步的目录；
3. 确定后**重开**终端（旧终端不会拿到新 PATH）；
4. 验证：`where fish` 应输出你那个目录下的 `fish.cmd`。

> ⚠️ 三条注意：
> - 只改**用户变量**，别改系统变量（影响全机所有账号）；
> - 加 PATH 之前先把 `fish.cmd` 建好，否则 PATH 里多一条指向空目录的项；
> - 若机器上已有别的 `fish` 命令（同名），用 `where fish` 看谁在前面，必要时用完整路径。

## 3. 验证（在任意工作目录）

```powershell
fish --root "I:\app\DSH-Home\workspace\learn\fish-devA\p0\data" healthcheck
```

本批（B1/B2/B3）新增的两族命令：

```powershell
fish --root <data_root> layout render --all --as-of 2026-10-26T00:00:00+08:00
fish --root <data_root> layout verify
```

## 4. 撤销

从用户 PATH 里删掉第 2 步加的那一行即可；`fish.cmd` 可留可删（它不含任何状态）。
本 CLI 不写注册表、不装服务、不改文件关联。
