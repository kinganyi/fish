#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T02 判据 · 采集层（`fish_activity` + `tool_activity` + 自动面 + 隐私 + 统计）。

## 判据清单

  C1  配置默认值**两份表必须一致**（`fish.CONFIG_DEFAULTS` ↔ `fish_activity.CONFIG_FALLBACK`）
  C2  分类：写类→`write`；Bash 强特征→`exec`（目标可解析才占面）；读类/其他→None
  C3  Bash 的两种形态：目标可解析（占面）/ 不可解析（只记 exec、三列路径 NULL）
  C4  落库：一行、字段齐全、`args_digest` 16 位、`ok` 三态
  C5  **隐私**：写含 `SECRET-XYZ` 的内容 → 库里命中 0 次；digest 与内容无关
  C6  聚合：`auto_surfaces` 只算写类、按目录合并、按窗口截断
  C7  快照：原子写、条目含声明面/实际面、`read_snapshot` 过期返回 None
  C7b 快照 `auto.dir_abs` **只来自事实列 `path_abs`**（评审 L1）：未映射项目也绝对，
      跨项目动作不套看板所在项目根（+ 变异自证）
  C8  cache 节流：同窗口内第二次不刷 presence
  C9  `prune` / `stats`：0 天不清理；统计口径对得上
  C10 citizens 查询**逐条冻结**（新模块自己的读点，不许悄悄增删）
  C11 变异：把 `read` 也落库 → C2 红；把内容写进 digest 输入 → C5 红
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fish  # noqa: E402
import fish_activity  # noqa: E402
import fish_surface  # noqa: E402


def _now(offset_min=0):
    import datetime
    base = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=offset_min)
    return base.strftime("%Y-%m-%dT%H:%M:%SZ")


class ActivityBase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-act-"))
        self.root = self.tmp / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES ('PJ','PJ',NULL,0,'active')")
        self.conn.execute(
            "INSERT INTO workspace_map (path_prefix,project,note,created_at) "
            "VALUES ('i:/proj','PJ',NULL,?)", (_now(),))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _payload(self, tool, tool_input, *, session="s1", cwd="i:/proj",
                 response=None):
        return {"session_id": session, "cwd": cwd, "tool_name": tool,
                "tool_input": tool_input, "tool_response": response}

    def _rows(self):
        return self.conn.execute(
            "SELECT host, session_key, tool_name, action, path, path_dir, path_abs, "
            "args_digest, ok, project FROM tool_activity ORDER BY id").fetchall()


class ConfigParityTest(ActivityBase):
    """C1：hook 快路径不许 import fish，但默认值必须只有一处真相。"""

    def test_fallback_matches_fish_defaults(self):
        for key, fallback in fish_activity.CONFIG_FALLBACK.items():
            self.assertIn(key, fish.CONFIG_DEFAULTS, f"{key} 不在 fish.CONFIG_DEFAULTS 里")
            self.assertEqual(fish.CONFIG_DEFAULTS[key][0], fallback,
                             f"{key} 的默认值两侧不一致（改一处必须改两处）")

    def test_get_cfg_reads_db_then_fallback(self):
        self.assertEqual(fish_activity.get_cfg(self.conn, "board.collision_ask"), "warn")
        fish.set_config(self.conn, "board.collision_ask", "ask")
        self.assertEqual(fish_activity.get_cfg(self.conn, "board.collision_ask"), "ask")
        self.assertEqual(fish_activity.get_cfg(self.conn, "nope.key", 7), 7)

    def test_read_cfg_works_without_db(self):
        empty = self.tmp / "nodb"
        empty.mkdir()
        self.assertEqual(fish_activity.read_cfg(empty, "activity.enabled"), True)


