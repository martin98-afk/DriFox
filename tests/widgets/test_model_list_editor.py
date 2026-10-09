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
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QApplication

from app.plugins.registries.provider_registry import ProviderDef, ProviderRegistry


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    from PyQt5.QtCore import QCoreApplication

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


class TestEditorConstruction:
    """P1 用户实测崩溃回归：卡片墙选预置服务商 → 进表单即崩。

    栈：``ProviderEditCard._init_ui`` → ``ModelListEditorWidget.__init__``
        → ``refresh_style`` → ``AttributeError: 'filtered_title'``

    为什么既有测试没拦住：既有 fixture 建完 editor 后**只调业务方法**，
    而崩溃发生在「宿主卡片构造链里 editor 的 refresh_style 被提前调用」——
    测试从未覆盖「宿主构造路径」这一入口（构造顺序差异）。
    本组补上：① 默认参数构造 ② 走编辑卡构造（含 preset）③ refresh_style 在
    子控件缺失时不得抛（防御式取属性）。
    """

    def test_default_ctor_does_not_crash(self, qapp):
        """默认参数构造（无 models / 无 parent）不崩"""
        from app.widgets.model_list_edit_dialog import ModelListEditorWidget

        w = ModelListEditorWidget()
        assert w.get_models() == []
        assert w.get_disabled_models() == []

    def test_ctor_with_default_model(self, qapp):
        """default_model 参数保留可传（星标语义已废弃，P1-9）：收下不用不崩"""
        from app.widgets.model_list_edit_dialog import ModelListEditorWidget

        w = ModelListEditorWidget(["a", "b"], default_model="b")
        assert w.get_models() == ["a", "b"]
        assert not hasattr(w, "setDefaultModel"), "星标语义应零残留"

    def test_refresh_style_tolerates_missing_children(self, qapp):
        """refresh_style 在子控件未创建时不得抛（构造早期调用的防御）"""
        from app.widgets.model_list_edit_dialog import ModelListEditorWidget

        w = ModelListEditorWidget(["m1"])
        # 模拟「构造链早期」：抹掉全部子控件引用，再调 refresh_style
        for attr in ("searchEdit", "hint_label", "candidate_title", "listWidget"):
            w.__dict__.pop(attr, None)
        # 不得抛 AttributeError（防御式取属性）
        w.refresh_style()

    def test_on_provider_changed_all_branches_no_crash(self, qapp, monkeypatch):
        """P0 回归：_on_provider_changed 全分支（预置/自定义/名称自动维护）不崩。

        崩溃史：星标退役后残留 setDefaultModel 调用（预置分支），用户点开
        编辑既有服务商即 AttributeError。本例锁死全部分支。
        """
        from app.plugins.registries.provider_registry import ProviderDef, ProviderRegistry
        from app.widgets.cards.settings.provider_edit_card import ProviderEditCard

        reg = ProviderRegistry()
        reg.register(
            ProviderDef(
                name="百度千帆",
                api_url="https://qianfan.baidubce.com/v2",
                default_model="ernie-4.0",
            ),
            source="plugin:test",
        )
        reg.register(ProviderDef(name="DeepSeek", api_url="https://api.deepseek.com"), source="plugin:test")
        monkeypatch.setattr(ProviderRegistry, "_instance", reg)
        monkeypatch.setattr(ProviderRegistry, "get_instance", classmethod(lambda cls: reg))

        card = ProviderEditCard(provider_name="", provider_info={}, is_new=True, preset_provider="")
        # 分支①：切到有插件预置的服务商（曾在此崩）
        card.nameCombo.setCurrentText("百度千帆")
        card._on_provider_changed("百度千帆")
        assert "ernie-4.0" in card.modelListEditor.get_models()
        # 分支②：配置名自动维护（用户没手填时跟随服务商名）
        assert card.configNameEdit.text() == "百度千帆"
        # 分支③：切到无预置的服务商（provider_default_config miss 分支）
        card._on_provider_changed("DeepSeek")
        assert card.modelListEditor is not None
        # 分支④：编辑态构造（非预置）同样不崩
        card2 = ProviderEditCard(
            provider_name="DeepSeek",
            provider_info={"provider_name": "DeepSeek", "API_URL": "https://api.deepseek.com"},
            is_new=False,
        )
        assert card2.modelListEditor is not None

    def test_provider_edit_card_constructs_with_preset(self, qapp, monkeypatch):
        """用户实测路径：编辑卡 + preset_provider 构造不崩（P1 崩溃点）"""
        from app.plugins.registries.provider_registry import ProviderDef, ProviderRegistry

        reg = ProviderRegistry()
        reg.register(
            ProviderDef(name="百度千帆", api_url="https://qianfan.baidubce.com/v2", default_model="ernie-4.0"),
            source="plugin:test",
        )
        monkeypatch.setattr(ProviderRegistry, "_instance", reg)
        monkeypatch.setattr(ProviderRegistry, "get_instance", classmethod(lambda cls: reg))

        from app.widgets.cards.settings.provider_edit_card import ProviderEditCard

        card = ProviderEditCard(provider_name="", provider_info={}, is_new=True, preset_provider="百度千帆")
        assert card.modelListEditor is not None, "编辑器应为常驻控件"

    def test_provider_edit_card_constructs_edit_mode(self, qapp, monkeypatch):
        """编辑态构造同样不崩（另一条构造分支）"""
        from app.plugins.registries.provider_registry import ProviderDef, ProviderRegistry

        reg = ProviderRegistry()
        reg.register(ProviderDef(name="DeepSeek", api_url="https://api.deepseek.com"), source="plugin:test")
        monkeypatch.setattr(ProviderRegistry, "_instance", reg)
        monkeypatch.setattr(ProviderRegistry, "get_instance", classmethod(lambda cls: reg))

        from app.widgets.cards.settings.provider_edit_card import ProviderEditCard

        card = ProviderEditCard(
            provider_name="DeepSeek",
            provider_info={"provider_name": "DeepSeek", "API_URL": "https://api.deepseek.com"},
            is_new=False,
        )
        assert card.modelListEditor is not None


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
        from PyQt5.QtGui import QKeyEvent

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
        """重复项行内名称标红，唯一项不受影响（setItemWidget 后走行控件）"""
        editor._make_row_item("model-a")  # 直接构造重复场景（_add_tokens 会跳过）
        editor._check_duplicates()
        assert editor._row_of("model-a")._duplicate is True
        assert editor._row_of("model-b")._duplicate is False

    def test_remove_duplicate_restores_color(self, editor):
        editor._make_row_item("model-a")
        editor.listWidget.setCurrentRow(2)  # 新加的重复行
        editor._delete_selected()  # 走真实删除路径（同步 _disabled）
        editor._check_duplicates()
        assert editor._row_of("model-a")._duplicate is False

    def test_rows_not_editable_and_no_dragdrop(self, editor):
        """P1-9 OpenCode 形态：setItemWidget 互斥 → 行不可编辑、无拖拽"""
        from PyQt5.QtWidgets import QListWidget

        assert not bool(editor.listWidget.item(0).flags() & Qt.ItemIsEditable)
        assert editor.listWidget.dragDropMode() == QListWidget.NoDragDrop


