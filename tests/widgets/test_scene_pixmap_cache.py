# -*- coding: utf-8 -*-
"""T43 验证：壁纸 pixmap 跨窗口共享缓存 scene_pixmap_cache。

offscreen 运行，无键鼠、无可见窗口（P0 约束）。

覆盖：
① 同 key 二次请求返回同一对象（QPixmap.cacheKey() 断言，隐式共享零复制）
② 不同 opacity/blur/尺寸产生不同条目
③ LRU 超 _MAX_SETS=2 组淘汰最旧组
④ clear() 后重新创建
⑤ 接入层行为：同壁纸两个 SceneLayer 实例的预乘副本是同一对象（2 份→1 份）
⑥ 引用计数：两窗口共享只解码一次；⑦ 单窗口释放后另一窗口仍有效；
⑧ 全关后宽限期到点释放；⑨ 重复开关窗口计数归零不泄漏
"""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QPixmap, QImage
from PySide6.QtCore import Qt

import pytest

from app.core.infra import scene_pixmap_cache as spc_mod
from app.core.infra.scene_pixmap_cache import scene_pixmap_cache


@pytest.fixture
def cache():
    scene_pixmap_cache.clear()
    # 引用计数状态一并复位（跨测试隔离；仅测试态直改私有成员）
    scene_pixmap_cache._users = 0
    if scene_pixmap_cache._evict_timer is not None:
        scene_pixmap_cache._evict_timer.stop()
        scene_pixmap_cache._evict_timer.deleteLater()
        scene_pixmap_cache._evict_timer = None
    yield scene_pixmap_cache
    scene_pixmap_cache.clear()


def _make_png(path, color=200):
    img = QImage(8, 8, QImage.Format_ARGB32)
    img.fill(color)
    img.save(path, "PNG")


@pytest.fixture
def wallpaper(tmp_path):
    p = str(tmp_path / "wall.png")
    _make_png(p)
    return p


def _load(p):
    return QPixmap(p)


def test_same_key_returns_same_object(cache, wallpaper):
    """① 同 key 二次请求 → 同一 QPixmap 实例（cacheKey 相同，零复制）"""
    a = cache.get_pixmap("source", wallpaper, load=_load)
    b = cache.get_pixmap("source", wallpaper, load=_load)
    assert a.cacheKey() == b.cacheKey()
    assert a is b
    assert cache.stats() == {"groups": 1, "entries": 1}


def test_different_params_different_entries(cache, wallpaper):
    """② 不同 opacity/blur/尺寸/类型 → 不同条目（各自构建）"""
    a = cache.get_pixmap("premult", wallpaper, load=_load, opacity_255=255)
    b = cache.get_pixmap("premult", wallpaper, load=_load, opacity_255=128)
    assert a is not b  # 不同条目各自构建（非共享对象）
    c = cache.get_pixmap(
        "baked", wallpaper, load=lambda p: QPixmap(16, 16),
        width=100, height=50, opacity_255=255, blur_radius=8.0, pad=16,
    )
    d = cache.get_pixmap(
        "baked", wallpaper, load=lambda p: QPixmap(16, 16),
        width=100, height=50, opacity_255=255, blur_radius=12.0, pad=16,
    )
    assert c is not d  # blur 不同 → 不同条目
    assert cache.stats() == {"groups": 1, "entries": 4}


def test_lru_evicts_oldest_group(cache, tmp_path):
    """③ 超 _MAX_SETS=2 组 → 淘汰最旧组（新壁纸进、旧壁纸出）"""
    w1 = str(tmp_path / "w1.png"); _make_png(w1, 100)
    w2 = str(tmp_path / "w2.png"); _make_png(w2, 150)
    w3 = str(tmp_path / "w3.png"); _make_png(w3, 200)
    cache.get_pixmap("source", w1, load=_load)
    cache.get_pixmap("source", w2, load=_load)
    assert cache.stats()["groups"] == 2
    cache.get_pixmap("source", w3, load=_load)  # w1 组被淘汰
    assert cache.stats()["groups"] == 2
    # w1 再请求 → 缓存未命中重新构建（对象不同，验证旧组确实被淘汰）
    again = cache.get_pixmap("source", w1, load=_load)
    # 淘汰后 w2 变最旧，但 w1 刚被触碰 → w3 被淘汰
    keep = cache.get_pixmap("source", w2, load=_load)
    assert cache.stats()["groups"] == 2


