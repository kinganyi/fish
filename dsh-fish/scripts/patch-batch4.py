# -*- coding: utf-8 -*-
"""批次 4 收尾：
  1) 删 3 条依赖已删 probe 脚本的判据（它们已失效，不能假装通过）
  2) 门禁负向测试：从"必须抛错"改成"必须产生指纹告警"（3a 改了语义）
  3) lib/*.map 移出入口（生产不需要，省 618 KB）
"""
import re
from pathlib import Path

P0 = Path(r'I:\app\fish-public\dsh-fish')
CS = P0 / "scripts" / "check-spec.mjs"
text = CS.read_text(encoding="utf-8")
orig = text

DROP = [
    "spec.tools_match_production",
    "spec.cli_map_covers_production",
    "spec.corpus_schema_matches_production",
]


def drop_record(src: str, name: str):
    """删掉 record('<name>', ...) 调用（括号平衡），返回 (新文本, 是否删掉)。"""
    idx = src.find(f"record('{name}'")
    if idx < 0:
        idx = src.find(f'record("{name}"')
    if idx < 0:
        return src, False
    # 从 record( 开始做括号平衡
    i = src.find("(", idx)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "(":
            depth += 1
        elif src[j] == ")":
            depth -= 1
            if depth == 0:
                end = j + 1
                # 连带吃掉行尾换行
                while end < len(src) and src[end] in "\r\n":
                    end += 1
                return src[:idx] + src[end:], True
    return src, False


for name in DROP:
    text, ok = drop_record(text, name)
    print(("  已删 " if ok else "  未命中 ") + name)

# 门禁负向测试：改断言
OLD = """    try {
      libApi.runStartupGate({ specDir: tmpDir, dbPath: paths.dbPath, hostVersion: null, strictHostVersion: false })
      return { threw: false, detail: '门禁没有抛错——指纹校验可能已被移除' }
    } catch (error) {
      return { threw: true, detail: `${error.code ?? error.name}: ${String(error.message).slice(0, 90)}` }
    }"""
NEW = """    // 2026-11-09（批次 3a）：门禁从 fail closed 改为 warn+降级，
    // 所以负向用例的判据从"必须抛错"改成"必须把指纹差异报出来"——
    // 目的没变：证明「指纹校验还在」，只是现在以 warning 而不是异常的形式存在。
    try {
      const result = libApi.runStartupGate({ specDir: tmpDir, dbPath: paths.dbPath, hostVersion: null, strictHostVersion: false })
      const warnings = Array.isArray(result.warnings) ? result.warnings : []
      const caught = warnings.some((w) => String(w).includes('指纹'))
      return {
        threw: caught,
        detail: caught
          ? `负向指纹被识别为告警（共 ${warnings.length} 条 warning）`
          : '门禁既没抛错也没告警——指纹校验可能已被移除',
      }
    } catch (error) {
      // 仍然抛错也算通过（严格模式下允许）
      return { threw: true, detail: `（严格模式仍抛错）${error.code ?? error.name}: ${String(error.message).slice(0, 80)}` }
    }"""

if OLD in text:
    text = text.replace(OLD, NEW, 1)
    print("  门禁负向判据已改（抛错 → 告警）")
else:
    print("  ⚠ 门禁负向判据锚点未命中")

if text != orig:
    CS.write_text(text, encoding="utf-8", newline="")
    print("check-spec.mjs 已写盘")

# .gitignore 加 lib/*.map
gi = P0 / ".gitignore"
g = gi.read_text(encoding="utf-8")
if "lib/*.map" not in g:
    g = g.rstrip("\n") + "\n\n# 2026-11-09：sourcemap 是构建产物（618 KB），生产不需要（package.json 的 files 也没收）\nlib/*.map\n"
    gi.write_text(g, encoding="utf-8", newline="")
    print(".gitignore 已加 lib/*.map")
