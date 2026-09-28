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
from copy import deepcopy
from enum import Enum
import uuid

import orjson as json
from loguru import logger

from app.utils.secret_store import MODE_KEYRING, MODE_NONE, MODE_PASSWORD

from qfluentwidgets import (
    BoolValidator,
    ConfigItem,
    ConfigSerializer,
    ConfigValidator,
    OptionsConfigItem,
    OptionsValidator,
    QConfig,
    RangeConfigItem,
    RangeValidator,
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




# OpenCode Zen 插件历史声明过的 default_model 值（插件演进过程中用过的默认模型）。
# 用途：老配置若「模型名称」仍是这些值之一，说明用户从未手动切换过模型，
# 可在插件升级默认模型后安全地随之升级；用户改成白名单外的值（自己选的模型）
# 一律不碰，避免覆盖用户选择。
#
# 维护：providers 插件每次修改 ProviderDef(default_model=...) 时，把旧值追加到此集合。
_LEGACY_OPENCODE_DEFAULT_MODELS = frozenset(
    {
        "deepseek-v4-flash-free",  # 初版默认（2026-09 实测已下线，服务端报 Model is unavailable）
    }
)


class Settings(QConfig):
    _instance = None
    # 类级别关闭标志 — 一旦设置，任何实例的 save() 都会跳过
    _closing_down = False
    # 配置是否成功从文件加载（用于外部判断默认值与实际值的区别）
    _config_loaded = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            # 密钥模式运行时状态（见 secret_mode / _apply_secret_mode）
            cls._instance._secrets_locked = False  # 密码模式下密钥未解锁
            cls._instance._cipher_backup = {}  # config_id → 密文（locked 期间落盘回写用）
            cls._instance._secret_password = ""  # 本次会话已解锁的密码（仅内存）
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
                # 确保内置 OpenCode 免费默认配置存在
                cls._ensure_default_opencode_provider(cls._instance)
            except Exception:
                logger.exception("无法加载配置文件")
                cls._config_loaded = False
            # 把 qfluentwidgets 全局 qconfig 指向本单例
            # 原因：SwitchSettingCard / SwitchButton 等内置控件内部用 qconfig.set(item, value)
            # 写入 item.value 后调 qconfig.save()；若 qconfig._cfg 仍是默认 QConfig 实例，
            # save() 会写到错误的 config/config.json，导致 UI 改动不持久化。
            # 替换后，qconfig.set / save / toDict 都走 Settings 单例，写入真正的 app.config。
            try:
                from qfluentwidgets import qconfig

                qconfig._cfg = cls._instance
            except Exception:
                pass
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
        # 密码模式未解锁：API_KEY 仍是密文（AES-GCM nonce 随机 → 密文每次不同），
        # 此时按 hash 重算 config_id 会让 id 每次启动漂移，导致服务商条目与
        # 已选模型映射错乱 → 整段跳过，解锁后由 unlock_secrets 补跑。
        if getattr(instance, "secrets_locked", False):
            logger.info("[_migrate_saved_providers] 密钥未解锁，跳过 config_id 重算")
            return

        from app.core.modelmeta.provider_profile import apply_provider_save

        new_saved_providers: dict = {}
        old_to_new: dict = {}

        for old_key, info in saved_providers.items():
            if not isinstance(info, dict):
                info = {}
            # 构造临时表项走 apply_provider_save：
            #   1) 计算新 hash；2) 合并同 apikey 重复条目；3) 写入 config_id 字段
            tmp_info = dict(info)
            tmp_info.pop("config_id", None)  # 强制按 hash 重算
            new_key = apply_provider_save(new_saved_providers, tmp_info, info.get("provider_name", old_key))
            old_to_new[old_key] = new_key

        # 没变化就别动磁盘
        if new_saved_providers.keys() == saved_providers.keys() and all(
            isinstance(v, dict) and v.get("config_id") == k for k, v in new_saved_providers.items()
        ):
            return

        instance.llm_saved_providers.value = new_saved_providers
        # 同步更新已选模型：旧 key → 新 key
        selected = instance.llm_selected_model.value
        if selected and selected in old_to_new:
            instance.llm_selected_model.value = old_to_new[selected]
        instance.save()
        logger.info(
            f"已迁移 {len(saved_providers)} 个服务商配置到 apikey hash 格式 （合并后 {len(new_saved_providers)} 条）"
        )

    @classmethod
    def _ensure_default_opencode_provider(cls, instance):
        """确保内置 OpenCode 免费默认配置存在（免 key 匿名调用）。

        - 防重复：若 saved_providers 中已有同 name 的配置，不再注入。
        - 用户手动删除后，下次启动会自动恢复。
        - 历史迁移：老版本注入过内置共享 key 的配置（含 legacy key），
          启动时自动清空 API_KEY 升级为免 key 配置（无共享 key 概念后，
          内置 key 已失效且免 key 端点不接受假 key）。
        """
        from app.constants import provider_default_config
        from app.core.modelmeta.provider_profile import compute_provider_config_id

        provider_name = "OpenCode Zen"
        # 数据源从硬编码 FREE_PROVIDERS 迁移到 providers 插件（OpenCode Zen 插件注册表）
        default_config = provider_default_config(provider_name)
        if not default_config:
            return

        api_url = default_config.get("API_URL", "")
        config_name = "opencode免费模型"

        saved_providers = instance.llm_saved_providers.value
        if not isinstance(saved_providers, dict):
            saved_providers = {}

        # 已存在同名配置：标记已注入（用户可改名/替换 key 来隐藏或自定义）
        #
        # 附带幂等迁移：若该配置的「模型名称」仍是插件历史默认值（用户从未
        # 手动切换过模型），则升级为插件当前声明的 default_model。修复的是
        # 「插件换了默认模型，老用户配置仍指向已下线模型」的问题——旧默认值
        # 下线后这类配置每次启动都拿不到可用默认，且异步刷新只回填「模型列表」
        # 不碰「模型名称」。用户自行选过的模型（白名单外）一律保持不动。
        declared_model = str(default_config.get("模型名称", "") or "")
        for config_id, info in saved_providers.items():
            if not isinstance(info, dict):
                continue
            if info.get("name") != config_name:
                continue
            instance.llm_default_opencode_injected.value = True
            current_model = str(info.get("模型名称", "") or "")
            if declared_model and current_model and current_model != declared_model:
                if current_model in _LEGACY_OPENCODE_DEFAULT_MODELS:
                    # deepcopy 后再写回：直接原地修改不会触发 valueChanged 信号
                    upgraded = deepcopy(saved_providers)
                    upgraded[config_id]["模型名称"] = declared_model
                    instance.llm_saved_providers.value = upgraded
                    instance.save()
                    logger.info(
                        f"OpenCode 默认配置模型已升级: {current_model} -> {declared_model} ({config_id})"
                    )
            return

        provider_info = {
            "provider_name": provider_name,
            "name": config_name,
            "API_URL": api_url,
            "API_KEY": "",  # 免 key 匿名调用：空 key 走剥离 Authorization 头逻辑
        }
        # 「模型名称」不在此写死 —— 由下方 default_config 注入插件声明的
        # ProviderDef.default_model（单一数据源在 providers 插件，本处只做消费）。
        # 不写 模型列表 —— 空列表会让模型选择器显示为空，
        # 不写此键则回退到 merged_provider_models（插件模型 + models.dev + 异步刷新），
        # 等异步刷新完成后才写入实际列表。
        # 继承 providers 插件默认配置中的其他默认参数（模型名/温度/最大Token/认证方式等）
        for key, value in default_config.items():
            if key not in provider_info:
                provider_info[key] = value

        config_id = compute_provider_config_id(provider_info)
        provider_info["config_id"] = config_id
        saved_providers[config_id] = provider_info

        instance.llm_saved_providers.value = saved_providers
        instance.llm_default_opencode_injected.value = True
        instance.save()
        logger.info(f"已自动注入默认 OpenCode 免费服务商配置: {config_name} ({config_id})")

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
        # 与 qfluentwidgets 的 QConfig.set() 保持一致：值未变化时不触发信号或写盘
        if item.value == value:
            return

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
        if getattr(self, "_closing", False):
            return
        try:
            from PyQt5.QtWidgets import QApplication

            if QApplication.closingDown():
                return
        except Exception:
            pass
        # 确保目录存在
        self.file.parent.mkdir(parents=True, exist_ok=True)
        # toDict() 内层值与 item.value 共享引用，必须深拷贝后剥钥，
        # 否则会污染内存态导致 UI 回显 / API 请求丢 key
        data = deepcopy(self.toDict())
        mode = str(self.secret_mode.value or MODE_KEYRING)
        if mode != MODE_NONE:
            try:
                from app.utils.secret_store import SecretStore, seal_secrets, strip_secrets

                if mode == MODE_PASSWORD:
                    # 密码模式：明文加密落盘；locked 期间用备份密文原样回写
                    seal_secrets(data, self._secret_password, self._cipher_backup, kdf_salt=self._password_kdf_salt())
                else:
                    strip_secrets(data, SecretStore(), mode=mode)
            except Exception:
                logger.exception("[SecretStore] 密钥剥出失败，本次按明文落盘")
        # 写入文件
        with open(self.file, "wb") as f:
            f.write(json.dumps(data, option=json.OPT_INDENT_2))

    def load(self):
        """load config，加载后按 secret_mode 回填服务商 API_KEY。

        必须在 _migrate_saved_providers（get_instance 中紧随 load 调用）之前
        完成：config_id 是 (API_URL, API_KEY) 的 hash，回填晚了会算错 hash。
        """
        super().load()
        try:
            self._migrate_secret_mode()
            self._apply_secret_mode()
        except Exception:
            logger.exception("[SecretStore] 密钥回填失败，按文件值继续")

    def _recover_flat_secrets_from_keyring(self):
        """一次性回迁：v0.5.11 keyring 化误剥的扁平 token 从凭证库迁回 app.config。

        背景：扁平 ConfigItem（Gitee OAuth token / GitHub token）的 value 是
        不可变 str，toDict 外壳上的回填写不回 item.value，导致 Gitee 绑定
        token 丢失、被迫重新绑定；且用户决策 Gitee token 不参与 keyring 加密
        （其云同步面由 config_sync 上传剔除/下载合并覆盖）。本方法把凭证库
        残留条目取回内存并落盘，然后删除凭证库条目，彻底退出 keyring 范围。
        """
        from app.utils.secret_store import LEGACY_FLAT_ACCOUNTS, SecretStore

        store = SecretStore()
        recovered = False
        for account in LEGACY_FLAT_ACCOUNTS:
            back = store.get(account)
            if not back:
                continue
            item = {
                "gitee/user_token": self.gitee_user_token,
                "gitee/user_refresh_token": self.gitee_user_refresh_token,
                "github/patch_token": self.github_token,
            }.get(account)
            if item is not None and not str(item.value or ""):
                item.value = back
                recovered = True
            store.delete(account)
        if recovered:
            self.save()
            logger.info("[SecretStore] 已从凭证库回迁扁平 token 至 app.config")

    # ── 密钥加密模式：keyring（本机凭证库）/ password（密码加密，随配置同步）/ none（明文） ──

    def _migrate_secret_mode(self):
        """旧布尔项 UseSystemKeyring → 新模式项 SecretMode（一次性迁移）。

        文件已存在 SecretMode 则以文件为准；缺失时按旧布尔项推导并落盘固化，
        保证老用户升级后行为不变（关闭过 keyring 的仍是明文模式）。
        """
        if self.secret_mode.value not in (MODE_KEYRING, MODE_PASSWORD, MODE_NONE):
            self.secret_mode.value = MODE_KEYRING
        if self._file_has_secret_mode():
            return
        self.secret_mode.value = MODE_NONE if self.use_system_keyring.value is False else MODE_KEYRING
        self.save()

    def _file_has_secret_mode(self) -> bool:
        """配置文件原始 JSON 中是否已存在 General.SecretMode 键"""
        try:
            with open(self.file, "rb") as f:
                raw = json.loads(f.read() or b"{}")
        except Exception:
            return False
        return "SecretMode" in (raw.get("General") or {})

    def _apply_secret_mode(self):
        """按 secret_mode 回填密钥（load 与解锁后复用同一入口）"""
        mode = str(self.secret_mode.value or MODE_KEYRING)
        self._secrets_locked = False
        if mode == MODE_NONE:
            return
        from app.utils.secret_store import (
            MASTER_PASSWORD_ACCOUNT,
            SecretStore,
            collect_ciphertexts,
            unwrap_secrets,
        )

        store = SecretStore()
        # toDict(serialize=False) 外壳是新 dict、内层是 item.value 原引用，
        # unwrap_secrets 就地改内层即写回内存态（仅对 dict 类 value 有效）
        data = self.toDict(serialize=False)
        if mode == MODE_PASSWORD:
            password = store.get(MASTER_PASSWORD_ACCOUNT)
            self._secret_password = password
            # 解密前先备份密文：解密失败会置空，落盘靠这份备份原样回写
            self._cipher_backup = collect_ciphertexts(data)
            unwrap_secrets(data, store, mode=mode, password=password)
            self._secrets_locked = self._has_locked_cipher()
            if self._secrets_locked:
                logger.info("[SecretStore] 密码模式：本机无记住的密码或解密失败，等待用户解锁")
            else:
                # 自动解锁成功（本机记住的密码可用）：备份密文只在 locked 期间用于
                # 原样回写，解锁后留着会让「是否存在未解密密文」的判断失准 —— 设置卡
                # 据此显示「等待解锁」，表现为每次启动都误报未解锁（明文实际已就绪）。
                self._cipher_backup = {}
            return
        unwrap_secrets(data, store, mode=mode)
        self._recover_flat_secrets_from_keyring()

    def _has_locked_cipher(self) -> bool:
        """是否存在「有密文备份但内存为空」的条目（即未解开的密钥）"""
        if not self._cipher_backup:
            return False
        saved = self.llm_saved_providers.value
        if not isinstance(saved, dict):
            return False
        for cfg_id in self._cipher_backup:
            info = saved.get(cfg_id)
            if isinstance(info, dict) and not str(info.get("API_KEY") or ""):
                return True
        return False

    def _password_kdf_salt(self) -> str:
        """密码模式批量 KDF salt（持久化复用，避免每次保存重复付 scrypt；nonce 仍每次随机）"""
        value = str(self.secret_kdf_salt.value or "")
        if not value:
            import secrets

            value = secrets.token_hex(16)
            self.secret_kdf_salt.value = value
        return value

    @property
    def secrets_locked(self) -> bool:
        """密码模式下密钥是否未解锁（True 时应提示用户输入密码）"""
        return bool(self._secrets_locked)

    def verify_secret_password(self, password: str) -> bool:
        """校验密码能否解开本机密文（无密文时恒真）"""
        from app.utils.secret_store import decrypt_secret

        for token in self._cipher_backup.values():
            try:
                decrypt_secret(token, password)
                return True
            except Exception:
                return False
        return True

    def unlock_secrets(self, password: str) -> bool:
        """用密码解锁密钥：成功则回填明文、清除 locked，返回 True"""
        if not self.verify_secret_password(password):
            return False
        from app.utils.secret_store import SecretStore, unwrap_secrets

        # locked 期间内存里是空值（密文只留在 _cipher_backup），先把密文填回
        # 内存再解密，否则 unwrap 对空值无动作、解锁后仍拿不到明文
        saved = self.llm_saved_providers.value
        if isinstance(saved, dict):
            for cfg_id, token in self._cipher_backup.items():
                info = saved.get(cfg_id)
                if isinstance(info, dict) and not str(info.get("API_KEY") or ""):
                    info["API_KEY"] = token
        self._secret_password = password
        unwrap_secrets(self.toDict(serialize=False), SecretStore(), mode=MODE_PASSWORD, password=password)
        self._cipher_backup = {}
        self._secrets_locked = False
        # 解锁后明文就绪，补跑被跳过的 config_id 迁移（此时 hash 才稳定）
        self._migrate_saved_providers(self)
        logger.info("[SecretStore] 密码模式：密钥已解锁")
        return True

    def set_secret_password(self, new_password: str, old_password: str = "") -> bool:
        """设置/修改加密密码：先用旧密码解开 locked 项，再用新密码加密落盘。

        存在未解开密文（locked）时必须给出正确的旧密码，否则拒绝——
        空密码过不了校验，密文不会被空值覆盖。
        """
        if not new_password:
            return False
        if self._cipher_backup and not self.unlock_secrets(old_password):
            return False
        self._secret_password = new_password
        self._cipher_backup = {}
        self._secrets_locked = False
        self.secret_kdf_salt.value = ""  # 换密码后换 salt（卫生习惯）
        self.save()
        return True

    def switch_secret_mode(self, new_mode: str, new_password: str = "", old_password: str = "") -> tuple[bool, str]:
        """切换加密方式（UI 唯一入口），返回 (是否成功, 提示)。

        - 切到 password：需新密码；当前有未解开密文时还需正确旧密码。
        - 从 password 切出（keyring / none）：若有未解开密文，需旧密码解开后
          才能以新形态落盘（明文都没有的话切过去只会得到空 key）。
        - 明文已在内存（keyring 已回填 / password 已解锁）时三者互切不需要重配 key。
        """
        if new_mode not in (MODE_KEYRING, MODE_PASSWORD, MODE_NONE):
            return False, "未知的加密方式"
        current = str(self.secret_mode.value or MODE_KEYRING)
        if current == new_mode:
            return True, ""

        if new_mode == MODE_PASSWORD:
            if not new_password:
                return False, "切换到密码加密需要先设置密码"
            if self._cipher_backup and not self.unlock_secrets(old_password):
                return False, "旧密码不正确，无法完成切换"
            # 先切模式再落盘：否则中间那次 save 会按旧模式把明文写进文件
            self.secret_mode.value = MODE_PASSWORD
            self._secret_password = new_password
            self._cipher_backup = {}
            self._secrets_locked = False
        else:
            # 从 password 切出：先解开 locked 项（没密码就没明文，切过去等于丢 key）
            if current == MODE_PASSWORD and self._cipher_backup and not self.unlock_secrets(old_password):
                return False, "旧密码不正确，无法解密已有密钥"
            if current == MODE_PASSWORD:
                self.forget_secret_password()
            self._secret_password = ""
            self._cipher_backup = {}
            self._secrets_locked = False
            self.secret_mode.value = new_mode

        self.save()
        return True, ""

    def remember_secret_password(self, password: str) -> bool:
        """把密码记到本机钥匙串（keyring 不可用时返回 False）"""
        from app.utils.secret_store import MASTER_PASSWORD_ACCOUNT, SecretStore

        return SecretStore().set(MASTER_PASSWORD_ACCOUNT, password)

    def forget_secret_password(self) -> None:
        """清除本机记住的密码"""
        from app.utils.secret_store import MASTER_PASSWORD_ACCOUNT, SecretStore

        SecretStore().delete(MASTER_PASSWORD_ACCOUNT)

    def reset_locked_secrets(self) -> None:
        """忘记密码兜底：清空所有已保存 API Key 与密文，转明文模式（不可恢复）"""
        saved = self.llm_saved_providers.value
        if isinstance(saved, dict):
            for info in saved.values():
                if isinstance(info, dict):
                    info["API_KEY"] = ""
        self._cipher_backup = {}
        self._secret_password = ""
        self._secrets_locked = False
        self.secret_mode.value = MODE_NONE
        self.forget_secret_password()
        self.save()
        logger.warning("[SecretStore] 已重置密码模式：所有已保存 API Key 被清空")

    # 开机自启
    auto_start = ConfigItem("General", "AutoStart", False, BoolValidator())

    # 版本信息
    current_version = "v0.6.3"
    # 通用设置
    auto_check_update = ConfigItem("General", "AutoCheckUpdate", True, BoolValidator())
    # 进入时崩溃通知：检测到上次崩溃 dump 后是否弹 InfoBar 提示用户。
    # 关闭后仍会扫描日志目录并把 .reported 标记已读（不重复扫描），
    # 仅不展示横幅；用户可手动到日志目录查看 dump 文件。
    crash_notify_on_startup = ConfigItem("General", "CrashNotifyOnStartup", True, BoolValidator())

    # 更新下载代理模式：direct=直连 / system=跟随系统 / prefix=加速前缀 / http=手动代理
    # 注意：加速前缀只作用于安装包下载；检查更新始终直连 api.github.com
    update_proxy_mode = ConfigItem(
        "Update",
        "ProxyMode",
        "direct",
        OptionsValidator(["direct", "system", "prefix", "http"]),
    )
    # 加速前缀地址（prefix 模式使用），如 https://ghfast.top/
    update_proxy_prefix = ConfigItem("Update", "ProxyPrefix", "https://ghfast.top/")
    # 手动代理地址（http 模式使用），形如 http://127.0.0.1:7890；不支持 socks5
    update_proxy_url = ConfigItem("Update", "ProxyUrl", "")

    # 单实例限制：开启后同时只允许运行一个 Drifox 实例（重启生效）
    enable_single_instance = ConfigItem("General", "EnableSingleInstance", False, BoolValidator())

    # 系统密钥存储（keyring）：开启后服务商 API Key / OAuth token 迁入 OS 凭证库，
    # app.config 落盘不含明文；关闭则回退明文落盘（与旧版一致）。
    # 注意：v0.5.12 起真正判定源是 secret_mode，本项仅用于老配置的一次性迁移。
    use_system_keyring = ConfigItem("General", "UseSystemKeyring", True, BoolValidator())

    # API Key 加密方式：keyring=系统钥匙串（本机绑定，换机需重填）/
    # password=密码加密（密文随配置同步，换机输同一密码即可解出）/ none=明文落盘
    secret_mode = ConfigItem(
        "General",
        "SecretMode",
        MODE_KEYRING,
        OptionsValidator([MODE_KEYRING, MODE_PASSWORD, MODE_NONE]),
    )

    # 密码模式批量 KDF salt（hex，持久化复用；nonce 每次随机，安全性不受影响）
    secret_kdf_salt = ConfigItem("General", "SecretKdfSalt", "")

    # 灰度开关：消息正文用纯 Qt 块级渲染器（MarkdownBlockViewer）替代 QWebEngineView。
    # 仅作用于 assistant 卡片（welcome 卡 JS 交互复杂暂不灰度）；默认关闭。
    qt_message_renderer = ConfigItem("General", "QtMessageRenderer", False, BoolValidator())

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
    # 保存的免费/自定义服务商配置
    llm_saved_providers = ConfigItem("LLM", "SavedProviders", {})
    # 默认 OpenCode 免费配置是否已注入（防止用户删除后反复自动创建）
    llm_default_opencode_injected = ConfigItem("LLM", "DefaultOpencodeInjected", False, BoolValidator())
    # 按模型名覆盖的参数（最大Token、温度、思考相关等），key=模型名
    llm_model_overrides = ConfigItem("LLM", "ModelOverrides", {})
    # 最近选择的模型
    llm_selected_model = ConfigItem("LLM", "SelectedModel", "")
    # 子智能体默认模型（用于 subagent_para，空字符串表示使用主模型）
    llm_subagent_default_model = ConfigItem("LLM", "SubagentDefaultModel", "")
    # 标题生成默认模型（用于 topic_summary，空字符串表示使用主模型）
    llm_title_gen_default_model = ConfigItem("LLM", "TitleGenDefaultModel", "")
    # 启用的技能列表
    llm_enabled_skills = ConfigItem(
        "LLM",
        "EnabledSkills",
        ["brainstorming", "visualization", "writing-plans", "find-skills", "skill-creator", "git-commit", "plugin-creator", "ui-plugin-creator"],
    )
    # 主智能体选择（单选，通过 inject_agent_identity hook 注入系统提示词）
    llm_primary_agent = ConfigItem("LLM", "PrimaryAgent", "")
    # 智能体完成通知
    llm_notify_enabled = ConfigItem("LLM", "NotifyEnabled", True, BoolValidator())
    # 桌面自动化总开关 (mouse/keyboard/screenshot 3 工具)
    # 默认禁用, 需用户在设置卡显式开启后才能被 LLM 调用
    llm_desktop_automation_enabled = ConfigItem("LLM", "DesktopAutomationEnabled", True, BoolValidator())
    # 通知提示音类型
    llm_notify_sound = OptionsConfigItem(
        "LLM",
        "NotifySound",
        "beep",
        OptionsValidator(["beep", "short", "none"]),
    )
    # 繁忙时 Enter 键行为（仅智能体运行时生效；Ctrl+Enter 恒为另一行为）
    # interject=插话发送（hook 式注入当前对话流，不停 worker）；queue=排队发送（排队卡片，结束后自动续发）
    busy_enter_behavior = OptionsConfigItem(
        "General",
        "BusyEnterBehavior",
        "interject",
        OptionsValidator(["interject", "queue"]),
    )
    # 全局字体设置
    llm_font_family = ConfigItem("LLM", "FontFamily", "楷体")

    # ========== UI appearance ==========
    # 界面字号档位：delta 键 "-5".."10"（实际字号 = 14 + delta，步进 1px）
    class _FontSizeValidator(OptionsValidator):
        """字号档位校验：旧档位键（small/medium/large/superlarge）读取时自动迁移到 delta 键"""

        _legacy = {"small": "-1", "medium": "0", "large": "2", "superlarge": "4"}

        def correct(self, value):
            if value in self._legacy:
                return self._legacy[value]
            return super().correct(value)

    ui_font_size = OptionsConfigItem(
        "UI",
        "FontSize",
        "2",
        _FontSizeValidator([str(d) for d in range(-5, 11)]),
    )
    ui_theme_style = OptionsConfigItem(
        "UI",
        "ThemeStyle",
        "lumia",
        OptionsValidator(["lumia"]),  # 运行时动态补充
    )
    ui_light_mode = ConfigItem("UI", "LightMode", True, BoolValidator())

    # 工具区折叠显示（简洁模式）：工具调用/思考块集中在卡片顶部可滚动容器
    ui_compact_tool_area = ConfigItem("UI", "CompactToolArea", True, BoolValidator())

    # 消息身份行：每条消息顶部显示发送者头像 + 名称（插件可覆盖身份）
    ui_message_identity = ConfigItem("UI", "MessageIdentity", True, BoolValidator())

    # ========== 像素桌宠 ==========
    pet_enabled = ConfigItem("UI", "PetEnabled", False, BoolValidator())
    # 对话页（TabPanel）显示模式：list=列表 / tree=工作区树
    tab_panel_mode = OptionsConfigItem(
        "UI", "TabPanelMode", "list", OptionsValidator(["list", "tree"])
    )
    pet_size = OptionsConfigItem("UI", "PetSize", "small", OptionsValidator(["small", "medium", "large"]))

    # 注：上次项目/上次欢迎 tab/树折叠态等「上次状态」已迁至 app_state（.drifox/cache/app_state.json），
    # 不再作为系统配置项；旧 app.config 中的值由 app_state 首次加载时一次性迁入。

    # ========== 欢迎卡片模式（sessions / 插件注册 tab）==========
    # 内置 mode 仅保留 sessions；其余（📜 更新 等）由插件注册，禁用插件后自动消失。
    welcome_mode = OptionsConfigItem("UI", "WelcomeMode", "sessions", OptionsValidator(["sessions"]))

    # 侧边栏折叠态记忆：仅记用户手动操作（标题栏按钮/拖拽把手松手）的终态，
    # 挤压等自动折叠不落盘，重启恢复用户意图而非临时状态
    ui_sidebar_collapsed = ConfigItem("UI", "SidebarCollapsed", False, BoolValidator())
    # 工作台显隐记忆：仅记用户手动开关（标题栏「右侧边栏」按钮）终态
    ui_workbench_visible = ConfigItem("UI", "WorkbenchVisible", False, BoolValidator())

    # ========== LLM API 服务配置 ==========
    llm_api_enabled = ConfigItem("LLM", "APIEnabled", False, BoolValidator())
    llm_api_port = RangeConfigItem("LLM", "APIPort", 8765, RangeValidator(1024, 65535))

    # ========== MCP 服务器配置 ==========
    mcp_servers = ConfigItem("MCP", "Servers", [], ListDictValidator())
    mcp_enabled = ConfigItem("MCP", "Enabled", True, BoolValidator())
    mcp_discovered = ConfigItem("MCP", "Discovered", False, BoolValidator())

    # ========== 插件系统配置 ==========
    enabled_plugins = ConfigItem("Plugin", "EnabledPlugins", [])
    disabled_plugins = ConfigItem("Plugin", "DisabledPlugins", [])
    # 组件级禁用（D9）：["plugin:component", ...]，如 "calendar:hooks"
    disabled_plugin_components = ConfigItem("Plugin", "DisabledComponents", [])

    # ========== Hook 安全配置（A2） ==========
    # python hook「标准路径」白名单扩展（叠加在内置基座 app.hooks/app.utils 之上）
    safe_python_modules = ConfigItem("Hooks", "SafePythonModules", [])
    # http hook 是否放行私网地址（默认拦截 127/8、10/8、172.16/12、192.168/16、169.254/16、::1）
    hook_allow_private_network = ConfigItem("Hooks", "AllowPrivateNetwork", False, BoolValidator())

    # ========== 插件市场安全配置（C1） ==========
    # 市场源 url 类型允许的 git host 扩展（叠加在内置 github/gitlab/gitee/bitbucket/gitcode 之上，
    # 内网 git 源显式加白用）
    marketplace_allowed_git_hosts = ConfigItem("Marketplace", "AllowedGitHosts", [])

    # ========== 插件覆盖策略（同名覆盖显性化） ==========
    # false 时用户目录同名插件跳过、系统版生效；默认 true 保 junction 部署工作流
    allow_user_override = ConfigItem("Plugin", "AllowUserOverride", True, BoolValidator())

    # ========== MCP/LSP 启动确认白名单（P1-3） ==========
    # 键格式 "<kind>:<plugin>:<server>"（如 "mcp:user-custom:fetch"），
    # 用户对非内置源 server 首次启动点「允许」后写入；拒绝仅本会话生效不落盘
    confirmed_plugin_servers = ConfigItem("Plugin", "ConfirmedPluginServers", [])

    # ========== Gitee 图床配置 ==========
    gitee_enabled = ConfigItem("Gitee", "Enabled", True, BoolValidator())
    gitee_token = ConfigItem("Gitee", "Token", "a5dcb6e2e7776143b7a7e7685a1f33a3")
    gitee_owner = ConfigItem("Gitee", "Owner", "dingmama123141")
    gitee_repo = ConfigItem("Gitee", "Repo", "DriFox_share")
    gitee_path = ConfigItem("Gitee", "Path", uuid.uuid4().hex)
    gitee_branch = ConfigItem("Gitee", "Branch", "master")

    # --- 用户 OAuth 绑定 ---
    gitee_bound = ConfigItem("Gitee", "Bound", False, BoolValidator())
    gitee_user_token = ConfigItem("Gitee", "UserToken", "")
    gitee_user_refresh_token = ConfigItem("Gitee", "UserRefreshToken", "")
    gitee_token_expires_at = ConfigItem("Gitee", "TokenExpiresAt", 0.0)
    gitee_user_owner = ConfigItem("Gitee", "UserOwner", "")
    gitee_user_repo = ConfigItem("Gitee", "UserRepo", "DriFox_uploads")
    gitee_sync_remind = ConfigItem("Gitee", "SyncRemind", True, BoolValidator())

    # OAuth 应用凭证（内置）
    gitee_oauth_client_id = ConfigItem(
        "Gitee", "OAuthClientID", "3efedde73e3c9e698b84a5f9ef781ad771059a01dd8fc839752cf0aed70037c2"
    )
    gitee_oauth_client_secret = ConfigItem(
        "Gitee", "OAuthClientSecret", "73236836a816f2d2de6826b86e36bf9cddf8ff551290be2c4977b620a98c74c6"
    )

    # ========== LSP 配置 ==========
    lsp_auto_diagnose = ConfigItem("LSP", "AutoDiagnose", False, BoolValidator())

    # ========== 工具开关控制 ==========
    tool_toggles = ConfigItem("Tools", "Toggles", {})
    tool_off_behavior = ConfigItem("Tools", "OffBehavior", "deny")
    # per-tool 关闭策略：{tool_name: "deny"|"ask"}，缺失回退 tool_off_behavior
    tool_permission_policy = ConfigItem("Tools", "PermissionPolicy", {})
    # 工具热重载风险通知（True=每次热重载弹提醒；False=用户选择不再提醒，持久化）
    tool_reload_risk_notice = ConfigItem("Tools", "ReloadRiskNotice", True, BoolValidator())

    # ========== 锁屏远程 ==========
    # 开启后锁屏状态下也保持系统唤醒、屏幕常亮，便于手机远程操控与自动化持续运行
    lock_screen_remote_enabled = ConfigItem("System", "LockScreenRemote", False, BoolValidator())

    # ========== 插件 pip 依赖安装 ==========
    # 市场插件 dependencies.pip 声明的依赖安装源（uv/wheel 回退共用）；
    # 空串 = PyPI 官方源；国内可填如 https://pypi.tuna.tsinghua.edu.cn/simple
    pip_index_url = ConfigItem("Pip", "IndexURL", "")

    # ========== Tab 管理器 ==========
    enable_tab_manager = ConfigItem("UI", "EnableTabManager", True, BoolValidator())
    # 窗口几何/面板宽度不做记忆（打开时固定默认 960x640 居中 + panel 280），
    # 原 tab_panel_width / tab_panel_collapsed / tab_manager_geometry 配置项已移除
    window_always_on_top = ConfigItem("UI", "WindowAlwaysOnTop", False, BoolValidator())

    # ========== 渲染与性能（Webview）==========
    # 说明：本组配置在 main.py 启动最早期由 app/utils/render_env.py 裸 JSON
    # 读取并换算为环境变量，QtWebEngine 初始化后修改无效 —— **所有项均重启生效**。
    # 默认值 = 历史 main.py 硬编码行为；"auto" 档沿用旧检测链
    # （DRIFOX_SOFTWARE_RENDER / DRIFOX_ENABLE_WEBGL 环境变量 → ~/.drifox 标记文件）。
    # 渲染后端（**已移除 auto 档** —— 它不检测机器，只是读 ~/.drifox/software_render
    # 标记文件，名不副实）。默认 = software（WARP，CPU 光栅，不碰显卡驱动）：
    # 出厂即最稳路径，硬件档由用户显式选择。
    # vulkan / d3d9 / swiftshader 是三个排障档（见 render_env._ANGLE_PLATFORM 注释）：
    # 仅「显卡驱动有问题」时试 —— 驱动支持不全可能黑屏（vulkan / d3d9）；
    # swiftshader = Qt 走 WARP + Chromium 走自带 CPU 光栅的双保险。
    # 兼容：历史配置里残留的 "auto"、手改的非法值一律按出厂默认 software 处理
    # （render_env 裸读原始值，旧检测链已删除）。
    # hardware 档默认附加 --disable-gpu-compositing（GPU 光栅 + CPU 合成，规避
    # 双合成器纹理交换闪烁，2026-09-11），ExtraChromiumFlags 可覆盖。
    render_backend = OptionsConfigItem(
        "Render",
        "RenderBackend",
        "software",
        OptionsValidator(["software", "hardware", "software_gl", "vulkan", "d3d9", "swiftshader"]),
    )
    # WebGL 解禁（3D 图形需要）：auto / on / off
    render_webgl = OptionsConfigItem(
        "Render",
        "WebglEnabled",
        "auto",
        OptionsValidator(["auto", "on", "off"]),
    )
    # Chromium renderer 进程硬上限（内存治理核心项）
    render_renderer_process_limit = RangeConfigItem(
        "Render", "RendererProcessLimit", 6, RangeValidator(1, 32)
    )
    # 单 renderer JS 堆上限（MB），防单页膨胀
    render_js_heap_mb = RangeConfigItem("Render", "JsHeapMb", 128, RangeValidator(64, 1024))
    # Chromium 低内存模式：压低渲染缓冲/缓存（省 50-150MB，抗锯齿略降）。
    # 默认开，但 hardware 档未显式设置时默认关（真实 GPU 光栅下降级 tile 策略
    # 会加剧合成错位，见 render_env.compute_settings）。
    render_low_end_device_mode = ConfigItem("Render", "LowEndDeviceMode", True, BoolValidator())
    # 合成器平滑滚动动画（默认关闭：外层滚动由 Qt 承载，卡内滚动只是安全网场景）
    render_smooth_scrolling = ConfigItem("Render", "SmoothScrolling", False, BoolValidator())
    # 2D canvas 抗锯齿（默认关闭：echarts 软件光栅下省内存提速，锯齿微增）
    render_canvas_aa = ConfigItem("Render", "CanvasAA", False, BoolValidator())
    # 后台渲染节流：关（默认，Chromium 原生节流）/ 开 ——
    # 追加 --disable-renderer-backgrounding + --disable-backgrounding-occluded-windows。
    # 长对话里离屏卡片被降优先级导致的流式卡顿可开，代价是离屏卡片回收变慢。
    render_disable_background_throttling = ConfigItem(
        "Render", "DisableBackgroundThrottling", False, BoolValidator()
    )
    # 共享 GL 上下文（Qt.AA_ShareOpenGLContexts）：默认开，省约 12.7% per-view 常驻
    # 内存；代价是全部消息卡共用一个 GL 上下文。多卡/图表闪烁排查时可关掉验证。
    render_share_gl_contexts = ConfigItem("Render", "ShareGLContexts", True, BoolValidator())
    # 禁用的 Chromium feature 列表（翻译/媒体路由/优化提示/窗口遮挡计算）
    render_disabled_features = ConfigItem(
        "Render",
        "DisabledFeatures",
        "Translate,MediaRouter,optimizeHints,CalculateNativeWinOcclusion",
    )
    # 高级：追加任意 Chromium 开关（置于内置 flags 末尾，同 flag 后者覆盖前者）
    render_extra_flags = ConfigItem("Render", "ExtraChromiumFlags", "")


def update_theme_options():
    """从 ThemeManager 动态更新主题选项验证器

    注意：此函数只更新验证器选项列表，**不重置当前值**。
    即使当前值不在列表中（插件主题尚未加载），也不在此处回退。
    由 _reload_themes_from_plugins() 中的安全网在插件主题加载完成后统一恢复。
    """
    try:
        from app.utils.theme_manager import theme_manager

        themes = list(theme_manager.list_themes().keys())
        if themes:
            settings = Settings.get_instance()
            settings.ui_theme_style.validator.__init__(themes)
    except Exception as e:
        import logging

        logging.warning(f"[update_theme_options] failed: {e}")


# 注册解释器退出时关闭配置写入保护
atexit.register(Settings._set_closing_down)
