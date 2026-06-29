# -*- coding: utf-8 -*-
"""
工具权限控制器 — per-window 状态管理

设计：
- 每个窗口拥有独立的 controller 实例
- 区分"用户偏好"(user_*) 和"当前生效"(active_*) 两套状态
- 智能体命令激活时,active_* 被替换为 agent 的工具权限;user_* 不变
- "恢复"按钮:active_* = user_*.copy()
- 用户编辑开关:agent 模式下只更新 user,卡片显示保持 active;
              非 agent 模式下同时更新 user 和 active
- 复制/分支窗口:复制 user + active + active_agent_name
- 启动时:user 从全局 Settings 读取"用户最后修改的偏好"
"""
from typing import Any, Dict, Optional

from loguru import logger
from PyQt5.QtCore import QObject, pyqtSignal

from app.tools.tool_classifier import DANGEROUS_TOOLS, SAFE_TOOLS, get_default_toggles
from app.utils.config import Settings


class ToolPermissionController(QObject):
    """工具权限控制器(per-window)"""

    # 当前生效状态变化
    togglesChanged = pyqtSignal(dict)        # 引擎读取这个来获取最新 toggles
    behaviorChanged = pyqtSignal(str)        # 关闭行为变化
    activeAgentChanged = pyqtSignal(str)     # 当前激活的智能体(空字符串=用户模式)

    # 用户偏好状态变化(用于 UI 反映"用户原始设置")
    userTogglesChanged = pyqtSignal(dict)
    userBehaviorChanged = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        # 加载全局默认值(用户最后修改的偏好)
        settings = Settings.get_instance()
        saved_toggles = dict(settings.tool_toggles.value or {})
        if not saved_toggles:
            all_tools = list(DANGEROUS_TOOLS) + list(SAFE_TOOLS)
            saved_toggles = get_default_toggles(all_tools)

        # 清理已删除工具的残留配置(只保留当前已知的工具)
        known_tools = set(DANGEROUS_TOOLS) | set(SAFE_TOOLS)
        cleaned_toggles = {k: v for k, v in saved_toggles.items() if k in known_tools}
        # 补全新增工具的默认开启
        for tool in known_tools:
            if tool not in cleaned_toggles:
                cleaned_toggles[tool] = True
        # 若发现残留,持久化清理后的结果,避免下次启动仍带过期条目
        if cleaned_toggles != saved_toggles:
            try:
                settings.tool_toggles.value = dict(cleaned_toggles)
                settings.save()
                stale = set(saved_toggles.keys()) - set(cleaned_toggles.keys())
                if stale:
                    logger.info(
                        f"[ToolPermission] 清理已删除工具的残留开关: {sorted(stale)}"
                    )
            except Exception as e:
                logger.warning(f"[ToolPermission] 持久化清理残留开关失败: {e}")

        # 用户偏好
        self._user_tool_toggles: Dict[str, bool] = dict(cleaned_toggles)
        self._user_tool_off_behavior: str = settings.tool_off_behavior.value or "deny"

        # 当前生效(初始 = 用户偏好)
        self._active_tool_toggles: Dict[str, bool] = dict(self._user_tool_toggles)
        self._active_tool_off_behavior: str = self._user_tool_off_behavior

        # 当前激活的智能体(None = 用户模式)
        self._active_agent_name: Optional[str] = None

    # ===================================================================
    #  Getters
    # ===================================================================

    def _complete_toggles(self, toggles: Dict[str, bool]) -> Dict[str, bool]:
        """补全未知工具的默认值,并清理已删除工具的残留配置

        工具被删除后,旧 toggle 还残留在配置中会导致统计数量虚高
        (例如 input box 显示的危险/安全总数比 TOOL_GROUPS 实际列表多)。
        这里统一过滤掉不在当前 DANGEROUS+SAFE 集合里的工具。
        """
        all_tools = list(DANGEROUS_TOOLS) + list(SAFE_TOOLS)
        all_tools_set = set(all_tools)
        # 清理已删除工具的残留开关
        result = {tool: enabled for tool, enabled in toggles.items() if tool in all_tools_set}
        # 补全新增工具的默认开启
        for tool in all_tools:
            if tool not in result:
                result[tool] = True
        return result

    def get_toggles(self) -> Dict[str, bool]:
        """获取当前生效的工具开关(供 engine 使用)"""
        return self._complete_toggles(self._active_tool_toggles)

    def get_behavior(self) -> str:
        """获取当前生效的关闭行为(供 engine 使用)"""
        return self._active_tool_off_behavior

    def get_user_toggles(self) -> Dict[str, bool]:
        """获取用户偏好的工具开关"""
        return self._complete_toggles(self._user_tool_toggles)

    def get_user_behavior(self) -> str:
        """获取用户偏好的关闭行为"""
        return self._user_tool_off_behavior

    def get_active_agent_name(self) -> Optional[str]:
        """获取当前激活的智能体名(None=用户模式)"""
        return self._active_agent_name

    def is_agent_active(self) -> bool:
        return self._active_agent_name is not None

    # ===================================================================
    #  用户编辑(只更新 user)
    # ===================================================================

    def set_user_toggle(self, tool_name: str, enabled: bool):
        """用户编辑单个开关:
        - 非 agent 模式:同时更新 user(偏好,持久化) 和 active(生效)
        - agent 模式:只更新 active(临时修改 agent 生效权限,user 偏好不变)
        """
        if self.is_agent_active():
            # agent 模式:只改 active,user 偏好不变
            self._active_tool_toggles[tool_name] = enabled
            self.togglesChanged.emit(self.get_toggles())
            # userTogglesChanged 不发送(偏好未变)
        else:
            # 用户模式:user 和 active 同步
            self._user_tool_toggles[tool_name] = enabled
            self._active_tool_toggles[tool_name] = enabled
            self._persist_user_toggles()
            self.togglesChanged.emit(self.get_toggles())
            self.userTogglesChanged.emit(self.get_user_toggles())

    def set_user_toggles(self, toggles: Dict[str, bool]):
        """批量更新开关(用于整组开关)"""
        if self.is_agent_active():
            # agent 模式:只改 active
            self._active_tool_toggles.update(toggles)
            self.togglesChanged.emit(self.get_toggles())
        else:
            # 用户模式:user 和 active 同步
            self._user_tool_toggles.update(toggles)
            self._active_tool_toggles.update(toggles)
            self._persist_user_toggles()
            self.togglesChanged.emit(self.get_toggles())
            self.userTogglesChanged.emit(self.get_user_toggles())

    def set_user_behavior(self, behavior: str):
        """用户编辑关闭行为"""
        if self.is_agent_active():
            # agent 模式:只改 active_behavior
            self._active_tool_off_behavior = behavior
            self.behaviorChanged.emit(behavior)
        else:
            # 用户模式:user 和 active 同步,并持久化
            self._user_tool_off_behavior = behavior
            self._active_tool_off_behavior = behavior
            Settings.get_instance().tool_off_behavior.value = behavior
            Settings.get_instance().save()
            self.behaviorChanged.emit(behavior)
            self.userBehaviorChanged.emit(behavior)

    def _persist_user_toggles(self):
        """同步 user_tool_toggles 到全局 Settings(保证新窗口默认值更新)"""
        Settings.get_instance().tool_toggles.value = dict(self._user_tool_toggles)
        Settings.get_instance().save()

    # ===================================================================
    #  智能体激活 / 恢复
    # ===================================================================

    def apply_agent(self, agent_name: str, agent_tools: Optional[Dict[str, bool]] = None,
                    agent_permission: Optional[Dict[str, Any]] = None):
        """激活智能体命令:把 active_* 替换为 agent 的工具权限

        Args:
            agent_name: 智能体名
            agent_tools: agent.tools 字典(白名单,可为 None)
            agent_permission: agent.permission 字典(allow/deny/ask 规则,可为 None)
        """
        from app.core.agent import PermissionResolver

        agent_tools = agent_tools or {}
        agent_permission = agent_permission or {}

        # 用 PermissionResolver 解析所有已知工具的最终权限
        try:
            resolver = PermissionResolver(agent_permission, {}, agent_tools)
        except Exception as e:
            logger.warning(f"[ToolPermission] 创建 resolver 失败: {e},agent 权限将被忽略")
            resolver = None

        all_tools = list(DANGEROUS_TOOLS) + list(SAFE_TOOLS)
        new_toggles: Dict[str, bool] = {}
        has_ask = False
        has_deny = False

        for tool in all_tools:
            if resolver is None:
                result = "allow"
            else:
                try:
                    result = resolver.resolve(tool)
                except Exception:
                    result = "allow"
            if result == "allow":
                new_toggles[tool] = True
            else:
                new_toggles[tool] = False
                if result == "ask":
                    has_ask = True
                elif result == "deny":
                    has_deny = True

        # 聚合 behavior:ask 优先于 deny
        if has_ask:
            new_behavior = "ask"
        elif has_deny:
            new_behavior = "deny"
        else:
            new_behavior = "deny"  # 全部 allow 时,理论上 behavior 不影响

        self._active_tool_toggles = new_toggles
        self._active_tool_off_behavior = new_behavior
        self._active_agent_name = agent_name

        enabled_count = sum(1 for v in new_toggles.values() if v)
        logger.info(
            f"[ToolPermission] apply_agent: {agent_name}, "
            f"enabled={enabled_count}/{len(new_toggles)}, behavior={new_behavior}"
        )

        self.togglesChanged.emit(self.get_toggles())
        self.behaviorChanged.emit(self.get_behavior())
        self.activeAgentChanged.emit(agent_name)

    def restore_user(self):
        """恢复用户偏好(取消智能体激活)"""
        self._active_tool_toggles = dict(self._user_tool_toggles)
        self._active_tool_off_behavior = self._user_tool_off_behavior
        self._active_agent_name = None

        logger.info("[ToolPermission] restored user permissions")

        self.togglesChanged.emit(self.get_toggles())
        self.behaviorChanged.emit(self.get_behavior())
        self.activeAgentChanged.emit("")  # 空字符串 = 用户模式

    # ===================================================================
    #  状态复制(用于窗口复制/分支)
    # ===================================================================

    def copy_state_from(self, other: "ToolPermissionController"):
        """从另一个 controller 复制完整状态(用于窗口复制/分支)"""
        self._user_tool_toggles = dict(other._user_tool_toggles)
        self._user_tool_off_behavior = other._user_tool_off_behavior
        self._active_tool_toggles = dict(other._active_tool_toggles)
        self._active_tool_off_behavior = other._active_tool_off_behavior
        self._active_agent_name = other._active_agent_name

        # 主动发射所有信号刷新 UI
        self.togglesChanged.emit(self.get_toggles())
        self.behaviorChanged.emit(self.get_behavior())
        self.activeAgentChanged.emit(self._active_agent_name or "")
        self.userTogglesChanged.emit(self.get_user_toggles())
        self.userBehaviorChanged.emit(self.get_user_behavior())
