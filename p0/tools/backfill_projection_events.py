#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2-2：给 projection-only 的 `code_index` 行补写 `memory.imported` 事件。

## 问题

生产 `code_index` 有若干行没有任何 `memory.recorded` 事件（审查按 rename map
归一后 4 条）。`rebuild_projection` 目前靠 §4b「快照补回」——快照丢了这些行就没了，
"事件是唯一真相、投影可整表重建"在那几条上是假的。

## 做法

为每条 projection-only 行**追加**一条 `memory.imported` 事件（append-only，不改历史）：

    payload = code / title / body_ref / body_sha256 / owner / status
              （另带重建所需的 mem_type / visibility / tags / occurred_at）
    cause   = backfill:p2-2

判据：dry-run 零写；apply 后事件出现、投影行仍在；rebuild 不依赖快照也能保留该行；
第二次 apply delta=0（幂等）。

## 用法

    python p0/tools/backfill_projection_events.py              # dry-run（默认，零写）
    python p0/tools/backfill_projection_events.py --apply      # 写前 conn.backup() + 追加
    python p0/tools/backfill_projection_events.py --json       # 机器可读

退出码：0 = 正常（dry-run 有待补行也返回 0）；1 = 有正文字段缺失、跳过该行；
2 = 判不到（库不存在 / 打不开）——**判不到不是通过**。
"""
import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
P0 = HERE.parent
DEFAULT_DATA = P0 / "data"
sys.path.insert(0, str(P0))

import fish  # noqa: E402
import fish_atomic  # noqa: E402


def _rename_map(conn):
    """`code.renamed.batch` 的最终映射（同 rebuild：后写覆盖先写）。"""
    out = {}
    for (payload_s,) in conn.execute(
            "SELECT payload FROM events WHERE type='code.renamed.batch' ORDER BY seq"):
        try:
            mapping = json.loads(payload_s).get("mapping") or {}
        except (ValueError, TypeError):
            mapping = {}
        for old, new in mapping.items():
            if old and new:
                out[old] = new
    return out


def _covered_codes(conn, rename):
    """已有 `memory.recorded` / `memory.imported` 覆盖的编码（按 rename 归一）。"""
    covered = set()
    for ev_type in ("memory.recorded", "memory.imported"):
        for (payload_s,) in conn.execute(
                "SELECT payload FROM events WHERE type=?", (ev_type,)):
            try:
                code = json.loads(payload_s).get("code")
            except (ValueError, TypeError):
                code = None
            if code:
                covered.add(rename.get(code, code))
    return covered


def _load_tags(raw):
    try:
        tags = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return tags if isinstance(tags, list) else []


def collect(conn, data_root):
    """列出 projection-only 行（含可计算的 body_sha256）；纯读，不写一个字节。"""
    data_root = Path(data_root)
    rename = _rename_map(conn)
    covered = _covered_codes(conn, rename)
    rows = []
    for code, title, owner, status, file_path, mem_type, visibility, tags, occurred in conn.execute(
            "SELECT code,title,owner,status,file_path,mem_type,visibility,tags,occurred_at "
            "FROM code_index ORDER BY code"):
        if code in covered:
            continue
        body = (data_root / file_path) if file_path else None
        sha = None
        if body is not None and body.is_file():
            sha = fish_atomic.sha256_of_file(body)
        rows.append({
            "code": code, "title": title, "owner": owner, "status": status,
            "file_path": file_path, "mem_type": mem_type, "visibility": visibility,
            "tags": _load_tags(tags), "occurred_at": occurred, "body_sha256": sha,
        })
    return rows


def _backup(conn, data_root):
    """写前快照（SQLite backup API，WAL 也拿到一致快照）。返回 (路径, 行数)。"""
    if conn.in_transaction:
        conn.commit()
    out_dir = Path(data_root) / "_backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"index-before-backfill-p2-2-{time.strftime('%y%m%d-%H%M%S')}.sqlite"
    dst = sqlite3.connect(str(dest))
    try:
        conn.backup(dst)
        rows = dst.execute("SELECT COUNT(*) FROM code_index").fetchone()[0]
    finally:
        dst.close()
    return str(dest), rows


def _emit(result, as_json):
    if as_json:
        print(json.dumps(result, ensure_ascii=False))
        return
    print(f"P2-2 projection-only 扫描：{result['data_root']}")
    for i, row in enumerate(result["rows"], 1):
        sha = (row["body_sha256"] or "（正文缺失，跳过）")[:16]
        print(f"  [{i}] {row['code']} | {row['title']} | {row['owner']} | "
              f"{row['status']} | sha256={sha}")
    print(f"共 {result['count']} 条")
    if result["mode"] == "dry-run":
        print("[dry-run] 未写入任何字节（加 --apply 才写）")
    else:
        if result.get("backup"):
            print(f"写前备份：{result['backup']}（{result.get('backup_rows')} 行）")
        print(f"已追加 memory.imported 事件 {result['written']} 条"
              f"（delta={result['written']}）")
    if result["skipped_missing_body"]:
        print("⚠ 正文缺失、未补事件：" + "、".join(result["skipped_missing_body"]))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="给 projection-only 的 code_index 行补写 memory.imported 事件（P2-2）")
    ap.add_argument("--data", default=str(DEFAULT_DATA), help="数据根（默认 p0/data）")
    ap.add_argument("--apply", action="store_true", help="真正写入（缺省 dry-run，零写）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args(argv)
    data = Path(args.data)
    db = data / "index.sqlite"
    if not db.exists():
        print(f"判不到：{db} 不存在（不是通过）", file=sys.stderr)
        return 2

    result = {"mode": "apply" if args.apply else "dry-run", "data_root": str(data),
              "count": 0, "rows": [], "written": 0, "backup": None,
              "backup_rows": None, "skipped_missing_body": []}
    if not args.apply:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            result["rows"] = collect(conn, data)
        except sqlite3.Error as exc:
            print(f"判不到：读取失败 {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        finally:
            conn.close()
        result["count"] = len(result["rows"])
        _emit(result, args.json)
        return 0

    conn = fish.connect(data)
    try:
        result["rows"] = collect(conn, data)
        result["count"] = len(result["rows"])
        writable = [r for r in result["rows"] if r["body_sha256"]]
        result["skipped_missing_body"] = [
            r["code"] for r in result["rows"] if not r["body_sha256"]]
        if writable:
            result["backup"], result["backup_rows"] = _backup(conn, data)
            for row in writable:
                fish.append_event(
                    conn, "memory.imported",
                    {"citizen_id": row["owner"] or "system", "evidence_level": "machine"},
                    {"kind": "memory", "id": row["code"]},
                    {"code": row["code"], "title": row["title"],
                     "body_ref": row["file_path"], "body_sha256": row["body_sha256"],
                     "owner": row["owner"], "status": row["status"],
                     "mem_type": row["mem_type"], "visibility": row["visibility"],
                     "tags": row["tags"], "occurred_at": row["occurred_at"]},
                    cause="backfill:p2-2", commit=False)
                result["written"] += 1
            conn.commit()
    finally:
        conn.close()
    _emit(result, args.json)
    return 1 if result["skipped_missing_body"] else 0


if __name__ == "__main__":
    sys.exit(main())
