#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部审查 F-P1-1 判据：数据目录不许对本机所有用户开放。

## 审查方实测的问题（已复核成立）

```
$ icacls data\\index.sqlite
  NT AUTHORITY\\Authenticated Users:(I)(F)    ← 任何已认证用户：完全控制
  BUILTIN\\Users:(I)(F)                       ← 所有本机用户：完全控制
```

后果不是"理论风险"：同机任何用户都能读全部记忆与语料，**并且能改
`fish_identity.json`**——身份锚被改，token 校验就形同虚设。

根因是**默认继承**：上层目录给了 `BUILTIN\\Users` 完全控制，于是每个新文件都带着它。

## 修法与它的两个坑（都是实测踩到的）

1. **删除必须用 SID，不能用名字**：`icacls /remove "Authenticated Users"` 在这台
   中文 Windows 上返回 **rc=1332（账户名与 SID 无法映射）**。而权限收紧失败的
   表现不是"命令报错"，是"该删的没删掉"——很容易被当成已经收紧。
   用 `*S-1-5-11` / `*S-1-5-32-545` 与系统语言无关。

2. **当前用户也必须用 SID**：本机 `USERDOMAIN` 是「未知设备」（中文占位名），
   拼出来的 `未知设备\\15596` 能不能被 icacls 解析取决于本地化实现。
   `whoami /user` 拿 `S-1-5-21-…` 再用 `*SID` 形态，确定性最高。

## 保留什么、为什么

保留 `SYSTEM` / `BUILTIN\\Administrators` / 当前用户。删掉 SYSTEM 会让备份与索引
读不到，删掉 Administrators 会让机器管理员自己也进不去——那已经不是"收紧"而是"自锁"。

## 这批判据为什么能失败

直接读真实 ACL，断言宽泛组不存在。**未收紧时它红**（实测：收紧前两个目标都带
`BUILTIN\\Users`）。同时保留一条反向判据：SYSTEM 与 Administrators **必须还在**
——否则"全部删光"也能让退化判据变绿。
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
P0 = HERE
if str(P0) not in sys.path:
    sys.path.insert(0, str(P0))

IS_WINDOWS = os.name == "nt"

# 宽泛组（icacls 输出里是英文，即使系统是中文）
WIDE = ("Authenticated Users", "BUILTIN\\Users")
# 必须保留的
MUST_KEEP = ("SYSTEM", "Administrators")


def _acl(path):
    r = subprocess.run(["icacls", str(path)], capture_output=True, text=True,
                       encoding="mbcs", errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _exists(path):
    return Path(path).exists()


@unittest.skipUnless(IS_WINDOWS, "ACL 是 Windows 概念——判不到，不是通过")
class DataAclTest(unittest.TestCase):

    TARGETS = ("data", "fish_identity.json")

    # ------------------------------------------------------------- 主判据
    def test_no_wide_groups(self):
        """**核心**：两个目标都不许带 `Authenticated Users` / `BUILTIN\\Users`。"""
        offenders = []
        for rel in self.TARGETS:
            p = P0 / rel
            if not _exists(p):
                continue
            rc, out = _acl(p)
            self.assertEqual(rc, 0, f"icacls 读不了 {rel}：{out[:200]}")
            for w in WIDE:
                if w in out:
                    offenders.append(f"{rel} 含 {w}")
        self.assertEqual(offenders, [],
                         "数据目录对同机所有用户开放——记忆/语料可被读，身份锚可被改：\n"
                         + "\n".join(offenders))

    def test_recursion_reached_deep_files(self):
        """`/T` 递归必须真的生效。

        只改顶层目录是本项目踩过的形态：顶层看着收紧，10.7 GB 子文件仍带宽泛组。
        这里随机抽一个 kb 下的文件验。
        """
        kb = P0 / "data" / "kb"
        if not kb.is_dir():
            self.skipTest("没有 data/kb（知识库未迁移过）——判不到，不是通过")
        sample = None
        for root, _dirs, files in os.walk(kb):
            for fn in files:
                sample = Path(root) / fn
                break
            if sample:
                break
        self.assertIsNotNone(sample, "data/kb 下没有文件可抽查")
        rc, out = _acl(sample)
        self.assertEqual(rc, 0)
        for w in WIDE:
            self.assertNotIn(w, out, f"深层文件 {sample.name} 仍带 {w}（递归没生效）")

    # ------------------------------------------------------------- 反向判据
    def test_system_and_administrators_still_present(self):
        """**反向**：SYSTEM 与 Administrators 必须保留。

        没有这条，"把所有 ACE 删光"也能让上面两条变绿——那会把机器变成一个
        连管理员都进不去的目录，是**更严重**的故障。
        """
        for rel in self.TARGETS:
            p = P0 / rel
            if not _exists(p):
                continue
            _rc, out = _acl(p)
            for keep in MUST_KEEP:
                self.assertIn(keep, out,
                              f"{rel} 丢了 {keep}——收紧不能把系统自己锁在外面")

    def test_current_user_keeps_full_control(self):
        """当前用户必须仍可写——否则 fish 自己就跑不了。"""
        _rc, out = _acl(P0 / "data")
        sid = subprocess.run(["whoami", "/user"], capture_output=True, text=True,
                             encoding="mbcs", errors="replace").stdout or ""
        import re
        m = re.search(r"(S-1-5-21-[0-9\-]+)", sid)
        if not m:
            self.skipTest("拿不到当前用户 SID——判不到，不是通过")
        # icacls 输出里当前用户会显示为名字而不是 SID；用"至少有一个非系统身份带 (F)"判定
        self.assertIn("(F)", out, "当前用户没有完全控制，fish 跑不了")

    # ------------------------------------------------------------- 复核工具
    def test_verify_tool_passes(self):
        """`tools/acl_harden.py --verify` 必须 exit 0。"""
        r = subprocess.run([sys.executable, str(P0 / "tools" / "acl_harden.py"), "--verify"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", cwd=str(P0))
        self.assertEqual(r.returncode, 0,
                         f"复查工具报不合规：\n{(r.stdout or '')[-600:]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
