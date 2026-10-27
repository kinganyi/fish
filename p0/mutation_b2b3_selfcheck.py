#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异自证执行器（B2 项目状态表 / B3 知识库 · 2026-10-26）。

为什么要有这个脚本：`docs/变异自证-B1B2B3-v0.1.md` 里写的"这条判据会红"如果只是
人写的推测，它自己就无法被证伪。本脚本把每条变异**真的**作用在一份临时副本的
`fish.py` 上，跑对应测试，要求出现**指定的**失败用例名——然后丢掉副本。

三条纪律（与既有 `mutation_*.py` 同构）：
  ① **只改临时副本**：`p0/fish.py` 一个字节不动（`_copy_workdir` 复制到 mkdtemp）；
  ② 变异前提必须成立：`old` 串在源码里找不到 → 直接报错（否则"红"可能是别的原因）；
  ③ 对照组：同一副本不施加变异时测试必须绿（证明红是变异造成的，不是环境）。

用法：`python mutation_b2b3_selfcheck.py [-v]`，Exit 0 = 全部符合预期。
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

# (编号, 说明, 被改文件, old, new, 测试模块, 期望出现的失败用例名)
MUTATIONS = [
    ("M-B1-1", "索引文件预算 128 KB → 无限大 → 截断路径不再触发",
     "fish.py",
     "LAYOUT_INDEX_MAX_BYTES = 128 * 1024",
     "LAYOUT_INDEX_MAX_BYTES = 10 ** 9",
     "test_layout", "test_memory_index_r1_readable_and_budgeted"),

    ("M-B1-2", "索引归属列退回完整 ULID（R1 修的就是这个）",
     "fish.py",
     '            names[key] = (display or "").strip() or _short_citizen_id(key)',
     "            names[key] = key",
     "test_layout", "test_memory_index_r1_readable_and_budgeted"),

    ("M-B2-1", "`_open_loop_rows` 丢掉 project 过滤 → 项目未了事项串项目",
     "fish.py",
     '    if project is not None:\n        sql += " AND project = ? "\n'
     '        params.append(str(project))\n',
     '    if project is not None:\n        pass\n',
     "test_open_loop_project", "test_project_filter_is_exclusive"),

    ("M-B2-2", "未了事项窗口改成 0 天 → 5 天前那条不再计数",
     "fish.py",
     "PST_OPEN_WINDOW_DAYS = 30",
     "PST_OPEN_WINDOW_DAYS = 0",
     "test_project_status", "test_open_loop_window_uses_30_days"),

    ("M-B2-3", "retired 混进主表 → 折叠区语义失效",
     "fish.py",
     '        visible = [r for r in rows if r["status"] != "retired"]',
     "        visible = rows",
     "test_project_status", "test_retired_folded_with_registry_count"),

    ("M-B2-4", "「无开档期」的显式 `—` 改成空串 → 三种 `—` 的契约破了",
     "fish.py",
     'PST_DASH_NO_OWNER = "—（无开档期）"',
     'PST_DASH_NO_OWNER = ""',
     "test_project_status", "test_three_explicit_dashes_and_no_none"),

    ("M-B3-1", "越界判定退回 startswith（不带分隔符）→ 假前缀目录被当成'在库里'",
     "fish.py",
     '    try:\n'
     '        common = os.path.commonpath([str(real_root), str(target)])\n'
     '        inside = os.path.normcase(common) == os.path.normcase(str(real_root))\n'
     '    except ValueError:                     # 不同盘符 → 不可能是子路径\n'
     '        inside = False\n',
     '    inside = str(target).startswith(str(real_root))   # 变异：假前缀漏洞\n',
     "test_kb_security", "test_fake_sibling_prefix_is_not_inside"),

    ("M-B3-2", "删掉写前备份 → 回执给的备份目录根本不存在",
     "fish.py",
     '    written = kb_atomic_write(abs_path, new_bytes, eol=eol)\n',
     '    written = kb_atomic_write(abs_path, new_bytes, eol=eol)\n'
     '    backup = {"backup_path": str(Path(data_root) / "_backups" / "kb" / "none")}\n',
     "test_kb_write", "test_every_write_has_backup_before_change"),

    ("M-B3-3", "update 不再要 base_sha256（乐观锁被拿掉）→ 可以覆盖别人的改动",
     "fish.py",
     '    if action == "update":\n'
     '        if not base_sha256:\n'
     '            raise KBError("stale_base", "update 必须带 base_sha256（乐观锁）",\n'
     '                          hint="先 `fish kb open <path>` 拿到 sha256，再 --base-sha256")\n'
     '        current_sha = kb_sha256_bytes(before)\n'
     '        if str(base_sha256).strip() != current_sha:\n'
     '            raise KBError("stale_base", "文件已被改动（base_sha256 不匹配）",\n'
     '                          hint="重新 `fish kb open` 读一次再写；不要覆盖别人的改动")\n',
     '    if action == "update":\n        pass\n',
     "test_kb_write", "test_update_requires_base_sha256"),

    ("M-B3-4", "frontmatter 用新键重建（丢未知键与注释）→ 人的文件头被压平",
     "fish.py",
     '            head = _kb_merge_frontmatter(split["head"], frontmatter or {})\n',
     '            head = [f"{k}: {v}" for k, v in (frontmatter or {}).items()]\n',
     "test_kb_write", "test_update_replaces_body_and_keeps_unknown_frontmatter"),

    ("M-B3-5", "tree 的遍历里顺手 open 一下 → '只统计不读正文'的承诺破了",
     # 2026-10-26 第二轮：tree 改成**一次遍历**（R3），不再经过 `_kb_walk`，
     # 所以这条变异的落点跟着搬到 `_kb_tree_dir` 的 stat 块——判据意图不变。
     "fish.py",
     '            try:\n'
     '                st = entry.stat(follow_symlinks=False)\n'
     '                size, m = int(st.st_size), float(st.st_mtime)\n',
     '            try:\n'
     '                open(entry.path, "rb").read(1)   # 变异：顺手读一下\n'
     '                st = entry.stat(follow_symlinks=False)\n'
     '                size, m = int(st.st_size), float(st.st_mtime)\n',
     "test_kb_read", "test_tree_counts_without_reading"),

    ("M-B3-6", "删掉逐级 reparse 检查 → 库内 junction 被当成普通目录",
     "fish.py",
     '    bad = _kb_first_reparse(real_root, lexical)\n'
     '    if bad:\n'
     '        raise KBError("reparse_point", f"路径含链接/联接点：{bad}（已拒绝）",\n'
     '                      hint="Obsidian vault 内不应有 junction；换成真实目录")\n',
     '    pass\n',
     "test_kb_security", "test_junction_inside_vault_to_inside_dir_is_reparse_point"),

    # ---------------------------------------------------------------- 第二轮（R1–R15）
    ("M-R1", "search 的失败审计退回「query 当 rel」→ 检索词又落进 kb_access.path",
     "fish.py",
     '    return _kb_guard(conn, vault_row, "", lambda: _kb_search_impl(\n'
     '        conn, vault_row, query, limit=limit, max_chars=max_chars,\n'
     '        max_tokens=max_tokens, citizen_id=citizen_id, via=via),\n'
     '        citizen_id=citizen_id, via=via, audit_rel="", audit_meta=audit_meta)\n',
     '    return _kb_guard(conn, vault_row, query, lambda: _kb_search_impl(\n'
     '        conn, vault_row, query, limit=limit, max_chars=max_chars,\n'
     '        max_tokens=max_tokens, citizen_id=citizen_id, via=via),\n'
     '        citizen_id=citizen_id, via=via)\n',
     "test_b3_round2.R1SearchPrivacyTest", "reject 行的 path 必须为空串"),

    ("M-R3", "`if level >= max_depth` 改成恒假 → --depth 又变成装饰（深层无限下钻）",
     "fish.py",
     '            if level >= max_depth:\n'
     '                # 超深处只汇总：记下"这里还有目录"，**不再 scandir**\n'
     '                hidden_dirs += 1\n',
     '            if False:\n'
     '                hidden_dirs += 1\n',
     # 2026-10-26 第四轮（评审 🟠-N2）：b2e41b9 把 depth 拆成"统计层 + 显示层"两层，
     # 只摘统计层时显示层仍按 depth 裁 → 用例不再在第 203 行（"depth=1 不该出现二级目录"）
     # 变红，而是推到第 210 行的 **scandir 计数** 断言。`expect` 必须钉在**实际失败的
     # 那条断言**上，否则执行器判 ✗（"门禁红灯 + 文档说谎"）。
     # 想恢复"文本层面的红"，得像 §6.2 的 M-R4 那样**两处一起摘**——那是另一条变异。
     "test_b3_round2.R3TreeDepthTest", "depth=1 的 scandir 次数必须受控"),

    ("M-R6", "tree 的遍历面删掉链接检查 → 顺着 junction 读库外",
     "fish.py",
     '        if _kb_is_reparse_entry(entry):\n            continue\n',
     '        pass\n',
     "test_b3_round2.R6TraversalLinkTest", "tree 顺着 junction 列出了库外文件"),

    ("M-R10", "忽略判定不再逐段归一 → 规则串与磁盘大小写不同时不再命中",
     # 第四轮（S1）之后，`.OBSIDIAN/` 这类**字面**变体会被 `realpath` 归一成磁盘真实
     # 大小写，于是"解析后复判"也接得住——只摘字面层的变异不再变红（纵深防御，不是回归）。
     # 规则串与磁盘大小写不同（规则 `media/**` vs 目录 `Media/`）时 realpath 帮不上忙，
      # 逐段归一是唯一防线；M-R10 的 `expect` 因此钉在那条断言上（见 R10 的第二个用例）。
     "fish.py",
     '    parts = [_kb_judge_component(p) for p in rel.split("/") if p]\n',
     '    parts = [p for p in rel.split("/") if p]\n',
     "test_b3_round2.R10IgnoreNormalizationTest", "规则串大小写不同也必须命中"),

    ("M-R15", "敏感名单判定置空 → 凭证文件又变成默认可读",
     "fish.py",
     # 第四轮（S1）把判定来源从 `_kb_is_sensitive_name(target["rel"])` 换成了
     # `kb_resolve_target` 返回的 `sensitive`（同时含字面串与解析后落点），
     # 所以这条变异的落点跟着搬——守的契约不变：R15 的"默认拒读"。
     '    sensitive = bool(target.get("sensitive"))\n'
     '    if sensitive and not allow_sensitive:\n',
     '    sensitive = False\n'
     '    if sensitive and not allow_sensitive:\n',
     "test_b3_round2.R15SensitiveTest", "敏感文件没有拒读"),

    # ---------------------------------------------------------------- 第四轮（S1/S2/S5/S6）
    # 这几类修复的共同点：**判定的对象换了**（字面串 → 解析后路径 / 行内规则 → 生效规则 /
    # 文件名 → 路径段 / 只护读 → 读写给闸）。判定对象类修复最容易"看着改了、其实没打中
    # 落点"，所以每条都把权威落点单独摘掉，要求指定断言必红。
    ("M-S1", "解析后路径不再复判忽略 → junction 别名（alias→.obsidian）又能读写配置目录",
     "fish.py",
     '    hit = _kb_ignored(resolved_rel, globs)\n',
     '    hit = None\n',
     "test_b3_round2.S1ResolvedPathTest", "junction 别名读忽略目录必须 ignored"),

    ("M-S2", "强制忽略项不再并入（只认行内规则）→ 旧行缺 90-模板 时又能写模板目录",
     "fish.py",
     '    out = list(KB_MANDATORY_DENY)\n',
     '    out = []\n',
     "test_b3_round2.S2MandatoryDenyTest", "只有两条 glob 的旧行也不能写 90-模板"),

    ("M-S5", "敏感判定退回只看 basename → 目录名叫凭证的凭证文件又不受保护",
     "fish.py",
     '    parts = [p for p in str(rel or "").replace("\\\\", "/").split("/")\n'
     '             if p and p not in (".", "..")]\n',
     '    parts = [os.path.basename(str(rel or "").replace("\\\\", "/"))]\n',
     "test_b3_round2.S5SensitivePathTest", "目录名命中敏感名单也必须 sensitive"),

    ("M-S6", "删掉写面的敏感闸 → append 又能静默改凭证文件",
     "fish.py",
     '    if target.get("sensitive") and not allow_sensitive:\n',
     '    if False:\n',
     "test_b3_round2.S6SensitiveWriteTest", "append 到凭证文件必须被拒"),

    # ---------------------------------------------------------------- 第五轮（N7）
    # 变异形态刻意选"退回 `bool()`"——那正是安全审计 N7 记的 fail-open 原貌：
    # `bool("false") is True`。守的契约：**授权旗的字符串形态必须被解析，不能被求值**。
    ("M-N7", "`allow_sensitive` 退回 `bool()` → 字符串 \"false\" 变成\"同意\"读凭证",
     "fish.py",
     '    if value is None:\n'
     '        return False\n'
     '    if isinstance(value, bool):\n'
     '        return value\n',
     '    return bool(value)        # 变异：fail-open，字符串 "false" 变成 True\n',
     "test_b3_round2.N7StrictBoolTest",
     "test_read_face_false_spellings_do_not_authorize"),
]

