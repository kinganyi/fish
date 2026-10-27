#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSH 适配器：`session.v4.jsonl.zstd` → 对话段（原始对话语料层 · T02 · 设计 §4.1）。

三个不能妥协的现场事实（都是 2026-10-27 只读取证的）
--------------------------------------------------
1. **一个文件是"很多个 zstd 帧"的串联**：实测单文件最多 2,773 帧、12.5 MB → 58 MB。
   所以游标只能记**帧边界字节**：从中间字节起读，`decompressobj` 只会报
   "Data corruption detected"（本轮实测：帧间截断 → `ZstdError`；尾帧未写完 →
   `eof=False` 且**不消耗字节**，两者语义不同，见 `iter_frames`）。
2. **`user/message` 不能照采**：`data.source.kind` 实测有 15 种，`kind='user'` 才是
   人/子代理真的说出口的话；其余 `time-context` / `runtime-context` / `skill-catalog`
   / `agent-instructions` / `goal` … 全是**注入块**（3 个大会话样本里 531 条 user 中
   只有 4 条是 `kind='user'`）。这就是 PRD §3.1 铁律的正确落法（D02）。
3. **`agent/inbox/spliced` 不入库**（D03）：它是既有消息的转投，实测与
   `user/message(seq=18)` 同 `data.id` 同文本——入库后同一句话会有两个门牌。

只读纪律
--------
本模块**只读**源日志（`open(path,'rb')`），从不写回。源日志里的 token 明文由
`fish_corpus_redact` 在落盘前兜住（PRD O7：不改源日志）。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import fish_corpus_redact as redact_mod
import fish_corpus_store as store
from fish_corpus_tag import TagError, Tagger

try:                                            # zstandard 是登记过的依赖（见依赖说明）
    import zstandard
except ImportError:                             # pragma: no cover —— 缺依赖时也要给人话
    zstandard = None

HOST = "dsh"
FRAME_NAME_V4 = "session.v4.jsonl.zstd"
FRAME_NAME_V3 = "session.v3.jsonl.zstd"
FRAME_NAME_LEGACY = "session.jsonl.zstd"
DEFAULT_CHUNK = 1 << 20                          # 压缩读单位（io 一次读多少）
# **有界内存的关键**：喂给解压器的一片。`decompressobj().decompress()` 不接受
# `max_length`（zstandard 0.25 实测），所以"一次喂多大"直接决定"一次最多解多大"——
# 小片喂，单次分配就只跟这一片的膨胀比有关，**不跟帧大小有关**。
DECOMPRESS_FEED = 64 << 10
DEFAULT_BATCH_FRAMES = 64                        # 每 64 帧提交一次（帧边界 = 提交边界）
# 单帧解压上限：默认 64 MiB（实测最大帧 152 KB，留 400 倍）。首采 538 会话里 10+ 个
# 帧被旧的 8 MiB 上限挡掉，所以这个数**必须可配**：配置键 `corpus.max_frame_bytes`，
# 或环境变量 `FISH_CORPUS_MAX_FRAME_BYTES` 临时覆盖（见 `resolve_max_frame_bytes`）。
DEFAULT_MAX_FRAME_BYTES = 64 << 20
ENV_MAX_FRAME_BYTES = "FISH_CORPUS_MAX_FRAME_BYTES"
CONFIG_KEY_MAX_FRAME_BYTES = "corpus.max_frame_bytes"
OFFSET_SPACE = "decompressed"                    # 偏移落在解压流上，不是压缩文件上

# v4 与 legacy（早期 `session.jsonl.zstd`，头部 `"version": 0`）**事件结构同构**：
# 2026-10-27 只读取证 17 个真实旧样本——事件类型名（`user/message` / `assistant/message`
# / `session/title` / `compaction/prune`…）、`seq`+`time`、`data.source.kind` 白名单语义
# 与 v4 完全一致，所以按同一套 `map_event` 解析，只在 `format` 上区分（`dsh.legacy`）。
# v3 仍未取证：继续登记降级（C-P0-9「总数静默少 1 即红」）。
SUPPORTED_FORMATS = (store.FORMAT_DSH_V4, store.FORMAT_DSH_LEGACY)
FORMAT_BY_NAME = {
    FRAME_NAME_V4: store.FORMAT_DSH_V4,
    FRAME_NAME_V3: store.FORMAT_DSH_V3,
    FRAME_NAME_LEGACY: store.FORMAT_DSH_LEGACY,
}

# 已知但**有意不入库**的事件类型（§4.1 表格）。列成集合而不是逐个 if：新增一种注入块时
# 必须先在这里登记，否则它会掉进 "unknown" 计数器里被人看见——这正是我们要的。
SKIP_EVENT_TYPES = frozenset((
    "system/message",        # 注入块，含 fish token 明文（F:38）
    "tool/call",             # 参数里有文件内容与命令（F:35-36）
    "tool/result",           # 结果正文 ≠ 对话
    "compaction/summary",    # 二次转述，语料层只认一手原文
    "step/start", "step/end", "turn/start", "turn/end",
    "workspace/changes", "session/meta", "session/heartbeat",
    # 2026-10-27（复审 🟡-14）：这五种是**流式碎片/回执**，不是独立事件。它们原先掉进
    # `unknown`，把 unknown 占比推到 >95%（实测），漂移信号被自己的噪声淹掉。
    "reasoning-chunks", "assistant/chunk", "tool-call-chunks", "text-chunks",
    "session-log-deepseek/delivery-accepted",
))

ATTACHMENT_TYPES = ("image", "file", "image_url", "file_attachment", "document")
TEXT_PART_TYPES = ("text", "input_text", "output_text")


class FrameError(RuntimeError):
    """一帧解不出来（帧间截断 / 坏描述符）。`reason_code` 直接进降级台账。"""

    def __init__(self, reason_code: str, message: str, offset: int = 0):
        super().__init__(message)
        self.reason_code = reason_code
        self.offset = offset


class AdapterError(RuntimeError):
    """适配器层面的失败（依赖缺失、目录不存在）。"""


