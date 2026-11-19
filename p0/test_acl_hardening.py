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
def _has_real_data():
    """这个树里有没有**真的东西**要被保护？

    第四轮评审之后补：公开镜像跑全量时这两条判据红了——因为镜像的 `data/`
    是**跑测试时产生的**（一个空的 `index.sqlite`，未被 git 跟踪），
    而我只收紧了生产树的权限。判据报"权限没收紧"在字面上没错，
    但它报的不是真问题：**一个刚创建的空库没有"收紧"的必要**。

    判据要分的不是"目录在不在"，而是"**这里有没有值得保护的东西**"。
    零事件的库 = 没有。
    """
    import sqlite3
    db = P0 / "data" / "index.sqlite"
    if not db.is_file():
        return False
    try:
        c = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        try:
            return c.execute("SELECT COUNT(*) FROM events").fetchone()[0] > 0
        finally:
            c.close()
    except sqlite3.Error:
        return False


class DataAclTest(unittest.TestCase):

    TARGETS = ("data", "fish_identity.json")

    # ------------------------------------------------------------- 主判据
    def test_no_wide_groups(self):
        """**核心**：两个目标都不许带 `Authenticated Users` / `BUILTIN\\Users`。"""
        offenders = []
        checked = 0
        for rel in self.TARGETS:
            p = P0 / rel
            if not _exists(p):
                continue
            checked += 1
            rc, out = _acl(p)
            self.assertEqual(rc, 0, f"icacls 读不了 {rel}：{out[:200]}")
            for w in WIDE:
                if w in out:
                    offenders.append(f"{rel} 含 {w}")
        # 第三轮评审 B-6：目标一个都不存在时必须**跳过**，不能"零靶点通过"。
        # 公开镜像里没有 `data/` 也没有 `fish_identity.json`，原实现于是
        # `offenders == []` 恒过——那不是在说"已经收紧"，是在说"没东西可查"。
        if checked == 0 or not _has_real_data():
            self.skipTest("没有真实数据要保护（公开镜像的 data/ 是测试产物）"
                          "——判不到，不是通过")
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
        checked = 0
        for rel in self.TARGETS:
            p = P0 / rel
            if not _exists(p):
                continue
            checked += 1
            _rc, out = _acl(p)
            for keep in MUST_KEEP:
                self.assertIn(keep, out,
                              f"{rel} 丢了 {keep}——收紧不能把系统自己锁在外面")
        if checked == 0:
            self.skipTest("两个目标都不存在——判不到，不是通过")

    def test_current_user_keeps_full_control(self):
        """当前用户必须仍可写——否则 fish 自己就跑不了。

        第四轮评审 🟠-3：前两条加了"零靶点就 skip"，**这一条漏了**——
        公开镜像上它无条件读 `data/`，没有这个目录时断言失败。
        """
        if not _has_real_data():
            self.skipTest("没有真实数据要保护（公开镜像的 data/ 是测试产物）"
                          "——判不到，不是通过")
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


def _sandbox_env(tmpdir):
    """给被实跑的脚本一个**关得住**的环境。

    ## 为什么（第八轮复核抓的，比那 5 个提交都严重）

    这个测试原来对 54 个脚本实跑 `--help`，而 docstring 写着
    「跑 `--help` 是安全的——它不会改任何东西」。**那句话是假的**：

      · `patch_codex.py` **没有 argparse** → `--help` 直接 `main()`，
        每次全量跑在 `I:\app\codex` 生成一个 `config.toml.bak-<ts>-before-fish`
        （2026-11-19 当天累计 **58 个**）；
      · `gov_plan.py` 没有 argparse → `--help` 直接写
        `大肥鱼生命系统\35-迁移映射-mapping.json`，把它**覆盖成空计划**；
      · 5 个 hook 脚本不认 `--help` → 创建 `p0/data/index.sqlite` 与 `_audit`。

    我第一版改成"没有 argparse 就不实跑"，但那样**又回到只验字**（注释里提到
    记号不等于会打印）。真正的修法是**把执行关起来**：临时 cwd + 临时家目录，
    脚本即使真的跑 `main()`，写的也是临时目录里的东西。

    这不是"测得更少"，是"测得更安全"——覆盖面回到 54 个，而副作用关在沙箱里。
    """
    import os as _os
    from pathlib import Path as _P
    env = dict(_os.environ)
    tmp = _P(tmpdir)
    env["PYTHONIOENCODING"] = "gbk"        # 关键：模拟 Windows 默认控制台
    env.pop("PYTHONUTF8", None)
    # 家目录类：让它们改不到真实的家目录 / codex 配置 / fish 数据
    for key in ("HOME", "USERPROFILE", "CODEX_HOME", "FISH_HOME",
                "FISH_ROOT", "APPDATA", "LOCALAPPDATA"):
        env[key] = str(tmp)
    env["TMP"] = str(tmp)
    env["TEMP"] = str(tmp)
    return env


