#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异自证 · T03b（推送合流：六特征重排 + 断路器）· **隔离副本**版（R3 收口 · 2026-10-28）。

## 为什么不再原地改（评审 R2 的 🟠 S2）

R2 的实现在仓库里直接改 `fish_push.py` / `fish_push_rerank.py`，跑完再逐字节还原。窗口期
里 `git status` 是 `M`——评审恰好在那段时间读了树（`git add -A` 会把变异带进提交）；更糟
的是"全量测试"与变异并发时，被测代码正是被改坏的那份，读数不可复现。

现在四道闸：

1. **门禁**（启动）：4 个目标文件的 `git hash-object` 必须等于 `git rev-parse HEAD:<blob>`，
   否则直接失败（不在一个已经脏的树上产生任何读数）；
2. **隔离**：`p0` 的必要文件（`.py` + 少量夹具；排除 `data/`、`_audit/`、`__pycache__`）
   复制进 `tempfile.mkdtemp()`，**只改副本**；副本内的 `test_push_rerank` 与本文件同构；
3. **收尾比对**（R4 · 评审 🟡3）：**只**比这 4 个受跟踪目标文件的 sha256 与启动快照——
   `git status` 整树比对已删除。理由：并发会话往 `docs/` 写一份评审报告就是一行 `??`，
   旧版会把"别人的未跟踪文件"读成"仓库被我改了" → 假红 + exit 1（我方与评审都实测到）；
   **untracked / 其它文件一律不参与**，脚本能写坏什么就只比什么；
4. **自测**（R4）：在**副本**里给目标文件加一行 → 收尾检测必须报红（证明检测本身不是恒绿）
   → 副本逐字节还原。R3 的版本往**仓库**写一行再还原，"仓库零写入"自己就不成立（窗口期
   `git status=M`），所以自测搬到副本；副本与仓库逐字节相同，"副本脏能被检出"即同一判据。

## 变异体清单（9 个，M8–M16）

| 变异体 | 改坏什么（文件） | 必须红的判据 |
|---|---|---|
| **M8** | 忽略 `top_k`，对**全池**重排（`fish_push_rerank.py`） | R1 只处理 top-K |
| **M9** | 断路器失效 **+** 重复读数写死 0 | R4 重复惩罚 / R6c 断路器 |
| **M10** | `_link_strengths` 恒 `{}`（首轮静默变异 ①） | R8 `link_strength` 真值 |
| **M11** | `_alias_group_for` 恒 `None`（首轮静默变异 ②） | R9 别名组 0.5 |
| **M12** | `RERANK_TOP_K_MAX = 10**9`（首轮静默变异 ③） | R1b `top_k` 上界 500 |
| **M13** | 断路器 `<=0` → `<0`（首轮静默变异 ④） | R11 `0` = 关闭 |
| **M14** | `_infer_project` 不看 `_ctx_project` **+** 回退"池内多个项目里挑一个" | R10 推断项目 / **R12d 混合池中性** |
| **M15** | `freshness` 恒 1.0（**同时**篡改 breakdown 与 score） | R3b 组合 oracle |
| **M16** | kb/corpus 的 `project_match` → 1.0（假特征回潮） | R9 非记忆类中性 0.5 |

## 判定口径（比 `rc != 0` 硬）

必须出现 `FAIL:`/`ERROR:` 且**行里就是那条指定用例**——被跳过的用例 rc 也是 0，只看退出
码会把"没跑"当成"抓到了"。副本里 `data/` 不存在，所以 R6b/R6d/R8b 会 **skip**（真库类判据
只在仓库里跑）；这条读数在基线段打印出来，不冒充通过。

驱动：`cd p0 && python mutation_push_rerank.py` → 收尾打印
`RESULT: 9/9 CAUGHT, REPO_CLEAN=true` 并 exit 0（9 个全抓到 + 4 个目标文件前后 sha256
相同）；任何仓库写入（4 个文件任一变化）都算失败，**不**用 `git status` 整树比对。
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent       # p0（仓库内，只读）
REPO = HERE.parent                           # fish-push 工作树根
sys.path.insert(0, str(HERE))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from mutation_source import SourceText, to_lf  # noqa: E402

