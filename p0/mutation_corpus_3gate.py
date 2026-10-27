#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异自证 · 语料层（2026-10-27 · 第一批三关修复 14 个 + 第二批 10 个 + 终批 8 个
+ 终验黄项 4 个 + 采集完整性 4 个）

为什么要有它：每条修复都补了"能失败"的判据，但**判据本身**也可能只是摆设。
本脚本对每条修复做一个"把修法改回旧行为"的变异体，跑对应判据，要求它**红**。
全 CAUGHT 才算这批判据有判别力；任一 MISSED 就说明那条判据抓不住自己声称的东西。

用法（在 p0 下）：

    python mutation_corpus_3gate.py            # 全跑（约 2~3 分钟）
    python mutation_corpus_3gate.py R2 M2      # 只跑指定变异体

纪律：所有变异都发生在 **tempfile 副本**里，工作树一字不动。
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

# (编号, 说明, [(文件, 旧文本, 新文本)], 期望红的判据)
MUTATIONS = (
    ("R1", "目录名≠头部 id：只允许重启一次 → 改回无条件重入（栈溢出）",
     [("fish_corpus_dsh.py",
       "session_id_override=true_sid, allow_restart=False)",
       "session_id_override=true_sid, allow_restart=True)")],
     "test_corpus_ingest_dsh.PathIdMismatchTest"),
    ("R2", "坏帧前的好帧先落盘、游标停最后成功边界 → 改回 flush()（游标退回旧值）",
     [("fish_corpus_dsh.py",
       "                flush(force=True, upto=pending_offset)",
       "                flush()   # 变异：游标不跟着这批走")],
     "test_corpus_ingest_dsh.TruncatedFrameTest"),
    ("R3", "0 段文件收尾 flush(force=True) → 改回不带 force（不写游标行）",
     [("fish_corpus_dsh.py",
       "        flush(force=True, upto=pending_offset)",
       "        flush(upto=pending_offset)")],
     "test_corpus_ingest_dsh.ZeroSegmentSourceTest"),
    ("S1", "脱敏改成「键+值整体消费」 → 改回只掩标签（留值）",
     [("fish_corpus_redact.py",
       '        r"(?i)[a-z0-9_]*(?:token|secret|api[_-]?key)[\\"\']?\\s*[=:：]\\s*[\\"\']?\\S+")),',
       '        r"(?i)[a-z0-9_]*(?:token|secret|api[_-]?key)\\s*[=:]")),')],
     "test_corpus_governance.SecretFormsTest"),
    ("S2", "doctor 扫全部可回显文本列 → 把 title 列从扫描面摘掉",
     [("fish_corpus_gov.py",
       '    ("corpus_session", "title", "title"),\n', "")],
     "test_corpus_governance.DoctorTextColumnTest"),
    ("S3", "标题写入前脱敏 → 改回原样写",
     [("fish_corpus_dsh.py",
       '                session["title"] = _safe_title(session.get("title"))',
       '                session["title"] = session.get("title")'),
      ("fish_corpus_dsh.py",
       "                    safe = _safe_title(str(title))       # S3：标题也是内容",
       "                    safe = str(title)"),
      ("fish_corpus_dsh.py",
       '                    "data": {"title": "凭证 %s 的会话" % secret}},',  # 不需要改
       '                    "data": {"title": "凭证 %s 的会话" % secret}},')],
     "test_corpus_ingest_dsh.TitleRedactionTest"),
    ("M1", "日常 ingest 也按台账过滤 → 摘掉台账合并",
     [("fish_corpus.py",
       "        skip |= tomb_sessions",
       "        pass  # 变异：日采不看台账")],
     "test_corpus_governance.ForgetErasureTest"),
    ("M2", "MCP project='' 是有效过滤 → 改回「空串=不过滤」",
     [("mcp_server.py",
       '                if key != "project" and value == "":',
       '                if value == "":')],
     "test_corpus_search.McpProjectFilterTest"),
    ("M3", "读路径只读连接 → 改回写连接（抢写锁 + 跑 DDL）",
     [("fish_corpus.py",
       "    return store.open_db(root, readonly=True), True",
       "    return store.open_db(root), True")],
     "test_corpus_push.ConvFailureTest"),
    ("M4", "停滞哨兵连续 3 轮报警 → 把 alarm 恒置 None",
     [("fish_corpus.py",
       '    alarm = "stalled" if streak >= STALL_STREAK_ALARM else None',
       "    alarm = None  # 变异：哨兵不叫")],
     "test_corpus_ingest_dsh.StallAlarmTest"),
    ("M5", "forget 后物理擦除（VACUUM+checkpoint） → 摘掉",
     [("fish_corpus_gov.py",
       '        result["erase"] = store.secure_erase(conn)',
       '        result["erase"] = None  # 变异：只删行不擦页'),
      ("fish_corpus_store.py",
       '    "PRAGMA secure_delete=ON",\n', "")],
     "test_corpus_governance.ForgetErasureTest"),
    ("M6", "单帧解压输出上限 → 把检查改成恒假",
     [("fish_corpus_dsh.py",
       "                if cap and produced > cap:",
       "                if False:  # 变异：上限不生效")],
     "test_corpus_ingest_dsh.OversizeGuardTest"),
    ("M7", "WB 续传用游标行号、不重读前缀 → 改回总是重数",
     [("fish_corpus_wb.py",
       "            base_lines = int(last_line)",
       "            base_lines = None  # 变异：重读前缀数行")],
     "test_corpus_ingest_wb.ResumePrefixTest"),
    ("M8", "流式碎片事件进 skip 表 → 把其中两种摘掉（掉回 unknown）",
     [("fish_corpus_dsh.py",
       '    "reasoning-chunks", "assistant/chunk", "tool-call-chunks", "text-chunks",\n'
       '    "session-log-deepseek/delivery-accepted",\n', "")],
     "test_corpus_ingest_dsh.SkipEventTypesTest"),
    # ── 第二批（2026-10-27 · 日报闸门 + forget 作用域 + 帧边界/停滞 + 擦除 + 脱敏）──
    ("G1", "日报语料采集闸门（默认不跑） → 把闸门摘掉（恢复无条件采集）",
     [("daily_report.py",
       '        if not enabled:\n'
       '            return "语料采集：跳过（%s。%s）" % (reason, CORPUS_ENABLE_HINT)\n',
       '        if False:  # 变异：闸门失效\n'
       '            return "语料采集：跳过（%s。%s）" % (reason, CORPUS_ENABLE_HINT)\n')],
     "test_corpus_contract.DailyCorpusGateTest"),
    ("G2", "pointer/range 台账在采集收口回收 → 摘掉回收（原文复活）",
     [("fish_corpus.py",
       "            purged = store.purge_segment_tombstones(conn)",
       '            purged = {"deleted": 0, "pointers": 0, "ranges": 0}  # 变异：不回收')],
     "test_corpus_governance.PointerRangeScopeTest"),
    ("G3", "offset==size 算帧边界（纯 mtime 触碰不重读） → 改回恒 False",
     [("fish_corpus_dsh.py",
       "        if offset == size:\n            return True",
       "        if False:  # 变异：文件尾不算边界\n            return True")],
     "test_corpus_ingest_dsh.MtimeTouchTest"),
    ("G4", "停滞判据要求新段+新事件双 0 → 改回只看新段",
     [("fish_corpus.py",
       "    if new_segments == 0 and new_events == 0 and source_moved:",
       "    if new_segments == 0 and source_moved:  # 变异：只看段数")],
     "test_corpus_ingest_dsh.StallAlarmTest"),
    ("G5", "源指纹不含 mtime（纯触碰不算源在动） → 把 mtime 塞回指纹",
     [("fish_corpus.py",
       '"SELECT host, session_id, bytes_total, events_seen, segments_kept"',
       '"SELECT host, session_id, bytes_total, events_seen, segments_kept, mtime_ns"'),
      ("fish_corpus.py",
       'blob = "|".join("%s:%s:%s:%s:%s" % tuple(row) for row in rows)',
       'blob = "|".join("%s:%s:%s:%s:%s:%s" % tuple(row) for row in rows)')],
     "test_corpus_ingest_dsh.MtimeTouchTest"),
    ("G6", "擦除 busy 不算成功 → 改回「有返回行就算成功」",
     [("fish_corpus_store.py",
       "        if int(row[0]) == 0:",
       "        if True:  # 变异：busy 也当成功")],
     "test_corpus_governance.EraseReceiptTest"),
    ("G7", "CLI 打印擦除读数 → 把回执去掉（命令行不再说擦没擦）",
     [("fish_corpus.py",
       '        erase = res.get("erase") or {}',
       "        erase = {}  # 变异：不回显擦除结果")],
     "test_corpus_governance.EraseReceiptTest"),
    ("G8", "cwd 写入前脱敏 → 改回原样落库",
     [("fish_corpus_tag.py",
       '            "cwd": _safe(cwd),',
       '            "cwd": cwd,  # 变异：明文 cwd 落库')],
     "test_corpus_governance.CwdRedactionTest"),
    ("G9", "oversize_text 也让状态/摘要一致 → 只写台账、不置 status",
     [("fish_corpus_dsh.py",
       '        counters["status"] = store.SOURCE_DEGRADED\n'
       '        counters["degraded"].append({\n'
       '            "reason_code": store.REASON_OVERSIZE_TEXT, "reason": reason,',
       '        counters["degraded"].append({\n'
       '            "reason_code": store.REASON_OVERSIZE_TEXT, "reason": reason,')],
     "test_corpus_ingest_dsh.OversizeGuardTest"),
    ("G10", "目录名≠头部 id 的告警并入返回 → 改回写死键（丢弃）",
     [("fish_corpus_dsh.py",
       '                    counters["warnings"].append("path_id_mismatch")',
       '                    counters["warning"] = "path_id_mismatch"  # 变异：死键')],
     "test_corpus_ingest_dsh.PathIdMismatchTest"),
    # ── 第三批（2026-10-27 终批：跨宿主 forget / 源变短 / 出口脱敏 / stats 同步 /
    #     retag 降级 / 擦除等待 / EP3 编码）──
    ("G11", "源变短（offset>size）不认边界 → 归零重采 → 改回「越过文件尾也算边界」",
     [("fish_corpus_dsh.py",
       "        if offset > size:\n"
       "            return False                       # 源变短了：游标悬空 → 调用方归零重采",
       "        if offset > size:\n            return True  # 变异：源变短也当文件尾")],
     "test_corpus_ingest_dsh.SourceShrunkTest"),
    ("G12", "跨宿主 session 台账（`*:<sid>`）→ 把通配那一半摘掉",
     [("fish_corpus_store.py",
       '    return (host, session_id) in sessions or ("*", session_id) in sessions',
       '    return (host, session_id) in sessions  # 变异：不看跨宿主台账')],
     "test_corpus_governance.CrossHostSessionForgetTest"),
    ("G13", "read 的 session_title 出口脱敏 → 改回原样回显",
     [("fish_corpus.py",
       '            "session_title": _safe_field(seg.get("session_title")),',
       '            "session_title": seg.get("session_title"),  # 变异：标题出口不打码')],
     "test_corpus_governance.ExitRedactionTest"),
    ("G14", "stats 的 orphan[].path 出口脱敏 → 改回原样回显",
     [("fish_corpus.py",
       '                   "path": _safe_field(r["path"])}',
       '                   "path": r["path"]}  # 变异：孤儿路径不打码')],
     "test_corpus_governance.ExitRedactionTest"),
    ("G15", "refresh_session_counts 同步游标里的 segments_kept → 只改会话表",
     [("fish_corpus_store.py",
       '    conn.execute("UPDATE corpus_source SET segments_kept=? WHERE host=? AND session_id=?",\n'
       '                 (seg, host, session_id))',
       "    pass  # 变异：不同步游标里的段数")],
     "test_corpus_governance.SourceSegmentsSyncTest"),
    ("G16", "retag 不许把 mapped 悄悄降成 unmapped → 摘掉拦截",
     [("fish_corpus_tag.py",
       "            downgrade = (source == PROJECT_UNMAPPED and not allow_downgrade\n"
       "                         and stored_project\n"
       '                         and row.get("project_source") == PROJECT_MAPPED)',
       "            downgrade = False  # 变异：已映射也能被悄悄降成未映射")],
     "test_corpus_governance.RetagMappingTest"),
    ("G17", "擦除期间 busy_timeout 压到 ≤2 s → 改回沿用调用方的 15 s",
     [("fish_corpus_store.py",
       "    _set_busy_timeout(conn, ERASE_BUSY_TIMEOUT_MS\n"
       "                      if restore_ms is None else min(restore_ms, ERASE_BUSY_TIMEOUT_MS))",
       "    _set_busy_timeout(conn, None)  # 变异：压不住等待上限（沿用 15 s）")],
     "test_corpus_governance.EraseLatencyTest"),
    ("G18", "EP3 子进程显式传 UTF-8 环境 → 改回继承控制台编码（cp936）",
     [("test_export_public.py",
       "            capture_output=True, text=True, encoding=\"utf-8\", errors=\"replace\",\n"
       "            env=_child_env())",
       "            capture_output=True, text=True, encoding=\"utf-8\", errors=\"replace\")")],
     "test_export_public.ExportPublicTest"),
    # ── 终验黄项（2026-10-27：retag 保身份 / CLI 降级行脱敏 / events_seen 口径）──
    ("G19", "CLI 降级行的源路径出口脱敏 → 改回原样回显（终端泄露 token）",
     [("fish_corpus.py",
       "            print(f\"  ⚠ 降级 {item['host']} {_safe_field(item['path'])} {item['reasons']}\")",
       "            print(f\"  ⚠ 降级 {item['host']} {item['path']} {item['reasons']}\")")],
     "test_corpus_governance.CliIngestRedactionTest"),
    ("G20", "retag 解析器没给出公民时保留原身份 → 改回照写 (None,None)（抹身份）",
     [("fish_corpus_tag.py",
       "            if resolved[0]:\n"
       "                citizen_id, identity_source = resolved",
       "            if True:  # 变异：解析器没给值也照写\n"
       "                citizen_id, identity_source = resolved")],
     "test_corpus_governance.RetagIdentityTest"),
    ("G21", "retag 把 citizen_of 透传给 Tagger → 改回丢掉解析器",
     [("fish_corpus.py",
       "        tagger = tag_mod.Tagger(project_of=project_of, citizen_of=citizen_of,\n"
       "                                candidates_of=candidates_of)",
       "        tagger = tag_mod.Tagger(project_of=project_of, citizen_of=None,  # 变异\n"
       "                                candidates_of=candidates_of)")],
     "test_corpus_governance.RetagIdentityTest"),
    ("G22", "源变短归零重采时 events_seen 以本次为准 → 改回叠加旧游标（虚增）",
     [("fish_corpus_dsh.py",
       "    events_base = 0 if not start else events_seen_before",
       "    events_base = events_seen_before  # 变异：归零重采也叠加旧游标")],
     "test_corpus_ingest_dsh.EventsSeenSemanticsTest"),
    # ── 采集完整性三修（2026-10-27：帧上限可配 + 超限保段 / v0 旧格式 / realpath 匹配）──
    ("A1", "帧上限默认提到 64 MiB（首采 10+ 个帧被 8 MiB 挡掉） → 改回 4 MiB",
     [("fish_corpus_dsh.py",
       "DEFAULT_MAX_FRAME_BYTES = 64 << 20",
       "DEFAULT_MAX_FRAME_BYTES = 4 << 20  # 变异：上限退回旧量级")],
     "test_corpus_ingest_dsh.FrameCapConfigTest"),
    ("A2", "超限帧先 flush 已解出的段再登记降级 → 把这次 flush 删掉（整帧丢）",
     [("fish_corpus_dsh.py",
       "                flush(force=True, upto=pending_offset)\n"
       "            except Exception:",
       "                pass  # 变异：不先 flush 就降级\n"
       "            except Exception:")],
     "test_corpus_ingest_dsh.OversizePartialKeepTest"),
    ("B1", "legacy v0（`session.jsonl.zstd`，无 vN）与 v4 同构可采 → 从支持列表摘掉",
     [("fish_corpus_dsh.py",
       "SUPPORTED_FORMATS = (store.FORMAT_DSH_V4, store.FORMAT_DSH_LEGACY)",
       "SUPPORTED_FORMATS = (store.FORMAT_DSH_V4,)  # 变异：v0 回到 unsupported_format")],
     "test_corpus_ingest_dsh.LegacyV0FormatTest"),
    ("C1", "项目解析按 realpath 补一次比对（链接/短路径） → 删掉物理比对分支",
     [("fish.py",
       "        canon = norm\n"
       "        if not (target == norm or target.startswith(norm + \"/\")):\n"
       "            canon = _canonical_path(prefix)       # C：两种写法指向同一物理目录\n"
       "            if not (target_canon == canon or target_canon.startswith(canon + \"/\")):\n"
       "                continue\n",
       "        canon = norm\n"
       "        if not (target == norm or target.startswith(norm + \"/\")):\n"
       "            continue  # 变异：只做文本归一\n")],
     "test_workspace_map.RealpathMatchTest"),
    # ── 采集时间窗口（2026-10-27 用户决策：只投影最近 3 天）──
    ("W1", "窗口比较恒 True（全采）→ 4 天前的会话也被投影进来",
     [("fish_corpus.py",
       '    return int(item.get("mtime_ns") or 0) >= int(cutoff_ns)',
       "    return True  # 变异：窗口形同虚设（全采）")],
     "test_corpus_ingest_dsh.WindowTest"),
    ("W2", "窗口比较恒 False（全丢）→ 2 天前的会话也采不到（静默少采）",
     [("fish_corpus.py",
       '    return int(item.get("mtime_ns") or 0) >= int(cutoff_ns)',
       "    return False  # 变异：窗口恒不通过（静默少采）")],
     "test_corpus_ingest_dsh.WindowTest"),
    ("W3", "日报语料步漏传 days（函数默认 None = 不限）→ 4 天前的会话也被日采进来",
     [("daily_report.py",
       "                days=fish_corpus.DEFAULT_WINDOW_DAYS,\n",
       "")],
     "test_corpus_push.DailyReportWindowTest"),
)

