#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
身份层 P1-c（MCP 用会话凭证识别身份）· 变异自证驱动
=====================================================
用途：**改真源码 → 跑 → 必红 → 字节还原**。十二条变异，一条都不许"照绿"。

为什么不只靠 test_identity_p1c.py 里的 `*_mutation_*` 用例：
  那些用例用 mock.patch 模拟"把某一环弄坏"，可重跑、能进 CI，但**不等于**真删源码——
  mock 可能掩盖"摘掉之后代码路径根本没走到"（例如把整个函数换掉，被测分支压根没执行）。
  这个脚本改的是磁盘上的**字节**，用独立的 pytest 进程跑目标判据，证明的是
  "真把这段删了/改了，那条判据真的会红"。两层互为补充：
  CI 守"判据有判别力"，本脚本守"删源码真的会红"。

只碰一个目标文件（P1-c 的改动面）：
  · mcp_server.py —— 十项：A/B×2/C/D/E/F/G×2/H/I

判据 E 是**本轮核心**：M-E 把身份放回进程内存（等价于 2026-10-23 那台 pid 37192 的形状），
它必须让"两会话不串号"这条判据当场红。若 M-E 照绿，说明判据 E 没有判别力。

纪律（与 mutation_identity_p1a/p1b.py 同一套）：
  · 读写走字节（文件是 LF 结尾的，文本模式在 Windows 上会把 LF 改成 CRLF → 还原不干净）；
  · 每改一次都比对 sha256，结束后**必须**逐字节还原并复核；
  · 任何异常（进程崩溃 / Ctrl-C）都走 finally 还原；
  · 还原失败 → 立刻非零退出并打印人工恢复步骤（备份在 历史版本\\）。

用法：
    python mutation_identity_p1c.py              # 全跑（约 1-2 分钟）
    python mutation_identity_p1c.py --only M-E   # 只跑一条
    python mutation_identity_p1c.py --list       # 只列清单
