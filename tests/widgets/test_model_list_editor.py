# -*- coding: utf-8 -*-
"""模型列表编辑器组件测试。

覆盖：三元组接口、勾选写隐藏集、别名读写、批量导入切分、搜索过滤不改变底层数据。

注意：QApplication 必须在 qfluentwidgets 首次使用前创建——否则 qconfig 的
C++ 对象无效，后续构造 QWidget 报 RuntimeError（与其它 widget 测试同因）。
"""

import sys

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

# 与 message_card 系列测试同因：Qt 属性 + QApplication 先于 qfluentwidgets
QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
_APP = QApplication.instance() or QApplication(sys.argv)


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return _APP


@pytest.fixture
def editor(_qapp):
    from app.widgets.model_list_edit_dialog import ModelListEditorWidget

    return ModelListEditorWidget()


class TestResultTriple:
    def test_get_result_returns_three_parts(self, editor):
        editor.set_models(["a", "b"])
        models, hidden, aliases = editor.get_result()
        assert models == ["a", "b"]
        assert hidden == []
        assert aliases == {}

    def test_roundtrip_hidden_and_aliases(self, editor):
        editor.set_models(["a", "b"], hidden=["b"], aliases={"a": "甲"})
        models, hidden, aliases = editor.get_result()
        assert models == ["a", "b"]
        assert hidden == ["b"]
        assert aliases == {"a": "甲"}

    def test_set_models_defaults(self, editor):
        """不传 hidden/aliases 时默认为空"""
        editor.set_models(["x"])
        _, hidden, aliases = editor.get_result()
        assert hidden == []
        assert aliases == {}


class TestHiddenToggle:
    def test_uncheck_marks_hidden(self, editor):
        editor.set_models(["a", "b"])
        editor.set_model_checked("a", False)
        _, hidden, _ = editor.get_result()
        assert hidden == ["a"]

    def test_recheck_removes_from_hidden(self, editor):
        editor.set_models(["a"], hidden=["a"])
        editor.set_model_checked("a", True)
        _, hidden, _ = editor.get_result()
        assert hidden == []

    def test_hidden_item_still_in_models(self, editor):
        """隐藏不等于删除：模型仍在列表里，只是不启用"""
        editor.set_models(["a", "b"])
        editor.set_model_checked("b", False)
        models, _, _ = editor.get_result()
        assert models == ["a", "b"]


class TestAlias:
    def test_set_and_clear_alias(self, editor):
        editor.set_models(["a"])
        editor.set_alias("a", "甲模型")
        assert editor.get_result()[2] == {"a": "甲模型"}
        editor.set_alias("a", "")
        assert editor.get_result()[2] == {}


class TestPasteImport:
    def test_split_by_newline(self, editor):
        editor.set_models(["a"])
        added = editor.import_text("b\nc")
        assert added == ["b", "c"]
        assert editor.get_result()[0] == ["a", "b", "c"]

    def test_split_by_comma_and_space(self, editor):
        editor.set_models([])
        added = editor.import_text("a, b c\nd")
        assert added == ["a", "b", "c", "d"]

    def test_skip_duplicates(self, editor):
        editor.set_models(["a"])
        added = editor.import_text("a\nb")
        assert added == ["b"], "已存在的模型不重复导入"

    def test_empty_text(self, editor):
        editor.set_models(["a"])
        assert editor.import_text("   \n  ") == []
        assert editor.get_result()[0] == ["a"]


class TestSearchFilter:
    def test_search_hides_non_matching(self, editor):
        editor.set_models(["glm-5", "hy3", "kimi-k2.5"])
        editor.set_search_text("hy")
        assert editor.visible_models() == ["hy3"]

    def test_search_does_not_change_data(self, editor):
        """搜索只影响显示，不改底层列表"""
        editor.set_models(["glm-5", "hy3"])
        editor.set_search_text("glm")
        assert editor.get_result()[0] == ["glm-5", "hy3"]

    def test_clear_search_shows_all(self, editor):
        editor.set_models(["a", "b"])
        editor.set_search_text("a")
        editor.set_search_text("")
        assert editor.visible_models() == ["a", "b"]

    def test_search_matches_alias(self, editor):
        """搜索命中别名"""
        editor.set_models(["glm-5"], aliases={"glm-5": "主力模型"})
        editor.set_search_text("主力")
        assert editor.visible_models() == ["glm-5"]


class TestRemove:
    def test_remove_model(self, editor):
        editor.set_models(["a", "b"])
        editor.remove_model("a")
        assert editor.get_result()[0] == ["b"]

    def test_remove_also_clears_alias_and_hidden(self, editor):
        editor.set_models(["a", "b"], hidden=["a"], aliases={"a": "甲"})
        editor.remove_model("a")
        models, hidden, aliases = editor.get_result()
        assert models == ["b"]
        assert hidden == []
        assert aliases == {}


class TestSetAllChecked:
    def test_uncheck_all(self, editor):
        editor.set_models(["a", "b"])
        editor.set_all_checked(False)
        assert editor.get_result()[1] == ["a", "b"]

    def test_check_all(self, editor):
        editor.set_models(["a", "b"], hidden=["a"])
        editor.set_all_checked(True)
        assert editor.get_result()[1] == []

    def test_batch_only_affects_visible(self, editor):
        """批量勾选只作用于搜索结果（用户先搜再全选是常见意图）"""
        editor.set_models(["glm-5", "hy3"])
        editor.set_search_text("glm")
        editor.set_all_checked(False)
        assert editor.get_result()[1] == ["glm-5"], "hy3 被搜索过滤掉，不应受影响"


class TestNoQtBuiltinEditing:
    """回归：Qt 内置编辑器与 QInputDialog 双路径打架。

    item.text 含元数据摘要（如「glm-5    204K · 思考」），若启用 Qt 内置编辑，
    双击会把整串当模型名编辑，改完还只写回 item.text（不回写 _models），
    导致模型名被污染且改动丢失。改名必须走单一写路径（QInputDialog）。
    """

    def test_edit_triggers_disabled(self, editor):
        from PyQt5.QtWidgets import QListWidget

        assert editor.listWidget.editTriggers() == QListWidget.NoEditTriggers

    def test_items_not_editable(self, editor):
        from PyQt5.QtCore import Qt

        editor.set_models(["glm-5"])
        item = editor.listWidget.item(0)
        assert not (item.flags() & Qt.ItemIsEditable), "item 不应可编辑（避免内置编辑器改坏文本）"

    def test_item_text_contains_metadata(self, editor):
        """item.text 确实带元数据摘要，故绝不能让 Qt 内置编辑器碰它"""
        editor.set_models(["glm-5"])
        text = editor.listWidget.item(0).text()
        assert text.startswith("glm-5")
        # 模型名仍是纯 id（item.text 是展示串，权威数据在 _models）
        assert editor.get_result()[0] == ["glm-5"]