class TestEditorFiltered:
    """候选区（原「被过滤模型」区）：波8 起默认折叠，标题可点击展开"""

    def test_set_filtered_shows_region_collapsed(self, editor):
        """设置候选后区域可见，但**默认折叠**（列表空，标题带「点击展开」）"""
        editor.set_filtered_models([" dall-e-3", "whisper-1"])
        assert editor.filteredWidget.isVisibleTo(editor)
        assert editor.filteredList.count() == 0, "折叠态不填列表"
        assert "点击展开" in editor.filtered_title.text()

    def test_expand_fills_list(self, editor):
        """展开后列表填入候选"""
        editor.set_filtered_models(["whisper-1", "tts-1"])
        editor._toggle_candidates()
        assert editor.filteredList.count() == 2
        assert "点击收起" in editor.filtered_title.text()

    def test_empty_filtered_hides_region(self, editor):
        editor.set_filtered_models([])
        assert not editor.filteredWidget.isVisibleTo(editor)

    def test_restore_filtered_moves_back(self, editor):
        editor.set_filtered_models(["whisper-1"])
        editor._toggle_candidates()  # 展开才能点到项
        item = editor.filteredList.item(0)
        editor._restore_filtered(item)
        assert "whisper-1" in editor.get_models()
        assert editor.get_filtered_models() == []
        assert not editor.filteredWidget.isVisibleTo(editor)


# ── ProviderEditCard 获取策略 ─────────────────────────────────


