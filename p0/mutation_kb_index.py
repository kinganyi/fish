# -*- coding: utf-8 -*-
"""`test_kb_index.py` 的变异驱动（K8 判别力的自证）。

三个变异，每个都必须让对应判据**变红**；而且不是靠"改坏代码让它崩"，
是靠**真的做错事**：

  · **M-K8**：把 `search_kb` 退回"每次检索都 walk + 读全文"的旧实现
    → `K3b`（检索零文件系统访问）必红。挑 K3b 而不是 K3 的耗时断言是**故意的**：
    耗时在慢机器/杀毒扫描下会抖，而"有没有调用 `_kb_walk`/`read_text`"是结构事实。
  · **M-K2**：让增量忽略 `(mtime, size)`（永远当作"变了"）
    → `K2`（无变化增量全 `unchanged`）必红。
  · **M-K5**：删文件时**物理删行**
    → `K5`（行还在、只是标 deleted）必红。

流程：**基线绿 → 变异红 → 逐字节还原（sha256 比对）→ 恢复后全量绿**。
还原用 `try/finally` + 字节级校验：中途崩了脚本也会把源码写回去；
最后还会把"还原后的文件字节"与"进脚本前的字节"做一次 sha256 全等断言。

⚠️ 脚本只在**本 worktree 的 `p0/fish_kb_index.py`** 上动手，且每次都还原；
不碰 `fish.py` / `fish_kb.py` / 任何既有测试。
"""
import hashlib
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "fish_kb_index.py"
TEST = "test_kb_index"


def run_tests(test_ids):
    """跑指定测试，返回 `(exit_code, stdout+stderr)`。"""
    proc = subprocess.run(
        [sys.executable, "-m", "unittest"] + list(test_ids) + ["-v"],
        cwd=str(HERE), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=900)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def is_green(out: str) -> bool:
    """判定：末行是 `OK`（或 `OK (skipped=…)`）且没有 FAILED。

    ⚠️ 不要写成 `"FAILED" in out is False`：那是**比较链**
    （`("FAILED" in out) and (out is False)`），永远为假，判据会被自己的写法骗过。
    """
    return "FAILED" not in out and re.search(r"^OK\b", out, re.M) is not None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


MUTATIONS = [
    (
        "M-K8",
        "search_kb 退回 rglob 扫描（模拟旧实现）",
        [f"{TEST}.BigVaultTest.test_K3b_search_never_touches_filesystem"],
        lambda s: s.replace(
            '    _ = project                                   # 见 docstring：保留位，不参与检索\n',
            '    # MUTATION M-K8：每次检索都 walk + 读全文（旧实现）\n'
            '    for _v in fish_kb.kb_vault_list(conn):\n'
            '        for _ent in fish_kb._kb_walk(_v, _v["path"]):\n'
            '            if _ent["is_dir"] or not str(_ent["rel"]).lower().endswith(\n'
            '                    (".md", ".markdown")):\n'
            '                continue\n'
            '            _txt = _ent["abs"].read_text(encoding="utf-8", errors="ignore")\n'
            '            if str(query or "").casefold() in _txt.casefold():\n'
            '                pass\n'
            '    _ = project                                   # 见 docstring：保留位\n', 1),
    ),
    (
        "M-K2",
        "增量忽略 (mtime, size)：永远当作变了",
        [f"{TEST}.BigVaultTest.test_K2_incremental_no_change_under_2s"],
        lambda s: s.replace(
            '    return (abs(float(old["mtime"]) - float(mtime)) < _KB_INDEX_MTIME_EPS\n'
            '            and int(old["size"]) == int(size))\n',
            '    return False  # MUTATION M-K2：mtime/size 不再参与判断\n', 1),
    ),
    (
        "M-K5",
        "删文件时物理删行（丢掉可追溯性）",
        [f"{TEST}.LifecycleTest.test_K5_deleted_file_marked_not_removed"],
        lambda s: s.replace(
            '    conn.execute("UPDATE kb_index SET status=\'deleted\', error=NULL, '
            'indexed_at=? "\n'
            '                 "WHERE rowid=?", (now_utc(), int(rowid)))\n',
            '    # MUTATION M-K5：直接删行\n'
            '    conn.execute("DELETE FROM kb_index WHERE rowid=?", (int(rowid),))\n', 1),
    ),
]