class ClassifyTest(ActivityBase):
    """C2/C3：分类是"H1 隐私 + 撞车判定"的入口，必须保守。"""

    def test_write_tools(self):
        for tool in ("write", "Write", "edit", "MultiEdit", "apply_patch"):
            c = fish_activity.classify_tool_call(
                tool, {"file_path": "p0/fish.py"}, cwd="i:/proj", project_root="i:/proj")
            self.assertIsNotNone(c, f"{tool} 应当是写类")
            self.assertEqual(c["action"], "write")
            self.assertEqual(c["path"], "p0/fish.py")
            self.assertEqual(c["path_dir"], "p0")
            self.assertEqual(c["path_abs"], "i:/proj/p0/fish.py")

    def test_read_and_other_not_recorded(self):
        for tool in ("read", "grep", "glob", "ls", "mcp__fish__remember"):
            self.assertIsNone(fish_activity.classify_tool_call(
                tool, {"file_path": "p0/fish.py"}, cwd="i:/proj"), f"{tool} 不该落库")

    def test_bash_resolvable_target_occupies_surface(self):
        c = fish_activity.classify_tool_call(
            "bash", {"command": "echo hi > out.txt"}, cwd="i:/proj",
            project_root="i:/proj")
        self.assertEqual(c["action"], "exec")
        self.assertEqual(c["path"], "out.txt")
        self.assertEqual(c["path_abs"], "i:/proj/out.txt")

    def test_bash_unresolvable_target_only_counts(self):
        """`git commit` / 包管理器 / `python -c` → 只记 exec，**不占面**（防假撞车）。"""
        for command in ("git commit -m x", "pnpm install", "python -c 'open(\"a\",\"w\")'"):
            c = fish_activity.classify_tool_call(
                "bash", {"command": command}, cwd="i:/proj", project_root="i:/proj")
            self.assertIsNotNone(c, f"{command} 应记为 exec")
            self.assertEqual(c["action"], "exec")
            self.assertIsNone(c["path_abs"], f"{command} 的目标不可解析，不该占面")
            self.assertIsNone(c["path_dir"])

    def test_bash_no_write_feature_not_recorded(self):
        self.assertIsNone(fish_activity.classify_tool_call(
            "bash", {"command": "ls -la"}, cwd="i:/proj"))

    def test_redirect_to_null_ignored(self):
        c = fish_activity.classify_tool_call(
            "bash", {"command": "make 2>/dev/null"}, cwd="i:/proj")
        self.assertIsNone(c, "重定向到 /dev/null 不是写动作")

    def test_powershell_and_sed_features(self):
        for command in ("Set-Content -Path a.txt -Value x", "sed -i 's/a/b/' c.txt",
                        "tee -a d.txt", "rm -rf e"):
            c = fish_activity.classify_tool_call("bash", {"command": command},
                                                 cwd="i:/proj")
            self.assertIsNotNone(c, f"{command} 应识别为写动作")
            self.assertIsNotNone(c["path_abs"], f"{command} 的目标应当可解析")

    def test_ok_three_states(self):
        self.assertIsNone(fish_activity.judge_ok(None), "没有 response = 未知，不是失败")
        self.assertEqual(fish_activity.judge_ok({"content": "ok"}), 1)
        self.assertEqual(fish_activity.judge_ok({"content": "Error: no such file"}), 0)
        self.assertEqual(fish_activity.judge_ok({"is_error": True}), 0)
        self.assertEqual(fish_activity.judge_ok({"is_error": False}), 1)