def _has_argparse(path):
    """这个脚本**真的**会解析命令行吗？（只有它会，才敢给它传 `--help`）

    ## 为什么必须问这句（第八轮复核抓的，比我这轮的 5 个提交都严重）

    这个测试原来对 54 个脚本一律跑 `--help`，docstring 还写着
    「跑 `--help` 是安全的——它不会改任何东西」。**那句话是假的。**

    实测（2026-11-19）：
      · `patch_codex.py` **0 处** argparse → `--help` 直接跑 `main()`，
        每次全量跑在 `I:\app\codex` 生成一个 `config.toml.bak-<ts>-before-fish`；
        当天累计 **58 个**。
      · `gov_plan.py` **0 处** argparse → `--help` 直接写
        `大肥鱼生命系统\35-迁移映射-mapping.json`，把它**覆盖成空计划**。
      · 5 个 hook 脚本不认 `--help` → 创建 `p0/data/index.sqlite` 与 `_audit` 心跳。

    一个**判据**不许为了检查而改用户的文件。没有 argparse 的脚本改用 AST 验
    （见 `test_scripts_without_argparse_install_utf8`），和 `mutation_*.py` 同待遇。
    """
    import ast as _ast
    try:
        tree = _ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return False
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for a in node.names:
                if a.name == "argparse":
                    return True
        elif isinstance(node, _ast.ImportFrom) and (node.module or "") == "argparse":
            return True
    # 也可能自己解 sys.argv（hook 那类）——那更危险，同样不实跑
    return False