FAIL_RE = ("FAILED", "ERROR:", "Traceback (most recent call last)")


def _copy_worktree(dst: Path) -> None:
    """只拷源码与判据（不拷 data/、research/、日志）。"""
    for path in HERE.glob("*.py"):
        shutil.copy2(path, dst / path.name)
    tools = HERE / "tools"
    if tools.exists():
        shutil.copytree(tools, dst / "tools", dirs_exist_ok=True)


def _apply(dst: Path, patches) -> "str | None":
    for name, old, new in patches:
        if old == new:
            continue                      # no-op 补丁（占位用）
        target = dst / name
        text = target.read_text(encoding="utf-8")
        if old not in text:
            return "靶点没找到：%s 里没有 %r" % (name, old[:60])
        target.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")
    return None


def _run(dst: Path, test: str) -> tuple:
    proc = subprocess.run([sys.executable, "-X", "utf8", "-m", "unittest", test],
                          cwd=str(dst), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, output


def main(argv=None) -> int:
    wanted = {a.upper() for a in (argv if argv is not None else sys.argv[1:])}
    rows, missed, broken = [], [], []
    for code, desc, patches, test in MUTATIONS:
        if wanted and code not in wanted:
            continue
        tmp = Path(tempfile.mkdtemp(prefix="corpus-mut-%s-" % code.lower()))
        try:
            _copy_worktree(tmp)
            problem = _apply(tmp, patches)
            if problem:
                broken.append((code, problem))
                rows.append((code, "BROKEN", problem))
                continue
            rc, output = _run(tmp, test)
            caught = rc != 0 and any(mark in output for mark in FAIL_RE)
            rows.append((code, "CAUGHT" if caught else "MISSED", desc))
            if not caught:
                missed.append((code, desc, output[-600:]))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    print("变异自证 · 语料层修复（第一批 14 + 第二批 10 + 终批 8 + 终验黄项 4"
          " + 采集完整性 4 + 时间窗口 3，共 %d 个变异体）" % len(rows))
    for code, verdict, desc in rows:
        print("  %-4s %-8s %s" % (code, verdict, desc))
    if broken:
        print("\n靶点失效（判据在跑之前就该修）：")
        for code, problem in broken:
            print("  %s: %s" % (code, problem))
    for code, desc, tail in missed:
        print("\nMISSED %s（%s）\n%s" % (code, desc, tail))
    ok = not missed and not broken
    print("\n结论：%s" % ("全 CAUGHT（%d/%d）" % (len(rows), len(rows)) if ok
                        else "有 MISSED/BROKEN（判据不成立）"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
