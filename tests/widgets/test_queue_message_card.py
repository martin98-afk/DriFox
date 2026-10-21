"""QueueMessageCard 纯函数逻辑测试（Qt 控件实例化依赖 QApplication，按项目惯例只测模块级纯函数）"""

from app.widgets.cards.floating.queue_message_card import summarize_entry_text


def test_summarize_collapses_whitespace():
    assert summarize_entry_text("  a \n b  ") == "a b"


def test_summarize_empty():
    assert summarize_entry_text("") == ""
    assert summarize_entry_text(None) == ""


def test_summarize_truncates_with_ellipsis():
    out = summarize_entry_text("字" * 80, limit=60)
    assert len(out) == 60
    assert out.endswith("…")


def test_summarize_short_passthrough():
    assert summarize_entry_text("你好世界", limit=60) == "你好世界"