MODULE = "test_push_rerank"                  # unittest 的点分模块名（不带 .py）
TEST_FILE = MODULE + ".py"                   # 同一个文件的磁盘名（门禁/比对用）
RERANK = "fish_push_rerank.py"
PUSH = "fish_push.py"
MUTATION = "mutation_push_rerank.py"
# 门禁 + 收尾比对的对象：本批**只应**修改这 4 个文件
TARGETS = (RERANK, PUSH, TEST_FILE, MUTATION)
# 变异不许影响既有判据：这些文件在变异态下仍必须全绿（重排默认关 → 它们走不到新代码）
OTHERS = ["test_push_p0", "test_push_p1a_candidates", "test_push_kb", "test_corpus_push",
          "test_split_contract"]
# 副本不带的数据目录（大 + 与判据无关；真库类判据在副本里 skip，基线段会打印出来）
EXCLUDE_DIRS = {"data", "_audit", "__pycache__", ".pytest_cache"}
# 生产树（只读）：副本里 `P0.parents[1]` 指不到它，用 FISH_PROD_P0 显式交回给 R14
PRODUCTION = HERE.parents[1] / "大肥鱼生命系统" / "p0"

MUTANTS = [
    {
        "id": "M8",
        "file": RERANK,
        "desc": "忽略 top_k，对全池重排（全量扫描回潮）",
        "pairs": [(
            "    try:\n"
            "        head_size = min(max(int(top_k), 1), RERANK_TOP_K_MAX)    "
            "# 配置写坏时 K 仍是 K\n"
            "    except (TypeError, ValueError):\n"
            "        head_size = RERANK_TOP_K_DEFAULT\n",
            "    head_size = len(cands)      # 变异：不许看 top_k，全池都重排\n")],
        "must_fail": ["%s.TopKTest.test_R1_only_top_k_is_reranked" % MODULE],
    },
    {
        "id": "M9",
        "file": RERANK,
        "desc": "断路器失效 + 重复读数写死 0（负向特征永不生效）",
        "pairs": [
            ("    if limit <= 0 or elapsed <= limit:\n"
             "        return out\n",
             "    if True:        # 变异：断路器失效（超时也照样推）\n"
             "        return out\n"),
            ("    cutoff = _utc_str(now_dt - datetime.timedelta(hours=float("
             "rank_mod.DUP_WINDOW_HOURS)))\n",
             "    return {}       # 变异：重复读数写死 0（dup_penalty 永不生效）\n"),
        ],
        "must_fail": ["%s.DupPenaltyTest.test_R4_dup_penalty_lowers_score" % MODULE,
                      "%s.HookBudgetTest.test_R6c_breaker_cuts_slow_component" % MODULE],
    },
    {
        "id": "M10",
        "file": RERANK,
        "desc": "link_strength 读数恒空（首轮静默变异 ①）",
        "pairs": [(
            "    name = str(project or \"\").strip()\n"
            "    target = [code for code in (codes or ()) if code]\n",
            "    return {}                   # 变异：链接强度恒 0\n")],
        "must_fail": ["%s.LinkStrengthTest.test_R8_link_strength_readings" % MODULE],
    },
    {
        "id": "M11",
        "file": RERANK,
        "desc": "别名组恒 None（首轮静默变异 ②）",
        "pairs": [(
            "    name = str(project or \"\").strip()\n"
            "    if not name:\n"
            "        return None\n",
            "    return None                 # 变异：别名组恒 None\n")],
        "must_fail": ["%s.ProjectMatchTest.test_R9_project_match_by_kind_class" % MODULE],
    },
    {
        "id": "M12",
        "file": RERANK,
        "desc": "top_k 上界写坏成 10**9（首轮静默变异 ③）",
        "pairs": [(
            "RERANK_TOP_K_MAX = 500                  # 配置写坏（10**9）时的上界："
            "top-K 必须真的是 K\n",
            "RERANK_TOP_K_MAX = 10 ** 9              # 变异：上界形同不存在\n")],
        "must_fail": ["%s.TopKBoundsTest.test_R1b_top_k_bounds_and_fallback" % MODULE],
    },
    {
        "id": "M13",
        "file": RERANK,
        "desc": "断路器 <=0 改成 <0（0 不再等于关闭 · 首轮静默变异 ④）",
        "pairs": [(
            "    if limit <= 0 or elapsed <= limit:\n",
            "    if limit < 0 or elapsed <= limit:      # 变异：0 不再是关闭\n")],
        "must_fail": ["%s.BreakerOffTest.test_R11_breaker_zero_means_off" % MODULE],
    },
    {
        "id": "M14",
        "file": RERANK,
        "desc": "_infer_project 不看 _ctx_project + 回退「多个项目里挑一个」（首轮静默变异 ⑤）",
        "pairs": [
            ("        name = str(cand.get(CTX_PROJECT) or \"\").strip()\n"
             "        if name and not ctx:\n"
             "            ctx = name\n",
             "        ctx = \"\"                  # 变异：不看 _ctx_project\n"),
            ("    return next(iter(projects)) if len(projects) == 1 else None\n",
             "    return sorted(projects)[0] if projects else None     # 变异：挑一个项目\n"),
        ],
        "must_fail": ["%s.InferProjectTest.test_R10_infer_project" % MODULE,
                      "%s.PromptContextTest.test_R12d_unknown_project_mixed_pool_is_neutral"
                      % MODULE],
    },
    {
        "id": "M15",
        "file": RERANK,
        "desc": "freshness 恒 1.0（**一致地**篡改 breakdown+score，和式仍绿）",
        "pairs": [(
            "            \"freshness\": round(_freshness_of(cand), 6),\n",
            "            \"freshness\": 1.0,          # 变异：分项与总分一起被改\n")],
        "must_fail": ["%s.OracleCompositeTest.test_R3b_composite_oracle" % MODULE],
    },
    {
        "id": "M16",
        "file": RERANK,
        "desc": "kb/corpus 的 project_match 恒 1.0（假特征回潮）",
        "pairs": [(
            "    if kind in (rank_mod.KIND_CLASS_KB, rank_mod.KIND_CLASS_CORPUS):\n"
            "        return PROJECT_NEUTRAL\n",
            "    if kind in (rank_mod.KIND_CLASS_KB, rank_mod.KIND_CLASS_CORPUS):\n"
            "        return 1.0                  # 变异：非记忆类白拿满格\n")],
        "must_fail": ["%s.ProjectMatchTest.test_R9_project_match_by_kind_class" % MODULE],
    },
]


