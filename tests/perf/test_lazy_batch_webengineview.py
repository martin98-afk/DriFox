# -*- coding: utf-8 -*-
"""
性能回归测试（#1 性能瓶颈报告 Top②）：WebEngineView 懒加载分批渲染 + Chromium 实例上限回收

(a) 对应瓶颈：历史消息中的 Markdown WebEngineView 一次性全部实例化，Chromium 实例过多
    导致内存与启动开销大。第一批修复改为懒加载分批（_process_next_lazy_batch），并设
    Chromium 实例上限（_max_rendered_cards）+ LRU 回收（_recycle_lru_batches）。

(b) 本测试未修改任何业务代码，仅静态分析：用 pathlib 读取 app/main_widget.py 源码文本
    + re 匹配，不 import PyQt5、不实例化任何 GUI 对象。

(c) 环境要求：pytest>=7 / Python3 / 对 app/ 源码有读权限 / 无需显示器 /
    无新三方依赖 / 跨平台 Windows 优先。
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "app" / "main_widget.py"


@pytest.fixture(scope="module")
def src_text() -> str:
    return SRC.read_text(encoding="utf-8")


def test_static_lazy_batch_present(src_text: str):
    """静态扫描：确认懒加载分批渲染机制存在。"""
    assert "def _process_next_lazy_batch" in src_text
    assert "ensure_rendered()" in src_text
    assert "QTimer.singleShot(80, self._process_next_lazy_batch)" in src_text


def test_perf_rendered_cards_bounded(src_text: str):
    """性能/回归断言：Chromium 实例上限常量存在，且 LRU 回收受上限约束。"""
    assert "_max_rendered_cards" in src_text
    assert "_recycle_lru_batches" in src_text
    assert "self._rendered_card_count > self._max_rendered_cards" in src_text
