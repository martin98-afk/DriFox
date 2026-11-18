"""状态环几何缓存：保持原始采样结果并限制缓存容量。"""

import pytest
from PyQt5.QtCore import QRectF
from PyQt5.QtGui import QPainterPath

from app.widgets.tab_panel import _status_ring_points


@pytest.mark.parametrize("geometry", [(1.5, 1.5, 23.0, 23.0, 5.0), (12.5, 8.5, 41.0, 19.0, 4.0)])
def test_ring_points_match_original(geometry):
    x, y, width, height, radius = geometry
    path = QPainterPath()
    path.addRoundedRect(QRectF(x, y, width, height), radius, radius)
    length = path.length()
    expected = tuple(path.pointAtPercent(path.percentAtLength(length * i / 240)) for i in range(240))
    assert _status_ring_points(*geometry) == expected


def test_ring_points_reuse_and_bound():
    _status_ring_points.cache_clear()
    geometry = (1.5, 1.5, 23.0, 23.0, 5.0)
    first = _status_ring_points(*geometry)
    assert _status_ring_points(*geometry) is first
    assert _status_ring_points.cache_info().hits == 1
    for width in range(40):
        _status_ring_points(1.5, 1.5, float(width + 10), 23.0, 5.0)
    assert _status_ring_points.cache_info().currsize == 32
    assert _status_ring_points(*geometry) == first
