#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
会话画像（S1）· 变异自证驱动（2026-10-24）
================================================
用途：**改真源码 → 跑 → 必红 → 字节还原**。十一条变异（判据 A/B/C/C-OK/C2/D/E/F/G/H/I），
一条都不许"照绿"。

为什么不只靠 test_profile_s1.py 里的断言：
  断言只证明"现在是对的"。它证明不了"把这段删掉之后它会红"——恒真断言在实现坏掉时
  照样通过。这个脚本改的是磁盘上的**字节**，用独立的 pytest 进程跑对应判据，
  证明的是"真把这段删了/改了，那条判据真的会红"。

只碰一个目标文件（本轮改动面）：
  · fish.py —— compute_session_profile 及其辅助函数：A / B / C / C-OK / C2 / D / E / F / G / H / I

纪律（与 mutation_identity_p1a/p1b/p1c.py、mutation_status_filter.py 同一套）：
  · 读写走字节（文件是 LF 结尾的，文本模式在 Windows 上会把 LF 改成 CRLF → 还原不干净）；
  · 每改一次都比对 sha256，结束后**必须**逐字节还原并复核；
  · 任何异常（进程崩溃 / Ctrl-C）都走 finally 还原；
  · 还原失败 → 立刻非零退出并打印人工恢复步骤（备份在 历史版本\\）。

用法：
    python mutation_profile_s1.py              # 全跑（约 30-60 秒）
    python mutation_profile_s1.py --only M-E   # 只跑一条
    python mutation_profile_s1.py --list       # 只列清单
