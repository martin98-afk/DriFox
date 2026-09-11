# -*- coding: utf-8 -*-
"""title_bar 模块 — 会话栏（项目/标题/右侧按钮组）

源 main_widget.py L2878-L3013（setup_ui 段，搬运时基线）。
属性契约（host.setattr，全量搬运原 self.* 赋值）：
- _project_branch_container  QFrame（项目+分支组合容器）
- _project_avatar           _SquareAvatar（项目缩写方形 icon）
- _project_label            QLabel（隐藏，仅 avatar 展示缩写）
- _branch_widget            插件 BranchChip（slot="branch" 装配；worktree-manager 缺失时为 None）
- title_edit                TitleEditWidget（行内标题编辑）
- balance_display           BalanceDisplay（余额/用量，稍后入底部工具栏）
- coding_plan_ring          CodingPlanRing（编码计划圆环）
- _coding_plan_hidden       bool（圆环初始隐藏标记）
- context_usage_ring        ContextUsageRing（上下文用量圆环）
- _history_questions_btn    TransparentToolButton（历史问题）
- _history_questions_badge  InfoBadge（问题数角标）
- _share_btn                TransparentToolButton（分享）
- diff_btn                  TransparentToolButton（差异对比）

极简化：移除 _pb_separator（▸ 三角连接符），avatar 与分支之间靠 8px 留白 + 视觉重量差分组。
容器不再 hover，avatar 与 branch 各自独立 hover（语义不同：切项目 vs 打开关键文档）。

host 方法/属性引用均经 getattr 兜底（模块对宿主弱耦合：主程序路径下解析为真实方法，
行为零变化；测试 stub 缺失时静默跳过）。
"""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QLabel
from app.plugins.contracts.ui_module import UIModule
from app.utils.utils import get_icon


