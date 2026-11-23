#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重拍 6 张验收截图，并**顺手把「不横向溢出」变成读数**（四栏后的版式判据）。

## 为什么截图要脚本化

评审上一轮的原话是"三张截图早于 dist 5–14 分钟，不能当当前树的读数"。手工截图的
时序靠人记，脚本化之后每张图的**文件名带时间戳**、并且同一进程里同时打印
`dist/index.html` 的 mtime——两者同屏可对账（`npm run smoke` 的 S6 也查这一条）。

## 除了图，还测什么

每张图都取同一组版面读数，**逐张打出来**（截图只能人眼看，读数才能机检）：

    scrollWidth / clientWidth   —— 横向溢出的唯一判据（`scrollWidth > clientWidth` = 溢出）
    gridTemplateColumns         —— 栏宽（现场是 `52px 200px … 320px`；设置是 `52px …`）
    navTabs                     —— 窄条里的页签数（现在必须 = 2：现场 + 设置）
    settingsHosts               —— 设置页左栏的宿主行数（`[data-host]`；其它页为 0）

## 用法

    python -X utf8 p0\\tools\\shoot_workbench.py --api http://127.0.0.1:8790 --static http://127.0.0.1:8799

只读：不构建、不改库、不改 `ui/dist`（只往 `ui/screenshots/` 写 png）。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_P0 = os.path.dirname(_HERE)
_REPO = os.path.dirname(_P0)
for path in (_P0, _HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

import accept_workbench as ACC  # noqa: E402  复用无头浏览器 / CDP / WebSocket 客户端

SHOT_DIR = os.path.join(_REPO, "ui", "screenshots")

PROBE = {
    "text": "document.body ? document.body.innerText : ''",
    "scrollWidth": "document.documentElement.scrollWidth",
    "clientWidth": "document.documentElement.clientWidth",
    "bodyScrollWidth": "document.body.scrollWidth",
    "navTabs": "document.querySelectorAll('nav[aria-label=\"顶级页签\"] [role=\"tab\"]').length",
    "columns": "getComputedStyle(document.querySelector('[data-page]').closest('.grid')).gridTemplateColumns",
    "columnsWidths": "Array.from(document.querySelectorAll('[data-page]').length ? document.querySelector('[data-page]').closest('.grid').children : []).map(function(n){return Math.round(n.getBoundingClientRect().width)}).join(',')",
    "settingsHosts": "document.querySelectorAll('[data-host]').length",
    "settingsDetail": "document.querySelectorAll('main[aria-label=\"设置\"] section[aria-label=\"宿主详情\"]')[0] ? document.querySelectorAll('main[aria-label=\"设置\"] section[aria-label=\"宿主详情\"]')[0].innerText.length : 0",
}


def shoot(url: str, path: str, width: int, height: int, wait_ms: int) -> dict:
    """渲染一个地址、取读数、写 PNG。返回读数（含 `overflow` 布尔）。"""
    browser = ACC.find_browser()
    if not browser:
        raise RuntimeError("找不到无头浏览器（Edge/Chrome）")
    profile = tempfile.mkdtemp(prefix="fish-shot-")
    port = ACC._free_port()
    proc = subprocess.Popen(
        [browser, "--headless=new", "--disable-gpu", "--no-first-run",
         "--no-default-browser-check", f"--user-data-dir={profile}",
         f"--remote-debugging-port={port}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        target = None
        for _ in range(80):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as resp:
                    pages = [t for t in json.loads(resp.read().decode()) if t.get("type") == "page"]
                    if pages:
                        target = pages[0]
                        break
            except Exception:  # noqa: BLE001  调试端口还没起来
                pass
            time.sleep(0.25)
        if not target:
            raise RuntimeError("CDP 目标没起来（调试端口绑不上）")
        ws = ACC._Ws(target["webSocketDebuggerUrl"])
        try:
            ws.call(1, "Page.enable")
            ws.call(2, "Runtime.enable")
            ws.call(3, "Emulation.setDeviceMetricsOverride",
                    {"width": width, "height": height, "deviceScaleFactor": 1,
                     "mobile": False})
            ws.call(4, "Page.navigate", {"url": url})
            time.sleep(wait_ms / 1000.0)
            out: dict = {}
            for index, (name, expression) in enumerate(PROBE.items(), start=10):
                result = ws.call(index, "Runtime.evaluate",
                                 {"expression": expression, "returnByValue": True})
                out[name] = result.get("result", {}).get("value")
            shot = ws.call(90, "Page.captureScreenshot",
                           {"format": "png", "captureBeyondViewport": False})
            with open(path, "wb") as handle:
                handle.write(base64.b64decode(shot["data"]))
        finally:
            ws.close()
    finally:
        ACC._kill_tree(proc)
        shutil.rmtree(profile, ignore_errors=True)
    out["overflow"] = int(out.get("scrollWidth") or 0) > int(out.get("clientWidth") or 0)
    out["url"] = url
    out["viewport"] = f"{width}x{height}"
    out["file"] = os.path.basename(path)
    out["textChars"] = len(out.pop("text") or "")
    return out


def first_key(api: str, scope: str) -> dict:
    """取一条**可进详情**的条目（折叠条不行：`/api/entry` 只解析 tool|post|push|event）。"""
    url = f"{api}/api/stream?win=24h&limit=30&scope={scope}"
    with urllib.request.urlopen(url, timeout=20) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    items = payload["data"]["items"]
    pick = next((item for item in items if not item.get("fold")), None)
    if pick is None:
        raise RuntimeError(f"{scope} 首屏 30 条全是折叠条，取不到可进详情的 key")
    return {"key": pick["key"], "project": pick.get("project"), "text": pick.get("text", "")[:40]}


def main() -> int:
    parser = argparse.ArgumentParser(description="重拍 5 张验收截图 + 版面读数（只读）")
    parser.add_argument("--api", default="http://127.0.0.1:8790")
    parser.add_argument("--static", default="http://127.0.0.1:8799")
    parser.add_argument("--stamp", default="", help="文件名时间戳（缺省取本地 HHMM）")
    parser.add_argument("--keep-old", action="store_true", help="不把旧截图挪进 _legacy-predist/")
    args = parser.parse_args()

    api = args.api.rstrip("/")
    static = args.static.rstrip("/")
    stamp = args.stamp or time.strftime("%H%M")
    if not os.path.isdir(SHOT_DIR):
        os.makedirs(SHOT_DIR, exist_ok=True)

    dist_index = os.path.join(_REPO, "ui", "dist", "index.html")
    dist_mtime = (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.path.getmtime(dist_index)))
                  if os.path.exists(dist_index) else "缺 dist/index.html")
    print(f"# 判定时刻 {ACC.iso_now()} · 本地戳 {stamp} · dist/index.html mtime {dist_mtime}")
    print(f"# API {api} · 静态 {static}")

    target = first_key(api, "project:CZHY")
    jobs = [
        ("01-main", f"{static}/?api={api}", 1440, 900),
        ("02-1280x720", f"{static}/?api={api}", 1280, 720),
        # 读口停掉：静态页 + 指到一个没人监听的端口 → 整屏「读口没有起来」
        ("03-static-apidown", f"{static}/?api=http://127.0.0.1:9", 1440, 900),
        ("04-project-czhy", f"{static}/?api={api}#/w?scope=project:CZHY&win=24h", 1440, 900),
        ("05-ledger-deeplink",
         f"{static}/?api={api}#/w?scope=project:CZHY&win=24h&sel={target['key']}&tab=ledger",
         1440, 900),
        # 设置页：**查询串深链**（`?page=settings`），与 `?api=` 共存（用户验收口径）
        ("06-settings", f"{static}/?api={api}&page=settings", 1440, 900),
    ]

    old = [name for name in os.listdir(SHOT_DIR) if name.endswith(".png")]
    if old and not args.keep_old:
        legacy = os.path.join(SHOT_DIR, "_legacy-predist")
        os.makedirs(legacy, exist_ok=True)
        for name in old:
            shutil.move(os.path.join(SHOT_DIR, name), os.path.join(legacy, name))
        print(f"# 旧截图 {len(old)} 张已挪进 screenshots/_legacy-predist/（留着对照，不算证据）")

    results = []
    fail = 0
    for label, url, width, height in jobs:
        path = os.path.join(SHOT_DIR, f"{label}-{stamp}.png")
        try:
            out = shoot(url, path, width, height, wait_ms=7000)
        except RuntimeError as exc:
            print(f"SKIP  {label}  {exc}")
            fail += 1
            continue
        results.append(out)
        flag = "OVERFLOW" if out["overflow"] else "ok"
        extra = (f"  宿主行={out['settingsHosts']}  详情字符={out['settingsDetail']}"
                 if out.get("settingsHosts") else "")
        print(f"SHOT  {out['file']}  {out['viewport']}  "
              f"scrollWidth={out['scrollWidth']} clientWidth={out['clientWidth']} [{flag}]  "
              f"栏宽={out['columnsWidths']}  窄条页签={out['navTabs']}  "
              f"文本 {out['textChars']} 字{extra}")
        if out["overflow"]:
            fail += 1

    print("\n# 汇总（逐张读数）")
    print(json.dumps({"shots": [{k: r[k] for k in
                                 ("file", "viewport", "scrollWidth", "clientWidth",
                                  "overflow", "columns", "columnsWidths", "navTabs",
                                  "settingsHosts", "settingsDetail")}
                                for r in results],
                      "entryKeyFor05": target["key"]}, ensure_ascii=False, indent=1))
    print(f"# 横向溢出张数 {sum(1 for r in results if r['overflow'])} / {len(results)}"
          f" · 截图 {len(results)} 张 · 失败 {fail}")
    return 0 if fail == 0 and len(results) == len(jobs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