class RecordTest(ActivityBase):
    """C4/C5：落一行，只落元数据。"""

    def test_record_and_privacy(self):
        result = fish_activity.handle_post_tool_use(
            self.conn, host="dsh",
            payload=self._payload("write", {
                "file_path": "i:/proj/_smoke.txt", "content": "SECRET-XYZ-123"}),
            now=_now(), data_dir=self.root)
        self.assertTrue(result["recorded"])
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        host, session_key, tool, action, path, path_dir, path_abs, digest, ok, project = rows[0]
        self.assertEqual(host, "dsh")
        self.assertEqual(session_key, "dsh:s1")
        self.assertEqual(tool, "write")
        self.assertEqual(action, "write")
        self.assertEqual(path, "_smoke.txt")
        self.assertEqual(path_dir, ".")
        self.assertEqual(path_abs, "i:/proj/_smoke.txt")
        self.assertEqual(len(digest), 16, "digest 是 sha256[:16]")
        self.assertIsNone(ok, "没有 tool_response → NULL（不假装成功）")
        self.assertEqual(project, "PJ", "项目按写入目标解析（不是只看 cwd）")
        # 隐私：库文件里**不能**出现内容
        raw = (self.root / "index.sqlite").read_bytes()
        self.assertEqual(raw.count(b"SECRET-XYZ-123"), 0,
                         "内容绝不落盘（库里命中 0 次）")
        self.assertEqual(raw.count(b"SECRET"), 0)

    def test_digest_independent_of_content(self):
        """同一文件、不同内容 → 长度不同则 digest 不同；**同一内容不同文件**也一定不同。"""
        base = {"file_path": "i:/proj/a.py"}
        first = fish_activity.classify_tool_call(
            "write", {**base, "content": "x" * 10}, cwd="i:/proj")
        second = fish_activity.classify_tool_call(
            "write", {**base, "content": "SECRET-XYZ" * 10}, cwd="i:/proj")
        self.assertNotEqual(first["args_digest"], second["args_digest"],
                            "长度进摘要（内容不进）")
        self.assertNotIn("SECRET", first["args_digest"] + second["args_digest"])

    def test_disabled_means_no_row(self):
        fish.set_config(self.conn, "activity.enabled", False)
        result = fish_activity.handle_post_tool_use(
            self.conn, host="dsh",
            payload=self._payload("write", {"file_path": "i:/proj/a.py"}),
            now=_now(), data_dir=self.root)
        self.assertFalse(result["recorded"])
        self.assertEqual(fish_activity.stats(self.conn)["rows"], 0)

    def test_workbuddy_dialect(self):
        """WorkBuddy payload：反斜杠路径 + 无 tool_use_id，结果必须一样。"""
        result = fish_activity.handle_post_tool_use(
            self.conn, host="workbuddy",
            payload={"session_id": "wb1", "cwd": "I:\\proj",
                     "tool_name": "Write",
                     "tool_input": {"file_path": "I:\\proj\\p0\\x.py"},
                     "tool_response": {"content": "ok"}},
            now=_now(), data_dir=self.root)
        self.assertTrue(result["recorded"])
        row = self._rows()[0]
        self.assertEqual(row[1], "codebuddy:wb1", "WorkBuddy 会话键前缀是 codebuddy")
        self.assertEqual(row[6], "i:/proj/p0/x.py")
        self.assertEqual(row[8], 1)


class AggregateTest(ActivityBase):
    """C6/C8：读时聚合 + 缓存节流。"""

    def _record(self, path, minutes_ago=0, *, session="s2", action="write"):
        self.conn.execute(
            "INSERT INTO tool_activity (host, session_key, citizen_id, project, tool_name, "
            "action, path, path_dir, path_abs, args_digest, ok, seen_at) "
            "VALUES ('dsh', ?, 'C1', 'PJ', 'write', ?, ?, ?, ?, 'x', 1, ?)",
            (session, action, path, fish_surface_dir(path), "i:/proj/" + path,
             _now(minutes_ago)))
        self.conn.commit()

    def test_auto_surfaces_groups_by_dir(self):
        self._record("p0/a.py", 1)
        self._record("p0/b.py", 2)
        self._record("docs/c.md", 3)
        self._record("p0/old.py", 120)                 # 超窗
        self.conn.execute("UPDATE tool_activity SET citizen_id='C1'")
        self._record_action("read", "p0/read.py", 1)   # 读类不参与
        info = fish_activity.auto_surfaces(self.conn, window_min=30)
        dirs = {d["dir"]: d["count"] for d in info["C1"]["dirs"]}
        self.assertEqual(dirs.get("p0"), 2)
        self.assertEqual(dirs.get("docs"), 1)
        self.assertNotIn("p0/read.py", dirs)

    def _record_action(self, action, path, minutes_ago):
        self.conn.execute(
            "INSERT INTO tool_activity (host, session_key, citizen_id, project, tool_name, "
            "action, path, path_dir, path_abs, args_digest, ok, seen_at) "
            "VALUES ('dsh','s9','C1','PJ','read',?,?,?,?,'x',1,?)",
            (action, path, fish_surface_dir(path), "i:/proj/" + path, _now(minutes_ago)))
        self.conn.commit()

    def test_refresh_presence_cache_throttles(self):
        board = fish.board_checkin(self.conn, "C1", "干活", scope="project", scope_key="PJ")
        now = _now()
        first = fish_activity.refresh_presence_cache(
            self.conn, citizen_id="C1", project="PJ", dirs=["p0"], seen_at=now,
            min_interval_sec=20)
        second = fish_activity.refresh_presence_cache(
            self.conn, citizen_id="C1", project="PJ", dirs=["docs"], seen_at=now,
            min_interval_sec=20)
        self.assertTrue(first)
        self.assertFalse(second, "同一秒内第二次必须被节流（不刷库）")
        row = self.conn.execute(
            "SELECT auto_surface, auto_source, doing FROM presence WHERE board_id=?",
            (board["id"],)).fetchone()
        self.assertEqual(row[0], "p0")
        self.assertEqual(row[1], "activity")
        self.assertEqual(row[2], "干活", "缓存刷新**不许**动人的声明")

    def test_refresh_does_not_create_rows(self):
        """没有今日 board 行时**不建**（自动行是 SessionStart 的职责）。"""
        before = self.conn.execute("SELECT COUNT(*) FROM presence").fetchone()[0]
        written = fish_activity.refresh_presence_cache(
            self.conn, citizen_id="C1", project="PJ", dirs=["p0"], seen_at=_now())
        self.assertFalse(written)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM presence").fetchone()[0],
                         before)