class TitleBarModule(UIModule):
    """会话栏模块：项目/分支容器 + 标题编辑 + 右侧按钮组，挂入 host 根布局"""

    module_id = "title_bar"

    def build(self, host) -> None:
        from PyQt5.QtCore import Qt
        from PyQt5.QtWidgets import QFrame, QLabel, QHBoxLayout
        from qfluentwidgets import (
            TransparentToolButton,
            FluentIcon,
            InfoBadge,
            InfoBadgePosition,
        )
        from app.utils.utils import get_font_family_css
        from app.widgets.ui_helpers import TitleEditWidget, Colors, font_size_css
        from app.widgets.balance_display import BalanceDisplay
        from app.widgets.coding_plan_ring import CodingPlanRing
        from app.widgets.context_usage_ring import ContextUsageRing
        from app.widgets.cards.settings.project_selector_card import (
            extract_project_initials,
            get_project_color,
            _SquareAvatar,
        )

        # host 弱耦合回调/属性（主程序路径解析为真实方法，测试 stub 缺失则兜底）
        _current_project = getattr(host, "_current_project", "")
        on_project_label_clicked = getattr(host, "_on_project_label_clicked", None)
        refresh_branch_widget_style = getattr(host, "_refresh_branch_widget_style", lambda: None)
        refresh_project_branch_style = getattr(host, "_refresh_project_branch_style", lambda: None)
        is_duplicate_window = getattr(host, "_is_duplicate_window", False)
        source_window = getattr(host, "_source_window", None)
        copy_branch_from = getattr(host, "_copy_branch_from", lambda *_a: None)
        update_branch = getattr(host, "_update_branch", lambda: None)
        on_title_edit_finished = getattr(host, "_on_title_edit_finished", None)
        toggle_history_questions_popup = getattr(host, "_toggle_history_questions_popup", None)
        on_share_clicked = getattr(host, "_on_share_clicked", None)
        # 差异按钮已迁移到工作台产物页；保留 host.diff_btn = None 的兼容，
        # 防止外部代码仍持有引用（见 main_widget._open_diff_viewer）。
        host.diff_btn = None

        layout = host.layout()

        # 标题栏分组分隔线（1px 竖线，用主题色 DIVIDER_COLOR）
        def _make_vdivider() -> QFrame:
            div = QFrame(host)
            div.setFrameShape(QFrame.VLine)
            div.setFixedHeight(18)
            div.setFixedWidth(1)
            Colors.refresh()
            div.setStyleSheet(f"color: {Colors.DIVIDER_COLOR}; background: {Colors.DIVIDER_COLOR}; border: none;")
            return div

        session_bar_layout = QHBoxLayout()

        # ===== 项目+分支组合控件（一体感布局） =====
        # 极简风格：项目 avatar 与分支文字之间靠 8px 留白分组，不再画三角连接符。
        host._project_branch_container = QFrame(host)
        host._project_branch_container.setObjectName("projectBranchContainer")
        pb_layout = QHBoxLayout(host._project_branch_container)
        pb_layout.setContentsMargins(4, 0, 8, 0)  # 左侧贴近标题编辑区，间距由 spacing 提供
        pb_layout.setSpacing(8)  # avatar ↔ 分支的连接：纯留白

        # 项目方形 icon（缩写字母，flat design squircle 风格）
        # 26px：用户反馈 22px 偏小，调大一点（字号随 size 自适应）
        host._project_avatar = _SquareAvatar(
            extract_project_initials(_current_project), get_project_color(_current_project), host, size=26
        )
        host._project_avatar.setCursor(Qt.PointingHandCursor)
        if on_project_label_clicked is not None:
            host._project_avatar.mousePressEvent = on_project_label_clicked
        host._project_avatar.setToolTip("点击切换项目")  # tooltip 在 _update_branch() 中动态更新（含项目名/路径/分支）
        pb_layout.addWidget(host._project_avatar)

        # 项目选择标签（隐藏，仅通过 avatar icon 展示项目缩写）
        host._project_label = QLabel(_current_project, host)
        host._project_label.setCursor(Qt.PointingHandCursor)
        if on_project_label_clicked is not None:
            host._project_label.mousePressEvent = on_project_label_clicked
        host._project_label.setToolTip("点击切换项目")
        host._project_label.setVisible(False)

        # Git 分支 chip：插件 slot 装配（worktree-manager 缺失时 slot 空置，无分支标签）
        # 分支点击 = 打开工作树页关键文档，avatar 点击 = 切项目，语义各自独立。
        # 点击行为/初始 tooltip/隐藏态均已内聚在插件的 BranchChip 内。
        host._branch_widget = None
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            _tb_info = UIPluginRegistry.get_instance().get_titlebar_widget("branch")
            if _tb_info is not None:
                host._branch_widget = _tb_info.widget_factory(host)
        except Exception:
            pass
        if host._branch_widget is not None:
            pb_layout.addWidget(host._branch_widget)

        refresh_project_branch_style()
        # 性能优化：复制/分支窗口直接从源窗口复制 git 分支标签状态，
        # 跳过同步 git 子进程（最坏可达 3s），避免重复窗口出现卡顿
        if is_duplicate_window and source_window is not None:
            copy_branch_from(source_window)
        else:
            update_branch()

        # 将组合控件加入布局
        # 标题编辑（行内编辑模式）
        host.title_edit = TitleEditWidget("新对话", host)
        font_css = get_font_family_css()
        Colors.refresh()
        title_style = f"""QLabel {{
            color: {Colors.TEXT_PRIMARY};
            {font_size_css(15)}
            font-weight: bold;
            padding: 6px 4px;
            border-radius: 10px;
            background-color: transparent;
            {font_css}
        }}
        QLabel:hover {{
            background-color: {Colors.HOVER_BG};
        }}
        QLineEdit {{
            color: {Colors.TEXT_PRIMARY};
            {font_size_css(15)}
            font-weight: bold;
            padding: 6px 4px;
            border-radius: 10px;
            background-color: transparent;
            border: none;
            {font_css}
        }}
        QLineEdit:focus {{
            background-color: {Colors.TOOLBAR_BG};
            border: 1px solid {Colors.BORDER};
        }}
    """
        host.title_edit.setStyleSheet(title_style)
        if on_title_edit_finished is not None:
            host.title_edit.returnPressed.connect(on_title_edit_finished)
            host.title_edit.editingFinished.connect(on_title_edit_finished)

        session_bar_layout.addWidget(host._project_branch_container)

        # 标题栏分组分隔线：[项目▸分支] │ [标题]
        session_bar_layout.addWidget(_make_vdivider())

        session_bar_layout.addWidget(host.title_edit, 1)  # 占据剩余空间

        # 先创建余额/用量/上下文组件（稍后添加到底部工具栏，模型选择右侧）
        host.balance_display = BalanceDisplay(host)
        host.coding_plan_ring = CodingPlanRing(host)
        # 圆环隐藏状态（ring 初始隐藏）：_on_coding_plan_result 据此判断
        # 是否打"无数据"日志，避免多标签页下无数据广播刷屏
        host._coding_plan_hidden = True
        host.context_usage_ring = ContextUsageRing(host)

        # 标题栏右侧：分享按钮 + 当前会话历史问题按钮（替代时间线节点）
        right_layout = QHBoxLayout()
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(2)
        right_layout.setAlignment(Qt.AlignVCenter)

        # 历史问题按钮（点击弹窗显示当前会话所有用户提问，支持快速跳转）
        host._history_questions_btn = TransparentToolButton(FluentIcon.MESSAGE, host)
        host._history_questions_btn.setFixedSize(28, 28)
        host._history_questions_btn.setToolTip("当前会话的用户提问历史")
        if toggle_history_questions_popup is not None:
            host._history_questions_btn.clicked.connect(toggle_history_questions_popup)
        right_layout.addWidget(host._history_questions_btn)
        # 右上角 InfoBadge，显示用户问题总数（自动跟随按钮位置）
        host._history_questions_badge = InfoBadge.attension(
            0, parent=host, target=host._history_questions_btn, position=InfoBadgePosition.LEFT
        )
        host._history_questions_badge.setVisible(False)

        # 分享按钮
        host._share_btn = TransparentToolButton(FluentIcon.SHARE, host)
        host._share_btn.setFixedSize(28, 28)
        host._share_btn.setToolTip("分享当前对话")
        if on_share_clicked is not None:
            host._share_btn.clicked.connect(on_share_clicked)
        right_layout.addWidget(host._share_btn)

        # 差异对比按钮已迁移到工作台产物页（见 WorkbenchPanel.diff_requested），
        # 此处不再创建 diff_btn，避免按钮无处不在。
        right_layout.addSpacing(8)  # 右侧留白

        session_bar_layout.addLayout(right_layout)
        layout.addLayout(session_bar_layout)
