#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fish · 推送词汇与采纳率口径（叶子模块：零 fish import，无第三方依赖）。

## 为什么单独成文件（112 W-B 的落点选择）

112 给的是"新建 `fish_push_vocab.py`（零 fish import）**或**放进 `fish_common`"。
原计划放进 `fish_common`（它已经是共享原语的家），但落地时撞上一条**硬契约**：

    `test_split_contract.FISH_COMMON_LINES_MAX = 970`

`fish_common` 在改动前是 966 行——只余 4 行余量。W-B + W-D 的两次"往下沉"
合计 +99 行会让它变成 1065 行，把「`fish_common` 不许长成第二个巨石」这条
守卫判据直接打红。窗口是**验收契约**，不是可以顺手调大的数字（112 的纪律：
不许调阈值）。所以选择新建叶子模块——代价是多一个文件，收益是这条契约继续有效。

## 这个文件里有什么

  · `PUSH_CHANNELS` —— `push_log.channel` 的**唯一真相**（顺序即 UI 显示顺序）；
  · `LEGACY_PUSH_CHANNELS` —— 两个历史词，**只做写入兼容与读端识别**；
  · `PUSH_CHANNELS_WRITE` —— 读端筛选允许的集合（= 实际三词 + 历史词）；
  · `accept_rate` —— 推送采纳率的**唯一实现**（三处读口都调它）。

## 为什么只有三个实际通道（112 §三 A1 的根因）

词表曾经在五处声明、给出三种答案。根因不是笔误，是**两个维度被并进了一个词表**：

  · `hook`        → `session_context.channel` 的值（`cli/hook/parent`，另一张表）；
  · `situational` → 候选 **kind** 的值（`fish_push.py` 的 kind 字段）。

生产库 6,326 行实测只有 `inject` / `prompt` / `tool_after`，写端四个调用点也只写
这三个。所以 UI 词表用这三个；历史词保留写入兼容（删掉它们不会报错，只会让库里的
历史行/外部调用方被新校验挡回去），读端聚合会把它们追加成额外行并标 `legacy=True`。

## 依赖纪律

本文件**只许有字面量与纯函数**：它被 hook 链（`fish_activity`）与读口
（`fish_push_audit` / `fish_workbench_api`）共用，import 成本必须接近 0。
"""
from __future__ import annotations

#: 推送渠道的**唯一真相**（`push_log.channel`）；顺序即 UI 的显示顺序。
PUSH_CHANNELS = ("inject", "prompt", "tool_after")

#: 历史词（各自属于另一个维度，见文件头）；**只做写入兼容与读端识别**，不进 UI 词表。
LEGACY_PUSH_CHANNELS = ("hook", "situational")

#: 读端筛选允许的值 = 实际三词 + 历史词。旧故障是"写端接受五词、读端只认三词"——
#: 写进去了却筛不出来（112 §三 A1）。写端另有一份**每次现算**的集合
#: （`fish._push_write_channels`）：那份要能被测试临时 patch 掉一个词。
PUSH_CHANNELS_WRITE = PUSH_CHANNELS + LEGACY_PUSH_CHANNELS


def accept_rate(accepted, ignored):
    """推送采纳率 = `accepted / (accepted + ignored)`；没有已判 → `None`（不是 0）。

    唯一实现（112 N-3 之前三份：`fish.push_stats` 内联两处、
    `fish_push_audit._rate`、`fish_workbench_api.collect_stats` 的推送 Top）。
    第三处的注释自己写着"与 push_stats 同一分母"——**用文字保证同步就是没保证**。

    **分母口径不许改**：已判 = accepted + ignored（pending 不进分母）。
    空数据回 `None` 不是 0：0 表示"推了但没人理"，None 表示"还没推过"，
    混用会让"还没开始推"看起来像"推了没用"。
    """
    accepted = int(accepted or 0)
    ignored = int(ignored or 0)
    decided = accepted + ignored
    return (accepted / decided) if decided else None
