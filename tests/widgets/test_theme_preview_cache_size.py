# -*- coding: utf-8 -*-
"""主题预览缓存尺寸公式化回归（#23：320×120 上限假设不成立的画质修复）

== 背景（ff17da1a0 画质回归）==
预览缓存按 primaryScreen dpr × 320×120 生成，但：
- _relayout 允许 2~4 列，4 列宽弹窗单格逻辑宽随弹窗宽增长（可达 ~400+ > 320）
- dpr 硬编码 primaryScreen，副屏 dpr 更高时整屏预览小图放大模糊
（_draw_cover 按 tile 所在屏 dpr 再缩放，缓存不足 = 小图放大）

== 修复 ==
- 缓存尺寸公式化 `_preview_cache_target_size(dpr)`：480×140 逻辑上界（400 最坏
  ×1.2 余量）× dpr，禁回全分辨率无界缓存
- dpr 取全部屏幕最大值（`_max_screen_dpr`），任意屏 tile 都有足够分辨率
- 内存上限公式注释在案（最坏条目 ≈2.42MB @dpr3，总量有界）
"""

import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt5.QtGui")

from PyQt5.QtCore import QSize  # noqa: E402
from PyQt5.QtGui import QGuiApplication  # noqa: E402

from app.widgets.cards.settings.theme_style_card import (  # noqa: E402
    _preview_cache_target_size,
    _max_screen_dpr,
    _PREVIEW_CACHE_TILE_W,
    _PREVIEW_CACHE_TILE_H,
)


class TestPreviewCacheTargetSize:
    """缓存尺寸公式：逻辑上界 × dpr（round 对齐像素格）"""

    def test_dpr1_baseline(self):
        assert _preview_cache_target_size(1.0) == QSize(_PREVIEW_CACHE_TILE_W, _PREVIEW_CACHE_TILE_H)
        assert _PREVIEW_CACHE_TILE_W == 480 and _PREVIEW_CACHE_TILE_H == 140

    def test_dpr2_doubles(self):
        assert _preview_cache_target_size(2.0) == QSize(960, 280)

    def test_dpr3_worst_entry(self):
        size = _preview_cache_target_size(3.0)
        assert size == QSize(1440, 420)
        # 内存上限公式（注释同源）：最坏条目 ≈2.42MB 有界
        bytes_worst = size.width() * size.height() * 4
        assert bytes_worst <= 2.6 * 1024 * 1024, f"最坏条目超界: {bytes_worst} 字节"

    def test_fractional_dpr_rounds(self):
        # 1.25 → 600×175（round(480*1.25)=600, round(140*1.25)=175）
        assert _preview_cache_target_size(1.25) == QSize(600, 175)

    def test_sub_one_dpr_clamped_to_1(self):
        """dpr 下限 1.0：离屏/无屏环境不得生成亚像素缓存"""
        assert _preview_cache_target_size(0.5) == QSize(_PREVIEW_CACHE_TILE_W, _PREVIEW_CACHE_TILE_H)

    def test_cache_stays_bounded_not_full_resolution(self):
        """缓存保持有界（禁回全分辨率）：dpr=2 时宽不得超过 逻辑上界×2×1.01 容差"""
        size = _preview_cache_target_size(2.0)
        assert size.width() <= int(_PREVIEW_CACHE_TILE_W * 2 * 1.01)
        assert size.height() <= int(_PREVIEW_CACHE_TILE_H * 2 * 1.01)


class TestMaxScreenDpr:
    def test_multi_screen_takes_max(self, monkeypatch):
        """多屏取最大 dpr（副屏高 dpr 场景，修复 primaryScreen 硬编码）"""
        screens = [
            SimpleNamespace(devicePixelRatio=lambda: 1.0),
            SimpleNamespace(devicePixelRatio=lambda: 2.0),
            SimpleNamespace(devicePixelRatio=lambda: 1.5),
        ]
        monkeypatch.setattr(QGuiApplication, "screens", staticmethod(lambda: screens))
        assert _max_screen_dpr() == 2.0

    def test_no_screens_falls_back_to_1(self, monkeypatch):
        """离屏/无屏环境回退 1.0（不得崩）"""
        monkeypatch.setattr(QGuiApplication, "screens", staticmethod(lambda: []))
        assert _max_screen_dpr() == 1.0

    def test_real_env_at_least_1(self):
        assert _max_screen_dpr() >= 1.0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