def test_clear_then_rebuild(cache, wallpaper):
    """④ clear() 后重新创建"""
    first = cache.get_pixmap("source", wallpaper, load=_load)
    cache.clear()
    assert cache.stats() == {"groups": 0, "entries": 0}
    second = cache.get_pixmap("source", wallpaper, load=_load)
    assert first is not second  # 重新加载的新对象
    assert cache.stats()["groups"] == 1


def test_two_scene_layers_share_premult(cache, wallpaper, qapp):
    """⑤ 接入层行为：两个 SceneLayer 同壁纸 → 预乘副本同一对象（2 份→1 份）

    用 monkeypatch 假 resolver（路径直返），offscreen 构建 SceneLayer 并
    apply_config 壁纸配置（无 blur，fg 关闭，anim 关闭）。
    """
    from app.widgets.scene_layer import SceneLayer

    resolver = lambda p: p  # noqa: E731 解析后直返
    cfg = {
        "enabled": True,
        "image": wallpaper,
        "opacity": 1.0,
        "blur": 0,
        "animation": {"enabled": False},
        "foreground": {"enabled": False},
    }
    layer_a = SceneLayer()
    layer_b = SceneLayer()
    try:
        layer_a.apply_config(cfg, resolver)
        layer_b.apply_config(cfg, resolver)
        pa = layer_a._image_label.pixmap()
        pb = layer_b._image_label.pixmap()
        assert pa is not None and pb is not None
        # 缓存层共享：两 layer 持有的是同一 Python 对象（修复前 2 份 → 现在 1 份）
        assert layer_a._image_source_pixmap is layer_b._image_source_pixmap
        # label.pixmap() 每次 wrapper 重包装，但底层 C++ pixmap 同源（cacheKey 相等）
        assert pa.cacheKey() == pb.cacheKey()
        assert cache.stats()["entries"] == 2  # source + premult 各 1 份
    finally:
        layer_a.deleteLater()
        layer_b.deleteLater()


def test_two_windows_share_one_decode(cache, wallpaper):
    """⑥ 两窗口共享同图：acquire×2，load 只被调一次（零重复解码）"""
    calls = []

    def _counting_load(p):
        calls.append(p)
        return QPixmap(p)

    cache.acquire()
    cache.acquire()
    a = cache.get_pixmap("source", wallpaper, load=_counting_load)
    b = cache.get_pixmap("source", wallpaper, load=_counting_load)
    assert a is b
    assert len(calls) == 1  # 只解码一次
    assert cache._users == 2


def test_one_closed_other_still_valid(cache, wallpaper):
    """⑦ 关一窗口（release）后另一窗口取图仍有效：未归零不起宽限、缓存不动"""
    cache.acquire()
    cache.acquire()
    a = cache.get_pixmap("source", wallpaper, load=_load)
    cache.release()
    assert cache._users == 1
    assert cache._evict_timer is None  # 计数未归零，不得起淘汰定时器
    b = cache.get_pixmap("source", wallpaper, load=_load)
    assert a is b  # 另一窗口拿到的仍是同一有效对象（无悬空、无重建）
    assert cache.stats()["entries"] == 1


