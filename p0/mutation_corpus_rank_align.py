#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异自证 · T03a（语料候选与 Ranker 对齐 · 2026-10-27 · **R2 收口轮**）。

## 为什么要有它

判据"读了绿"和"能红"是两件事。本脚本把实现逐条改坏，证明对应判据真的会抓到，
然后逐字节还原（`mutation_source.SourceText` 的道具）。

| 变异体 | 改坏什么 | 必须红的判据 |
|---|---|---|
| **M10** | 从 `score_breakdown` 里删掉一项（`project_match`） | C1 结构齐全 |
| **M11** | 给 `DefaultRanker.rank` 注入随机数 | C2 两遍一致 |
| **M12** | 候选层的 `count=False` 改回 `count=True`（自己又统计一遍全库） | **C5 结构闸**（SQL ⊆ 裸检索） |
| **M13** | 候选层把重复度读数写死 0（负向特征永不生效） | C1 全局重复度（A） |
| **M14** | 候选的 `freshness` 退回 float（= 首轮原状，同名异型） | C1 freshness 同型（B） |
| **M15** | `semantic_kind` 忽略 `kind_class`（归一入口失效） | C6 三类映射（D） |

**R2 的三处变化**（首轮 QA Y6 / 评审 S5）：

1. **M12 不再依赖真库**：C5 的主闸已改成"候选层 SQL ⊆ `search(count=False)`"的
   结构判据，并在**合成 1000 段**夹具里常开跑——所以"抓没抓到"是确定的，不是
   墙钟抽签（首轮 M12 的红与否取决于当时机器负载）；
2. **判定改为认具体用例**：必须出现 `FAIL:`/`ERROR:` 且**行里就是那条指定用例**，
   `rc != 0` 不再够用（被跳过的用例 rc 也是 0）；
3. **不再有"判不到也算 exit 0"**：本轮没有任何变异体依赖环境变量，出现"没跑"就
   直接非零退出（`--strict` 变成默认行为）。

