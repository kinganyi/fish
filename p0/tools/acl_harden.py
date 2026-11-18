#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""收紧数据目录的 Windows ACL（F-P1-1，2026-11-18 外部审查）。

## 审查方实测的问题（已复核成立）

```
$ icacls data\\index.sqlite
  NT AUTHORITY\\Authenticated Users:(I)(F)    ← 任何已认证用户：完全控制
  BUILTIN\\Users:(I)(F)                       ← 所有本机用户：完全控制
```

`(I)` = **继承自上层目录**（`data/` 上是 `(I)(OI)(CI)(F)`）。后果：同机任何用户
都能**读**全部记忆与语料、**改**身份锚 `fish_identity.json`——改完就不需要 token
也能绑 owner。

这不是"权限配错了"，是**默认继承**：本机 `I:\\app\\DSH-Home\\workspace\\...` 这一路的
上层目录给了 `BUILTIN\\Users` 完全控制，于是每个新文件都带着它。

## 收紧到什么程度

**保留**：`SYSTEM`、`BUILTIN\\Administrators`、当前用户。这三个是系统运行与排障必需的
——删掉 SYSTEM 会让备份/索引服务读不到，删掉 Administrators 会让机器管理员自己也进不去，
那已经不是"收紧"而是"自锁"。

**移除**：`NT AUTHORITY\\Authenticated Users`、`BUILTIN\\Users`。这两个是"任何登录用户"
与"本机所有用户"，单用户机器上它们没有任何正当用途。

**中断继承**（`/inheritance:r`）：否则父目录的宽松 ACE 会继续往下灌，移除只是临时的。

## 用法

```
python tools/acl_harden.py            # 只报告现状与计划（默认，不改任何东西）
python tools/acl_harden.py --apply    # 真执行
python tools/acl_harden.py --verify   # 只校验（给判据用；不合规时非零退出）
```

## 为什么默认 dry-run

