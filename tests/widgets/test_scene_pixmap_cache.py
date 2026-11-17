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
⑧ 全关（计数归零）立即释放；⑨ 重复开关窗口计数归零不泄漏
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtGui import QPixmap, QImage
from PyQt5.QtCore import Qt

import pytest

from app.core.infra.scene_pixmap_cache import baked_cap, scene_pixmap_cache


@pytest.fixture
def cache():
    scene_pixmap_cache.clear()
    # 引用计数状态一并复位（跨测试隔离；仅测试态直改私有成员）
    scene_pixmap_cache._users = 0
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
    """③ 超 _MAX_SETS=2 组 → 淘汰最旧组（I4：scene 层与 window/sidebar 区块层

    接入共享缓存后允许两组共存，防 scene 层 resize 烘焙热路径被 area 冷组挤掉；
    第 3 组写入才淘汰最旧组。"""
    w1 = str(tmp_path / "w1.png"); _make_png(w1, 100)
    w2 = str(tmp_path / "w2.png"); _make_png(w2, 150)
    w3 = str(tmp_path / "w3.png"); _make_png(w3, 200)
    first = cache.get_pixmap("source", w1, load=_load)
    second = cache.get_pixmap("source", w2, load=_load)
    third = cache.get_pixmap("source", w3, load=_load)
    assert cache.stats()["groups"] == 2, "两组共存（_MAX_SETS=2），w1 组已被淘汰"
    # 两次命中触碰使 LRU 序变 [w2, w3]（w3 最新）
    assert cache.get_pixmap("source", w2, load=_load) is second
    assert cache.get_pixmap("source", w3, load=_load) is third
    # 第 3 组写入（w1 重建）→ 淘汰最旧组 w2
    cache.get_pixmap("source", w1, load=_load)
    assert cache.stats()["groups"] == 2
    assert cache.get_pixmap("source", w3, load=_load) is third, "热组 w3 保留"
    again = cache.get_pixmap("source", w2, load=_load)
    assert again is not second, "最旧组 w2 被淘汰重建"

def test_area_premult_kind(cache, wallpaper):
    """[I4] area_premult kind：同图同 opacity 共享条目，不同 opacity 分条"""
    a = cache.get_pixmap(
        "area_premult", wallpaper,
        load=lambda p: QPixmap.fromImage(_load(p).toImage()),
        opacity_255=184,
    )
    b = cache.get_pixmap(
        "area_premult", wallpaper,
        load=lambda p: QPixmap.fromImage(_load(p).toImage()),
        opacity_255=184,
    )
    assert a is b, "同图同 opacity → 命中同一条目（跨窗口共享）"
    c = cache.get_pixmap(
        "area_premult", wallpaper,
        load=lambda p: QPixmap.fromImage(_load(p).toImage()),
        opacity_255=128,
    )
    assert c is not a, "不同 opacity → 不同条目"
    # source 与 area_premult 同图互不挤占（同一组的不同 sub_key）
    src = cache.get_pixmap("source", wallpaper, load=_load)
    assert cache.get_pixmap("source", wallpaper, load=_load) is src
    assert cache.stats() == {"groups": 1, "entries": 3}


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
    """⑦ 关一窗口（release）后另一窗口取图仍有效：未归零不清缓存"""
    cache.acquire()
    cache.acquire()
    a = cache.get_pixmap("source", wallpaper, load=_load)
    cache.release()
    assert cache._users == 1
    b = cache.get_pixmap("source", wallpaper, load=_load)
    assert a is b  # 另一窗口拿到的仍是同一有效对象（无悬空、无重建）
    assert cache.stats()["entries"] == 1


