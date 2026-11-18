#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""附件入库：内容寻址 + 去重 + **四道安全校验**（F-P1-3，2026-11-18 外部审查）。

## 这个模块修的是什么

审查方指出附件入口四件事全没做。逐条对照原实现：

```python
data = src.read_bytes()          # ① 没有大小上限：10 GB 的文件 = 一次 OOM
h = hashlib.sha256(data).hexdigest()   # ④ 不是流式：整份进内存
ext = src.suffix.lower()
bpath.write_bytes(data)          # 写一半断电 → 目录里留一个 sha256 对不上的 blob
```

② 没有敏感名单：`--attach ~/.ssh/id_rsa` 把私钥按内容寻址复制进 `blobs/`。
   **内容寻址意味着改名也挡不住**——sha256 一样就还是同一份。
③ 没有允许根：机器上任何可读路径都能被吸进来，包括系统目录。

## 依赖方向

`fish_common` ← `fish_attach` ← `fish`。只从 `fish_common` 取 `get_config`，
不认识记忆、身份、注入——所以它可以被单独测。
"""
import fnmatch
import hashlib
import os
from pathlib import Path

from fish_common import get_config


# ---------------------------------------------------------------- 附件安全
# F-P1-3（2026-11-18 外部审查）：附件入口原本四件事全没做——
#   ① 没有大小上限：`src.read_bytes()` 把整个文件读进内存，`--attach` 一个
#      10 GB 的文件就是一次 OOM，而调用方看到的是"进程被杀"。
#   ② 没有敏感名单：`--attach ~/.ssh/id_rsa` 会把私钥按内容寻址复制进 `blobs/`。
#      内容寻址意味着**改名也挡不住**——sha256 一样就还是同一份。
#   ③ 没有允许根：机器上任何可读路径都能被吸进来，包括系统目录。
#   ④ 不是流式：先整份读、再一次写，中间没有 temp→rename，写一半断电就留半个文件
#      （内容寻址的目录里出现一个 sha256 对不上的 blob，比没有更坏）。
ATTACH_MAX_BYTES_KEY = "attach.max_mb"
ATTACH_MAX_MB_DEFAULT = 64

# 附件专用的敏感名单：在 KB 那套（凭证/密钥/密码）之上补**附件场景**才会遇到的
# 形态。为什么不能只复用 `KB_SENSITIVE_PATTERNS`：那套是为"知识库笔记"设计的，
# 里面刻意不加 `*token*`（会打到 tokenize 这类正常笔记）；但附件是**原样复制**，
# 一个叫 `tokens.txt` 的文件里放的就是真 token。
ATTACH_SENSITIVE_PATTERNS = (
    "*token*", "*credential*", "*secret*", "*password*", "*passwd*",
    "*.pem", "*.key", "*.pfx", "*.p12", "*.jks", "*.keystore",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", ".env", ".env.*",
    "*凭证*", "*密码*", "*密钥*",
)

# 系统目录：吸进附件没有正当用途，但一旦发生就是把系统文件复制进了记忆库
ATTACH_DENY_ROOTS = ("c:\\windows", "c:\\program files", "c:\\program files (x86)",
                     "c:\\programdata")


class AttachmentRejected(ValueError):
    """附件被安全策略拒绝。**单独一个类型**：调用方要能把它和"文件不存在"区分开。"""

    def __init__(self, code, message, hint=""):
        super().__init__(message)
        self.code = code
        self.hint = hint


def _attach_max_bytes(conn=None) -> int:
    """上限从配置读（`attach.max_mb`），读不到用默认。

    为什么允许配置：不同人贴的东西差别很大（截图几百 KB，视频可能 200 MB）。
    写死一个数会逼人改代码，那比"有一个偏小的上限"更糟。
    """
    mb = ATTACH_MAX_MB_DEFAULT
    if conn is not None:
        try:
            got = get_config(conn, ATTACH_MAX_BYTES_KEY)
            if got is not None:
                mb = int(float(got))
        except (TypeError, ValueError, Exception):  # A档：读不到就用默认，不因配置坏了拒绝附件
            mb = ATTACH_MAX_MB_DEFAULT
    return max(1, mb) * 1024 * 1024


def _attach_check_name(src: Path) -> None:
    """文件名命中敏感名单 → 拒绝。**不做静默脱敏**：脱敏后的附件是坏附件。"""
    import fnmatch
    name = src.name.lower()
    for pat in ATTACH_SENSITIVE_PATTERNS:
        if fnmatch.fnmatch(name, pat.lower()):
            raise AttachmentRejected(
                "attach_sensitive",
                f"附件 `{src.name}` 命中敏感名单（{pat}），拒绝入库。",
                hint="确实要存就把它改成一个不含凭证的副本，别把原件塞进记忆库")


def _attach_check_root(src: Path) -> None:
    """系统目录下的文件一律拒绝——吸进附件没有正当用途。"""
    try:
        real = str(src.resolve()).lower()
    except OSError:
        return
    for deny in ATTACH_DENY_ROOTS:
        if real.startswith(deny):
            raise AttachmentRejected(
                "attach_denied_root",
                f"附件 `{src}` 在系统目录下，拒绝入库。",
                hint="把要存的文件复制到工作目录再 attach")


def validate_attachment(src, *, conn=None, allow_sensitive=False):
    """**只校验，不写任何东西**（F-P1-X / 独立评审 🟠-6）。

    ## 为什么要把校验单独拆出来

    `store_blob` 原来把"校验 + 落盘"做在一起，而 `record_memory` 在它**之前**
    已经 `next_code()` 分配了编码。于是：一条被拒的附件 → 抛异常 → 编码**已经烧掉**，
    下一条记忆从 -02 开始。本文件上级（fish.py）自己写着「编码一经分配不可回收」，
    这条路径正好违反它。

    拆开之后：`validate_attachment` 在 `next_code` **之前**跑，`store_blob` 在之后。
    被拒的附件一个编码都不烧；而落盘仍然发生在分配之后，
    不会因为"先写了 blob 再失败"留下更多孤儿文件。
    """
    src = Path(src)
    if not src.is_file():
        raise AttachmentRejected("attach_not_file",
                                 f"附件不是普通文件：{src}",
                                 hint="目录不能当附件；先打包成一个文件")
    if not allow_sensitive:
        _attach_check_name(src)
        _attach_check_root(src)
    size = src.stat().st_size
    cap = _attach_max_bytes(conn)
    if size > cap:
        raise AttachmentRejected(
            "attach_too_large",
            f"附件 {size / 1024 / 1024:.1f} MB 超过上限 {cap / 1024 / 1024:.0f} MB：{src.name}",
            hint=f"提高上限：fish config set {ATTACH_MAX_BYTES_KEY} <MB>；"
                 "或先把文件压小")
    return size


def store_blob(root: Path, src: Path, *, conn=None, allow_sensitive=False) -> dict:
    """把文件按内容寻址存进 blobs/，重复内容只存一份。返回附件元数据。

    安全策略（F-P1-3）：大小上限 / 敏感名单 / 系统目录 / 流式哈希 + 原子落盘。
    四者都在**读第一个字节之前**判定——先把 10 GB 读进内存再检查就晚了。
    """
    src = Path(src)
    # 校验与 store_blob 共用一份（`record_memory` 会在 next_code **之前**
    # 先单独调 validate_attachment，所以这里再跑一遍是幂等的、也必须跑：
    # 直接调 store_blob 的其它调用方不该绕过校验）。
    size = validate_attachment(src, conn=conn, allow_sensitive=allow_sensitive)

    ext = src.suffix.lower()
    # 流式：分块读 → 边算 sha256 边写 temp。**不整份进内存**。
    h = hashlib.sha256()
    tmp_dir = root / "blobs" / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"{os.getpid()}-{id(src)}.part"
    try:
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            while True:
                chunk = fin.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
                fout.write(chunk)
        digest = h.hexdigest()
        bdir = root / "blobs" / digest[:2]
        bdir.mkdir(parents=True, exist_ok=True)
        bpath = bdir / f"{digest}{ext}"
        if bpath.exists():
            tmp.unlink()               # 去重：同一内容只存一份
        else:
            os.replace(tmp, bpath)     # 原子：要么没有，要么是完整的
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass  # A档：清理失败不能掩盖原始异常
        raise
    return {"name": src.name, "blob": f"blobs/{digest[:2]}/{digest}{ext}",
            "size": size, "sha256": digest, "mime": ext.lstrip(".")}
