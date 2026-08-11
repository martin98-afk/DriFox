# -*- coding: utf-8 -*-
"""回归测试：插件行高必须随 tag/描述内容增长，不得统一压扁导致截断

复现背景：行内 Tag 标签行用 _FlowLayout 自动换行，但容器是裸 QWidget
（hasHeightForWidth 默认 False）。父 QVBoxLayout 计算行高时对 tag 行
回退用 sizeHint（= _FlowLayout.minimumSize，只含单行 tag 高），换行后
的总高度不计入 → 多行 tag 被压扁截断，且不同 tag 数量的行高趋同。

修复：新增 _FlowContainer（转发 heightForWidth 给 _FlowLayout），
父布局按换行后总高计算行高。
"""

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_MARKETPLACE = ROOT / "plugins" / "plugin-marketplace"
if str(PLUGIN_MARKETPLACE) not in sys.path:
    sys.path.insert(0, str(PLUGIN_MARKETPLACE))


def _app():
    from PyQt5.QtWidgets import QApplication

    return QApplication.instance()


def _make_plugins():
    return [
        {
            "name": "few-tags",
            "version": "1.0",
            "description": "短描述",
            "categories": ["工具"],
        },
        {
            "name": "many-tags",
            "version": "1.0",
            "description": "短描述",
            "categories": [
                "alpha",
                "beta",
                "gamma",
                "delta",
                "epsilon",
                "zeta",
                "eta",
                "theta",
                "iota",
                "kappa",
                "lambda",
                "mu",
                "nu",
                "xi",
                "omicron",
                "pi",
            ],
        },
        {
            "name": "long-desc",
            "version": "1.0",
            "description": "这是一个很长的描述" * 30,
            "categories": ["工具"],
        },
    ]


def _new_card(monkeypatch):
    if str(PLUGIN_MARKETPLACE) not in sys.path:
        sys.path.insert(0, str(PLUGIN_MARKETPLACE))
    from ui.cards import MarketplaceCard
    from ui.marketplace_manager import MarketplaceSourceManager

    monkeypatch.setattr(
        MarketplaceSourceManager,
        "get_sources",
        lambda self: [{"name": "fake", "source": {"source": "url", "url": "x"}}],
    )
    monkeypatch.setattr(
        MarketplaceSourceManager,
        "fetch_marketplace",
        lambda self, src, force=False: {"name": "fake", "plugins": _make_plugins()},
    )
    card = MarketplaceCard()
    card._build_local_extra_plugins = lambda: []
    return card


def _pump(seconds=0.2):
    app = _app()
    deadline = time.time() + seconds
    while time.time() < deadline:
        if app is not None:
            app.processEvents()
        time.sleep(0.01)


def test_row_height_grows_with_tags_and_desc(monkeypatch):
    """行高随 tag 数量 / 描述长度增长（修复前多 tag 行与单 tag 行等高）"""
    card = _new_card(monkeypatch)
    card.show()
    card.show_card()

    deadline = time.time() + 8
    while time.time() < deadline:
        _pump(0.05)
        if len(card._row_map) >= 3:
            break
    assert len(card._row_map) >= 3, f"未渲染 3 行: {len(card._row_map)}"

    _pump(0.5)  # 等 reveal + 布局稳定
    rows = {name: row for name, row in card._row_map.items()}
    h_few = rows["few-tags"].height()
    h_many = rows["many-tags"].height()
    h_long = rows["long-desc"].height()
    print(f"  few={h_few} many={h_many} long={h_long}")

    # 多 tag 行必须明显高于单 tag 行（修复前等高 → 断言失败）
    assert h_many > h_few + 10, f"多 tag 行未增高: few={h_few} many={h_many}"
    # 长描述行必须明显高于短描述行
    assert h_long > h_few + 10, f"长描述行未增高: few={h_few} long={h_long}"
    # 行高与自身 sizeHint 一致（未被压缩）
    for name in ("few-tags", "many-tags", "long-desc"):
        r = rows[name]
        assert r.height() >= r.sizeHint().height() * 0.85, (
            f"{name} 被压缩: height={r.height()} sizeHint={r.sizeHint().height()}"
        )


def test_many_tags_widget_reports_flow_height(monkeypatch):
    """_FlowContainer 必须转发换行高度（hasHeightForWidth + heightForWidth）"""
    from PyQt5.QtWidgets import QLabel

    card = _new_card(monkeypatch)
    card.show()
    card.show_card()

    deadline = time.time() + 8
    while time.time() < deadline:
        _pump(0.05)
        if len(card._row_map) >= 3:
            break
    _pump(0.5)

    row = card._row_map["many-tags"]
    # 找到 tags 容器
    from ui.cards import _FlowContainer

    found = None
    for w in row.findChildren(_FlowContainer):
        found = w
        break
    assert found is not None, "行内应有 _FlowContainer（tag 容器）"
    assert found.hasHeightForWidth(), "_FlowContainer 必须支持 heightForWidth"
    # 换行后总高 > 单行高（多 tag 必然换行）
    w = found.width()
    hfw = found.heightForWidth(w)
    single = 0
    for i in range(found._flow_layout.count()):
        it = found._flow_layout.itemAt(i)
        lbl = it.widget()
        single = max(single, lbl.sizeHint().height())
    print(f"  tags: width={w} hfw={hfw} single_tag_h={single}")
    assert hfw >= single * 2 - 2, f"hfw 未包含换行高度: hfw={hfw} single={single}"
    # QLabel 不该有内容截断（行内 tag 文本完整）
    for i in range(found._flow_layout.count()):
        lbl = found._flow_layout.itemAt(i).widget()
        assert lbl.text(), "tag QLabel 文本为空"