CONTROL_MODULES = ("test_layout", "test_open_loop_project", "test_project_status",
                   "test_kb_vault", "test_kb_read", "test_kb_write", "test_kb_security",
                   "test_b3_round2")


def _copy_workdir(tmp: Path) -> Path:
    """把跑变异所需的最小文件集复制到临时目录（不碰真实 p0/）。"""
    work = tmp / "work"
    work.mkdir(parents=True, exist_ok=True)
    # 2026-10-26 合并后修复：原来只复制 fish.py + CONTROL_MODULES + 两个变异脚本；
    # 合并后测试模块依赖了更多模块（install_cli/fish_activity/fish_surface…），
    # 副本不自足 → 对照组红（被误判成"环境问题"）。改为复制 p0 下全部 *.py，
    # 让副本自足（不复制 data/ 与子目录）。
    for f in HERE.glob("*.py"):
        shutil.copy2(f, work / f.name)
    return work


def _run_tests(work: Path, module: str) -> subprocess.CompletedProcess:
    # 子进程统一 UTF-8（R9 · QA F-QA-6）：不设时子进程按 cp936 写中文，父进程按 utf-8 解
    # → `expect in output` 永远 False（"变异假红"）。
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "unittest", module, "-v"],
        cwd=str(work), capture_output=True, text=True, env=env,
        encoding="utf-8", errors="replace")


