# -*- coding: utf-8 -*-
"""模型列表编辑器：批量启停 + 表头排序 + 只看启用（2026-11-19）

覆盖用户实测痛点：
1. 几十个模型逐个点开关 → 多选 + 右键批量启用/关闭/移除。
2. 列表长只能肉眼翻 → 表头点列排序（含无价格项恒垫底）、只看启用。

技术约束（实测得出，勿改实现路径）：
- 行控件走 setItemWidget；takeItem 会**连带销毁**行控件且无法重新挂载，所以排序
  只能 sortItems + 自定义 __lt__，不能手动 takeItem/insertItem 重排。
- 行控件上的 Ctrl/Shift 点击天然冒泡到 QListWidget（多选不用改事件链路）。
- 行控件上的右键能收到 customContextMenuRequested，但不改选中态，菜单自己判定位。
"""

import sys

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    from PyQt5.QtCore import QCoreApplication

    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


@pytest.fixture()
def editor(qapp):
    from app.widgets.model_list_edit_dialog import ModelListEditorWidget

    w = ModelListEditorWidget(["cheap", "no-price", "pricey", "mid"])
    w.resize(800, 520)
    w.show()
    return w


def _stub_costs(editor, costs: dict):
    """把能力查询替换为固定价格表（costs 里没有的 = models.dev 无数据）"""

    def _caps(name):
        if name in costs:
            return {"cost": {"input": costs[name], "output": costs[name] * 2}}
        return {}

    editor._caps_for = _caps


class TestSortByPrice:
    def test_ascending_cheapest_first(self, editor):
        _stub_costs(editor, {"cheap": 0.1, "mid": 1.0, "pricey": 10.0})
        editor._on_header_clicked("input")
        assert editor.get_models() == ["cheap", "mid", "pricey", "no-price"]

    def test_missing_price_always_last(self, editor):
        """无价格恒垫底，升降都不参与「便宜/贵」语义"""
        _stub_costs(editor, {"cheap": 0.1, "mid": 1.0, "pricey": 10.0})
        editor._on_header_clicked("input")
        assert editor.get_models()[-1] == "no-price"
        editor._on_header_clicked("input")  # 切降序
        assert editor.get_models() == ["pricey", "mid", "cheap", "no-price"]

    def test_each_price_column_sorts_independently(self, editor):
        """点哪列排哪列：输入列与输出列口径不同（这里输出价 = 输入价 × 2，排序一致但走不同键）"""
        _stub_costs(editor, {"cheap": 0.1, "mid": 1.0, "pricey": 10.0})
        editor._on_header_clicked("input")
        by_input = editor.get_models()
        editor._on_header_clicked("output")
        assert editor.get_models() == by_input, "换列重置为升序（不继承上一列的降序）"

    def test_rows_survive_sort(self, editor):
        """排序不得销毁行控件（takeItem 重排会，sortItems 不会）"""
        _stub_costs(editor, {"cheap": 0.1, "mid": 1.0, "pricey": 10.0})
        editor._on_header_clicked("input")
        for i in range(editor.listWidget.count()):
            assert editor.listWidget.itemWidget(editor.listWidget.item(i)) is not None

    def test_sort_result_is_what_gets_saved(self, editor):
        """排序即落盘：get_models() 直接返回新序，保存链无需改动"""
        _stub_costs(editor, {"cheap": 0.1, "mid": 1.0, "pricey": 10.0})
        editor._on_header_clicked("input")
        assert editor.get_models() == ["cheap", "mid", "pricey", "no-price"]


class TestSortByNameAndEnabled:
    def test_name_toggle_asc_desc(self, editor):
        editor._on_header_clicked("name")
        asc = editor.get_models()
        assert asc == sorted(asc)
        editor._on_header_clicked("name")
        assert editor.get_models() == list(reversed(asc))

    def test_header_arrow_marks_current_column(self, editor):
        editor._on_header_clicked("name")
        assert "↑" in editor._header_cols["name"].text()
        assert "↑" not in editor._header_cols["input"].text()
        editor._on_header_clicked("name")
        assert "↓" in editor._header_cols["name"].text()

    def test_enabled_column_groups_disabled_last(self, editor):
        """点「启用」列 = 启用在前（这就是「启用/关闭分段」）"""
        editor._batch_set_enabled(["cheap", "mid"], False)
        editor._on_header_clicked("enabled")
        assert editor.get_models() == ["no-price", "pricey", "cheap", "mid"]

    def test_toggle_realigns_when_sorted_by_enabled(self, editor):
        """正按启用态排序时再切开关，顺序要跟着变（否则表头箭头在扯谎）"""
        editor._on_header_clicked("enabled")
        editor.listWidget.setCurrentRow(0)
        first = editor.get_models()[0]
        editor._batch_set_enabled([first], False)
        assert editor.get_models()[-1] == first

    def test_set_models_resets_sort_arrows(self, editor):
        """外部装载（磁盘顺序）必须清掉排序态，否则箭头误导"""
        editor._on_header_clicked("name")
        editor.set_models(["z", "a"])
        assert editor._sort_col == ""
        assert editor.get_models() == ["z", "a"]
        assert "↑" not in editor._header_cols["name"].text()

    def test_add_tokens_keeps_sort_when_active(self, editor):
        """排序态下新增行要按当前列归位，不能带着名称键混进价格序"""
        _stub_costs(editor, {"cheap": 0.1, "mid": 1.0, "pricey": 10.0})
        editor._on_header_clicked("input")
        editor._add_tokens(["aaa-very-cheap"])
        _stub_costs(editor, {"cheap": 0.1, "mid": 1.0, "pricey": 10.0, "aaa-very-cheap": 0.01})
        editor._apply_sort()
        names = editor.get_models()
        assert names.index("aaa-very-cheap") < names.index("cheap"), "价格序不能被新增打乱"


