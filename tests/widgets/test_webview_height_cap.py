# -*- coding: utf-8 -*-
"""回归：CodeWebViewer 高度上限按 DPR 钳制，防 GPU 上下文丢失。

症状（2026-09-12）：
    GLES2DecoderImpl::ResizeOffscreenFramebuffer failed to allocate storage
    due to excessive dimensions. → Context lost → MakeCurrent failed。

根因：MAX_HEIGHT=10000 是逻辑像素，Chromium 离屏表面按「逻辑 × DPR」
分配物理纹理，D3D11/WARP 单纹理硬上限 16384px。225% 缩放（DPR 2.25）
下撞顶即 22500 物理 > 16384 → 分配失败。

修复：逻辑上限 = min(10000, floor(16000 / DPR))，超限内容走内滚安全网。
"""

from app.widgets.message_card import _PHYSICAL_TEXTURE_LIMIT, _logical_height_cap


def test_dpr_1_keeps_class_limit():
    """100% 缩放：纯函数给物理安全值 16000，调用点 min 类上限后仍是 10000。"""
    assert _logical_height_cap(1.0) == 16000
    assert min(10000, _logical_height_cap(1.25)) == 10000


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