class Frame:
    """一个完整帧：`text` 是解压出的 JSONL 文本，`consumed` 是它占的压缩字节数。"""

    __slots__ = ("text", "consumed", "end_offset", "complete")

    def __init__(self, text: bytes, consumed: int, end_offset: int, complete: bool = True):
        self.text = text
        self.consumed = consumed
        self.end_offset = end_offset
        self.complete = complete

    def __repr__(self) -> str:                  # 只为报错可读
        return "Frame(<%d B>, consumed=%d, complete=%s)" % (
            len(self.text), self.consumed, self.complete)


# ------------------------------------------------------------------ 发现


def discover(roots) -> list:
    """扫出所有会话文件（mtime 倒序：活跃会话先采，K5 的增量口径）。

    **目录名只用于定位文件，绝不用于判项目**（R1）：实测目录名形如
    `--I-app-DSH-Home-workspace-~5DE5~4F5C~6742~9879--`，那是路径的转义编码，
    拿它判项目等于把「工作区」当「项目」，而 PRD C-P0-3 红② 正是这一条。
    """
    if isinstance(roots, (str, os.PathLike)):
        roots = [roots]
    found = []
    for root in roots or ():
        base = Path(root)
        if not base.exists():
            continue
        for path in base.rglob("session*.jsonl.zstd"):
            try:
                st = path.stat()
            except OSError:
                continue                        # 读不到的具体降级由 ingest 记（这里不吞）
            name = path.name
            fmt = FORMAT_BY_NAME.get(name)
            if fmt is None:
                # 新版本改了文件名：按"路径里带 vN"兜底判格式，仍不认识就算 legacy。
                fmt = store.FORMAT_DSH_LEGACY
            found.append({
                "host": HOST,
                "path": str(path),
                "session_id": path.parent.name,
                "format": fmt,
                "size": int(st.st_size),
                "mtime_ns": int(st.st_mtime_ns),
            })
    found.sort(key=lambda item: item["mtime_ns"], reverse=True)
    return found


# ------------------------------------------------------------------ 帧解码


def _declared_frame_size(head: bytes) -> int:
    """帧头里**声明**的解压后字节数；没声明（流式写的帧）/读不出 → -1。

    实测 DSH 的帧都是流式写出来的（`frame_content_size` 恒为 UNKNOWN），所以这条
    预检在日常数据上等于空转；它的价值在"高压缩比炸弹"：单发压缩帧会把真实大小
    写进帧头，先看这里就不用把它解出来再发现（M6）。
    """
    if zstandard is None or len(head) < 6:
        return -1
    try:
        return int(zstandard.frame_content_size(head))
    except (zstandard.ZstdError, ValueError, TypeError):
        return -1


def resolve_max_frame_bytes(config_of=None, explicit=None) -> int:
    """单帧解压上限的取值顺序：**显式参数 > 环境变量 > 配置键 > 缺省 64 MiB**。

    `config_of(key)` 由门面注入（判据 J2：语料层不许 import 主库）。读配置失败、值不是
    正整数（手写成 `"64MB"`/`0`/负数）时一律退回缺省——**上限不该成为一次采集失败的
    理由**，但也不许悄悄变成 0（0 会被下游理解成"不限"）。
    """
    candidates = [explicit, os.environ.get(ENV_MAX_FRAME_BYTES)]
    if callable(config_of):
        try:
            candidates.append(config_of(CONFIG_KEY_MAX_FRAME_BYTES))
        except Exception:            # noqa: BLE001 —— 读不到配置就退回缺省，见 docstring
            candidates.append(None)
    for value in candidates:
        try:
            num = int(value)
        except (TypeError, ValueError):
            continue
        if num > 0:
            return num
    return DEFAULT_MAX_FRAME_BYTES


CHUNK_DATA = "chunk"            # 一块解压输出
CHUNK_FRAME_END = "frame_end"   # 一帧结束：(压缩字节数, 帧尾偏移)
CHUNK_TAIL = "tail"             # 尾帧没写完：已解出字节数（不消耗、不提交）


def iter_frame_chunks(path, offset: int = 0, chunk_size: int = DEFAULT_CHUNK,
                      max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES):
    """从 `offset`（**必须是帧边界**）开始逐帧解压，按块产出；内存**不随帧大小增长**。

    产出 `(kind, payload)`：
      · `("chunk", bytes)`        —— 一块解压输出（≤ `DECOMPRESS_FEED` 那一片的产出）
      · `("frame_end", (consumed, end_offset))` —— 一帧结束，`consumed` 是它占的压缩字节
      · `("tail", produced)`      —— 尾帧没写完（活跃会话正在追加）：**不消耗字节**，
        下次从同一位置重解。半帧的正文可能与下次拼出来的不一样，写进去就是"半条脏段"。

    帧间截断（中间那帧坏了）无法继续：`decompressobj` 会把后面那帧的字节当成坏数据，
    `ZstdError: Data corruption detected`。这时抛 `FrameError(truncated_frame)`。

    `max_frame_bytes`：单帧解压输出上限。超限抛 `FrameError(oversize_frame)`——
    **降级而不是吞掉**，但这个异常在**此前的块已经交出去之后**才抛：调用方据此把
    "已解出的段"先落库再登记降级（不能整帧丢，用户要的是真实原始对话）。
    两道上限：帧头声明的大小预检（单发压缩帧，连解都不解）+ 累计输出硬检查（流式帧）。
    """
    if zstandard is None:
        raise AdapterError("缺 zstandard 依赖：pip install zstandard（见 docs/依赖说明-corpus.md）")
    pos = int(offset)
    cap = int(max_frame_bytes or 0)
    feed = max(1, min(int(chunk_size), DECOMPRESS_FEED))
    decompressor = zstandard.ZstdDecompressor()
    with open(path, "rb") as fh:
        fh.seek(pos)
        while True:
            head = fh.read(chunk_size)
            if not head:
                return                                   # 干净 EOF
            declared = _declared_frame_size(head)
            if cap and declared > cap:
                raise FrameError(
                    store.REASON_OVERSIZE_FRAME,
                    "帧头声明解压后 %d B，超过上限 %d B（帧起点 %d）"
                    % (declared, cap, pos), pos)
            dobj = decompressor.decompressobj()
            fed = 0
            produced = 0
            at = 0                       # 当前缓冲里还没喂给解压器的位置
            while True:
                if at >= len(head):      # 这一块喂完了：读下一块
                    head = fh.read(chunk_size)
                    at = 0
                    if not head:
                        yield (CHUNK_TAIL, produced)
                        return
                piece = head[at:at + feed]
                at += len(piece)
                try:
                    out = dobj.decompress(piece)
                except zstandard.ZstdError as exc:
                    raise FrameError(store.REASON_TRUNCATED_FRAME,
                                     "zstd: %s（帧起点 %d）" % (exc, pos), pos) from exc
                fed += len(piece)
                produced += len(out)
                if cap and produced > cap:
                    raise FrameError(
                        store.REASON_OVERSIZE_FRAME,
                        "帧解压输出已超过上限 %d B（帧起点 %d；已交付 %d B，那些段会落库）"
                        % (cap, pos, produced - len(out)), pos)
                if out:
                    yield (CHUNK_DATA, out)
                if dobj.eof:
                    consumed = fed - len(dobj.unused_data)
                    if consumed <= 0:
                        raise FrameError(store.REASON_TRUNCATED_FRAME,
                                         "帧长度为 0（起点 %d）" % pos, pos)
                    pos += consumed
                    yield (CHUNK_FRAME_END, (consumed, pos))
                    fh.seek(pos)
                    break


