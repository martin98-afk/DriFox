# -*- coding: utf-8 -*-
"""EngineHost 契约 — UI 插件 context["services"] 的类型化语义声明。

现状：main_widget._build_ui_services() 返回 dict（14 个服务函数），插件按下标
取用、无静态检查。本 Protocol 是**语义锚点**——
- 插件作者：以本文件为服务面清单写代码（IDE 补全/类型检查）
- 主程序：dict 键集与本 Protocol 方法集保持一致（tests 守卫防漂移）
- 运行时**不改**：仍注入 dict，不强制包对象（零破坏，渐进收紧）

迁移路径：后续版本可让 services dict 增加属性访问适配（MappingProxy +
__getattr__），插件代码无需改动。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class EngineHost(Protocol):
    """对话引擎插件可用的宿主服务面（对应 ctx["services"] 全部 14 键）"""

    # ===== 对话栈驱动 =====
    def get_model_config(self) -> Dict[str, Any]:
        """当前模型配置（provider/api_key/base_url/model 等）"""
        ...

    def get_tool_executor(self) -> Any:
        """全局工具执行器（能力面最宽，慎用——见 ToolScope 后续契约）"""
        ...

    def get_agent_manager(self) -> Any:
        """Agent 管理器（列表/获取/权限）"""
        ...

    def get_agent_prompt(self, name: str) -> str:
        """指定 agent 的系统提示词"""
        ...

    def get_tools_schema(self, agent_name: str) -> List[Dict[str, Any]]:
        """指定 agent 视角的工具 schema（含 deny 过滤）"""
        ...

    # ===== 工作目录 =====
    def set_workdir(self, path: str) -> None:
        """设置工作目录（注意：当前为全局副作用——多窗口场景见 SessionLease 后续契约）"""
        ...

    def get_workdir(self) -> str:
        """当前工作目录"""
        ...

    def sync_working_directory(self) -> None:
        """同步工作目录到 UI 显示"""
        ...

    # ===== 上下文 =====
    def get_compactor(self) -> Any:
        """上下文压缩器"""
        ...

    # ===== 对话执行栈（EP2：插件不再 deep import app.core.conversation） =====
    def conversation_stack(self) -> Any:
        """对话执行栈工厂（满足 ConversationStackFactory 契约：
        create_core / create_executor，见 contracts/conversation_stack.py）"""
        ...

    # ===== 会话回写 =====
    def save_messages_to_session(self, messages: List[Dict[str, Any]]) -> None:
        """长任务消息并入当前会话（结束后调用）"""
        ...

    # ===== 独占模式（引用计数，幂等） =====
    def enter_exclusive_ui_mode(self, source_id: str) -> None: ...
    def exit_exclusive_ui_mode(self, source_id: str) -> None: ...

    # ===== UI 反馈 =====
    def hide_card(self, card_id: str) -> None:
        """隐藏浮动卡（window 级；Tab 全局作用域用
        UIPluginRegistry.hide_floating_card_globally）"""
        ...

    def notify(self, title: str, message: str) -> None:
        """InfoBar 通知（右下角，5 秒）"""
        ...