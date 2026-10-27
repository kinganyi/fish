#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异驱动的「换行无关」源码读写（2026-10-26 · F1）。

## 为什么需要它

本仓库工作树是 **CRLF**（`git config core.autocrlf=false`；`fish.py` 11359 个
CRLF、`mcp_server.py` 1838 个），而变异驱动的靶点片段里写的是 `'\\n'`。旧实现
`Path.read_bytes().decode("utf-8")` 不做换行归一 —— 靶点在 CRLF 树上命中 0 次，
整条变异被跳过、脚本以非零码退出（"功能正常但退出码 1"的同款假失败）。

## 三条纪律

1. **匹配在 LF 归一视图上做**：`SourceText.text` 永远是 LF，靶点照旧写 `'\\n'`；
2. **写回保持文件原本的风格**：不制造 CRLF/LF 混行（混行的 diff 会把真正的改动淹掉）；
3. **还原走原始字节**：`restore()` 把构造时读到的 `raw` 原样写回，逐字节一致——
   还原能力**不依赖**"归一化是否可逆"，即使文件里有混行也不受影响。

## 用法（每个变异体一条）

    src = SourceText(path)          # 读一次，记住原字节
    src.write(src.replaced(old, new))   # 命中必须恰好 1 次，否则抛 AssertionError
    try:
        ...跑判据...
    finally:
        src.restore()               # 逐字节还原
        assert src.restored()
"""
import hashlib
from pathlib import Path


def to_lf(text: str) -> str:
    """CRLF → LF（幂等）。靶点片段与源码都先过这一步再比较。"""
    return text.replace("\r\n", "\n")


def detect_newline(text: str) -> str:
    """判主换行风格：CRLF 数量多于"裸 LF"就是 CRLF 文件，否则 LF。

    只用来决定**写回**用哪种风格；判据（还原）永远走原始字节，不看这个值。
    """
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


class SourceText:
    """一个源文件：LF 归一视图 + 原字节句柄。"""

    def __init__(self, path):
        self.path = Path(path)
        self.raw = self.path.read_bytes()
        self.raw_text = self.raw.decode("utf-8")
        self.newline = detect_newline(self.raw_text)
        self.text = to_lf(self.raw_text)

    def sha256(self) -> str:
        """原字节的 sha256（基线/还原比对都用它）。"""
        return hashlib.sha256(self.raw).hexdigest()

    def count(self, snippet: str) -> int:
        """片段在 LF 归一视图里的命中次数（与文件是 CRLF 还是 LF 无关）。"""
        return self.text.count(to_lf(snippet))

    def replaced(self, old: str, new: str) -> str:
        """返回替换后的 LF 文本；命中次数不是恰好 1 次就抛错（绝不静默跳过）。"""
        hits = self.count(old)
        if hits != 1:
            raise AssertionError(
                f"{self.path.name} 里变异片段命中 {hits} 次（要求恰好 1 次）：{old[:60]!r}")
        return self.text.replace(to_lf(old), to_lf(new), 1)

    def write(self, text_lf: str) -> None:
        """按文件原本的换行风格写回（入参必须是 LF 文本）。"""
        body = text_lf if self.newline == "\n" else text_lf.replace("\n", "\r\n")
        self.path.write_bytes(body.encode("utf-8"))

    def restore(self) -> None:
        """把构造时读到的**原始字节**写回：逐字节还原。"""
        self.path.write_bytes(self.raw)

    def restored(self) -> bool:
        """当前磁盘内容是否与原字节完全一致。"""
        return self.path.read_bytes() == self.raw