class TestEditorEnableToggle:
    """P1-9 OpenCode 风格清单：行内启停开关（「模型关闭列表」语义）"""

    def test_toggle_off_adds_to_disabled(self, editor):
        """开关关 → 进关闭集；开关开 → 移出；get_disabled 按主列表序"""
        editor.set_models(["m1", "m2", "m3"])
        editor._on_row_toggle("m2", False)
        editor._on_row_toggle("m1", False)
        assert editor.get_disabled_models() == ["m1", "m2"], "应按主列表序"
        assert editor.get_models() == ["m1", "m2", "m3"], "关闭不改存在性"

        editor._on_row_toggle("m1", True)
        assert editor.get_disabled_models() == ["m2"]

    def test_row_widget_reflects_toggle(self, editor):
        """开关切换不重建列表：行控件即时反映开关态与淡化（真实信号链路）"""
        editor.set_models(["m1", "m2"])
        assert editor.listWidget.count() == 2
        row = editor._row_of("m1")
        assert row.switch.isChecked() is True

        row.switch.setChecked(False)  # 真实路径：checkedChanged → _emit_toggle
        assert editor.get_disabled_models() == ["m1"]
        assert row.switch.isChecked() is False
        assert editor.listWidget.count() == 2, "不重建整列表"

    def test_set_disabled_models_loads_disk_value(self, editor):
        """装载磁盘「模型关闭列表」：行开关回显，不触回调"""
        editor.set_models(["m1", "m2"])
        editor.set_disabled_models(["m2"])
        assert editor._row_of("m2").switch.isChecked() is False
        assert editor._row_of("m1").switch.isChecked() is True
        assert editor.get_disabled_models() == ["m2"]

    def test_delete_row_clears_disabled_state(self, editor):
        """删除行 = 移出主列表，关闭态随之清除（删除后回候选区可找回）"""
        editor.set_models(["m1", "m2"])
        editor._on_row_toggle("m1", False)
        editor.listWidget.setCurrentRow(0)
        editor._delete_selected()
        assert editor.get_models() == ["m2"]
        assert editor.get_disabled_models() == []

    def test_construct_with_existing_disabled_list_shows_off(self, qapp):
        """P0 回显红线：带存量「模型关闭列表」构造 → 对应行初始 Off"""
        from app.widgets.model_list_edit_dialog import ModelListEditorWidget

        w = ModelListEditorWidget(["m1", "m2", "m3"])
        w.set_disabled_models(["m2"])
        assert w._row_of("m2").switch.isChecked() is False, "存量关闭列表必须回显 Off"
        assert w._row_of("m1").switch.isChecked() is True
        assert w.get_disabled_models() == ["m2"]

    def test_construct_member_not_in_disabled_list_shows_on(self, qapp):
        """P0 回显补例：主列表存在但不在关闭列表 → 初始 On"""
        from app.widgets.model_list_edit_dialog import ModelListEditorWidget

        w = ModelListEditorWidget(["m1", "m2"])
        w.set_disabled_models(["m2"])
        assert w._row_of("m1").switch.isChecked() is True, "不在关闭列表的成员必须回显 On"

    def test_disabled_model_not_in_candidates(self, editor):
        """候选区自查：关闭模型在主列表 → 天然不进候选区（候选区排除主列表全员）"""
        editor.set_models(["m1", "m2"])
        editor._on_row_toggle("m1", False)
        editor.refresh_candidates("某服务商", fetched_models=["m1", "m9"])
        assert "m1" not in editor.get_candidate_models(), "关闭模型在主列表，不得进候选区"
        assert "m9" in editor.get_candidate_models()


