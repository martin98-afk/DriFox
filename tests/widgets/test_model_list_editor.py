# -*- coding: utf-8 -*-
"""模型列表编辑功能优化回归测试

覆盖：
1. SearchableEditableComboBox 保序去重（修复 set() 打乱拖拽顺序 + 菜单重复项）。
2. ModelListEditorWidget 搜索过滤 / 批量粘贴（多行+逗号）/ 重复标红 / 被过滤模型加回。
3. ProviderEditCard 获取结果合并/替换策略（不再静默覆盖手工列表）。
4. 默认模型失效提示（不再静默切换）。
5. 清空列表保存确认（不再静默恢复旧数据）。
6. fetch 元组返回解包（models + filtered_out）。
"""

import sys

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from app.plugins.registries.provider_registry import ProviderDef, ProviderRegistry


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    from PySide6.QtCore import QCoreApplication

    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


# ── SearchableEditableComboBox ────────────────────────────────


class TestComboOrdering:
    def test_additems_preserves_order(self, qapp):
        """回归：原实现用 set() 合并 _item_texts，拖拽排好的顺序写回后被随机打乱"""
        from app.widgets.searchable_editable_combobox import SearchableEditableComboBox

        combo = SearchableEditableComboBox()
        combo.addItems(["zeta", "alpha", "mid"])
        texts = [combo.itemText(i) for i in range(combo.count())]
        assert texts == ["zeta", "alpha", "mid"]
        assert combo._item_texts == ["zeta", "alpha", "mid"]

    def test_additems_dedupes_menu_and_texts(self, qapp):
        """回归：基类 addItem 不查重，旧实现批量添加含重复文本时菜单出现重复项"""
        from app.widgets.searchable_editable_combobox import SearchableEditableComboBox

        combo = SearchableEditableComboBox()
        combo.addItems(["a", "b", "a", "b", "a"])
        texts = [combo.itemText(i) for i in range(combo.count())]
        assert texts == ["a", "b"]
        assert combo._item_texts == ["a", "b"]

    def test_additem_duplicate_noop(self, qapp):
        """单条 addItem 重复文本应跳过（旧行为：菜单加重复项、_item_texts 不加 → 双源不一致）"""
        from app.widgets.searchable_editable_combobox import SearchableEditableComboBox

        combo = SearchableEditableComboBox()
        combo.addItem("m1")
        combo.addItem("m1")
        assert combo.count() == 1
        assert combo._item_texts == ["m1"]


# ── ModelListEditorWidget ─────────────────────────────────────


@pytest.fixture()
def editor(qapp):
    from app.widgets.model_list_edit_dialog import ModelListEditorWidget

    w = ModelListEditorWidget(["model-a", "model-b"])
    return w


class TestEditorSearch:
    def test_filter_hides_non_matching(self, editor):
        editor.searchEdit.setText("model-a")
        assert not editor.listWidget.item(0).isHidden()
        assert editor.listWidget.item(1).isHidden()

    def test_clear_filter_restores(self, editor):
        editor.searchEdit.setText("model-a")
        editor.searchEdit.clear()
        assert not editor.listWidget.item(1).isHidden()


class TestEditorBulkAdd:
    def test_multiline_and_comma_split(self, editor):
        """多行 + 中英文逗号混合输入批量拆分"""
        from app.widgets.model_list_edit_dialog import _split_model_input

        tokens = _split_model_input("m1\nm2\nm3, m4，m5")
        assert tokens == ["m1", "m2", "m3", "m4", "m5"]

    def test_add_tokens_skips_duplicates(self, editor):
        added = editor._add_tokens(["model-a", "new-1", "new-1"])
        assert added == 1
        assert editor.get_models() == ["model-a", "model-b", "new-1"]

    def test_paste_event_bulk_import(self, editor, monkeypatch):
        """Ctrl+V 批量粘贴（模拟 keyPressEvent 路径）"""
        from PySide6.QtGui import QKeyEvent

        monkeypatch.setattr(
            type(editor),
            "_clipboard_text",
            staticmethod(lambda: "pasted-1\npasted-2"),
        )
        event = QKeyEvent(QKeyEvent.KeyPress, Qt.Key_V, Qt.ControlModifier)
        editor.keyPressEvent(event)
        assert "pasted-1" in editor.get_models()
        assert "pasted-2" in editor.get_models()


class TestEditorDuplicateMark:
    def test_duplicate_marked_red(self, editor):
        """重复项前景色标红，唯一项恢复默认"""
        editor._add_tokens(["model-a"])  # 内部跳过重复，直接构造重复场景：
        dup = editor._make_item("model-a")
        editor.listWidget.addItem(dup)
        editor._check_duplicates()
        assert dup.foreground().color() == QColor("#e05656")
        assert editor.listWidget.item(0).foreground().color() == QColor("#e05656")
        assert editor.listWidget.item(1).foreground().color() != QColor("#e05656")

    def test_remove_duplicate_restores_color(self, editor):
        dup = editor._make_item("model-a")
        editor.listWidget.addItem(dup)
        editor._check_duplicates()
        editor.listWidget.takeItem(editor.listWidget.row(dup))
        editor._check_duplicates()
        assert editor.listWidget.item(0).foreground().color() != QColor("#e05656")

    def test_initial_items_editable(self, editor):
        """回归：set_models/addItems 的默认项无 ItemIsEditable，双击编辑失效"""
        assert bool(editor.listWidget.item(0).flags() & Qt.ItemIsEditable)


