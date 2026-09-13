"""繁忙时行为判定纯函数测试"""

from app.main_widget import resolve_busy_behavior


def test_default_interject():
    assert resolve_busy_behavior("interject", False) == "interject"


def test_queue_passthrough():
    assert resolve_busy_behavior("queue", False) == "queue"


def test_inverse_flips():
    assert resolve_busy_behavior("interject", True) == "queue"
    assert resolve_busy_behavior("queue", True) == "interject"


def test_invalid_falls_back():
    assert resolve_busy_behavior("bogus", False) == "interject"
    assert resolve_busy_behavior("", True) == "queue"