# ---------------------------------------------------------------- 仓库读数（只读）
def _git(*args):
    """只读 git 调用（`-C <repo>`，不回显 stderr）。"""
    proc = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def _blob_hash(name):
    """工作树文件的 blob sha（与 `git add` 会写进索引的内容一致）。"""
    rc, out, _err = _git("hash-object", "p0/%s" % name)
    return out if rc == 0 else ""


def _head_hash(name):
    """HEAD 里该文件的 blob sha；未跟踪 → 空串。"""
    rc, out, _err = _git("rev-parse", "HEAD:p0/%s" % name)
    return out if rc == 0 else ""


def _repo_state():
    """仓库快照：**只**这 4 个受跟踪目标文件的 sha256（键 = 文件名）。

    为什么不再看 `git status`（R4 · 评审 🟡3）：并发会话往 `docs/` 加一份评审报告就是一行
    `??`，整树比对会把"别人的未跟踪文件"读成"仓库被我改了" → 假红 + exit 1。脚本能写坏的
    只有这 4 个文件，那也只比这 4 个；untracked / 其它文件的增删与本脚本无关。
    """
    return {name: _sha(HERE / name) for name in TARGETS}


def _gate_problems():
    """门禁：4 个目标文件必须与 HEAD 逐字节一致（不在脏树上产读数）。"""
    problems = []
    for name in TARGETS:
        worktree, head = _blob_hash(name), _head_hash(name)
        if not head:
            problems.append("%s 不在 HEAD 里（未跟踪，先提交）" % name)
        elif worktree != head:
            problems.append("%s 与 HEAD 不一致（工作树已脏）" % name)
    return problems


def _drift(baseline, root=None):
    """收尾比对：4 个目标文件的 sha256 与启动快照逐项相同吗。

    `root` 是给自测用的：传副本目录即可用**同一段判据**验"副本变脏会不会被检出"，而仓库
    一个字节都不动（副本与仓库在 [1/5] 已验过逐字节相同）。
    """
    base = Path(root or HERE)
    return ["%s sha256 变了" % name for name in TARGETS
            if _sha(base / name) != baseline[name]]