def iter_frames(path, offset: int = 0, chunk_size: int = DEFAULT_CHUNK,
                max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES):
    """`iter_frame_chunks` 的整帧视图（**整帧进内存**，只给测试/小样本用）。

    `ingest_file` 走的是逐块接口（内存与帧大小无关）；这个包装留着是因为适配器对外
    仍以"帧"为单位（`DshAdapter.iter_frames`）。超限时同一帧的前几块被丢弃——
    要"先落库再降级"必须走 `iter_lines`。
    """
    parts = []
    for kind, payload in iter_frame_chunks(path, offset, chunk_size, max_frame_bytes):
        if kind == CHUNK_DATA:
            parts.append(payload)
            continue
        if kind == CHUNK_TAIL:
            yield Frame(b"".join(parts), 0, offset, False)
            return
        consumed, end_offset = payload
        yield Frame(b"".join(parts), consumed, end_offset, True)
        parts = []
        offset = end_offset


def iter_lines(path, offset: int = 0, chunk_size: int = DEFAULT_CHUNK,
               max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES):
    """逐帧解压 → **逐行**产出；`ingest_file` 的解析源（内存与帧大小无关）。

    产出 `(kind, payload)`：
      · `("line", (帧内解压偏移, 行字节))` —— 行的**第一个字节**在解压流里的偏移
      · `("frame_end", (压缩字节数, 帧尾偏移))`
      · `("tail", produced)` —— 半帧，不交半行

    行不跨帧拼接（半条脏段），但**帧尾没有换行的那一行照常交出**（与旧的 `split(b"\n")`
    口径一致：DSH 的帧总以换行结尾，所以这条只在坏帧上才走得到）。
    """
    buf = b""
    buf_pos = 0                     # `buf` 首字节在**当前帧解压流**里的偏移
    for kind, payload in iter_frame_chunks(path, offset, chunk_size, max_frame_bytes):
        if kind == CHUNK_DATA:
            data = buf + payload
            start = 0
            pos = buf_pos
            while True:
                nl = data.find(b"\n", start)
                if nl < 0:
                    break
                yield ("line", (pos, data[start:nl]))
                pos += nl - start + 1
                start = nl + 1
            buf = data[start:]
            buf_pos = pos
            continue
        if kind == CHUNK_TAIL:
            yield ("tail", payload)
            return
        if buf:
            yield ("line", (buf_pos, buf))
        buf, buf_pos = b"", 0
        yield ("frame_end", payload)


ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"      # 0xFD2FB528（小端）
ZSTD_SKIPPABLE = (b"\x50\x2a\x4d\x18", b"\x5f\x2a\x4d\x18")


def is_frame_boundary(path, offset: int, size: "int | None" = None) -> bool:
    """`offset` 是不是一个 zstd 帧的起点（**正好在文件尾也算合法**：那里没有帧）。

    为什么要这个检查：游标是"上次提交到的帧边界"，但如果源文件被**重写**（会话重放、
    日志修复），那个偏移就落在某帧中间——`decompressobj` 对"半帧"的判定与"文件尾"
    长得一模一样，于是**静默少采**，而且下一次也是静默。宁可归零重采（幂等 upsert），
    也不要安静地少一半对话。

    2026-10-27 复审 🟠-2 的两次修正（**两条方向相反，不能合并成 `>=`**）：
      · `offset == size`（已被消费到文件尾）**必须算边界**。否则源 mtime 一变（纯 touch）
        就把整份文件从 0 重读一遍——采不到新段，却把停滞哨兵喂成假报警；
      · `offset > size`（源被修剪得更短）**必须不算边界**。旧实现的 `offset >= size → True`
        在这里把"游标悬空"认成了"文件尾"：`iter_frames` 从越界位置读不到任何帧，于是
        `segments_new=0`、游标停在旧值，新内容**永远**读不到（实测源 668 B → 281 B，
        之后每一轮都是 0 新增）。
    `size` 可由调用方传（已 stat 过就不必再 stat 两次）。
    """
    if offset <= 0:
        return True
    try:
        if size is None:
            size = Path(path).stat().st_size
        offset, size = int(offset), int(size)
        if offset == size:
            return True                        # 正好在文件尾：没有帧可读，但边界成立
        if offset > size:
            return False                       # 源变短了：游标悬空 → 调用方归零重采
        with open(path, "rb") as fh:
            fh.seek(offset)
            magic = fh.read(4)
    except OSError:
        return False
    return magic == ZSTD_MAGIC or magic in ZSTD_SKIPPABLE


