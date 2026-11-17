# -*- coding: utf-8 -*-
"""InputCardModule：输入卡模块——属性契约 + compose 端到端

契约集提取命令（搬运基线 main_widget.setup_ui 1-indexed L3274-3415）：
    python -X utf8 -c "import re; lines=open('app/main_widget.py',encoding='utf-8').read().split(chr(10)); pat=re.compile(r'self\\.([\\w]+)\\s*[:=]'); attrs=[m.group(1) for l in lines[3273:3415] if (m:=pat.match(l.strip()))]; print(chr(10).join(attrs))"
"""

import pytest
from unittest.mock import MagicMock

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
from app.widgets.ui_composition import compose


# 源段 self.X = 赋值全集（含类型注解）：host 上契约属性
# P1-1 懒实例化：_command_card / _file_mention_card 不再是 build 即时契约，
# 改为「装配后契约」（host._install_lazy_cards() 完成后存在，见装配段注释）
_CONTRACT_ATTRS = (
    "_bottom_input_container",
    "_bottom_input_layout",
    "_input_card",
    "_input_card_wrapper",
    "_attach_container",
    "_attach_layout",
    "_attachments",
    "_history_working_attachments",
    "input_area",
    "_undo_delete_card",
    "_undo_delete_store",
    "_truncation_sentinel",
    "_pending_send_after_truncation",
    "_pending_send_user_text",
)

# 延迟装配契约：install 完成后 host 上必须存在的两卡属性
_LAZY_CARD_ATTRS = ("_command_card", "_file_mention_card")



def test_module_id():
    from app.widgets.modules.input_card_module import InputCardModule

    assert InputCardModule.module_id == "input_card"


def test_compose_builds_input_card(fresh_registry, qapp):
    from PyQt5.QtWidgets import QWidget

    from app.widgets.cards.card_container import CompletionCardContainer
    from app.widgets.modules.input_card_module import InputCardModule

    fresh_registry.register_ui_module("input_card", InputCardModule, plugin_name="system")

    # host 桩：提供 build 依赖的宿主属性/方法（真实 QWidget 以支持父子控件关系）
    class _Host(QWidget):
        def __init__(self):
            super().__init__()
            self._window_id = "test-window"
            self._card_manager = MagicMock()
            self._bottom_card_container = MagicMock()
            # L1 补全容器用真实控件而非 MagicMock：build 会对它调 add_card()，
            # 桩掉就测不出"补全卡真的进了容器"这条契约
            self._completion_container = CompletionCardContainer()
            for _m in (
                "_load_input_history",
                "_update_subagents_param_description",
                "_ensure_file_mention_cache",
                "_on_send_clicked",
                "_on_send_clicked_inverse",
                "_on_stop_clicked",
                "_on_clear_shortcut",
                "_on_agent_changed",
                "_on_slash_triggered",
                "_on_slash_dismissed",
                "_on_slash_show_hint",
                "_on_at_triggered",
                "_on_at_dismissed",
                "_on_files_dropped",
                "_on_file_mention_confirmed",
                "_on_entering_history_mode",
                "_on_history_attachments_restored",
                "_on_history_mode_exited",
                "_on_attachments_removed_from_text",
                "_on_file_mention_selected",
                "_on_mention_selected",
                "_on_session_selected",
                "_rebuild_attachment_chips",
                "_restore_deleted_message",
                "_on_undo_dismiss_requested",
                "_on_undo_delete_dismissed",
                "_on_queue_insert_requested",
                "_on_queue_remove_requested",
                "_on_queue_edit_requested",
                "_on_subagent_model_config_changed",
            ):
                setattr(self, _m, lambda *a, **k: None)

    host = _Host()
    report = compose(host, ["input_card"])
    assert report["input_card"] == "system"
    for attr in _CONTRACT_ATTRS:
        assert hasattr(host, attr), f"missing host attribute: {attr}"

    # ── P1-1 懒实例化：构造段零实例化 ──
    # 两卡属性在 build 返回时不存在（main_widget 的 hasattr 守卫依赖此语义
    # 安全跳过），装配入口 _install_lazy_cards 已挂到 host
    assert not hasattr(host, "_command_card"), "构造段不应实例化 CommandCard"
    assert not hasattr(host, "_file_mention_card"), "构造段不应实例化 FileMentionCard"
    assert callable(getattr(host, "_install_lazy_cards", None))
    assert set(host._completion_container._cards) == set(), "装配前补全容器应为空"

    # 同步触发装配（单测不等真实 50ms timer）：装配后契约与原同步版一致
    host._install_lazy_cards()
    for attr in _LAZY_CARD_ATTRS:
        assert hasattr(host, attr), f"missing lazy card attribute after install: {attr}"
    from app.widgets.cards.floating.command_card import CommandCard
    from app.widgets.cards.floating.file_mention_card import FileMentionCard

    assert isinstance(host._command_card, CommandCard)
    assert isinstance(host._file_mention_card, FileMentionCard)

    # ── L1 拆层契约：补全卡（command / file_mention）进 COMPLETION 容器，
    #    BOTTOM 容器只留会话状态卡。同容器会让两者争抢同一段高度预算，
    #    实测排队卡会被压在命令卡参数行上（见 test_card_layers 的多卡高度用例）。
    from app.widgets.cards.card_manager import ContainerType

    assert host._completion_container.container_type is ContainerType.COMPLETION
    assert set(host._completion_container._cards) == {"command", "file_mention"}
    assert {c.args[0] for c in host._bottom_card_container.add_card.call_args_list} == {
        "undo_delete",
        "message_queue",
    }