这是**改文件系统权限**。改错了的后果不是"测试红"，是"文件打不开"。所以默认只看不做，
`--apply` 必须显式给。
"""
import argparse
import os
import subprocess
import sys

# 第三轮评审：本文件打印 ✓ / ✗ 这些符号，而 Windows 控制台默认是 GBK，
# 直接 print 会抛 UnicodeEncodeError → **退出码 1**。
# 这是本项目已经踩过的坑（`fish.py` 与几个 mutation 脚本都修过），
# 但当时只修了那两个脚本，没扩到 `tools/*.py`——于是这条红在第三轮评审时
# 是"今天唯一失败的判据"。教训：修一个坑时要问"这个坑还在哪些同族文件里"。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass  # A档：拿不到可重配的流（如已被重定向到 StringIO）就按原样


HERE = os.path.dirname(os.path.abspath(__file__))
P0 = os.path.dirname(HERE)

# 要保护的目标（相对 p0）。`data` 是整棵（含 kb / thumb / projects）；
# `fish_identity.json` 是身份锚——它被改了，token 校验就形同虚设。
TARGETS = ("data", "fish_identity.json")

# 必须保留的（系统运行 / 排障）
KEEP = ("SYSTEM", "Administrators")
# 必须移除的（"任何登录用户" / "本机所有用户"）。
#
# ## 为什么删除要用 SID 而不是名字（2026-11-18 实测）
#
# 第一次用 `icacls /remove "Authenticated Users"` 在这台**中文 Windows** 上返回
# **rc=1332（账户名与 SID 无法映射）**——名字解析依赖系统语言，而权限收紧失败的
# 表现不是"命令报错"，是"该删的没删掉"，很容易被当成已经收紧。
#
# icacls 输出里仍用英文显示这些知名 SID，所以**检测**按名字没问题；
# 但**删除**必须用 `*SID`：S-1-5-11 = Authenticated Users，S-1-5-32-545 = BUILTIN\Users。
DROP_SIDS = ("*S-1-5-11", "*S-1-5-32-545")
DROP = ("Authenticated Users", "Users")   # 仅用于**检测**（icacls 输出里是英文）


def _run(args, cwd=None):
    r = subprocess.run(args, capture_output=True, text=True,
                       encoding="mbcs", errors="replace", cwd=cwd)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def current_user():
    """当前用户的 **SID**（icacls 用 `*SID` 形态引用）。

    为什么不用 `域\\用户名`：本机的 `USERDOMAIN` 是「未知设备」（中文占位名），
    拼出来的 `未知设备\\15596` 在 icacls 里**能不能解析取决于本地化实现**——
    而权限收紧失败的表现是"文件突然打不开"，不是"命令报错"。SID 与语言无关，
    是这个场景唯一稳妥的写法。
    """
    rc, out = _run(["whoami", "/user"])
    import re as _re
    m = _re.search(r"(S-1-5-21-[0-9\-]+)", out or "")
    if not m:
        raise SystemExit("错误：拿不到当前用户 SID（whoami /user 失败）——"
                         "权限收紧中止，不做半套")
    return "*" + m.group(1)


def read_acl(path):
    """返回 (rc, 原始输出, [(身份, 权限串)])。"""
    rc, out = _run(["icacls", path])
    entries = []
    for line in out.splitlines():
        s = line.strip()
        if not s or s.lower().startswith(("successfully", "processed", "failed")):
            continue
        # 形态： "  BUILTIN\\Users:(I)(F)" 或带中文提示的变体
        if ":" not in s:
            continue
        ident, _, perms = s.partition(":")
        ident = ident.strip()
        if not ident or " " not in ident and "\\" not in ident and ident.isupper():
            continue
        entries.append((ident, perms.strip()))
    return rc, out, entries


def inspect(targets=TARGETS):
    """体检：每个目标是否仍带着宽泛组。"""
    me = current_user()
    report = []
    for rel in targets:
        path = os.path.join(P0, rel)
        if not os.path.exists(path):
            report.append({"target": rel, "exists": False, "wide": [], "ok": None})
            continue
        rc, out, entries = read_acl(path)
        wide = []
        for ident, perms in entries:
            low = ident.lower()
            if any(d.lower() in low for d in DROP):
                wide.append(ident)
        report.append({"target": rel, "exists": True, "rc": rc,
                       "wide": sorted(set(wide)), "ok": not wide,
                       "me": me, "raw": out})
    return report


def harden(targets=TARGETS, apply=False):
    """中断继承 + 移除宽泛组 + 保留 SYSTEM/Administrators/当前用户。"""
    me = current_user()
    actions = []
    for rel in targets:
        path = os.path.join(P0, rel)
        if not os.path.exists(path):
            actions.append((rel, "skip", "不存在"))
            continue
        is_dir = os.path.isdir(path)
        # ① 中断继承：把继承来的 ACE 复制成本地显式 ACE 再断开（/inheritance:r 是直接丢弃，
        #    会把当前用户自己那份也丢掉——所以用 /inheritance:d 转成显式，然后手工删宽泛的）
        cmds = [["icacls", path, "/inheritance:d"]]
        # ② 递归移除宽泛组（/T 递归；/C 继续即使出错；失败项会被收集）
        for sid in DROP_SIDS:
            cmds.append(["icacls", path, "/remove", sid, "/T", "/C", "/Q"])
        # ③ 显式给当前用户完全控制（防止 ② 之后自己反而进不去）
        cmds.append(["icacls", path, "/grant",
                     (f"{me}:(OI)(CI)F" if is_dir else f"{me}:F")])
        for cmd in cmds:
            if not apply:
                actions.append((rel, "plan", " ".join(cmd)))
                continue
            rc, out = _run(cmd)
            actions.append((rel, "ok" if rc == 0 else "fail",
                            " ".join(cmd) + ("" if rc == 0 else f" → rc={rc} {out[:200]}")))
    return actions


def main(argv=None):
    ap = argparse.ArgumentParser(description="收紧 fish 数据目录的 ACL")
    ap.add_argument("--apply", action="store_true", help="真执行（默认只报告）")
    ap.add_argument("--verify", action="store_true", help="只校验；不合规时 exit 1")
    args = ap.parse_args(argv)

    me = current_user()
    print(f"当前用户：{me}")
    print(f"保留：{', '.join(KEEP)} + 当前用户")
    print(f"移除：{', '.join(DROP)}")
    print()

    report = inspect()
    wide_any = False
    for item in report:
        if not item["exists"]:
            print(f"  [{item['target']}] 不存在，跳过")
            continue
        if item["wide"]:
            wide_any = True
            print(f"  ✗ {item['target']}：仍带宽泛组 {item['wide']}")
        else:
            print(f"  ✓ {item['target']}：无宽泛组")

    if args.verify:
        if wide_any:
            print("\n✗ 校验失败：仍有目标带 'Authenticated Users' / 'Users'")
            return 1
        print("\n✓ 校验通过：所有目标已收紧")
        return 0

    if not args.apply:
        print("\n--- 计划（未执行；加 --apply 才动）---")
        for rel, state, detail in harden(apply=False):
            print(f"  [{rel}] {state}: {detail}")
        print("\n（dry-run 结束，未改动任何权限）")
        return 0

    print("\n--- 执行 ---")
    fails = []
    for rel, state, detail in harden(apply=True):
        tag = "✓" if state == "ok" else ("-" if state == "skip" else "✗")
        print(f"  {tag} [{rel}] {detail[:140]}")
        if state == "fail":
            fails.append((rel, detail))

    print("\n--- 执行后复核 ---")
    after = inspect()
    still = [i["target"] for i in after if i.get("wide")]
    if fails or still:
        print(f"✗ 仍有问题：失败动作 {len(fails)} 个；仍宽泛 {still}")
        return 1
    print("✓ 全部收紧")
    return 0


if __name__ == "__main__":
    sys.exit(main())