# ------------------------------------------------------------------ 事件映射


def _to_utc(value):
    """毫秒时间戳 → `2026-10-27T04:06:43Z`（全库统一 UTC `Z`，避免 8 小时隐形错）。

    拒绝"猜时区"：拿到秒级时间戳（10 位）时按秒处理，拿不到就返回 None，
    由调用方回退到会话创建时间——**不拿本地时间顶替**。
    """
    if value is None or value == "":
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num > 1e11:            # 毫秒（13 位）；1e11 秒 ≈ 公元 5138 年，不会误判
        num = num / 1000.0
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(num))


def _text_parts(content) -> tuple:
    """从 `content[]` 取文本 part 与附件计数；返回 `([str], {类型: 数量})`。

    只认 `text` 族：`reasoning` / `tool-call` 一律不取（PRD §3.1 铁律：
    实测 40 个会话 reasoning 4448 / text 1194 / tool-call 6062，照采会把噪音与
    隐私一起搬进来）。附件只留占位与计数（体积与隐私，W:33 同口径）。
    """
    texts = []
    attachments = {}
    if not isinstance(content, list):
        return texts, attachments
    for item in content:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind in TEXT_PART_TYPES:
            value = item.get("text")
            if value:
                texts.append(str(value))
        elif kind in ATTACHMENT_TYPES:
            attachments[kind] = attachments.get(kind, 0) + 1
    return texts, attachments


def _attach_placeholders(texts: list, attachments: dict) -> list:
    """把附件占位并进**最后一个**文本 part：不新增段，但读者看得到"这里有过 2 张图"。"""
    if not attachments or not texts:
        return texts
    tag = "〔附件:" + "+".join("%s×%d" % (k, v) for k, v in sorted(attachments.items())) + "〕"
    texts[-1] = (texts[-1] + "\n" + tag) if texts[-1] else tag
    return texts


def map_event(ev: dict):
    """事件 → `(kind, payload)`。kind∈{header,title,user,assistant,prune,skip,unknown}。

    `skip` 与 `unknown` 分开：前者是"我们**决定**不入库"（注入块/工具结果），
    后者是"我们不认识它"（格式漂移的第一信号）。两者都不让整文件失败，
    但统计口径不同——C-P0-1 红② 要的正是 unknown 只计数。
    """
    if not isinstance(ev, dict):
        return "unknown", None
    etype = ev.get("type") or ""
    data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
    if etype == "session":
        return "header", ev
    if etype == "session/title":
        return "title", data
    if etype == "user/message":
        source = data.get("source") if isinstance(data.get("source"), dict) else {}
        kind = source.get("kind") or ""
        if kind == "user":
            return "user", data
        return "skip", "user/message:%s" % (kind or "no-kind")
    if etype == "assistant/message":
        return "assistant", data
    if etype == "compaction/prune":
        return "prune", data
    if etype == "agent/inbox/spliced":
        return "skip", "agent/inbox/spliced"        # D03：既有消息的转投
    if etype in SKIP_EVENT_TYPES:
        return "skip", etype
    return "unknown", etype


def _header_of(ev: dict, path_session_id) -> dict:
    """`session` 事件 → 打标用的会话头。

    目录名与头部 id 理论上同一个值（实测 515/515），若不同则以**头部**为准
    （头部是会话自己的事实，目录名只是文件系统的路径），并记 `path_id_mismatch`。
    """
    sid = ev.get("id") or ev.get("sessionId") or ""
    origin = ev.get("origin")
    header = {
        "session_id": str(sid) if sid else str(path_session_id),
        "cwd": ev.get("cwd"),
        "title": None,
        "origin": origin,
        "parent_session": ev.get("parentSession"),
        "delegation_depth": ev.get("delegationDepth"),
        "agent_preset": ev.get("agentPreset"),
        "role": None,
        "created_at": _to_utc(ev.get("createdAt")),
        "extra": {"version": ev.get("version")},
    }
    if sid and str(sid) != str(path_session_id):
        header["path_id_mismatch"] = 1
    return header


def _safe_title(title):
    """会话标题写入前脱敏（S3）：标题里贴过 token 的现场是真的。

    为什么在适配器里做而不是等体检：`corpus_session.title` 会出现在 `search`/`read`
    的回执与推送行里，**它是可回显文本列**——脏标题从写进去那一刻就开始外泄。
    """
    if not title:
        return title
    text, _hits, _kinds = redact_mod.redact(str(title))
    return text


def _shadow_locs(data: dict) -> list:
    """`compaction/prune` → 被移出上下文的 `loc` 列表（`seq=<n>`）。

    实测结构 `{shadowedRange, shadowedSeqs, shadowedTokenCount}`。`shadowedRange`
    在样本里是 `[from,to]` 形态；两种都给时按并集处理（重叠不算错）。
    """
    locs = []
    seqs = data.get("shadowedSeqs") or data.get("shadowedSeqs".upper()) or []
    if isinstance(seqs, list):
        for seq in seqs:
            try:
                locs.append("seq=%d" % int(seq))
            except (TypeError, ValueError):
                continue
    rng = data.get("shadowedRange")
    if isinstance(rng, (list, tuple)) and len(rng) == 2:
        try:
            lo, hi = int(rng[0]), int(rng[1])
        except (TypeError, ValueError):
            lo, hi = 0, -1
        if 0 <= lo <= hi and hi - lo <= 100000:
            locs.extend("seq=%d" % n for n in range(lo, hi + 1))
    return locs


# ------------------------------------------------------------------ 单文件采集


def _same_file_as_cursor(stat_info: dict, cursor: dict) -> bool:
    """`size` 与 `mtime_ns` 都没变才算"没新字节"。

    为什么两者都要比（M21）：只看 size 会漏掉"同样大小但内容被改写"（会话被修剪、
    重放），只看 mtime 会在某些文件系统精度下反复重采同一份。
    """
    return (int(stat_info["size"]) == int(cursor.get("bytes_total") or 0)
            and int(stat_info["mtime_ns"]) == int(cursor.get("mtime_ns") or 0))


