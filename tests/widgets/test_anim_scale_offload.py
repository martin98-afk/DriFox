# -*- coding: utf-8 -*-
"""O1 动画预缩放下放解码线程 回归测试。

背景：动画帧的 cover/contain 缩放缓存 key 含 id(img)，每帧必 miss，
15fps 下每帧一次全幅缩放（2K 目标实测 3.5ms/帧）。O1 把缩放挪进解码
线程：SceneLayer 登记几何（register_geometry）→ 解码线程持快照预缩放 →
成品经 on_animation_frame 下发（view._pending_scaled）→ paint 短路直取；
未命中一律走同步兜底（原逻辑一字不动）。

验证（_AnimRenderMixin 纯逻辑驱动，不建真 widget，仿
test_tray_hotkey_health_check.py 的最小状态模式）：
1. 快照空（_pending_scaled=None）→ 同步兜底，产出正确尺寸成品
2. 几何过期（pending 缺当前几何 key）→ 同步兜底，不误用 stale 成品
3. tile → 原帧直绘（现状语义不变）
4. pending 命中 → 直取成品，不触发同步缩放（_scaled 缓存保持未写）
5. _scale_frame_for cover 裁切数学与同步路径逐像素同源
6. 几何注册表 register/unregister 往返 + 未知 key 静默
7. 回退开关 _scale_off：True 时快照恒空（整帧走旧路径的前提）
"""

import collections
import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QRect, Qt
from PyQt5.QtGui import QColor, QImage
from PyQt5.QtWidgets import QApplication

from app.widgets.anim_background import AnimationSource, _scale_frame_for
from app.widgets.scene_layer import _AnimRenderMixin

@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app

def make_frame(w=160, h=90) -> QImage:
    img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
    img.fill(QColor(10, 20, 30))
    return img

class _Probe(_AnimRenderMixin):
    """最小渲染探针：update 置空、rect 给固定目标尺寸（paint 手动驱动）。"""

    def __init__(self, fit, position="center", target=(64, 48)):
        self._init_render_state()
        self.update = lambda *a, **k: None
        self._fit = fit
        self._position = position
        self._target = QRect(0, 0, target[0], target[1])

    def rect(self):
        return self._target

def _make_source() -> AnimationSource:
    """最小状态集的 AnimationSource（跳过重 __init__，只喂注册表依赖）。"""
    src = AnimationSource.__new__(AnimationSource)
    src._geo_lock = threading.Lock()
    src._geometries = {}
    src._scale_off = False
    src._prefetch_lock = threading.Lock()
    src._prefetch = collections.deque()
    src._prefetch_space = threading.Condition(src._prefetch_lock)
    src._prefetch_stop = threading.Event()
    src.failed = False
    return src

def test_empty_snapshot_falls_back(qapp):
    """分支① 快照空 → 同步兜底：cover 成品尺寸 = 目标尺寸"""
    p = _Probe("cover")
    p.set_frame(make_frame())
    out = p._scaled_frame(p._target)
    assert out is not None
    assert (out.width(), out.height()) == (64, 48)
    assert p._scaled_key is not None, "兜底路径应写入同步缓存"

def test_stale_geometry_falls_back(qapp):
    """分支② 几何过期（pending 缺当前几何 key）→ 同步兜底，不误用 stale"""
    p = _Probe("cover")
    p.set_frame(make_frame())
    stale = {(100, 100, 32, 32, "cover", "center"): make_frame(32, 32)}
    p._pending_scaled = stale
    out = p._scaled_frame(p._target)
    assert out is not None
    assert (out.width(), out.height()) == (64, 48)
    assert out.width() != 32, "不得返回过期几何的成品"

def test_tile_returns_original(qapp):
    """分支③ tile → 原帧直绘（不缩放，现状语义不变）"""
    p = _Probe("tile")
    img = make_frame()
    p.set_frame(img)
    assert p._scaled_frame(p._target) is img

def test_pending_hit_short_circuits(qapp):
    """正路径 pending 命中 → 直取成品，同步缩放未发生（_scaled 未写）"""
    p = _Probe("cover")
    img = make_frame()
    p.set_frame(img)
    product = make_frame(64, 48)
    p._pending_scaled = {
        (img.width(), img.height(), 64, 48, "cover", "center"): product
    }
    out = p._scaled_frame(p._target)
    assert out is product
    assert p._scaled is None, "命中路径不得再走同步缩放"

def test_pending_null_frame_falls_back(qapp):
    """兜底条件④ pending 项 isNull → 落同步兜底"""
    p = _Probe("cover")
    img = make_frame()
    p.set_frame(img)
    p._pending_scaled = {
        (img.width(), img.height(), 64, 48, "cover", "center"): QImage()
    }
    out = p._scaled_frame(p._target)
    assert out is not None and (out.width(), out.height()) == (64, 48)

def test_scale_frame_for_cover_crop_math(qapp):
    """_scale_frame_for cover 先裁后缩：1600x900 → 2560x720 裁中段 450 高"""
    img = make_frame(1600, 900)
    out = _scale_frame_for(img, 2560, 720, "cover", "center")
    assert out is not None
    assert (out.width(), out.height()) == (2560, 720)
    # 同步路径同参数裁切矩形：scale=max(1.6,0.8)=1.6 → 可见段 1600x450，sy=225
    expect = img.copy(QRect(0, 225, 1600, 450)).scaled(
        2560, 720, Qt.IgnoreAspectRatio, Qt.SmoothTransformation
    )
    assert expect == out, "线程预缩放输出须与同步裁切+缩放逐像素一致"

def test_geometry_registry_roundtrip(qapp):
    """注册表 register/unregister 往返；未知 key 静默；幂等覆盖"""
    src = _make_source()
    src.register_geometry(101, (64, 48, "cover", "center"))
    src.register_geometry(102, (32, 32, "contain", "top"))
    with src._geo_lock:
        assert dict(src._geometries) == {
            101: (64, 48, "cover", "center"),
            102: (32, 32, "contain", "top"),
        }
    src.register_geometry(101, (80, 60, "cover", "bottom"))  # 幂等覆盖
    src.unregister_geometry(101)
    src.unregister_geometry(999)  # 不存在 → 静默
    with src._geo_lock:
        assert src._geometries == {102: (32, 32, "contain", "top")}

def test_scale_off_flag_semantics(qapp):
    """回退开关 _scale_off：置 True 后语义为快照恒空（loop 内旁路的前提）"""
    src = _make_source()
    src.register_geometry(101, (64, 48, "cover", "center"))
    src._scale_off = True
    # 仿 _prefetch_loop 的取快照段：开关命中 → 快照空
    with src._geo_lock:
        snapshot = {} if src._scale_off else dict(src._geometries)
    assert snapshot == {}, "_scale_off=True 必须产出空快照（兜底恒命中）"
    src._scale_off = False
    with src._geo_lock:
        snapshot = {} if src._scale_off else dict(src._geometries)
    assert snapshot == {101: (64, 48, "cover", "center")}
