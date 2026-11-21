#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""双跑器 · Python 侧（golden 基准）。

契约（与 `run.mjs` 严格一致）：
  输入 = `fixtures/*.json`
  输出 = `out/py.json`，形如
    `{"side": "python", "python": "3.11.9", "fixtures": [{id, op, status, output?|reason?}]}`

**只读纪律**：生产库从不被打开写入。每个 fixture 先把 `p0/data/index.sqlite` 用 SQLite
自己的 backup 通道复制到临时目录，再在**副本**上跑 `FishServer`（它会 `executescript` +
`migrate`，这是写操作——所以必须在副本上）。跑完即删。

为什么直接复用 `mcp_server.FishServer.call_tool`：这就是模型今天真正看到的那条路径。
自己再写一遍"等价的 Python 调用"等于换了一个基准，对拍就失去意义。

用法：python test/conformance/run.py [--out <file>] [--fixtures <dir>]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

CONFORMANCE_DIR = Path(__file__).resolve().parent
PACKAGE_ROOT = CONFORMANCE_DIR.parent.parent
P0_ROOT = PACKAGE_ROOT.parent / "p0"
SOURCE_DB = P0_ROOT / "data" / "index.sqlite"


def _configure_stdio() -> None:
    """Windows 控制台默认 cp936：不钉死 UTF-8 会把中文写成 GBK 字节（读端按 UTF-8 解码即乱码）。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
        sys.stderr.reconfigure(encoding="utf-8", errors="strict")
    except AttributeError:  # pragma: no cover
        pass


def _load_fixtures(fixtures_dir: Path) -> list[dict]:
    if not fixtures_dir.is_dir():
        raise SystemExit(f"fixtures 目录不存在：{fixtures_dir}")
    files = sorted(p for p in fixtures_dir.glob("*.json"))
    if not files:
        raise SystemExit(f"fixtures 目录为空：{fixtures_dir}")
    fixtures = []
    for file_path in files:
        data = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "id" not in data or "op" not in data:
            raise SystemExit(f"fixture 缺少 id/op：{file_path.name}")
        fixtures.append(data)
    return fixtures


def _snapshot_db(target_data_dir: Path, source_db: Path) -> Path:
    """把生产库复制到临时目录（只读源库，走 SQLite backup 通道）。"""
    target_data_dir.mkdir(parents=True, exist_ok=True)
    target = target_data_dir / "index.sqlite"
    src = sqlite3.connect(f"file:{source_db.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


def _resolve_snapshot(fixture: dict) -> Path:
    """fixture 可显式指定库快照（相对 `p0/` 或绝对路径）；缺省用生产库。"""
    raw = fixture.get("snapshotDb")
    if not raw:
        return SOURCE_DB
    candidate = Path(str(raw))
    if not candidate.is_absolute():
        candidate = P0_ROOT / candidate
    if not candidate.exists():
        raise FileNotFoundError(f"fixture 指定的库快照不存在：{candidate}")
    return candidate


def _run_snapshot_op(fixture: dict, server_module, fish_module):
    """在临时副本上跑一次 MCP 工具调用，返回回包文本。"""
    token_spec = fixture.get("token") or {"mode": "anonymous"}
    source_db = _resolve_snapshot(fixture)
    with tempfile.TemporaryDirectory(prefix="fish-conf-") as tmp:
        data_dir = Path(tmp) / "data"
        _snapshot_db(data_dir, source_db)
        server = server_module.FishServer(data_dir)
        try:
            ident = server_module.IDENTITY_ANONYMOUS
            if token_spec.get("mode") == "issue":
                citizen_id = str(token_spec["citizenId"])
                issued = fish_module.issue_session_token(
                    server.conn,
                    citizen_id=citizen_id,
                    session_key=str(token_spec.get("sessionKey") or "fixture:session"),
                    evidence=str(token_spec.get("evidence") or "machine"),
                    channel=str(token_spec.get("channel") or "cli"),
                )
                server.conn.commit()
                ident = server._resolve_identity(issued["token"])
                if ident.status != server_module.IDENTITY_VALID:
                    raise RuntimeError(f"fixture 签发的 token 未被识别：status={ident.status}")
            tool_name = fixture.get("toolName") or fixture["op"]
            args = dict(fixture.get("args") or {})
            text = server.call_tool(tool_name, args, ident)
            return {"text": text}
        finally:
            server.conn.close()


def main() -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="dsh-fish 双跑器 · Python 侧")
    parser.add_argument("--out", default=str(CONFORMANCE_DIR / "out" / "py.json"))
    parser.add_argument("--fixtures", default=str(CONFORMANCE_DIR / "fixtures"))
    args = parser.parse_args()

    if not SOURCE_DB.exists():
        print(f"找不到生产库：{SOURCE_DB}", file=sys.stderr)
        return 2

    sys.path.insert(0, str(P0_ROOT))
    import fish  # noqa: E402
    import mcp_server  # noqa: E402

    results = []
    for fixture in _load_fixtures(Path(args.fixtures)):
        base = {"id": fixture["id"], "op": fixture["op"], "file": fixture.get("file", "")}
        try:
            if fixture["op"] == "cut":
                inputs = fixture.get("inputs") or []
                if not inputs:
                    raise ValueError("cut fixture 缺少 inputs[]")
                results.append({**base, "status": "ok", "output": {"tokens": [fish.index_tokens(str(t)) for t in inputs]}})
            else:
                results.append({**base, "status": "ok", "output": _run_snapshot_op(fixture, mcp_server, fish)})
        except Exception as exc:  # noqa: BLE001 —— 单个 fixture 失败不该中断整批
            results.append({**base, "status": "error", "reason": f"{type(exc).__name__}: {exc}"})

    payload = {
        "side": "python",
        "python": sys.version.split()[0],
        "fixtures": results,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    ok = sum(1 for r in results if r["status"] == "ok")
    errors = sum(1 for r in results if r["status"] == "error")
    print(f"# run.py（Python 侧）→ {out_path}")
    for r in results:
        print(f"  {r['status']:<7} {r['id']}{('  ' + r['reason']) if r.get('reason') else ''}")
    print(f"# ok={ok} error={errors}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
