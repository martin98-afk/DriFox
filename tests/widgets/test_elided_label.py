# -*- coding: utf-8 -*-
"""_ElidedLabel 文本格式归属测试（#33：原文含 < > 时不误判富文本）

背景：无高亮分支用未 escape 原文 setText，QLabel AutoText 模式下
原文含 < 会被误判为 RichText（标签被吞/渲染错乱）。

修复：无高亮/未布局（width<=0）分支强制 PlainText 字面显示；
高亮分支保持 RichText（<span> 颜色高亮不受影响）。
"""

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication


def _ensure_qapp():
    """确保 QApplication 可用"""
    return QApplication.instance() or QApplication(sys.argv)


def _make_label(text):
    """构造独立的 _ElidedLabel（不加入布局，便于控制宽度）"""
    from app.widgets.elided_label import _ElidedLabel

    return _ElidedLabel(text)


class TestElidedLabelTextFormat:
    def test_plain_text_angle_brackets_literal(self):
        """无高亮 + 原文含 < >：强制 PlainText，字符字面显示（text() 保持原文）"""
        _ensure_qapp()
        lbl = _make_label("")
        lbl.resize(400, 30)
        lbl.show()
        # 真实调用路径：调用方 setText → _update_elided
        lbl.setText("任务 <2> 进行中")
        app = QApplication.instance()
        for _ in range(3):
            app.processEvents()
        # 全文可显示：不做省略，text() 为原文；PlainText 下 <2> 字面渲染
        assert lbl.text() == "任务 <2> 进行中"
        assert lbl.textFormat() == Qt.PlainText

    def test_constructed_text_format_forced_on_resize(self):
        """构造即含 < > + 从不 setText：resize 触发 _update_elided 后 format 被修正为 PlainText"""
        _ensure_qapp()
        lbl = _make_label("任务 <2> 进行中")
        # 不调 setText，直接 resize 触发 _update_elided（widget 显示必经布局/resize）
        lbl.resize(400, 30)
        lbl.show()
        lbl._update_elided()
        app = QApplication.instance()
        for _ in range(3):
            app.processEvents()
        assert lbl.textFormat() == Qt.PlainText, "未 setText 时 resize 也应修正为 PlainText"

    def test_elided_text_still_plain(self):
        """省略路径（文本超宽触发 elidedText）：省略后仍为 PlainText"""
        _ensure_qapp()
        lbl = _make_label("<prefix> " + "A" * 200)
        lbl.resize(80, 30)
        lbl.show()
        lbl._update_elided()
        app = QApplication.instance()
        for _ in range(3):
            app.processEvents()
        assert lbl.text() != "<prefix> " + "A" * 200, "超宽文本应被省略"
        assert lbl.textFormat() == Qt.PlainText, "省略后仍应为纯文本渲染"

    def test_highlight_still_rich_text(self):
        """有高亮命中：保持 RichText + <span> 高亮（不破坏高亮分支）"""
        _ensure_qapp()
        lbl = _make_label("使用 <b> 与搜索关键字")
        lbl.resize(400, 30)
        lbl.show()
        lbl._update_elided()
        app = QApplication.instance()
        for _ in range(3):
            app.processEvents()
        lbl.setHighlights(["关键字"], "#FF6600")
        assert lbl.textFormat() == Qt.RichText, "高亮分支必须保持 RichText（span 颜色生效）"
        assert "<span" in lbl.text()
        assert "关键字" in lbl.text()

    def test_no_layout_width_original_plain(self):
        """未布局（width<=0）时 setText：显示原文且为 PlainText"""
        _ensure_qapp()
        lbl = _make_label("初始")
        lbl.resize(0, 0)
        assert lbl.width() <= 0
        lbl.setText("<pending> 状态")  # 触发 width<=0 分支
        assert lbl.text() == "<pending> 状态"
        assert lbl.textFormat() == Qt.PlainText


class TestElidedLabelSingleLine:
    """单行语义回归：控制字符不得进入渲染文本（会话预览含 \\n 被行高截断）

    背景：会话预览取自消息原文（含 \\n），QLabel 会真换行渲染，而行高由外部
    钉死（会话行 44px）→ 多出的行被裁，视觉上叠字。且 QFontMetrics.elidedText
    把 \\n 当零宽字符（测量与渲染不一致），省略后控制字符仍在。
    """

    @staticmethod
    def _settle(lbl):
        app = QApplication.instance()
        for _ in range(3):
            app.processEvents()

    def test_newline_stripped_in_render_kept_in_tooltip(self):
        """渲染文本无换行；_full_text / tooltip 保留原文（回读语义不变）"""
        _ensure_qapp()
        lbl = _make_label("")
        lbl.resize(200, 20)
        lbl.show()
        raw = "第一行内容\n第二行内容也在这里\n第三行"
        lbl.setText(raw)
        self._settle(lbl)
        lbl._update_elided()
        self._settle(lbl)

        assert "\n" not in lbl.text(), f"渲染文本不应含换行：{lbl.text()!r}"
        assert lbl._full_text == raw, "_full_text 应保留原文（外部按原文比较）"
        assert lbl.toolTip() == raw, "tooltip 应保留原文（多行更好读）"

    def test_newline_replaced_not_removed(self):
        """换行为替换而非删除：全文可显示时控制字符变空格"""
        _ensure_qapp()
        lbl = _make_label("")
        lbl.resize(600, 20)
        lbl.show()
        lbl.setText("a\nb")
        self._settle(lbl)
        lbl._update_elided()
        assert lbl.text() == "a b", f"应换成空格保持字符数，实际 {lbl.text()!r}"

    def test_no_layout_branch_also_single_line(self):
        """未布局（width<=0）分支同样归一化（此路径也直接喂 QLabel 渲染）"""
        _ensure_qapp()
        lbl = _make_label("")
        lbl.resize(0, 0)
        lbl.setText("x\ny")
        assert "\n" not in lbl.text(), "width<=0 分支也不应把换行交给 QLabel"

    def test_highlight_path_single_line_and_span_intact(self):
        """高亮路径：归一化后仍能定位匹配并渲染 <span>（下标对齐）"""
        _ensure_qapp()
        lbl = _make_label("")
        lbl.resize(600, 20)
        lbl.show()
        lbl.setText("第一行\n关键字在这里")
        lbl.setHighlights(["关键字"], "#FF6600")
        self._settle(lbl)
        assert "\n" not in lbl.text(), f"高亮渲染也不应含换行：{lbl.text()!r}"
        assert "<span" in lbl.text(), "高亮 span 应保留"
        assert "关键字" in lbl.text(), "匹配内容应可见"