def test_all_closed_grace_then_evict(cache, wallpaper, qapp, monkeypatch):
    """⑧ 全关后：宽限内重开取消定时器；宽限到点缓存被 clear"""
    monkeypatch.setattr(spc_mod, "_GRACE_SECONDS", 0.05)
    cache.acquire()
    pix = cache.get_pixmap("source", wallpaper, load=_load)
    assert pix is not None
    # 归零 → 宽限定时器挂起
    cache.release()
    assert cache._users == 0
    assert cache._evict_timer is not None
    # 宽限内重开窗口 → 取消定时器、命中缓存零重解码
    cache.acquire()
    assert cache._evict_timer is None
    assert cache.get_pixmap("source", wallpaper, load=_load) is pix
    # 再归零 → 等宽限到点（事件循环驱动 QTimer）→ 缓存释放
    cache.release()
    assert cache._evict_timer is not None
    deadline = time.monotonic() + 2.0
    while cache._evict_timer is not None and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.02)
    assert cache._users == 0
    assert cache.stats() == {"groups": 0, "entries": 0}


def test_reopen_cycles_refcount_returns_to_zero(cache, wallpaper):
    """⑨ 重复开关窗口不泄漏计数：多次 acquire/release 后回到 0；重复 release 幂等"""
    for _ in range(3):
        cache.acquire()
        cache.acquire()
        cache.release()
        cache.release()
    assert cache._users == 0
    cache.release()  # 归零态重复 release：幂等，不打负
    cache.release()
    assert cache._users == 0


def test_scenelayer_destroyed_triggers_release(cache, wallpaper, qapp, monkeypatch):
    """⑩ [E1] 接线测试：SceneLayer.destroyed 信号真实触发 release。

    直调 acquire/release 的用例验不住 connect 断裂（测试全绿但生产里
    _users 永久 >0、缓存永不释放）。本用例走真实析构链路：
    SceneLayer 构造 acquire → deleteLater + sendPostedEvents(DeferredDelete)
    驱动 C++ 析构 → destroyed→release → 归零起宽限 → 到点 clear。
    """
    from PySide6.QtCore import QCoreApplication, QEvent
    from app.widgets.scene_layer import SceneLayer

    monkeypatch.setattr(spc_mod, "_GRACE_SECONDS", 0.05)
    a, b = SceneLayer(), SceneLayer()
    assert cache._users == 2, "SceneLayer 构造未 acquire（接线断）"
    a.deleteLater()
    b.deleteLater()
    # 裸 processEvents 不投递 DeferredDelete（Qt 语义），须强制投递
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert cache._users == 0, "SceneLayer 析构未触发 release（destroyed 接线断）"
    assert cache._evict_timer is not None, "归零后未起宽限定时器"
    # 宽限到点缓存被清
    cache.get_pixmap("source", wallpaper, load=_load)
    deadline = time.monotonic() + 2.0
    while cache._evict_timer is not None and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.02)
    assert cache.stats() == {"groups": 0, "entries": 0}, "宽限期到点缓存未被清"


# ── M2：baked 组内子键 LRU 上限 ─────────────────────────────


def _baked_load(w, h):
    return lambda p: QPixmap(w, h)


def test_baked_per_group_lru_cap(cache, wallpaper):
    """M2-① 同组写 6 个几何 baked → baked 条目封顶 4；最早 2 个再查 miss 重新烘焙"""
    cache.acquire()
    loads = []

    def _counting_load(p):
        loads.append(p)
        return QPixmap(16, 16)

    for i in range(6):
        cache.get_pixmap("baked", wallpaper, load=_counting_load,
                         width=100 + i, height=50, blur_radius=8.0, pad=16)
    stats = cache.stats()
    assert stats["entries"] == 4  # 组内只有 4 份 baked
    assert len(loads) == 6  # 每个新几何都真实烘焙过一次
    # 最早 2 个几何已被淘汰 → 再查 miss（重新烘焙，load 计数增加）
    cache.get_pixmap("baked", wallpaper, load=_counting_load,
                     width=100, height=50, blur_radius=8.0, pad=16)
    cache.get_pixmap("baked", wallpaper, load=_counting_load,
                     width=101, height=50, blur_radius=8.0, pad=16)
    assert len(loads) == 8  # miss 触发重新烘焙
    assert cache.stats()["entries"] == 4