def fish_surface_dir(path):
    idx = path.rfind("/")
    return path[:idx] if idx > 0 else "."


class SnapshotTest(ActivityBase):
    """C7：快照是 PreToolUse 的唯一输入，必须原子且可判过期。"""

    def _prepare(self):
        board = fish.board_checkin(self.conn, "C1", "改 fish", surface="p0/fish.py",
                                   scope="project", scope_key="PJ")
        self.conn.execute("UPDATE presence SET agent_id='dsh:other' WHERE board_id=?",
                          (board["id"],))
        self.conn.commit()
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,status,created_at,host) "
            "VALUES ('C1','主AI','resident','active',?,'dsh')", (_now(),))
        self.conn.commit()
        return board

    def test_build_snapshot_entries(self):
        self._prepare()
        out = self.root / fish_activity.SNAPSHOT_RELPATH
        snap = fish_activity.build_snapshot(self.conn, out_path=out, window_min=90)
        self.assertTrue(out.exists(), "快照必须真的写盘")
        self.assertEqual(snap["entries"][0]["label"], "主AI")
        entry = snap["entries"][0]
        self.assertEqual(entry["session_key"], "dsh:other")
        self.assertIn("p0/fish.py", entry["declared"])
        self.assertIn("i:/proj/p0/fish.py", entry["declared_abs"],
                      "绝对面按项目根解析（撞车判定要用）")
        # 原子写：不该留下 .tmp
        self.assertFalse(out.with_name(out.name + ".tmp").exists())

    # ---------------------------------------------------------------- L1（评审）
    #
    # 这两条钉的是**同一件事**：快照里的 `auto.dir_abs` 只能来自事实列 `path_abs`，
    # 不能拿"相对显示串 + 看板所属项目根"反推——反推出来是一个**不存在的路径**
    # （A 板 + B 项目的动作 → `i:/a/p0`；base 为空时干脆留相对串），
    # 而 `find_collisions` 拿它判撞车 = 拿伪造的路径判撞车。

    def _activity_row(self, *, project, path_dir, path_abs, session="dsh:other",
                      minutes_ago=0):
        """造一行写动作事实（`path_dir` 是**显示列**，`path_abs` 是**事实列**）。"""
        self.conn.execute(
            "INSERT INTO tool_activity (host, session_key, citizen_id, project, tool_name, "
            "action, path, path_dir, path_abs, args_digest, ok, seen_at) "
            "VALUES ('dsh', ?, 'C1', ?, 'write', 'write', ?, ?, ?, 'x', 1, ?)",
            (session, project, path_dir, path_dir, path_abs, _now(minutes_ago)))
        self.conn.commit()

    def test_auto_dir_abs_from_fact_when_no_project(self):
        """L1 ①：未映射项目（root 板，base=None）时 `dir_abs` 仍必须是**绝对且真实**。"""
        import shutil
        real = Path(tempfile.mkdtemp(prefix="fish-l1-"))
        self.addCleanup(shutil.rmtree, real, True)
        folder = real / "p0" / "data"
        folder.mkdir(parents=True)
        path_abs = fish_surface.normalize_path(folder / "x.db")
        board = fish.board_checkin(self.conn, "C1", "改东西", scope="root", scope_key="")
        self.conn.execute("UPDATE presence SET agent_id='dsh:other' WHERE board_id=?",
                          (board["id"],))
        self.conn.commit()
        self._activity_row(project=None, path_dir="p0/data", path_abs=path_abs)
        snap = fish_activity.build_snapshot(
            self.conn, out_path=self.root / fish_activity.SNAPSHOT_RELPATH, window_min=90)
        auto = snap["entries"][0]["auto"]
        self.assertEqual(len(auto), 1, f"写动作必须进快照：{snap['entries']}")
        item = auto[0]
        self.assertTrue(fish_surface.is_absolute(item["dir_abs"]),
                        f"dir_abs 必须是绝对路径（旧实现会留下相对串）：{item}")
        self.assertEqual(item["dir_abs"], fish_surface.dir_of(path_abs),
                         "dir_abs 只能是事实列 path_abs 的目录")
        self.assertTrue(Path(item["dir_abs"]).is_dir(), "这条路必须真实存在")
        self.assertTrue(path_abs.startswith(item["dir_abs"] + "/"),
                        "path_abs 必须落在 dir_abs 之下")

    def test_auto_dir_abs_not_rebased_to_board_project(self):
        """L1 ②：同公民在 A 板、动作在 B 项目 → `dir_abs` 以事实为准，不套看板项目根。"""
        self._prepare()                      # A 板：project PJ（根 i:/proj）
        self.conn.execute(
            "INSERT INTO workspace_map (path_prefix,project,note,created_at) "
            "VALUES ('i:/other','PB',NULL,?)", (_now(),))
        self.conn.commit()
        path_abs = "i:/other/p0/data/x.db"
        self._activity_row(project="PB", path_dir="p0/data", path_abs=path_abs)
        snap = fish_activity.build_snapshot(
            self.conn, out_path=self.root / fish_activity.SNAPSHOT_RELPATH, window_min=90)
        item = snap["entries"][0]["auto"][0]
        self.assertEqual(item["dir_abs"], fish_surface.dir_of(path_abs),
                         "要看动作事实，不要看板所属项目")
        self.assertNotEqual(item["dir_abs"], "i:/proj/p0/data",
                            "旧实现按看板项目根反推会造出这个**不存在**的路径")

    def test_L1_mutation_display_derived_dir_abs_goes_red(self):
        """变异自证：把 `dir_abs` 改回"显示串 + 看板项目根反推" → ② 的断言必须红。

        2026-10-26（撞车自撞修复）：快照的「实际面」现在有**两条来源**——公民级
        `auto_surfaces` 与会话级 `auto_surfaces_by_session`（设计 45 §3.6）。变异必须
        **同时**伪造两者，否则其中一条还在给真值，变异不生效、判据假绿。
        本判据钉的不变量没变：`dir_abs` 只能来自事实列 `path_abs`。
        """
        original = fish_activity.auto_surfaces
        original_by_session = fish_activity.auto_surfaces_by_session

        def _forge(info):
            for entry in info.values():
                for item in entry["dirs"]:
                    item["dir_abs"] = fish_surface.make_abs(item["dir"], "i:/proj")
            return info

        try:
            def forged(conn, *, window_min=30, now=None, project=None):
                return _forge(original(conn, project=project, window_min=window_min,
                                       now=now))

            def forged_by_session(conn, *, window_min=30, now=None, project=None):
                return _forge(original_by_session(conn, project=project,
                                                  window_min=window_min, now=now))

            fish_activity.auto_surfaces = forged
            fish_activity.auto_surfaces_by_session = forged_by_session
            self._prepare()
            path_abs = "i:/other/p0/data/x.db"
            self._activity_row(project="PB", path_dir="p0/data", path_abs=path_abs)
            snap = fish_activity.build_snapshot(
                self.conn, out_path=self.root / fish_activity.SNAPSHOT_RELPATH,
                window_min=90)
            item = snap["entries"][0]["auto"][0]
            self.assertNotEqual(item["dir_abs"], fish_surface.dir_of(path_abs),
                                "变异后应当被改写成 i:/proj/p0/data——"
                                "若仍然相等，说明 L1 ② 没在钉'事实列'")
            self.assertEqual(item["dir_abs"], "i:/proj/p0/data")
        finally:
            fish_activity.auto_surfaces = original
            fish_activity.auto_surfaces_by_session = original_by_session
        # 恢复后必须回到"以事实为准"
        snap = fish_activity.build_snapshot(
            self.conn, out_path=self.root / fish_activity.SNAPSHOT_RELPATH, window_min=90)
        self.assertEqual(snap["entries"][0]["auto"][0]["dir_abs"], "i:/other/p0/data")

    def test_read_snapshot_expiry(self):
        out = self.root / fish_activity.SNAPSHOT_RELPATH
        self._prepare()
        fish_activity.build_snapshot(self.conn, out_path=out)
        self.assertIsNotNone(fish_activity.read_snapshot(out, max_age_sec=3600),
                             "新鲜快照必须读得出来")
        # 过旧：把 generated_at 改到 2 小时前 → 返回 None（PreToolUse 据此放行）
        payload = json.loads(out.read_text(encoding="utf-8"))
        payload["generated_at"] = _now(120)
        out.write_text(json.dumps(payload), encoding="utf-8")
        self.assertIsNone(fish_activity.read_snapshot(out, max_age_sec=3600))

    def test_read_snapshot_bad_file(self):
        out = self.root / "bad.json"
        out.write_text("{not json", encoding="utf-8")
        self.assertIsNone(fish_activity.read_snapshot(out))
        self.assertIsNone(fish_activity.read_snapshot(self.root / "missing.json"))