# ---------------------------------------------------------------- 隔离副本
def _make_copy():
    """把 p0 的必要文件复制进临时根（**排除** data/ _audit/ __pycache__），返回副本根。"""
    root = Path(tempfile.mkdtemp(prefix="mutation-t03b-copy-"))
    dest = root / "p0"
    copied = 0
    for src in HERE.rglob("*"):
        rel = src.relative_to(HERE)
        if any(part in EXCLUDE_DIRS for part in rel.parts) or not src.is_file():
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        copied += 1
    return root, dest, copied


def _run(specs, cwd, log_dir):
    """在**副本**里跑 unittest（副本的 `sys.path[0]` 就是它自己）。"""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    if PRODUCTION.is_dir():
        env["FISH_PROD_P0"] = str(PRODUCTION)     # R14 要用真语料（只读）
    cmd = [sys.executable, "-m", "unittest", *specs]
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, timeout=1800)
    name = "-".join(spec.split(".")[-1] for spec in specs)[:80]
    log = Path(log_dir) / ("%s.log" % name)
    log.write_text("$ python -m unittest %s（cwd=%s）\nexit=%d\n\n---- stdout ----\n%s\n"
                   "---- stderr ----\n%s"
                   % (" ".join(specs), cwd, proc.returncode, proc.stdout, proc.stderr),
                   encoding="utf-8")
    tail = (proc.stderr or proc.stdout).strip().splitlines()[-6:]
    return proc.returncode, tail, proc.stdout + "\n" + proc.stderr


def _mutate(src, pairs) -> str:
    """按顺序应用多个靶点（每个都要求命中恰好 1 次），返回 LF 文本。"""
    text = src.text
    for old, new in pairs:
        hits = text.count(to_lf(old))
        if hits != 1:
            raise AssertionError("%s 里变异片段命中 %d 次（要求恰好 1 次）：%r"
                                 % (src.path.name, hits, old[:60]))
        text = text.replace(to_lf(old), to_lf(new), 1)
    return text


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
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _selftest_detector(baseline, log_dir, copy_p0):
    """自测：在**副本**里给目标文件加一行 → 收尾检测**必须**报红；还原后必须干净。

    为什么要有它：一个恒 False 的检测器也能让上面所有断言绿，"仓库未变"就成了空话。
    为什么改在副本（R4 · 评审 🟡3）：R3 的版本往**仓库**写一行再逐字节写回——窗口期
    `git status` 是 `M`，"仓库零写入"这句话自己就不成立，还会和并发读树的会话撞车。
    副本在 [1/5] 已验过与仓库逐字节相同，所以"副本变脏能被检出"就是同一个判据。
    """
    path = Path(copy_p0) / RERANK
    original = path.read_bytes()
    try:
        path.write_bytes(original + "\n# mutation selftest：这一行必须被检出\n".encode("utf-8"))
        caught = bool(_drift(baseline, copy_p0))
    finally:
        path.write_bytes(original)
    clean = not _drift(baseline, copy_p0)
    (Path(log_dir) / "selftest-detector.log").write_text(
        "caught=%r clean=%r copy=%s\n" % (caught, clean, copy_p0), encoding="utf-8")
    return caught, clean


