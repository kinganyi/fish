#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对拍比较器（`fish.py` 拆包第一轮 · T01 · 设计 §1.5）。

两件事，缺一不可
----------------
1. **掩码自证**：`mask(旧版第 1 遍) == mask(旧版第 2 遍)`。
   这条先跑——它证明"掩码集覆盖了全部环境噪声"。没有这一步，`旧 == 新` 就可能是
   "两边都被同一个掩码洗成了同一段文本"，那是假绿。
2. **拆包判定**：`mask(旧版第 1 遍) == mask(新版第 1 遍)`。
   掩码只抹**噪声**（时间/根路径/随机 id/耗时），抹不掉的差异就是行为差异。

掩码清单（设计 §1.5，7 个）
---------------------------
`<ROOT>` 工作副本或夹具的绝对路径（含 JSON 转义形态） `<TS>` ISO8601 时间戳
`<BTS>` 备份目录名里的紧凑时间戳                     `<DT>` 捕获当天的日期
`<ID>` write_id / ulid / uuid hex                    `<SEC>` 耗时读数（ms / s / *_ms 字段）
`<OBS>` Obsidian CLI 的绝对路径

每条规则都报**命中次数**：`hits_noise`（抹掉第 1/2 遍之间的噪声）与 `hits_diff`
（抹掉旧/新版之间的差异）。命中数写进 `noise.json`——"哪些掩码真的在干活、
哪些只是在待命"必须是可查的读数，不是口头承诺。

退出码：0 = 完全一致；2 = 有差异或掩码不足（两种情况都必须红）。
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GOLDEN = HERE.parent / "_golden"
R1 = GOLDEN / "r1"

