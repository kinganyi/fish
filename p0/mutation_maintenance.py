#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`test_maintenance.py` 的变异驱动（M1/M2/M4/M5 的判别力自证）。

三个变异，每个都必须让对应判据**变红**，而且不是靠"改坏代码让它崩"，是靠
**真的做错事**：

  · **M5 / M1**：`_within_interval` 恒返回 False（间隔闸门失效）
    → M1（第二次全部 `skipped(interval)`）必红。M5 与 M1 是同一段代码的
      两种叫法：55 号书里 M5 就是"去掉间隔判断 → 重复执行"。
  · **M2**：去掉单任务的 `except Exception`（让异常向上抛）
    → M2（失败只记台账、不阻塞后续）必红。
  · **M4**：`corpus_ingest` 的配置闸门改成恒真（默认开）
    → M4（默认关时一次都不许调采集）必红。

流程：**基线绿 → 变异红 → 逐字节还原（sha256 比对）→ 恢复后全量绿**。
还原用 `try/finally` + 字节级校验：中途崩了脚本也会把源码写回去；
最后还会把"还原后的文件字节"与"进脚本前的字节"做一次 sha256 全等断言。

⚠️ 脚本只动**本 worktree 的 `p0/fish_maintenance.py`**，且每次都还原；
不碰 `fish.py` / `fish_kb.py` / `fish_kb_index.py` / 既有测试。
"""
import hashlib
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "fish_maintenance.py"
TEST = "test_maintenance"


def run_tests(test_ids):
    """跑指定测试，返回 `(exit_code, stdout+stderr)`。"""
    proc = subprocess.run(
        [sys.executable, "-m", "unittest"] + list(test_ids) + ["-v"],
        cwd=str(HERE), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def is_green(out):
    """判定：末段有 `OK`（或 `OK (skipped=…)`）且没有 FAILED。

    不要写成 `"FAILED" in out is False`：那是比较链，永远为假。
    """
    return "FAILED" not in out and re.search(r"^OK\b", out, re.M) is not None


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _replace(source, old, new):
    if old not in source:
        raise SystemExit("变异锚点没找到（源码已改？）：%r" % old)
    return source.replace(old, new, 1)


# ---------------------------------------------------------------- 变异体


def _mut_interval(source):
    """M5/M1：间隔判断恒过期。"""
    old = ("        return last + datetime.timedelta(hours=float(interval_hours)) "
           "> now_dt")
    new = "        return False   # 变异 M1：间隔闸门失效（每次都要重跑）"
    return _replace(source, old, new)


def _mut_swallow(source):
    """M2：不再兜单任务异常。"""
    old = ("        except Exception as exc:          "
           "# noqa: BLE001 —— 单任务异常不阻塞后续")
    new = ("        except ZeroDivisionError as exc:  "
           "# 变异 M2：不再兜单任务异常")
    return _replace(source, old, new)


def _mut_corpus_default(source):
    """M4：corpus_ingest 闸门恒真（默认开）。"""
    old = ('    if not _as_bool(_cfg_required(deps, "corpus.daily_enabled", False)):\n'
           "        raise _Skip(REASON_CONFIG)")
    new = ("    if False:   # 变异 M4：corpus 默认开\n"
           "        raise _Skip(REASON_CONFIG)")
    return _replace(source, old, new)


MUTATIONS = (
    ("M5/M1", "间隔判断恒过期（间隔闸门失效）",
     [f"{TEST}.M1IntervalTest.test_M1_second_run_is_skipped_forced_reruns"],
     _mut_interval),
    ("M2", "去掉单任务 try/except（异常向上抛）",
     [f"{TEST}.M2IsolationTest.test_M2_error_recorded_and_others_still_run"],
     _mut_swallow),
    ("M4", "corpus_ingest 闸门恒真（默认开）",
     [f"{TEST}.M4CorpusGateTest.test_M4_corpus_ingest_default_off_then_config_on"],
     _mut_corpus_default),
)


def main():
    original = SRC.read_bytes()
    digest = sha256_bytes(original)
    pristine = original.decode("utf-8")

    base_ids = [test_id for _, _, ids, _ in MUTATIONS for test_id in ids]
    print("[baseline] %d 条判据（变异前必须全绿）" % len(base_ids))
    rc, out = run_tests(base_ids)
    if not is_green(out):
        print(out)
        print("[baseline] 基线不绿，变异结果没有意义：先修实现/判据")
        return 2
    print("[baseline] OK\n")

    rows = []
    try:
        for name, desc, ids, mutate in MUTATIONS:
            mutated = mutate(pristine)
            if mutated == pristine:
                raise SystemExit(f"{name} 变异没有改变源码")
            SRC.write_bytes(mutated.encode("utf-8"))
            try:
                rc, out = run_tests(ids)
            finally:
                SRC.write_bytes(original)
                if sha256_bytes(SRC.read_bytes()) != digest:
                    raise SystemExit(f"{name} 变异后还原失败（sha256 不等）")
            caught = (not is_green(out)) and ("FAILED" in out)
            rows.append((name, desc, "CAUGHT" if caught else "MISSED"))
            print("[%s] %s -> %s" % (name, desc, "CAUGHT" if caught else "MISSED"))
            if not caught:
                print(out)
    finally:
        SRC.write_bytes(original)

    if sha256_bytes(SRC.read_bytes()) != digest:
        print("源码与进入脚本前的字节不一致（还原失败）")
        return 3

    print("\n[restore] 源码已逐字节还原（sha256 %s…）" % digest[:12])
    rc, out = run_tests(base_ids)
    green = is_green(out)
    print("[restore] 恢复后基线：%s" % ("OK" if green else "FAILED"))
    if not green:
        print(out)

    print("\n=== 变异自证结果 ===")
    for name, desc, verdict in rows:
        print("%-6s %-8s %s" % (name, verdict, desc))
    all_caught = all(verdict == "CAUGHT" for _, _, verdict in rows)
    print("CAUGHT %d/%d" % (sum(1 for _, _, v in rows if v == "CAUGHT"), len(rows)))
    return 0 if (all_caught and green) else 1


if __name__ == "__main__":
    sys.exit(main())