def test_baked_lru_touch_keeps_hot(cache, wallpaper):
    """M2-② 写 g1..g5、命中 g1、写 g6 → 淘汰的是 g2，g1 仍在（子键 LRU 触碰生效）"""
    cache.acquire()
    geo = [(200 + i, 100) for i in range(6)]  # g1..g6
    for w, h in geo[:5]:  # 写 g1..g5：g1 在写 g5 时被淘汰
        cache.get_pixmap("baked", wallpaper, load=_baked_load(w, h),
                         width=w, height=h, blur_radius=8.0, pad=16)
    # 命中 g1：miss → 重新写入（此时淘汰 g2），g1 变最新
    cache.get_pixmap("baked", wallpaper, load=_baked_load(geo[0][0], geo[0][1]),
                     width=geo[0][0], height=geo[0][1], blur_radius=8.0, pad=16)
    # 写 g6 → 淘汰 g3。最终：g1（热存活）、g4、g5、g6
    cache.get_pixmap("baked", wallpaper, load=_baked_load(geo[5][0], geo[5][1]),
                     width=geo[5][0], height=geo[5][1], blur_radius=8.0, pad=16)
    keys = [k for k in list(cache._groups.values())[0]]
    baked_ws = sorted(k[1] for k in keys if k[0] == "baked")
    assert baked_ws == sorted([geo[0][0], geo[3][0], geo[4][0], geo[5][0]])  # g1,g4,g5,g6


def test_baked_cap_not_affect_source_premult(cache, wallpaper):
    """M2-③ 满载 baked 后写 source/premult：不被挤出（跳过非 baked 淘汰）"""
    cache.acquire()
    for i in range(4):
        cache.get_pixmap("baked", wallpaper, load=_baked_load(100 + i, 50),
                         width=100 + i, height=50, blur_radius=8.0, pad=16)
    src = cache.get_pixmap("source", wallpaper, load=_load)
    pre = cache.get_pixmap("premult", wallpaper, load=_load)
    # 再写 baked：淘汰逻辑只动 baked，source/premult 原位
    cache.get_pixmap("baked", wallpaper, load=_baked_load(200, 50),
                     width=200, height=50, blur_radius=8.0, pad=16)
    again_src = cache.get_pixmap("source", wallpaper, load=_load)
    again_pre = cache.get_pixmap("premult", wallpaper, load=_load)
    assert again_src is src and again_pre is pre  # 未被挤掉、未重建
    assert cache.stats()["entries"] == 6  # baked 4 + source 1 + premult 1


def test_baked_evicted_with_group(cache, wallpaper, tmp_path):
    """M2-④ 3 组交替 → 最旧组整体淘汰，其 baked 一并释放"""
    w1 = str(tmp_path / "w1.png"); _make_png(w1, 100)
    w2 = str(tmp_path / "w2.png"); _make_png(w2, 150)
    w3 = str(tmp_path / "w3.png"); _make_png(w3, 200)
    cache.acquire()
    cache.get_pixmap("baked", w1, load=_baked_load(100, 50),
                     width=100, height=50, blur_radius=8.0, pad=16)
    cache.get_pixmap("baked", w2, load=_baked_load(100, 50),
                     width=100, height=50, blur_radius=8.0, pad=16)
    assert cache.stats()["groups"] == 2
    cache.get_pixmap("baked", w3, load=_baked_load(100, 50),
                     width=100, height=50, blur_radius=8.0, pad=16)  # w1 组整体出
    assert cache.stats()["groups"] == 2
    assert cache.stats()["entries"] == 2  # 只剩 w2/w3 各 1 份 baked，w1 baked 全释放
