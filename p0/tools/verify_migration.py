#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""迁移后校验：`code_index` 里每条 `file_path` 是否真的存在于 data 根下。

## 为什么需要它（2026-11-23 复查 BUG-3）

2026-11-20 的复制迁移漏搬了 318 条正文（10-28~11-18 批次），直到事后按日期分布才
发现。**迁移流程当时没有"逐条核文件存在性"这一步**，所以"搬完看起来成功"与"真的
搬全"是两件事。

这条工具就是那一步的判据：**active 缺失数必须为 0，否则退出码 1**。
非 active（withdrawn/superseded）的缺失单独列出——它们是历史留痕，不算迁移失败。

用法（只读）：
    python p0/tools/verify_migration.py                # 默认查 p0/data
    python p0/tools/verify_migration.py --data <data>  # 换数据根
    python p0/tools/verify_migration.py --json         # 机器可读

退出码：
    0 = active 缺失 0（迁移通过）
    1 = 有 active 缺失（迁移失败，必须回查）
    2 = 库不存在 / 读取失败（判不到，不是通过）
"""
import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DATA = HERE.parent / "data"


def verify(data_root):
    data = Path(data_root)
    db = data / "index.sqlite"
    if not db.exists():
        raise FileNotFoundError(f"index.sqlite 不存在：{db}（判不到，不是通过）")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT code, project, file_path, status, recorded_at FROM code_index "
        "WHERE file_path IS NOT NULL AND file_path != ''"
    ).fetchall()
    active_missing, other_missing = [], []
    for code, project, fp, status, recorded_at in rows:
        try:
            ok = (data / fp).exists()
        except OSError:
            ok = False
        item = {"code": code, "project": project, "file_path": fp,
                "status": status, "recorded_at": recorded_at}
        if not ok:
            (active_missing if status == "active" else other_missing).append(item)
    by_date = Counter((x["recorded_at"] or "")[:10] for x in active_missing)
    by_project = Counter(x["project"] or "(未分类)" for x in active_missing)
    return {
        "data_root": str(data),
        "total_with_path": len(rows),
        "active_missing": active_missing,
        "non_active_missing": other_missing,
        "active_missing_by_date": dict(sorted(by_date.items())),
        "active_missing_by_project": dict(by_project.most_common()),
        "ok": len(active_missing) == 0,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="迁移后校验：active 记忆正文必须全部存在")
    ap.add_argument("--data", default=str(DEFAULT_DATA), help="data 根（含 index.sqlite）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args(argv)
    try:
        res = verify(args.data)
    except Exception as exc:  # noqa: BLE001
        print(f"判不到：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"data={res['data_root']}")
        print(f"有路径的记录={res['total_with_path']}")
        print(f"active 缺失={len(res['active_missing'])}"
              f"（按日期 {res['active_missing_by_date']}；按项目 {res['active_missing_by_project']}）")
        print(f"非 active 缺失={len(res['non_active_missing'])}（历史留痕，不算迁移失败）")
        for x in res["active_missing"][:20]:
            print("  ACTIVE_MISSING", x["code"], x["file_path"])
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
