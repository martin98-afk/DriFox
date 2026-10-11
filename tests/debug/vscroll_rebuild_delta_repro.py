# -*- coding: utf-8 -*-
"""虚拟滚动「占位重建 → pin 起步高度 → viewer 首报」锚定增量失真复现脚本。

    python tests/debug/vscroll_rebuild_delta_repro.py

## 现象

列表滚动时，被原位重建的批次（T42 第 1.5 步 / `_restore_placeholders_in_viewport`）
会先把卡片 `pin_layout_height(H)` 钉在占位高度 H，而新建 viewer 仍从最小高度
40 起步。viewer 首个真实高度上报时，`_apply_viewer_height` 记的增量是

    _last_height_delta = HF - viewer.height()      # = HF - 40

而**卡片的实际高度变化**是

    Δcard = (HF + K) - H                            # K = 页脚/内边距等常量

两者相差整整一个 K 与 H 的量级：外层 `_on_message_card_height_changed` 拿这个
虚高增量做锚定补偿（`value += delta`）→ 视口被凭空拽走数百 px，并且同一 delta
还把滚动上界抬成 `max + delta`，连带污染贴底判定 —— 即「滚动时动态加载卡片后
滚轮到处乱飞」。

## 实测（H=800 / HF=760 / K=144）

    卡片实际高度: 800 -> 904   真实 Δ = 104
    上报的 _last_height_delta = 720
    外层 setValue(5720)（视口被拽 +720），setMaximum(20720)（上界虚增 720）

判据：补偿量必须与卡片实际高度变化一致（误差 ≤ 2px）。
"""

import os
import sys

os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from unittest.mock import MagicMock  # noqa: E402

from PyQt5.QtCore import QObject, Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication, QVBoxLayout, QWidget  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication.instance() or QApplication([])

from app.core.infra.webengine_profile import init_shared_web_profile  # noqa: E402

try:
    init_shared_web_profile()
except Exception:  # noqa: BLE001
    pass

from app.main_widget import OpenAIChatToolWindow  # noqa: E402
from app.widgets.message_card import MessageCard  # noqa: E402

VIEW_START = 5000  # 视口顶部（内容坐标）：卡片整体在其上方 → 走锚定补偿分支
H = 800  # 卸载时实高（占位高度）
HF = 760  # 重建后 viewer 真实高度（同内容同宽）


class _Harness(QObject):
    """承载 `_on_message_card_height_changed` 的轻量 QObject 载体。"""

    _on_message_card_height_changed = OpenAIChatToolWindow._on_message_card_height_changed
    _should_follow_bottom = OpenAIChatToolWindow._should_follow_bottom
    _is_view_at_bottom = OpenAIChatToolWindow._is_view_at_bottom
    _programmatic_scroll = OpenAIChatToolWindow._programmatic_scroll
    _sync_scroll_maximum = lambda self: 20000  # noqa: E731


def _make_card(parent, layout):
    card = MessageCard(role="assistant", parent=parent)
    layout.insertWidget(0, card)
    card.ensure_rendered()
    for _ in range(6):
        app.processEvents()
    return card


def _run_case(container, lay, hf: int) -> bool:
    """跑一档「真实内容高度 hf」，返回是否符合判据。"""
    card = _make_card(container, lay)
    natural_h, natural_viewer_h = card.height(), card.viewer.height()
    k = natural_h - natural_viewer_h

    # 卸载侧量出的占位高度 H（这里取一个与 hf 不同的值，模拟宽度变化等场景）
    card.pin_layout_height(H, content_overhead=k)
    for _ in range(6):
        app.processEvents()

    bar = MagicMock()
    bar.value.return_value = VIEW_START
    bar.maximum.return_value = 20000
    area = MagicMock()
    area.widget.return_value = container
    area.verticalScrollBar.return_value = bar

    win = _Harness()
    win.chat_scroll_area = area
    win._programmatic_scroll_depth = 0
    win._user_intentionally_away_from_bottom = True  # 用户在列表中段阅读（非跟随态）
    win._is_streaming = False
    win._scroll_max_cache = None
    card.heightChanged.connect(win._on_message_card_height_changed)

    before_h = card.height()
    card._apply_viewer_height(hf)
    for _ in range(6):
        app.processEvents()
    after_h = card.height()

    real_delta = after_h - before_h
    legacy_delta = hf - natural_viewer_h  # 修复前口径：viewer 从 40 起步
    compensated = bar.setValue.call_args_list[0][0][0] - VIEW_START if bar.setValue.call_args_list else 0

    print(f"  K={k}  卡片 {before_h} -> {after_h}（真实 Δ={real_delta}）")
    print(f"  修复前口径 delta={legacy_delta} → 视口被拽 {legacy_delta:+d}")
    print(f"  修复后补偿 = {compensated:+d}")
    return abs(compensated - real_delta) <= 2


def main():
    container = QWidget()
    lay = QVBoxLayout(container)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    lay.addStretch(1)
    container.resize(500, 20000)
    container.show()

    ok = True
    for hf in (760, 300, 1500):  # 变高 / 变矮（符号反向）/ 大跨度
        print(f"[HF={hf}]")
        ok = _run_case(container, lay, hf) and ok
        print()

    print("PASS" if ok else "FAIL（补偿量与卡片真实变化不符 → 视口乱飞）")
    return 0 if ok else 1


if __name__ == "__main__":
    rc = main()
    # 直接退出：WebEngine 在解释器收尾期析构会 access violation（offscreen 常见），
    # 与本脚本结论无关，避免它掩盖真实退出码。
    sys.stdout.flush()
    os._exit(rc)
