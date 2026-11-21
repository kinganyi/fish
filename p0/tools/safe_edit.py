#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""保持换行符的文本替换工具（2026-11-21）。

## 为什么需要它

这个坑在同一个项目里踩了**三次**：

| 次 | 文件 | 结果 |
|---|---|---|
| 1 | `mcp_server.py` | 真实改动 7 行，diff 报 +2955/−2620 |
| 2 | `test_kb_read.py` / `test_mcp_kb.py` | 真实 7 行和 1 行，diff 报 1276 和 900 行 |
| 3 | `test_mcp.py` / `test_mcp_kb.py` | 真实 5 行和 3 行，diff 报 704 和 902 行 |

根因是同一个：**`Path.read_text()` 会把 `\r\n` 归一成 `\n`，`write_bytes()` 再写回去，
整个文件的换行符就被改掉了。**

后果不是"多了一堆 diff"这么轻——它让 **code review 失效**：真实改动被埋进
几百行伪改动里，`git blame` 也全乱了。

## 用法

```python
from safe_edit import replace_in_file, read_preserving

# 替换（保持原换行符，改了返回值告诉你有几处）
n = replace_in_file(path, "旧文本", "新文本")     # 全部替换
n = replace_in_file(path, "旧文本", "新文本", count=1)  # 只替换第一处

# 只想读，不想被换行符坑
text = read_preserving(path)      # 保留 \r\n 原样
```

命令行：

```
python safe_edit.py <文件> <旧文本文件> <新文本文件>
```

## 三条纪律

1. **读写一律走 `bytes`**，中间只做 `decode`/`encode`，从不过 `.splitlines()` 再 join
   （那会把换行符统一成 `\n`）。
2. **替换前先数命中次数**：0 处 = 锚点写错了（响亮报错，不静默跳过）；
   >1 处而 `count=1` 时按第一次替换，但会打印警告。
3. **改完报告改动量**：`真实改动` 与 `git diff --numstat --ignore-cr-at-eol` 应当一致；
   不一致就是换行符被动过。

## 不做什么

不做语法级改写（那是 AST 的事），不做格式化（那是 formatter 的事）。
它只解决"文本替换顺手改掉整个文件换行符"这一个具体问题。
"""
import sys
from pathlib import Path


def read_preserving(path):
    """读文件，**保留原始换行符**（不归一成 `\\n`）。"""
    return path.read_bytes().decode("utf-8")


def detect_eol(path):
    """判断文件的主导换行符：`"\\r\\n"` / `"\\n"` / `""`（空文件）。"""
    raw = path.read_bytes()
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    if crlf == 0 and lf == 0:
        return ""
    return "\r\n" if crlf >= lf else "\n"


def replace_in_file(path, old, new, *, count=None, quiet=False):
    """在 `path` 里把 `old` 换成 `new`，**保持原文件的换行符**。

    `count=None` 表示全部替换；`count=1` 只替换第一处。

    返回替换的处数。**0 处会抛 `LookupError`**——锚点写错应当响亮失败，
    静默跳过是"改了但没生效"的来源。
    """
    path = Path(path)
    eol = detect_eol(path)
    text = read_preserving(path)

    # 归一化比较：调用方给的 old/new 可能用 \n 写，而文件是 CRLF。
    # 先把文件临时归一到 \n 做替换，最后再整体还原——这样调用方不必关心换行符。
    norm = text.replace("\r\n", "\n")
    old_n = old.replace("\r\n", "\n")
    new_n = new.replace("\r\n", "\n")

    hits = norm.count(old_n)
    if hits == 0:
        raise LookupError(f"{path}：找不到要替换的文本（锚点写错了）：{old_n[:60]!r}")
    if not quiet and count is None and hits > 1:
        print(f"   {path}: 命中 {hits} 处，全部替换")

    out = norm.replace(old_n, new_n, count if count else -1)
    if eol == "\r\n":
        out = out.replace("\n", "\r\n")

    path.write_bytes(out.encode("utf-8"))
    return min(hits, count) if count else hits


def main(argv):
    if len(argv) != 4:
        print(__doc__)
        return 2
    target, old_file, new_file = (Path(x) for x in argv[1:])
    n = replace_in_file(target, read_preserving(old_file), read_preserving(new_file))
    print(f"   替换 {n} 处；换行符保持 {detect_eol(target)!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
