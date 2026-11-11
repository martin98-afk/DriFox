# -*- coding: utf-8 -*-
"""[A3-3] 池容量实验开关：DRIFOX_POOL_MAX_IDLE 参数化验证（#23）。

- 默认（不设 env）= 2，与 fd440524f 行为逐字节等价
- 显式值生效；非法/缺失/<1 回退 2
- 扩容后超额仍按「最老出池」淘汰（pop(0) 顺序语义不变）
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtWidgets import QWidget

from app.widgets import webview_pool as wvp


class _FakeViewer(QWidget):
    """够过 _is_usable/_ensure_host/release 链的最小 viewer 替身"""

    _renderer_pid = 0
    _context_lost = False

    def reset_for_reuse(self):
        pass

    def page(self):
        return object()  # 非 None 即可


def test_default_capacity_unchanged(monkeypatch):
    """不设 env → 上限 2（默认行为与 fd440524f 一致）；非法/缺失/<1 同样回退 2"""
    monkeypatch.delenv("DRIFOX_POOL_MAX_IDLE", raising=False)
    assert wvp._load_max_idle_per_bucket() == 2
    for bad in ("", "abc", "0", "-3"):
        monkeypatch.setenv("DRIFOX_POOL_MAX_IDLE", bad)
        assert wvp._load_max_idle_per_bucket() == 2, f"非法值 {bad!r} 未回退"


def test_env_override_and_oldest_eviction(monkeypatch):
    """env=4 生效；扩容后第 5 个入池仍淘汰最老（顺序语义不变）"""
    monkeypatch.setenv("DRIFOX_POOL_MAX_IDLE", "4")
    assert wvp._load_max_idle_per_bucket() == 4

    monkeypatch.setattr(wvp, "MAX_IDLE_PER_BUCKET", 4)
    pool = wvp.WebViewPool()
    viewers = [_FakeViewer() for _ in range(5)]
    for v in viewers[:4]:
        assert pool.release(v) is True
    assert pool.size() == 4
    # 全部仍在池（未超额）
    for v in viewers[:4]:
        assert v in pool._idle[False]
    # 第 5 个入池 → 最老（第一个）被淘汰出池
    assert pool.release(viewers[4]) is True
    assert pool.size() == 4
    assert viewers[0] not in pool._idle[False]
    for v in viewers[1:]:
        assert v in pool._idle[False]


def test_default_two_bucket_behaviour(monkeypatch):
    """默认 2：第 3 个入池淘汰最老（现状回归基线）"""
    monkeypatch.delenv("DRIFOX_POOL_MAX_IDLE", raising=False)
    monkeypatch.setattr(wvp, "MAX_IDLE_PER_BUCKET", 2)
    pool = wvp.WebViewPool()
    viewers = [_FakeViewer() for _ in range(3)]
    for v in viewers[:2]:
        assert pool.release(v) is True
    assert pool.size() == 2
    assert pool.release(viewers[2]) is True
    assert pool.size() == 2
    assert viewers[0] not in pool._idle[False]
    assert viewers[1] in pool._idle[False]
