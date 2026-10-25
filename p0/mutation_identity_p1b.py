#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
身份层 P1-b（session 命令族）· 变异自证驱动
=============================================
用途：**改真源码 → 跑 → 必红 → 字节还原**。十条变异，一条都不许"照绿"。

为什么不只靠 test_identity_p1b.py 里的 `*_mutation_*` 用例：
  那些用例用 mock.patch 模拟"把某一环弄坏"，可重跑、能进 CI，但**不等于**真删源码——
  mock 可能掩盖"摘掉之后代码路径根本没走到"。这个脚本改的是磁盘上的**字节**，用独立的
  pytest 进程跑目标判据，证明的是"真把这段删了，那条判据真的会红"。
  两层互为补充：CI 守"判据有判别力"，本脚本守"删源码真的会红"。

两个目标文件（P1-b 只碰这两个）：
  · fish.py        —— 八条签发/吊销/档期/落盘/凭证格式
  · mcp_server.py  —— 两条"不许注册 session 工具"（判据 H）

纪律（与 mutation_identity_p1a.py 同一套）：
  · 读写走字节（文件是 LF 结尾的，文本模式在 Windows 上会把 LF 改成 CRLF → 还原不干净）；
  · 每改一次都比对 sha256，结束后**必须**逐字节还原并复核；
  · 任何异常（进程崩溃 / Ctrl-C）都走 finally 还原；
  · 还原失败 → 立刻非零退出并打印人工恢复步骤（备份在 历史版本\\）。

用法：
    python mutation_identity_p1b.py             # 全跑（约 1-2 分钟）
    python mutation_identity_p1b.py --only M-E  # 只跑一条
    python mutation_identity_p1b.py --list      # 只列清单
