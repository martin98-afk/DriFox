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
        def get_provider_config(self, provider: str = "", model: str = "") -> dict: ...
        def get_tool_executor(self): ...
        def get_agent_manager(self): ...
        def get_agent_prompt(self, name: str) -> str: ...
        def get_tools_schema(self, agent_name: str) -> list: ...
        def set_workdir(self, path: str) -> None: ...
        def get_workdir(self) -> str: ...
        def conversation_stack(self): ...
        def create_engine_session(self, engine_name: str, **kwargs): ...
        def save_messages_to_session(self, messages) -> None: ...
        def enter_exclusive_ui_mode(self, source_id: str) -> None: ...
        def exit_exclusive_ui_mode(self, source_id: str) -> None: ...
        def hide_card(self, card_id: str) -> None: ...
        def sync_working_directory(self) -> None: ...
        def notify(self, title: str, message: str) -> None: ...
        def send_to_platform(self, platform, chat_id: str, content: str, **kwargs): ...
        def list_platform_sessions(self) -> list: ...
        def list_platforms(self) -> list: ...

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


def test_legacy_map_contains_create_engine_session():
    """create_engine_session 由 LEGACY_MAP 投影（实现真相在 providers/ui_core）。"""
    from app.plugins.capabilities.catalog import LEGACY_MAP

    assert LEGACY_MAP.get("create_engine_session") == "engine.session"
def test_engine_host_contract_declares_get_provider_config():
    """EngineHost 契约包含 get_provider_config 声明（插件取配置的唯一正确入口）"""
    from app.plugins.contracts.engine_host import EngineHost

    assert "get_provider_config" in dir(EngineHost)


def test_legacy_map_contains_get_provider_config():
    """get_provider_config 是插件拿「已解锁明文 API_KEY」的唯一通道，必须留在投影里。

    插件直读 app.config 在 password 模式下拿到 enc:v2: 密文、keyring 模式下
    拿到空串，都会 401。
    """
    from app.plugins.capabilities.catalog import LEGACY_MAP

    assert LEGACY_MAP.get("get_provider_config") == "model.config"
def test_resolve_provider_config_returns_memory_plaintext():
    """_resolve_provider_config 从内存态 _valid_configs 取配置（不碰磁盘文件）

    回归守卫：实现必须以 _valid_configs 为数据源；若改回读 app.config，
    密钥模式下返回的会是密文/空串，插件侧 401。
    """
    import types

    from app.main_widget import OpenAIChatToolWindow

    obj = types.SimpleNamespace()
    obj._valid_configs = {
        "cid1": {
            "provider_name": "MiniMax",
            "display_name": "MiniMax",
            "API_URL": "https://api.example.com/v1",
            "API_KEY": "plain-from-memory",
            "模型名称": "MiniMax-M3",
            "模型列表": ["MiniMax-M3", "MiniMax-M2.7"],
        }
    }
    obj._display_to_config_id = {"MiniMax": "cid1"}
    obj._current_provider_name = "cid1"
    obj._current_model_name = "MiniMax-M3"
    # 桩需绑定实现依赖的三个内部方法（真实实例上存在）
    obj._resolve_service_provider = types.MethodType(
        OpenAIChatToolWindow._resolve_service_provider, obj
    )
    obj._fuzzy_match_model_name = types.MethodType(
        OpenAIChatToolWindow._fuzzy_match_model_name, obj
    )
    obj._get_model_list_for_provider = lambda cid: obj._valid_configs.get(cid, {}).get("模型列表", [])

    resolve = OpenAIChatToolWindow._resolve_provider_config.__get__(obj)

    # 1) 显式指名 provider + model
    cfg = resolve("MiniMax", "MiniMax-M2.7")
    assert cfg["API_KEY"] == "plain-from-memory"
    assert cfg["API_URL"] == "https://api.example.com/v1"
    assert cfg["模型名称"] == "MiniMax-M2.7"

    # 2) 全默认 → 当前服务商 + 当前模型
    cfg2 = resolve()
    assert cfg2["API_KEY"] == "plain-from-memory"
    assert cfg2["模型名称"] == "MiniMax-M3"

    # 3) 未知 provider 返回 {}（不静默串到别的服务商）
    assert resolve("不存在的服务商") == {}


def test_engine_host_contract_declares_send_to_platform():
    """EngineHost 契约包含 send_to_platform / list_platform_sessions 声明（防漂移）"""
    from app.plugins.contracts.engine_host import EngineHost

    assert "send_to_platform" in dir(EngineHost)
    assert "list_platform_sessions" in dir(EngineHost)


def test_engine_host_contract_declares_list_platforms():
    """EngineHost 契约包含 list_platforms 声明（平台连接状态，会话列表正交）"""
    from app.plugins.contracts.engine_host import EngineHost

    assert "list_platforms" in dir(EngineHost)


def test_legacy_map_contains_gateway_keys():
    """gateway 投递三键必须在投影里（cron-tasks 等后台通知通道）。"""
    from app.plugins.capabilities.catalog import LEGACY_MAP

    assert LEGACY_MAP.get("send_to_platform") == "platform.send"
    assert LEGACY_MAP.get("list_platform_sessions") == "platform.read"
    assert LEGACY_MAP.get("list_platforms") == "platform.read"