驱动：cd p0 && python mutation_corpus_rank_align.py   → exit 0 = 全部被抓
"""
import hashlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from mutation_source import SourceText, to_lf  # noqa: E402

MODULE = "test_corpus_rank_align"


def _mutate(src, pairs) -> str:
    """按顺序应用多个靶点（每个都要求命中恰好 1 次）；返回 LF 文本。

    为什么要支持多靶点（M14）：一次"退回 float"要同时改**两个**出口（候选字段与
    scorer 路径的 `setdefault`），只改一处会先被 `dict(...)` 打崩、把变异变成
    "什么都红"，那就证明不了判据本身在守什么。多靶点让变异精确等价于**首轮原状**。
    """
    text = src.text
    for old, new in pairs:
        hits = text.count(to_lf(old))
        if hits != 1:
            raise AssertionError("%s 里变异片段命中 %d 次（要求恰好 1 次）：%r"
                                 % (src.path.name, hits, old[:60]))
        text = text.replace(to_lf(old), to_lf(new), 1)
    return text


def _pairs(*specs) -> list:
    """`(old, new)` 两元组列表（顺序应用）。"""
    return list(specs)

MUTANTS = [
    {
        "id": "M10",
        "file": "fish_corpus.py",
        "desc": "score_breakdown 少一项（project_match）",
        "pairs": _pairs((
            '''            "project_match": rank_mod.project_match(hit.get("project"), project),\n''',
            "")),
        "must_fail": ["%s.CandidateShapeTest.test_c1_item_has_kind_class_score_and_breakdown"
                      % MODULE],
        "others": ["test_corpus_push"],
    },
    {
        "id": "M11",
        "file": "fish_corpus_rank.py",
        "desc": "DefaultRanker 注入随机性（第一阶段不再可复算）",
        "pairs": _pairs((
            '''            scored.append((cand["pointer"], round(score, 6), why))''',
            '''            scored.append((cand["pointer"], round(
                score + __import__("random").uniform(0.0, 0.05), 6), why))''')),
        "must_fail": ["%s.RankerDeterminismTest.test_c2_default_ranker_is_deterministic"
                      % MODULE],
        "others": ["test_corpus_push"],
    },
    {
        "id": "M12",
        "file": "fish_corpus.py",
        "desc": "候选层把 count 打开（自己又统计一遍全库命中）",
        "pairs": _pairs((
            '''                 # 只要 top-K：不统计全库总数（5 万段库上那一次 COUNT ≈ 26ms，白付）
                 count=False)''',
            '''                 count=True)''')),
        "must_fail": ["%s.CandidateSqlShapeTest.test_c5_candidate_layer_sql_is_subset_of_search"
                      % MODULE],
        "others": ["test_corpus_push"],
    },
    {
        "id": "M13",
        "file": "fish_corpus.py",
        "desc": "候选层把重复度读数写死 0（dup_penalty 永不生效）",
        "pairs": _pairs((
            '''        dup_count = int(dup_count_map.get(code, 0)) if dup_count_map else 0''',
            '''        dup_count = 0''')),
        "must_fail": ["%s.CandidateShapeTest.test_c1_dup_penalty_is_global_repetition"
                      % MODULE],
        "others": ["test_corpus_push"],
    },
    {
        "id": "M14",
        "file": "fish_corpus.py",
        "desc": "候选的 freshness 退回 float（= 首轮原状，与 mem 侧同名异型）",
        "pairs": _pairs(
            ('''                      "freshness": dict(freshness),''',
             '''                      "freshness": freshness["factor"],'''),
            ('''        cand.setdefault("freshness", dict(item["freshness"]))''',
             '''        cand.setdefault("freshness", item["freshness"])''')),
        "must_fail": ["%s.CandidateShapeTest.test_c1_freshness_has_the_same_dict_shape_in_all_classes"
                      % MODULE],
        "others": ["test_corpus_push"],
    },
    {
        "id": "M15",
        "file": "fish_corpus_rank.py",
        "desc": "semantic_kind 忽略 kind_class（三类归一的优先级失效）",
        "pairs": _pairs((
            '''    if kind_class in KIND_CLASSES:''',
            '''    if False and kind_class in KIND_CLASSES:  # 变异：忽略已归一的字段''')),
        "must_fail": ["%s.SemanticKindTest.test_c6_kind_class_wins_and_candidates_are_self_consistent"
                      % MODULE],
        "others": ["test_corpus_push"],
    },
]


def _run(*specs):
    cmd = [sys.executable, "-m", "unittest", *specs]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(cmd, cwd=str(HERE), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, timeout=1800)
    name = "-".join(spec.split(".")[-1] for spec in specs)[:80]
    log = Path(LOG_DIR) / ("%s.log" % name)
    log.write_text(
        "$ python -m unittest %s\nexit=%d\n\n---- stdout ----\n%s\n---- stderr ----\n%s"
        % (" ".join(specs), proc.returncode, proc.stdout, proc.stderr), encoding="utf-8")
    tail = (proc.stderr or proc.stdout).strip().splitlines()[-6:]
    return proc.returncode, tail, proc.stdout + "\n" + proc.stderr


def _outcomes(out):
    """从 unittest 输出里取 `[(FAIL|ERROR, 用例 id), ...]` —— 只认具体用例。"""
    found = []
    for line in out.splitlines():
        text = line.strip()
        for kind in ("FAIL:", "ERROR:"):
            if text.startswith(kind):
                found.append((kind[:-1], text[len(kind):].strip()))
    return found


def _skipped_count(out) -> str:
    """unittest 的 `OK (skipped=N)` / `FAILED (... skipped=N)` → N（自己数，不猜）。"""
    match = re.search(r"skipped=(\d+)", out)
    return match.group(1) if match else "0"


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    print("=" * 74)
    print("T03a · 变异自证（M10 / M11 / M12 / M13 / M14 / M15 · R2）")
    print("=" * 74)
    print("\n[0/3] 基线：%s 必须全绿（真库读数是可选的，不影响本脚本）" % MODULE)
    rc, tail, out = _run(MODULE)
    print("  %-24s %s" % (MODULE, "绿 ✓" if rc == 0 else "红 ✗ (rc=%d)" % rc))
    if rc != 0:
        print("基线不是全绿——判据本身有问题，终止。", file=sys.stderr)
        print("\n".join(tail), file=sys.stderr)
        return 1
    skipped = [line for line in out.splitlines() if line.startswith("[C5")]
    for line in skipped:
        print("  " + line)
    print("  跳过（判不到，不算通过）：%s" % _skipped_count(out))

    failures = []
    print("\n[1/3] 逐个变异 → 指定判据必红 → 其它判据不许新红 → 逐字节还原")
    for mutant in MUTANTS:
        path = HERE / mutant["file"]
        base_sha = _sha(path)
        src = SourceText(path)
        try:
            try:
                src.write(_mutate(src, mutant["pairs"]))
            except AssertionError as exc:
                print("\n%s 靶点失效：%s" % (mutant["id"], exc), file=sys.stderr)
                failures.append((mutant["id"], "靶点失效"))
                continue
            caught, clean = True, True
            for spec in mutant["must_fail"]:
                rc, tail, out = _run(spec)
                wanted = spec.split(".")[-1]
                outcomes = _outcomes(out)
                hit = [kind for kind, test_id in outcomes if test_id.split(" ")[0] == wanted
                       or test_id.startswith(wanted)]
                if rc == 0 or not hit:
                    caught = False
                    print("\n    ↳ %s 变异后没有被指定判据抓到：rc=%d，输出里的失败用例=%r"
                          % (spec, rc, [test_id for _kind, test_id in outcomes][:3]))
                else:
                    print("    ↳ %s %s ✓（%s）" % (wanted, hit[0],
                                                [test_id for _kind, test_id in outcomes][:1]))
            for other in mutant["others"]:
                rc, tail, _out = _run(other)
                if rc != 0:
                    clean = False
                    print("\n    ↳ %s 出现预期外新红：%s" % (other, tail))
            print("%s %s %s" % (mutant["id"], "CAUGHT" if caught and clean else "MISSED",
                                mutant["desc"]))
            if not caught:
                failures.append((mutant["id"], "指定判据没红"))
            if not clean:
                failures.append((mutant["id"], "其它判据出现新红"))
        finally:
            src.restore()
            restored = _sha(path) == base_sha
            print("    还原：sha256 %s（%s）"
                  % ("一致 ✓" if restored else "不一致 ✗", path.name))
            if not restored:
                return 2
    print("\n[2/3] 还原后复跑")
    rc, tail, out = _run(MODULE)
    print("  %-24s %s" % (MODULE, "绿 ✓" if rc == 0 else "红 ✗ (rc=%d)" % rc))
    if rc != 0:
        failures.append((MODULE, "还原后仍红"))

    print("\n[3/3] 汇总")
    for mutant in MUTANTS:
        print("  %s  %s" % (mutant["id"], "抓到了" if all(
            mutant["id"] != mid for mid, _ in failures) else "没抓到"))
    print("  日志目录：%s" % LOG_DIR)
    if failures:
        for mid, why in failures:
            print("  %s: %s" % (mid, why), file=sys.stderr)
        print("结果：变异自证 **未通过**", file=sys.stderr)
        return 2
    print("结果：%d/%d 变异体全部被抓住，其余判据无新红" % (len(MUTANTS), len(MUTANTS)))
    print("exit=0")
    return 0


LOG_DIR = tempfile.mkdtemp(prefix="mutation-t03a-")

if __name__ == "__main__":
    sys.exit(main())
