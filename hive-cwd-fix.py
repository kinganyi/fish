# -*- coding: utf-8 -*-
"""修 dsh-hive 的 {{cwd}} 未注册变量（0.2.1 会因此整轮失败，0.2.0 宽容）。

默认 --dry-run：只显示会改什么，不动文件。
--apply       ：先备份再改。
--verify      ：检查两个 preset 里还有没有 {{cwd}}。

改法：把 `suffix: Your working directory is {{cwd}}.` 换成不含变量的静态句
（persona 的 suffix 是可选装饰，去掉不损失能力；{{model}} 保留，它是宿主注册的）。
"""
import argparse
import shutil
from datetime import datetime
from pathlib import Path

HIVE = Path(r"I:\app\DSH-Home\.dsh-data\plugins\dsh-hive")
FILES = [HIVE / "preset" / "dafish-team.patch.yml", HIVE / "preset" / "preset.patch.yml"]

OLD = "suffix: Your working directory is {{cwd}}."
NEW = "suffix: Work from the session working directory; prefer absolute paths."
STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true")
    g.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    hits = []
    for f in FILES:
        if not f.exists():
            print(f"[MISS] {f}")
            continue
        text = f.read_text(encoding="utf-8")
        if OLD in text:
            hits.append(f)
            line_no = next(i + 1 for i, ln in enumerate(text.split("\n")) if OLD in ln)
            print(f"[HIT ] {f.name}:L{line_no}")
        else:
            print(f"[skip] {f.name} 不含目标行")

    if args.verify:
        print(f"\n=== verify: 剩余 {{{{cwd}}}} 出现次数 ===")
        total = 0
        for f in FILES:
            if f.exists():
                n = f.read_text(encoding="utf-8").count("{{cwd}}")
                total += n
                print(f"  {f.name}: {n}")
        print(f"  合计: {total}  {'✅ 已清零' if total == 0 else '❌ 仍有残留'}")
        return

    if not hits:
        print("\n无需改动。")
        return

    if not args.apply:
        print(f"\n=== DRY-RUN：将把 {len(hits)} 个文件里的这一行替换 ===")
        print(f"  旧: {OLD}")
        print(f"  新: {NEW}")
        print("\n确认无误后加 --apply 执行（会先备份为 *.bak-cwdfix-<ts>）。")
        return

    for f in hits:
        bak = f.with_suffix(f.suffix + f".bak-cwdfix-{STAMP}")
        shutil.copy2(f, bak)
        text = f.read_text(encoding="utf-8")
        text = text.replace(OLD, NEW)
        f.write_text(text, encoding="utf-8")
        print(f"[OK] {f.name} 已改（备份 {bak.name}）")
    print("\n完成。重启 DSH 后生效。")


if __name__ == "__main__":
    main()
