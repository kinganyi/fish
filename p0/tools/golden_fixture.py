#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""冻结夹具生成器（`fish.py` 拆包第一轮 · T01 · 设计 §1.5）。

生成什么
--------
`_golden/r1/fixture/` 是一份**可重复生成**的冻结工作区：

  fixture/
    data/index.sqlite                 ← `fish init` 建库，行里的时间/id 全部钉死
    data/_backups/kb/<vault_id>/       ← 预置一份"很久以前"的回滚点（meta.json + payload）
      20260102T030405Z-.../
    vault/                            ← 6 个 .md（含中文名 + 同名不同目录 + 90-模板）
      .obsidian/app.json              ← 被强制忽略的运行目录
      attachments/图.png              ← 媒体文件（魔数真 PNG）
      30-新写/                        ← **空目录**：`kb write` 不自动建目录，写命令要用到它
    content.md                        ← `kb write --content-file` 的固定输入
    seed.json                         ← 本文件的产物清单（钉住的事实 + 文件摘要）

为什么必须冻结
--------------
`kb` 的每条命令都会 `executescript(SCHEMA) + migrate`，所以"只读命令"并不存在
（设计 §1.5）。既然一定会建库/写库，对拍就**不能**用生产库 `p0/data`：
既会污染真库，也会让两版读数互相干扰。唯一安全的做法是「冻结夹具副本 + 路径硬断言」。
夹具把三类噪声源先钉住，剩下的才是真正需要对拍的差异：

  ① 文件 mtime  → `os.utime` 钉到 `PIN_TS`（`kb tree/open` 会打印它）；
  ② 库里的时间戳 / 随机 id → 建库后逐列回填 `PIN_TS` 与钉死的 id；
  ③ 备份目录名里的紧凑时间戳 → 只留预置的那一份。

纪律
----
· 只写 `--out` 目录，且**硬断言**它位于某个 `_golden/` 之下；绝不写 `p0/data`；
· 生成**幂等**：每次先整目录删掉再重建；同参数生成的逻辑内容完全一致；
· 时间只来自 `PIN_*` 常量，不读系统时钟（除了 `fish` 自己写的行，随后被回填）。
· 生成完后**不留** `fish-home/`（门面由对拍命令 `layout render` 现场渲染，
  否则副本会带着"上一次渲染"的陈旧门面，`layout verify` 读到的就不是同一件事）。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

# ------------------------------------------------------------------ 钉死的常量
PIN_TS = "2026-01-02T03:04:05Z"                 # 所有时间戳的统一取值（ISO8601 Z）
PIN_STAMP = "20260102T030405Z"                  # 备份目录名里的紧凑时间戳
PIN_VAULT_ID = "01GOLDEN0000000000000000V"      # 26 字符，与 ulid 同形
PIN_CITIZEN_ID = "C-GOLDEN-0001"
PIN_CITIZEN_NAME = "冻结夹具公民"
PIN_PERIOD_KEY = "260102"                       # 看板期号（2026-01-02）
PIN_SEED_WRITE_REL = "30-新写/冻结种子.md"       # bootstrap 写掉再回滚：留下审计行与备份
PIN_BACKUP_REL = "10-漫剧/方法论/提示词手册.md"  # 预置回滚点针对的文件
PIN_BACKUP_WRITE_ID = "3-5eed0001"

VAULT_NAME = "golden"
WRITE_DIR = "30-新写"                            # 写命令的目标目录（必须在夹具里先存在）

# `fish` 自己写的行里，这些列名一律回填 `PIN_TS`（只改非 NULL 的行：
# `closed_at` / `archived_at` 这类"NULL 有语义"的列不能被打上时间戳）。
Pinned_COLS = ("ts", "created_at", "updated_at", "last_seen_at", "last_active_at",
               "last_scan_at", "occurred_at", "recorded_at", "closed_at", "archived_at")

# `events` 整表被 `ev_no_update` 触发器钉成不可变（产品语义：事件流水只增不改）。
# 夹具**不绕过**它：`events.ts` 保留 bootstrap 时刻，`layout verify` 那条
# "门面 as-of 早于最后一件事" 的 warning 因此保留原文，里面的时间戳由 `<TS>` 抹平。
PIN_SKIP_TABLES = ("events",)