def _control_check(tmp: Path, verbose: bool) -> bool:
    """对照组：未变异的副本，两个测试模块都必须绿。"""
    work = _copy_workdir(tmp / "control")
    ok = True
    for module in CONTROL_MODULES:
        proc = _run_tests(work, module)
        passed = proc.returncode == 0
        ok = ok and passed
        print(f"[对照] {module}: {'OK（绿）' if passed else '红——环境有问题，变异结果不可信'}")
        if verbose and not passed:
            print(proc.stdout[-2000:], proc.stderr[-2000:])
    return ok


def main(argv) -> int:
    # R9（QA F-QA-6）：cp936 控制台下 ✓/✗ 会触发 UnicodeEncodeError（看起来像"变异假红"）。
    # 脚本自己把 stdout/stderr 钉成 UTF-8，别让"能不能复跑"依赖调用方的环境变量。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    verbose = "-v" in argv
    tmp = Path(tempfile.mkdtemp(prefix="fish-mutation-"))
    bad = 0
    try:
        if not _control_check(tmp, verbose):
            print("✗ 对照组失败：先修环境，再看变异。")
            return 1
        for mid, desc, filename, old, new, module, expect in MUTATIONS:
            work = _copy_workdir(tmp / mid)
            src = (work / filename).read_text(encoding="utf-8")
            if old not in src:
                print(f"✗ {mid} 前提不成立：源码里找不到要被替换的片段")
                bad += 1
                continue
            (work / filename).write_text(src.replace(old, new, 1), encoding="utf-8")
            proc = _run_tests(work, module)
            red = proc.returncode != 0 and expect in (proc.stdout + proc.stderr)
            mark = "✓" if red else "✗"
            print(f"{mark} {mid} {module}::{expect} —— {desc}")
            if not red:
                bad += 1
                if verbose:
                    print(proc.stdout[-3000:], proc.stderr[-2000:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"共 {len(MUTATIONS)} 条变异：{'全部按预期变红' if not bad else f'{bad} 条不符合预期'}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
