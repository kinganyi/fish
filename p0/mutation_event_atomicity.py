#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异自证：P1-2「状态 + 事件同事务」的判据**能不能红**。

驱动方式（改**真源码** → 跑指定判据 → 必红 → 字节级还原）：
    python mutation_event_atomicity.py

每个变异体只改"最可能被写错的那一句"：
  · 把某函数改回「先 commit 状态、再 append_event」→ 判据必须红；
  · 删掉 `conn.rollback()` → 判据必须红（同连接会看见未回滚的脏状态）；
  · 让嵌套的 `find_or_create_session_citizen` 恢复默认提交 → start 的失败注入必须红；
  · 把 `fish_db.set_config` 改回先提交 → config 判据必须红。

基线先全绿（否则"红"说明不了任何事）。还原后复跑必须全绿，且每个文件 sha256
与基线逐字节一致；任何一步失败立即中止并保留备份。
"""
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

P0 = Path(__file__).resolve().parent

# (编号, 说明, 目标文件, 旧片段, 新片段, 判据节点)
MUTATIONS = [
    ("M-RA-COMMIT", "record_access 改回「先 commit 状态、再 append_event」",
     "fish_memory.py",
     '        append_event(conn, "memory.accessed",\n'
     '                     {"citizen_id": actor or "unknown", "evidence_level": "machine"},\n'
     '                     {"kind": "memory", "id": codes[0] if len(codes) == 1 else "*"},\n'
     '                     {"codes": codes, "source": source, "counted": bool(counted)}, commit=False)\n'
     '        conn.commit()\n'
     '    except Exception: conn.rollback(); raise\n'
     '    return len(codes)\n',
     '        conn.commit()          # 变异：状态先落盘\n'
     '        append_event(conn, "memory.accessed",\n'
     '                     {"citizen_id": actor or "unknown", "evidence_level": "machine"},\n'
     '                     {"kind": "memory", "id": codes[0] if len(codes) == 1 else "*"},\n'
     '                     {"codes": codes, "source": source, "counted": bool(counted)}, commit=False)\n'
     '        conn.commit()\n'
     '    except Exception: conn.rollback(); raise\n'
     '    return len(codes)\n',
     ["test_event_atomicity.py::MemoryAtomicityTest"
      "::test_record_access_failure_rolls_back_counters"]),

    ("M-RA-NOROLLBACK", "record_access 删掉 rollback（只留 raise）",
     "fish_memory.py",
     '                     {"codes": codes, "source": source, "counted": bool(counted)}, commit=False)\n'
     '        conn.commit()\n'
     '    except Exception: conn.rollback(); raise\n'
     '    return len(codes)\n',
     '                     {"codes": codes, "source": source, "counted": bool(counted)}, commit=False)\n'
     '        conn.commit()\n'
     '    except Exception: raise                 # 变异：删掉 rollback\n'
     '    return len(codes)\n',
     ["test_event_atomicity.py::MemoryAtomicityTest"
      "::test_record_access_failure_rolls_back_counters"]),

    ("M-SS-COMMIT", "start_session 改回「先 commit 状态、再 append_event」",
     "fish_session.py",
     '        append_event(\n'
     '            conn, "session.opened",\n',
     '        conn.commit()          # 变异：状态先落盘\n'
     '        append_event(\n'
     '            conn, "session.opened",\n',
     ["test_event_atomicity.py::SessionAtomicityTest"
      "::test_start_session_failure_rolls_back_all_state"]),

    ("M-SS-NOROLLBACK", "start_session 删掉 rollback（只留 raise）",
     "fish_session.py",
     '        conn.commit()\n'
     '    except Exception: conn.rollback(); raise\n'
     '    # FB-08：签发后查**同名在用公民**。查在签发之后是刻意的——名字不唯一是既定\n',
     '        conn.commit()\n'
     '    except Exception: raise                 # 变异：删掉 rollback\n'
     '    # FB-08：签发后查**同名在用公民**。查在签发之后是刻意的——名字不唯一是既定\n',
     ["test_event_atomicity.py::SessionAtomicityTest"
      "::test_start_session_failure_rolls_back_all_state"]),

    ("M-NESTED-COMMIT", "start 的 find_or_create 恢复默认提交（嵌套事件提前落盘）",
     "fish_session.py",
     '                kind=kind, role=role, commit=False)\n',
     '                kind=kind, role=role)          # 变异：嵌套 append_event 恢复默认提交\n',
     ["test_event_atomicity.py::SessionAtomicityTest"
      "::test_start_session_nested_event_failure_rolls_back_citizen"]),

    ("M-SC-COMMIT", "fish_db.set_config 改回「先 commit 配置、再 append_event」",
     "fish_db.py",
     '        )\n'
     '        if emit_event:\n'
     '            append_event(conn, "config.changed",\n',
     '        )\n'
     '        conn.commit()          # 变异：配置先落盘\n'
     '        if emit_event:\n'
     '            append_event(conn, "config.changed",\n',
     ["test_event_atomicity.py::ConfigAtomicityTest"
      "::test_set_config_failure_rolls_back_value"]),
]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run(nodes) -> int:
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", *nodes],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=str(P0))
    return r.returncode


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:      # noqa: BLE001
            pass

    touched = sorted({m[2] for m in MUTATIONS})
    backups = {name: P0 / (name + ".mea-bak") for name in touched}
    base = {}
    for name in touched:
        path = P0 / name
        base[name] = sha(path)
        shutil.copy2(path, backups[name])
        print(f"基线 {name} sha256 = {base[name][:16]}…")

    nodes = sorted({n for *_, ns in MUTATIONS for n in ns})
    print(f"\n=== 基线（未变异）应全绿：{len(nodes)} 条判据 ===")
    bad_base = False
    for node in nodes:
        rc = run([node])
        print(f"  {node:86} {'绿 ✓' if rc == 0 else f'红 ✗ (rc={rc})'}")
        bad_base = bad_base or rc != 0
    if bad_base:
        print("\n基线不全绿，终止（备份在 p0/*.mea-bak）。")
        for name in touched:
            shutil.copy2(backups[name], P0 / name)
            backups[name].unlink(missing_ok=True)
        return 1

    results = []
    for mid, desc, name, old, new, tests in MUTATIONS:
        path = P0 / name
        src = path.read_text(encoding="utf-8")
        if src.count(old) != 1:
            print(f"\n{mid}: 片段命中 {src.count(old)} 次（必须恰好 1 次），跳过")
            results.append((mid, desc, "SKIP"))
            continue
        path.write_text(src.replace(old, new, 1), encoding="utf-8", newline="")
        try:
            rc = run(tests)
            status = "红（符合预期）" if rc != 0 else "绿（变异没生效！）"
            print(f"\n{mid} [{status}] {desc}")
            print(f"    文件: {name} / 判据: {', '.join(tests)}")
            results.append((mid, desc, status))
        finally:
            shutil.copy2(backups[name], path)
            restored = sha(path) == base[name]
            print(f"    还原: {'逐字节一致 ✓' if restored else '不一致 ✗'}")
            if not restored:
                print(f"    ✗✗ 还原失败，中止！（备份在 {backups[name]}）")
                return 2

    print("\n=== 还原后复跑 ===")
    after_ok = True
    for node in nodes:
        rc = run([node])
        print(f"  {node:86} {'绿 ✓' if rc == 0 else f'红 ✗ (rc={rc})'}")
        after_ok = after_ok and rc == 0

    print("\n" + "=" * 86)
    for mid, desc, status in results:
        print(f"{mid:16} {status:18} {desc}")
    print("=" * 86)
    all_red = all(s == "红（符合预期）" for _, _, s in results)
    same = all(sha(P0 / name) == base[name] for name in touched)
    print(f"变异全部变红: {'是 ✓' if all_red else '否 ✗'}；"
          f"源码逐字节还原: {'是 ✓' if same else '否 ✗'}；"
          f"复跑全绿: {'是 ✓' if after_ok else '否 ✗'}")
    for name in touched:
        backups[name].unlink(missing_ok=True)
    return 0 if (all_red and same and after_ok) else 2


if __name__ == "__main__":
    sys.exit(main())