# 钉住"写入面"的表：这些表在 bootstrap 后必须与时间无关（其余表由 Pinned_COLS 覆盖）。
EVENT_ID_FMT = "E{:023d}"

VAULT_FILES = {
    "00-首页/欢迎.md": (
        "---\n"
        "tags: [首页]\n"
        "created: 2026-01-02\n"
        "---\n"
        "# 欢迎\n"
        "\n"
        "冻结夹具的第一篇笔记。**金色池塘** 这个词只在这里出现一次，用来做确定性检索。\n"
        "\n"
        "## 本节\n"
        "\n"
        "- 目的：给 `fish kb` 的读路径一个稳定输入\n"
        "- 纪律：内容改了就重抓基线\n"
    ),
    "10-漫剧/方法论/提示词手册.md": (
        "---\n"
        "tags: [漫剧, 方法论]\n"
        "---\n"
        "# 提示词手册\n"
        "\n"
        "分镜提示词的写法：镜号 → 画面 → 运镜 → 时长。\n"
        "\n"
        "- 一镜一意\n"
        "- 光线先定，动作后定\n"
    ),
    "20-项目-A/笔记.md": (
        "# 项目 A 笔记\n"
        "\n"
        "同名文件之一（在 A 里）。用来看「同名不同目录」的提示是否成立。\n"
    ),
    "20-项目-B/笔记.md": (
        "# 项目 B 笔记\n"
        "\n"
        "同名文件之二（在 B 里）。\n"
    ),
    "30-草稿/半成品.md": (
        "# 半成品\n"
        "\n"
        "只写了一半的稿子。\n"
    ),
    "90-模板/模板.md": (
        "# 模板\n"
        "\n"
        "`90-模板` 是强制忽略目录（裁决 Q13）：AI 一律不许写它。\n"
    ),
    ".obsidian/app.json": '{\n  "legacyEditor": false\n}\n',
}

# `kb write --content-file` 的固定输入（create 与 append 共用同一个文件，
# 这样夹具表面只有设计里写的 `content.md` 一个正文文件）。
CONTENT_MD = (
    "# 基线新建\n"
    "\n"
    "`kb write --create` 的输入正文。只在这里出现的词：**银色走廊**。\n"
)

# 1x1 PNG：真魔数 + 真 IHDR/IDAT/IEND（75 B），用来验"媒体文件"这条判定。
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6360000002000100ffff0300000600"
    "0557bfabd40000000049454e44ae426082")


