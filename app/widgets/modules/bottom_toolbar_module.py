# -*- coding: utf-8 -*-
"""bottom_toolbar 模块 — 工具栏条/模型按钮/capsule/光晕

源 main_widget.setup_ui 3417-3450 段（独立工具栏条 + 输入区光晕 + 尾调用）。
所有产物 setattr(host, <原属性名>, <widget>)，宿主其余代码靠属性访问。

属性契约（host.setattr）：
- _bottom_toolbar_strip _model_btn_container current_model_btn
- settings_btn effort_btn _tool_toggle_btn _toolbar_capsule
- memory_btn history_btn new_session_btn _input_glow_underlay
- _model_btn_icon _model_btn_text _model_sep_name _model_sep_usage
- _settings_btn_icon _settings_effort_label _tool_danger_label
- _tool_safe_label _tool_restore_btn _bottom_toolbar_shadow
- _input_card_primary_shadow _input_card_ambient_shadow
- _current_provider_name _current_model_name _user_manually_selected_model
- _input_card_focused _input_area_collapsed _plugin_input_buttons
"""

from app.plugins.contracts.ui_module import UIModule


class BottomToolbarModule(UIModule):
    """工具栏模块——独立 strip + 模型胶囊 + 右侧功能按钮 + 输入区光晕。

    尾段（underlay 抬升 / 事件过滤 / 绝对定位 / tooltip）均为宿主方法或
    容器操作，统一在 build 尾调用；宿主方法缺失时静默跳过（测试 stub 场景）。
    """

    module_id = "bottom_toolbar"

    def build(self, host) -> None:
        from PyQt5.QtCore import Qt
        from PyQt5.QtGui import QColor
        from PyQt5.QtWidgets import (
            QGraphicsDropShadowEffect,
            QLabel,
            QPushButton,
            QHBoxLayout,
            QWidget,
        )
        from qfluentwidgets import TransparentToolButton
        from app.utils.design_tokens import Colors, font_size_css
        from app.utils.utils import get_font_family_css, get_icon
        from app.widgets.bottom_input_area import InputGlowUnderlay
        from app.widgets.simple_hover_tooltip import (
            batch_install_hover_tooltips,
            install_hover_tooltip,
        )
        from app.widgets.ui_helpers import MODEL_BTN_STYLE
        from app.main_widget import _ThemedIconLabel

        # bottom_layout 由 input_card 段创建并挂到 _bottom_input_container
        bottom_layout = host._bottom_input_container.layout()

        # ===== 独立工具栏条（钉在主窗口底部，不受 _input_card 缩放影响）=====
        # 关键：工具栏从 _input_card 中拆出，作为 _input_card 的 sibling
        # 放在主 layout 自己的容器里。这样 _input_card 缩小到 0 时，
        # 工具栏的窗口绝对坐标不变——按钮栏不出现视觉跳动。
        # 视觉上是独立第二张卡：下方圆角 + 渐变 + 边框；颜色使用专属
        # TOOLBAR_STRIP_BG/TOOLBAR_STRIP_BORDER token（与输入卡片解耦，
        # 主题可分别调控）。
        # 工具栏作为 host 的直接子控件（不放在任何 layout 里），
        # 通过 resizeEvent 绝对定位到窗口底部。这样输入卡折叠/展开时
        # 工具栏的窗口绝对 Y 坐标完全不变，不再被 VBoxLayout 推上/推下。
        host._bottom_toolbar_strip = QWidget(host)
        host._bottom_toolbar_strip.setObjectName("bottomToolbarStrip")
        host._bottom_toolbar_strip.setFixedHeight(36)
        strip_layout = QHBoxLayout(host._bottom_toolbar_strip)
        # 上下 3px 留白 + 28px 内容 = 34px，工具栏 28px 居中放置
        strip_layout.setContentsMargins(10, 4, 10, 4)
        strip_layout.setSpacing(8)

        # ===== 工具栏（现在挂在独立 strip 上）=====
        toolbar_widget = QWidget(host._bottom_toolbar_strip)
        # 28px 高度匹配 strip 内部 28px 内容区，配合 VCenter 完美居中
        toolbar_widget.setFixedHeight(28)
        toolbar_widget.setStyleSheet("background: transparent; border: none;")
        toolbar_layout = QHBoxLayout(toolbar_widget)
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_layout.setSpacing(8)
        # 内部子项统一 28px 时无需对齐；当前 26/28/28 混用 → VCenter 兜底
        toolbar_layout.setAlignment(Qt.AlignVCenter)

        # 模型选择（无边框，只保留背景）
        host._model_btn_container = QWidget(toolbar_widget)
        host._model_btn_container.setFixedHeight(26)
        Colors.refresh()
        host._model_btn_container.setStyleSheet(f"""
            background: {Colors.TOOLBAR_BG};
            border: none;
            border-radius: 8px;
        """)
        model_layout = QHBoxLayout(host._model_btn_container)
        model_layout.setContentsMargins(8, 0, 4, 0)
        model_layout.setSpacing(0)
        # 模型胶囊内竖向分隔线：把 [模型名] | [思考强度+配置] | [用量上下文] 三组分开
        host._model_sep_name = QWidget(host._model_btn_container)
        host._model_sep_name.setFixedSize(1, 16)
        host._model_sep_name.setStyleSheet(f"background: {Colors.BORDER};")
        host._model_sep_name.setAttribute(Qt.WA_TransparentForMouseEvents)
        host._model_sep_usage = QWidget(host._model_btn_container)
        host._model_sep_usage.setFixedSize(1, 16)
        host._model_sep_usage.setStyleSheet(f"background: {Colors.BORDER};")
        host._model_sep_usage.setAttribute(Qt.WA_TransparentForMouseEvents)
        host.current_model_btn = QWidget(host._model_btn_container)
        host.current_model_btn.setCursor(Qt.PointingHandCursor)
        host.current_model_btn.setStyleSheet(MODEL_BTN_STYLE)
        host.current_model_btn.mousePressEvent = lambda e: host._toggle_model_selector_card()
        btn_layout = QHBoxLayout(host.current_model_btn)
        btn_layout.setContentsMargins(2, 2, 0, 2)
        btn_layout.setSpacing(4)
        host._model_btn_icon = QLabel(host.current_model_btn)
        host._model_btn_icon.setStyleSheet("background: transparent; border: none;")
        host._model_btn_icon.setFixedSize(18, 18)
        host._model_btn_icon.setScaledContents(True)
        btn_layout.addWidget(host._model_btn_icon)
        host._model_btn_text = QLabel("正在加载...", host.current_model_btn)
        host._model_btn_text.setStyleSheet(host._get_model_btn_text_style())
        btn_layout.addWidget(host._model_btn_text)
        model_layout.addWidget(host.current_model_btn, 1)
        model_layout.addSpacing(6)
        model_layout.addWidget(host._model_sep_name)
        model_layout.addSpacing(6)
        host.settings_btn = QWidget(host._model_btn_container)
        host.settings_btn.setObjectName("settingsEffortBtn")
        host.settings_btn.setCursor(Qt.PointingHandCursor)
        host.settings_btn.setStyleSheet(f"""
            QWidget#settingsEffortBtn {{
                background: transparent;
                border: none;
                border-radius: 8px;
            }}
            QWidget#settingsEffortBtn:hover {{
                background: {Colors.HOVER_BG_STRONG};
            }}
        """)
        host.settings_btn.setToolTip("模型参数配置")
        host.settings_btn.mousePressEvent = lambda e: host._toggle_model_config_card()
        settings_btn_layout = QHBoxLayout(host.settings_btn)
        settings_btn_layout.setContentsMargins(4, 2, 6, 2)
        settings_btn_layout.setSpacing(5)
        host._settings_btn_icon = QLabel(host.settings_btn)
        host._settings_btn_icon.setFixedSize(16, 16)
        host._settings_btn_icon.setScaledContents(True)
        host._settings_btn_icon.setPixmap(get_icon("模型选择").pixmap(16, 16))
        settings_btn_layout.addWidget(host._settings_btn_icon)

        # 思考强度胶囊（独立控件，与配置卡片按钮分离）：模型支持 reasoning_effort
        # 且思考模式开启时显示当前等级；点击直接循环轮换等级（方便快速调强度）
        host.effort_btn = QWidget(host._model_btn_container)
        host.effort_btn.setObjectName("effortCycleBtn")
        host.effort_btn.setCursor(Qt.PointingHandCursor)
        host.effort_btn.setStyleSheet("""
            QWidget#effortCycleBtn {
                background: transparent;
                border: none;
            }
        """)
        host.effort_btn.setToolTip("点击切换思考强度等级")
        host.effort_btn.mousePressEvent = lambda e: host._cycle_effort_level(e)
        effort_btn_layout = QHBoxLayout(host.effort_btn)
        effort_btn_layout.setContentsMargins(0, 0, 0, 0)
        effort_btn_layout.setSpacing(0)
        host._settings_effort_label = QLabel("", host.effort_btn)
        host._settings_effort_label.setAttribute(Qt.WA_TransparentForMouseEvents)  # 点击穿透到外层轮换按钮
        host._settings_effort_label.setStyleSheet(host._get_settings_effort_style())
        effort_btn_layout.addWidget(host._settings_effort_label)
        model_layout.addWidget(host.effort_btn)
        model_layout.addWidget(host.settings_btn)
        model_layout.addWidget(host._model_sep_usage)

        # 余额/用量/上下文放入模型选择胶囊内
        model_layout.addSpacing(6)
        model_layout.addWidget(host.balance_display)
        model_layout.addWidget(host.coding_plan_ring)
        model_layout.addWidget(host.context_usage_ring)
        model_layout.addSpacing(2)

        toolbar_layout.addWidget(host._model_btn_container)

        host._current_provider_name = ""
        host._current_model_name = ""
        # #4 语义：本窗口用户是否手动选过模型（_on_model_selected_from_popup 置位）。
        # 同步跟随判定：True → 保持自身选择；False（首次加载/默认态）→ 跟随云端 SelectedModel。
        host._user_manually_selected_model = False

        # ===== 工具开关双色分段按钮 =====
        host._tool_toggle_btn = QWidget(toolbar_widget)
        host._tool_toggle_btn.setFixedHeight(26)
        host._tool_toggle_btn.setCursor(Qt.PointingHandCursor)
        Colors.refresh()
        host._tool_toggle_btn.setStyleSheet(f"""
            background: {Colors.TOOLBAR_BG};
            border: none;
            border-radius: 8px;
        """)
        host._tool_toggle_btn.mousePressEvent = lambda e: host._toggle_tool_control_card()
        tt_layout = QHBoxLayout(host._tool_toggle_btn)
        tt_layout.setContentsMargins(6, 0, 6, 0)
        tt_layout.setSpacing(0)

        # 图标（主题感知 SVG — 自动适配浅色/深色模式）
        tt_icon = _ThemedIconLabel("工具", 18, host._tool_toggle_btn)
        tt_icon.setStyleSheet("background: transparent; border: none;")
        tt_layout.addWidget(tt_icon)
        tt_layout.addSpacing(4)

        # 左：危险工具数（暗红）
        host._tool_danger_label = QLabel("0")
        host._tool_danger_label.setAlignment(Qt.AlignCenter)
        host._tool_danger_label.setFixedHeight(20)
        host._tool_danger_label.setStyleSheet(f"""
            background: {Colors.STATUS_DANGER_BG_DARK};
            color: white; font-weight: 700;
            border: none; border-top-left-radius: 4px; border-bottom-left-radius: 4px;
            padding: 0 8px;
            {font_size_css(13)} {get_font_family_css()}
        """)
        tt_layout.addWidget(host._tool_danger_label)

        # 右：安全工具数（暗绿）
        host._tool_safe_label = QLabel("0")
        host._tool_safe_label.setAlignment(Qt.AlignCenter)
        host._tool_safe_label.setFixedHeight(20)
        host._tool_safe_label.setStyleSheet(f"""
            background: {Colors.SUCCESS_DARK};
            color: white; font-weight: 700;
            border: none; border-top-right-radius: 4px; border-bottom-right-radius: 4px;
            padding: 0 8px;
            {font_size_css(13)} {get_font_family_css()}
        """)
        tt_layout.addWidget(host._tool_safe_label)

        # 恢复按钮（仅 agent 覆盖时显示，不打开卡片即可恢复）
        host._tool_restore_btn = QPushButton("↺", host._tool_toggle_btn)
        host._tool_restore_btn.setFixedSize(20, 20)
        host._tool_restore_btn.setCursor(Qt.PointingHandCursor)
        host._tool_restore_btn.setToolTip("取消 agent 覆盖，恢复用户工具权限")
        host._tool_restore_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; border: none;
                color: #ff9500; {font_size_css(13)} {get_font_family_css()}
                font-weight: bold; padding: 0;
            }}
            QPushButton:hover {{
                color: #ffb84d;
            }}
        """)
        host._tool_restore_btn.setVisible(False)
        host._tool_restore_btn.clicked.connect(lambda: host._on_tool_restore())
        tt_layout.addWidget(host._tool_restore_btn)

        # 工具权限按钮移到右侧（右对齐）
        toolbar_layout.addStretch(1)

        toolbar_layout.addWidget(host._tool_toggle_btn)

        # 右侧功能按钮组（无边框，间距加宽）
        host._toolbar_capsule = QWidget(toolbar_widget)
        host._toolbar_capsule.setFixedHeight(28)
        Colors.refresh()
        host._toolbar_capsule.setStyleSheet(f"""
            background: {Colors.TOOLBAR_BG};
            border: none;
            border-radius: 10px;
        """)
        capsule_layout = QHBoxLayout(host._toolbar_capsule)
        capsule_layout.setContentsMargins(6, 2, 6, 2)
        capsule_layout.setSpacing(4)

        Colors.refresh()
        btn_capsule_style = f"""
            TransparentToolButton {{ background: transparent; border: none; }}
            TransparentToolButton:hover {{ background: {Colors.HOVER_BG_STRONG}; border-radius: 5px; }}
        """

        # 长期记忆按钮已移除 —— 记忆功能完全迁移到右侧工作台（WorkbenchPanel 记忆页）。
        # 保留一个零尺寸锚点占位（objectName="memory"）：插件可用
        # position="before:memory" / "after:memory" 锚定按钮位置，
        # 直接删掉按钮会让这类锚点静默降级到末尾追加。
        host.memory_btn = None  # 兼容：外部可能仍持有引用
        host._memory_anchor = QWidget(host._toolbar_capsule)
        host._memory_anchor.setObjectName("memory")
        host._memory_anchor.setFixedSize(0, 0)
        capsule_layout.addWidget(host._memory_anchor)
        # hide()：hidden widget 不占布局空间也不产生 spacing（0×0 仍会引入两段
        # 4px spacing，胶囊左侧挂 14px 空白）；QLayoutItem 保留 → objectName 锚定照常
        host._memory_anchor.hide()

        # 历史会话按钮已移除 —— 历史会话完全迁移到右侧工作台（WorkbenchPanel「历史会话」页签）。
        # 保留一个零尺寸锚点占位（objectName="history"）：插件可用
        # position="before:history" / "after:history" 锚定按钮位置，
        # 直接删掉按钮会让这类锚点静默降级到末尾追加。
        host.history_btn = None  # 兼容：外部可能仍持有引用
        host._history_anchor = QWidget(host._toolbar_capsule)
        host._history_anchor.setObjectName("history")
        host._history_anchor.setFixedSize(0, 0)
        capsule_layout.addWidget(host._history_anchor)
        host._history_anchor.hide()  # 同 memory：隐藏消除孤立 spacing，锚定语义保留

        # 新建对话按钮（从右上移到右下）
        host.new_session_btn = TransparentToolButton(get_icon("新会话"), host._toolbar_capsule)
        host.new_session_btn.setFixedSize(24, 24)
        host.new_session_btn.setStyleSheet(btn_capsule_style)
        host.new_session_btn.setToolTip("新建对话")
        host.new_session_btn.setObjectName("new_session")  # Phase E：插件按钮 position 锚点
        host.new_session_btn.clicked.connect(host._create_new_session)
        capsule_layout.addWidget(host.new_session_btn)

        # 为工具栏按钮安装自绘 hover tooltip（绕开 QToolTip 样式问题）
        # 注：memory_btn / history_btn 已移除（记忆、历史会话迁移到工作台），不再参与安装
        for _tb in [host.new_session_btn]:
            install_hover_tooltip(_tb)

        # Phase D：输入区插件按钮（_init_ui_plugins_deferred 加载插件后再构建一次）
        host._plugin_input_buttons = []
        host._build_plugin_input_buttons()

        toolbar_layout.addWidget(host._toolbar_capsule)

        # 工具栏挂到独立 strip（不在 _input_card 里了）
        strip_layout.addWidget(toolbar_widget)

        host._bottom_input_container.setAttribute(Qt.WA_TranslucentBackground, True)
        # 统一胶囊光晕底层：跨越输入卡 + 工具栏整个胶囊，由 paintEvent 自绘连贯环绕光，
        # 避免两个独立 widget 各挂 QGraphicsDropShadowEffect 时光晕只走局部轮廓、
        # 接缝处互相遮挡导致"只上半弧形发光"的诡异观感。
        host._input_glow_underlay = InputGlowUnderlay(host)
        # 旧的 input_card 主光 / wrapper 环境光保留为占位但默认关闭：发光统一由 underlay 提供。
        # 之所以不直接删除，是为了保留 setGraphicsEffect 钩子，方便未来需要时复用。
        host._input_card_primary_shadow = QGraphicsDropShadowEffect(host._input_card)
        host._input_card_primary_shadow.setOffset(0, 0)
        host._input_card_primary_shadow.setBlurRadius(0)
        host._input_card_primary_shadow.setColor(QColor(0, 0, 0, 0))
        host._input_card.setGraphicsEffect(host._input_card_primary_shadow)
        host._input_card_ambient_shadow = QGraphicsDropShadowEffect(host._input_card_wrapper)
        host._input_card_ambient_shadow.setOffset(0, 0)
        host._input_card_ambient_shadow.setBlurRadius(0)
        host._input_card_ambient_shadow.setColor(QColor(0, 0, 0, 0))
        host._input_card_wrapper.setGraphicsEffect(host._input_card_ambient_shadow)
        # 工具栏自身只保留失焦态的轻微下投阴影增强"落地"感，聚焦发光交给 underlay 统一处理
        host._bottom_toolbar_shadow = QGraphicsDropShadowEffect(host._bottom_toolbar_strip)
        host._bottom_toolbar_shadow.setBlurRadius(14)
        host._bottom_toolbar_shadow.setOffset(0, 4)
        host._bottom_toolbar_shadow.setColor(QColor(0, 0, 0, 70))
        host._bottom_toolbar_strip.setGraphicsEffect(host._bottom_toolbar_shadow)
        host._input_card_focused = False
        host._input_area_collapsed = False
        host._apply_bottom_input_stack_style()

        bottom_layout.addWidget(host._input_card_wrapper)
        # 预留 36px 空间给工具栏（工具栏本身不在 layout 里，绝对定位）。
        # 输入卡 + 这 36px = 输入容器高度；工具栏钉死在窗口底部 36px，
        # 与输入容器底部对齐（输入卡隐藏时容器仍占 36px，工具栏位置不变）。
        bottom_layout.addSpacing(36)

        # 向内发光：underlay 必须在输入容器 / 工具栏 **之上** 才不会被它们
        # 的不透明背景盖住；setAttribute(Qt.WA_TranslucentBackground, True)
        # 已让鼠标事件全部穿透，不影响文本输入 / 按钮点击。
        host._input_glow_underlay.raise_()

        # 输入卡 wrapper / 容器尺寸变化 → 同步胶囊光晕底层几何
        # （输入框高度自适应、系统卡片折叠都会改它们的尺寸）
        try:
            host._input_card_wrapper.installEventFilter(host)
        except Exception:
            pass
        try:
            host._bottom_input_container.installEventFilter(host)
        except Exception:
            pass

        # 初始定位工具栏（resizeEvent 会持续更新）
        try:
            host._position_bottom_toolbar()
        except Exception:
            pass

        # 初始刷新工具开关按钮
        try:
            host._refresh_tool_toggle_btn()
        except Exception:
            pass

        # 统一安装自绘 hover tooltip，替换所有原生 QToolTip
        batch_install_hover_tooltips(host)
