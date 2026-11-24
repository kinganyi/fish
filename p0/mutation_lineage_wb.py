#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W3（WorkBuddy 适配器）判据的变异自证（113 §4.3，2026-11-25）。

## 为什么 W3 要变异

W3 的判据几乎全是"**例外为 0**"与"来源对不对"：`basis='default'` = 0、
`origin='main' AND agent_id LIKE 'agent-%'` = 0、成员之间不互为父、挪出 `subagents/`
必须 `unknown/missing`。这类判据最容易被写成**恒绿**——过滤条件写错一格、`set()`
比错了对象，都会安静地过。所以每条都要先在坏输入下红一次，再还原复绿。

## 一个数据级 + 五个源码级

  D0（数据级，不是源码变异）
      临时树里把一份 transcript **挪出 `subagents/`**（真搬，不是打桩）
      → 该行必须 `unknown/missing`、**不得 main**；共用判据（`assert_missing_rows_are_not_main`）
      必须受得住；把同一行伪造成 `main` 喂给同一条判据 → **必须红**（证明它有牙）。
      顺带把"主键集合"的两种口径都打出来：文件名 stem（真）vs 首行 `sessionId`（假的）。

  M1  `session_id` 改成首行 `sessionId`            → J-W3-id 红（主键集合变）
  M2  `BASIS_PATH` 常量改成 `"default"`            → J3③ 红（basis 越界）
  M3  runtime 分支短路（`if False and …`）         → 成员父不再是 lead（J3①/兄弟 红）
  M4  判不到的兜底改成 `main`                       → J3③ 红（挪出去的行变 main）
  M5  无 transcript 成员的 `agent_id` 拿 `agentId` 顶上 → 退化② 判据红

