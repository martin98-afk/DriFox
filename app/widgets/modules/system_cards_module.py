# -*- coding: utf-8 -*-
"""system_cards 模块 — 六张系统卡懒创建（源 main_widget.setup_ui L2979-3112）

搬运时基线：app/main_widget.py 的 setup_ui 中「六张系统卡片框架懒创建（P0-1 性能优化）」
整段（2979-3112）原样搬移；self → host，赋值显式挂回 host；import 提升到 build 内。

契约属性集（grep `self.[a-z_]+ *=` over 2979-3112，逐项 host.setattr）：
- _share_card _share_card_content
- _history_questions_card _history_questions_card_content
- _memory_card _memory_card_popup _model_config_card _model_config_popup
- _model_selector_card _model_selector_card_content
- _tool_control_card（批1 懒创建：None 占位，_ensure_tool_control_card 按需构建）
- _question_floating_widget（批1 懒创建：None 占位，_ensure_question_floating_widget 按需构建）

★ `_history_card` / `_history_popup_card` **不在**本模块契约内：历史会话页已
插件化（history-manager 插件的工作台页），二者是 `MainWidget` 上的只读代理
属性（转发插件服务），任何宿主模块都不应赋值。

★ 项目选择卡片（_project_selector_card / _project_new_edit 等）已迁入
history-manager 插件（左侧停靠区「历史会话」卡内的可折叠面板）。
host 方法依赖（build 内调用/连接，插件 override 时应保持同名）：
- _card_manager(_bottom_card_container) / _register_cards_to_manager / _system_card_ids
- _init_builtin_commands（经 QTimer.singleShot 延迟注册）
"""

from app.plugins.contracts.ui_module import UIModule


class SystemCardsModule(UIModule):
    """系统卡模块：六张系统卡片框架懒创建 + 问答浮动卡"""

    module_id = "system_cards"

    def build(self, host) -> None:
        from PyQt5.QtCore import QTimer
        # ── 六张系统卡片框架懒创建（P0-1 性能优化）──
        # 原 setup_ui 同步段直接创建 6 张 BaseSettingsCard 框架（~160ms），
        # 改为 _ensure_xxx_card() 惰性创建：deferred 链预构建 + 打开入口兜底。
        # 属性名保持稳定（None 占位），引用点已有 hasattr/getattr/if 保护。
        # ★ _history_card / _history_popup_card 不在此占位：历史会话页已插件化
        #   （history-manager），两者是 MainWidget 上的**只读代理属性**
        #   （转发插件服务），不可赋值。
        host._share_card = None
        host._share_card_content = None
        host._history_questions_card = None
        host._history_questions_card_content = None
        host._memory_card = None
        host._memory_card_popup = None
        host._model_config_card = None
        host._model_config_popup = None
        host._model_selector_card = None
        host._model_selector_card_content = None

        # 工具控制卡片（批1 懒创建）：build 期仅 None 占位，构造/接线/注册在
        # host._ensure_tool_control_card() 中按需执行（toggle 入口兜底）。
        host._tool_control_card = None

        # 模型选择卡片框架懒创建（P0-1）：见上方 _ensure_model_selector_card() 说明

        # 项目选择卡片已迁入 history-manager 插件：左侧停靠区「历史会话」卡片内的
        # 可折叠项目选择面板（复用 ProjectSelectorCardContent，首行「全部项目」）。
        # 宿主只保留项目数据与切换方法（_on_project_selected / _on_new_project_created 等），
        # 面板 UI 与信号转发见 plugins/history-manager/ui/history_page.py。

        # 问题悬浮卡（批1 懒创建）：同上，构造/接线/注册在
        # host._ensure_question_floating_widget() 中按需执行（弹出链入口兜底）。
        host._question_floating_widget = None

        # 注册卡片到 CardManager（优先级：数值越小权限越高）
        host._register_cards_to_manager()

        # 系统卡片打开时隐藏文本输入框（保留按钮栏），关闭时恢复
        # _system_card_ids 在 __init__ 顶部初始化为 _BASE_SYSTEM_CARD_IDS，
        # UI 插件注册浮动卡片后通过 register_system_card() 扩展该集合。
        for _cid in host._system_card_ids:
            host._card_manager.on_card_shown(host._window_id, _cid, lambda cid: host._on_system_card_opened(cid))
            host._card_manager.on_card_hidden(host._window_id, _cid, lambda cid: host._on_system_card_closed(cid))

        # ===== 内置命令先注册（UI 插件命令依赖 CommandManager） =====
        # [PERF] 延迟 100ms 到首帧之后注册，节省 ~200ms 关键路径时间。
        # 为什么是 100ms 而非 singleShot(0)：Qt QTimer 按到期时间排序，
        # singleShot(0) 到期时间 ≈ 创建时间，早于 main.py 中 _show_popup 的
        # singleShot(0)（创建更晚），导致 BuiltinCommands 仍在窗口显示前执行。
        # 100ms 延迟确保到期时间晚于所有 singleShot(0)，在窗口第一次绘制后注册。
