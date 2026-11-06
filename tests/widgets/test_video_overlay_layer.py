# -*- coding: utf-8 -*-
"""VideoOverlayLayer：multiply 像素合成、cover 几何、配置清理、循环重播"""
import pytest
from PyQt5.QtCore import QRect, QSize
from PyQt5.QtGui import QColor, QImage
from PyQt5.QtMultimedia import QMediaPlayer


@pytest.fixture
def layer(qapp):
    from app.widgets.video_overlay_layer import VideoOverlayLayer
    w = VideoOverlayLayer()
    w.resize(64, 64)
    yield w
    w.deleteLater()


def test_cover_rect_matches_when_same_size():
    from app.widgets.video_overlay_layer import VideoOverlayLayer
    r = VideoOverlayLayer._cover_rect(QSize(64, 64), QRect(0, 0, 64, 64))
    assert r == QRect(0, 0, 64, 64)


def test_cover_rect_crops_wide_source():
    from app.widgets.video_overlay_layer import VideoOverlayLayer
    # 128x64 源铺进 64x64 目标：高是限制维度（scale=1.0），宽 128 溢出左右各裁 32
    r = VideoOverlayLayer._cover_rect(QSize(128, 64), QRect(0, 0, 64, 64))
    assert r.width() == 128 and r.height() == 64
    assert r.x() == -32 and r.y() == 0


def test_cover_rect_position_top_aligns_overflow():
    from app.widgets.video_overlay_layer import VideoOverlayLayer
    # 竖版 1080x1920 铺进 1600x900：scale=1.48→高 2844，溢出 1944；top=顶部对齐
    r = VideoOverlayLayer._cover_rect(QSize(1080, 1920), QRect(0, 0, 1600, 900), "top")
    assert r.y() == 0 and r.x() == 0
    assert r.height() == 2844
    # center 居中、bottom 贴底
    rc = VideoOverlayLayer._cover_rect(QSize(1080, 1920), QRect(0, 0, 1600, 900), "center")
    assert rc.y() == -972
    rb = VideoOverlayLayer._cover_rect(QSize(1080, 1920), QRect(0, 0, 1600, 900), "bottom")
    assert rb.y() == -1944


def test_apply_config_reads_position(layer):
    layer._player.play = lambda: None
    layer._player.setMedia = lambda *a: None
    layer.apply_config({"video": "x.mp4", "position": "top"})
    assert layer._position == "top"
    layer.apply_config({"video": "x.mp4", "position": "nonsense"})
    assert layer._position == "center"  # 非法值回退默认
    layer.apply_config({"video": "x.mp4"})
    assert layer._position == "center"


def test_multiply_pixel_blend(layer, qapp):
    """白底 + 200 灰帧 + opacity 0.28 → 期望 240（±2 容差）

    数学：multiply 中间值 = 255*200/255 = 200；
    final = 255*(1-0.28) + 200*0.28 = 239.6
    白底经补偿色铺（真实运行时下层内容透上来作 multiply 的底）。
    """
    layer._boost = QColor(255, 255, 255)
    frame = QImage(64, 64, QImage.Format_RGB32)
    frame.fill(QColor(200, 200, 200))
    layer._surface.frame = frame
    layer._opacity = 0.28
    pix = layer.grab()
    img = pix.toImage()
    assert abs(QColor(img.pixel(32, 32)).red() - 240) <= 2
    assert abs(QColor(img.pixel(32, 32)).green() - 240) <= 2


def test_boost_color_painted(layer):
    """补偿色直接可见（视频帧未到时）"""
    layer._boost = QColor(255, 253, 247)
    pix = layer.grab()
    c = QColor(pix.toImage().pixel(32, 32))
    assert (c.red(), c.green(), c.blue()) == (255, 253, 247)


def test_apply_config_none_stops_and_clears(layer):
    layer._surface.frame = QImage(8, 8, QImage.Format_RGB32)
    calls = []
    layer._player.play = lambda: calls.append("play")
    layer._player.setMedia = lambda *a: calls.append("setMedia")
    layer.apply_config(None)
    assert layer._surface.frame is None
    assert layer._boost is None


def test_black_frame_does_not_blacken(layer):
    """黑帧 + 白补偿底 → 期望 184（=255×0.72），纯黑帧不得把底涂黑"""
    layer._boost = QColor(255, 255, 255)
    frame = QImage(64, 64, QImage.Format_RGB32)
    frame.fill(QColor(0, 0, 0))
    layer._surface.frame = frame
    layer._opacity = 0.28
    pix = layer.grab()
    c = QColor(pix.toImage().pixel(32, 32))
    assert abs(c.red() - 184) <= 2


def test_loop_replays_on_end_of_media(layer):
    plays = []
    layer._player.play = lambda: plays.append(1)
    layer._on_status(QMediaPlayer.EndOfMedia)
    assert len(plays) == 1
    layer._on_status(QMediaPlayer.LoadedMedia)
    assert len(plays) == 1
