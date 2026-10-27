#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fish · 路径与「占用面」原语（纯函数，无 DB、无第三方依赖，可进 hook 快路径）。

## 为什么单独成模块（而不是留在 fish.py）

`fish.py` 是 8900+ 行、import 一次约 400ms。而 PreToolUse / PostToolUse 是
**每次工具调用**都要跑的：把「路径怎么归一」「两个面算不算撞」塞在 fish 里，
这两个 hook 就只能付 400ms 的启动成本，或者各写一份——历史上正是「各写一份」
让占用面判定漂过（`EVAL-261025-09`：声明了 `surface` 却没有任何一处做交集）。

本模块 import 成本 ~1ms，是 hooks 与 fish 共用的**唯一**一份原语：

  · `fish.py`   —— 从本模块 import 并原样再导出（`fish.surface_hit` 仍可用，
                   现有 24 处调用与既有测试一个都不用改）；
  · `fish_activity.py` —— 采集/快照/撞车判定直接用这里的函数；
  · hooks       —— 只 import 本模块 + `fish_activity`，不 import `fish`。

## 三条纪律（都是踩过的坑）

1. **不做 `realpath`/不解析符号链接**：只做词法归一。解析符号链接要访问磁盘，
   在 hook 快路径上是不可接受的成本，而且会引入「同一文件两个路径」的不确定性。
2. **按路径分量判前缀，不做字符串包含**：`a/b` 不该命中 `a/bc`（那是两条不同的路）。
3. **绝对 vs 相对是常态**：生产 `presence.surface` 里已有 `I:/app/...` 这样的绝对路径，
   只做「同基准前缀」比较会**静默漏判**（看着有判定，其实永远不命中）。
   `path_hit` 就是为这一条存在的：短的一侧是长的一侧的后缀、且短侧够深 → 命中；
   **目录形态的相对面**（`p0/data`）还要按分量链包含判（目标可能是它下面的文件），
   文件面保持后缀语义（评审 L2 的判据就钉在这里）。
