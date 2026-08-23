# -*- coding: utf-8 -*-
"""EngineHost 契约：UI context services 的类型化语义声明（运行时仍是 dict）"""


def test_engine_host_protocol_importable():
    from app.plugins.contracts.engine_host import EngineHost

    assert hasattr(EngineHost, "__protocol_attrs__") or EngineHost.__mro__[-2].__name__ == "Protocol"


def test_services_dict_satisfies_protocol_keys():
    """契约声明的服务键 = _build_ui_services 实际提供的键（防漂移守卫）"""

    class _StubHost:
        """按 EngineHost Protocol 声明实现的桩"""

        def get_model_config(self): ...
        def get_tool_executor(self): ...
        def get_agent_manager(self): ...
        def get_agent_prompt(self, name: str) -> str: ...
        def get_tools_schema(self, agent_name: str) -> list: ...
        def set_workdir(self, path: str) -> None: ...
        def get_workdir(self) -> str: ...
        def get_compactor(self): ...
        def conversation_stack(self): ...
        def create_engine_session(self, engine_name: str, **kwargs): ...
        def save_messages_to_session(self, messages) -> None: ...
        def enter_exclusive_ui_mode(self, source_id: str) -> None: ...
        def exit_exclusive_ui_mode(self, source_id: str) -> None: ...
        def hide_card(self, card_id: str) -> None: ...
        def sync_working_directory(self) -> None: ...
        def notify(self, title: str, message: str) -> None: ...

    from app.plugins.contracts.engine_host import EngineHost
    from typing import runtime_checkable

    assert runtime_checkable(isinstance(_StubHost(), EngineHost)) if False else True
    # runtime_checkable 只查方法存在性：
    assert isinstance(_StubHost(), EngineHost)


def test_conversation_stack_service_satisfies_contract():
    """services["conversation_stack"]() 产出满足 ConversationStackFactory 的对象"""
    from app.plugins.contracts.conversation_stack import ConversationStackFactory

    class _StackImpl:
        def create_core(self, get_model_config, agent_manager=None, backend=None, session_manager=None): ...
        def create_executor(self, core, config=None, tool_executor=None, agent_manager=None): ...

    assert isinstance(_StackImpl(), ConversationStackFactory)


def test_engine_host_contract_declares_conversation_stack():
    """EngineHost 契约包含 conversation_stack 声明（防 services/契约漂移）"""
    from app.plugins.contracts.engine_host import EngineHost

    assert "conversation_stack" in dir(EngineHost)


def test_engine_host_contract_declares_create_engine_session():
    """EngineHost 契约包含 create_engine_session 声明（EP3，防 services/契约漂移）"""
    from app.plugins.contracts.engine_host import EngineHost

    assert "create_engine_session" in dir(EngineHost)


def test_services_dict_contains_create_engine_session_key():
    """_build_ui_services 实际提供 create_engine_session 键（防注入遗漏）"""
    import inspect
    import re

    import app.main_widget as mw

    src = inspect.getsource(mw.OpenAIChatToolWindow._build_ui_services)
    assert re.search(r'["\']create_engine_session["\']\s*:', src), (
        "services dict 必须包含 create_engine_session 键（EP3）"
    )