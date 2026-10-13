# -*- coding: utf-8 -*-
"""回归：CodeWebViewer 高度上限按 DPR 钳制，防 GPU 上下文丢失。

症状（2026-09-12）：
    GLES2DecoderImpl::ResizeOffscreenFramebuffer failed to allocate storage
    due to excessive dimensions. → Context lost → MakeCurrent failed。

根因：MAX_HEIGHT（当时 10000）是逻辑像素，Chromium 离屏表面按「逻辑 × DPR」
分配物理纹理，D3D11/WARP 单纹理硬上限 16384px。225% 缩放（DPR 2.25）
下撞顶即 22500 物理 > 16384 → 分配失败。

修复：逻辑上限 = min(MAX_HEIGHT, floor(16000 / DPR))，超限内容走内滚安全网。
现行 MAX_HEIGHT=3000（2026-10-13 用户要求下调，卡内滚动可靠性已由
wheel delegate 抢断修复兜住），纹理钳制在 DPR ≤ 5.3 时不再先咬，保留作兜底。
"""

from app.widgets.card_viewers import CodeWebViewer
from app.widgets.message_card import (
    _PHYSICAL_TEXTURE_LIMIT,
    _logical_height_cap,
    _logical_height_cap_for_area,
)


def test_dpr_1_keeps_class_limit():
    """100% 缩放：纯函数给物理安全值 16000，调用点 min 类上限后仍是类上限。"""
    assert _logical_height_cap(1.0) == 16000
    assert min(CodeWebViewer.MAX_HEIGHT, _logical_height_cap(1.25)) == CodeWebViewer.MAX_HEIGHT


def test_dpr_225_shrinks_below_texture_limit():
    """225% 缩放（black 本机）：上限收缩到 7111，物理尺寸不超纹理上限。"""
    cap = _logical_height_cap(2.25)
    assert cap == 7111
    # 核心不变项：上限 × DPR 不得超物理纹理限制
    assert cap * 2.25 <= _PHYSICAL_TEXTURE_LIMIT


def test_extreme_dpr_floors_at_2000():
    """极端高 DPR 保底 2000，保底本身也不超物理上限。"""
    cap = _logical_height_cap(8.0)
    assert cap == 2000
    assert cap * 8.0 <= _PHYSICAL_TEXTURE_LIMIT


def test_abnormal_dpr_treated_as_1():
    """异常 DPR（0/负数/None）按 1.0 处理。"""
    assert _logical_height_cap(0) == 16000
    assert _logical_height_cap(-1.5) == 16000
    assert _logical_height_cap(None) == 16000


# ── 面积上限（2026-10-11「独显/高分屏内存暴涨」排查） ──
# 单边高度上限挡不住内存：成本 ∝ 宽×高×dpr²（实测 9.2MB/百万物理像素），
# 旧上限 10000 时 dpr=2.25 + 1800 宽 = 4050×22500 物理 ≈ 91Mpx ≈ 840MB 单卡。
# 故再按物理面积封顶（CodeWebViewer.MAX_SURFACE_MPX，默认 12Mpx）。
# 现行 MAX_HEIGHT=3000 后面积上限仅在 DPR ≥ 2.4 先咬（见 card_viewers 注释）。


def test_area_cap_bites_at_dpr_225():
    """225% 缩放 + 700 逻辑宽：面积换算上限 ~3386 逻辑（>3000 → 主闸仍是 MAX_HEIGHT）。

    纯函数结果与类常量解耦：此处只验证换算公式与不变式；生效值由
    ``min(MAX_HEIGHT, 面积上限, 纹理上限)`` 在调用点取得（见 case 文件头注释）。
    """
    cap = _logical_height_cap_for_area(700, 2.25, CodeWebViewer.MAX_SURFACE_MPX)
    assert cap == 3386
    # 不变式：逻辑宽×高×dpr² ≤ 面积上限
    assert (700 * cap) * (2.25**2) <= CodeWebViewer.MAX_SURFACE_MPX * 1_000_000
    # 说明性约束（非断言类常量取值）：新 MAX_HEIGHT=3000 下面积上限不再先咬
    assert cap > CodeWebViewer.MAX_HEIGHT


def test_area_cap_inactive_at_dpr_1():
    """100% 缩放：换算高度远超 MAX_HEIGHT → 面积上限对老机器等于不生效。"""
    assert _logical_height_cap_for_area(700, 1.0, 12.0) == 17142
    assert _logical_height_cap_for_area(700, 1.0, 12.0) > CodeWebViewer.MAX_HEIGHT


def test_area_cap_halves_when_width_doubles():
    assert _logical_height_cap_for_area(1400, 2.25, 12.0) == 1693
    assert _logical_height_cap_for_area(700, 2.25, 12.0) == 3386


def test_area_cap_abnormal_inputs():
    """异常入参不炸、不留 0 高：dpr 按 1.0，宽/面积非法时保底 200。"""
    assert _logical_height_cap_for_area(700, 0, 12.0) == 17142
    assert _logical_height_cap_for_area(700, None, 12.0) == 17142
    assert _logical_height_cap_for_area(0, 2.25, 12.0) == 200
    assert _logical_height_cap_for_area(None, 2.25, 12.0) == 200
    assert _logical_height_cap_for_area(700, 2.25, 0) == 200