class TestDedupeAndRowWidth:
    """P0：磁盘「模型列表」重复数据 → 写入源头唯一；行宽跟随 viewport 铺满"""

    def test_set_models_dedupes_preserving_order(self, editor):
        """set_models 重复项去重保序（磁盘存量重复不得入列表）"""
        editor.set_models(["m2", "m1", "m2", "m3", "m1"])
        assert editor.get_models() == ["m2", "m1", "m3"]
        assert editor.listWidget.count() == 3, "重复行不得渲染"

    def test_ctor_dedupes(self, qapp):
        """构造入口同样去重（词典初始化漏一路的兜底）"""
        from app.widgets.model_list_edit_dialog import ModelListEditorWidget

        w = ModelListEditorWidget(["a", "b", "a"])
        assert w.get_models() == ["a", "b"]

    def test_row_widget_fills_viewport_width(self, qapp):
        """行宽自适应：容器拉宽后行 widget 宽度跟随 viewport（铺满无空白）"""
        from app.widgets.model_list_edit_dialog import ModelListEditorWidget

        w = ModelListEditorWidget(["m1", "m2"])
        w.resize(700, 300)
        w.show()
        qapp.processEvents()
        w.resize(1400, 300)
        qapp.processEvents()
        vp_w = w.listWidget.viewport().width()
        for i in range(w.listWidget.count()):
            row = w.listWidget.itemWidget(w.listWidget.item(i))
            assert row is not None
            assert row.width() == vp_w, f"行 widget 应铺满 viewport（{row.width()} != {vp_w}）"
        w.close()


class TestEditorCandidates:
    """候选区（默认折叠）与三源刷新"""

    def test_collapsed_by_default_and_expands(self, editor):
        """默认折叠：区域可见但列表空；点击标题展开"""
        editor.set_candidate_models(["c1", "c2"])
        assert editor.candidateWidget.isVisibleTo(editor)
        assert editor.candidateList.count() == 0, "折叠态不填充列表"
        assert "点击展开" in editor.candidate_title.text()

        editor._toggle_candidates()
        assert editor.candidateList.count() == 2
        assert "点击收起" in editor.candidate_title.text()

    def test_execute_custom_hint(self, editor):
        """folded_hint 可自定义（候选区泛化）"""
        editor.set_candidate_models(["x"], folded_hint="其他")
        assert "其他" in editor.candidate_title.text()

    def test_refresh_candidates_three_sources_dedup(self, editor, monkeypatch):
        """三源（词典 ∪ 已拉取 ∪ 残留）去重，且排除已在主列表的"""
        import app.constants as constants

        monkeypatch.setattr(
            constants,
            "get_merged_provider_models",
            lambda: {"测试服务商": ["dict-1", "dict-2", "shared"]},
        )
        editor.set_models(["shared", "in-main"])  # shared 已在主列表
        editor.set_candidate_models(["stale-1"])

        editor.refresh_candidates("测试服务商", fetched_models=["fetched-1", "shared"])

        got = editor.get_candidate_models()
        assert got == ["dict-1", "dict-2", "fetched-1", "stale-1"], f"实际 {got}"
        assert "shared" not in got, "已在主列表的必须排除"
        assert "in-main" not in got

    def test_refresh_candidates_follows_provider_switch(self, editor, monkeypatch):
        """切服务商 → 候选区跟随（不同服务商取不同词典）"""
        import app.constants as constants

        monkeypatch.setattr(
            constants,
            "get_merged_provider_models",
            lambda: {"A 家": ["a-1"], "B 家": ["b-1", "b-2"]},
        )
        editor.refresh_candidates("A 家")
        assert editor.get_candidate_models() == ["a-1"]

        editor.refresh_candidates("B 家")
        assert "b-1" in editor.get_candidate_models() and "b-2" in editor.get_candidate_models()


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
    """波8 新形态：`_apply_fetched` 是兼容入口（直接写主列表），主路径走候选区"""

    def test_replace_writes_main_list(self, card):
        """replace：主列表被覆盖"""
        card._apply_fetched(["m1", "m2"], mode="replace")
        assert card.modelListEditor.get_models() == ["m1", "m2"]

    def test_merge_keeps_manual_models(self, card):
        """merge：现有在前、新增去重追加（旧 default 断言随星标退役删除）"""
        card.modelListEditor.set_models(["manual-1", "remote-a"])
        card._apply_fetched(["remote-a", "remote-b"], mode="merge")
        assert card.modelListEditor.get_models() == ["manual-1", "remote-a", "remote-b"]


class TestFetchTupleUnpack:
    def test_do_fetch_thread_unpacks_tuple(self, card):
        """内置 fetch 返回 (models, filtered_out[, status]) 元组，线程内正确解包"""
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


