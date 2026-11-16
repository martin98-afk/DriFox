# -*- coding: utf-8 -*-
"""服务商编辑卡「幽灵弹窗」回归测试

缺陷现象
--------
系统配置 → 服务商列表 → 点「编辑」（以及服务商卡片墙 →「自定义」）时，
屏幕上会闪过一个空的白框，一闪即逝。

根因
----
``ProviderEditCard._init_ui`` 里「能力类型」行的容器写法：

    cap_row_widget = QWidget()                              # 无父
    cap_row_widget.setLayout(cap_row)
    cap_row_widget.setVisible(not self.preset_provider)     # ← 此刻 isWindow()=True
    main_layout.addWidget(cap_row_widget)                   # 入布局后才被 reparent

无父 QWidget 本身就是顶层窗口，``setVisible(True)`` 会让它作为一个独立窗口
显示出来（实测 640x480 的空 QWidget）；下一行 addWidget 把它收编成子控件，
顶层窗口随之销毁，用户看到的就是「闪一下的幽灵弹窗」。
预置服务商路径传 ``setVisible(False)``，不触发，故只有编辑/自定义两条路可见。

修法
----
1. 构造时给父级（``QWidget(self)``），它从一开始就待在父树里；
2. 可见性在 ``addWidget`` 之后设置（先入布局再显隐）。

本测试锁定：三条打开路径的构造全程都不产生任何「无父顶层」的 Show 事件。
"""

import sys
from pathlib import Path

from PyQt5.QtCore import QEvent, QObject
from PyQt5.QtWidgets import QWidget

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_INFO = {
    "config_id": "abc123",
    "provider_name": "DeepSeek",
    "name": "deep",
    "API_URL": "https://api.deepseek.com",
    "API_KEY": "sk-xx",
    "模型列表": ["deepseek-chat"],
}


class _GhostSpy(QObject):
    """捕获「无父且独立成窗」的 Show 事件 = 用户能看见的幽灵弹窗"""

    def __init__(self, app):
        super().__init__()
        self._app = app
        self.hits: list[str] = []
        self.recording = False

    def eventFilter(self, obj, ev):
        if self.recording and ev.type() == QEvent.Show:
            try:
                if obj.parent() is None and obj.isWindow() and isinstance(obj, QWidget):
                    self.hits.append(f"{type(obj).__name__} {obj.objectName()!r} {obj.width()}x{obj.height()}")
            except RuntimeError:
                pass
        return False


_HOST = {}


def _host(qapp):
    """可见宿主窗口（模拟卡片挂在 TabManagerWindow 下），首次调用即建好"""
    if "w" not in _HOST:
        w = QWidget()
        w.resize(900, 900)
        w.show()
        qapp.processEvents()
        _HOST["w"] = w
    return _HOST["w"]


def _collect_ghost_shows(card_cls, kwargs, qapp) -> list[str]:
    host = _host(qapp)
    spy = _GhostSpy(qapp)
    qapp.installEventFilter(spy)
    spy.recording = True
    try:
        card = card_cls(parent=host, **kwargs)
        qapp.processEvents()
    finally:
        spy.recording = False
        qapp.removeEventFilter(spy)
    card.deleteLater()
    qapp.processEvents()
    return spy.hits


class TestProviderEditCardNoGhostWindow:
    def test_edit_existing_provider(self, qapp):
        """编辑已有服务商：曾经闪 1 个 640x480 空窗"""
        from app.widgets.cards.settings.provider_edit_card import ProviderEditCard

        hits = _collect_ghost_shows(
            ProviderEditCard,
            {"provider_name": "DeepSeek", "provider_info": dict(_INFO), "is_new": False},
            qapp,
        )
        assert hits == [], f"构造编辑卡时出现了顶层幽灵窗口：{hits}"

    def test_add_custom_provider(self, qapp):
        """卡片墙 →「自定义」：同样曾闪 1 个空窗"""
        from app.widgets.cards.settings.provider_edit_card import ProviderEditCard

        hits = _collect_ghost_shows(
            ProviderEditCard,
            {"provider_name": "", "provider_info": {}, "is_new": True},
            qapp,
        )
        assert hits == [], f"构造自定义添加卡时出现了顶层幽灵窗口：{hits}"

    def test_add_preset_provider(self, qapp):
        """卡片墙 → 预置服务商：能力类型行本就隐藏，此前无闪现（守回归）"""
        from app.widgets.cards.settings.provider_edit_card import ProviderEditCard

        hits = _collect_ghost_shows(
            ProviderEditCard,
            {"provider_name": "", "provider_info": {}, "is_new": True, "preset_provider": "DeepSeek"},
            qapp,
        )
        assert hits == [], f"构造预置添加卡时出现了顶层幽灵窗口：{hits}"
