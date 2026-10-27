#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公开包导出（P1-2）：复制 + 排除 + 逐行校验 + 报告。

## 为什么要有它

公开包此前靠手工 robocopy + 手改文档：换行差异、漏排除、数字漂移全靠人盯。
本脚本把"导出"变成**可重复、可校验、可判据**的动作：

  · 只导出代码与说明文档（`*.py` / `*.md`）；
  · 硬排除运行时目录/文件（`data/`、`_audit/`、`历史版本/`、`_golden/`、身份、库、日志）；
  · 复制后**逐行回读校验**（换行归一比较，内容少一行就报）；
  · 输出 JSON 报告（文件清单 / 错误 / 含本机路径的文件——公开包 README 已声明"路径示例需调整"）。

## 用法

    python scripts/export_public.py --src . --dest <目标目录>
    python scripts/export_public.py --src . --dest <目标目录> --check-only   # 只校验
    python scripts/export_public.py --src . --dest <目标目录> --report <json>

退出码：0 = 通过；2 = 校验失败（有 missing/content/excluded-present）。
"""
import argparse
import fnmatch
import json
import re
import sys
from pathlib import Path

# 硬排除：这些目录下的东西**永不**进公开包
EXCLUDE_DIRS = {"data", "_audit", "历史版本", "_golden", "__pycache__", ".pytest_cache"}
# 硬排除：按文件名/后缀
EXCLUDE_FILES = ("fish_identity.json", "*.pyc", "*.pyo", "*.sqlite",
                 "*.sqlite-wal", "*.sqlite-shm", "*.log", "*.bak")
# 只导出这两类（代码 + 说明）
INCLUDE_GLOBS = ("*.py", "*.md")

# 本机路径模式（只报告，不替换——公开包 README 已声明"路径示例需调整"）
LOCAL_PATH_RE = re.compile(
    r"I:\\app\\DSH-Home|C:\\Users\\15596|E:\\Obsidian|[A-Za-z]:\\(?:Users|app)\\[^\s\"']{0,80}")


def iter_sources(src: Path):
    """产出 (绝对路径, 相对路径)：过滤排除目录/文件，只留代码与 md。"""
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(src)
        if set(rel.parts[:-1]) & EXCLUDE_DIRS:
            continue
        if any(fnmatch.fnmatch(rel.name, pat) for pat in EXCLUDE_FILES):
            continue
        if not any(fnmatch.fnmatch(rel.name, pat) for pat in INCLUDE_GLOBS):
            continue
        yield path, rel


def copy_tree(src: Path, dest: Path) -> list:
    written = []
    for path, rel in iter_sources(src):
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())   # 逐字节（保留源行尾）
        written.append(str(rel).replace("\\", "/"))
    return written


def iter_dest(dest: Path):
    for path in sorted(dest.rglob("*")):
        if path.is_file():
            yield path


def verify(src: Path, dest: Path) -> dict:
    errors = []
    local_paths = []
    sources = list(iter_sources(src))
    expected_rel = {rel.as_posix() for _, rel in sources}
    expected = len(sources)
    for path, rel in sources:
        target = dest / rel
        if not target.exists():
            errors.append(f"missing: {rel.as_posix()}")
            continue
        a = path.read_text(encoding="utf-8", errors="replace").splitlines()
        b = target.read_text(encoding="utf-8", errors="replace").splitlines()
        if a != b:
            errors.append(f"content: {rel.as_posix()}")
        hits = LOCAL_PATH_RE.findall(
            target.read_text(encoding="utf-8", errors="replace"))
        if hits:
            local_paths.append({"file": rel.as_posix(), "hits": len(hits)})
    # 目标树里不许出现被排除的目录/文件（防"手抖拷进去"）
    for bad_dir in EXCLUDE_DIRS:
        if (dest / bad_dir).exists():
            errors.append(f"excluded-present: {bad_dir}/")
    for path in iter_dest(dest):
        if any(fnmatch.fnmatch(path.name, pat) for pat in EXCLUDE_FILES):
            errors.append(f"excluded-present: {path.relative_to(dest).as_posix()}")
    # 目标树有、源树没有的文件（只报告，需人工确认是否该删）
    extra = [p.relative_to(dest).as_posix() for p in iter_dest(dest)
             if p.relative_to(dest).as_posix() not in expected_rel]
    if extra:
        errors.append(f"extra-files: {len(extra)}（目标树有源树没有的文件，需人工确认）")
    return {"expected_files": expected, "errors": errors,
            "local_path_files": local_paths, "extra_files": extra[:50]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="公开包导出（复制 + 排除 + 校验 + 报告）")
    ap.add_argument("--src", default=".", help="源目录（p0 工作目录）")
    ap.add_argument("--dest", required=True, help="公开包 p0 目标目录")
    ap.add_argument("--check-only", action="store_true", help="只校验目标树（不复制）")
    ap.add_argument("--report", default="", help="JSON 报告路径")
    args = ap.parse_args(argv)
    src, dest = Path(args.src).resolve(), Path(args.dest).resolve()
    if not src.exists():
        print(f"源目录不存在：{src}", file=sys.stderr)
        return 1
    if src == dest or src in dest.parents:
        print("目标目录不能是源目录或其子目录", file=sys.stderr)
        return 1
    written = [] if args.check_only else copy_tree(src, dest)
    result = verify(src, dest)
    report = {"src": str(src), "dest": str(dest), "check_only": bool(args.check_only),
              "written": written if not args.check_only else [],
              "recorded_files": len(written), **result}
    print(f"导出：{report['recorded_files']} 个文件 → {dest}")
    print(f"校验：expected={result['expected_files']} errors={len(result['errors'])}")
    for err in result["errors"][:20]:
        print(f"  ✗ {err}")
    if result["local_path_files"]:
        print(f"含本机路径的文件（README 已声明需自行调整）："
              f"{len(result['local_path_files'])} 个")
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"报告：{args.report}")
    return 0 if not result["errors"] else 2


if __name__ == "__main__":
    sys.exit(main())