def ingest_file(conn, path, cursor=None, *, tagger=None, dry_run: bool = False,
                batch_frames: int = DEFAULT_BATCH_FRAMES,
                chunk_size: int = DEFAULT_CHUNK,
                max_frame_bytes: "int | None" = None,
                block_sessions=None, session_id_override=None,
                allow_restart: bool = True) -> dict:
    """采一个会话文件。**逐块解压、逐行解析、按批提交**，游标只前进到已提交的帧边界。

    返回计数器（`segments_new/segments_updated/skipped/unknown/bytes_consumed/...`），
    降级不抛异常而是写台账（C-P0-1 红①：坏帧要**列名**出现，且不写半条脏段）。

    `max_frame_bytes`：单帧解压上限。`None` = 按 `resolve_max_frame_bytes` 的三级
    取值（显式 > 环境变量 > 配置键 > 缺省 64 MiB），所以**不传也是可配的**。

    `session_id_override`（R1）：目录名与头部 id 不一致时，第二轮起直接按**真 id**
    采（映射落在 `corpus_meta`），避免每轮都重新发现一次不一致。
    `allow_restart`（R1）：只允许**一次**重启。旧实现在这里无条件重入，
    目录名 ≠ 头部 id 的文件会一直"重取 sid → 又不一致 → 再重取"，实测栈溢出。
    """
    path = Path(path)
    sid = str(session_id_override or path.parent.name)
    counters = {
        "host": HOST, "path": str(path), "session_id": sid, "format": store.FORMAT_DSH_V4,
        "status": store.SOURCE_OK, "frames": 0, "events_seen": 0, "segments_new": 0,
        "segments_updated": 0, "segments_skipped": 0, "unknown_events": 0,
        "bytes_consumed": 0, "bytes_total": int(cursor.get("bytes_total") or 0) if cursor else 0,
        "unchanged": False, "dry_run": bool(dry_run), "degraded": [], "orphan": False,
        # 非降级的告警（目录名≠头部 id 等）：它们**不是损失**（映射照采），但也不能
        # 只写进一个没人读的键——复审 🟡-18 的"死键"就是这么来的。
        "warnings": [],
    }
    if zstandard is None:
        raise AdapterError("缺 zstandard 依赖")
    try:
        stat_info = {"size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns}
    except OSError as exc:
        counters["status"] = store.SOURCE_DEGRADED
        counters["degraded"].append({"reason_code": store.REASON_UNREADABLE,
                                     "reason": "%s: %s" % (type(exc).__name__, exc),
                                     "segments_kept": 0})
        if not dry_run:
            store.record_degraded(conn, HOST, path, sid, store.REASON_UNREADABLE,
                                  counters["degraded"][0]["reason"], 0)
            if cursor:
                store.set_source_status(conn, HOST, sid, store.SOURCE_DEGRADED,
                                        store.REASON_UNREADABLE)
            conn.commit()
        return counters
    counters["format"] = (cursor or {}).get("format") or FORMAT_BY_NAME.get(
        path.name, store.FORMAT_DSH_LEGACY)
    if cursor and _same_file_as_cursor(stat_info, cursor):
        counters["unchanged"] = True
        return counters
    if block_sessions and sid in block_sessions:
        counters["status"] = "blocked"
        return counters

    start = int((cursor or {}).get("bytes_total") or 0)
    if cursor:
        counters["format"] = cursor.get("format") or counters["format"]
    if start and not is_frame_boundary(path, start, stat_info["size"]):
        # 源文件被重写过（偏移不再落在帧边界）：归零重采。段以 `loc` 为幂等键，
        # 重采只会**更新**已有段，不会产生重复段——比静默少采安全得多。
        counters["cursor_reset"] = True
        start = 0
    if counters["format"] not in SUPPORTED_FORMATS:
        # v3：事件模型仍未取证（T-V1）。**登记降级**而不是假装能采。
        reason = "格式 %s 的事件模型未取证，P0 不采（T-V1）" % counters["format"]
        counters["status"] = store.SOURCE_DEGRADED
        counters["degraded"].append({"reason_code": store.REASON_UNSUPPORTED_FORMAT,
                                     "reason": reason, "segments_kept": 0})
        if not dry_run:
            store.record_degraded(conn, HOST, path, sid, store.REASON_UNSUPPORTED_FORMAT,
                                  reason, store.count_segments(conn, HOST, sid))
            conn.commit()
        return counters
    cap = resolve_max_frame_bytes(None, max_frame_bytes)
    counters["max_frame_bytes"] = cap

    tagger = tagger or Tagger()
    session = None                       # 打标后的会话行（拿到头部才成立）
    first_seen_at = (cursor or {}).get("first_ingested_at")
    committed_offset = start             # **只在 flush() 成功之后**前进（R2）
    pending_offset = start               # 本批已解析到的帧边界（坏帧回退到这里）
    pending_bytes = 0
    last_seq = (cursor or {}).get("last_seq_seen")
    # `corpus_source.events_seen` 的口径 = **文件当前实有事件数**（扫描口径），不是跨轮累计。
    # `start == 0` 表示这一次是从头扫（首采，或源变短/被重写后的归零重采）：整份文件已经
    # 重读了一遍，再叠加旧游标里的数就是**重复计数**（实测 3+2=5，而文件只有 2 个事件）。
    events_seen_before = int((cursor or {}).get("events_seen") or 0)
    events_base = 0 if not start else events_seen_before
    batch = []                           # 待应用的段/打标动作
    batch_stats = {}                     # event_type → [seen, kept, skipped]
    batch_frames = max(1, int(batch_frames))

    def pending_session_row():
        """没有头部时的占位会话行：FK 需要它，而"cwd 未知"要如实写 `no_cwd`。"""
        row = tagger.tag_session({"session_id": sid, "extra": {"header_missing": 1}}, HOST)
        return row

    def ensure_session():
        """**惰性**建占位会话行（2026-10-27）：不要再在建库一开始就写一行。

        旧实现在读帧之前就无条件 `ensure_session()`：目录名 ≠ 头部 id 时那行会变成
        幽灵会话（真 id 另有一行），"第二次 ingest 幂等"立刻变成"每轮多一行"。
        """
        nonlocal session
        if session is not None:
            return session
        existing = store.get_session(conn, HOST, sid)
        if existing:
            session = existing
            return session
        session = pending_session_row()
        if not dry_run:
            with store.write_txn(conn):
                store.upsert_session(conn, session)
        return session

    def kept_segments(extra=0):
        """此刻"已提交 + 本批待提交 + extra"的段数——台账里的 `segments_kept` 要是真值。

        为什么不能只查 `count_segments`：段是**按批**提交的，采集中途查库只看到上一批，
        于是一条被截断的消息会写成 `segments_kept=0`（与"段确实入库了"相反，复审 安全🟡）。
        """
        pending = sum(1 for action in batch if action[0] == "segment")
        return store.count_segments(conn, HOST, sid) + pending + int(extra)

    def note_oversize(loc, extra=1):
        """段正文被截断（M6）：**声明过的**损失，绝不静默吞。

        2026-10-27 复审（安全 🟡）：旧实现只写台账、不置 `status`，于是 ingest 摘要
        说"降级 0"而台账/`doctor` 里有一条 `oversize_text`——同一次采集两个相反读数。
        现在三处一致：① `status=degraded`（文件确实有声明过的损失，摘要据此计数）；
        ② `segments_kept` 记**真值**（被截断的段仍然入库，写 0 与事实相反）；
        ③ 台账行与返回值里的 `segments_kept` 用同一个数。
        """
        kept = kept_segments(extra)
        reason = "loc=%s 的正文超过 %d 字，已截断（保留前缀）" % (loc, store.MAX_PART_CHARS)
        counters["status"] = store.SOURCE_DEGRADED
        counters["degraded"].append({
            "reason_code": store.REASON_OVERSIZE_TEXT, "reason": reason,
            "segments_kept": kept})
        if not dry_run:
            store.record_degraded(conn, HOST, path, sid, store.REASON_OVERSIZE_TEXT, reason,
                                  kept)

    def flush(force: bool = False, upto=None):
        """提交本批：段 + 打标 + 事件统计 + 游标，**同一事务**。

        `upto` = 这批数据对应的帧边界。**游标只在事务成功后写进去**（R2）：
        旧实现先把 `committed_offset` 往前推、再 flush，于是"batch 被回滚 + 游标越过
        坏帧"= 那几帧的原话永久少采，而且第二次跑也不会补（游标已经越过它们了）。
        `force=True`：即使没有段也要写游标行——0 段会话（只有头部/只有注入块）
        否则在 `corpus_source` 里没有行，对账会出现 sessions 40 vs sources 39（P2-1）。
        """
        nonlocal batch, batch_stats, committed_offset
        if not batch and not force:
            return
        boundary = committed_offset if upto is None else int(upto)
        if dry_run:
            batch = []
            batch_stats = {}
            committed_offset = boundary
            return
        with store.write_txn(conn):
            for action in batch:
                kind = action[0]
                if kind == "segment":
                    created_flag = store.upsert_segment(conn, action[1])[1]
                    if created_flag:
                        counters["segments_new"] += 1
                    else:
                        counters["segments_updated"] += 1
                elif kind == "prune":
                    store.mark_shadowed(conn, HOST, action[1], action[2])
                elif kind == "title":
                    conn.execute(
                        "UPDATE corpus_session SET title=? WHERE host=? AND session_id=?",
                        (action[1], HOST, action[2]))
            for event_type, (seen, kept, skipped) in batch_stats.items():
                store.bump_event_stat(conn, HOST, sid, event_type, seen=seen, kept=kept,
                                      skipped=skipped)
            if session is not None:
                store.upsert_session(conn, session)
            store.upsert_source(conn, {
                "host": HOST, "session_id": sid, "path": str(path),
                "format": counters["format"], "bytes_total": boundary,
                "mtime_ns": int(stat_info["mtime_ns"]),
                "events_seen": events_base + counters["events_seen"],
                "segments_kept": store.count_segments(conn, HOST, sid),
                "last_seq_seen": last_seq, "last_line_seen": None,
                "status": counters["status"], "degraded_reason": (
                    counters["degraded"][-1]["reason_code"] if counters["degraded"] else None),
                "first_ingested_at": first_seen_at})
        batch = []
        batch_stats = {}
        committed_offset = boundary

    def stat_event(event_type, kept=0, skipped=0):
        slot = batch_stats.setdefault(event_type, [0, 0, 0])
        slot[0] += 1
        slot[1] += kept
        slot[2] += skipped

    frame_start = start                 # 解压流里的帧起点（= 上次提交的字节数）
    try:
        for kind, payload in iter_lines(path, start, chunk_size, cap):
            if kind == "frame_end":
                # 帧边界 = 提交边界：这一帧已完整解出，游标可以前进到这里。
                consumed, end_offset = payload
                counters["frames"] += 1
                frame_start = end_offset
                pending_offset = end_offset
                pending_bytes += consumed
                if len(batch) >= batch_frames:
                    flush(upto=pending_offset)
                    counters["bytes_consumed"] += pending_bytes
                    pending_bytes = 0
                continue
            if kind == "tail":
                # 尾帧未写完：不消耗、不提交半帧。本批已完整帧照常提交。
                break
            line_from, line = payload
            if not line.strip():
                continue
            try:
                ev = json.loads(line.decode("utf-8", errors="replace"))
            except ValueError as exc:
                # 坏 JSON 行：跳过 + 计数（C-P0-1 红①不适用"整文件失败"）
                counters["degraded"].append({
                    "reason_code": store.REASON_BAD_JSON,
                    "reason": "%s（帧内偏移 %d）" % (exc, line_from),
                    "segments_kept": store.count_segments(conn, HOST, sid) if not dry_run else 0})
                stat_event("<bad_json>", skipped=1)
                continue
            counters["events_seen"] += 1
            seq = ev.get("seq")
            if isinstance(seq, int):
                last_seq = seq if last_seq is None else max(last_seq, seq)
                loc = "seq=%d" % seq
            else:
                loc = "no-seq"
            ev_kind, ev_data = map_event(ev)
            if ev_kind == "header":
                header = _header_of(ev_data, sid)
                true_sid = header["session_id"]
                stat_event("session", kept=1)
                if true_sid != sid:
                    # 目录名≠头部 id 是**非降级告警**：映射照采（下面重启一次），
                    # 但"发生过不一致"必须能出现在返回/CLI 上（复审 🟡：旧键没人读）。
                    counters["warnings"].append("path_id_mismatch")
                    if allow_restart:
                        # 只重启**一次**：先把"目录名 → 头部 id"落盘，再按真 id 重入。
                        # 下一轮 `_ingest_host` 直接查这条映射拿游标，不再重入（R1）。
                        if not dry_run:
                            store.set_path_session(conn, HOST, sid, true_sid)
                            conn.commit()
                        flush()          # 头部帧本身不产段：这里一般什么都不写
                        restarted = ingest_file(
                            conn, path, store.read_cursor(conn, HOST, true_sid),
                            tagger=tagger, dry_run=dry_run,
                            batch_frames=batch_frames, chunk_size=chunk_size,
                            max_frame_bytes=cap,
                            block_sessions=block_sessions,
                            session_id_override=true_sid, allow_restart=False)
                        # 本轮的告警（目录名≠头部 id）属于**这一次采集**：重入的
                        # 返回会把它们丢掉，所以在这里并回去（复审 🟡 的死键修复）。
                        restarted["warnings"] = (list(counters["warnings"])
                                                 + list(restarted.get("warnings") or ()))
                        return restarted
                    # 已经按头部 id 采过一轮、却又读到**另一个** id：收敛成降级。
                    # 绝不递归（这正是旧实现栈溢出的那条路），也不假装能采全。
                    flush(force=True)
                    reason = ("会话头 id 前后不一致：文件内先 %s 后 %s（目录名 %s）"
                              "——已停止在本帧前，交人处理" % (sid, true_sid, sid))
                    counters["status"] = store.SOURCE_DEGRADED
                    counters["degraded"].append({
                        "reason_code": store.REASON_BAD_HEADER, "reason": reason,
                        "segments_kept": 0 if dry_run else store.count_segments(
                            conn, HOST, sid)})
                    if not dry_run:
                        store.record_degraded(conn, HOST, path, sid,
                                              store.REASON_BAD_HEADER, reason,
                                              store.count_segments(conn, HOST, sid))
                        store.upsert_source(conn, {
                            "host": HOST, "session_id": sid, "path": str(path),
                            "format": counters["format"], "bytes_total": committed_offset,
                            "mtime_ns": int(stat_info["mtime_ns"]),
                            "events_seen": events_base + counters["events_seen"],
                            "segments_kept": store.count_segments(conn, HOST, sid),
                            "last_seq_seen": last_seq, "last_line_seen": None,
                            "status": store.SOURCE_DEGRADED,
                            "degraded_reason": store.REASON_BAD_HEADER,
                            "first_ingested_at": first_seen_at})
                        conn.commit()
                    counters["bytes_total"] = committed_offset
                    return counters
                session = tagger.tag_session(header, HOST)
                session["title"] = _safe_title(session.get("title"))
                if not dry_run:
                    store.upsert_session(conn, session)
            elif ev_kind == "title":
                title = (ev_data or {}).get("title")
                if title:
                    safe = _safe_title(str(title))       # S3：标题也是内容
                    batch.append(("title", safe, sid))
                    if session is not None:
                        session["title"] = safe
                stat_event("session/title", kept=1 if title else 0)
            elif ev_kind in ("user", "assistant"):
                made, skipped, truncated = _segments_of(
                    ev, ev_data, ev_kind, loc, frame_start, line_from, len(line),
                    session or ensure_session())
                if truncated:
                    note_oversize(loc, extra=len(made))
                if not made:
                    stat_event(ev.get("type") or ev_kind, skipped=skipped or 1)
                else:
                    for seg in made:
                        batch.append(("segment", seg, None))
                    if dry_run:
                        counters["segments_new"] += len(made)
                    stat_event(ev.get("type") or ev_kind, kept=len(made),
                               skipped=skipped)
                    if session is not None:
                        session["last_event_at"] = made[-1]["occurred_at"]
                if skipped:
                    counters["segments_skipped"] += skipped
            elif ev_kind == "prune":
                locs = _shadow_locs(ev_data or {})
                if locs:
                    batch.append(("prune", sid, locs))
                stat_event("compaction/prune", kept=len(locs))
            elif ev_kind == "skip":
                stat_event(str(ev_data or "skip"), skipped=1)
            else:
                counters["unknown_events"] += 1
                stat_event(str(ev_data or "unknown"), skipped=1)
            if len(batch) >= batch_frames:
                # 段太多就先落盘（游标仍停在最后一帧边界：帧没读完，不能前进）。
                flush(upto=pending_offset)
                counters["bytes_consumed"] += pending_bytes
                pending_bytes = 0
        flush(force=True, upto=pending_offset)
        counters["bytes_consumed"] += pending_bytes
    except TagError as exc:
        # 会话头自相矛盾（子代理缺父/缺深度）：登记降级，已提交的段保留。
        counters["status"] = store.SOURCE_DEGRADED
        reason = "%s: %s" % (type(exc).__name__, exc)
        counters["degraded"].append({"reason_code": store.REASON_BAD_HEADER,
                                     "reason": reason,
                                     "segments_kept": 0 if dry_run else store.count_segments(conn, HOST, sid)})
        if not dry_run:
            store.record_degraded(conn, HOST, path, sid, store.REASON_BAD_HEADER, reason,
                                  store.count_segments(conn, HOST, sid))
            # 补一条游标（状态 degraded）：只 `UPDATE` 的话，**首轮就失败**的文件
            # 在 `corpus_source` 里没有任何行，孤儿/对账都看不见它（C-P0-9 要它出现）。
            store.upsert_source(conn, {
                "host": HOST, "session_id": sid, "path": str(path),
                "format": counters["format"], "bytes_total": committed_offset,
                "mtime_ns": int(stat_info["mtime_ns"]),
                "events_seen": events_base + counters["events_seen"],
                "segments_kept": store.count_segments(conn, HOST, sid),
                "last_seq_seen": last_seq, "last_line_seen": None,
                "status": store.SOURCE_DEGRADED, "degraded_reason": store.REASON_BAD_HEADER,
                "first_ingested_at": first_seen_at})
            conn.commit()
    except FrameError as exc:
        # 帧间截断/超大帧（R2）：**先把本批好帧落盘**，再把游标停在最后成功边界。
        #
        # 旧实现的两个错叠在一起成了"永久少采"：① 坏帧只 ROLLBACK，坏帧之前的整批
        # （默认 64 帧！）连坐丢掉；② `committed_offset` 已经前进过，游标被写成"越过
        # 坏帧"的值 → 下一轮从那之后开始读，丢掉的那批永远不会再采。
        #
        # A③（2026-10-27 采集完整性）：**先 flush 再登记降级**，且台账里的
        # `segments_kept` 用 flush 之后的真值。超限帧是"解到上限"才停的，那之前解出的
        # 段已经在本批里了——先写台账会把它们记成 0（与事实相反），顺序反过来才能
        # "已解出的段落库 + 损失如实登记"。变异体：把这段 flush 删掉 → 段全丢。
        counters["status"] = store.SOURCE_DEGRADED
        if not dry_run:
            try:
                # force=True：坏帧之前可能刚有过"帧内提交"，此刻 batch 是空的但游标还
                # 停在上一帧边界——不写就行的话游标永远追不上，下一轮又从头解这帧。
                flush(force=True, upto=pending_offset)
            except Exception:                # noqa: BLE001 —— 落盘失败也要如实登记原始错误
                conn.rollback()
        kept = 0 if dry_run else store.count_segments(conn, HOST, sid)
        counters["degraded"].append({"reason_code": exc.reason_code, "reason": str(exc),
                                     "segments_kept": kept})
        if not dry_run:
            store.record_degraded(conn, HOST, path, sid, exc.reason_code, str(exc), kept)
            store.upsert_source(conn, {
                "host": HOST, "session_id": sid, "path": str(path),
                "format": counters["format"], "bytes_total": committed_offset,
                "mtime_ns": int(stat_info["mtime_ns"]),
                "events_seen": events_base + counters["events_seen"],
                "segments_kept": store.count_segments(conn, HOST, sid),
                "last_seq_seen": last_seq, "last_line_seen": None,
                "status": store.SOURCE_DEGRADED, "degraded_reason": exc.reason_code,
                "first_ingested_at": first_seen_at})
            conn.commit()
    except OSError as exc:
        counters["status"] = store.SOURCE_DEGRADED
        reason = "%s: %s" % (type(exc).__name__, exc)
        counters["degraded"].append({"reason_code": store.REASON_UNREADABLE,
                                     "reason": reason, "segments_kept": 0})
        if not dry_run:
            store.record_degraded(conn, HOST, path, sid, store.REASON_UNREADABLE, reason,
                                  store.count_segments(conn, HOST, sid))
            conn.commit()
    if not dry_run:
        store.refresh_session_counts(conn, HOST, sid)
        conn.commit()
    counters["bytes_total"] = committed_offset
    return counters


def _segments_of(ev, data, kind, loc, frame_start, line_from, line_len, session) -> tuple:
    """一条消息 → `(若干段, 被跳过的 part 数, 是否截断)`（一个 text part 一段）。

    正文超 `store.MAX_PART_CHARS` 时**截断并声明**（第三个返回值 → `oversize_text`
    降级条目）：那条消息的剩余部分不进库，但"它被截过"写进台账，不是静默丢。
    """
    if kind == "user":
        content = data.get("content")
        msg_id = data.get("id")
        turn = data.get("turn")
        step = data.get("step")
    else:
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        content = message.get("content")
        msg_id = message.get("id") or data.get("id")
        turn = data.get("turn")
        step = data.get("step")
    texts, attachments = _text_parts(content)
    texts = _attach_placeholders(texts, attachments)
    skipped = 0
    for item in content or []:
        if isinstance(item, dict) and item.get("type") not in TEXT_PART_TYPES:
            skipped += 1
    if not texts:
        return [], skipped, False
    occurred = _to_utc(ev.get("time")) or session.get("started_at") or store.now_utc()
    segs = []
    truncated_any = False
    for part, text in enumerate(texts):
        redacted, hits, _ = redact_mod.redact(text)
        redacted, truncated = store.cap_part_text(redacted)
        truncated_any = truncated_any or truncated
        segs.append({
            "host": HOST, "session_id": session["session_id"], "loc": loc, "part": part,
            "role": "user" if kind == "user" else "assistant",
            "msg_id": str(msg_id) if msg_id else None,
            "turn": int(turn) if isinstance(turn, int) else None,
            "step": int(step) if isinstance(step, int) else None,
            "occurred_at": occurred,
            "text": redacted,
            "chars": len(redacted),
            "redacted_count": hits,
            "raw_sha256": store.text_sha256(text),
            "byte_from": frame_start + line_from,
            "byte_to": frame_start + line_from + line_len,
            "offset_space": OFFSET_SPACE,
            "shadowed": 0,
        })
    return segs, skipped, truncated_any


class DshAdapter:
    """`DshAdapter`（设计 §3 类图）：把模块函数收成可注入的适配器对象。"""

    host = HOST
    formats = SUPPORTED_FORMATS

    def discover(self, roots) -> list:
        return discover(roots)

    def iter_frames(self, path, offset: int = 0):
        return iter_frames(path, offset)

    def map_event(self, ev):
        return map_event(ev)

    def ingest_file(self, conn, path, cursor=None, **kwargs):
        return ingest_file(conn, path, cursor, **kwargs)
