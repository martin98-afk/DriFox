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

from app.tools.tool_classifier import get_all_tools, get_default_toggles
from app.utils.config import Settings

# 合法的 per-tool 关闭策略值（ask=询问用户 / deny=直接拒绝）
VALID_TOOL_POLICIES = ("deny", "ask")


def resolve_tool_off_policy(
    check_name: str,
    controller: Optional["ToolPermissionController"],
    policies: Dict[str, str],
    behavior: str,
) -> str:
    """解析工具「关闭后」的处理策略：per-tool 策略优先，缺失回退全局 behavior。

    - controller 存在：走 controller.get_tool_policy（含 active 层状态，agent 激活生效）
    - controller 不存在（API 模式）：用传入的 Settings policies 字典兜底
    - 非法值（非 deny/ask）回退全局 behavior，保证返回值只允许 deny/ask

    供 engine / subagent_worker 两处执行层共用，保证口径逐字一致。
    """
    if controller is not None:
        policy = controller.get_tool_policy(check_name)
    else:
        policy = policies.get(check_name)
    return policy if policy in VALID_TOOL_POLICIES else behavior


class ToolPermissionController(QObject):
    """工具权限控制器(per-window)"""

    # 当前生效状态变化
    togglesChanged = pyqtSignal(dict)  # 引擎读取这个来获取最新 toggles
    behaviorChanged = pyqtSignal(str)  # 关闭行为变化
    policiesChanged = pyqtSignal(dict)  # per-tool 关闭策略变化(active 层)
    activeAgentChanged = pyqtSignal(str)  # 当前激活的智能体(空字符串=用户模式)

    # 用户偏好状态变化(用于 UI 反映"用户原始设置")
    userTogglesChanged = pyqtSignal(dict)
    userBehaviorChanged = pyqtSignal(str)
    userPoliciesChanged = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        # 加载全局默认值(用户最后修改的偏好)
        settings = Settings.get_instance()
        saved_toggles = dict(settings.tool_toggles.value or {})
        if not saved_toggles:
            all_tools = get_all_tools()
            saved_toggles = get_default_toggles(all_tools)

        # 清理已删除工具的残留配置(只保留当前已知的工具)
        known_tools = set(get_all_tools())
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
                    logger.info(f"[ToolPermission] 清理已删除工具的残留开关: {sorted(stale)}")
            except Exception as e:
                logger.warning(f"[ToolPermission] 持久化清理残留开关失败: {e}")

        # 用户偏好
        self._user_tool_toggles: Dict[str, bool] = dict(cleaned_toggles)
        self._user_tool_off_behavior: str = settings.tool_off_behavior.value or "deny"
        # per-tool 关闭策略(用户偏好层)：{tool_name: "deny"|"ask"}，缺失回退全局 behavior
        saved_policies = dict(settings.tool_permission_policy.value or {})
        self._user_tool_policies: Dict[str, str] = self._clean_policies(saved_policies)
        # ★ T28：用户显式调整过的工具集合（区分"显式开启"与"默认开启"）
        # 执行层用它实现"UI 覆盖模板"：显式开启 → UI 为准放行（覆盖模板 deny）
        self._user_modified: set = set()

        # 当前生效(初始 = 用户偏好)
        self._active_tool_toggles: Dict[str, bool] = dict(self._user_tool_toggles)
        self._active_tool_off_behavior: str = self._user_tool_off_behavior
        self._active_tool_policies: Dict[str, str] = dict(self._user_tool_policies)

        # 当前激活的智能体(None = 用户模式)
        self._active_agent_name: Optional[str] = None

        # ── 监听配置外部同步刷新（仅真正的云端/外部配置同步） ──
        # 关键改动：不再监听 Settings.*.valueChanged，避免「一个 tab 编辑 → 全局
        # 信号 → 所有 tab 的 _on_settings_*_changed 被触发刷新」的跨标签广播。
        # 改为只订阅 ConfigSyncService.settingsRestored（云端/外部配置重载完成后
        # 发射，无参），各 tab 内存状态相互独立；用户编辑仍写入全局 Settings
        # （保留「最后一份」），新建 tab 启动时读取即继承，分支/复制走 copy_state_from。
        try:
            from app.core.config_sync import ConfigSyncService

            ConfigSyncService.get_instance().settingsRestored.connect(self._on_config_synced)
        except Exception:
            pass

    # ===================================================================
    #  Getters
    # ===================================================================

    def _complete_toggles(self, toggles: Dict[str, bool]) -> Dict[str, bool]:
        """补全未知工具的默认值,并清理已删除工具的残留配置

        工具被删除后,旧 toggle 还残留在配置中会导致统计数量虚高
        (例如 input box 显示的危险/安全总数比注册工具实际列表多)。
        这里统一过滤掉不在当前已注册工具集合里的工具。
        """
        all_tools = get_all_tools()
        all_tools_set = set(all_tools)
        # 清理已删除工具的残留开关
        result = {tool: enabled for tool, enabled in toggles.items() if tool in all_tools_set}
        # 补全新增工具的默认开启
        for tool in all_tools:
            if tool not in result:
                result[tool] = True
        return result

    def _clean_policies(self, policies: Dict[str, str]) -> Dict[str, str]:
        """清理 per-tool 策略：过滤已删除工具 + 非法值（仅保留 deny/ask）"""
        if not isinstance(policies, dict):
            return {}
        known_tools = set(get_all_tools())
        return {
            k: v for k, v in policies.items()
            if k in known_tools and v in VALID_TOOL_POLICIES
        }

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

    # ── per-tool 关闭策略(ask/deny) ─────────────────────────────────

    def _complete_policies(self, policies: Dict[str, str], fallback: str) -> Dict[str, str]:
        """补全所有已知工具的关闭策略：per-tool 缺失/非法回退 fallback（全局 behavior）"""
        result = {}
        for tool in get_all_tools():
            policy = policies.get(tool)
            result[tool] = policy if policy in VALID_TOOL_POLICIES else fallback
        return result

    def get_tool_policies(self) -> Dict[str, str]:
        """获取当前生效的 per-tool 关闭策略(全量补全，缺失回退 active behavior)"""
        return self._complete_policies(self._active_tool_policies, self._active_tool_off_behavior)

    def get_tool_policy(self, tool_name: str) -> str:
        """获取当前生效的单工具关闭策略：per-tool 缺失回退全局 behavior"""
        policy = self._active_tool_policies.get(tool_name)
        if policy not in VALID_TOOL_POLICIES:
            return self._active_tool_off_behavior
        return policy

    def get_active_tool_behavior_map(self) -> Dict[str, str]:
        """当前生效的「工具 → 关闭策略」完整映射（行渲染与"未统一"判定共用）

        所有已知工具均有值：per-tool 策略优先，缺失回退 active behavior。
        保证 UI 行内下拉与右上角全局下拉的聚合口径一致。
        """
        return self.get_tool_policies()

    def get_user_tool_policies(self) -> Dict[str, str]:
        """获取用户偏好的 per-tool 关闭策略(全量补全，缺失回退 user behavior)"""
        return self._complete_policies(self._user_tool_policies, self._user_tool_off_behavior)

    def get_user_tool_policy(self, tool_name: str) -> str:
        """获取用户偏好的单工具关闭策略：per-tool 缺失回退全局 behavior"""
        policy = self._user_tool_policies.get(tool_name)
        if policy not in VALID_TOOL_POLICIES:
            return self._user_tool_off_behavior
        return policy

    def get_active_agent_name(self) -> Optional[str]:
        """获取当前激活的智能体名(None=用户模式)"""
        return self._active_agent_name

    def is_agent_active(self) -> bool:
        return self._active_agent_name is not None

    def is_user_modified(self, tool_name: str) -> bool:
        """该工具是否被用户显式调整过（T28：UI 覆盖模板的判定依据）。

        返回 True 表示用户在该会话/窗口中明确开启或关闭过此工具——
        执行层据此让 UI 优先于模板（显式开启 → 放行覆盖模板 deny）。
        """
        return tool_name in self._user_modified

    # ===================================================================
    #  用户编辑(只更新 user)
    # ===================================================================

    def set_user_toggle(self, tool_name: str, enabled: bool):
        """用户编辑单个开关:
        - 非 agent 模式:同时更新 user(偏好,持久化) 和 active(生效)
        - agent 模式:只更新 active(临时修改 agent 生效权限,user 偏好不变)
        """
        # ★ T28：记录用户显式调整（两种模式都算——UI 覆盖模板的判定依据）
        self._user_modified.add(tool_name)
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
        # ★ T28：逐个记录用户显式调整
        self._user_modified.update(toggles.keys())
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

    # ── per-tool 关闭策略编辑 ─────────────────────────────────────────

    def set_user_tool_policy(self, tool_name: str, policy: str):
        """用户编辑单个工具的关闭策略（ask/deny）：
        - 非 agent 模式:同时更新 user(偏好,持久化) 和 active(生效)
        - agent 模式:只更新 active(临时修改 agent 生效权限,user 偏好不变)

        ★ MAJOR-1：策略修改不写入 _user_modified——只改策略时开关仍 off，
        执行层走 per-tool 策略分支（ask→询问/deny→拒绝），模板 deny 不被绕过；
        用户显式拨开关才进 _user_modified（T28 UI 覆盖模板判定集）。
        """
        if policy not in VALID_TOOL_POLICIES:
            return
        if self.is_agent_active():
            # agent 模式:只改 active,user 偏好不变
            self._active_tool_policies[tool_name] = policy
            self.policiesChanged.emit(self.get_tool_policies())
        else:
            # 用户模式:user 和 active 同步
            self._user_tool_policies[tool_name] = policy
            self._active_tool_policies[tool_name] = policy
            self._persist_user_policies()
            self.policiesChanged.emit(self.get_tool_policies())
            self.userPoliciesChanged.emit(self.get_user_tool_policies())

    def set_user_tool_policies(self, policies: Dict[str, str]):
        """批量更新工具关闭策略（用于右上角"未统一"强制统一）

        ★ MAJOR-1：同 set_user_tool_policy,不写入 _user_modified。
        """
        valid = {k: v for k, v in policies.items() if v in VALID_TOOL_POLICIES}
        if not valid:
            return
        if self.is_agent_active():
            # agent 模式:只改 active
            self._active_tool_policies.update(valid)
            self.policiesChanged.emit(self.get_tool_policies())
        else:
            # 用户模式:user 和 active 同步
            self._user_tool_policies.update(valid)
            self._active_tool_policies.update(valid)
            self._persist_user_policies()
            self.policiesChanged.emit(self.get_tool_policies())
            self.userPoliciesChanged.emit(self.get_user_tool_policies())

    def _persist_user_policies(self):
        """同步 user_tool_policies 到全局 Settings(保证新窗口默认值更新)"""
        Settings.get_instance().tool_permission_policy.value = dict(self._user_tool_policies)
        Settings.get_instance().save()

    def _persist_user_toggles(self):
        """同步 user_tool_toggles 到全局 Settings(保证新窗口默认值更新)"""
        Settings.get_instance().tool_toggles.value = dict(self._user_tool_toggles)
        Settings.get_instance().save()

    # ===================================================================
    #  智能体激活 / 恢复
    # ===================================================================

    def apply_agent(
        self,
        agent_name: str,
        agent_tools: Optional[Dict[str, bool]] = None,
        agent_permission: Optional[Dict[str, Any]] = None,
    ):
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

        all_tools = get_all_tools()
        new_toggles: Dict[str, bool] = {}
        new_policies: Dict[str, str] = {}
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
                # per-tool 关闭策略：ask→ask / deny→deny（allow 工具不写，回退聚合 behavior）
                if result in VALID_TOOL_POLICIES:
                    new_policies[tool] = result
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
        self._active_tool_policies = new_policies
        self._active_agent_name = agent_name

        enabled_count = sum(1 for v in new_toggles.values() if v)
        logger.info(
            f"[ToolPermission] apply_agent: {agent_name}, "
            f"enabled={enabled_count}/{len(new_toggles)}, behavior={new_behavior}"
        )

        self.togglesChanged.emit(self.get_toggles())
        self.behaviorChanged.emit(self.get_behavior())
        self.policiesChanged.emit(self.get_tool_policies())
        self.activeAgentChanged.emit(agent_name)

    def restore_user(self):
        """恢复用户偏好(取消智能体激活)"""
        self._active_tool_toggles = dict(self._user_tool_toggles)
        self._active_tool_off_behavior = self._user_tool_off_behavior
        self._active_tool_policies = dict(self._user_tool_policies)
        self._active_agent_name = None

        logger.info("[ToolPermission] restored user permissions")

        self.togglesChanged.emit(self.get_toggles())
        self.behaviorChanged.emit(self.get_behavior())
        self.policiesChanged.emit(self.get_tool_policies())
        self.activeAgentChanged.emit("")  # 空字符串 = 用户模式

    # ===================================================================
    #  Settings 外部变更响应（配置同步后自动刷新）
    # ===================================================================

    def _on_config_synced(self):
        """配置外部同步（ConfigSyncService.settingsRestored）后刷新用户偏好。

        settingsRestored 无参，统一从全局 Settings 重新读取三件套并在本控制器内刷新。
        仅响应真正的云端/外部配置重载，不再因兄弟 tab 的本地编辑而广播刷新。

        行为：
        - 始终更新 _user_*（用户偏好）
        - 非 agent 模式下同步更新 _active_*（当前生效）
        - agent 模式下只更新 _user_*，不覆盖智能体权限
        """
        settings = Settings.get_instance()
        new_toggles = settings.tool_toggles.value
        new_behavior = settings.tool_off_behavior.value
        new_policies = settings.tool_permission_policy.value

        # ── toggles ──
        if isinstance(new_toggles, dict):
            cleaned = self._complete_toggles(new_toggles)
            if cleaned != self._user_tool_toggles:
                self._user_tool_toggles = cleaned
                if not self.is_agent_active():
                    self._active_tool_toggles = dict(cleaned)
                    self.togglesChanged.emit(self.get_toggles())
                self.userTogglesChanged.emit(self.get_user_toggles())
                logger.info("[ToolPermission] 用户偏好已从配置同步刷新(toggles)")

        # ── behavior ──
        if new_behavior in ("deny", "ask") and new_behavior != self._user_tool_off_behavior:
            self._user_tool_off_behavior = new_behavior
            if not self.is_agent_active():
                self._active_tool_off_behavior = new_behavior
                self.behaviorChanged.emit(new_behavior)
            self.userBehaviorChanged.emit(new_behavior)
            logger.info("[ToolPermission] 关闭行为偏好已从配置同步刷新")

        # ── policies ──
        if isinstance(new_policies, dict):
            cleaned = self._clean_policies(new_policies)
            if cleaned != self._user_tool_policies:
                self._user_tool_policies = cleaned
                if not self.is_agent_active():
                    self._active_tool_policies = dict(cleaned)
                    self.policiesChanged.emit(self.get_tool_policies())
                self.userPoliciesChanged.emit(self.get_user_tool_policies())
                logger.info("[ToolPermission] 关闭策略偏好已从配置同步刷新")

        # ── 清理已被云端配置移除的工具名:避免 _user_modified 残留旧键 ──
        # 云端配置若替换/删减了 _user_tool_toggles 的键(工具增删),旧工具名
        # 仍留在 _user_modified 中会导致 is_user_modified() 误判为"用户显式调整过"。
        if self._user_modified:
            self._user_modified &= set(self._user_tool_toggles.keys())

    # ===================================================================
    #  状态复制(用于窗口复制/分支)
    # ===================================================================

    def copy_state_from(self, other: "ToolPermissionController"):
        """从另一个 controller 复制完整状态(用于窗口复制/分支)"""
        self._user_tool_toggles = dict(other._user_tool_toggles)
        self._user_tool_off_behavior = other._user_tool_off_behavior
        self._user_tool_policies = dict(other._user_tool_policies)
        self._active_tool_toggles = dict(other._active_tool_toggles)
        self._active_tool_off_behavior = other._active_tool_off_behavior
        self._active_tool_policies = dict(other._active_tool_policies)
        self._active_agent_name = other._active_agent_name
        # ★ T28：同步用户显式调整集合（分支窗口行为一致）
        self._user_modified = set(other._user_modified)

        # 主动发射所有信号刷新 UI
        self.togglesChanged.emit(self.get_toggles())
        self.behaviorChanged.emit(self.get_behavior())
        self.policiesChanged.emit(self.get_tool_policies())
        self.activeAgentChanged.emit(self._active_agent_name or "")
        self.userTogglesChanged.emit(self.get_user_toggles())
        self.userBehaviorChanged.emit(self.get_user_behavior())
        self.userPoliciesChanged.emit(self.get_user_tool_policies())
