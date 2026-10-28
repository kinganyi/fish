#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拆包契约判据（`fish.py` 拆包第一轮 · T04 · 设计 §1.6）。

这份文件只回答一个问题：**"拆了但调用面没变"这句话有没有可验证的证据**。
它不看实现细节，只锁四条对外承诺：

- **J1 / J1b / J1c / J1d 调用面零变化**
  `fish.<name>` 与 `fish_kb.<name>` 必须是**同一个对象**（不是同名副本），
  清单与 AST 双向对齐（手写清单漏一个 = 运行时静默少一个），
  `_kb_commands_ready()` 仍为 True（它靠 `globals()` 判符号，最容易在搬迁后静默降级），
  共享原语同样是同一对象。

- **J2 依赖单向**：`fish → fish_kb → fish_common`。
  静态查 AST（两份新模块都不许 import fish），运行时在**干净子进程**里查
  `sys.modules`（同时验证两个方向的导入顺序都能跑通）。

- **J3 没有"漏 import"的定时炸弹**：用 `symtable` 找出 `fish_kb` 里所有
  **全局引用**，逐个要求在模块顶层有定义或 import。这类错误只在被调用的那一刻炸，
  单靠"跑一遍测试"抓不全。

- **J4 体量与例外**：行数落在窗口内、`__all__` 规模合理、
  `_kb_push_candidates` 必须在 `fish.py` 且**不在** `fish_kb`（它属于推送管线）。

- **J5 新模块不依赖模块自身身份**：不许出现 `__file__` / `__name__`
  ——搬迁后的函数若靠它们定位路径，指到的是**新文件**而不是原来的 `fish.py`。

