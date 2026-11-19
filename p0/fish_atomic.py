#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""原子写文件（F-P2-8，2026-11-18 外部审查）。

## 审查方指出的问题

> 正文/附件非原子写；事件无 body hash；崩溃/断电 → 半文件；坏 blob 不修复；无法校验

实测（2026-11-19）：`record_memory` 写正文是

    (root / rel).write_text("\\n".join(lines) + "\\n", encoding="utf-8")

**直接写**。进程在这行的中间被杀掉，盘上就留下半个 Markdown：
前半有、后半没有，而投影里那条记录看起来完全正常——**没有任何东西会告诉你它坏了**。

## 为什么必须临时文件 + 替换

`os.replace` 在同一个文件系统上是**原子的**（POSIX rename / Windows MoveFileEx）：
要么看到旧内容、要么看到新内容，没有"写了一半"这个状态。

三个细节，少一个就不算原子：

  1. **临时文件必须和目标是同一个目录**。跨盘 `os.replace` 会退化成复制+删除，
     又变回非原子。
  2. **必须 `flush` + `fsync`**。只写进 OS 缓存的话，断电后 os.replace 可能
     已经把名字换过去了、内容却还在缓存里没落盘——这叫"零长度文件"，
     比半个文件更难查。
  3. **失败要清理临时文件**。不然崩一次就在目录里留一个 `.tmp`，
     跑多了会攒出一堆看着像正文的垃圾。

## 边界

本模块**不处理**并发写同一个文件（那是另一件事，靠调用方的锁）。
它保证的是：**任何时刻打开这个路径，要么是完整旧内容，要么是完整新内容。**
"""

import io
import os
import tempfile
from pathlib import Path

# 临时文件后缀。选 `.tmp` 是为了让"正文目录里出现 .tmp"成为一个显眼的信号。
TMP_SUFFIX = ".tmp"


def write_text_atomic(path, text, *, encoding="utf-8", newline="") -> Path:
    """把 `text` 原子地写到 `path`。返回写入的路径。

    `newline=""` 与 `Path.write_text` 的默认行为对齐：**不做换行转换**，
    所以逐字节等价于原来那次 `write_text`——本函数只改"怎么写"，不改"写什么"。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # ① 同目录临时文件：跨目录会让 os.replace 退化成复制+删除
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", suffix=TMP_SUFFIX,
                               dir=str(target.parent))
    try:
        with io.open(fd, "w", encoding=encoding, newline=newline) as fh:
            fh.write(text)
            fh.flush()
            # ② fsync：没有它，断电会留下"名字换了、内容没落盘"的零长度文件
            os.fsync(fh.fileno())
        # ③ 原子替换（同目录 ⇒ 真原子）
        os.replace(tmp, target)
        tmp = None
        return target
    finally:
        # ④ 失败也要收尾，别在正文目录里留 .tmp
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:      # A档：临时文件已经没了就没得删，不影响主流程
                pass


def sha256_of_text(text, *, encoding="utf-8") -> str:
    """正文的哈希——写进事件里，让"正文被改过"变成可判定。"""
    import hashlib
    return hashlib.sha256(text.encode(encoding)).hexdigest()
