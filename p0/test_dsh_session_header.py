#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSH 新版本会话头回归判据（P2-3，2026-11-26）。

## 为什么单独一条

2026-11-26 的 DSH v4 会话日志首帧 `session` 事件是 **camelCase**：
`id` / `createdAt` / `cwd` / `delegationDepth` / `isSeeded` / `agentPreset` / `version`
（子代理另有 `parentSession` / `origin`）。`fish_lineage_dsh.header_from_event`
把它们归一成 snake_case：`session_id` / `parent_session` / `delegation_depth` /
`is_seeded` / `agent_preset`。

适配器**只认 camelCase 是有意的**：宿主当前写的就是这套字段名。将来宿主改字段名时，
本文件的变异判据会立刻红——而不是让父链悄悄断掉、把子代理误判成根会话。

## 真实样本

取自本机 **2026-11-26 03:45–03:46（UTC+8）扫描到**的 DSH v4 会话日志首帧（cwd 为工作区
路径，不含用户隐私；`createdAt` 是宿主原值的 epoch 毫秒，比文件最后写入时刻更早）：

  根会话  id=session-151b5ad8-d65c-4b7e-af38-d044d39779e9  createdAt=1795603727139
  子代理  id=442a021e-e311-4c5e-9ad1-1ef6a2b82e6c  createdAt=1795635825041
          parentSession=session-b6e726dd-9d2a-4707-bec1-6b7a15c6a154  origin=subagent

## 判据（每条都能失败）

1. 根会话样本 → 归一字段逐项正确（含 createdAt → ISO、is_seeded 保留布尔）；
2. 子代理样本 → `classify` 判 `origin='subagent'` 且父正确；
3. **变异（样本级）**：把样本 dict 的 `parentSession` 改成 `parent_session`
   （模拟宿主改字段名）→ 正判据必须红。**源码级**变异另见 `mutation_dsh_header.py`
   （把 `fish_lineage_dsh.header_from_event` 的提取表达式改掉 → 本文件必须红，逐字节还原后绿）；
4. `header_from_event` 对 `sessionId` 旧写法仍兼容（本机 828 份首帧扫描未发现旧写法，
   但适配器保留 fallback，这条锁住兼容性）。