def test_release_at_zero_clears_immediately(cache, wallpaper):
    """⑧ 全关（计数归零）立即 clear，不等宽限；重开需重建（M-1b）"""
    cache.acquire()
    pix = cache.get_pixmap("source", wallpaper, load=_load)
    assert pix is not None
    cache.release()
    assert cache._users == 0
    assert cache.stats() == {"groups": 0, "entries": 0}, "归零后未立即释放（托盘駐留期驻留）"
    # 重开窗口：缓存已清 → 重新构建（非同一对象）
    cache.acquire()
    again = cache.get_pixmap("source", wallpaper, load=_load)
    assert again is not pix
    assert cache.stats()["entries"] == 1


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


def test_scenelayer_destroyed_triggers_release(cache, wallpaper, qapp):
    """⑩ [E1] 接线测试：SceneLayer.destroyed 信号真实触发 release。

    直调 acquire/release 的用例验不住 connect 断裂（测试全绿但生产里
    _users 永久 >0、缓存永不释放）。本用例走真实析构链路：
    SceneLayer 构造 acquire → deleteLater + sendPostedEvents(DeferredDelete)
    驱动 C++ 析构 → destroyed→release → 归零立即 clear。
    """
    from PyQt5.QtCore import QCoreApplication, QEvent
    from app.widgets.scene_layer import SceneLayer

    a, b = SceneLayer(), SceneLayer()
    assert cache._users == 2, "SceneLayer 构造未 acquire（接线断）"
    cache.get_pixmap("source", wallpaper, load=_load)
    assert cache.stats()["entries"] == 1
    a.deleteLater()
    b.deleteLater()
    # 裸 processEvents 不投递 DeferredDelete（Qt 语义），须强制投递
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert cache._users == 0, "SceneLayer 析构未触发 release（destroyed 接线断）"
    assert cache.stats() == {"groups": 0, "entries": 0}, "归零后未立即清空缓存"


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
    """M2-④ 切组 → 最旧组整体淘汰，其 baked 一并释放"""
    w1 = str(tmp_path / "w1.png"); _make_png(w1, 100)
    w2 = str(tmp_path / "w2.png"); _make_png(w2, 150)
    w3 = str(tmp_path / "w3.png"); _make_png(w3, 200)
    cache.acquire()
    cache.get_pixmap("baked", w1, load=_baked_load(100, 50),
                     width=100, height=50, blur_radius=8.0, pad=16)
    cache.get_pixmap("baked", w2, load=_baked_load(100, 50),
                     width=100, height=50, blur_radius=8.0, pad=16)
    assert cache.stats() == {"groups": 2, "entries": 2}  # I4：两组共存
    cache.get_pixmap("baked", w3, load=_baked_load(100, 50),
                     width=100, height=50, blur_radius=8.0, pad=16)  # 最旧组 w1 整体出
    assert cache.stats() == {"groups": 2, "entries": 2}


# ── M-1c：baked 组内上限按像素面积反算 ─────────────────────

def test_baked_cap_by_area():
    """M-1c 纯函数：4K→1、1440p→3、1080p→4（现状不变）；脏参数回落硬上限"""
    assert baked_cap(3840, 2160) == 1   # 31.64 MiB/份 > 48 MiB/2
    assert baked_cap(2560, 1440) == 3   # 14.06 MiB/份 ×3 = 42.2 MiB
    assert baked_cap(1920, 1080) == 4   # 7.91 MiB/份 → 6 截到硬上限 4
    assert baked_cap(0, 0) == 4         # 非正尺寸：不因脏参数缩容
    assert baked_cap(None, 1080) == 4   # 非法尺寸同上


def test_baked_cap_applied_4k(cache, wallpaper):
    """M-1c 接入：4K 几何写第 2 份即淘汰第 1 份；同组换 1080p 几何仍容 4 份"""
    cache.acquire()
    cache.get_pixmap("baked", wallpaper, load=_baked_load(3840, 2160),
                     width=3840, height=2160, blur_radius=8.0, pad=16)
    cache.get_pixmap("baked", wallpaper, load=_baked_load(3841, 2160),
                     width=3841, height=2160, blur_radius=8.0, pad=16)
    assert cache.stats()["entries"] == 1, "4K 单份超预算一半 → 上限降为 1，第二份写入淘汰第一份"
    for i in range(6):
        cache.get_pixmap("baked", wallpaper, load=_baked_load(1920 + i, 1080),
                         width=1920 + i, height=1080, blur_radius=8.0, pad=16)
    keys = [k for k in list(cache._groups.values())[0] if k[0] == "baked"]
    assert len(keys) == 4, "1080p 面积小 → 上限回到硬上限 4（M2 行为零变化）"