class TestFetchSuccessInjectsCandidates:
    """波8 重写：拉取结果不再弹三选，改为整体注入候选区"""

    def test_no_dialog_and_results_go_to_candidates(self, card, monkeypatch):
        """核验：不再构造 ChoiceDialog，结果进候选区"""
        import app.widgets.common_dialogs as cd_mod

        called = {"dialog": False}

        class FakeCD:
            def __init__(self, *a, **k):
                called["dialog"] = True

        monkeypatch.setattr(cd_mod, "ChoiceDialog", FakeCD)
        _stub_infobar(monkeypatch)

        card.modelListEditor.set_models(["keep-1"])
        card._on_fetch_success(["remote-1", "keep-1"])

        assert called["dialog"] is False, "波8 起不再弹三选弹窗"
        assert "remote-1" in card.modelListEditor.get_candidate_models(), "拉取结果应进候选区"
        assert "keep-1" not in card.modelListEditor.get_candidate_models(), "已在主列表的不进候选"
        assert card.modelListEditor.get_models() == ["keep-1"], "候选注入不改主列表"

    def test_fetch_meta_written_after_candidates(self, card, monkeypatch):
        """波7 状态元数据：候选注入完成后写入"""
        _stub_infobar(monkeypatch)
        card._fetch_meta = {}
        card._on_fetch_success(["m1"])
        assert card._fetch_meta.get("status") == "ok"
        assert card._fetch_meta.get("ts")

    def test_default_missing_warns_but_keeps_list(self, card, monkeypatch):
        """拉取结果含已关闭模型 → 仍进候选区（关闭模型不过滤，可找回）"""
        _stub_infobar(monkeypatch)
        card.modelListEditor.set_models(["keep-1"])
        card.modelListEditor._on_row_toggle("old-1", False)
        card.modelListEditor.set_models(["keep-1"])  # 重装模拟关闭行已移出主列表

        card._on_fetch_success(["new-1", "old-1"])

        assert "old-1" in card.modelListEditor.get_candidate_models(), "关闭的模型不应被过滤出候选区"
        assert card.modelListEditor.get_models() == ["keep-1"], "不得改主列表"


class TestSaveEmptyConfirm:
    def test_save_empty_requires_confirm(self, card, monkeypatch):
        """回归：清空列表保存时弹确认，取消则不保存（旧逻辑静默恢复旧数据）"""
        card.provider_info = {"模型列表": ["old-1"]}
        card.modelListEditor.set_models(["old-1"])
        card.modelListEditor.set_models([])

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
        card.modelListEditor.set_models(["old-1"])
        card.modelListEditor.set_models([])

        hooks = {}

        import app.widgets.common_dialogs as cd_mod

        class FakeConfirm:
            def __init__(self, *a, **k):
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
        card.modelListEditor.set_models(["a", "b"])
        saved = {}
        card.saved.connect(lambda name, info: saved.update(info=info))
        card._on_save()
        assert saved.get("info", {}).get("模型列表") == ["a", "b"]

    def test_save_writes_model_name_auto_maintained(self, card, monkeypatch):
        """P1-9：「模型名称」由 plan 自动维护——存档值不在启用列表 → 切启用首项"""
        card.provider_info = {"模型名称": "b"}
        card.modelListEditor.set_models(["a", "b"])
        card.modelListEditor._on_row_toggle("b", False)  # b 关闭 → 启用列表只剩 a
        saved = {}
        card.saved.connect(lambda name, info: saved.update(info=info))
        card._on_save()
        info = saved.get("info", {})
        assert info.get("模型名称") == "a", "存档值被关闭 → 自动切启用首项"
        assert info.get("模型列表") == ["a", "b"], "存在性不动"
        assert info.get("模型关闭列表") == ["b"], "关闭列表恒写"

    def test_save_model_name_in_enabled_list_kept(self, card):
        """存档值在启用列表 → 保持不变"""
        card.provider_info = {"模型名称": "a"}
        card.modelListEditor.set_models(["a", "b"])
        saved = {}
        card.saved.connect(lambda name, info: saved.update(info=info))
        card._on_save()
        assert saved.get("info", {}).get("模型名称") == "a"

    def test_save_model_name_empty_list_cleared(self, card):
        """启用列表空（全关/空表）→ 「模型名称」置空串"""
        card.provider_info = {"模型名称": "a", "模型列表": ["a"]}
        card.modelListEditor.set_models(["a"])
        card.modelListEditor._on_row_toggle("a", False)
        saved = {}
        card.saved.connect(lambda name, info: saved.update(info=info))
        card._on_save()
        assert saved.get("info", {}).get("模型名称") == ""
