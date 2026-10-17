# -*- coding: utf-8 -*-
"""回归测试：用户气泡目标宽度「全链唯一口径」

Bug（2026-10-12 用户反馈「横向 resize 松手后收敛慢/跳变」）
--------------------------------------------------------
用户气泡的 margin 曾在三处各算一份：

* 拖拽防抖 ``_do_debounced_resize``：``vw * 0.25``（75%）
* 松手全量同步 ``_process_sync_width_batch``：``vw * 0.06``（94%）← 漏改
* 滚动同步 / 新增卡 ``_sync_single_card_width``：``vw * 0.06``（94%）← 漏改

于是松手瞬间卡片宽度从 75% 跳回 94%，PlainTextViewer 按新宽重折行 → 高度变
→ ``setFixedHeight`` → 宿主锚定补偿，一整套二次收敛。

修复：口径收敛到 ``message_card.user_bubble_target_width``，三个调用点共用。

运行::

    python -m pytest tests/widgets/test_user_bubble_width_consistency.py -v
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.widgets.message_card import user_bubble_target_width  # noqa: E402

_SRC_MAIN = PROJECT_ROOT / "app" / "main_widget.py"
_SRC_CARD = PROJECT_ROOT / "app" / "widgets" / "message_card.py"


# ---------------------------------------------------------------- 纯函数口径


def test_target_width_is_75_percent_of_container():
    """口径定义：容器宽 - max(24, 容器宽 × 0.25)，下限 320。"""
    assert user_bubble_target_width(1000) == 1000 - 250
    assert user_bubble_target_width(800) == 800 - 200


def test_target_width_floor_applies_to_narrow_container():
    """窄容器：margin 有 24px 下限，且目标宽不低于 320。"""
    # 80 * 0.25 = 20 < 24 → margin 取 24
    assert user_bubble_target_width(80) == 320  # 80 - 24 = 56 → 抬到 320
    assert user_bubble_target_width(200) == 320  # 200 - 50 = 150 → 抬到 320
    # 恰好越过下限的宽度：margin 仍取 24
    assert user_bubble_target_width(300) == 300 - 24


def test_target_width_monotonic_non_decreasing():
    """宽度单调不减：容器变宽，目标宽不得变窄（防止出现反直觉回缩）。"""
    prev = 0
    for w in range(100, 2000, 7):
        cur = user_bubble_target_width(w)
        assert cur >= prev, f"容器 {w} 的目标宽 {cur} 小于前一档 {prev}"
        prev = cur


def test_target_width_matches_drag_path_ratio():
    """回归靶点：口径必须是 75%（0.25），不得回退成 94%（0.06）。

    0.06 是 bug 值：1000 宽容器下算出 940，与拖拽路径的 750 差 190px，
    正是松手跳宽的幅度。
    """
    w = 1000
    assert user_bubble_target_width(w) == 750
    assert user_bubble_target_width(w) != w - max(24, int(w * 0.06))


# ---------------------------------------------------------------- 调用点一致性


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_no_stale_margin_literals_in_main_widget():
    """main_widget 不得再残留自算的 user margin（0.06 是 bug 值）。"""
    src = _read(_SRC_MAIN)
    assert "int(viewport_width * 0.06)" not in src, (
        "main_widget 仍残留 user margin 0.06（94%）口径——松手后会把 75% 落定的气泡"
        "重新撑到 94%，与拖拽路径打架。必须统一走 user_bubble_target_width()。"
    )
    assert "int(parent_width * 0.06)" not in src, "main_widget 残留 parent 口径 0.06"


def test_main_widget_uses_shared_helper_at_all_width_sync_sites():
    """三个宽度同步点都必须调用共享口径函数。"""
    src = _read(_SRC_MAIN)
    # 拖拽防抖 / 全量分批同步 / 单卡同步 各一处
    assert src.count("user_bubble_target_width(") >= 4, (
        "main_widget 中 user_bubble_target_width 调用点不足：拖拽防抖、全量分批、"
        "单卡同步三处 + 导入一处，至少要 4 次出现"
    )


def test_helper_is_defined_once_in_message_card():
    """口径函数唯一定义点在 message_card（其余模块一律 import）。"""
    assert _read(_SRC_CARD).count("def user_bubble_target_width") == 1
    main_src = _read(_SRC_MAIN)
    assert "def user_bubble_target_width" not in main_src, (
        "main_widget 不得再定义第二份口径实现（会再次分裂）"
    )
    assert "user_bubble_target_width" in main_src, "main_widget 必须 import 共享口径"


def test_message_card_no_arg_sync_width_uses_shared_helper():
    """MessageCard.sync_width 的无参分支也必须用同一口径。"""
    src = _read(_SRC_CARD)
    start = src.find("def sync_width(self, force: bool = False, target_width: int | None = None):")
    assert start != -1, "未找到 MessageCard.sync_width"
    end = src.find("\n    def ", start + 1)
    body = src[start:end]
    assert "user_bubble_target_width(parent_width)" in body, (
        "sync_width 无参分支必须走共享口径，否则 parent 推算路径再次分裂"
    )


def test_no_hardcoded_user_ratio_in_message_card():
    """message_card 内不得再有 user 专用的硬编码 margin 比例。"""
    src = _read(_SRC_CARD)
    # 允许在 helper 定义体内出现 0.25；其它位置不允许 user 比例字面量
    helper_start = src.find("def user_bubble_target_width")
    helper_end = src.find("\n\n\n", helper_start)
    rest = src[:helper_start] + src[helper_end:]
    assert "parent_width * 0.25" not in rest, "message_card 残留 parent 口径 0.25 硬编码"
    assert "parent_width * 0.06" not in rest, "message_card 残留 parent 口径 0.06 硬编码"


# ---------------------------------------------------------------- 行为级：三路径同宽


def test_debounced_and_full_sync_produce_same_width(qapp):
    """行为验证：拖拽防抖与松手全量同步算出的 user 卡宽度必须相等。

    直接调用两个入口的真实实现（stub 掉 QTimer 续批），比较 sync_width 的
    target_width 入参。
    """
    from unittest.mock import MagicMock, patch

    from app.main_widget import OpenAIChatToolWindow
    from app.widgets.message_card import MessageCard

    viewport_w = 1000
    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    win._is_destroyed = False
    win._last_chat_viewport_width = 0
    win._restore_epoch = 1
    win._sync_width_queue = []
    win._sync_width_idx = 0
    win._ensure_height_batch = MagicMock(return_value=None)
    win._begin_restore_chain = MagicMock()
    win._resize_preview_active = False
    win._resize_debounce_timer = MagicMock()
    win._resize_complete_timer = MagicMock()

    scroll_area = MagicMock()
    scroll_area.viewport.return_value.width.return_value = viewport_w
    scroll_area.viewport.return_value.rect.return_value = MagicMock()
    scroll_area.verticalScrollBar.return_value.value.return_value = 0
    win.chat_scroll_area = scroll_area

    card = MessageCard(role="user")
    card.sync_width = MagicMock(wraps=card.sync_width)
    layout = MagicMock()
    layout.count.return_value = 1
    item = MagicMock()
    item.widget.return_value = card
    layout.itemAt.side_effect = lambda i: item if i == 0 else None
    win.chat_layout = layout

    orchestrator = MagicMock()
    orchestrator.is_current.return_value = True

    # ① 拖拽防抖路径
    with patch("app.main_widget.ResizeOrchestrator.get_instance", return_value=orchestrator):
        win._do_debounced_resize()
    assert card.sync_width.call_count == 1
    drag_target = card.sync_width.call_args.kwargs.get("target_width")

    # ② 松手全量同步路径（首帧即全部处理完）
    card.sync_width.reset_mock()
    with (
        patch("app.main_widget.ResizeOrchestrator.get_instance", return_value=orchestrator),
        patch("app.main_widget.QTimer.singleShot", staticmethod(lambda ms, cb: None)),
    ):
        win._sync_all_cards_width()
    assert card.sync_width.call_count >= 1
    full_target = card.sync_width.call_args.kwargs.get("target_width")

    # ③ 滚动同步路径
    card.sync_width.reset_mock()
    win._scroll_sync_timer = MagicMock()
    win._last_visible_card_ids = set()
    win._restore_queue = []
    win._batch_placeholders = []
    win._log_render_quota = MagicMock()
    win._viewport_batch_range = MagicMock(return_value=None)
    win._sync_visible_cards_on_scroll()
    assert card.sync_width.call_count >= 1
    scroll_target = card.sync_width.call_args.kwargs.get("target_width")

    assert drag_target == full_target == scroll_target, (
        f"三条路径目标宽必须一致，实得 拖拽={drag_target} 全量={full_target} 滚动={scroll_target}"
    )
    assert drag_target == user_bubble_target_width(viewport_w)


def test_all_sync_sites_yield_same_target_for_various_widths():
    """多宽度枚举：口径函数自洽（覆盖窄容器下限抬升段）。"""
    for w in (320, 480, 640, 800, 1024, 1440, 1920, 2560):
        target = user_bubble_target_width(w)
        assert target >= 320, f"宽度 {w} 目标 {target} 低于 320 下限"
        if w > 400:
            assert target == w - int(w * 0.25), f"宽度 {w} 未按 75% 口径计算"
