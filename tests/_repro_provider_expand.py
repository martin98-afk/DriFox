# -*- coding: utf-8 -*-
"""临时复现脚本（用完即删）：首次打开设置 → 服务商列表最底部是否可触达"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger

logger.remove()
logger.add(sys.stderr, level="WARNING")

from PyQt5.QtCore import QTimer
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QVBoxLayout, QWidget

app = QApplication.instance() or QApplication(sys.argv)

from app.widgets.cards.card_container import TopCardContainer
from app.widgets.cards.card_manager import CardManager, ContainerType
from app.widgets.cards.settings.llm_settings_card import LLMSettingsCard
from app.widgets.cards.settings.provider_setting_card import ProviderItem

mgr = CardManager.get_instance()

WIN_H = int(sys.argv[1]) if len(sys.argv) > 1 else 760

win = QWidget()
win.resize(1100, WIN_H)
lay = QVBoxLayout(win)
lay.setContentsMargins(0, 0, 0, 0)
container = TopCardContainer()
container.set_overlay_mode(True)  # ★ 真实：全局 TOP 容器是覆盖层模式
container.bind_card_manager(mgr, "w1")
lay.addWidget(container)
lay.addStretch(1)
win.show()  # ★ 真实顺序：宿主窗口先显示，面板后懒构建
QTest.qWait(50)

panel = LLMSettingsCard(win)
panel.setVisible(False)
# 加压：30 条服务商，拉长列表
_real_providers = dict(panel.llmProviderCard.providers)
_fat = {}
for i in range(30):
    base = list(_real_providers.items())[i % len(_real_providers)]
    _fat[f"fat_{i}"] = dict(base[1])
panel.llmProviderCard.providers = _fat
panel.llmProviderCard._rebuild_rows(panel.llmProviderCard._compute_suffix_map())
mgr.register_card("w1", ContainerType.TOP, "settings", panel, system_card=True)
container.add_card("settings", panel)

card = panel.llmProviderCard
QTest.qWait(50)
print(f"构造后(隐藏) card.width={card.width()} isExpand={card.isExpand} h={card.height()}")

mgr.show_card("settings", "w1")
QTest.qWait(1500)

area = panel._page_scrolls["provider"]
inner = area.widget()
sb = area.verticalScrollBar()


def report(tag):
    sw = card.widget()
    print(
        f"    scrollWidget.h={sw.height()} viewport.h={card.viewport().height()} "
        f"view.h={card.view.height()} view.hint={card.view.sizeHint().height()} "
        f"space.h={card.spaceWidget.height()} sb.max={card.verticalScrollBar().maximum()}"
    )
    vp_h = area.viewport().height()
    print(
        f"[{tag}] winH={WIN_H} panel.h={panel.height()} container.h={container.height()} vp.h={vp_h} "
        f"inner.h={inner.height()} inner.hint={inner.sizeHint().height()} "
        f"sb.max={sb.maximum()} card.h={card.height()}"
    )
    sb.setValue(sb.maximum())
    QApplication.processEvents()
    QTest.qWait(30)
    print(f"    滚到底后 scrollWidget.h={card.widget().height()} viewport.h={card.viewport().height()}")
    rows = inner.findChildren(ProviderItem)
    print(f"    行数={len(rows)}")
    if rows:
        r = rows[-1]
        bottom = r.mapTo(area.viewport(), r.rect().bottomLeft()).y()
        print(f"    末行 bottom={bottom} vp.h={vp_h} 完整可见={0 <= bottom <= vp_h} 名={r.nameLabel.text()}")
    print(f"    卡片底部={card.mapTo(area.viewport(), card.rect().bottomLeft()).y()} vp.h={vp_h} "
          f"sb.val={sb.value()} sb.max={sb.maximum()}")


report("首开")
card.setExpand(False)
QTest.qWait(700)
card.setExpand(True)
QTest.qWait(1200)
report("折叠再展开")

QTimer.singleShot(0, app.quit)