"""
from __future__ import annotations

import posixpath
import re
from typing import Iterable

# 写类工具名（**小写归一后**比较）。DSH 实测工具名是小写 `write`/`edit`；
# WorkBuddy 侧是 `Write`/`Edit`——所以判定前必须先 `.lower()`，否则 matcher
# 匹配上了、代码判定却漏（那是「静默不记」里最难发现的一种）。
WRITE_TOOL_NAMES = ("write", "edit", "multiedit", "notebookedit", "apply_patch", "patch")
# Bash 方言兜底：DSH 叫 `bash`，别的宿主可能叫 `shell`。两者都按 exec 分类。
BASH_TOOL_NAMES = ("bash", "shell")

_SURFACE_SEP = re.compile(r"[,，;；\n]")
_DRIVE_ROOT = re.compile(r"[A-Za-z]:")
_MULTI_SLASH = re.compile(r"/+")


# ------------------------------------------------------------------ 路径归一


def normalize_path(path) -> str:
    """把路径归一成**内部唯一写法**：正斜杠 · 折叠重复斜杠 · 归一 `.`/`..` · 全小写。

    与 `fish.normalize_workspace_path` 同一套规则（那边是 workspace_map 的存法，
    这边是路径原语的存法——**规则相同，实现只有这一份**，`fish` 里那个函数保留
    原名以保护既有调用与测试，语义与这里一致）。

    盘符根（`C:` → `c:/`）保留最小形态，不塌成空串：塌了之后"谁是根"就丢了。
    UNC（`\\\\server\\share`）被压成 `/server/share`：两侧用同一条归一，匹配不受影响。

    空值/纯空白返回空串：由调用方决定它是"写入错误"还是"这次没给"。
    """
    if path is None:
        return ""
    raw = str(path).strip()
    if not raw:
        return ""
    s = _MULTI_SLASH.sub("/", raw.replace("\\", "/"))
    # posixpath 而不是 os.path：Windows 上后者会把 `/` 转回 `\`，让上面的归一白做。
    s = posixpath.normpath(s)
    while len(s) > 1 and s.endswith("/"):
        s = s[:-1]
    # 顺序不能反：先补盘符根的斜杠、再判尾斜杠，否则补上的 `/` 会被砍掉。
    if _DRIVE_ROOT.fullmatch(s):
        s += "/"
    return s.lower()


def path_segments(path) -> tuple:
    """路径的**分量**元组（丢掉空段）；`a/b/c` → `('a','b','c')`。"""
    norm = normalize_path(path)
    if not norm:
        return ()
    return tuple(p for p in norm.split("/") if p)


def make_abs(path, base=None) -> str:
    """把（可能相对的）路径解析成**归一化绝对路径**；相对路径按 `base` 解析。

    为什么需要它：采集落库的 `path_abs` 必须跨会话可比（不同会话的 cwd 不同，
    相对路径直接比会得出"两个不相干的人在同一块"或"同一个人在两块"的错结论）。
    `base` 为空时原样归一（**不猜 cwd**：hook 的进程 cwd 不是 AI 的工作目录）。
    """
    norm = normalize_path(path)
    if not norm:
        return ""
    if is_absolute(norm):
        return norm
    base_norm = normalize_path(base)
    if not base_norm:
        return norm
    return normalize_path(base_norm.rstrip("/") + "/" + norm)


def is_absolute(path) -> bool:
    """路径是不是绝对路径（`/x`、`c:/x`、`//server/share`）；先归一再看形态。"""
    norm_path = normalize_path(path)
    if not norm_path:
        return False
    if norm_path.startswith("/"):
        return True
    return bool(_DRIVE_ROOT.fullmatch(norm_path[:2]))


def relativize(path, base) -> str:
    """`path` 在 `base` 之下 → 相对路径；否则返回 `normalize_path(path)`。

    展示用：看板里出现一屏 `i:/app/...` 就没人看了，所以人能看的列走相对路径；
    机器判定（撞车）走 `path_abs`。**两者分开存**，不互相冒充。
    """
    p = normalize_path(path)
    b = normalize_path(base)
    if not p:
        return ""
    if not b:
        return p
    if p == b:
        return "."
    prefix = b.rstrip("/") + "/"
    if p.startswith(prefix):
        return p[len(prefix):]
    return p


def dir_of(path):
    """路径的**目录部分**（聚合键）；文件直接位于根时返回 `.`，绝不返回空串。

    为什么是 `.` 而不是空串：`path_dir` 是 30 分钟聚合的分组键，
    空串会让"根目录下的写"与"解析失败的写"混成一组——两种完全不同的事。
    """
    norm = normalize_path(path)
    if not norm:
        return None
    idx = norm.rfind("/")
    if idx < 0:
        return "."
    head = norm[:idx]
    if not head:
        return "/"
    if _DRIVE_ROOT.fullmatch(head):
        return head + "/"
    return head


def is_write_tool(name) -> bool:
    """是不是写类工具（大小写不敏感；DSH 小写、WorkBuddy 首字母大写都能过）。"""
    return (name or "").strip().lower() in WRITE_TOOL_NAMES


def is_bash_tool(name) -> bool:
    """是不是命令执行类工具（`bash` / `shell`）。"""
    return (name or "").strip().lower() in BASH_TOOL_NAMES


# ------------------------------------------------------------------ 占用面


def normalize_surface(surface) -> list:
    """把 `surface` 归一化成**排序去重的前缀列表**。

    规则（`EVAL-261025-09` 建议 3）：小写、正斜杠、去首尾空白、去尾斜杠、去重排序。
    为什么必须归一化：`p0/fish.py` 与 `p0/fish.py `（尾空格）此前算两回事，
    而两侧一旦漂了无人发现——那正是"声明了但防不住"的最隐蔽形态。
    """
    out = []
    for part in _SURFACE_SEP.split(str(surface or "")):
        s = (part or "").strip().replace("\\", "/").lower().rstrip("/")
        if not s:
            continue
        s = _MULTI_SLASH.sub("/", s)
        if s not in out:
            out.append(s)
    return sorted(out)


def surface_hit(a, b):
    """两个归一化前缀是否**命中**（含目录前缀语义），命中返回共同前缀。

    为什么不是简单相等（`EVAL-261025-09` 建议 4）：
    声明 `backend/routes` 应能命中 `backend/routes/tasks.py`——要求调用方列全所有
    文件是做不到的，而漏掉的恰好是"我以为没人在动"的那块。

    边界纪律：按**路径分量**判前缀，不做字符串前缀——
    `a/b` 不该命中 `a/bc`（那是两条不同的路，字符串前缀会误报）。
    """
    if not a or not b:
        return None
    if a == b:
        return a
    if b.startswith(a + "/"):
        return a
    if a.startswith(b + "/"):
        return b
    return None


def looks_like_file(path) -> bool:
    """这条归一化路径**形态上像不像一个文件**（最后一段含点）。

    为什么需要这条判别（评审 L2）：`path_hit` 对**目录面**要做"分量链包含"匹配
    ——目录里更深的目标（`p0/data` ↔ `…/p0/data/x.db`）当然算撞车；而**文件面**
    只能做后缀匹配（一个具体文件不可能"包含"另一个文件）。

    判据只认"**最后一段**含点"（`fish.py` / `x.db` / `.gitignore`），**不做磁盘访问**
    （hook 快路径不许 stat，见模块头三条纪律第 1 条）。

    代价（评审 F7 更正举例）：**最后一段**带点的目录会被当成文件面，例如
    `data/v1.2`、`releases/v2.1`——它们的最后一段是版本号目录，却被判成文件，
    于是退化成"只做后缀匹配"，命不中它下面的目标（**漏判不误判**，与
    `min_segments` 的取向一致）。反过来 `v1.2/data` **不受影响**：它最后一段是
    `data`、不含点，仍按目录面走分量链包含（旧注释把这个例子写反了，实测
    `looks_like_file('v1.2/data') is False`）。
    """
    norm = normalize_path(path)
    if not norm:
        return False
    return "." in norm.rsplit("/", 1)[-1]


def _contains_run(chain, run) -> bool:
    """`run` 是否作为**连续的一段分量**出现在 `chain` 里（分量边界，不是字符串包含）。

    `('p0','data')` 在 `('i:','app','p0','data','x.db')` 里 → True；
    `('src','a')` 在 `('i:','x','src','ab','y')` 里 → False（`a` ≠ `ab`）。
    """
    m = len(run)
    n = len(chain)
    if m <= 0 or n < m:
        return False
    for start in range(0, n - m + 1):
        if chain[start:start + m] == run:
            return True
    return False


def path_hit(surface, target, *, min_segments: int = 2):
    """绝对/相对混用时的**保守命中**；命中返回较短的那一侧（给人看的共同前缀）。

    四步（顺序即优先级）：

      1. 先走 `surface_hit` —— 两侧同基准（都是相对、或都是绝对）时它就够了
         （绝对目录面命中它下面的任何目标，就靠这一步）；
      2. **目录形态的相对面**走"分量链包含"：`p0/data` 命中
         `i:/app/x/p0/data/x.db`（目录里更深的目标也是同一块）；
         `src/a` **不**命中 `i:/app/x/src/ab/y`（分量边界，`a` ≠ `ab`）；
         1 段面被 `min_segments` 挡住（`p0` 到处同名）；
      3. 再判「短的一侧是长的一侧的后缀，**且短侧至少 `min_segments` 段**」——
         `p0/fish.py`（2 段）命中 `i:/app/x/p0/fish.py`；`data`（1 段）不命中任何东西；
      4. 只按 `/` 分量边界比较，**不做字符串包含**（`fish.py` 不命中 `myfish.py`）。

    为什么第 2 步只给**相对目录**（评审 L2 的判据）：绝对目录的"目录里更深"由第 1 步
    `surface_hit` 已经覆盖；相对目录与绝对目标**不同基准**，第 1/3 步都只能比后缀，
    于是 `p0/data` 对 `…/p0/data/x.db` 永远不命中——那是"看着有判定、其实永远不命中"
    的静默漏判（`EVAL-261025-09` 那一类）。**文件面保持后缀语义**：一个文件不能
    "包含"别的路径，放开它只会制造假撞车。

    `min_segments=2` 是刻意保守：1 段的面（`p0`、`src`、`docs`）在真实机器上
    到处都是同名目录，用它判撞车必然误报，而**误报比漏报更伤**——警告一旦掺假
    就会被学会忽略（`EVAL-261025-13` 刚教过这一条）。

    ⚠️ 已知代价：相对面与绝对目标之间**无法证明基准**，所以 `p0/data` 也会命中
    另一个项目里的 `p0/data`。这是"相对 vs 绝对"的固有限制（设计 §11.1 R6），
    靠 `min_segments` + 只对写类 + 90/30 分钟窗口 + 冷却压住；更可靠的办法是
    调用方先按项目根把相对面转成绝对（`make_abs`），本函数只是兜底。
    """
    a = normalize_path(surface)
    b = normalize_path(target)
    if not a or not b:
        return None
    hit = surface_hit(a, b)
    if hit:
        return hit
    sa, sb = path_segments(a), path_segments(b)
    floor = max(int(min_segments), 1)
    if not is_absolute(a) and not looks_like_file(a) and len(sa) >= floor:
        if _contains_run(sb, sa):
            return a
    if len(sa) >= floor and len(sb) > len(sa) and sb[-len(sa):] == sa:
        return a
    if len(sb) >= floor and len(sa) > len(sb) and sa[-len(sb):] == sb:
        return b
    return None


def longest_prefix(path, pairs: Iterable):
    """按「最长前缀 + 路径分量边界」解析归属，返回 `(归一前缀, 值)`；无匹配返回 None。

    与 `fish.resolve_project_by_cwd` 同语义（含 tie-break）：
      · 同长度不可能有两条不同前缀（主键唯一），但仍给稳定次序——
        按值字典序，免得将来碰上脏数据时输出随查询顺序漂；
      · **没有匹配返回 None，绝不回退到"猜一个"**（R1 的病根就是猜）。

    `pairs` 是 `(前缀, 值)` 的可迭代对象（workspace_map 的 `(path_prefix, project)`）。
    """
    target = normalize_path(path)
    if not target:
        return None
    best = None                     # (长度, 值, 归一前缀)
    for key, value in pairs:
        norm = normalize_path(key)
        if not norm:
            continue
        if target == norm or target.startswith(norm + "/"):
            cand = (len(norm), value, norm)
            if best is None or cand[0] > best[0] or (cand[0] == best[0] and cand[1] < best[1]):
                best = cand
    return (best[2], best[1]) if best else None