class StatsPruneTest(ActivityBase):
    """C9：0 = 不清理（沿用 decay.*=0 的约定）。"""

    def _seed(self, days_ago):
        import datetime
        ts = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.conn.execute(
            "INSERT INTO tool_activity (host, session_key, project, tool_name, action, "
            "path, path_dir, path_abs, args_digest, ok, seen_at) "
            "VALUES ('dsh','dsh:s','PJ','write','write','p0/a.py','p0','i:/proj/p0/a.py',"
            "'x',1,?)", (ts,))
        self.conn.commit()

    def test_prune_zero_means_keep(self):
        self._seed(30)
        self.assertEqual(fish_activity.prune(self.conn, days=0), 0, "0 = 不清理")
        self.assertEqual(fish_activity.prune(self.conn, days=14), 1)

    def test_stats_shape(self):
        self._seed(0)
        st = fish_activity.stats(self.conn, days=1)
        for key in ("rows", "write_rows", "exec_rows", "sessions", "hosts",
                    "top_dirs", "asked", "by_hour"):
            self.assertIn(key, st)
        self.assertEqual(st["rows"], 1)
        self.assertEqual(st["write_rows"], 1)
        self.assertEqual(st["sessions"], 1)
        self.assertEqual(st["hosts"], ["dsh"])