用法：`python -X utf8 mutation_lineage_wb.py [--only D0|M1..M5] [--data-only] [--src-only]`
"""
import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from mutation_source import SourceText

HERE = Path(__file__).resolve().parent
WB_PY = HERE / "fish_lineage_wb.py"
TEST = "test_lineage_wb.py"

#: D0 用的假宿主常量（与真宿主无关：这是一棵临时树）
LEAD = "12345678-1234-4234-8234-123456789abc"
STEM = "agent-0123456789abcdef"
HEADER_SESSION_ID = "01a1e955-3697-7bcc-9556-efe6b8e0e695"

# (名字, 文件, -k 选择器, 靶点原文, 变异后, 打的是哪条判据)
MUTANTS = [
    ("M1", WB_PY, "j3_primary_key_set_equals_transcript_stems",
     "    if header is None:\n        header = read_header(path)\n",
     "    if header is None:\n        header = read_header(path)\n"
     "    if (header or {}).get(\"sessionId\"):\n"
     "        stem = str(header[\"sessionId\"])\n",
     "J-W3-id：id 来源从文件名漂到首行 sessionId（742 行主键会整批漂移）"),
    ("M2", WB_PY, "j3_basis_path_when_runtime",
     'BASIS_PATH = "path"\n',
     'BASIS_PATH = "default"\n',
     "J3③：basis 兜了个 default（113 §4.2 明令禁止）"),
    ("M3", WB_PY, "j3_runtime_member_gets_lead_parent",
     '    if member is not None and member["lead"]:\n',
     '    if False and member is not None and member["lead"]:\n',
     "J3①②：runtime 优先级被短路，成员父掉回路径父"),
    ("M4", WB_PY, "j3_stray_transcript_is_missing",
     '    decision["reason"] = "not_in_subagents"\n    return decision\n',
     '    decision["reason"] = "not_in_subagents"\n'
     '    decision.update(origin="main", basis=BASIS_MISSING)\n'
     "    return decision\n",
     'J3③：判不到兜回 main（fish_corpus_tag.py:240 的反面教材）'),
    ("M5", WB_PY, "j3_member_without_transcript_has_no_agent_id",
     '        "agent_id": stem,                                         # ← 只有文件名能当 agent_id\n',
     '        "agent_id": stem or agent_key,\n',
     "退化②：无 transcriptPath 的成员拿 agentId 标签顶了 agent_id"),
]


def _run(k):
    """跑一个测试子集：返回 (是否绿, 末行摘要)。"""
    r = subprocess.run([sys.executable, "-X", "utf8", "-m", "pytest", TEST, "-q",
                        "--no-header", "-p", "no:cacheprovider", "-k", k],
                       cwd=str(HERE), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=900)
    out = (r.stdout or "") + (r.stderr or "")
    tail = [x for x in out.strip().split("\n") if x.strip()][-1:] or ["(无输出)"]
    return r.returncode == 0, tail[0].strip()[:120]


def _build_tree(root: Path) -> Path:
    """一棵最小 WB 树：父会话目录 + 一份 transcript + sessions 行（父必须能印证）。"""
    sub = root / "projects" / "ws-one" / LEAD / "subagents"
    sub.mkdir(parents=True)
    path = sub / f"{STEM}.jsonl"
    path.write_text(json.dumps({"type": "message", "role": "user",
                                "sessionId": HEADER_SESSION_ID, "cwd": "/w"},
                               ensure_ascii=False) + "\n", encoding="utf-8")
    (root / "teams").mkdir(exist_ok=True)
    conn = sqlite3.connect(str(root / "workbuddy.db"))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, updated_at INTEGER)")
    conn.execute("INSERT INTO sessions VALUES (?,?,?)", (LEAD, "/w", 1795000000000))
    conn.commit()
    conn.close()
    return path


def data_mutation() -> bool:
    """D0：把 transcript 挪出 `subagents/` → 必须 unknown/missing，且判据对 main 有牙。"""
    import fish_lineage_wb as wb
    import test_lineage_wb as T          # 与测试**同一份**判据

    tmp = tempfile.mkdtemp(prefix="fish-w3-d0-")
    ok = True
    try:
        root = Path(tmp) / "wb"
        path = _build_tree(root)
        runtime = wb.load_runtime(root)
        sessions = wb.load_sessions(root)
        case = unittest.TestCase()

        def scan_by_id():
            return {node["session_id"]: node
                    for node in wb.scan(root=root, runtime=runtime, sessions=sessions)}

        # ---- 对照组：还在 subagents/ 下 → basis=path、父=目录
        node = scan_by_id()[STEM]
        try:
            assert node["basis"] == "path" and node["parent_session"] == LEAD, \
                f"对照组就红了：{node['basis']} / {node['parent_session']}"
            T.assert_basis_allowed(case, [node], "【D0 对照组】")
            print("  D0      ok 对照组绿（basis=path，父=目录）")
        except AssertionError as exc:
            print(f"  D0      x 对照组就红了：{exc}")
            return False
        # 主键的两种口径（真 vs 假）：首行 sessionId 与文件名 stem 不同 → 换来源主键必变
        print(f"  D0      ok 主键口径：文件名 stem={STEM} / 首行 sessionId={HEADER_SESSION_ID} "
              f"→ 两者{'不同' if STEM != HEADER_SESSION_ID else '相同（判据无意义！）'}")
        if STEM == HEADER_SESSION_ID:
            ok = False

        # ---- 真搬：移出 subagents/（同一磁盘上的 rename，不是打桩）
        moved = path.parent.parent / path.name
        path.rename(moved)
        node = scan_by_id()[STEM]
        try:
            T.assert_missing_rows_are_not_main(case, node, "【D0 挪出后】")
            print("  D0      ok 挪出后变红成 unknown/missing（不是 main）")
        except AssertionError as exc:
            print(f"  D0      x 挪出后仍是别的来源：{exc}")
            ok = False
        if node["basis"] == "path":
            print("  D0      x 挪出后仍按 path 判父——stray 没被捕获")
            ok = False

        # ---- 反向自证：同一行伪造成 main，共用判据**必须**拒绝
        fake = dict(node)
        fake.update(origin="main", basis="missing")
        try:
            T.assert_missing_rows_are_not_main(case, fake, "【D0 main 伪造】")
            print("  D0      x 判据对 main 无牙（伪造成 main 仍然绿）")
            ok = False
        except AssertionError:
            print("  D0      ok 判据对 main 有牙（伪造成 main 立刻红）")

        # ---- 移回：必须复绿
        moved.rename(path)
        node = scan_by_id()[STEM]
        try:
            assert node["basis"] == "path" and node["parent_session"] == LEAD
            print("  D0      ok 移回复绿")
        except AssertionError as exc:
            print(f"  D0      x 移回后没复绿：{node['basis']} / {exc}")
            ok = False
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def source_mutants(picked):
    fails = []
    for name, path, k, old, new, why in picked:
        print(f"  {name}  [{path.name} / -k {k}]")
        print(f"       打的是：{why}")
        green, tail = _run(k)
        if not green:
            print(f"       x 对照组就红了（判据本身有问题）：{tail}")
            fails.append(name)
            continue
        src = SourceText(path)
        before = src.sha256()
        try:
            try:
                src.write(src.replaced(old, new))
            except AssertionError as exc:
                print(f"       x 靶点未命中：{exc}")
                fails.append(name)
                continue
            mut_green, mut_tail = _run(k)
            if mut_green:
                print("       x 变异后仍绿——这条判据没有判别力")
                fails.append(name)
            else:
                print(f"       ok 变红：{mut_tail}")
        finally:
            src.restore()
        if not (src.sha256() == before and src.restored()):
            print("       x 还原失败：工作树被变异体污染，立刻停机检查")
            fails.append(name)
            continue
        back_green, back_tail = _run(k)
        if back_green:
            print("       还原复绿")
        else:
            print(f"       x 还原后仍红：{back_tail}")
            fails.append(name)
    return fails


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="只跑某个变异体（D0 / M1..M5）")
    ap.add_argument("--data-only", action="store_true")
    ap.add_argument("--src-only", action="store_true")
    a = ap.parse_args(argv)

    fails = []
    print("\n=== W3 变异自证（113 §4.3）===")
    if not a.src_only:
        print("\n[D0] 数据级变异：把 transcript 挪出 subagents/（临时树）")
        if a.only and a.only != "D0":
            print("  D0      跳过（--only 指定了别的）")
        elif not data_mutation():
            fails.append("D0")
    if not a.data_only:
        picked = [m for m in MUTANTS if not a.only or m[0] == a.only]
        if picked:
            print("\n[源码级变异]")
            fails.extend(source_mutants(picked))

    print()
    if fails:
        print(f"  失败：{fails}")
        return 1
    print("  全部变异体通过（红→还原→绿 三步都成立）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
