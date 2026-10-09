# -*- coding: utf-8 -*-
"""
服务商编辑卡片 - 将弹窗改为卡片形式（保留文字标签）
"""

import threading

import requests
from loguru import logger
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    InfoBarPosition,
    LineEdit,
    PrimaryPushButton,
)

from app.constants import (
    get_merged_provider_models,
    provider_default_config,
)
from app.plugins.registries.provider_registry import ProviderRegistry
from app.utils.design_tokens import Colors, font_size_css
from app.utils.provider_icons import get_provider_icon
from app.utils.provider_ui_meta import get_auth_type, get_preset_urls
from app.utils.utils import get_font_family_css
from app.widgets.cards.settings.provider_setting_card import ProviderIconWidget
from app.widgets.model_list_edit_dialog import ModelListEditorWidget
from app.widgets.searchable_editable_combobox import SearchableEditableComboBox


def _is_text_chat_model(model_id: str) -> bool:
    """判断模型是否为文本聊天模型，过滤掉图片、音频、词嵌入等非文本模型"""
    if not model_id:
        return False
    model_lower = model_id.lower()
    non_text_keywords = [
        # 图片生成/视觉模型
        "dall-e",
        "dalle",
        "stable-diffusion",
        "sd-",
        "imagen",
        "flux",
        "image",
        "diffusion",
        "kandinsky",
        "midjourney",
        "wan",
        "vision",
        "vl",
        "llava",
        "seance",
        "cogview",
        "cogvideo",
        "pixart",
        "visual",
        # 音频模型
        "whisper",
        "tts",
        "speech",
        "audio",
        "piper",
        "voice",
        # 词嵌入模型
        "embedding",
        "embed",
        "text-embedding",
        "bge",
        # 其他非聊天模型
        "moderation",
        "rerank",
        "search",
        "retrieval",
    ]
    for keyword in non_text_keywords:
        if keyword in model_lower:
            return False
    return True


def fetch_provider_models(api_url: str, api_key: str, provider_name: str, auth_type: str = "bearer") -> tuple:
    """Fetch model list from provider API.

    Returns ``(text_chat_models, filtered_out_models, status)``：

    - ``text_chat_models``：通过关键词规则的对话模型
    - ``filtered_out_models``：被关键词规则过滤掉的非对话模型（UI 展示、可点回）
    - ``status``：``"ok"`` 成功且有模型 / ``"empty"`` 成功但无模型 /
      ``"auth_failed"`` 401/403 / ``"unreachable"`` 网络异常或超时

    status 供定时刷新服务判定「健康检查」结果（三元组向后兼容：旧调用方按
    二元组解包会忽略第三项）。
    """
    headers = {"Authorization": f"Bearer {api_key}"} if auth_type == "bearer" else {}

    urls_to_try = []
    if provider_name == "DeepSeek":
        urls_to_try = [f"{api_url.rstrip('/')}/models"]
    else:
        urls_to_try = [
            f"{api_url.rstrip('/')}/models",
            f"{api_url.rstrip('/')}/v1/models",
        ]

    last_error = ""
    last_status = "unreachable"
    for url in urls_to_try:
        try:
            response = requests.get(url, headers=headers, timeout=10)

            if response.status_code == 200:
                data = response.json()

                if isinstance(data, dict):
                    if "data" in data:
                        all_models = [
                            m.get("id") or m.get("name", "") or m.get("model", "")
                            for m in data["data"]
                            if isinstance(m, dict)
                        ]
                    elif "models" in data:
                        all_models = [
                            m.get("id") or m.get("name", "") or m.get("model", "")
                            for m in data["models"]
                            if isinstance(m, dict)
                        ]
                    else:
                        all_models = []
                elif isinstance(data, list):
                    all_models = data
                else:
                    all_models = []

                filtered = [m for m in all_models if m and _is_text_chat_model(m)]
                removed = [m for m in all_models if m and not _is_text_chat_model(m)]
                return filtered, removed, ("ok" if (filtered or removed) else "empty")

            last_error = f"HTTP {response.status_code}"
            last_status = "auth_failed" if response.status_code in (401, 403) else "unreachable"
        except Exception as e:
            last_error = str(e)

    logger.warning(f"[ProviderEditCard] All attempts failed. Last error: {last_error}")
    return [], [], last_status


