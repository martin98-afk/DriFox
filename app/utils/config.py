# -*- coding: utf-8 -*-
"""
全局配置管理 - 基于 qfluentwidgets QConfig

使用单例模式管理全局配置，包括：
- LLM 模型配置（API URL、模型名称、认证方式）
- 界面配置（主题、字体）
- 用户偏好配置

配置持久化到 JSON 文件。
"""
import atexit
import orjson as json

from copy import deepcopy
from enum import Enum
from loguru import logger
from qfluentwidgets import (
    ConfigSerializer,
    ConfigItem,
    QConfig,
    OptionsValidator,
    BoolValidator,
    RangeValidator,
    OptionsConfigItem,
    ConfigValidator,
    RangeConfigItem,
)


class PatchPlatform(Enum):
    GITHUB = "github"
    GITEE = "gitee"
    GITCODE = "gitcode"


class ListDictValidator(ConfigValidator):
    def correct(self, value):
        if isinstance(value, list):
            return value
        return []


class QuickComponentsSerializer(ConfigSerializer):
    def serialize(self, value):
        return value  # list[dict] 是 JSON-safe

    def deserialize(self, value):
        if isinstance(value, list):
            return value
        return []


class Settings(QConfig):
    _instance = None
    # 类级别关闭标志 — 一旦设置，任何实例的 save() 都会跳过
    _closing_down = False
    # 配置是否成功从文件加载（用于外部判断默认值与实际值的区别）
    _config_loaded = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def _set_closing_down(cls):
        """设置关闭标志，阻止所有后续写入"""
        cls._closing_down = True
        if cls._instance is not None:
            cls._instance._closing = True

    @classmethod
    def get_instance(cls):
        """获取配置实例（单例模式）"""
        if cls._instance is None:
            cls._instance = cls()
            # 配置文件路径：使用数据目录
            from app.utils.utils import get_app_data_dir
            app_data_dir = get_app_data_dir()
            cls._instance.file = app_data_dir / "app.config"
            try:
                # 在加载配置前先扩展主题选项验证器，防止保存的主题被拒绝
                # 注意：此时 PluginManager 可能未初始化，只能加载系统主题
                # 所以要把已保存的主题值也加入验证器，避免被拒绝重置
                cls._extend_theme_validator_before_load()
                cls._instance.load()
                cls._config_loaded = True  # 标记配置成功加载
                # 迁移旧格式的服务商配置
                cls._migrate_saved_providers(cls._instance)
            except Exception:
                logger.exception("无法加载配置文件")
                cls._config_loaded = False
        return cls._instance

    @classmethod
    def _migrate_saved_providers(cls, instance):
        """迁移旧格式的服务商配置：键统一为 apikey 的稳定 hash（替代旧 uuid）。

        - 旧格式 1（provider_name 为键）→ 新格式
        - 旧格式 2（uuid 为键但 value 内缺 config_id 字段）→ 补齐 config_id，
          并把 key 重映射为 apikey hash（与 value 内一致）
        - 同 apikey 的重复条目：合并为 1 条（dict 顺序中后写入者胜出）
        """
        saved_providers = instance.llm_saved_providers.value
        if not saved_providers or not isinstance(saved_providers, dict):
            return

        from app.core.provider_profile import apply_provider_save

        new_saved_providers: dict = {}
        old_to_new: dict = {}

        for old_key, info in saved_providers.items():
            if not isinstance(info, dict):
                info = {}
            api_key = info.get("API_KEY", "")
            # 构造临时表项走 apply_provider_save：
            #   1) 计算新 hash；2) 合并同 apikey 重复条目；3) 写入 config_id 字段
            tmp_info = dict(info)
            tmp_info.pop("config_id", None)  # 强制按 hash 重算
            new_key = apply_provider_save(
                new_saved_providers, tmp_info, info.get("provider_name", old_key)
            )
            old_to_new[old_key] = new_key

        # 没变化就别动磁盘
        if new_saved_providers.keys() == saved_providers.keys() and all(
            isinstance(v, dict) and v.get("config_id") == k
            for k, v in new_saved_providers.items()
        ):
            return

        instance.llm_saved_providers.value = new_saved_providers
        # 同步更新已选模型：旧 key → 新 key
        selected = instance.llm_selected_model.value
        if selected and selected in old_to_new:
            instance.llm_selected_model.value = old_to_new[selected]
        instance.save()
        logger.info(
            f"已迁移 {len(saved_providers)} 个服务商配置到 apikey hash 格式 "
            f"（合并后 {len(new_saved_providers)} 条）"
        )

    @classmethod
    def _extend_theme_validator_before_load(cls):
        """加载配置前扩展主题验证器，确保已保存的主题不会被拒绝

        此时 PluginManager 可能未初始化，只能获取系统/内置主题。
        通过直接读取配置文件中的已保存主题值，将其也加入验证器列表，
        避免 load() 时验证器拒绝未知的插件主题 ID 并重置为默认值。
        """
        try:
            from app.utils.theme_manager import theme_manager
            # 获取当前已加载的主题（可能只有系统主题）
            themes = list(theme_manager.list_themes().keys())
            if not themes:
                return

            # 直接在文件中读取已保存的主题值（不触发 load 的验证）
            if cls._instance.file and cls._instance.file.exists():
                try:
                    raw = cls._instance.file.read_text(encoding="utf-8")
                    import orjson as json
                    data = json.loads(raw)
                    saved_theme = data.get("UI", {}).get("ThemeStyle")
                    if saved_theme and saved_theme not in themes:
                        themes.append(saved_theme)  # 临时加入，防止 load 时被拒绝
                except Exception:
                    pass

            cls._instance.ui_theme_style.validator.__init__(themes)
        except Exception as e:
            import logging
            logging.warning(f"[_extend_theme_validator_before_load] failed: {e}")

    @classmethod
    def save_config(cls):
        """保存配置"""
        instance = cls.get_instance()
        instance.save()

    def set(self, item, value, save=False, copy=True):
        """set the value of config item

        Parameters
        ----------
        item: ConfigItem
            config item

        value:
            the new value of config item

        save: bool
            whether to save the change to config file

        copy: bool
            whether to deep copy the new value
        """
        # deepcopy new value
        try:
            item.value = deepcopy(value) if copy else value
        except Exception:
            item.value = value

        if save:
            self.save()

        if item.restart:
            self._cfg.appRestartSig.emit()

        if item is self._cfg.themeMode:
            self.theme = value
            self._cfg.themeChanged.emit(value)

        if item is self._cfg.themeColor:
            self._cfg.themeColorChanged.emit(value)

    def save(self):
        """save config - 关闭时不写入磁盘，防止覆盖用户粘贴的配置"""
        # 三层防护：类级别关闭标志 | 实例级别关闭标志 | app 正在退出
        if Settings._closing_down:
            return
        if getattr(self, '_closing', False):
            return
        try:
            from PyQt5.QtWidgets import QApplication
            if QApplication.closingDown():
                return
        except Exception:
            pass
        # 确保目录存在
        self.file.parent.mkdir(parents=True, exist_ok=True)
        # 写入文件
        with open(self.file, "wb") as f:
            f.write(json.dumps(self.toDict(), option=json.OPT_INDENT_2))

    # 开机自启
    auto_start = ConfigItem("General", "AutoStart", False, BoolValidator())

    # 版本信息
    current_version = "v0.2.7"
    # 通用设置
    auto_check_update = ConfigItem("General", "AutoCheckUpdate", True, BoolValidator())

    # 版本管理设置
    patch_platform = ConfigItem(
        "Patch",
        "Platform",
        "github",
        OptionsValidator([p.value for p in PatchPlatform]),
    )

    # GitHub 配置
    github_repo = "martin98-afk/DriFox"
    github_token = ConfigItem("Patch", "GitHub/Token", "")

    # ========== 大模型对话默认配置 ==========
    llm_model = ConfigItem("LLM", "Model", "qwen/qwen3-30b-a3b-2507")
    llm_api_key = ConfigItem("LLM", "APIKey", "")
    llm_api_base = ConfigItem("LLM", "APIBase", "http://127.0.0.1:1234/v1")
    llm_max_tokens = ConfigItem("LLM", "MaxTokens", 2048, RangeValidator(1024, 400960))
    llm_temperature = ConfigItem("LLM", "Temperature", 0.7, RangeValidator(0, 1))
    # 保存的免费/自定义服务商配置
    llm_saved_providers = ConfigItem("LLM", "SavedProviders",{})
    # 按模型名覆盖的参数（最大Token、温度、思考相关等），key=模型名
    llm_model_overrides = ConfigItem("LLM", "ModelOverrides", {})
    # 最近选择的模型
    llm_selected_model = ConfigItem("LLM", "SelectedModel", "")
    # 子智能体默认模型（用于 subagent_para / subagent_dag，空字符串表示使用主模型）
    llm_subagent_default_model = ConfigItem("LLM", "SubagentDefaultModel", "")
    # 启用的技能列表
    llm_enabled_skills = ConfigItem("LLM", "EnabledSkills", [
        "brainstorming", "writing-plans", "find-skills", "skill-creator", "git-commit", "minimax-image-understanding"])
    # 智能体完成通知
    llm_notify_enabled = ConfigItem("LLM", "NotifyEnabled", True, BoolValidator())
    # 桌面自动化总开关 (mouse/keyboard/screenshot 3 工具)
    # 默认禁用, 需用户在设置卡显式开启后才能被 LLM 调用
    llm_desktop_automation_enabled = ConfigItem(
        "LLM", "DesktopAutomationEnabled", True, BoolValidator()
    )
    # 通知提示音类型
    llm_notify_sound = OptionsConfigItem(
        "LLM",
        "NotifySound",
        "beep",
        OptionsValidator(["beep", "short", "none"]),
    )
    # 全局字体设置
    llm_font_family = ConfigItem("LLM", "FontFamily", "Segoe UI")

    # ========== UI appearance ==========
    ui_font_size = OptionsConfigItem(
        "UI",
        "FontSize",
        "medium",
        OptionsValidator(["small", "medium", "large", "superlarge"]),
    )
    ui_theme_style = OptionsConfigItem(
        "UI",
        "ThemeStyle",
        "fallout",
        OptionsValidator(["fallout"]),  # 运行时动态补充
    )

    # ========== 会话项目管理 ==========
    current_project = ConfigItem("Session", "CurrentProject", "默认项目")

    # ========== LLM API 服务配置 ==========
    llm_api_enabled = ConfigItem("LLM", "APIEnabled", False, BoolValidator())
    llm_api_port = RangeConfigItem(
        "LLM", "APIPort", 8765, RangeValidator(1024, 65535)
    )

    # ========== MCP 服务器配置 ==========
    mcp_servers = ConfigItem("MCP", "Servers", [], ListDictValidator())
    mcp_enabled = ConfigItem("MCP", "Enabled", True, BoolValidator())
    mcp_discovered = ConfigItem("MCP", "Discovered", False, BoolValidator())

    # ========== 插件系统配置 ==========
    enabled_plugins = ConfigItem("Plugin", "EnabledPlugins", [])

    # ========== 云组件库API ==========
    SERPAPI_KEY = ConfigItem(
        "CloudAPI",
        "SerpAPI",
        "42e2b2817bf48352d3caa227212ebb82d6f8839cdd39b304c68cf58b42961c27",
    )

    # ========== Gateway 通讯平台配置 ==========
    # 企业微信
    gateway_wecom_enabled = ConfigItem("Gateway", "WeCom/Enabled", False, BoolValidator())
    gateway_wecom_bot_id = ConfigItem("Gateway", "WeCom/BotID", "")
    gateway_wecom_secret = ConfigItem("Gateway", "WeCom/Secret", "")
    gateway_wecom_websocket_url = ConfigItem("Gateway", "WeCom/WebSocketURL", "wss://openws.work.weixin.qq.com")

    # 钉钉
    gateway_dingtalk_enabled = ConfigItem("Gateway", "DingTalk/Enabled", False, BoolValidator())
    gateway_dingtalk_client_id = ConfigItem("Gateway", "DingTalk/ClientID", "")
    gateway_dingtalk_client_secret = ConfigItem("Gateway", "DingTalk/ClientSecret", "")
    
    # Telegram
    gateway_telegram_enabled = ConfigItem("Gateway", "Telegram/Enabled", False, BoolValidator())
    gateway_telegram_token = ConfigItem("Gateway", "Telegram/Token", "")
    gateway_telegram_require_mention = ConfigItem("Gateway", "Telegram/RequireMention", True, BoolValidator())
    
    # Discord
    gateway_discord_enabled = ConfigItem("Gateway", "Discord/Enabled", False, BoolValidator())
    gateway_discord_token = ConfigItem("Gateway", "Discord/Token", "")
    gateway_discord_require_mention = ConfigItem("Gateway", "Discord/RequireMention", True, BoolValidator())
    
    # WhatsApp (Twilio)
    gateway_whatsapp_enabled = ConfigItem("Gateway", "WhatsApp/Enabled", False, BoolValidator())
    gateway_whatsapp_account_sid = ConfigItem("Gateway", "WhatsApp/AccountSID", "")
    gateway_whatsapp_auth_token = ConfigItem("Gateway", "WhatsApp/AuthToken", "")
    gateway_whatsapp_from_number = ConfigItem("Gateway", "WhatsApp/FromNumber", "")
    
    # 飞书
    gateway_feishu_enabled = ConfigItem("Gateway", "Feishu/Enabled", False, BoolValidator())
    gateway_feishu_app_id = ConfigItem("Gateway", "Feishu/AppID", "")
    gateway_feishu_app_secret = ConfigItem("Gateway", "Feishu/AppSecret", "")
    
    # Slack
    gateway_slack_enabled = ConfigItem("Gateway", "Slack/Enabled", False, BoolValidator())
    gateway_slack_bot_token = ConfigItem("Gateway", "Slack/BotToken", "")
    gateway_slack_app_token = ConfigItem("Gateway", "Slack/AppToken", "")

    # ========== Gitee 图床配置 ==========
    gitee_enabled = ConfigItem("Gitee", "Enabled", True, BoolValidator())
    gitee_token = ConfigItem("Gitee", "Token", "a5dcb6e2e7776143b7a7e7685a1f33a3")
    gitee_owner = ConfigItem("Gitee", "Owner", "dingmama123141")
    gitee_repo = ConfigItem("Gitee", "Repo", "canvas-mind-components")
    gitee_path = ConfigItem("Gitee", "Path", "drifox")
    gitee_branch = ConfigItem("Gitee", "Branch", "master")

    # ========== LSP 配置 ==========
    lsp_auto_diagnose = ConfigItem("LSP", "AutoDiagnose", False, BoolValidator())

    # ========== 工具开关控制 ==========
    tool_toggles = ConfigItem("Tools", "Toggles", {})
    tool_off_behavior = ConfigItem("Tools", "OffBehavior", "deny")



def update_theme_options():
    """从 ThemeManager 动态更新主题选项验证器"""
    try:
        from app.utils.theme_manager import theme_manager
        themes = list(theme_manager.list_themes().keys())
        if themes:
            settings = Settings.get_instance()
            settings.ui_theme_style.validator.__init__(themes)
            if settings.ui_theme_style.value not in themes:
                settings.ui_theme_style.value = themes[0]
    except Exception as e:
        import logging
        logging.warning(f"[update_theme_options] failed: {e}")
# 注册解释器退出时关闭配置写入保护
atexit.register(Settings._set_closing_down)
