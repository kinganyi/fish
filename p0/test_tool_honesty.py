#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 判据（外部审查 🟠-B2/B3）：工具诚实——SKIP/MISS 不许包装成成功。

H-1 `mutation_workspace_normalize.py`：SKIP/MISS 必须让进程非 0
H-2 `mutation_injection_status.py`：同上
H-3 `tools/golden_compare.py`：缺 run2 = 判不到（noise.identical 默认 False）

判据是**静态**读源码（不复用被测实现的运行结果——那等于自己证自己）。
"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _src(rel):
    return (HERE / rel).read_text(encoding="utf-8")


class ToolHonestyTest(unittest.TestCase):
    """「判不到」必须让进程非 0——不许 fail-open。

    ## 第三轮评审：这三条原来是假的

    原实现是 `assertIn("SKIP", src)` 这类**全文子串**检查。
    评审官的变异：把 `mutation_workspace_normalize.py` 里过滤条件那个 "SKIP" 去掉
    （SKIP 重新变回 exit 0）—— **判据仍绿**，因为 "SKIP" 在文件别处还出现很多次。

    子串检查回答的是"这个字在文件里有没有"；判据要问的是
    "**决定退出码的那段逻辑里有没有它**"。在真文件里前者几乎永远成立，所以前者永远绿。

    ## 改法

    H-1/H-2：用 AST 定位"喂给 `return 2` 那个 if 的赋值"和那个 if 本身，
    把这段源码拼起来查——评审官去掉的那个 "SKIP" 正好就在这段里，
    而文件别处的 "SKIP" 不在。**这是"精确到决定点"的文本判据，不是全文搜索。**

    H-3：`golden_compare.py` 的真实行为依赖文件系统（要两遍对照产物），
    判据里静态构造不出那个场景。**如实 skipTest 并写明缺什么**——
    跳不是通过（三态分明）。
    """

    def _exit2_region(self, rel):
        """返回「决定 `return 2` 的那段逻辑」的源码：喂它的赋值 + if 条件 + if 体。"""
        import ast
        src = _src(rel)
        tree = ast.parse(src)
        bn = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        self.assertTrue(bn, f"{rel} 里没有函数——判据失去靶点")
        regions = []
        for fn in bn:
            # 找 body 里带 `return 2` 的 if
            for idx, stmt in enumerate(fn.body):
                if not isinstance(stmt, ast.If):
                    continue
                has2 = any(
                    isinstance(st, ast.Return)
                    and isinstance(st.value, ast.Constant)
                    and st.value.value == 2
                    for st in stmt.body)
                if not has2:
                    continue
                parts = []
                # 条件里引用的变量名 → 找它在同一函数里最近的赋值
                names = {n.id for n in ast.walk(stmt.test)
                         if isinstance(n, ast.Name)}
                for k in range(idx - 1, -1, -1):
                    prev = fn.body[k]
                    if isinstance(prev, ast.Assign):
                        tgt = {t.id for t in prev.targets if isinstance(t, ast.Name)}
                        if tgt & names:
                            seg = ast.get_source_segment(src, prev)
                            if seg:
                                parts.append(seg)
                            names -= tgt
                    if not names:
                        break
                parts.append(ast.get_source_segment(src, stmt.test) or "")
                for st in stmt.body:
                    parts.append(ast.get_source_segment(src, st) or "")
                regions.append("\n".join(parts))
        self.assertTrue(regions,
                        f"{rel} 里找不到「带 return 2 的 if」——判据失去靶点，不是通过")
        return "\n".join(regions)

    # H-1
    def test_H1_workspace_normalize_skips_are_failures(self):
        """SKIP 必须出现在"决定 return 2 的那段逻辑"里。

        评审官的变异就是删掉这里面的 "SKIP"；全文别处的 "SKIP" 不算数。
        """
        region = self._exit2_region("mutation_workspace_normalize.py")
        self.assertIn("SKIP", region,
                      "决定 return 2 的逻辑里没有 SKIP——SKIP 会重新变成 exit 0。\n"
                      f"  实际这段：{region[:300]}")

    # H-2
    def test_H2_injection_status_skips_are_failures(self):
        region = self._exit2_region("mutation_injection_status.py")
        self.assertIn("SKIP", region,
                      f"决定 return 2 的逻辑里没有 SKIP：{region[:300]}")

    # H-3
    def test_H3_golden_missing_run2_is_not_pass(self):
        """**判不到**：这条需要两遍对照的真实产物，静态构造不出来。

        三态分明：**跳不是通过**。原判据是
        `assertIn('"identical": old2 is not None', src)`——同样是读源码文本，
        评审官指出它改坏实现也照样绿。

        要真正判它，得在临时目录里跑两遍对照然后只喂一遍给 `golden_compare`。
        那是一个独立的集成判据（约 30 行 + 子进程），本轮不做。
        **把它显式标成 skip，比留一条假绿的判据强。**
        """
        self.skipTest(
            "H-3 需要用真实对照产物跑 golden_compare 才能判（静态构造不出）。"
            "原判据是子串检查，已证实改坏实现仍绿——判不到，不是通过。")



if __name__ == "__main__":
    unittest.main(verbosity=2)