纪律：本文件**不建库、不写文件**（连 tempfile 都不需要）；只读源码与模块对象。
"""
import ast
import builtins
import importlib.util
import subprocess
import symtable
import sys
import unittest
from pathlib import Path

P0 = Path(__file__).resolve().parent
FISH = P0 / "fish.py"
FISH_KB = P0 / "fish_kb.py"
FISH_COMMON = P0 / "fish_common.py"
FISH_PUSH = P0 / "fish_push.py"

spec = importlib.util.spec_from_file_location("fish", str(FISH))
fish = importlib.util.module_from_spec(spec)
sys.modules.setdefault("fish", fish)
spec.loader.exec_module(fish)

if str(P0) not in sys.path:
    sys.path.insert(0, str(P0))
import fish_common  # noqa: E402
import fish_kb  # noqa: E402

# 共享原语（设计 §1.2）：五个名字，两边必须是同一对象。
COMMON_NAMES = ("ulid", "now_utc", "iso_local", "count_tokens", "_exc_text")

# J4 行数窗口。设计 §1.6 写的是 15,600–15,900（由"fish_kb ≈ 2,629 行"的估算反推），
# 实测整段搬运（注释与 docstring 照搬）把 fish.py 从 18,356 行降到 15,380 行；
# 设计的估算与它自己给的 15,779 也互相矛盾（2,629+70 ≠ 18,357−15,779）。
# 窗口按实测收紧：判据目的不变（少搬/多搬一整个段都要红），数字以实测为准。
# 【偏差已上报，见交付报告"与设计的偏差"第 1 条】
#
# 2026-10-27（T02 拆包 fish_push）：fish.py 15,464 → 13,980（push 闭包 33 名/1,048 行搬走，
# 30 名基础层下沉 fish_common）。窗口随拆包**迁移**：下界按新基线留余量，**上界不变**；
# 防"多搬/漏搬"的硬判据由 J1/J1b/J7（名字集合 + 对象同一）与 J8（函数体 AST 等价）承担，
# 行数窗口只守"文件规模"。同时给新模块各自加窗口，防未来又长回去。
# 评审 🟡-7（2026-10-27）：窗口从 300 行放宽到 2,000 行被推翻——按实测收窄到 ±200 行。
# 2026-10-28（T03b 合并）：fish_push 因 rerank 接线（+60 行）到 1,309 → 窗口上抬到 1,400
# （功能增长导致的判据迁移，不是弱化：下限仍 1,050，且 J7/J8 守语义）。
FISH_LINES_MIN = 13800
# 2026-10-29（P1 修复轮，外部审查 §8 P1）：🔴11 公民状态校验 / 🔴10 链式+唯一约束 /
# 🔴6 body_relpath 路径穿越拦截 / 🔴9 append_event 事务参数 都落在 fish.py 上，
# 实测 14,302 行 → 上界 14,300 → **14,500**。这是"功能增长"的判据迁移（不是放宽）：
# 下界不动、新模块窗口不动，且这些改动的行为由各自判据（test_identity_lifecycle /
# test_event_tx / test_path_traversal）逐条钉住。
FISH_LINES_MAX = 14500
EXPECTED_LINE_ACTUAL = 14032
FISH_PUSH_LINES_MIN = 1050
# 2026-10-29（P0-1/P0-2 推送去重）：空 session 全局去重 + 结案（accepted/ignored）
# 永久不推 → 实测 1,405 行，上限 1,400 → **1,450**。下界不动；行为由
# test_push_dedupe（3）与 test_push_settled（4）逐条钉住。
FISH_PUSH_LINES_MAX = 1450
FISH_COMMON_LINES_MIN = 550
FISH_COMMON_LINES_MAX = 750


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _module_level_names(tree: ast.Module) -> set[str]:
    """模块顶层绑定的名字（def/class/赋值/import）。"""
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(a.asname or a.name for a in node.names)
    return names


def _defined_names(tree: ast.Module) -> set[str]:
    """模块顶层**定义**的名字（def/class/赋值；**不含 import 别名**）。

    为什么需要它：J7/J4 要判"某函数已搬走（fish.py 顶层不再定义）"，而
    `from fish_push import (...)` 的 re-export 会让 `_module_level_names` 里出现同名——
    那是导入面，不是定义面。混用会把合法的 re-export 判成"没搬干净"。
    """
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _imported_modules(tree: ast.Module) -> set[str]:
    """模块顶层 import 了哪些包名（`from a.b import c` 记 `a.b`）。"""
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module)
    return mods


def _imported_names(tree: ast.Module) -> set[str]:
    """模块顶层 import 进来的**名字**（`__all__` = 顶层绑定 − 这些 − 记账名）。"""
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(a.asname or a.name for a in node.names)
    return names


class SplitSurfaceTest(unittest.TestCase):
    """J1 家族：调用面零变化（名字、对象身份、清单对齐）。"""

    def test_j1_every_kb_name_is_the_same_object_in_fish(self):
        """J1：`fish_kb.__all__` 里每个名字在 `fish` 侧都在，且**是同一个对象**。

        为什么必须是同一对象：外部 12 个测试文件 335 处 + `mcp_server` 14 处引用的
        都是 `fish.xxx`。若再导出时复制了一份（比如又写一遍函数体，或包一层 wrapper），
        改动 `fish_kb` 的实现不会影响 `fish`，两套行为会慢慢漂开——而判据仍是绿的。
        """
        self.assertGreater(len(fish_kb.__all__), 100, "导出面太小，明显漏名")
        missing, not_same = [], []
        for name in fish_kb.__all__:
            if not hasattr(fish, name):
                missing.append(name)
                continue
            if getattr(fish, name) is not getattr(fish_kb, name):
                not_same.append(name)
        self.assertEqual(missing, [], "fish 侧缺名（外部引用会在运行时消失）")
        self.assertEqual(not_same, [], "fish 侧的名字与 fish_kb 不是同一对象")

    def test_j1b_reexport_list_matches_ast(self):
        """J1b：`fish.py` 的 `from fish_kb import (...)` 与 `fish_kb` 的导出面**双向**相等。

        单向查漏不够：多写一个不存在的名字会 ImportError（响），
        少写一个才是静默的——所以两侧集合都要比，且与 `__all__` 三方对齐。
        """
        tree = ast.parse(_read(FISH))
        imported = set()
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "fish_kb":
                imported.update(a.asname or a.name for a in node.names)
        self.assertTrue(imported, "fish.py 里没有 from fish_kb import（拆包没做完？）")
        self.assertEqual(sorted(imported), sorted(fish_kb.__all__),
                         "fish.py 的再导出清单与 fish_kb.__all__ 不一致")
        tree_kb = ast.parse(_read(FISH_KB))
        defined = (_module_level_names(tree_kb) - _imported_names(tree_kb)
                   - {"__all__", "_IMPORTED"})
        self.assertEqual(sorted(defined), sorted(fish_kb.__all__),
                         "fish_kb.__all__ 与它自己的顶层定义集合不一致（_IMPORTED 记漏了）")

    def test_j1c_kb_commands_ready_is_true(self):
        """J1c：`_kb_commands_ready()` 必须为 True。

        它用 `globals()` 判 `kb_tree` / `kb_write` 是否存在。搬迁后 `globals()`
        指向 `fish_kb` 的命名空间——如果导出面漏了这两个名字之一，CAPABILITIES 页
        会静默退回"尚未接入"，而其它判据全绿。这条就是那个缺口的探针。
        """
        self.assertIs(fish._kb_commands_ready(), True)
        self.assertIs(fish_kb._kb_commands_ready(), True)
        self.assertIs(fish._kb_commands_ready, fish_kb._kb_commands_ready,
                      "再导出必须是同一对象（否则 fish 侧调用的是另一份实现）")

    def test_j1d_shared_primitives_are_the_same_object(self):
        """J1d：5 个共享原语在 `fish` 与 `fish_common` 之间是同一个对象。

        这条守的是"只有一份实现"：时间格式、ULID 形态、token 计数一旦分家，
        `kb` 与记忆/看板写出来的时间戳会慢慢不一致（而单测各自都绿）。
        """
        for name in COMMON_NAMES:
            self.assertTrue(hasattr(fish_common, name), f"fish_common 缺 {name}")
            self.assertTrue(hasattr(fish, name), f"fish 侧缺 {name}")
            self.assertIs(getattr(fish, name), getattr(fish_common, name),
                          f"{name} 在 fish 与 fish_common 里不是同一个对象")


class SplitDependencyTest(unittest.TestCase):
    """J2 / J3 / J5：依赖方向、漏 import、模块身份。"""

    def test_j2_no_back_import_static(self):
        """J2（静态）：两份新模块都不许 import `fish`；`fish_common` 也不许 import `fish_kb`。

        循环导入的症状不是报错而是"某一侧拿到半初始化的模块"——属性缺失会以
        `AttributeError` 在随机位置出现。所以方向用 AST 钉死，不靠"跑起来没炸"。
        """
        for path, forbidden in ((FISH_KB, {"fish"}),
                                (FISH_COMMON, {"fish", "fish_kb"}),
                                # 评审 🟡-8：T02 新增的 fish_push 也要查（漏 import 炸弹）
                                (FISH_PUSH, {"fish"})):
            mods = _imported_modules(ast.parse(_read(path)))
            bad = sorted(m for m in mods if m in forbidden)
            self.assertEqual(bad, [], f"{path.name} 反向依赖了 {bad}")

    def test_j2_no_back_import_runtime_and_both_orders(self):
        """J2（运行时）：干净子进程里 `import fish_kb` 不得把 `fish` 带进来；
        且两个导入顺序（fish→fish_kb / fish_kb→fish）都要能跑通。

        为什么要子进程：同进程里别的测试已经把 `fish` 放进 `sys.modules`，
        "有没有被带进来"就测不出来了——那是测试环境的假象，不是代码性质。
        """
        probe = (
            "import sys\n"
            "import fish_kb\n"
            "assert 'fish' not in sys.modules, 'import fish_kb 把 fish 带进来了'\n"
            "assert 'fish_common' in sys.modules, 'fish_kb 应当依赖 fish_common'\n"
            "print('OK-KB-FIRST')\n"
        )
        proc = subprocess.run([sys.executable, "-X", "utf8", "-c", probe],
                              cwd=str(P0), capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 0,
                         f"反向依赖/导入顺序失败：{proc.stdout}{proc.stderr}")
        self.assertIn("OK-KB-FIRST", proc.stdout)

        probe2 = (
            "import fish\n"
            "import fish_kb\n"
            "assert fish.kb_tree is fish_kb.kb_tree\n"
            "print('OK-FISH-FIRST')\n"
        )
        proc2 = subprocess.run([sys.executable, "-X", "utf8", "-c", probe2],
                               cwd=str(P0), capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
        self.assertEqual(proc2.returncode, 0,
                         f"先 import fish 再 import fish_kb 失败："
                         f"{proc2.stdout}{proc2.stderr}")

    def test_j3_no_unresolved_globals(self):
        """J3：`fish_kb` / `fish_common` / `fish_push` 里**所有全局引用**都必须在顶层有定义或 import。

        为什么用 `symtable` 而不是"跑一遍"：漏 import 只在**那条分支被走到**时抛
        `NameError`。`kb backup prune` 里少个 `_exc_text`、`doctor` 少个 `shutil`，
        常规测试路径可能一辈子碰不到，而线上第一次碰到就是崩溃。
        `symtable` 能精确区分"全局引用"与"局部变量"，不会把循环变量算进来。

        评审 🟡-8（2026-10-27）：T02 的 `fish_push.py` 恰好踩过"漏 import"的坑
        （`fish_activity`/`fish_naming`/`fish_surface`/`fish_kb`），探针必须覆盖它。
        """

        def missing_of(path):
            table = symtable.symtable(_read(path), path.name, "exec")
            module_names = set(table.get_identifiers())
            missing = {}

            def walk(tbl, scope):
                for sym in tbl.get_symbols():
                    name = sym.get_name()
                    if sym.is_global() and name not in module_names \
                            and not hasattr(builtins, name):
                        missing.setdefault(name, set()).add(scope)
                for child in tbl.get_children():
                    walk(child, child.get_name())

            walk(table, "<module>")
            return {k: sorted(v) for k, v in sorted(missing.items())}

        for path in (FISH_KB, FISH_COMMON, FISH_PUSH):
            self.assertEqual(missing_of(path), {},
                             f"{path.name} 里有全局名既没定义也没 import（运行时才会炸）")

    def test_j5_new_modules_do_not_use_module_identity(self):
        """J5：新模块 0 处 `__file__` / `__name__`（模块级 dunder 的**取值**）。

        为什么禁：`DEFAULT_ROOT = Path(__file__).resolve().parent / "data"` 这种写法
        搬进 `fish_kb` 后指向的是 `p0/`（恰好一样），搬进别处就指向别处——
        它让"这个模块在哪"变成隐式依赖。`type(x).__name__` 不在禁列（那是类型名）。
        """
        for path in (FISH_KB, FISH_COMMON, FISH_PUSH):
            tree = ast.parse(_read(path))
            hits = [node.lineno for node in ast.walk(tree)
                    if isinstance(node, ast.Name) and node.id in ("__file__", "__name__")]
            self.assertEqual(hits, [],
                             f"{path.name} 仍在用模块自身身份（__file__/__name__）：行 {hits}")


class SplitSizeTest(unittest.TestCase):
    """J4：体量窗口与例外清单。"""

    def test_j4_line_count_within_window(self):
        """J4：`fish.py` 行数落在窗口内；`__all__` 规模合理。

        窗口的作用是"整段搬运漏了一段/多搬了一段"要红。行数口径与设计一致：
        `len(fish.py 文本.split("\\n"))`（末尾换行也算一行）。
        """
        lines = len(_read(FISH).split("\n"))
        self.assertGreaterEqual(lines, FISH_LINES_MIN,
                                f"fish.py 只有 {lines} 行：可能多搬走了东西")
        self.assertLessEqual(lines, FISH_LINES_MAX,
                             f"fish.py 有 {lines} 行：可能漏搬了东西")
        push_lines = len(_read(P0 / "fish_push.py").split("\n"))
        self.assertGreaterEqual(push_lines, FISH_PUSH_LINES_MIN,
                                f"fish_push.py 只有 {push_lines} 行：搬漏了")
        self.assertLessEqual(push_lines, FISH_PUSH_LINES_MAX,
                             f"fish_push.py 有 {push_lines} 行：长回去了")
        common_lines = len(_read(FISH_COMMON).split("\n"))
        self.assertGreaterEqual(common_lines, FISH_COMMON_LINES_MIN,
                                f"fish_common.py 只有 {common_lines} 行：下沉漏了")
        self.assertLessEqual(common_lines, FISH_COMMON_LINES_MAX,
                             f"fish_common.py 有 {common_lines} 行：长回去了")
        self.assertGreater(len(fish_kb.__all__), 100,
                           "fish_kb 的导出面太小，明显漏名")
        self.assertGreaterEqual(len(_read(FISH_KB).split("\n")), 2500,
                                "fish_kb 太小：看起来没搬全")

    def test_j4_push_candidates_lives_in_fish_push(self):
        """J4：`_kb_push_candidates` 现在**属于 fish_push**（T02 拆包），fish 侧靠 re-export 可用。

        历史（第一轮拆包）：它当时刻意留在 fish.py（设计裁决 5：第二轮随 `fish_push` 改名
        再一起动）。T02 就是那一轮——所以判据随之迁移：
        · `fish_push._kb_push_candidates` 必须存在；
        · `fish_kb` 里必须没有；
        · `fish.py` 的**顶层定义**里不能再有它（只在 fish.py 顶部 re-export）。
        """
        import fish_push
        self.assertTrue(hasattr(fish_push, "_kb_push_candidates"),
                        "_kb_push_candidates 不在 fish_push 里（推送管线会断）")
        self.assertFalse(hasattr(fish_kb, "_kb_push_candidates"),
                         "_kb_push_candidates 被搬进 fish_kb 了（不该）")
        top = _defined_names(ast.parse(_read(FISH)))
        self.assertNotIn("_kb_push_candidates", top,
                         "fish.py 顶层仍定义 _kb_push_candidates（应只在 fish_push）")


class PushModuleContractTest(unittest.TestCase):
    """J6/J7/J8：`fish_push` 的拆分契约（T02，2026-10-27）。"""

    def test_j6_push_module_does_not_import_fish(self):
        """J6：`fish_push` 不许 import fish（干净子进程验证，防循环依赖）。"""
        code = (
            "import sys\n"
            f"sys.path.insert(0, r'{P0}')\n"
            "import fish_push\n"
            "assert 'fish' not in sys.modules, 'fish_push 把 fish 带进来了'\n"
            "print('ok')\n"
        )
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, cwd=str(P0), timeout=120)
        self.assertEqual(proc.returncode, 0, f"J6 失败：{proc.stderr[-800:]}")

    def test_j7_reexport_surface(self):
        """J7：`fish_push.__all__` 每个名字在 fish 侧都在且是同一对象；fish.py 顶层不再定义。"""
        import fish_push
        self.assertGreater(len(fish_push.__all__), 25, "fish_push.__all__ 太小（漏名）")
        for name in fish_push.__all__:
            self.assertTrue(hasattr(fish, name), f"fish 缺 re-export：{name}")
            self.assertIs(getattr(fish, name), getattr(fish_push, name),
                          f"{name} 不是同一个对象（re-export 断了）")
        top = _defined_names(ast.parse(_read(FISH)))
        for name in ("build_push_candidates", "select_push_candidates",
                     "build_prompt_candidates", "build_push_block",
                     "mark_push_accepted", "mark_push_accepted_prefix",
                     "_kb_push_candidates", "_conv_push_candidates"):
            self.assertNotIn(name, top, f"{name} 仍在 fish.py 顶层定义（应只在 fish_push）")

    def _git_blob(self, rev, path):
        """取某次提交时的文件内容（None = 取不到）。"""
        try:
            out = subprocess.run(
                ["git", "-C", str(P0.parent), "show", f"{rev}:{path}"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=120)
            return out.stdout if out.returncode == 0 and out.stdout.strip() else None
        except Exception:      # noqa: BLE001
            return None

    def test_j8_function_bodies_semantically_identical(self):
        """J8：T02 拆包前后**公共函数/类定义** AST 等价（评审 🟠-4/5 的收口）。

        **基准是 T02 提交那一刻**（`da8d24f` 及其父），不是"现在"——否则 T03/T04/T05/B
        的后续改动会被误算成"搬运违例"。
        优先本地备份；没有则从 git blob 取（发布树无 `历史版本/`，评审 🟠-4）。
        比对**全交集**（实测 386 个定义），而不是只比 12 个搬走的函数（评审 🟠-5）。

        **覆盖边界（外部审查 🔴12 的收口，2026-10-28 晚明确）**：
          覆盖 —— 顶层 `FunctionDef/AsyncFunctionDef/ClassDef` 的 **body**（含函数体、
                  嵌套函数与全部语句）；这是"搬运没改行为"的主证据。
          不覆盖 —— 模块级 `Import/ImportFrom`、`Assign/AnnAssign`、`If/Try`（条件定义）。
                  拆包**必然**改 import（`fish.X` → 直接 import）与常量来源，逐字节比会把
                  "正确的拆包"判成违例；这些由 J1b（re-export 清单双向相等）、J7（同一
                  对象）、J2/J6（依赖方向）与 1,587 条行为判据兜底。
          一句话：**J8 保证"函数与类的定义等价"，不保证"整个模块字节等价"**——引用它时
          必须带上这句限定（审查官指出：一个连审查者都会误读的判据，其保证力不应被夸大）。
        """
        pre = P0 / "历史版本" / "fish.py.20261027-t02-pre.bak"
        if pre.exists():
            old_src, origin = pre.read_text(encoding="utf-8", errors="replace"), "backup"
        else:
            old_src, origin = self._git_blob("da8d24f^", "p0/fish.py"), "git:da8d24f^"
        if not old_src:
            self.skipTest("T02 前版本不可达（备份与 git blob 都取不到）——判不到，不是通过")
        new_srcs = [self._git_blob("da8d24f", p)
                    for p in ("p0/fish.py", "p0/fish_push.py", "p0/fish_common.py")]
        if not all(new_srcs):
            self.skipTest("T02 提交快照取不到（git 不可用）——判不到，不是通过")
        old_defs = {}
        for node in ast.parse(old_src).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                old_defs[node.name] = node
        new_defs = {}
        for src in new_srcs:
            for node in ast.parse(src).body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    new_defs[node.name] = node
        # 已登记差异（每条必须有原因；T02 报告 §2 的六类副作用，逐条对应）
        known_diff = {
            # 跨模块引用改写：`fish.X` → 直接 import 的 X（拆包必需）
            "_kb_push_candidates": "跨模块引用改写（kb_vault_list/_kb_search_multi 改为直接 import）",
            "_conv_push_candidates": "跨模块引用改写（fish_activity/fish_corpus 改为直接 import）",
            "build_prompt_candidates": "跨模块引用改写（内部 helper 已搬出）",
            "build_push_candidates": "跨模块引用改写（内部 helper 已搬出）",
            "select_push_candidates": "跨模块引用改写（内部 helper 已搬出）",
            "build_push_block": "跨模块引用改写（内部 helper 已搬出）",
            "clean_prompt": "函数内 import fish_activity（跨模块调用，不 import fish）",
        }
        common = sorted(set(old_defs) & set(new_defs))
        self.assertGreaterEqual(len(common), 380,
                                f"只找到 {len(common)} 个公共定义（实测应 386）：{origin}")
        changed = []
        for name in common:
            if name in known_diff:
                continue
            a = ast.dump(ast.Module(body=old_defs[name].body, type_ignores=[]),
                         annotate_fields=False)
            b = ast.dump(ast.Module(body=new_defs[name].body, type_ignores=[]),
                         annotate_fields=False)
            if a != b:
                changed.append(name)
        self.assertEqual(changed, [],
                         f"这些定义在 T02 拆分中变了（且未登记原因）：{changed[:10]}（来源 {origin}）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