# 规则表：族名 → 正则列表。`<DT>` / `<OBS>` / `<ROOT>` 需要上下文，单独在 mask_text 里处理。
RULES: dict[str, list[str]] = {
    "<TS>": [r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"],
    "<BTS>": [r"\b\d{8}T\d{6}Z\b"],
    "<ID>": [r"\b\d{1,6}-[0-9a-f]{8}\b",      # write_id：<access_id>-<hex8>
             r"\b\d{13}[0-9A-F]{13}\b",        # ulid
             r"\b[0-9a-f]{32}\b"],             # uuid hex（sha256 是 64 位，**不**在名单里）
    "<SEC>": [r"\b\d+(?:\.\d+)?\s*ms\b",
              r"\b\d+\.\d+\s*s\b",
              r'"\w*ms"\s*:\s*\d+(?:\.\d+)?'],  # JSON 里的 scan_ms / elapsed_ms
    "<ROOT>": [],
    "<DT>": [],
    "<OBS>": [],
}
MASK_NAMES = ["<ROOT>", "<TS>", "<BTS>", "<DT>", "<ID>", "<SEC>", "<OBS>"]


# ------------------------------------------------------------------ 掩码
def collect_roots(runs: list[dict]) -> list[str]:
    """两份捕获里出现过的路径前缀（越长越先替换，否则父路径会先吃掉子路径）。

    两种形态都要收：原样（`I:\\a\\b`）与 JSON 里的转义形态（`I:\\\\a\\\\b`）。
    注意**只**转义反斜杠——`json.dumps(path)` 会把中文转成 `\\uXXXX`，
    而 fish 的输出是 `ensure_ascii=False`（中文原样），拿它做替身会永远匹配不上。
    """
    roots = set()
    for run in runs:
        for key in ("fixture", "scratch", "checkout"):
            value = run.get("meta", {}).get(key)
            if value:
                roots.add(str(value))
    both = set()
    for item in roots:
        both.add(item)
        both.add(item.replace("\\", "\\\\"))
    return sorted(both, key=len, reverse=True)


def collect_days(runs: list[dict]) -> list[str]:
    """捕获当天的日期（两种形态都要抹）：`2026-10-27` 与看板期号 `261027`。

    为什么两种：`board show` 的标题是 ISO 日期，而看板**文件名**用的是期号
    （`看板-261027.md`）。只抹一种，另一种就会在跨天时变成假差异。
    """
    days = set()
    for run in runs:
        day = run.get("meta", {}).get("captured_local_date")
        if not day:
            continue
        days.add(day)
        try:
            parts = [int(x) for x in day.split("-")]
            days.add("%02d%02d%02d" % (parts[0] % 100, parts[1], parts[2]))
        except (TypeError, ValueError):
            pass
    return sorted(days, key=len, reverse=True)


def mask_text(text: str, *, roots: list[str], days: list[str]) -> tuple[str, dict]:
    """按规则抹噪声；返回 (结果, 每族命中次数)。"""
    hits = {name: 0 for name in MASK_NAMES}
    out = text
    for root in roots:
        if root and root in out:
            hits["<ROOT>"] += out.count(root)
            out = out.replace(root, "<ROOT>")
    for family, patterns in RULES.items():
        for pattern in patterns:
            out, n = re.subn(pattern, family, out)
            hits[family] += n
    for day in days:
        if day in out:
            hits["<DT>"] += out.count(day)
            out = out.replace(day, "<DT>")
    # Obsidian CLI 的绝对路径：只抹"带 Obsidian 的路径 token"，不碰正文
    out, n = re.subn(r"[A-Za-z]:[\\/](?:[^\s\"',]*Obsidian[^\s\"',]*)", "<OBS>", out)
    hits["<OBS>"] += n
    return out, hits


def block_of(item: dict) -> str:
    """一条命令的可比文本：slug + exit + stdout + stderr（顺序固定）。"""
    return (f"=== {item['slug']} | exit={item['exit']} ===\n"
            f"{item['stdout']}\n--- stderr ---\n{item['stderr']}\n")


# ------------------------------------------------------------------ 比较
def load_run(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"找不到捕获结果：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def compare_pair(old_run: dict, new_run: dict, *, roots: list[str], days: list[str],
                 label: str) -> tuple[bool, list[str], dict]:
    """逐命令比较两份捕获（掩码之后）；返回 (是否全等, 差异行, 读数)。"""
    old_by_slug = {c["slug"]: c for c in old_run["commands"]}
    new_by_slug = {c["slug"]: c for c in new_run["commands"]}
    if list(old_by_slug) != list(new_by_slug):
        raise SystemExit(
            f"两份捕获的命令清单不一致（{label}）：\n"
            f"  old={list(old_by_slug)}\n  new={list(new_by_slug)}")
    diffs: list[str] = []
    hits = {name: 0 for name in MASK_NAMES}
    raw_diff_slugs: list[str] = []
    for slug in old_by_slug:
        old_raw = block_of(old_by_slug[slug])
        new_raw = block_of(new_by_slug[slug])
        if old_raw != new_raw:
            raw_diff_slugs.append(slug)
        old_block, old_hits = mask_text(old_raw, roots=roots, days=days)
        new_block, new_hits = mask_text(new_raw, roots=roots, days=days)
        for name in MASK_NAMES:
            hits[name] += old_hits[name] + new_hits[name]
        if old_block == new_block:
            continue
        diffs.append(f"### 差异：{label} / {slug}"
                     f"（exit old={old_by_slug[slug]['exit']}"
                     f" new={new_by_slug[slug]['exit']}）")
        diffs.extend(difflib.unified_diff(old_block.splitlines(), new_block.splitlines(),
                                          fromfile=f"old/{slug}", tofile=f"new/{slug}",
                                          lineterm="", n=3))
        diffs.append("")
    return (not diffs), diffs, {"hits": hits, "raw_diff_slugs": raw_diff_slugs}


def sha256_file(path: Path) -> str:
    if not path.is_file():
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_manifest(base: Path, *, old_dir: Path, new_dir: Path, report: dict) -> dict:
    """`manifest.json`：这批对拍**到底比了什么**（夹具 / 命令清单 / 两版捕获 / 源码）。

    为什么单独留一份：`diff.txt` 说"结果"，`noise.json` 说"掩码"，而"输入是哪一版、
    夹具的库摘要是什么、命令清单的哈希是多少"这三件事散落在目录里。半年后要复查
    "这次 PASS 是不是拿错了文件"，只有清单能回答。
    """
    p0 = HERE.parent
    sources = {}
    for name in ("fish.py", "fish_kb.py", "fish_common.py"):
        path = p0 / name
        if path.is_file():
            text = path.read_bytes().decode("utf-8")
            sources[name] = {"lines": len(text.split("\n")),
                             "bytes": path.stat().st_size,
                             "sha256": sha256_file(path)}
    seed = {}
    seed_path = base / "fixture" / "seed.json"
    if seed_path.is_file():
        seed = json.loads(seed_path.read_text(encoding="utf-8"))
    manifest = {
        "round": "kbsplit-r1",
        "design": "fish.py 拆包第一轮设计 §1.5（2026-10-27）",
        "sources": sources,
        "fixture": {
            "path": "fixture",
            "db_digest": seed.get("db_digest"),
            "seed_sha256": sha256_file(seed_path),
            "files": len((seed.get("vault") or {}).get("files") or []),
            "pinned": seed.get("pinned"),
        },
        "commands": {
            "file": "tools/golden_cmds.txt",
            "count": report.get("commands"),
            "sha256": sha256_file(p0 / "tools" / "golden_cmds.txt"),
        },
        "captures": {
            "old": {"dir": str(old_dir),
                    "checkout": report["checkouts"]["old"],
                    "runs": {name: sha256_file(old_dir / name)
                             for name in ("run-1.json", "run-2.json")}},
            "new": {"dir": str(new_dir),
                    "checkout": report["checkouts"]["new"],
                    "runs": {name: sha256_file(new_dir / name)
                             for name in ("run-1.json", "run-2.json")}},
        },
        "masks": {"hits": report["mask_hits"], "noise_masks": report["noise_masks"],
                  "dormant_masks": report["dormant_masks"]},
        "verdict": {"value": report["verdict"],
                     "noise_identical": report["noise"].get("identical")},
    }
    (base / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description="比较两份 golden 捕获（掩码 + diff）")
    ap.add_argument("--old", default=str(R1 / "old"), help="旧版捕获目录（含 run-1/run-2）")
    ap.add_argument("--new", default=str(R1 / "new"), help="新版捕获目录")
    ap.add_argument("--base", default=str(R1), help="noise.json / diff.txt 写到哪")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    old_dir, new_dir = Path(args.old).resolve(), Path(args.new).resolve()
    base = Path(args.base).resolve()
    base.mkdir(parents=True, exist_ok=True)

    old1 = load_run(old_dir / "run-1.json")
    new1 = load_run(new_dir / "run-1.json")
    old2_path = old_dir / "run-2.json"
    old2 = load_run(old2_path) if old2_path.is_file() else None

    roots = collect_roots([old1, new1] + ([old2] if old2 else []))
    days = collect_days([old1, new1] + ([old2] if old2 else []))

    # ---- 第一关：掩码必须吃掉"同一版本两遍之间"的全部噪声（否则判定是假的）
    noise = {"available": old2 is not None, "identical": True, "hits": {},
             "raw_diff_slugs": []}
    if old2 is not None:
        same_noise, noise_diffs, noise_reading = compare_pair(
            old1, old2, roots=roots, days=days, label="noise(old run1 vs run2)")
        noise.update({"identical": same_noise, "hits": noise_reading["hits"],
                      "raw_diff_slugs": noise_reading["raw_diff_slugs"]})
        if not same_noise:
            noise["unmasked_diffs"] = noise_diffs[:60]

    # ---- 第二关：旧 vs 新
    equal, diffs, reading = compare_pair(old1, new1, roots=roots, days=days,
                                        label="split(old vs new)")

    dormant = [name for name in MASK_NAMES if not reading["hits"][name]]
    noise_masks = [name for name in MASK_NAMES if noise["hits"].get(name)]
    verdict = "PASS" if (equal and noise["identical"]) else "FAIL"
    report = {
        "generated_by": "tools/golden_compare.py",
        "inputs": {"old": str(old_dir), "new": str(new_dir)},
        "checkouts": {"old": old1["meta"]["checkout"], "new": new1["meta"]["checkout"]},
        "captured_local_date": {"old": old1["meta"]["captured_local_date"],
                                "new": new1["meta"]["captured_local_date"]},
        "commands": len(old1["commands"]),
        "noise": noise,
        "mask_hits": reading["hits"],
        "noise_masks": noise_masks,
        "dormant_masks": dormant,
        "raw_diff_slugs": reading["raw_diff_slugs"],
        "verdict": verdict,
    }
    (base / "noise.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
    write_manifest(base, old_dir=old_dir, new_dir=new_dir, report=report)

    lines = ["# 对拍报告（旧版 vs 新版，掩码后逐字节比较）", ""]
    lines.append(f"- 旧版：`{old1['meta']['checkout']}` → 捕获 `{old_dir}`")
    lines.append(f"- 新版：`{new1['meta']['checkout']}` → 捕获 `{new_dir}`")
    lines.append(f"- 命令条数：{len(old1['commands'])}")
    lines.append(f"- 噪声自证：{'有 run-2' if old2 else '**缺 run-2，无法自证**'} → "
                 f"{'一致' if noise['identical'] else '**不一致（掩码不足）**'}")
    fired = "、".join(f"{k}×{v}" for k, v in reading["hits"].items() if v) or "（无）"
    lines.append(f"- 掩码命中（旧+新）：{fired}")
    lines.append(f"- 未命中的掩码：{'、'.join(dormant) or '（无，全部在用）'}")
    lines.append(f"- 未掩码即有差异的命令：{'、'.join(reading['raw_diff_slugs']) or '（无）'}")
    lines.append(f"- 判定：**{verdict}**")
    lines.append("")
    if verdict == "PASS":
        lines.append(f"{len(old1['commands'])} 条命令在掩码后逐字节一致；"
                     "掩码在旧版两遍之间抹掉的全部噪声均已被覆盖。")
    else:
        if not noise["identical"]:
            lines.append("## 掩码不足（旧版跑两遍仍不一致）")
            lines.append("说明还有一类噪声没被掩码覆盖——此时**不能**用同一套掩码判定拆包。")
            lines.append("")
            lines.extend(noise.get("unmasked_diffs") or [])
            lines.append("")
        lines.extend(diffs)
    (base / "diff.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    if not args.quiet:
        print(f"噪声自证：{'一致' if noise['identical'] else '不一致（掩码不足）'}"
              f"（run-2 {'存在' if old2 else '缺失'}）")
        print(f"掩码命中（旧+新）：{fired}")
        print(f"未命中掩码：{'、'.join(dormant) or '（无）'}")
        print(f"未掩码即有差异的命令：{'、'.join(reading['raw_diff_slugs']) or '（无）'}")
        print(f"判定：{verdict}")
        print(f"报告：{base / 'noise.json'} / {base / 'diff.txt'}")
    return 0 if verdict == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
