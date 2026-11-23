#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fish · 宿主**词表**（叶子模块：只写字面量，**零 fish import**）。

## 为什么单独一个文件（设计 106 §1/§2）

「宿主的词」和「某台宿主的事实」是两件事：

  · **词**（会话键前缀、前缀别名、语料能采的白名单）→ 本文件；
  · **事实**（装没装、配置在哪、版本号、hook 事件）→ `fish_hosts.HOSTS`。

把它们混在一起的代价是**依赖方向反转**：`fish_hosts` 要 import 词表，所以词表
绝不能反过来 import 任何鱼模块——否则闭环，且会把 `fish_hosts` 的 242–627 ms
（`fish_ui` + `http.server` + `dataclasses`）拖进 PostToolUse 热路径。
`fish_activity.py` 的边界是「hook 每次工具调用都跑、不许 import fish 的 400 ms」，
所以本文件**只许有字面量**：无函数、无 import、无副作用，import σ ≈ 1–3 ms。

## 三条字面量，各是一条「对外世界已有事实的登记」

1. `PROTOCOL_VOCABULARY` —— 会话键前缀词表（**有序**，顺序就是对外契约）。
   实测库里 `tool_activity.session_key` 的前缀只有 `codebuddy`（26,200 行）与
   `dsh`（861 行），但**历史契约**包含 `workbuddy` / `cli` / `self`：删掉它不会报错，
   只会让老会话键**静默失效**（`split_session_key` 返回 `("", "")`）。
   所以它是「已经写进这个世界的事实」，**不是注册表的函数**——若按 `HOSTS` 推导，
   注册表少一条 spec 时 `consistency()` 就再也发现不了「库里有个没人认识的宿主」
   （设计 §1 三点口径之一，已被变异实测证明判据会失去判别力）。

2. `PREFIX_OVERRIDES` —— 前缀**唯一**的非恒等映射。WorkBuddy 的会话键前缀是
   `codebuddy`（历史原因：它和 CodeBuddy 同一族产品），所以 `workbuddy → codebuddy`；
   其余前缀都是恒等映射，不在这里重复登记。

3. `CORPUS_ADAPTER_HOSTS` —— 语料层**实际实现**了适配器的宿主白名单。
   **不是**「谁应该被采集」的产品口径（那是 `fish_corpus.DEFAULT_HOSTS` 的调用方语义），
   而是「哪个宿主真有实现类」。`fish_corpus` 在 import 期拿它与实现表**硬对拍**：
   白名单多写一条而没实现 → 导入即失败，不许静默少一行。

## 谁消费它

  · `fish_naming.SESSION_HOSTS`（派生，名字保留，逐字同序 → 护住 `verify_hosts` ④ 的 tuple 相等）
  · `fish_activity.HOST_KEY_PREFIX`（派生；新增的两键是恒等映射，与兜底「取原值」行为等价）
  · `fish_hosts`（`PROTOCOLS` / `consistency()` / 安装器渲染计划）
  · `fish_corpus` / `fish_corpus_tag`（白名单对拍）

**改动纪律**：这里改一个字，四个消费方一起变——这正是本文件存在的理由。
"""
from __future__ import annotations

#: 会话键前缀词表（**有序**；顺序是 `verify_hosts` ④ 锁定的对外契约，不许调换）。
PROTOCOL_VOCABULARY = ("dsh", "codebuddy", "workbuddy", "cli", "self")

#: 前缀 → 会话键实际前缀的**非恒等**映射（唯一一条：WorkBuddy 走 codebuddy 前缀）。
PREFIX_OVERRIDES = {"workbuddy": "codebuddy"}

#: 语料层**有实现类**的宿主白名单（`fish_corpus` import 期与实现表硬对拍）。
CORPUS_ADAPTER_HOSTS = ("dsh", "workbuddy")