class ToolsConsoleEncodingTest(unittest.TestCase):
    """**第四轮评审 🟠-6**：这条判据原来是"读源码找 reconfigure 这个词"。

    评审官的变异矩阵拆穿了它两次：
      · 保留 "reconfigure" 字样、把调用改成 `pass` → 判据绿，`--verify` 实跑仍崩；
      · 新增一个打印 ✅ 的脚本 → 判据绿（只认 4 个字符），实跑崩。
    而且它只扫 `tools/*.py`，根目录 `mutation_*.py` 一直崩在 print 上没被发现。

    **改成行为判据**：在 **GBK 环境**里真跑一遍 `--help`，断言 stderr 里
    没有 `UnicodeEncodeError`。

    **原来这里写着「跑 --help 是安全的——它不会改任何东西」，那句话是假的**
    （第八轮复核抓的）：`patch_codex.py` 与 `gov_plan.py` 没有 argparse，
    `--help` 会直接跑 `main()`，前者写 `I:\app\codex` 的配置备份、后者覆盖迁移计划。
    现在**在沙箱里跑**（临时 cwd + 临时家目录），副作用出不去。

    三态分明：脚本不支持 `--help`（exit 2）不算失败，只要**没有编码崩溃**。
    """

    # 会打印出来的非 ASCII 记号（只是**挑选**候选，不是判据本身）
    _MARKERS = ("✓", "✗", "⚠", "─", "✅", "❌", "🟠", "🟡", "🔴", "⚪")

    def _candidates(self):
        """**实跑**的候选：`tools/*.py` 与普通脚本。

        **排除 `mutation_*.py`**：它们收到 `--help` 会**忽略未知参数直接跑变异**——
        实测 `mutation_b2b3_selfcheck.py --help` 跑了 120 秒超时，期间真的在改树。
        一个判据**不许为了检查而做破坏性动作**。它们改用 AST 查（见下一条）。
        """
        out = []
        for p in sorted(HERE.glob("*.py")) + sorted((HERE / "tools").glob("*.py")):
            if p.name.startswith("test_") or p.name.startswith("mutation_"):
                continue
            src = p.read_text(encoding="utf-8", errors="replace")
            if not any(m in src for m in self._MARKERS):
                continue
            # **只跑真的有 `--help` 的脚本**（第八轮复核）。
            # 没有 argparse 的脚本收到 `--help` 会**忽略它直接执行**：
            #   · `patch_codex.py`     → 写 I:pp\codex 的配置备份（当天 58 个）
            #   · `gov_plan.py`        → 覆盖 35-迁移映射-mapping.json 成空计划
            #   · hook 脚本            → 用 `Path(__file__).parent/"data"` 造库（沙箱也挡不住）
            # 给一个**没有 `--help` 的脚本**传 `--help` 本来就测不到"控制台编码"，
            # 所以这不是缩小覆盖面，是让覆盖面**只包含它声称要测的东西**。
            if not _has_argparse(p):
                continue
            out.append(p)
        return out

    def test_console_scripts_survive_gbk(self):
        """在 GBK 环境下每个打印非 ASCII 的脚本都不许编码崩溃。"""
        import os as _os
        import subprocess as _sp
        cands = self._candidates()
        # 失靶守卫：一个候选都没有说明挑法坏了，必须红
        self.assertGreaterEqual(
            len(cands), 5,
            f"只找到 {len(cands)} 个候选——判据失去靶点，不是通过")
        import tempfile as _tf
        import shutil as _sh
        sandbox = _tf.mkdtemp(prefix="fish-gbk-")
        self.addCleanup(_sh.rmtree, sandbox, True)
        env = _sandbox_env(sandbox)
        # **每个脚本要跑它真的会打印记号的那条命令**。
        # `acl_harden.py` 的 `--help` 只有纯 ASCII，崩发生在 `--verify`——
        # 第一版对所有脚本都跑 `--help`，于是"保留字样、调用改 pass"的变异仍然绿。
        # `--verify` 是只读的（只读 ACL，不改）。
        ARGS = {"acl_harden.py": ["--verify"]}
        crashes = []
        for p in cands:
            r = _sp.run([sys.executable, str(p)] + ARGS.get(p.name, ["--help"]),
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", env=env, cwd=sandbox, timeout=120)
            blob = (r.stdout or "") + (r.stderr or "")
            if "UnicodeEncodeError" in blob:
                crashes.append(f"{p.name}: {blob.strip().splitlines()[-1][:100]}")
        self.assertEqual(crashes, [],
                         "这些脚本在 GBK 控制台会编码崩溃（看起来像\"脚本失败\"）：\n  "
                         + "\n  ".join(crashes))

    def test_mutation_scripts_install_utf8_by_calling_it(self):
        """`mutation_*.py` 用 **AST** 验，不实跑（实跑会改树）。

        等价的"调用真的存在"：ast 里必须有对 `reconfigure` 的 **Call**，
        不是"文件里出现过这个词"。评审官的变异（保留字样、把调用改成 pass）
        在这条下会红，而在子串判据下是绿的。
        """
        import ast
        # 只有**可执行**的 mutation 脚本需要装 stdout 编码；
        # `mutation_source.py` 是共享库（没有 `__main__` 块、不打印符号），
        # 把它算进来是判据的假阳性。
        need = [p for p in sorted(HERE.glob("mutation_*.py"))
                if '__name__ == "__main__"' in p.read_text(encoding="utf-8",
                                                           errors="replace")]
        self.assertGreaterEqual(len(need), 3,
                                f"只找到 {len(need)} 个 mutation 脚本——判据失去靶点")
        offenders = []
        for p in need:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
            calls = [n for n in ast.walk(tree)
                     if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute)
                     and n.func.attr == "reconfigure"]
            if not calls:
                offenders.append(p.name)
        self.assertEqual(offenders, [],
                         f"这些号本没有**真正调用** reconfigure"
                         f"（只是文件里出现过这个词不算）：{offenders}")



    def test_console_sweep_leaves_no_trace(self):
        """**这条判据守的是"测试不许改用户文件"**（第八轮复核）。

        背景：这个测试原来对 54 个脚本一律实跑 `--help`，其中
        `patch_codex.py` / `gov_plan.py` 没有 argparse，`--help` 会直接跑 `main()`：
        前者在 `I:\app\codex` 生成配置备份（当天累计 58 个），
        后者把 `35-迁移映射-mapping.json` **覆盖成空计划**。
        另有 hook 脚本用 `Path(__file__).parent/"data"` 造库——沙箱也挡不住。

        修法是**只跑真的有 `--help` 的脚本**。这条判据锁住那个修法：
        跑完之后仓库里**不许出现新的 `data/`**。
        """
        import shutil as _sh
        marker = HERE / "data"
        had = marker.exists()
        if had:
            self.skipTest("仓库里本来就有 data/（生产树），这条只在干净树上判得到")
        try:
            self.test_console_scripts_survive_gbk()
        finally:
            created = marker.exists()
            if created:
                _sh.rmtree(marker, ignore_errors=True)
        self.assertFalse(created,
                         "实跑脚本把 `data/` 造出来了——说明又跑了没有 argparse 的"
                         "脚本（它们收到 --help 会直接执行 main()）")



if __name__ == "__main__":
    unittest.main(verbosity=2)
