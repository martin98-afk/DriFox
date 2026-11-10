# -*- coding: utf-8 -*-
"""T43 验证：壁纸 pixmap 跨窗口共享缓存 scene_pixmap_cache。

offscreen 运行，无键鼠、无可见窗口（P0 约束）。

覆盖：
① 同 key 二次请求返回同一对象（QPixmap.cacheKey() 断言，隐式共享零复制）
② 不同 opacity/blur/尺寸产生不同条目
③ LRU 超 _MAX_SETS=2 组淘汰最旧组
④ clear() 后重新创建
⑤ 接入层行为：同壁纸两个 SceneLayer 实例的预乘副本是同一对象（2 份→1 份）
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtGui import QPixmap, QImage
from PyQt5.QtCore import Qt

import pytest

from app.core.infra import scene_pixmap_cache as spc_mod
from app.core.infra.scene_pixmap_cache import scene_pixmap_cache


@pytest.fixture
def cache():
    scene_pixmap_cache.clear()
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