class TestEditorFiltered:
    def test_set_filtered_shows_region(self, editor):
        editor.set_filtered_models([" dall-e-3", "whisper-1"])
        assert editor.filteredWidget.isVisibleTo(editor)
        assert editor.filteredList.count() == 2

    def test_empty_filtered_hides_region(self, editor):
        editor.set_filtered_models([])
        assert not editor.filteredWidget.isVisibleTo(editor)

    def test_restore_filtered_moves_back(self, editor):
        editor.set_filtered_models(["whisper-1"])
        item = editor.filteredList.item(0)
        editor._restore_filtered(item)
        assert "whisper-1" in editor.get_models()
        assert editor.get_filtered_models() == []
        assert not editor.filteredWidget.isVisibleTo(editor)


# ── ProviderEditCard 获取策略 ─────────────────────────────────


@pytest.fixture()
def card(qapp, monkeypatch):
    from app.widgets.cards.settings import provider_edit_card as mod

    provider = ProviderDef(name="测试服务商")
    reg = ProviderRegistry()
    reg.register(provider, source="plugin:test")
    monkeypatch.setattr(ProviderRegistry, "_instance", reg)
    monkeypatch.setattr(ProviderRegistry, "get_instance", classmethod(lambda cls: reg))
    card = mod.ProviderEditCard(provider_name="测试服务商", provider_info={}, is_new=False)
    card.show()  # isVisible() 依赖祖先链可见，测试需真实 show（offscreen 平台）
    return card


def _stub_infobar(monkeypatch):
    """InfoBar 弹到 TabManagerWindow 上，测试环境没有主窗口，替换为收集器"""
    import qfluentwidgets

    shown = []
    for name in ("success", "warning", "error", "info"):
        monkeypatch.setattr(
            qfluentwidgets.InfoBar,
            name,
            staticmethod(lambda title, content, shown=shown, **kw: shown.append((name, title, content))),
        )
    return shown


class TestApplyFetched:
    def test_empty_existing_replaces(self, card, monkeypatch):
        """回归：首次获取（现有列表为空）直接填入，兼容 models_hook 旧测试语义"""
        shown = _stub_infobar(monkeypatch)
        card._apply_fetched(["m1", "m2"], mode="replace")
        assert card.modelCombo.get_all_models() == ["m1", "m2"]
        assert any(t == "已替换" for _, t, _ in shown)

    def test_merge_keeps_manual_models(self, card, monkeypatch):
        """核心回归：获取不再静默覆盖手工列表，合并时现有在前、新增去重追加"""
        shown = _stub_infobar(monkeypatch)
        card.modelCombo.addItems(["manual-1", "remote-a"])
        card._apply_fetched(["remote-a", "remote-b"], mode="merge")
        assert card.modelCombo.get_all_models() == ["manual-1", "remote-a", "remote-b"]
        assert any("新增 1 个" in c for _, _, c in shown)

    def test_replace_keeps_current_default_if_present(self, card, monkeypatch):
        _stub_infobar(monkeypatch)
        card.modelCombo.addItems(["old-1", "old-2"])
        card.modelCombo.setCurrentIndex(1)  # 默认 = old-2
        card._apply_fetched(["x1", "old-2"], mode="replace")
        assert card.modelCombo.currentText() == "old-2"

    def test_replace_invalid_default_warns(self, card, monkeypatch):
        """回归：默认模型不在获取结果中时提示并切到第一项，不再静默改值"""
        shown = _stub_infobar(monkeypatch)
        card.modelCombo.addItems(["old-1"])
        card.modelCombo.setCurrentIndex(0)
        card._apply_fetched(["new-1"], mode="replace")
        assert card.modelCombo.currentText() == "new-1"
        assert any(t == "默认模型已失效" and "old-1" in c for _, t, c in shown)

    def test_editor_visible_synced(self, card, monkeypatch):
        """回归：编辑器展开时获取，编辑器必须同步刷新（否则收起时旧数据写回吞掉结果）"""
        _stub_infobar(monkeypatch)
        card.modelCombo.addItems(["manual-1"])
        card.modelListEditor.set_models(["manual-1"])
        card.modelListEditor.setVisible(True)
        card._apply_fetched(["remote-1"], mode="merge")
        assert card.modelListEditor.get_models() == ["manual-1", "remote-1"]

    def test_filtered_models_passed_to_editor(self, card, monkeypatch):
        """被过滤模型展示在编辑器过滤区（P2 防误杀可见化）"""
        card._filtered_out_models = ["whisper-1"]
        card.modelListEditor.setVisible(True)
        card._apply_fetched(["m1"], mode="replace")
        assert card.modelListEditor.get_filtered_models() == ["whisper-1"]