def test_lazy_cards_registered_after_install(fresh_registry, qapp):
    """P1-1 懒实例化：装配后两卡进 L1 补全容器，构造段不注册"""
    from PyQt5.QtWidgets import QWidget

    from app.widgets.cards.card_container import CompletionCardContainer
    from app.widgets.cards.card_manager import ContainerType
    from app.widgets.modules.input_card_module import InputCardModule

    fresh_registry.register_ui_module("input_card", InputCardModule, plugin_name="system")

    class _Host(QWidget):
        def __init__(self):
            super().__init__()
            self._window_id = "test-window"
            self._card_manager = MagicMock()
            self._bottom_card_container = MagicMock()
            self._completion_container = CompletionCardContainer()
            for _m in (
                "_load_input_history",
                "_update_subagents_param_description",
                "_ensure_file_mention_cache",
                "_on_send_clicked",
                "_on_send_clicked_inverse",
                "_on_stop_clicked",
                "_on_clear_shortcut",
                "_on_agent_changed",
                "_on_slash_triggered",
                "_on_slash_dismissed",
                "_on_slash_show_hint",
                "_on_at_triggered",
                "_on_at_dismissed",
                "_on_files_dropped",
                "_on_file_mention_confirmed",
                "_on_entering_history_mode",
                "_on_history_attachments_restored",
                "_on_history_mode_exited",
                "_on_attachments_removed_from_text",
                "_on_file_mention_selected",
                "_on_mention_selected",
                "_on_session_selected",
                "_rebuild_attachment_chips",
                "_restore_deleted_message",
                "_on_undo_dismiss_requested",
                "_on_undo_delete_dismissed",
                "_on_queue_insert_requested",
                "_on_queue_remove_requested",
                "_on_queue_edit_requested",
                "_on_subagent_model_config_changed",
            ):
                setattr(self, _m, lambda *a, **k: None)

    host = _Host()
    compose(host, ["input_card"])
    assert set(host._completion_container._cards) == set()
    register_ids = {c.args[2] for c in host._card_manager.register_card.call_args_list}
    assert "command" not in register_ids and "file_mention" not in register_ids

    host._install_lazy_cards()
    assert set(host._completion_container._cards) == {"command", "file_mention"}
    register_ids = {c.args[2] for c in host._card_manager.register_card.call_args_list}
    assert {"command", "file_mention"} <= register_ids
    assert host._completion_container.container_type is ContainerType.COMPLETION