def main() -> int:
    # ⚠️ 一律走**字节**：`read_text`/`write_text` 在 Windows 上会把 `\n` 翻成 `\r\n`
    # （`open(newline=None)` 的换行翻译），那样"还原"只是**文本**相同、**字节**不同，
    # 而"逐字节还原"要的正是后者。读字节 → 解码打补丁 → 编码写回 → 用字节 sha256 校验。
    original_bytes = SRC.read_bytes()
    original = original_bytes.decode("utf-8")
    if b"\r\n" in original_bytes:
        print("⚠️ 源文件含 CRLF——本脚本按字节还原，但变异锚点按 LF 编写，"
              "请先统一为 LF（否则会被判 MUTATION-INVALID）")
    orig_sha = sha256_bytes(original_bytes)
    print(f"源文件 {SRC.name}：{len(original.splitlines())} 行 / "
          f"{len(original_bytes)} B / sha256={orig_sha[:16]}")
    print("=" * 72)

    results = []
    try:
        # 1) 基线：三个目标判据 + 全量都必须绿
        baseline_ids = [tid for _, _, ids, _ in MUTATIONS for tid in ids]
        code, out = run_tests(baseline_ids)
        print(f"[基线·目标判据] exit={code}  "
              f"{'✅ 绿' if code == 0 and is_green(out) else '❌ 红（基线不绿，变异无意义）'}")
        if code != 0 or not is_green(out):
            print(out[-3000:])
            return 1
        code, out = run_tests([TEST])
        print(f"[基线·全量] exit={code}  "
              f"{'✅ 绿' if code == 0 and is_green(out) else '❌ 红'}")
        if code != 0 or not is_green(out):
            print(out[-4000:])
            return 1

        # 2) 逐个变异
        for tag, desc, ids, patch in MUTATIONS:
            mutated = patch(original)
            if mutated == original:
                print(f"\n[{tag}] ⚠️ 替换没匹配上：这是**变异体自身**的问题（MUTATION-INVALID）")
                results.append((tag, desc, "MUTATION-INVALID"))
                continue
            SRC.write_bytes(mutated.encode("utf-8"))
            try:
                code, out = run_tests(ids)
                caught = code != 0 or not is_green(out)
                results.append((tag, desc, "CAUGHT" if caught else "ESCAPED"))
                print(f"\n[{tag}] {desc}")
                print(f"  目标：{', '.join(i.split('.', 1)[1] for i in ids)}")
                print(f"  exit={code}  "
                      f"{'✅ CAUGHT（判据变红）' if caught else '❌ ESCAPED（判据没红！）'}")
                for line in out.splitlines():
                    if line.startswith(("FAIL:", "ERROR:")) or "AssertionError" in line:
                        print(f"    {line.strip()[:150]}")
            finally:
                # 逐字节还原（每一次变异后都立刻还原，绝不共享"脏"状态）
                SRC.write_bytes(original_bytes)
                restored_sha = sha256_bytes(SRC.read_bytes())
                if restored_sha != orig_sha:
                    print(f"  ❌ 还原失败：sha256 {restored_sha[:16]} != {orig_sha[:16]}")
                    return 1
                print(f"  ↩️ 已还原（字节 sha256={restored_sha[:16]} 全等）")
    finally:
        # 兜底：任何异常路径都把原字节写回去
        if sha256_bytes(SRC.read_bytes()) != orig_sha:
            SRC.write_bytes(original_bytes)
            print("[finally] 源码已按字节强制还原")

    # 3) 恢复后全量复跑
    code, out = run_tests([TEST])
    green = code == 0 and is_green(out)
    print("\n" + "=" * 72)
    print("变异汇总")
    print("=" * 72)
    for tag, desc, verdict in results:
        print(f"  {tag}  {verdict:16} {desc}")
    print(f"\n[恢复后] exit={code}  {'✅ 全绿' if green else '❌ 仍红'}")
    if not green:
        print(out[-3000:])
    bad = [r for r in results if r[2] != "CAUGHT"]
    if bad or not green:
        print(f"\n结论：❌ 未通过（{len(bad)} 个变异体没被抓住，或恢复后不绿）")
        return 1
    print("结论：✅ 3/3 变异体 CAUGHT；源码逐字节还原；恢复后全量绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