class ProviderEditCard(QWidget):
    """服务商编辑卡片 - 紧凑设计"""

    # 信号
    saved = pyqtSignal(str, dict)  # provider_name, provider_info
    closed = pyqtSignal()
    fetchSuccess = pyqtSignal(list)  # 获取成功信号
    fetchFailed = pyqtSignal(str)  # 获取失败信号（失败原因，空串=无内容）
    loginSuccess = pyqtSignal(str, str)  # 登录成功（api_key, info）
    loginFailed = pyqtSignal(str)  # 登录失败（原因）

    def __init__(
        self,
        provider_name: str = "",
        provider_info: dict = None,
        is_new: bool = True,
        parent=None,
        preset_provider: str = "",
    ):
        super().__init__(parent)
        self.provider_name = provider_name
        self.provider_info = (provider_info or {}).copy()
        self.is_new = is_new
        # 预置服务商（卡片墙选中的服务商名）：is_new 下用它锁定服务商与预填参数，
        # 用户只需补 API_KEY。空串 = 走原手工表单流程。
        self.preset_provider = str(preset_provider or "")
        self._original_info = (provider_info or {}).copy()
        self._fetched_models = []
        self._filtered_out_models = []
        # 手动刷新的状态元数据（ts/status），保存时随 payload 落盘供列表行状态点用
        self._fetch_meta: dict = {}
        self._auto_config_name = ""
        # 收集 SearchableEditableComboBox 引用用于刷新
        self._searchable_combos = []
        self._init_ui()

        # 连接信号
        self.fetchSuccess.connect(self._on_fetch_success)
        self.fetchFailed.connect(self._on_fetch_failed)
        self.loginSuccess.connect(self._on_login_success)
        self.loginFailed.connect(self._on_login_failed)

    def _static_provider_row(self, provider_name: str) -> QWidget:
        """预置态的静态服务商展示行：图标 + 加粗名 + 「预置服务商」小标。

        对齐卡片墙 tile 与列表卡的 icon+文字语言（用户反馈：禁用灰下拉观感差）。
        """
        box = QWidget(self)
        box.setStyleSheet("background: transparent;")
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        row.addWidget(ProviderIconWidget(provider_name, 28), 0, Qt.AlignVCenter)

        name = BodyLabel(provider_name)
        name.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; {font_size_css(14)} font-weight: 600; {get_font_family_css()}"
        )
        row.addWidget(name, 0, Qt.AlignVCenter)

        tag = BodyLabel("预置服务商")
        tag.setStyleSheet(f"color: {Colors.TEXT_MUTED}; {font_size_css(11)}; {get_font_family_css()}")
        row.addWidget(tag, 0, Qt.AlignVCenter)
        return box

    def _init_ui(self):
        self._apply_style()

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(4, 6, 4, 6)
        main_layout.setSpacing(6)

        # 连接配置区域
        # 服务商名称行
        current_provider = self.provider_name if not self.is_new else None
        template_url = ""
        if self.is_new:
            name_row = QHBoxLayout()
            # 服务商名称标签 - 固定宽度右对齐
            name_label = BodyLabel("服务商:")
            name_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            name_row.addWidget(name_label)
            self.nameCombo = SearchableEditableComboBox()
            self._searchable_combos.append(self.nameCombo)
            self.nameCombo.setMaxVisibleItems(10)
            for provider_name in ProviderRegistry.get_instance().names():
                icon = get_provider_icon(provider_name)
                self.nameCombo.addItem(provider_name, icon=icon)
            self.nameCombo.setDisabled(False)
            # ⚠ blockSignals 包裹：预置态下 setCurrentIndex 会触发 _on_provider_changed，
            # 而它内部会把 configNameEdit 重写成「服务商名」并重载预设 URL，把预填洗掉
            # （本项最大的翻车点，改动前先想清楚信号链）。
            self.nameCombo.blockSignals(True)
            if self.preset_provider:
                idx = self.nameCombo.findText(self.preset_provider)
                self.nameCombo.setCurrentIndex(idx if idx >= 0 else 0)
            else:
                self.nameCombo.setCurrentIndex(0)
            self.nameCombo.blockSignals(False)
            self.nameCombo.currentTextChanged.connect(self._on_provider_changed)

            if self.preset_provider:
                # 预置态：**静态展示行**替代禁用灰下拉（用户反馈 disabled combo
                # 无法点击、无 icon，观感像坏掉的控件）。
                # combo 保留但隐藏 —— 保存链（_on_save / _sync_login_btn /
                # _update_extra_config_visibility）都要读 currentText()，删控件会断链。
                self.nameCombo.setVisible(False)
                name_row.addWidget(self._static_provider_row(self.preset_provider))
                name_row.addStretch(1)
            else:
                name_row.addWidget(self.nameCombo, 1)

            main_layout.addLayout(name_row)
            first_provider = self.nameCombo.currentText()
            if self.preset_provider:
                # 预置态：预填 URL / 认证方式 / 默认模型，仅 API_KEY 可编辑
                from app.widgets.cards.settings.provider_picker_card import preset_provider_summary

                summary = preset_provider_summary(self.preset_provider) or {}
                self.provider_info = {
                    "API_URL": summary.get("API_URL", ""),
                    "API_KEY": self.provider_info.get("API_KEY", ""),
                    "模型名称": summary.get("模型名称", ""),
                    "认证方式": summary.get("认证方式", ""),
                }
                template = dict(self.provider_info)
                template_url = summary.get("API_URL", "")
            else:
                template = provider_default_config(first_provider) or {}
                template_url = template.get("API_URL", "")
            current_provider = first_provider
        else:
            if provider_default_config(self.provider_name) is not None:
                template = provider_default_config(self.provider_name) or {}
            else:
                template = self.provider_info
            current_provider = self.provider_name
            template_url = template.get("API_URL", "")
            name_row = QHBoxLayout()
            name_row.addWidget(BodyLabel("服务商:"))
            name_row.addWidget(ProviderIconWidget(self.provider_name, 24))
            name_row.addWidget(BodyLabel(self.provider_name))
            name_row.addStretch(1)
            main_layout.addLayout(name_row)

        # 配置名称行（紧跟服务商名称行）
        config_name_row = QHBoxLayout()
        config_name_label = BodyLabel("配置名称:")
        config_name_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        config_name_row.addWidget(config_name_label)
        self.configNameEdit = LineEdit()
        # 如果是编辑模式，且 provider_info 中有 name 字段，则填充
        if not self.is_new and "name" in self.provider_info:
            self.configNameEdit.setText(self.provider_info["name"])
        else:
            # 新建时，默认使用服务商名称
            if self.is_new:
                self._auto_config_name = self.nameCombo.currentText()
                self.configNameEdit.setText(self._auto_config_name)
            else:
                self.configNameEdit.setText(self.provider_name)
        config_name_row.addWidget(self.configNameEdit, 1)
        main_layout.addLayout(config_name_row)

        # API URL 行
        url_row = QHBoxLayout()
        url_label = BodyLabel("API URL:")
        url_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        url_row.addWidget(url_label)
        self.apiUrlCombo = SearchableEditableComboBox()
        self._searchable_combos.append(self.apiUrlCombo)
        # 传入当前服务商名称和模板URL，加载预设URL列表
        self._load_preset_urls(provider_name=current_provider, template_url=template_url)
        current_url = self.provider_info.get("API_URL", template_url)
        if current_url:
            existing_items = [self.apiUrlCombo.itemText(i) for i in range(self.apiUrlCombo.count())]
            if current_url not in existing_items:
                self.apiUrlCombo.addItem(current_url)
            idx = self.apiUrlCombo.findText(current_url)
            if idx >= 0:
                self.apiUrlCombo.setCurrentIndex(idx)
            else:
                self.apiUrlCombo.setCurrentText(current_url)
        if self.is_new and self.preset_provider:
            # 预置态：URL 由插件声明给定，禁改（避免用户误改 endpoint 后无法调用）。
            # ⚠ 必须限 is_new：编辑态 preset_provider 无意义，若也走只读会把
            # 用户编辑既有配置的 URL 锁死（R6-4 发现的真 bug）。
            self.apiUrlCombo.setDisabled(True)
        url_row.addWidget(self.apiUrlCombo, 1)
        main_layout.addLayout(url_row)

        # API Key 行
        key_row = QHBoxLayout()
        key_label = BodyLabel("API Key:")
        key_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        key_row.addWidget(key_label)
        self.apiKeyEdit = LineEdit()
        self.apiKeyEdit.setEchoMode(QLineEdit.Password)
        current_key = self.provider_info.get("API_KEY", template.get("API_KEY", ""))
        if current_key:
            self.apiKeyEdit.setText(current_key)
        key_row.addWidget(self.apiKeyEdit, 1)
        # 「获取 API KEY」（打开注册页）与「登录」（OAuth 类）互斥显示，同在 Key 行
        self.getKeyBtn = PrimaryPushButton("获取 API KEY")
        self.getKeyBtn.clicked.connect(self._open_register_url)
        key_row.addWidget(self.getKeyBtn)
        self.loginBtn = PrimaryPushButton("登录")
        self.loginBtn.clicked.connect(self._on_auto_login)
        self.loginBtn.setVisible(False)
        key_row.addWidget(self.loginBtn)
        main_layout.addLayout(key_row)

        # ── 模型管理区（波8 重构：combo + 「编辑列表」按钮 → 常驻列表编辑器）──
        # 旧形态是「下拉选一个默认模型 + 点按钮展开列表编辑器」，两处割裂；
        # 新形态只留一个常驻编辑器：行首★=默认模型，列表即「模型列表」。
        # 保存链：model = getDefaultModel()、models = get_models()（见 _on_save）。
        current_model = self.provider_info.get("模型名称", template.get("模型名称", ""))
        saved_models = self.provider_info.get("模型列表", [])
        initial_models: list = list(saved_models) if isinstance(saved_models, list) else []
        if not initial_models:
            # 无存档列表 → 取词典（插件声明 + models.dev），与旧 combo 行为一致
            merged_provider_models = get_merged_provider_models()
            key = self.nameCombo.currentText() if self.is_new else self.provider_name
            if key in merged_provider_models:
                initial_models = list(merged_provider_models[key])
            elif not self.is_new and provider_default_config(self.provider_name) is not None:
                dm = (provider_default_config(self.provider_name) or {}).get("模型名称", "")
                if dm:
                    initial_models = [dm]
        if current_model and current_model not in initial_models:
            initial_models.append(current_model)

        model_row = QHBoxLayout()
        model_row.addWidget(BodyLabel("模型管理:"))

        self.fetchBtn = PrimaryPushButton("获取模型列表")
        self.fetchBtn.clicked.connect(self._on_fetch_models)
        model_row.addWidget(self.fetchBtn)
        model_row.addStretch(1)
        main_layout.addLayout(model_row)

        # 模型列表编辑器（常驻可见；旧代码是点「编辑列表」才显示）
        self.modelListEditor = ModelListEditorWidget(
            initial_models, parent=self, default_model=str(current_model or "")
        )
        self.modelListEditor.setVisible(True)
        main_layout.addWidget(self.modelListEditor)

        # 自动刷新 / 健康检查（per-provider 双开关，随配置落盘）
        # ⚠ 红线：开关值走 build_provider_save_plan 的 form_values（bool 直传），
        # **绝不进 extra_fields** —— collect_extra_fields 对编辑器调 .text()，
        # SwitchButton 没有该方法，必炸 AttributeError。
        from qfluentwidgets import SwitchButton

        switch_row = QHBoxLayout()
        auto_label = BodyLabel("自动刷新模型")
        auto_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        switch_row.addWidget(auto_label)
        self.autoRefreshSwitch = SwitchButton()
        self.autoRefreshSwitch.setChecked(bool(self.provider_info.get("自动刷新模型", False)))
        self.autoRefreshSwitch.setToolTip("每 24 小时拉取一次模型列表；默认模型仍可用时静默更新")
        switch_row.addWidget(self.autoRefreshSwitch)
        switch_row.addSpacing(16)
        health_label = BodyLabel("健康检查")
        health_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        switch_row.addWidget(health_label)
        self.healthCheckSwitch = SwitchButton()
        self.healthCheckSwitch.setChecked(bool(self.provider_info.get("健康检查", False)))
        self.healthCheckSwitch.setToolTip("每 24 小时探测一次可用性，只更新状态不修改模型列表")
        switch_row.addWidget(self.healthCheckSwitch)
        switch_row.addStretch(1)
        main_layout.addLayout(switch_row)

        # 双开关行（后面是套餐用量区）

        # 套餐用量查询额外配置（可选）— 由 providers 插件声明（ProviderDef.extra_quota_fields）
        self._extra_config_section = QWidget()
        extra_layout = QVBoxLayout(self._extra_config_section)
        extra_layout.setContentsMargins(4, 2, 0, 4)
        extra_layout.setSpacing(6)

        # 小标题
        section_title = BodyLabel("套餐用量查询（可选）")
        extra_layout.addWidget(section_title)

        # (provider_name, config_key) -> row widget；字段定义全部来自插件
        self._extra_field_rows: dict = {}

        registry = ProviderRegistry.get_instance()
        for provider in registry.all():
            for field in provider.extra_quota_fields:
                config_key = field.key
                row_widget = QWidget()
                row_layout = QHBoxLayout(row_widget)
                row_layout.setContentsMargins(0, 0, 0, 0)
                lbl = BodyLabel(field.label)
                lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                row_layout.addWidget(lbl)
                editor = LineEdit()
                editor.setPlaceholderText(field.placeholder)
                existing = self.provider_info.get(config_key, "")
                if existing:
                    editor.setText(existing)
                edit_attr = f"quota_edit_{provider.name}_{config_key}"
                setattr(self, edit_attr, editor)
                row_layout.addWidget(editor, 1)
                extra_layout.addWidget(row_widget)
                self._extra_field_rows[(provider.name, config_key)] = (row_widget, edit_attr)

        main_layout.addWidget(self._extra_config_section)

        # 初始可见性由当前服务商决定
        self._update_extra_config_visibility()

        # 底部弹性空间：将所有内容推到上方
        main_layout.addStretch(1)

        # 保存按钮已移到 BaseSettingsCard 标题栏，信号由外部连接

        # 新建时调用一次初始化
        if self.is_new:
            self._on_provider_changed(self.nameCombo.currentText())
        else:
            # 编辑模式无 provider 切换，直接按当前服务商联动三按钮显隐
            self._sync_login_btn()

    def _apply_style(self):
        """应用主题感知的基础样式"""
        Colors.refresh()
        self.setStyleSheet(f"""
            QWidget {{
                background: transparent;
            }}
            QLineEdit {{
                background-color: {Colors.CONTENT_BG};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 4px;
                padding: 4px 8px;
                {get_font_family_css()}
                font-size: {font_size_css(12)};
            }}
            QLineEdit:focus {{
                border-color: {Colors.INPUT_FOCUS_BORDER};
            }}
        """)

    def refresh_style(self):
        """主题切换时刷新编辑卡片样式 + 子 SearchableEditableComboBox"""
        self._apply_style()
        for combo in self._searchable_combos:
            try:
                combo.refresh_style()
            except RuntimeError:
                pass
        editor = getattr(self, "modelListEditor", None)
        if editor is not None:
            try:
                editor.refresh_style()
            except RuntimeError:
                pass

    def _load_preset_urls(self, provider_name: str = None, template_url: str = ""):
        """加载预设的 API URL 端点

        候选来源单一化：全部走 app.utils.provider_ui_meta.get_preset_urls
        （插件 preset_urls 声明 → api_url → 默认配置兜底）。原先的 10 个
        elif 硬编码链已删除——它与插件声明双源且已漂移（火山插件声明
        api/coding/v3，硬编码只给 api/v3；阿里云/智谱各 2 个 URL 也只在一处）。
        """
        preset_urls = get_preset_urls(provider_name) if provider_name else []

        all_urls = list(dict.fromkeys(preset_urls + [template_url]))

        self.apiUrlCombo.blockSignals(True)
        self.apiUrlCombo.clear()
        self.apiUrlCombo.addItems(all_urls)
        self.apiUrlCombo.blockSignals(False)

    def _on_provider_changed(self, name: str):
        """服务商变化时更新预设值"""
        # 过滤缓存属于上一个服务商，切换后作废
        self._filtered_out_models = []
        if self.is_new and hasattr(self, "configNameEdit"):
            current_config_name = self.configNameEdit.text()
            if not current_config_name.strip() or current_config_name == self._auto_config_name:
                self.configNameEdit.setText(name)
                self._auto_config_name = name
        if provider_default_config(name) is not None:
            template = provider_default_config(name) or {}
            template_url = template.get("API_URL", "")
            self._load_preset_urls(name, template_url)

            preset_url = template.get("API_URL", "")
            existing_items = [self.apiUrlCombo.itemText(i) for i in range(self.apiUrlCombo.count())]
            if preset_url and preset_url in existing_items:
                idx = self.apiUrlCombo.findText(preset_url)
                self.apiUrlCombo.setCurrentIndex(idx)
            else:
                self.apiUrlCombo.setCurrentText(preset_url)

            merged_provider_models = get_merged_provider_models()
            # 切服务商 → 主列表重建为该服务商词典（默认取首项），候选区跟随刷新。
            # （旧实现是刷 combo 下拉；现在是「主列表 + 候选区」双区模型）
            models = list(merged_provider_models.get(name, []))
            default_model = template.get("模型名称", "")
            if default_model and default_model not in models:
                models.append(default_model)
            self.modelListEditor.setDefaultModel(default_model if default_model else "")
            self.modelListEditor.set_models(models)
            if not default_model and models:
                self.modelListEditor.setDefaultModel(models[0])
        # 候选区跟随当前服务商（词典 ∪ 已拉取 ∪ 残留，排除主列表已有的）
        try:
            target_provider = self.nameCombo.currentText() if self.is_new else self.provider_name
            self.modelListEditor.refresh_candidates(target_provider, self._fetched_models)
        except Exception:
            pass
        self._update_extra_config_visibility()

    def _update_extra_config_visibility(self):
        """根据当前服务商名称显示/隐藏套餐配置区（字段由插件声明）"""
        if not hasattr(self, "_extra_config_section"):
            return
        provider = self.nameCombo.currentText() if self.is_new else self.provider_name

        # 当前服务商的额外字段定义（providers 插件）
        current_fields = {}
        registry = ProviderRegistry.get_instance()
        p = registry.get(provider)
        if p is not None:
            current_fields = {f.key for f in p.extra_quota_fields}

        # 先全部隐藏
        for row_widget, _ in self._extra_field_rows.values():
            row_widget.setVisible(False)

        # 只显示当前服务商声明的字段行
        shown = 0
        for (pname, config_key), (row_widget, _) in self._extra_field_rows.items():
            if pname == provider and config_key in current_fields:
                row_widget.setVisible(True)
                shown += 1

        self._extra_config_section.setVisible(shown > 0)
        self._sync_login_btn()

    def _sync_login_btn(self):
        """按服务商 capabilities 联动三按钮：OAuth 类（有 login_hook）只显「登录」并
        隐藏「获取模型列表」（无 /models REST，用内置模型表）；普通类只显「获取 API KEY」"""
        if not hasattr(self, "loginBtn"):
            return
        provider = self.nameCombo.currentText() if self.is_new else self.provider_name
        p = ProviderRegistry.get_instance().get(provider)
        has_login = bool(p and p.capabilities.get("login_hook"))
        self.loginBtn.setVisible(has_login)
        self.getKeyBtn.setVisible(not has_login)
        # 有专属 models_hook 的 OAuth 类（如 CodeBuddy）仍可获取；纯内置表则隐藏。
        # fetchBtn 在 loginBtn 之后创建，构造期触发本函数时可能尚未存在
        fetch_btn = getattr(self, "fetchBtn", None)
        if fetch_btn is not None:
            has_models_hook = bool(p and p.capabilities.get("models_hook"))
            fetch_btn.setVisible(has_models_hook or not has_login)

    def _open_register_url(self):
        """打开当前服务商的注册/取 Key 页面"""
        name = self.nameCombo.currentText() if self.is_new else self.provider_name
        self._open_help_url(name)

    def _on_auto_login(self):
        """登录：后台执行 capabilities["login_hook"]，成功后回填 API Key 输入框"""
        provider = self.nameCombo.currentText() if self.is_new else self.provider_name
        p = ProviderRegistry.get_instance().get(provider)
        hook = p.capabilities.get("login_hook") if p else None
        if hook is None:
            return
        self.loginBtn.setEnabled(False)
        from qfluentwidgets import InfoBar
        from app.widgets.tab_manager_window import TabManagerWindow

        parent = TabManagerWindow.get_instance() or self.window()
        InfoBar.info(
            "登录",
            "已打开浏览器等待授权，完成后自动回填（最长等待 5 分钟）",
            parent=parent,
            duration=6000,
            position=InfoBarPosition.BOTTOM,
        )
        threading.Thread(target=self._do_login_thread, args=(hook,), daemon=True).start()

    def _do_login_thread(self, hook):
        """后台执行 login_hook（可长阻塞：轮询浏览器授权结果）"""
        try:
            result = hook()
        except Exception as e:  # noqa: BLE001 —— 失败原因需透传到 UI
            self.loginFailed.emit(str(e))
            return
        if not result or not result.get("api_key"):
            self.loginFailed.emit("登录结果为空")
            return
        self.loginSuccess.emit(result["api_key"], result.get("info", ""))

    def _on_login_success(self, api_key: str, info: str):
        """登录成功（主线程）：回填 API Key 输入框，保存时走原生加密链"""
        self.loginBtn.setEnabled(True)
        self.apiKeyEdit.setText(api_key)
        from qfluentwidgets import InfoBar
        from app.widgets.tab_manager_window import TabManagerWindow

        parent = TabManagerWindow.get_instance() or self.window()
        InfoBar.success(
            "登录成功",
            info or "API Key 已回填，请保存配置",
            parent=parent,
            duration=4000,
            position=InfoBarPosition.BOTTOM,
        )

    def _on_login_failed(self, reason: str):
        """登录失败（主线程）"""
        self.loginBtn.setEnabled(True)
        from qfluentwidgets import InfoBar
        from app.widgets.tab_manager_window import TabManagerWindow

        parent = TabManagerWindow.get_instance() or self.window()
        InfoBar.error(
            "登录失败",
            reason[:120],
            parent=parent,
            duration=5000,
            position=InfoBarPosition.BOTTOM,
        )

    def _open_help_url(self, name: str):
        """打开帮助链接"""
        default_cfg = provider_default_config(name)
        if default_cfg:
            import webbrowser

            url = default_cfg.get("获取地址", "")
            if url:
                webbrowser.open(url)

    def _on_fetch_models(self):
        """获取模型列表"""
        from qfluentwidgets import InfoBar
        from app.widgets.tab_manager_window import TabManagerWindow

        parent = TabManagerWindow.get_instance() or self.window()
        api_url = self.apiUrlCombo.currentText().strip()
        api_key = self.apiKeyEdit.text().strip()
        provider_name = self.nameCombo.currentText() if self.is_new else self.provider_name

        # capabilities["models_hook"]：服务商自定义获取（不依赖输入框 URL，
        # 但需要当前表单值——如 CodeBuddy 按 API_KEY 里的 refresh_token 换 token）
        p = ProviderRegistry.get_instance().get(provider_name)
        hook = p.capabilities.get("models_hook") if p else None
        if hook is not None:
            self.fetchBtn.setEnabled(False)
            InfoBar.info("获取中", "正在获取模型列表...", parent=parent, duration=3000, position=InfoBarPosition.BOTTOM)
            hook_config = {
                "API_URL": api_url,
                "API_KEY": api_key,
                "模型名称": self.modelListEditor.getDefaultModel().strip(),
                # 认证方式问插件声明（百度千帆是 bce），写死 bearer 会让签名走错分支；
                # 未注册的服务商回落到存档值，再兜底 bearer
                "认证方式": get_auth_type(provider_name, self.provider_info.get("认证方式", "")),
            }
            threading.Thread(target=self._do_fetch_thread, args=(hook, hook_config), daemon=True).start()
            return

        if not api_url or (not api_key and str(self.provider_info.get("认证方式", "") or "").lower() != "none"):
            # 本地服务商（认证方式 none，如 Ollama / LM Studio）不需要 key，
            # 只要求 URL；其余服务商仍必须两者齐全。
            InfoBar.warning(
                "提示", "请先填写 API URL 和 Key", parent=parent, duration=2000, position=InfoBarPosition.BOTTOM
            )
            return

        self.fetchBtn.setEnabled(False)
        InfoBar.info("获取中", "正在获取模型列表...", parent=parent, duration=3000, position=InfoBarPosition.BOTTOM)

        def do_fetch():
            return fetch_provider_models(api_url, api_key, provider_name)

        thread = threading.Thread(target=self._do_fetch_thread, args=(do_fetch,))
        thread.daemon = True
        thread.start()

    def _do_fetch_thread(self, fetch_func, *args):
        """在后台线程中获取模型。

        异常必须转成 fetchFailed 信号：线程内异常若逃逸会静默杀死线程，
        fetchBtn 停在禁用态且无任何提示（表现为「一直卡住」）。
        """
        import time

        time.sleep(0.1)
        try:
            result = fetch_func(*args)
        except Exception as e:  # noqa: BLE001 —— 失败原因需透传到 UI
            logger.warning(f"[ProviderEditCard] 获取模型列表失败: {e}")
            self.fetchFailed.emit(str(e))
            return
        # 内置 fetch 返回 (models, filtered_out) 元组；插件 models_hook 返回纯列表
        if isinstance(result, tuple):
            models, removed = (list(result[0]), list(result[1]) if len(result) > 1 else [])
        else:
            models, removed = list(result), []
        self._fetched_models = models
        self._filtered_out_models = removed
        if models:
            self.fetchSuccess.emit(models)
        else:
            self.fetchFailed.emit("")

    def _on_fetch_success(self, models: list):
        """获取成功（主线程）。

        波8 重构：不再弹「合并 / 替换」三选弹窗，而是把拉取结果**整体注入候选区**
        （与词典/被过滤项同区），由用户点击加回所需模型。理由：三选弹窗强制用户
        在「看不到当前列表」的情况下做决策，而候选区让两边的差异一目了然、可逐个取舍。

        默认模型失效判定保留（若默认不在拉取结果里，提示但不动主列表）。
        """
        self.fetchBtn.setEnabled(True)
        self._fetched_models = [str(m) for m in (models or []) if m]

        # 默认模型失效提示（保留原语义，只是不再改主列表）
        current_default = self.modelListEditor.getDefaultModel()
        if current_default and self._fetched_models and current_default not in self._fetched_models:
            try:
                from qfluentwidgets import InfoBar, InfoBarPosition
                from app.widgets.tab_manager_window import TabManagerWindow

                parent = TabManagerWindow.get_instance() or self.window()
                InfoBar.warning(
                    "默认模型不在拉取结果中",
                    f"「{current_default}」未出现在本次获取的列表里（已保留，可手动确认）",
                    parent=parent,
                    duration=4000,
                    position=InfoBarPosition.BOTTOM,
                )
            except Exception:
                pass

        # 拉取结果 → 候选区（与词典合并去重；已在主列表的排除）
        provider_key = self.nameCombo.currentText() if self.is_new else self.provider_name
        self.modelListEditor.refresh_candidates(provider_key, self._fetched_models)

        # 波7 状态元数据：在候选注入完成之后写（与刷新服务同键）
        from app.core.modelmeta.model_refresh_service import STATUS_OK, now_ts_str

        self._fetch_meta = {"ts": now_ts_str(), "status": STATUS_OK}

        if not self._fetched_models:
            try:
                from qfluentwidgets import InfoBar, InfoBarPosition
                from app.widgets.tab_manager_window import TabManagerWindow

                parent = TabManagerWindow.get_instance() or self.window()
                InfoBar.info(
                    "未获取到模型",
                    "接口返回为空，请检查服务商配置",
                    parent=parent,
                    duration=3000,
                    position=InfoBarPosition.BOTTOM,
                )
            except Exception:
                pass
            return
        try:
            from qfluentwidgets import InfoBar, InfoBarPosition
            from app.widgets.tab_manager_window import TabManagerWindow

            parent = TabManagerWindow.get_instance() or self.window()
            InfoBar.success(
                "已获取",
                f"{len(self._fetched_models)} 个模型已加入候选区（点击展开加回）",
                parent=parent,
                duration=3000,
                position=InfoBarPosition.BOTTOM,
            )
        except Exception:
            pass

    def _apply_fetched(self, models: list, mode: str = "replace"):
        """兼容入口：把拉取结果写入主列表（旧三选弹窗的合并/替换语义）。

        波8 起默认路径走 `_on_fetch_success` → 候选区；本方法保留供测试与
        插件/外部直接调用（合并 = 现有在前去重追加；替换 = 覆盖）。
        """
        new_list = list(models or [])
        if mode == "merge":
            existing = self.modelListEditor.get_models()
            new_list = existing + [m for m in new_list if m not in existing]
        self.modelListEditor.set_models(new_list)
        if not self.modelListEditor.getDefaultModel() and new_list:
            self.modelListEditor.setDefaultModel(new_list[0])

    def _on_fetch_failed(self, reason: str = ""):
        """获取失败（主线程）；reason 为插件抛出的原因，空串走通用提示"""
        self.fetchBtn.setEnabled(True)
        # 失败状态同样随保存携带（供列表行状态点显示红/灰点）
        from app.core.modelmeta.model_refresh_service import STATUS_UNREACHABLE, now_ts_str

        self._fetch_meta = {"ts": now_ts_str(), "status": STATUS_UNREACHABLE}
        from qfluentwidgets import InfoBar
        from app.widgets.tab_manager_window import TabManagerWindow

        parent = TabManagerWindow.get_instance() or self.window()
        InfoBar.error(
            "失败",
            reason[:150] if reason else "获取模型列表失败，请检查配置",
            parent=parent,
            duration=5000,
            position=InfoBarPosition.BOTTOM,
        )

    def _on_manage_models(self):
        """（已废弃）旧「编辑列表」按钮的展开/收起逻辑。

        波8 起编辑器**常驻可见**，不再需要显式切换；方法保留为空壳以防外部
        （插件/旧代码）仍有引用，调用无副作用。
        """

    def _sync_editor_to_combo(self):
        """（已废弃）旧的「编辑器 → 下拉」单向同步。

        波8 起只有一份数据源（编辑器），不存在双源漂移，故无需同步。
        """

    def _on_save(self):
        """保存。

        不再手工保留 config_id——config_id 现在由 main_widget 端基于 apikey
        的稳定 hash 计算（见 app.core.modelmeta.provider_profile.apply_provider_save），
        编辑同 apikey 始终命中同一条目，不会再产生重复。

        取值 / 判空 / 组字典 三段已抽到 provider_save_plan（纯函数可单测），
        本方法只负责 UI 交互（确认框）与发信号。
        """
        from app.core.modelmeta.provider_save_plan import build_provider_save_plan, collect_extra_fields

        provider_name = self.nameCombo.currentText() if self.is_new else self.provider_name
        # 单一数据源：编辑器（行首★为默认模型，列表即「模型列表」）
        current_models = self.modelListEditor.get_models()
        default_model = self.modelListEditor.getDefaultModel()

        # 套餐用量额外字段：显式管理键恒写入（含空串），详见 collect_extra_fields
        extra_fields = collect_extra_fields(
            provider_name,
            self._extra_field_rows.items(),
            lambda attr: getattr(self, attr, None),
        )

        plan = build_provider_save_plan(
            form_values={
                "api_url": self.apiUrlCombo.currentText(),
                "api_key": self.apiKeyEdit.text(),
                # 「模型名称」恒写含空串语义不变（plan 会 strip；空串=显式清空）
                "model": default_model,
                # 认证方式问插件声明（bce/none/anthropic…）；未注册的服务商回落到
                # 存档值，再兜底 bearer——写死 bearer 会覆盖百度千帆的 bce 签名
                "auth_type": get_auth_type(provider_name, self.provider_info.get("认证方式", "")),
                "name": self.configNameEdit.text(),
                "models": current_models,
                # 双开关（bool 直传；红线：绝不进 extra_fields —— SwitchButton 无 .text()）
                "auto_refresh": self.autoRefreshSwitch.isChecked(),
                "health_check": self.healthCheckSwitch.isChecked(),
                # 手动刷新的状态元数据（有则随保存落盘）
                "fetch_meta": self._fetch_meta,
            },
            old_info=self.provider_info,
            extra_fields=extra_fields,
        )

        if plan["confirm_clear"]:
            # 用户清空了列表但旧列表非空：确认后才允许存空（旧逻辑会静默恢复旧数据）
            from app.widgets.common_dialogs import ConfirmDialog
            from app.widgets.tab_manager_window import TabManagerWindow

            parent = TabManagerWindow.get_instance() or self.window()
            dialog = ConfirmDialog(
                title="清空模型列表",
                content="模型列表已清空。保存后该服务商将没有可选模型，确定继续？",
                confirm_text="清空并保存",
                cancel_text="返回修改",
                parent=parent,
            )
            confirmed = {}
            dialog.confirmed.connect(lambda: confirmed.update(yes=True))
            dialog.exec_()
            if not confirmed.get("yes"):
                return

        self.provider_info = plan["payload"]
        self.saved.emit(provider_name, self.provider_info)

    def _on_cancel(self):
        """取消"""
        self.closed.emit()
