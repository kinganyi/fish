# -*- coding: utf-8 -*-
"""变异自证（目标铁律："判据必须变异自证"）—— 补本会话 P0/P1/P2 判据的变异证据。

对每个关键判据做三件事：
  1. 把**实现**改成坏的（唯一锚点替换）
  2. 跑判据 → **必须红**（否则说明判据没有判别力）
  3. 按**字节原文**还原 → 再跑 → **必须绿**
报告 CAUGHT / MISSED / SKIP，任一 MISSED 或还原失败 → 进程非 0（工具诚实）。
"""
import subprocess
import sys
from pathlib import Path

# 迁移（评审 🟠4）：路径改相对——旧版写死本机绝对路径，
# 从任何副本里跑都会去改写**原树**（评审官因此不敢在快照里重跑）。
P0 = Path(__file__).resolve().parent

# (id, 说明, 文件, 原文, 改坏后, 测试模块, 期望红?)
MUTATIONS = [
    ("M1", "KB 召回：禁用 OR 回退（长句重新变成 0 命中）",
     "fish_kb_index.py",
     "    if not total and len(tokens) > 1:",
     "    if False and len(tokens) > 1:",
     "test_kb_recall"),
    ("M2", "注入预算：超支声明不进包正文（恢复旧 bug）",
     "fish.py",
     '        pack = (pack.rstrip("\\n")\n                + f"\\n（仍超预算：{final}>{budget_orig} tokens；最小包已无可裁段）\\n")\n        final = count_tokens(pack)',
     "        pass  # mutated: 声明只留在 meta，不进正文",
     "test_injection_budget"),
    ("M3", "hook 快通道：关闭态不读 config（恢复旧行为）",
     "hook_fastpath.py",
     '        return str(row[0]).strip().lower() in ("true", "1", "yes", "on")',
     "        return default  # mutated: 永远走默认（关态失效）",
     "test_hook_fastpath"),
    ("M4", "价值门：importance 硬零（恢复 2026-10-29 部署踩到的 bug）",
     "fish_push_rank.py",
     "    return (rel * (0.15 + 0.85 * max(0.0, min(imp, 1.0)))) >= floor",
     "    return (rel * imp) >= floor  # mutated: 新记忆 importance=0 → 全被挡",
     "test_mmr_value"),
    ("M5", "身份生命周期：validate 不查公民状态（退役 token 复活）",
     "fish.py",
     '    if row is None or str(row[0] or "") != "active":',
     "    if False:  # mutated: 不查公民状态",
     "test_identity_lifecycle"),
    ("M6", "P0-1 去重：空 session 返回空集（工具后通道重新失去去重）",
     "fish_push.py",
     '    if not session_key:\n        return {r[0] for r in conn.execute(\n            "SELECT DISTINCT code FROM push_log WHERE code IS NOT NULL AND ("\n            "pushed_at >= ? OR reaction IN (?,?))", (cutoff, *settled)).fetchall()}',
     "    if not session_key:\n        return set()  # mutated: 恢复旧 bug",
     "test_push_settled"),
    ("M7", "MMR：同项目相似度调回 0.8（🟠5 的重惩罚）",
     "fish_push_rank.py",
     "        try:\n"
     '            sim_same = float(get_config(conn, "push.mmr_same_project_sim"))\n'
     "        except KeyError:    # 键未登记 → 用默认（类型错/坏值要响亮失败）\n"
     "            sim_same = 0.4",
     "        sim_same = 0.8  # mutated: 跳过配置，回到重惩罚",
     "test_mmr_value"),
    ("M8", "MMR：只看 same 不看标题 Jaccard（近重复不再让位）",
     "fish_push_rank.py",
     "        return max(same, jac)",
     "        return same  # mutated: 标题完全相同也不再判重复",
     "test_mmr_value"),
    ("M9", "三因子：KB 候选也吃 importance（🟠4 的回归）",
     "fish_push_rank.py",
     '        if kind in ("kb", "kb_related", "corpus", "conv",\n'
     '                    "conv_related", "board", "situational"):\n'
     "            imp = 1.0\n",
     "        if False:  # mutated: KB 也被 importance 恒乘 0.2512\n"
     "            imp = 1.0\n",
     "test_factors"),
]


def run(test_module):
    r = subprocess.run([sys.executable, "-m", "unittest", test_module],
                       cwd=str(P0), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode, r.stdout + r.stderr


def main():
    """跑全部变异；返回进程退出码（0 = 全部 CAUGHT，2 = 有 MISSED/SKIP）。

    **`__main__` 保护**（外部审查 🟡8）：本模块会改写源码，被 import 就动手
    是危险的（评审官自己差点踩到）。所以顶层零副作用，`python -m` 导入安全。
    """
    results = []
    for mid, desc, rel, old, new, test in MUTATIONS:
        path = P0 / rel
        original = path.read_bytes()
        text = original.decode("utf-8")
        if old not in text:
            results.append((mid, "SKIP", f"锚点缺失：{rel}"))
            continue
        try:
            path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="")
            rc_bad, out_bad = run(test)
            caught = rc_bad != 0
            # 按字节还原
            path.write_bytes(original)
            rc_ok, out_ok = run(test)
            restored = rc_ok == 0
            verdict = "CAUGHT" if (caught and restored) else (
                "MISSED" if not caught else "RESTORE-FAILED")
            results.append((mid, verdict, desc))
        finally:
            path.write_bytes(original)

    print("=" * 74)
    for mid, verdict, note in results:
        print(f"{mid:4} {verdict:16} {note}")
    print("=" * 74)
    bad = [r for r in results if r[1] != "CAUGHT"]
    print(f"CAUGHT {len(results) - len(bad)} / {len(results)}"
          + (f"；未通过：{[r[0] for r in bad]}" if bad else ""))
    return 0 if not bad else 2


if __name__ == "__main__":
    sys.exit(main())