"""
import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEST_FILE = "test_identity_p1b.py"
TEST_CLASS = "FishIdentityP1bTest"

TARGETS = {
    "fish.py": HERE / "fish.py",
    "mcp_server.py": HERE / "mcp_server.py",
}

# 一条变异 = (编号, 守哪条判据, 目标文件, 目标用例, 原样片段, 改成什么, 说明)
MUTATIONS = (
    (
        "M-A", "A", "fish.py", "test_A_start_issues_token",
        '    conn.execute(\n'
        '        "INSERT INTO session_tokens (token_hash,citizen_id,session_key,evidence,channel,"\n'
        '        "parent_token,issued_at,expires_at) VALUES (?,?,?,?,?,?,?,?)",\n'
        '        (thash, citizen_id, session_key, evidence, channel, parent_hash, issued_at,\n'
        '         expires_at))\n',
        '    _dropped = (thash, citizen_id, session_key, evidence, channel, parent_hash,\n'
        '                issued_at, expires_at)   # 变异：签发了但不入库\n',
        "把凭证的 INSERT 摘掉（token 照样返回，但库里没有这一行）",
    ),
    (
        "M-A2", "A", "fish.py", "test_A_start_issues_token",
        '    token = f"{SESSION_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"\n',
        "    token = secrets.token_urlsafe(32)   # 变异：去掉 fsh_ 前缀\n",
        "去掉 token 的 fsh_ 前缀（约 1.3% 的 token 会以 `-` 开头 → argparse 把凭证当选项）",
    ),
    (
        "M-B", "B", "fish.py", "test_B_idempotent_same_env",
        "    found = find_session_citizen(conn, session_key)\n",
        "    found = None   # 变异：永不复用已有会话公民\n",
        "跳过『按 host_session_key 找已有公民』（每次 start 都新建）",
    ),
    (
        "M-C", "C", "fish.py", "test_C_evidence_grading",
        '        return {"session_key": f"{SELF_SESSION_PREFIX}{reported}",\n'
        '                "evidence": "self-reported", "channel": "cli", "host": "cli",\n',
        '        return {"session_key": f"{SELF_SESSION_PREFIX}{reported}",\n'
        '                "evidence": "machine", "channel": "cli", "host": "cli",\n',
        "自报级也贴 machine 标签（等价于『永远返回 machine』）",
    ),
    (
        "M-D", "D", "fish.py", "test_D_multi_project_stints",
        '    from_ts = now_utc()\n'
        '    cur = conn.execute(\n'
        '        "INSERT INTO project_stints (citizen_id,account_id,project,from_ts,set_by) "\n',
        '    close_open_stints(conn, citizen_id, reason="project_switch")   # 变异：先关后开\n'
        '    from_ts = now_utc()\n'
        '    cur = conn.execute(\n'
        '        "INSERT INTO project_stints (citizen_id,account_id,project,from_ts,set_by) "\n',
        "把「先关后开」加回去（2026-10-24 语义变更前的老行为）→ 旧项目的档期被关掉",
    ),
    (
        "M-E", "E", "fish.py", "test_E_agent_one_hop",
        '    if parent["evidence"] != "machine":\n',
        "    if False:   # 变异：不再检查父的证据级别（允许链式继承）\n",
        "摘掉一跳守卫里的证据级别检查（inherited 也能当父）",
    ),
    (
        "M-F", "F", "fish.py", "test_F_end_revokes",
        "    return conn.execute(sql, params).rowcount\n",
        "    return 0   # 变异：吊销语句算了但不执行\n",
        "吊销变成空操作（end 之后 token 仍然有效）",
    ),
    (
        "M-G", "G", "fish.py", "test_G_token_never_written_to_disk",
        "    _print_session_start(res)\n",
        '    (Path(args.root) / ".fish-token").write_text(res["token"], encoding="utf-8")\n'
        "    _print_session_start(res)\n",
        "把 token 明文写进 <root>/.fish-token（凭证落盘）",
    ),
    (
        "M-H1", "H", "mcp_server.py", "test_H_no_mcp_session_tool",
        "TOOLS = [\n",
        "TOOLS = [\n"
        "    {\n"
        '        "name": "session",\n'
        '        "title": "会话身份",\n'
        '        "description": "变异：伪造的 MCP 签发入口",\n'
        '        "inputSchema": {"type": "object", "properties": {}},\n'
        "    },\n",
        "往 MCP 工具表里塞一个 session 工具（工具表 11 → 12）",
    ),
    (
        "M-H2", "H", "mcp_server.py", "test_H_no_mcp_session_tool",
        '        table = {\n            "remember": self.tool_remember,\n',
        '        table = {\n            "session": self.tool_healthcheck,\n'
        '            "remember": self.tool_remember,\n',
        "往 MCP 分发里挂 session（工具表没变，但真能被调起来）",
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
    """跑目标用例，返回 (是否通过, 最后一行输出)。判据"红"= 不通过（失败或报错都算）。"""
    node = f"{TEST_FILE}::{TEST_CLASS}::{test_name}"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", node, "-q", "--no-header"],
        cwd=str(HERE), capture_output=True)
    out = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    lines = [ln.strip() for ln in out.strip().splitlines() if ln.strip()]
    tail = lines[-1] if lines else "(无输出)"
    return proc.returncode == 0, tail[:160]


def apply_mutation(path: Path, old: str, new: str) -> str:
    """就地改源码，返回改之前的原文（用于还原）。找不到/多处命中就报错，绝不静默跳过。"""
    text = read_source(path)
    hits = text.count(old)
    if hits != 1:
        raise AssertionError(f"{path.name} 里变异片段命中 {hits} 次（要求恰好 1 次）：{old[:60]!r}")
    write_source(path, text.replace(old, new))
    return text


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="身份层 P1-b 变异自证")
    parser.add_argument("--only", help="只跑某一条（如 M-E）")
    parser.add_argument("--list", action="store_true", help="只列变异清单，不执行")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):        # 控制台是 GBK，不让符号把输出搞崩
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.list:
        for mut_id, judgment, fname, test_name, _o, _n, note in MUTATIONS:
            print(f"{mut_id:<6}{judgment:<4}{fname:<15}{test_name:<40}{note}")
        return 0

    baseline = {name: sha256(path) for name, path in TARGETS.items()}
    for name, path in TARGETS.items():
        print(f"基线 {name}：{len(path.read_bytes())} 字节 / sha256 {baseline[name][:16]}…")

    # 环境自证：先跑一遍未变异的用例，绿了才有资格谈"变异后变红"
    passed, tail = run_test("test_A_start_issues_token")
    if not passed:
        print(f"[X] 环境自证失败：未变异的判据 A 都没过（{tail}）——先修环境再看变异")
        return 2
    print("环境自证：未变异时判据 A 通过 [OK]\n")

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
                    f"用 历史版本\\{fname}.<时间戳>.bak 覆盖 {fname}")

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

    print("=" * 92)
    for mut_id, judgment, red, restored_ok, note, _tail in rows:
        print(f"{mut_id:<6}{judgment:<5}{'红/预期' if red else '[X]意外':<12}"
              f"{'已还原' if restored_ok else '[X]未还原':<12}{note}")
    print("=" * 92)
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