# ------------------------------------------------------------------ 小工具
def pin_epoch() -> int:
    """`PIN_TS` 的 unix 秒（`os.utime` 用；不猜闰秒、不读本地时区）。"""
    dt = datetime.datetime.strptime(PIN_TS, "%Y-%m-%dT%H:%M:%SZ")
    return int(dt.replace(tzinfo=datetime.timezone.utc).timestamp())


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_crlf(path: Path, text: str) -> None:
    """夹具里的 `.md` 统一 CRLF（与真实库里的 52/60 篇一致；`kb open` 会报 eol=crlf）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))


def _assert_inside_golden(path: Path) -> None:
    """硬断言：夹具目录必须位于一个 `_golden` 目录之下（设计 §1.5 的第一道闸）。"""
    parts = [p.lower() for p in path.resolve().parts]
    if "_golden" not in parts:
        raise SystemExit(
            f"拒绝执行：--out 必须在 `_golden/` 之下，收到 {path}。\n"
            "  （夹具是可丢弃的派生物，绝不允许生成到源码目录或生产库旁边。）")
    idx = parts.index("_golden")
    if idx + 1 >= len(parts):
        raise SystemExit(f"拒绝执行：--out 不能就是 `_golden` 目录本身：{path}")


def run_fish(fish_py: Path, root: Path, argv: list[str], *, cwd: Path) -> dict:
    """跑一条 `fish` 命令，返回 `{argv, exit, stdout, stderr}`（供 seed.json 留痕）。"""
    cmd = [sys.executable, "-X", "utf8", str(fish_py), "--root", str(root)] + list(argv)
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    rec = {"argv": ["fish"] + list(argv), "exit": proc.returncode,
           "stdout": proc.stdout, "stderr": proc.stderr}
    if proc.returncode != 0:
        raise SystemExit(
            "夹具 bootstrap 命令失败（夹具必须从干净的 fish 起，不能靠手改库绕过）：\n"
            f"  {rec['argv']}\n"
            f"  exit={proc.returncode}\n"
            f"  stdout={proc.stdout!r}\n"
            f"  stderr={proc.stderr!r}")
    return rec


# ------------------------------------------------------------------ 生成
def build_vault(vault: Path) -> list[dict]:
    """写 vault 内容并把每个文件的 mtime 钉死；返回产物清单（相对路径 + 摘要）。"""
    for rel, text in VAULT_FILES.items():
        _write_crlf(vault / rel, text)
    png = vault / "attachments" / "图.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    png.write_bytes(PNG_BYTES)
    (vault / WRITE_DIR).mkdir(parents=True, exist_ok=True)   # 空目录：写命令的目标父目录

    mtime = pin_epoch()
    out = []
    for path in sorted(p for p in vault.rglob("*") if p.is_file()):
        os.utime(path, (mtime, mtime))
        out.append({"rel": path.relative_to(vault).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path)})
    return out


def pin_database(db: Path) -> dict:
    """把 bootstrap 写出来的"现在"全部钉成 `PIN_TS`；返回实际改动读数。

    分三步，每一步都可解释：
      ① `kb_vault`：vault_id / 三个时间列（`kb tree`、`kb doctor` 会打印它们）；
      ② 其余表的**已知时间列 + 已知 id 列**：只改非 NULL 行，保住"NULL 有语义"；
      ③ `events.event_id`：ulid 形态的随机 id，重新编成 `E000…<seq>`（唯一约束仍在）。
    """
    conn = sqlite3.connect(str(db))
    try:
        changed: dict[str, int] = {}
        cur = conn.execute(
            "UPDATE kb_vault SET vault_id=?, last_scan_at=?, created_at=?, updated_at=? "
            "WHERE name=?", (PIN_VAULT_ID, PIN_TS, PIN_TS, PIN_TS, VAULT_NAME))
        changed["kb_vault"] = cur.rowcount

        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for table in tables:
            if table in PIN_SKIP_TABLES:
                continue
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            if not cols:
                continue
            hits = 0
            for col in cols:
                if col in Pinned_COLS:
                    cur = conn.execute(
                        f"UPDATE {table} SET {col}=? WHERE {col} IS NOT NULL", (PIN_TS,))
                    hits += cur.rowcount
            if hits:
                changed[table] = hits

        # 期号：只有看板有"期"这个概念，其余表的同名列不存在。
        if any(r[1] == "period_key" for r in conn.execute("PRAGMA table_info(boards)")):
            cur = conn.execute("UPDATE boards SET period_key=?", (PIN_PERIOD_KEY,))
            if cur.rowcount:
                changed["boards.period_key"] = cur.rowcount

        # 审计行里的 vault_id：bootstrap 时用真 id 写的，回填成钉死的那一个。
        cur = conn.execute("UPDATE kb_access SET vault_id=?", (PIN_VAULT_ID,))
        if cur.rowcount:
            changed["kb_access.vault_id"] = cur.rowcount
        # `events` 的 `event_id` **不回填**：`ev_no_update` 触发器把整表钉成不可变
        # （这是产品语义，夹具不该绕过）。它不影响对拍——捕获每次都从同一份夹具复制，
        # 随机 ulid 在"两遍"与"两版"之间都是同一串字节。
        conn.commit()

        rows = {
            "kb_vault": conn.execute(
                "SELECT vault_id, name, mode, ignore_globs, max_open_bytes, is_default, "
                "last_scan_at, last_scan_files, last_scan_notes, last_scan_bytes "
                "FROM kb_vault ORDER BY name").fetchall(),
            "kb_access": conn.execute(
                "SELECT vault_id, path, citizen_id, action, result, via, ts "
                "FROM kb_access ORDER BY id").fetchall(),
            "citizens": conn.execute(
                "SELECT citizen_id, display_name, kind, status, is_owner, created_at "
                "FROM citizens ORDER BY citizen_id").fetchall(),
            "boards": conn.execute(
                "SELECT scope, scope_key, period_key, status FROM boards ORDER BY id").fetchall(),
            "counts": {
                t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in tables},
        }
    finally:
        conn.close()
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str)
    return {"changed": changed, "digest": sha256_bytes(payload.encode("utf-8"))}


def write_preset_backup(data: Path, vault: Path) -> dict:
    """预置**一份很久以前**的回滚点（设计 §1.5）：`meta.json` + `payload`。

    为什么要预置：`kb doctor`（备份体积/最旧最新）与 `kb backup prune`（按天清理）
    都要有"旧备份"可读；靠 bootstrap 现写会产生"今天的时间戳"，测不到保留策略。
    """
    target = vault / PIN_BACKUP_REL
    payload = target.read_bytes()
    slug = "10-漫剧_方法论_提示词手册.md"
    base = data / "_backups" / "kb" / PIN_VAULT_ID / f"{PIN_STAMP}-{slug}"
    base.mkdir(parents=True, exist_ok=True)
    (base / "payload").write_bytes(payload)
    meta = {
        "rel": PIN_BACKUP_REL,
        "ts": PIN_STAMP,
        "bytes_before": len(payload),
        "sha256_before": sha256_bytes(payload),
        "write_id": PIN_BACKUP_WRITE_ID,
        "action": "append",
        "reason": "冻结夹具预置回滚点（T01）",
    }
    (base / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"dir": f"_backups/kb/{PIN_VAULT_ID}/{base.name}",
            "bytes": len(payload), "meta_keys": sorted(meta)}


def drop_generated_backups(data: Path, keep_name: str) -> int:
    """删掉 bootstrap 现写出来的回滚点，只留预置那一份（否则目录名带真实时间戳）。

    连"真 vault_id 的空壳目录"一起清：bootstrap 用的是当场生成的 ulid，
    留着它 `_backups/kb/` 下就会多一个没人认领的目录。
    """
    root = data / "_backups" / "kb"
    removed = 0
    if not root.is_dir():
        return 0
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if not path.is_dir() or path == root:
            continue
        if path.name == keep_name or path.name == PIN_VAULT_ID:
            continue
        shutil.rmtree(path)
        removed += 1
    return removed


def main() -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="生成 fish 拆包对拍的冻结夹具（T01）")
    ap.add_argument("--out", default=str(here.parent / "_golden" / "r1" / "fixture"),
                    help="夹具根（必须位于 _golden/ 之下）")
    ap.add_argument("--fish", default=str(here.parent / "fish.py"),
                    help="用哪个 fish.py 起夹具（默认本目录的 fish.py）")
    ap.add_argument("--quiet", action="store_true", help="只打印最后一行摘要")
    args = ap.parse_args()

    out = Path(args.out).resolve()
    fish_py = Path(args.fish).resolve()
    _assert_inside_golden(out)
    if not fish_py.is_file():
        raise SystemExit(f"找不到 fish.py：{fish_py}")
    if out.exists():
        shutil.rmtree(out)
    data = out / "data"
    vault = out / "vault"
    out.mkdir(parents=True)

    log = [] if args.quiet else None

    def say(msg: str) -> None:
        if log is None:
            print(msg)

    files = build_vault(vault)
    _write_crlf(out / "content.md", CONTENT_MD)
    say(f"[1/6] vault 写好：{len(files)} 个文件（mtime 钉到 {PIN_TS}）")

    content = out / "content.md"
    steps = [
        run_fish(fish_py, data, ["init"], cwd=out),
        run_fish(fish_py, data, ["kb", "vault", "add", "--name", VAULT_NAME,
                                 "--path", str(vault), "--mode", "rw", "--default",
                                 "--note", "冻结夹具（R1 拆包对拍）"], cwd=out),
        run_fish(fish_py, data, ["kb", "vault", "scan"], cwd=out),
        # 写一次再回滚：让 `kb stats` / 审计面有"真实的写与撤"可读，
        # 同时库回到"没有 30-新写/基线.md"的干净状态（那份文件由对拍命令自己造）。
        run_fish(fish_py, data, ["kb", "write", PIN_SEED_WRITE_REL, "--create",
                                 "--content-file", str(content), "--reason", "夹具种子"],
                 cwd=out),
    ]
    page = steps[-1]["stdout"]
    write_id = ""
    for line in page.splitlines():
        if "fish kb rollback" in line:
            write_id = line.rsplit(" ", 1)[-1].strip()
            break
    if not write_id:
        raise SystemExit(f"没能从 create 回执里取到 write_id：{page!r}")
    steps.append(run_fish(fish_py, data, ["kb", "rollback", write_id], cwd=out))
    say(f"[2/6] 库已建：init / vault add / scan / write+rollback（write_id={write_id}）")

    # 主人与看板走产品路径（**不**手插行）：先钉住公民行，再用 `fish owner set`。
    db = data / "index.sqlite"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO citizens (citizen_id, display_name, kind, role, host, status, "
            "created_at, is_owner, account_id, device_id, host_session_key) "
            "VALUES (?, ?, 'resident', 'owner', 'dsh', 'active', ?, 0, NULL, NULL, NULL)",
            (PIN_CITIZEN_ID, PIN_CITIZEN_NAME, PIN_TS))
        conn.commit()
    finally:
        conn.close()
    steps.append(run_fish(fish_py, data, ["owner", "set", PIN_CITIZEN_ID], cwd=out))
    steps.append(run_fish(fish_py, data, ["board", "checkin", "--citizen", PIN_CITIZEN_ID,
                                          "--doing", "冻结夹具：预置一次签到"], cwd=out))
    say("[3/6] 主人与看板已就位（走 owner set / board checkin 的产品路径）")

    pinned = pin_database(db)
    say("[4/6] 库内时间/id 已钉死：" + ", ".join(
        f"{k}={v}" for k, v in sorted(pinned["changed"].items())))

    removed = drop_generated_backups(data, f"{PIN_STAMP}-10-漫剧_方法论_提示词手册.md")
    preset = write_preset_backup(data, vault)
    say(f"[5/6] 回滚点：删掉 bootstrap 现写的 {removed} 份，预置 {preset['dir']}")

    # `fish board checkin` 会顺手把看板文件渲染进门面根（`fish-home/00-首页/看板-<期号>.md`）。
    # 那是**产品输出**，不是夹具输入：期号取自"今天"，留着它就把当天日期焊进夹具了。
    # 门面一律由对拍命令 `layout render` 现场渲染（设计 §1.5），所以这里删干净。
    facade = out / "fish-home"
    if facade.exists():
        shutil.rmtree(facade)
    if (out / "fish-home").exists():
        raise SystemExit("夹具里出现了 fish-home/：门面必须由对拍命令现场渲染，不能带陈旧副本")

    manifest = {
        "fixture": "r1",
        "generated_by": "tools/golden_fixture.py",
        "pinned": {
            "ts": PIN_TS,
            "stamp": PIN_STAMP,
            "vault_id": PIN_VAULT_ID,
            "citizen_id": PIN_CITIZEN_ID,
            "period_key": PIN_PERIOD_KEY,
            "backup_rel": PIN_BACKUP_REL,
            "backup_write_id": PIN_BACKUP_WRITE_ID,
        },
        "vault": {"name": VAULT_NAME, "dir": "vault", "files": files},
        "content_md": {"rel": "content.md", "bytes": (out / "content.md").stat().st_size,
                       "sha256": sha256_file(out / "content.md")},
        "preset_backup": preset,
        "db_digest": pinned["digest"],
        "bootstrap": [{"argv": s["argv"], "exit": s["exit"]} for s in steps],
    }
    seed_path = out / "seed.json"
    seed_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    say(f"[6/6] seed.json 写好（db_digest={pinned['digest'][:16]}）")
    print(f"夹具就绪：{out}\n  文件 {len(files)} 个 / 库摘要 {pinned['digest'][:16]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