class TestFetchTupleUnpack:
    def test_do_fetch_thread_unpacks_tuple(self, card):
        """内置 fetch 返回 (models, filtered_out) 元组，线程内正确解包"""
        results = {}
        card.fetchSuccess.connect(lambda models: results.update(models=models))
        card._do_fetch_thread(lambda: (["m1"], ["filtered-1"]))
        assert results.get("models") == ["m1"]
        assert card._filtered_out_models == ["filtered-1"]

    def test_do_fetch_thread_accepts_plain_list(self, card):
        """插件 models_hook 返回纯列表仍兼容"""
        results = {}
        card.fetchSuccess.connect(lambda models: results.update(models=models))
        card._do_fetch_thread(lambda: ["m1"])
        assert results.get("models") == ["m1"]
        assert card._filtered_out_models == []


class TestOnFetchSuccessDialog:
    def test_dialog_emitted_when_existing(self, card, monkeypatch):
        """现有列表非空时弹合并/替换选择框，不再直接覆盖"""
        card.modelCombo.addItems(["keep-1"])
        chosen_key = {}

        class FakeDialog:
            def __init__(self, *a, **k):
                pass

            def chosen(self):  # pragma: no cover - 签名占位
                pass

            chosen_signal = None

            def exec_(self):
                pass

        # 拦截 ChoiceDialog 构造（函数内 from ... import，需 patch 源头模块），模拟用户点「合并」
        captured = {}

        def fake_init(self, title, content, options, parent=None, cancel_text="取消"):
            captured["options"] = options
            self.chosen = type("Sig", (), {"connect": staticmethod(lambda fn: captured.update(fn=fn))})()
            self.exec_ = lambda: captured["fn"]("merge")

        import app.widgets.common_dialogs as cd_mod

        monkeypatch.setattr(cd_mod, "ChoiceDialog", type("CD", (), {"__init__": fake_init}))
        card._on_fetch_success(["remote-1"])
        assert ("merge", "合并") in [(k, l) for k, l in captured["options"]]
        assert card.modelCombo.get_all_models() == ["keep-1", "remote-1"]


class TestSaveEmptyConfirm:
    def test_save_empty_requires_confirm(self, card, monkeypatch):
        """回归：清空列表保存时弹确认，取消则不保存（旧逻辑静默恢复旧数据）"""
        card.provider_info = {"模型列表": ["old-1"]}
        card.modelCombo.addItems(["old-1"])
        card.modelListEditor.set_models([])
        card.modelListEditor.setVisible(True)

        # 模拟用户点「返回修改」：confirmed 永不触发（patch 源头模块，函数内 import）
        import app.widgets.common_dialogs as cd_mod

        class FakeConfirm:
            def __init__(self, *a, **k):
                self.confirmed = type("Sig", (), {"connect": staticmethod(lambda fn: None)})()

            def exec_(self):
                pass

        monkeypatch.setattr(cd_mod, "ConfirmDialog", FakeConfirm)
        saved = {}
        card.saved.connect(lambda name, info: saved.update(info=info))
        card._on_save()
        assert "info" not in saved, "取消清空确认后不应保存"

    def test_save_empty_confirmed_stores_empty(self, card, monkeypatch):
        """确认清空后保存空列表（不再回退旧数据）"""
        card.provider_info = {"模型列表": ["old-1"]}
        card.modelCombo.addItems(["old-1"])
        card.modelListEditor.set_models([])
        card.modelListEditor.setVisible(True)

        hooks = {}

        import app.widgets.common_dialogs as cd_mod

        class FakeConfirm:
            def __init__(self, *a, **k):
                hooks["confirmed_cb"] = None

                class Sig:
                    @staticmethod
                    def connect(fn):
                        hooks["confirmed_cb"] = fn

                self.confirmed = Sig()

            def exec_(self):
                hooks["confirmed_cb"]()

        monkeypatch.setattr(cd_mod, "ConfirmDialog", FakeConfirm)
        saved = {}
        card.saved.connect(lambda name, info: saved.update(info=info))
        card._on_save()
        assert saved.get("info", {}).get("模型列表") == []

    def test_save_normal_list_unchanged(self, card, monkeypatch):
        """正常保存不受影响"""
        card.provider_info = {"模型列表": ["a"]}
        card.modelCombo.clear()
        card.modelCombo.addItems(["a", "b"])
        card.modelListEditor.setVisible(False)
        saved = {}
        card.saved.connect(lambda name, info: saved.update(info=info))
        card._on_save()
        assert saved.get("info", {}).get("模型列表") == ["a", "b"]
