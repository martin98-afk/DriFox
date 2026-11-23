# -*- coding: utf-8 -*-
"""#R1 回归：@ 卡会话搜索回落路径钳幅（消 get_all(2000) 大库整读敞口）。

背景：_fetch_session_items 主路径走 get_sessions_lightweight（零 blob 反序列
化）；回落路径原为 `get_all(limit=_SESSION_SEARCH_LIMIT)`（2000），在「实现
get_all 却无 light 委托」的引擎 + 大库形态下 = 2000 条 SELECT * 全量 zstd 解压
= GB 级瞬时峰值（单会话解压峰值实测 26.9MB）。修复：回落钳幅 200 条 + 警示。

覆盖（CTO 点名「2000 会话场景不触发全量回落」，spy 引擎调用计数）：
① 主路径：light 可用 → get_all 零调用（spy=0）；
② 回落路径：light 返回 None → get_all 被调用但 limit 钳到 200（非 2000）；
③ 源码守卫：钳幅表达式存在，防回归删除。
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PyQt5.QtWidgets", reason="需要 PyQt5 运行时")


_APP = Path(__file__).resolve().parents[2] / "app" / "widgets" / "cards" / "floating" / "file_mention_card.py"


def _fake_sessions(n: int) -> list:
    """n 条最小会话 dict（_fetch_session_items 映射循环所需字段）。"""
    return [
        {
            "session_id": f"s{i}",
            "title": f"T{i}",
            "project": "P",
            "preview": "",
            "pinned": 0,
        }
        for i in range(n)
    ]


class _SpyEngine:
    """会话存储引擎替身：light/get_all 调用计数（spy）。"""

    id = "spy"

    def __init__(self, light_result, n_sessions: int = 2000):
        self._light_result = light_result
        self.light_calls = 0
        self.get_all_calls: list = []
        self._sessions = _fake_sessions(n_sessions)

    def get_sessions_lightweight(self, limit: int = 100, offset: int = 0, project=None):
        self.light_calls += 1
        if callable(self._light_result) and not isinstance(self._light_result, list):
            return self._light_result(limit=limit, offset=offset, project=project)
        return self._light_result

    def get_all(self, limit: int = 100, offset: int = 0):
        self.get_all_calls.append(limit)
        return self._sessions[:limit]


def _make_card():
    """最小 FileMentionCard（__new__ 绕过 UI 构造，只设 _fetch_session_items 依赖）。"""
    from app.widgets.cards.floating.file_mention_card import FileMentionCard

    card = FileMentionCard.__new__(FileMentionCard)
    card._SESSION_SEARCH_LIMIT = 2000
    card._session_project = ""
    return card


def _patch_storage(monkeypatch, engine):
    from app.core.conversation import backend as backend_mod

    monkeypatch.setattr(backend_mod, "get_session_storage", lambda: engine)


def test_light_hit_does_not_call_get_all(monkeypatch):
    """① 主路径（2000 会话库）：light 可用 → get_all 零调用（不触发全量回落）。"""
    engine = _SpyEngine(light_result=None or None)  # 占位，下方覆盖
    engine._light_result = _fake_sessions(2000)
    _patch_storage(monkeypatch, engine)

    card = _make_card()
    items = card._fetch_session_items("")
    assert engine.light_calls == 1
    assert engine.get_all_calls == [], "light 可用时绝不得回落 get_all（2000 条整读敞口）"
    assert len(items) == 2000


def test_fallback_capped_at_200_not_full_limit(monkeypatch):
    """② 回落路径（light=None）：get_all 被调用但 limit 钳到 200，非 2000 全量。"""
    engine = _SpyEngine(light_result=None)
    _patch_storage(monkeypatch, engine)

    card = _make_card()
    card._fetch_session_items("")
    assert engine.light_calls == 1
    assert len(engine.get_all_calls) == 1
    assert engine.get_all_calls[0] == 200, (
        f"回落 limit 必须钳到 200（防 2000 条全量解压 GB 峰值），实际 {engine.get_all_calls[0]}"
    )


def test_fallback_cap_expression_in_source():
    """③ 源码守卫：钳幅表达式存在（min(...,200)），防回归删除。"""
    src = _APP.read_text(encoding="utf-8")
    assert "min(self._SESSION_SEARCH_LIMIT, 200)" in src, (
        "回落路径必须保留钳幅（get_all 全量 2000 条 = 大库 GB 级解压敞口）"
    )
    assert "storage.get_all(limit=_fallback_limit)" in src