"""
import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEST_FILE = "test_identity_p1c.py"
TEST_CLASS = "FishIdentityP1cTest"

TARGETS = {
    "mcp_server.py": HERE / "mcp_server.py",
}

# 一条变异 = (编号, 守哪条判据, 目标文件, 目标用例, 原样片段, 改成什么, 说明)
MUTATIONS = (
    (
        "M-A", "A", "mcp_server.py", "test_A_token_attribution",
        "        citizen_id = fish.validate_session_token(self.conn, token)\n",
        '        citizen_id = "C-PROC-ACTOR"   # 变异：不校验 token，直接给进程态身份\n',
        "身份不再来自 token，而是进程里一个写死的公民（等价于『退回进程态』）",
    ),
    (
        "M-B1", "B", "mcp_server.py", "test_B_no_token_is_unattributed",
        "        return ident.citizen_id or UNATTRIBUTED\n",
        '        return ident.citizen_id or "unknown"   # 变异：用旧占位值冒充"未归属"\n',
        "无凭证时写旧占位 'unknown'（不是明确的 'unattributed'）",
    ),
    (
        "M-B2", "B", "mcp_server.py", "test_B_no_token_is_unattributed",
        "        hint = None if name in HINT_SELF_EXPLAINED else self._identity_hint(ident)\n",
        "        hint = None   # 变异：不追加归属提示\n",
        "未归属时不告诉调用方（提示消失 → AI 以为自己已归属）",
    ),
    (
        "M-C", "C", "mcp_server.py", "test_C_fake_token_rejected",
        "        if info is None:\n            return IDENTITY_REJECTED\n",
        '        if info is None:\n'
        '            return Identity("C-FAKE", None, "self-reported", IDENTITY_VALID)   '
        "# 变异：假 token 也接受\n",
        "库里查不到的 token 也当有效（假凭证就能冒充一个身份）",
    ),
    (
        "M-D", "D", "mcp_server.py", "test_D_revoked_and_expired_tokens_rejected",
        "        if not citizen_id:\n",
        "        if False:   # 变异：不判吊销 / 过期\n",
        "摘掉有效性判定（只查'这行在不在'，不查 revoked_at / expires_at）",
    ),
    (
        "M-E", "E", "mcp_server.py", "test_E_two_sessions_do_not_cross_talk",
        "        if ident is None:\n"
        "            ident = self._resolve_identity((args or {}).get(TOKEN_ARG))\n",
        "        if not hasattr(self, \"_mut_identity\"):   # 变异：进程内记住第一个身份\n"
        "            self._mut_identity = self._resolve_identity("
        "(args or {}).get(TOKEN_ARG))\n"
        "        ident = self._mut_identity\n",
        "把身份放回进程内存（第一次解析后一直复用）→ 第二个会话沿用它 = 串号",
    ),
    (
        "M-F", "F", "mcp_server.py", "test_F_whoami_without_token_reveals_nobody",
        "        lines = []\n        if ident.status == IDENTITY_VALID:\n",
        "        lines = []\n"
        "        if ident.status != IDENTITY_VALID:   # 变异：无凭证时退回 env 身份\n"
        "            _m = fish.find_citizen_by_name(self.conn, os.environ.get(\"FISH_ACTOR\", \"\"))\n"
        "            if _m:\n"
        "                ident = Identity(_m[0], None, \"self-reported\", IDENTITY_VALID)\n"
        "        if ident.status == IDENTITY_VALID:\n",
        "whoami 无凭证时从 FISH_ACTOR 捞出名字当身份（旧实现的取证来源）",
    ),
    (
        "M-G1", "G", "mcp_server.py", "test_G_tool_signature_only_adds_optional_token",
        "        props[TOKEN_ARG] = dict(TOKEN_PROPERTY)\n",
        "        props[TOKEN_ARG] = dict(TOKEN_PROPERTY)\n"
        '        props["mutant_extra"] = {"type": "string"}   # 变异：多一个入参字段\n',
        "偷偷给工具多加一个入参字段（工具签名漂移）",
    ),
    (
        "M-G2", "G", "mcp_server.py", "test_G_tool_signature_only_adds_optional_token",
        "        props[TOKEN_ARG] = dict(TOKEN_PROPERTY)\n",
        "        props[TOKEN_ARG] = dict(TOKEN_PROPERTY)\n"
        '        tool["inputSchema"].setdefault("required", []).append(TOKEN_ARG)   '
        "# 变异：token 变必填\n",
        "把 token 塞进 required（旧调用不带 token 就会当场断）",
    ),
    (
        "M-H1", "H", "mcp_server.py", "test_H_no_process_identity_state",
        "        fish.migrate(self.conn)\n",
        "        fish.migrate(self.conn)\n"
        "        self._actor = None   # 变异：把进程态身份加回来\n",
        "把 `self._actor` 加回 __init__（进程态身份复活）",
    ),
    (
        "M-H2", "H", "mcp_server.py", "test_H_no_process_identity_state",
        "        fish.migrate(self.conn)\n",
        "        fish.migrate(self.conn)\n"
        "        self._session_actor = os.environ.get(\"FISH_SESSION_ACTOR\") or None   "
        "# 变异：会话名进进程\n",
        "把 `self._session_actor` 加回 __init__（事故里被串号的那个字段）",
    ),
    (
        "M-I", "I", "mcp_server.py", "test_I_token_never_persisted",
        "        citizen_id = fish.validate_session_token(self.conn, token)\n",
        "        citizen_id = fish.validate_session_token(self.conn, token)\n"
        "        (self.root / \"_audit\").mkdir(exist_ok=True)\n"
        "        (self.root / \"_audit\" / \"leak-token.txt\").write_text(token, "
        "encoding=\"utf-8\")   # 变异：凭证落盘\n",
        "把 token 明文写进 <root>/_audit/（凭证泄漏进长期日志）",
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
    parser = argparse.ArgumentParser(description="身份层 P1-c 变异自证")
    parser.add_argument("--only", help="只跑某一条（如 M-E）")
    parser.add_argument("--list", action="store_true", help="只列变异清单，不执行")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):        # 控制台是 GBK，不让符号把输出搞崩
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.list:
        for mut_id, judgment, fname, test_name, _o, _n, note in MUTATIONS:
            print(f"{mut_id:<6}{judgment:<4}{fname:<15}{test_name:<50}{note}")
        return 0

    baseline = {name: sha256(path) for name, path in TARGETS.items()}
    for name, path in TARGETS.items():
        print(f"基线 {name}：{len(path.read_bytes())} 字节 / sha256 {baseline[name][:16]}…")

    # 环境自证：先跑一遍未变异的用例，绿了才有资格谈"变异后变红"
    passed, tail = run_test("test_A_token_attribution")
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
                    f"用 历史版本\\{fname}.20261024-0315-p1c.bak 覆盖 {fname}")

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
        print(f"{mut_id:<6}{judgment:<5}{'红/预期' if red else '[X]意外':<12}"
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