"""
from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fish_lineage_dsh as dsh  # noqa: E402

try:
    import zstandard
except ImportError:                # pragma: no cover —— 缺依赖时显式跳过
    zstandard = None

#: 真实样本 1：根会话（2026-11-26 03:45 本地，createdAt=1795603727139ms）
REAL_ROOT_HEADER = {
    "type": "session",
    "version": 4,
    "id": "session-151b5ad8-d65c-4b7e-af38-d044d39779e9",
    "createdAt": 1795603727139,
    "cwd": r"I:\app\DSH-Home\workspace\fish-工作区",
    "delegationDepth": 0,
    "isSeeded": True,
    "agentPreset": "dafish-team",
    "parentSession": None,
    "origin": None,
}

#: 真实样本 2：子代理（2026-11-26 03:46 本地，createdAt=1795635825041ms）
REAL_SUBAGENT_HEADER = {
    "type": "session",
    "version": 4,
    "id": "442a021e-e311-4c5e-9ad1-1ef6a2b82e6c",
    "createdAt": 1795635825041,
    "cwd": r"I:\app\DSH-Home\workspace\fish-工作区",
    "delegationDepth": 1,
    "isSeeded": False,
    "agentPreset": "dafish-team",
    "parentSession": "session-b6e726dd-9d2a-4707-bec1-6b7a15c6a154",
    "origin": "subagent",
}

ROOT_CREATED_ISO = "2026-11-25T10:48:47Z"       # 1795603727139ms → UTC
SUBAGENT_CREATED_ISO = "2026-11-25T19:43:45Z"   # 1795635825041ms → UTC


def frame_bytes(events) -> bytes:
    """把事件压成一个 zstd 帧（真实 DSH 文件的首帧就是多行 JSONL 的一个帧）。"""
    body = "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)
    return zstandard.ZstdCompressor().compress(body.encode("utf-8"))


def assert_subagent_contract(case: unittest.TestCase, header: dict) -> None:
    """子代理头的**正判据**（变异测试也调这一份：两份拷贝会各漂各的）。"""
    case.assertEqual(header["session_id"], REAL_SUBAGENT_HEADER["id"])
    case.assertEqual(header["parent_session"], REAL_SUBAGENT_HEADER["parentSession"],
                     "parentSession 必须归一为 parent_session")
    case.assertEqual(header["delegation_depth"], 1)
    case.assertIs(header["is_seeded"], False)
    case.assertEqual(header["agent_preset"], "dafish-team")
    verdict = dsh.classify(header, known_ids={REAL_SUBAGENT_HEADER["id"],
                                               REAL_SUBAGENT_HEADER["parentSession"]})
    case.assertEqual(verdict["origin"], "subagent")
    case.assertEqual(verdict["relation"], "spawn")
    case.assertEqual(verdict["basis"], "header")
    case.assertEqual(verdict["rule"], "subagent")


class DshSessionHeaderTest(unittest.TestCase):
    """真实 2026-11-26 会话头 + 变异自证。"""

    def setUp(self):
        if zstandard is None:                       # pragma: no cover
            self.skipTest("缺 zstandard 依赖：DSH 会话头无从构造")
        self._tmp = tempfile.mkdtemp(prefix="fish-dsh-header-")
        self.frames = Path(self._tmp) / "sessions"
        self.frames.mkdir()

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def write_frame(self, event: dict, *, dir_name: str | None = None) -> Path:
        session_id = event.get("id") or event.get("sessionId") or "sample"
        path = Path(self.frames) / "--ws--" / (dir_name or session_id) / dsh.FRAME_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(frame_bytes([event]))
        return path

    # ---------------------------------------------------------------- 1
    def test_01_real_root_header_normalized(self):
        """真实根会话头：camelCase → snake_case，createdAt 转 ISO。"""
        path = self.write_frame(REAL_ROOT_HEADER)
        header = dsh.read_header(path)
        self.assertEqual(header["session_id"], REAL_ROOT_HEADER["id"])
        self.assertIsNone(header["parent_session"])
        self.assertEqual(header["delegation_depth"], 0)
        self.assertIs(header["is_seeded"], True)
        self.assertEqual(header["agent_preset"], "dafish-team")
        self.assertEqual(header["cwd"], REAL_ROOT_HEADER["cwd"])
        self.assertEqual(header["created_at"], ROOT_CREATED_ISO)
        self.assertEqual(header["version"], 4)
        self.assertNotIn("path_id_mismatch", header)

        verdict = dsh.classify(header)
        self.assertEqual((verdict["origin"], verdict["relation"], verdict["basis"],
                          verdict["rule"]), ("main", None, "header", "seeded-root"))

    # ---------------------------------------------------------------- 2
    def test_02_real_subagent_header_normalized_and_classified(self):
        """真实子代理头：parentSession 归一 + classify 判 subagent 且父正确。"""
        path = self.write_frame(REAL_SUBAGENT_HEADER)
        header = dsh.read_header(path)
        self.assertEqual(header["created_at"], SUBAGENT_CREATED_ISO)
        assert_subagent_contract(self, header)

    # ---------------------------------------------------------------- 3
    def test_03_mutation_parentSession_renamed_must_turn_red(self):
        """变异（**样本级**）：样本 dict 的 `parentSession` → `parent_session` → 正判据红。

        这不是"适配器有 bug"：适配器只认 camelCase 是**有意**的。本判据证明
        "输入字段名变了 → 父链读不到"。**源码级**变异（改 `header_from_event` 的
        提取表达式）在 `mutation_dsh_header.py`——两者互补：这里钉行为，那里钉实现。
        """
        mutated = copy.deepcopy(REAL_SUBAGENT_HEADER)
        mutated["parent_session"] = mutated.pop("parentSession")
        path = self.write_frame(mutated)
        header = dsh.read_header(path)
        self.assertIsNone(header["parent_session"],
                          "适配器只认 camelCase；改字段名后应当读不到父（变异生效）")
        with self.assertRaises(AssertionError):
            assert_subagent_contract(self, header)

    # ---------------------------------------------------------------- 4
    def test_04_header_from_event_accepts_legacy_session_id(self):
        """旧写法 `sessionId` 仍被 `header_from_event` 接受（兼容判据）。"""
        legacy = {"type": "session", "version": 3, "sessionId": "legacy-session-0001",
                  "createdAt": 1795603727139, "cwd": r"I:\app\DSH-Home\workspace\fish-工作区",
                  "delegationDepth": 0, "isSeeded": False, "agentPreset": "dafish-team"}
        path = self.write_frame(legacy, dir_name="legacy-dir")
        header = dsh.read_header(path)
        self.assertEqual(header["session_id"], "legacy-session-0001")
        self.assertEqual(header["path_session_id"], "legacy-dir")
        self.assertEqual(header["path_id_mismatch"], 1)

        direct = dsh.header_from_event(legacy, path)
        self.assertEqual(direct["session_id"], "legacy-session-0001")
        self.assertNotIn("id", direct)

    # ---------------------------------------------------------------- 5
    def test_05_real_samples_are_camel_case(self):
        """样本本身钉死 camelCase 形状：防止有人把 fixture 改成 snake_case 让判据失真。"""
        camel = ("id", "createdAt", "cwd", "delegationDepth", "isSeeded", "agentPreset",
                 "version")
        snake = ("session_id", "created_at", "delegation_depth", "is_seeded", "agent_preset")
        for sample in (REAL_ROOT_HEADER, REAL_SUBAGENT_HEADER):
            for key in camel:
                self.assertIn(key, sample)
            for key in snake:
                self.assertNotIn(key, sample)
        self.assertIn("parentSession", REAL_SUBAGENT_HEADER)
        self.assertIn("origin", REAL_SUBAGENT_HEADER)


if __name__ == "__main__":
    unittest.main(verbosity=2)
