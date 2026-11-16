# -*- coding: utf-8 -*-
"""候选 1 主题检测链缓存化的实现侧单测（子任务 #29-③）。

golden（tests/golden/golden_render_equiv.py）对此改动有盲区：产品高亮 HTML
自 2026-10-30 noclasses=False 后与主题无关，golden 两态 sha 天然相同，
缓存失效钩子漏挂在产物层测不出。本文件补实现侧行为断言：
    1. 同版本号重复渲染不重复执行全局刷新（缓存命中）；
    2. ThemeRefreshCoordinator.get_version() bump 后下一次渲染重刷（失效生效）；
    3. refresh_font 实例路径强制置失效（双保险钩子）；
    4. version 探测异常时保守退化为每帧重刷（等价旧行为）。

纯 Python 断言，不实例化 QWebEngineView（offscreen 实例化必崩，见
tests/perf/test_stream_render_bench.py 头注）；_ensure_theme_chain 不依赖
实例状态，用哑实例绑定调用。
"""

import pytest

from app.widgets import card_viewers as cv
from app.widgets.card_viewers import CodeWebViewer


class _Stub:
    """哑宿主：_ensure_theme_chain 不读实例字段，任意对象即可绑定调用。"""


@pytest.fixture()
def chain_state():
    """保存/恢复类级签名，避免污染其他测试。"""
    saved = CodeWebViewer._theme_chain_ver
    CodeWebViewer._theme_chain_ver = None
    yield
    CodeWebViewer._theme_chain_ver = saved


@pytest.fixture()
def counters(monkeypatch):
    """统计四个全局刷新函数的调用次数。"""
    calls = {"pygments": 0, "diff": 0, "icon": 0, "font": 0}

    def _spy(name):
        def _f(*a, **kw):
            calls[name] += 1

        return _f

    monkeypatch.setattr(cv, "set_pygments_style", _spy("pygments"))
    # set_diff_highlight_style 在 _ensure_theme_chain 内函数级 import，需 patch 其源模块
    from app.widgets import render_helpers

    monkeypatch.setattr(render_helpers, "set_diff_highlight_style", _spy("diff"))
    monkeypatch.setattr(cv, "_update_icon_prefix", _spy("icon"))
    monkeypatch.setattr(cv, "_refresh_code_font_size", _spy("font"))
    return calls


def _patch_version(monkeypatch, value):
    from app.utils.theme_refresh import ThemeRefreshCoordinator

    monkeypatch.setattr(
        ThemeRefreshCoordinator, "get_version", staticmethod(lambda: value)
    )


def test_same_version_no_refresh(chain_state, counters, monkeypatch):
    """同版本号多次调用只刷一次（缓存命中）。"""
    _patch_version(monkeypatch, 1)
    CodeWebViewer._ensure_theme_chain(_Stub())
    CodeWebViewer._ensure_theme_chain(_Stub())
    CodeWebViewer._ensure_theme_chain(_Stub())
    assert counters["pygments"] == 1
    assert counters["diff"] == 1
    assert counters["icon"] == 1
    assert counters["font"] == 1


def test_version_bump_refreshes(chain_state, counters, monkeypatch):
    """版本号 bump 后下一次渲染重刷（失效钩子生效）。"""
    _patch_version(monkeypatch, 1)
    CodeWebViewer._ensure_theme_chain(_Stub())
    assert counters["pygments"] == 1
    _patch_version(monkeypatch, 2)
    CodeWebViewer._ensure_theme_chain(_Stub())
    assert counters["pygments"] == 2
    assert counters["font"] == 2


def test_refresh_font_invalidates(chain_state, counters, monkeypatch):
    """refresh_font 实例路径强制置失效：下次渲染重刷。"""
    _patch_version(monkeypatch, 1)
    CodeWebViewer._ensure_theme_chain(_Stub())
    assert counters["pygments"] == 1
    # 不 bump version，只触发字体刷新路径的失效钩子
    CodeWebViewer._theme_chain_ver = None
    CodeWebViewer._ensure_theme_chain(_Stub())
    assert counters["pygments"] == 2


def test_version_probe_error_rebuilds_each_time(chain_state, counters, monkeypatch):
    """version 探测异常 → 保守退化：每次调用都重刷（等价旧版每帧跑）。"""
    from app.utils.theme_refresh import ThemeRefreshCoordinator

    def _boom():
        raise RuntimeError("probe unavailable")

    monkeypatch.setattr(ThemeRefreshCoordinator, "get_version", staticmethod(_boom))
    CodeWebViewer._ensure_theme_chain(_Stub())
    CodeWebViewer._ensure_theme_chain(_Stub())
    assert counters["pygments"] == 2
    assert CodeWebViewer._theme_chain_ver is None