def main():
    print("=" * 78)
    print("T03b · 变异自证（M8–M16 · 隔离副本模式 · %s）" % MODULE)
    print("=" * 78)
    log_dir = Path(tempfile.mkdtemp(prefix="mutation-t03b-logs-"))
    print("日志目录：%s" % log_dir)

    print("\n[0/5] 门禁：4 个目标文件必须与 HEAD 一致（脏树上不产读数）")
    problems = _gate_problems()
    if problems:
        for problem in problems:
            print("  ✗ %s" % problem, file=sys.stderr)
        print("门禁失败：先把工作树提交/还原，再跑变异。", file=sys.stderr)
        return 3
    baseline = _repo_state()
    for name in TARGETS:
        print("  %-24s %s ✓" % (name, baseline[name][:12]))
    print("  比对范围：只有这 4 个受跟踪文件（untracked / 其它文件不参与，"
          "并发会话随便写 docs/）")

    print("\n[1/5] 建隔离副本（排除 %s）" % "/".join(sorted(EXCLUDE_DIRS)))
    copy_root, copy_p0, copied = _make_copy()
    print("  副本：%s（%d 个文件）" % (copy_p0, copied))
    for name in TARGETS:
        same = _sha(copy_p0 / name) == _sha(HERE / name)
        print("  %-24s 与仓库逐字节相同：%s" % (name, "✓" if same else "✗"))
        if not same:
            print("副本与仓库不一致：拒绝继续（读数是假的）。", file=sys.stderr)
            return 4

    failures = []
    print("\n[2/5] 副本基线：%s 必须全绿" % MODULE)
    rc, tail, out = _run([MODULE], copy_p0, log_dir)
    print("  %-24s %s" % (MODULE, "绿 ✓" if rc == 0 else "红 ✗ (rc=%d)" % rc))
    if rc != 0:
        print("基线不是全绿——判据本身有问题，终止。", file=sys.stderr)
        print("\n".join(tail), file=sys.stderr)
        return 1
    print("  副本内跳过（真库类判据，不算通过）：%s" % _skipped_count(out))
    for line in out.splitlines():
        if line.startswith("[R6") or line.startswith("[R8b]") or line.startswith("[R14]"):
            print("  " + line)

    print("\n[3/5] 逐个变异（**只改副本**）→ 指定判据必红 → 其它判据不许新红 → 还原副本")
    for mutant in MUTANTS:
        path = copy_p0 / mutant["file"]
        pristine = path.read_bytes()
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
                spec = spec.strip()
                rc, tail, out = _run([spec], copy_p0, log_dir)
                wanted = spec.split(".")[-1]
                outcomes = _outcomes(out)
                hit = [kind for kind, test_id in outcomes
                       if test_id.split(" ")[0] == wanted or test_id.startswith(wanted)]
                if rc == 0 or not hit:
                    caught = False
                    print("\n    ↳ %s 变异后没有被指定判据抓到：rc=%d，失败用例=%r"
                          % (spec, rc, [tid for _kind, tid in outcomes][:3]))
                else:
                    print("    ↳ %s %s ✓" % (wanted, hit[0]))
            for other in OTHERS:
                rc, tail, _out = _run([other], copy_p0, log_dir)
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
            path.write_bytes(pristine)
            restored = _sha(path) == _sha(HERE / mutant["file"])
            print("    副本还原：sha256 %s（%s）"
                  % ("一致 ✓" if restored else "不一致 ✗", mutant["file"]))
            if not restored:
                return 4

    print("\n[4/5] 自测检测器：在**副本**里造脏 → 必须被检出 → 还原后必须干净（仓库零写入）")
    caught, clean = _selftest_detector(baseline, log_dir, copy_p0)
    print("  往副本的 fish_push_rerank.py 加一行 → 检出：%s；还原后干净：%s"
          % ("✓" if caught else "✗", "✓" if clean else "✗"))
    if not (caught and clean):
        print("检测器自测失败：收尾的「REPO_CLEAN=true」不可信。", file=sys.stderr)
        failures.append(("selftest", "检测器恒绿或未还原"))

    print("\n[5/5] 仓库不变性：4 个受跟踪目标文件 sha256 必须与启动快照逐项相同")
    drift = _drift(baseline)
    print("  4 个目标文件 sha256：%s"
          % ("✓ 全等（脚本全程未写仓库——变异与自测改的都是副本）" if not drift
             else "✗ %r" % (drift,)))

    print("\n[汇总]")
    for mutant in MUTANTS:
        missed = [mid for mid, _why in failures if mid == mutant["id"]]
        print("  %s  %s" % (mutant["id"], "抓到了" if not missed else "没抓到"))
    print("  日志：%s（副本：%s）" % (log_dir, copy_root))
    shutil.rmtree(copy_root, ignore_errors=True)
    caught_count = sum(1 for mutant in MUTANTS
                       if all(mid != mutant["id"] for mid, _why in failures))
    repo_clean = not drift
    print("RESULT: %d/%d CAUGHT, REPO_CLEAN=%s"
          % (caught_count, len(MUTANTS), "true" if repo_clean else "false"))
    if failures or drift:
        for mid, why in failures:
            print("  %s: %s" % (mid, why), file=sys.stderr)
        print("结果：变异自证 **未通过**（%d/%d 抓到；仓库 4 文件 %s）"
              % (caught_count, len(MUTANTS), "未变" if repo_clean else "被改过"),
              file=sys.stderr)
        return 2
    print("结果：%d/%d 变异体全部被抓住，其余判据无新红；仓库零写入（4 个目标文件 sha256 未变）"
          % (len(MUTANTS), len(MUTANTS)))
    print("exit=0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