class FrozenCitizensSqlTest(unittest.TestCase):
    """C10：新模块的 citizens 读点**逐条冻结**（设计 T02 §7）。

    与 `test_identity_p1a` 判据 G 同一个纪律，但作用域只在本模块：
    这样"新模块加了一个 citizens 查询"会被这里挡住，而不是悄悄改旧基线。
    """

    # 函数内**字面量**形式的 citizens 读点：只有快照（只读 LEFT JOIN 拿可读名 + host）。
    # 2026-10-26（批 D·FB-14）：`activity_sessions` 是**新登记的读点**——它按会话聚合
    # 「正在动」的行，顺带 LEFT JOIN citizens 取可读名（查不到 → NULL，绝不猜名）。
    # 登记在这里而不是放宽判据：判据仍然是"**逐条**冻结"，加读点必须显式声明。
    EXPECTED_FUNCS = {"_presence_entries", "activity_sessions"}
    # 挂在模块常量上的两条（`resolve_citizen` 用）：逐字冻结，改一个空格都算变更。
    FROZEN_SQL = {
        "SQL_SESSION_TOKEN_CITIZEN": (
            "SELECT citizen_id FROM session_tokens WHERE session_key=? AND "
            "revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ?) "
            "ORDER BY issued_at DESC LIMIT 1"),
        "SQL_HOST_SESSION_CITIZEN": (
            "SELECT citizen_id FROM citizens_all WHERE host_session_key=? AND "
            "status='active' ORDER BY created_at LIMIT 1"),
    }

    def test_module_sql_constants_frozen(self):
        for name, want in self.FROZEN_SQL.items():
            self.assertEqual(" ".join(getattr(fish_activity, name).split()), want,
                             f"{name} 被改了（这是新模块的 citizens 读点，必须声明）")

    def test_in_function_citizens_reads_declared(self):
        import ast
        src = (HERE / "fish_activity.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef,)):
                for sub in ast.walk(node):
                    text = None
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        text = " ".join(sub.value.split())
                    if text and "citizens" in text and text.upper().startswith(
                            ("SELECT", "INSERT", "UPDATE", "DELETE", "PRAGMA")):
                        names.add(node.name)
        self.assertEqual(names, self.EXPECTED_FUNCS,
                         f"新模块里出现未登记的 citizens 读点：{sorted(names)}")

    def test_resolve_citizen_reads_token_then_host(self):
        tmp = Path(tempfile.mkdtemp(prefix="fish-act-id-"))
        try:
            conn = fish.connect(tmp)
            conn.executescript(fish.SCHEMA)
            fish.migrate(conn)
            conn.execute(
                "INSERT INTO citizens (citizen_id,display_name,kind,status,created_at,"
                "host,host_session_key) VALUES ('C9','甲','resident','active',?,"
                "'dsh','dsh:s9')", (_now(),))
            conn.commit()
            self.assertEqual(fish_activity.resolve_citizen(conn, "dsh:s9"), "C9",
                             "没有 token 时回落到 host_session_key")
            self.assertIsNone(fish_activity.resolve_citizen(conn, "dsh:nope"))
            self.assertIsNone(fish_activity.resolve_citizen(conn, ""))
        finally:
            conn.close()
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class MutationTest(ActivityBase):
    """C11：判据必须能变红。"""

    def test_record_read_goes_red(self):
        """把 `read` 也落库 → 「读类不落行」必须红。"""
        original = fish_activity.classify_tool_call

        def loose(tool_name, tool_input, **kw):
            got = original(tool_name, tool_input, **kw)
            if got is None:
                return {"action": "other", "tool_name": str(tool_name).lower(),
                        "path": "", "path_dir": None, "path_abs": "",
                        "args_digest": "x" * 16, "ok": None}
            return got

        fish_activity.classify_tool_call = loose
        try:
            result = fish_activity.handle_post_tool_use(
                self.conn, host="dsh",
                payload=self._payload("read", {"file_path": "i:/proj/a.py"}),
                now=_now(), data_dir=self.root)
            self.assertTrue(result["recorded"], "变异后 read 会被落库——判据该红")
        finally:
            fish_activity.classify_tool_call = original
        self.assertIsNone(fish_activity.classify_tool_call(
            "read", {"file_path": "i:/proj/a.py"}), "恢复后必须回到 None")

    def test_content_in_digest_goes_red(self):
        """把内容拼进 digest 输入 → 「摘要不含内容」必须红（同一断言反向成立）。"""
        same_len_a = fish_activity.classify_tool_call(
            "write", {"file_path": "i:/proj/a.py", "content": "A" * 10}, cwd="i:/proj")
        same_len_b = fish_activity.classify_tool_call(
            "write", {"file_path": "i:/proj/a.py", "content": "B" * 10}, cwd="i:/proj")
        self.assertEqual(same_len_a["args_digest"], same_len_b["args_digest"],
                         "同长度不同内容 → 摘要相同（证明摘要的输入里没有内容）")
        # 变异：把内容也哈希进去 → 上面那条等式立刻不成立
        original = fish_activity._digest

        def leaky(tool_name, path_abs, action, length, feature, content="A" * 10):
            import hashlib
            return hashlib.sha256(
                f"{tool_name}|{path_abs}|{action}|{length}|{feature}|{content}".encode()
            ).hexdigest()[:16]

        try:
            fish_activity._digest = leaky
            mutated_a = fish_activity.classify_tool_call(
                "write", {"file_path": "i:/proj/a.py", "content": "A" * 10}, cwd="i:/proj")
            self.assertNotEqual(same_len_a["args_digest"], mutated_a["args_digest"],
                                "变异后摘要变了——说明原文那条等式确实在测'无内容'")
        finally:
            fish_activity._digest = original
        self.assertIs(fish_activity._digest, original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