# ── M3：premult 按需生成（blur>0 时只是过程副本） ───────────

def _write_bg(tmp_path, name="bg.png", w=64, h=48):
    p = str(tmp_path / name)
    img = QImage(w, h, QImage.Format_ARGB32)
    img.fill(0xFF336699)
    assert img.save(p, "PNG")
    return p


def _cached_kinds(cache):
    return sorted(k[0] for b in cache._groups.values() for k in b)


def test_premult_cached_only_without_blur(cache, qapp, tmp_path):
    """M3 两分支：blur>0 → premult 不入缓存（只剩 source+baked，apply_config
    末尾同步烘 baked 顶替）；blur==0 → premult 是最终显示副本，仍走共享缓存"""
    from app.widgets.scene_layer import SceneLayer

    bg = _write_bg(tmp_path)
    base = {
        "image": bg,
        "opacity": 0.5,
        "animation": {"enabled": False},
        "foreground": {"enabled": False},
    }

    blurred = SceneLayer()
    try:
        blurred.apply_config(dict(base, blur=8), lambda p: p)
        assert _cached_kinds(cache) == ["baked", "source"], (
            "blur>0：premult 不得驻留共享缓存（同步烘 baked 顶替，省一份驻留）"
        )
    finally:
        blurred.deleteLater()

    cache.clear()
    plain = SceneLayer()
    try:
        plain.apply_config(dict(base, blur=0), lambda p: p)
        assert _cached_kinds(cache) == ["premult", "source"], (
            "blur==0：premult 是最终显示副本，必须仍走共享缓存"
        )
    finally:
        plain.deleteLater()


def test_blur_scene_zero_geometry_waits_for_resize(cache, qapp, tmp_path):
    """M3+ 早返回锚定：几何 0 → apply_config 不崩、label 无 pixmap、baked
    未生成；把 source 直接挂 label 时早返回也不动它；resize 后重走烘焙恢复"""
    from app.widgets.scene_layer import SceneLayer

    bg = _write_bg(tmp_path)
    layer = SceneLayer()  # 无父：_sync_geometry 直接返回，几何由 resize 控制
    try:
        layer.resize(0, 0)
        layer.apply_config(
            {
                "image": bg,
                "opacity": 0.5,
                "blur": 8,
                "animation": {"enabled": False},
                "foreground": {"enabled": False},
            },
            lambda p: p,
        )
        label = layer._image_label
        assert label is not None
        pm = label.pixmap()
        assert pm is None or pm.isNull(), "geo=0 早返回不得挂未预乘 source"
        assert _cached_kinds(cache) == ["source"], "几何未定不得生成 baked"

        # 早返回分支对「label 已被挂上 source」的旧状态必须安全：不动它、不崩
        label.setPixmap(layer._image_source_pixmap)
        layer._bake_image_now()
        assert label.pixmap().cacheKey() == layer._image_source_pixmap.cacheKey()

        # resize → _sync_content_geometry → 重走烘焙，baked 顶替
        layer.resize(400, 300)
        layer._sync_content_geometry()
        layer._bake_image_now()  # 跳过 150ms 防抖
        pm = label.pixmap()
        assert pm is not None and not pm.isNull()
        assert pm.width() == label.width() and pm.height() == label.height()
        assert label.hasScaledContents() is False
        assert _cached_kinds(cache) == ["baked", "source"]
    finally:
        layer.deleteLater()