"""
import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEST_FILE = "test_profile_s1.py"
TEST_CLASS = "FishProfileS1Test"
BACKUP_HINT = "历史版本\\fish.py.20261024-1855-s1.bak"

TARGETS = {
    "fish.py": HERE / "fish.py",
}

# 一条变异 = (编号, 守哪条判据, 目标文件, 目标用例, 原样片段, 改成什么, 说明)
MUTATIONS = (
    (
        "M-A", "A", "fish.py", "test_A_tool_usage_splits_by_session",
        '        if _session_label(rec) == label:\n'
        '            usage[tool] = usage.get(tool, 0) + 1\n',
        '        if True:                       # 变异：不按会话过滤\n'
        '            usage[tool] = usage.get(tool, 0) + 1\n',
        "tool_usage 不按 session_actor 过滤 → 别的会话的调用也算进本会话画像",
    ),
    (
        "M-B", "B", "fish.py", "test_B_tool_name_normalized",
        'TOOL_ALIASES = {"resolve_code": "resolve"}\n',
        'TOOL_ALIASES = {}\n',
        "取消别名表 → resolve_code 与 resolve 变成两个键（同一工具被拆成两条）",
    ),
    (
        "M-C", "C", "fish.py", "test_C_blind_spot_from_same_account_peers",
        '        if _session_label(rec) == my_label:\n'
        '            continue\n',
        '        if _session_label(rec) != my_label:      # 变异：只数本会话自己\n'
        '            continue\n',
        "peer 口径反转成「本会话自己」→ 盲区永远算不出来（盲区必须来自别的会话）",
    ),
    (
        "M-C-OK", "C", "fish.py", "test_C_blind_spot_from_same_account_peers",
        '        if rec.get("ok") is not True:\n'
        '            continue\n',
        '        if False:                      # 变异：失败的调用也算「用过」\n'
        '            continue\n',
        "失败调用也算用过 → 本会话 5 次报错的 neighbors 把盲区吃掉（ok=True 是铁律）",
    ),
    (
        "M-C2", "C2", "fish.py", "test_C2_blind_spot_never_falls_back_to_whole_db",
        '    basis = scope.get("basis")\n'
        '    if basis == "actor":\n',
        '    return True                        # 变异：任何会话都算同类\n'
        '    basis = scope.get("basis")\n'
        '    if basis == "actor":\n',
        "同类判定恒真 → 退回全库，不相干账号的调用也算「他们常用」",
    ),
    (
        "M-D", "D", "fish.py", "test_D_blind_spot_threshold_is_five",
        'BLIND_SPOT_MIN_PEER_CALLS = 5        # 同类其他会话累计调用 ≥5 次才算"他们常用"\n',
        'BLIND_SPOT_MIN_PEER_CALLS = 4        # 变异：门槛降到 4\n',
        "门槛 5 改成 4 → 只调 4 次的工具也报成盲区",
    ),
    (
        "M-E", "E", "fish.py", "test_E_knowledge_gap_requires_zero_own_reads",
        '           "                 WHERE s.code = m.code AND s.session_key = ?) "\n',
        '           "                 WHERE s.code = m.code AND s.session_key = ? AND 0) "\n',
        "判据 ③（本会话 0 次）恒真 → 自己读过的条目也被算成知识遗漏",
    ),
    (
        "M-F", "F", "fish.py", "test_F_knowledge_gap_only_active",
        '           "JOIN code_index ci ON ci.code = m.code AND ci.status = \'active\' "\n',
        '           "JOIN code_index ci ON ci.code = m.code "\n',
        "不再筛 status → 作废条目（withdrawn）被别人读过就成了「知识遗漏」",
    ),
    (
        "M-G", "G", "fish.py", "test_G_missing_audit_does_not_crash",
        '    except (OSError, UnicodeDecodeError):\n',
        '    except ZeroDivisionError:      # 变异：文件不存在就抛\n',
        "审计文件不存在不再兜住 → FileNotFoundError 冒出画像计算（判据 G 会红）",
    ),
    (
        "M-H", "H", "fish.py", "test_H_prod_read_only_reading",
        '    label = (session_actor or "").strip() or None\n',
        '    label = None                     # 变异：不认入参会话名\n',
        "会话名解析失效 → 生产库真实会话算出空画像（判据 H 会红）",
    ),
    (
        "M-I", "I", "fish.py", "test_I_read_only_and_fixed_fields",
        '    profile["stats"]["access_log_total"] = _access_log_total(conn, label)\n'
        '    return profile\n',
        '    profile["stats"]["access_log_total"] = _access_log_total(conn, label)\n'
        '    conn.execute("INSERT INTO memory_access_log (code,via,accessed_at) "\n'
        '                 "VALUES (\'SYNTH-I\',\'manual\',?)", (computed_at,))   # 变异：写表\n'
        '    return profile\n',
        "画像计算偷偷写表（本步必须只读）→ 行数变化被判据 I 抓住",
    ),
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_source(path: Path) -> str:
    """按字节读、再解码：文件是 LF 结尾的，文本模式会把换行悄悄改写掉。"""
    return path.read_bytes().decode("utf-8")


def write_source(path: Path, text: str):
    path.write_bytes(text.encode("utf-8"))


def run_test(test_name: str) -> tuple:
    """跑目标用例，返回 (是否通过, 结果摘要)。

    "没有跑到用例"（名字写错 / 收集失败）必须**响亮报错**，不能当成"红"——
    否则一条永远跑不起来的变异会伪装成"判据有判别力"。

    摘要里带上**失败原因那一行**（`--tb=line`）：只写"1 failed"不足以证明红的是
    我们想钉的那条断言，而不是夹具本身崩了。
    """
    node = f"{TEST_FILE}::{TEST_CLASS}::{test_name}"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", node, "-q", "--no-header", "--tb=line"],
        cwd=str(HERE), capture_output=True)
    out = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    lines = [ln.strip() for ln in out.strip().splitlines() if ln.strip()]
    summary = lines[-1] if lines else "(无输出)"
    if "no tests ran" in out.lower() or "error: not found" in out.lower():
        raise RuntimeError(f"目标用例没有跑起来（{node}）：{summary}")
    reason = next((ln for ln in reversed(lines)
                   if "AssertionError" in ln or "Failed:" in ln or "Error:" in ln
                   or "Exception" in ln), "")
    tail = f"{reason} ‖ {summary}" if reason else summary
    return proc.returncode == 0, tail[:240]


def apply_mutation(path: Path, old: str, new: str) -> str:
    """就地改源码，返回改之前的原文（用于还原）。找不到/多处命中就报错，绝不静默跳过。"""
    text = read_source(path)
    hits = text.count(old)
    if hits != 1:
        raise AssertionError(f"{path.name} 里变异片段命中 {hits} 次（要求恰好 1 次）：{old[:60]!r}")
    write_source(path, text.replace(old, new))
    return text


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="会话画像 S1 变异自证")
    parser.add_argument("--only", help="只跑某一条（如 M-E）")
    parser.add_argument("--list", action="store_true", help="只列变异清单，不执行")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):        # 控制台是 GBK，不让符号把输出搞崩
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.list:
        for mut_id, judgment, fname, test_name, _o, _n, note in MUTATIONS:
            print(f"{mut_id:<8}{judgment:<5}{fname:<10}{test_name:<52}{note}")
        return 0

    baseline = {name: sha256(path) for name, path in TARGETS.items()}
    for name, path in TARGETS.items():
        print(f"基线 {name}：{len(path.read_bytes())} 字节 / sha256 {baseline[name][:16]}…")

    # 环境自证：未变异时判据 A 与判据 H 都必须通过——绿了才有资格谈"变异后变红"。
    for probe in ("test_A_tool_usage_splits_by_session",
                  "test_H_prod_read_only_reading"):
        passed, tail = run_test(probe)
        if not passed:
            print(f"[X] 环境自证失败：未变异时 {probe} 都没过（{tail}）——先修环境再看变异")
            return 2
    print("环境自证：未变异时判据 A / H 均通过 [OK]\n")

    rows = []
    failures = []
    for mut_id, judgment, fname, test_name, old, new, note in MUTATIONS:
        if args.only and args.only != mut_id:
            continue
        path = TARGETS[fname]
        original = None
        went_green = None
        restored_ok = False
        tail = ""
        try:
            original = apply_mutation(path, old, new)
            went_green, tail = run_test(test_name)
        except Exception as exc:                   # noqa: BLE001 —— 任何异常都要先还原
            failures.append(f"{mut_id} 执行异常：{exc}")
        finally:
            if original is not None:
                write_source(path, original)
            restored_ok = sha256(path) == baseline[fname]
            if not restored_ok:
                failures.append(
                    f"{mut_id} 还原后 {fname} 的 sha256 不一致！立即手工恢复："
                    f"用 {BACKUP_HINT} 覆盖 {fname}")

        red = went_green is False and restored_ok
        if went_green is False and original is None:
            red = False                           # 片段没命中，不算"变异生效过"
        if went_green is not False:
            failures.append(f"{mut_id} 变异后判据仍然是绿的——这条判据没有判别力")
        rows.append((mut_id, judgment, red, restored_ok, note, tail))
        print(f"{mut_id} [判据 {judgment}] {'红（符合预期）' if red else '[X] 意外'}")
        print(f"   变异：{note}")
        print(f"   目标：{TEST_FILE}::{TEST_CLASS}::{test_name}")
        print(f"   结果：{tail}")
        print(f"   还原：{'sha256 与基线一致' if restored_ok else '[X] sha256 不一致'}\n")

    print("=" * 96)
    for mut_id, judgment, red, restored_ok, note, _tail in rows:
        print(f"{mut_id:<8}{judgment:<5}{'红/预期' if red else '[X]意外':<12}"
              f"{'已还原' if restored_ok else '[X]未还原':<12}{note}")
    print("=" * 96)
    same = all(sha256(path) == baseline[name] for name, path in TARGETS.items())
    detail = " / ".join(f"{name} {sha256(path)[:12]}…" for name, path in TARGETS.items())
    print(f"最终 sha256：{detail} {'（与基线一致）' if same else '（[X] 与基线不一致）'}")
    if failures:
        print("\n失败项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"\n全部 {len(rows)} 条变异：改源码后判据全红、字节已还原。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