class TestBatchToggle:
    def test_multi_selection_mode(self, editor):
        from PyQt5.QtWidgets import QListWidget

        assert editor.listWidget.selectionMode() == QListWidget.ExtendedSelection

    def test_batch_disable_and_enable(self, editor):
        editor._batch_set_enabled(["cheap", "mid"], False)
        assert editor.get_disabled_models() == ["cheap", "mid"]
        assert editor.get_models() == ["cheap", "no-price", "pricey", "mid"], "关闭不改存在性"
        editor._batch_set_enabled(["cheap", "mid"], True)
        assert editor.get_disabled_models() == []

    def test_batch_updates_row_switches(self, editor):
        """批量走 set_enabled_state（blockSignals），行开关必须同步且不重复触发回调"""
        editor._batch_set_enabled(["cheap"], False)
        row = editor._row_of("cheap")
        assert row.switch.isChecked() is False
        assert editor._row_of("mid").switch.isChecked() is True

    def test_selected_names_in_list_order(self, editor):
        editor.listWidget.item(3).setSelected(True)
        editor.listWidget.item(0).setSelected(True)
        assert editor._selected_names() == ["cheap", "mid"], "按列表序而非点选序"

    def test_invert_selection_skips_hidden(self, editor):
        editor.onlyEnabledBox.setChecked(True)
        editor._batch_set_enabled(["cheap"], False)  # cheap 被隐藏
        editor._invert_selection()
        assert "cheap" not in editor._selected_names()

    def test_remove_names_clears_disabled(self, editor):
        editor._batch_set_enabled(["cheap"], False)
        editor._remove_names(["cheap"])
        assert "cheap" not in editor.get_models()
        assert editor.get_disabled_models() == []

    def test_context_menu_remove_actually_removes(self, editor, monkeypatch):
        # 回归：QAction.triggered(bool) 直接连带一参的槽会传 False，
        # _remove_names(False) 抛 TypeError 被 excepthook 吞掉，用户看到「点了没反应」
        from PyQt5.QtWidgets import QMenu

        editor.listWidget.clearSelection()
        editor.listWidget.item(0).setSelected(True)
        editor.listWidget.item(1).setSelected(True)

        clicked = []

        def fake_exec(self, *args, **kwargs):
            for act in self.actions():
                if act.text().startswith("移除所选"):
                    clicked.append(act.text())
                    act.trigger()
                    break
            return None

        monkeypatch.setattr(QMenu, "exec_", fake_exec)
        editor._on_context_menu(
            editor.listWidget.visualItemRect(editor.listWidget.item(0)).center()
        )

        assert clicked, "右键菜单里应有「移除所选」"
        assert editor.get_models() == ["pricey", "mid"]


class TestDeleteSelected:
    def test_multi_select_delete(self, editor):
        editor.listWidget.item(0).setSelected(True)
        editor.listWidget.item(2).setSelected(True)
        editor._delete_selected()
        assert editor.get_models() == ["no-price", "mid"]

    def test_fallback_to_current_row(self, editor):
        """无选中时回退到 currentRow（兼容既有单选调用点）"""
        editor.listWidget.setCurrentRow(1)
        editor.listWidget.clearSelection()
        editor._delete_selected()
        assert "no-price" not in editor.get_models()


class TestOnlyEnabledFilter:
    def test_hides_disabled_rows(self, editor):
        editor._batch_set_enabled(["cheap", "pricey"], False)
        editor.onlyEnabledBox.setChecked(True)
        visible = [
            editor.listWidget.item(i).data(Qt.UserRole)
            for i in range(editor.listWidget.count())
            if not editor.listWidget.item(i).isHidden()
        ]
        assert visible == ["no-price", "mid"]

    def test_disable_while_filtering_disappears_immediately(self, editor):
        """开着「只看启用」时关掉某行 → 立即从视野消失（用户明确要求）"""
        editor.onlyEnabledBox.setChecked(True)
        editor.listWidget.item(0).setSelected(True)
        editor._batch_set_enabled(editor._selected_names(), False)
        assert editor.listWidget.item(0).isHidden()

    def test_single_row_toggle_also_refilters(self, editor):
        editor.onlyEnabledBox.setChecked(True)
        editor._on_row_toggle("cheap", False)
        assert editor.listWidget.item(0).isHidden()

    def test_combines_with_keyword(self, editor):
        editor._batch_set_enabled(["mid"], False)
        editor.searchEdit.setText("p")  # 命中 cheap/no-price/pricey? → 只有 pricey 与 no-price 含 p
        editor.onlyEnabledBox.setChecked(True)
        visible = [
            editor.listWidget.item(i).data(Qt.UserRole)
            for i in range(editor.listWidget.count())
            if not editor.listWidget.item(i).isHidden()
        ]
        assert "mid" not in visible
        assert all("p" in n for n in visible)

    def test_unchecking_restores_all(self, editor):
        editor._batch_set_enabled(["cheap"], False)
        editor.onlyEnabledBox.setChecked(True)
        editor.onlyEnabledBox.setChecked(False)
        assert all(
            not editor.listWidget.item(i).isHidden() for i in range(editor.listWidget.count())
        )
