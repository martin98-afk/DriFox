# -*- coding: utf-8 -*-
"""
通用工具函数模块

提供应用程序使用的各种工具函数：
- 路径和资源管理
- 字体和图标配置
- 技能加载和管理
- 应用更新检查
- 异步更新检查器
- JSON 序列化/反序列化
"""
import asyncio  # 用于 AsyncUpdateChecker
import os
import re
import socket
import sys
import weakref
from pathlib import Path

import httpx
import orjson as json
import psutil
import requests
import yaml
from loguru import logger
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QFont, QIcon, QIconEngine

from app.utils.config import Settings

try:
    from pypinyin import Style, pinyin
except ImportError:
    pinyin = None

from app.utils.icon_name_map import ICON_NAME_TO_FILE

# 浅色图标映射（按需导入，避免循环）
_ICON_NAME_TO_FILE_LIGHT = None


def _get_light_icon_map():
    """懒加载浅色图标映射"""
    global _ICON_NAME_TO_FILE_LIGHT
    if _ICON_NAME_TO_FILE_LIGHT is None:
        try:
            from app.utils.icon_name_map_light import ICON_NAME_TO_FILE_LIGHT as m
            _ICON_NAME_TO_FILE_LIGHT = m
        except ImportError:
            _ICON_NAME_TO_FILE_LIGHT = {}
    return _ICON_NAME_TO_FILE_LIGHT


# ANSI 颜色代码映射
ANSI_COLOR_MAP = {
    "30": "#000000",  # 黑色
    "31": "#ff0000",  # 红色
    "32": "#00ff00",  # 绿色
    "33": "#ffff00",  # 黄色
    "34": "#0000ff",  # 蓝色
    "35": "#ff00ff",  # 紫色
    "36": "#00ffff",  # 青色
    "37": "#ffffff",  # 白色
    "90": "#808080",  # 亮黑
    "91": "#ff5555",  # 亮红
    "92": "#50fa7b",  # 亮绿
    "93": "#f1fa8c",  # 亮黄
    "94": "#8be9fd",  # 亮蓝
    "95": "#ff79c6",  # 亮紫
    "96": "#8be9fd",  # 亮青
    "97": "#ffffff",  # 亮白
}


def get_app_data_dir() -> Path:
    """获取应用数据目录（跨平台兼容）

    开发环境: 当前目录/.drifox
    PyInstaller打包: ~/.drifox（用户 home 目录，可写）
    macOS .app: ~/Library/Application Support/Drifox/.drifox
    """
    # 开发环境
    if not hasattr(sys, '_MEIPASS') and not getattr(sys, 'frozen', False):
        return Path('.drifox')

    # macOS .app: 使用 Application Support（用户可写）
    if sys.platform == 'darwin':
        from AppKit import NSApplicationSupportDirectory, NSFileManager, NSUserDomainMask
        paths = NSFileManager.defaultManager().URLsForDirectory_inDomains_(
            NSApplicationSupportDirectory, NSUserDomainMask
        )
        if paths:
            app_support_path = paths[0].fileSystemRepresentation().decode('utf-8')
            app_support = Path(app_support_path) / 'Drifox'
            app_support.mkdir(parents=True, exist_ok=True)
            return app_support / '.drifox'

    # Windows/Linux 打包: 使用 ~/.drifox（用户 home，不受安装位置限制）
    return Path.home() / '.drifox'


_MIGRATED_FLAG = False  # 防止重复迁移


def _checkpoint_sqlite_db(db_path: Path):
    """对 SQLite 数据库执行 WAL 检查点，确保数据全部刷回主文件"""
    import sqlite3
    try:
        conn = sqlite3.connect(str(db_path), timeout=1)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
    except Exception:
        pass


def migrate_app_data_if_needed():
    """将旧版本数据迁移到用户可写目录（仅打包版需要）

    旧路径: <安装目录>/.drifox (Program Files 等，可能权限受限)
    新路径: ~/.drifox 或 macOS: Application Support

    迁移仅在以下情况执行：
    1. 旧目录存在有效数据（app.config 或 sessions.db）
    2. 新目录不存在，或新目录存在但没有有效数据（只有 logs/ 等空壳子目录）
    """
    global _MIGRATED_FLAG
    if _MIGRATED_FLAG:
        return
    _MIGRATED_FLAG = True

    # 开发环境不需要迁移
    if not hasattr(sys, '_MEIPASS') and not getattr(sys, 'frozen', False):
        return

    import shutil

    from loguru import logger

    # 旧路径：安装目录旁（Program Files）
    old_dir = Path(sys._MEIPASS).parent / '.drifox' if hasattr(sys, '_MEIPASS') else None
    if not old_dir or not old_dir.exists():
        logger.debug(f"[迁移] 旧目录不存在，跳过迁移: {old_dir}")
        return

    # 检查旧目录是否有有效数据
    has_old_data = (old_dir / "app.config").exists() or (old_dir / "sessions.db").exists()
    if not has_old_data:
        logger.debug(f"[迁移] 旧目录无有效数据，跳过迁移: {old_dir}")
        return

    new_dir = get_app_data_dir()
    if new_dir.exists():
        # 新目录已存在：检查是否有有效数据，有则跳过，无则覆盖
        has_new_data = (new_dir / "app.config").exists() or (new_dir / "sessions.db").exists()
        if has_new_data:
            logger.info(f"[迁移] 目标目录已有数据，跳过迁移: {new_dir}")
            return
        logger.info(f"[迁移] 目标目录存在但无有效数据，准备覆盖: {new_dir}")

    # 关键：先 checkpoint SQLite 数据库，把 WAL 数据刷回主文件
    # 否则 shutil.copytree 可能只复制到不完整的 .db 文件
    for db_file in old_dir.glob("*.db"):
        logger.info(f"[迁移] 检查点: {db_file.name}")
        _checkpoint_sqlite_db(db_file)

    logger.info(f"[迁移] 复制数据: {old_dir} → {new_dir}")
    new_dir.parent.mkdir(parents=True, exist_ok=True)
    # 如果新目录存在但无有效数据，先删除再复制，避免残留旧 logs/ 等
    if new_dir.exists():
        shutil.rmtree(str(new_dir))
    shutil.copytree(str(old_dir), str(new_dir), dirs_exist_ok=False)

    # 验证迁移结果
    old_db = old_dir / "sessions.db"
    new_db = new_dir / "sessions.db"
    if old_db.exists() and new_db.exists():
        logger.info(f"[迁移] sessions.db: {old_db.stat().st_size} → {new_db.stat().st_size} bytes")
    elif new_db.exists():
        logger.info("[迁移] sessions.db 复制完成")
    else:
        logger.warning("[迁移] sessions.db 未找到，数据可能是空的")


def get_pinyin_search_keys(text):
    """生成拼音全拼和首字母缩写"""
    if not pinyin or not text:
        return ""
    # 提取首字母 (Style.FIRST_LETTER)
    first_letters = "".join([i[0][0] for i in pinyin(text, style=Style.FIRST_LETTER)])
    # 提取全拼 (Style.NORMAL)
    full_pinyin = "".join([i[0] for i in pinyin(text, style=Style.NORMAL)])
    return f"{first_letters} {full_pinyin} {text}".lower()


def kill_proc_tree(pid):
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for child in children:
            child.kill()
        parent.kill()
        psutil.wait_procs(children + [parent], timeout=5)
    except psutil.NoSuchProcess:
        pass


# 预编译 ANSI 处理正则表达式
_ANSI_CURSOR_PATTERN = re.compile(r"\x1b\[[0-9;]*[ABCDHfJKmnsu]")
_ANSI_COLOR_PATTERN = re.compile(r"\x1b\[([0-9;]*)m")
_ANSI_RESET_PATTERN = re.compile(r"\x1b\[0m")
_ANSI_REMAINS_PATTERN = re.compile(r"\x1b\[[0-9;]*m")


def ansi_to_html(text):
    """
    将 ANSI 颜色代码转换为 HTML span 标签
    """
    if not text:
        return ""

    # 移除光标控制序列（如 \x1b[2K）
    text = _ANSI_CURSOR_PATTERN.sub("", text)

    # 处理颜色代码
    def replace_ansi(match):
        codes = match.group(1).split(";")
        color = None
        bold = False

        for code in codes:
            if code in ANSI_COLOR_MAP:
                color = ANSI_COLOR_MAP[code]
            elif code == "1":
                bold = True

        if color:
            style = f"color: {color};"
            if bold:
                style += " font-weight: bold;"
            return f'<span style="{style}">'
        elif bold:
            return '<span style="font-weight: bold;">'
        else:
            return "<span>"

    # 替换 ANSI 开始序列 \x1b[...m
    text = _ANSI_COLOR_PATTERN.sub(replace_ansi, text)

    # 替换 ANSI 结束序列 \x1b[0m 为 </span>
    text = _ANSI_RESET_PATTERN.sub("</span>", text)

    # 处理剩余的 ANSI 序列（清理）
    text = _ANSI_REMAINS_PATTERN.sub("", text)

    # 转换换行符
    text = text.replace("\n", "<br>")

    return text


def ansi_to_rich_text(text):
    """
    将 ANSI 转换为 Qt Rich Text（备用方案）
    """
    return f"<pre style='font-family: Consolas, monospace;'>{ansi_to_html(text)}</pre>"


def resource_path(relative_path) -> str:
    """获取打包后资源文件的绝对路径"""
    if hasattr(sys, "_MEIPASS"):
        # 如果是打包后的环境
        base_path = sys._MEIPASS
    else:
        # 开发环境，直接使用当前路径
        base_path = os.path.abspath(".")

    return os.path.join(base_path, relative_path)


def get_port_node(port):
    """安全获取端口所属节点，兼容 property 和 method"""
    node = port.node
    return node() if callable(node) else node


# 图标缓存（仅按 icon_name 缓存 QIcon，theme 感知由 QIconEngine 处理）
_ICON_CACHE: dict = {}


class _ThemeIconEngine(QIconEngine):
    """主题感知图标引擎 — 每次 paint 时自动根据当前主题加载对应颜色图标。

    Qt 内部通过 key() 缓存 pixmap。key() 包含 light/dark 标识，
    主题切换后 key 变化 → Qt 自动重新请求 pixmap → 图标自动适配。
    """

    def __init__(self, icon_name: str):
        super().__init__()
        self._icon_name = icon_name

    def _load(self) -> QIcon:
        """根据当前主题加载正确颜色的图标（无缓存，每次调用都检查）"""
        is_light = _is_current_theme_light()
        prefix = ":/icons_light" if is_light else ":/icons"

        # 浅色模式：优先查浅色映射表
        if is_light:
            light_map = _get_light_icon_map()
            if self._icon_name in light_map:
                path = f"{prefix}/{light_map[self._icon_name]}"
                icon = QIcon(path)
                if not icon.isNull():
                    return icon

        # 通用映射表 / 浅色回退
        filename = ICON_NAME_TO_FILE.get(self._icon_name)
        if filename:
            path = f":/icons/{filename}"
            icon = QIcon(path)
            if not icon.isNull():
                return icon

        # fallback
        try:
            from qfluentwidgets import FluentIcon
            return FluentIcon.APPLICATION.icon()
        except Exception:
            pass
        return QIcon()

    def paint(self, painter, rect, mode, state):
        self._load().paint(painter, rect, Qt.AlignmentFlag.AlignCenter, mode, state)

    def pixmap(self, size, mode, state):
        return self._load().pixmap(size, mode, state)

    def clone(self):
        return _ThemeIconEngine(self._icon_name)

    def key(self):
        # 主题切换后 key 变化 → Qt pixmap 缓存自动失效
        is_light = _is_current_theme_light()
        return f"_ThemeIconEngine:{self._icon_name}:{'light' if is_light else 'dark'}"

    def iconName(self):
        return self._icon_name


def get_icon(icon_name: str) -> QIcon:
    """
    从 Qt 资源系统加载图标（主题感知，自动适配浅色/深色）

    使用 _ThemeIconEngine 实现：主题切换后无需刷新 widget，
    Qt 内部 pixmap 缓存自动失效，下次 paint 时加载正确颜色。

    Args:
        icon_name: 图标名（不含扩展名），如 "copy"

    Returns:
        QIcon 实例
    """
    if icon_name in _ICON_CACHE:
        return _ICON_CACHE[icon_name]

    icon = QIcon(_ThemeIconEngine(icon_name))
    _ICON_CACHE[icon_name] = icon
    return icon


def _is_current_theme_light() -> bool:
    """判断当前主题是否为浅色模式"""
    try:
        from app.utils.theme_manager import theme_manager
        return theme_manager.is_light_theme()
    except Exception as e:
        import logging
        _log = logging.getLogger(__name__)
        _log.warning(f"[utils] is_light_theme check failed, fallback to dark: {e}")
        return False


def invalidate_icon_cache():
    """清除图标缓存（主题切换时调用，已不再必需——引擎自动感知）"""
    global _ICON_CACHE
    _ICON_CACHE.clear()



def get_local_skills() -> list:
    """获取本地技能列表，支持多个搜索路径

    搜索路径优先级：
    - 已启用插件的 skills/ 目录（PluginManager）
    - 旧路径做回退：app/skills, .opencode/skills, ~/.agents/skills

    返回字段：
    - name: 技能名称（无前缀，用于 load_skill 内部查找）
    - qualified_name: 完整名称（用户插件含 plugin: 前缀，用于列表展示）
    - description: 技能描述
    - plugin_name: 所属插件名（非插件来源为 None）
    - is_system: 是否为系统插件技能
    """
    # 性能优化：基于 mtime + 文件数的缓存
    # 每次 get_local_skills() 都要遍历所有 skill 目录 + 读 SKILL.md + YAML 解析，
    # 在用户每次 /skill 命令时被调用一次，单次 30+ ms。新增/编辑/删除 skill 文件会改
    # mtime，自动失效；插件启用状态变化会改 PluginManager 内目录 mtime，同样失效。
    # 如需手动失效（极少见），调用 invalidate_skills_cache()。
    global _skills_cache, _skills_cache_key
    cache_key = _compute_skills_cache_key()
    if _skills_cache is not None and _skills_cache_key == cache_key:
        return _skills_cache

    results = []
    seen = set()
    seen_qualified = set()

    # ---- Phase 1: PluginManager 路径（带插件上下文） ----
    try:
        from app.core.plugin_manager import PluginManager
        pm = PluginManager.get_instance()
        if pm.is_initialized():
            for item in pm.get_skills_with_plugin():
                skills_base = item["path"]
                plugin_name = item["plugin_name"]
                is_system = item["is_system"]

                if not skills_base.exists():
                    continue
                for skill_dir in skills_base.iterdir():
                    if not skill_dir.is_dir():
                        continue
                    if skill_dir.name.startswith("_") or skill_dir.name.startswith("."):
                        continue
                    entry = _parse_skill_dir(skill_dir, plugin_name, is_system)
                    if entry and entry["name"] not in seen:
                        seen.add(entry["name"])
                        seen_qualified.add(entry["qualified_name"])
                        results.append(entry)
    except (ImportError, Exception):
        pass

    # ---- Phase 2: 旧路径回退（无插件上下文） ----
    fallback_dirs = [
        Path(__file__).parent.parent / "skills",
        Path(__file__).parent.parent / ".opencode" / "skills",
        get_app_data_dir() / "skills",
        Path.home() / ".agents" / "skills",
    ]

    for skills_base in fallback_dirs:
        if not skills_base.exists():
            continue
        for skill_dir in skills_base.iterdir():
            if not skill_dir.is_dir():
                continue
            if skill_dir.name.startswith("_") or skill_dir.name.startswith("."):
                continue
            entry = _parse_skill_dir(skill_dir, plugin_name=None, is_system=True)
            if entry and entry["name"] not in seen:
                seen.add(entry["name"])
                seen_qualified.add(entry["qualified_name"])
                results.append(entry)

    # 写入缓存后再返回
    _skills_cache = results
    _skills_cache_key = cache_key
    return results


# ========== Skills 缓存（性能优化：避免每次 /skill 命令都重读所有 SKILL.md）==========
_skills_cache: list | None = None
_skills_cache_key: tuple = ()


def _compute_skills_cache_key() -> tuple:
    """计算 skills 缓存 key：(扫描目录数, 文件数, max_mtime, total_size)

    比逐文件 stat 列表更轻：单次扫描 ~2 ms（vs 旧 12 ms）。
    任何 SKILL.md / skill.md 增删改都改 mtime 或 count，自动失效。
    """
    scan_dirs: list[Path] = []

    try:
        from app.core.plugin_manager import PluginManager
        pm = PluginManager.get_instance()
        if pm.is_initialized():
            for item in pm.get_skills_with_plugin():
                p = item.get("path")
                if p and Path(p).exists():
                    scan_dirs.append(Path(p))
    except (ImportError, Exception):
        pass

    scan_dirs.extend([
        Path(__file__).parent.parent / "skills",
        Path(__file__).parent.parent / ".opencode" / "skills",
        get_app_data_dir() / "skills",
        Path.home() / ".agents" / "skills",
    ])

    file_count = 0
    max_mtime = 0.0
    total_size = 0
    for skills_base in scan_dirs:
        if not skills_base.exists():
            continue
        try:
            for skill_dir in skills_base.iterdir():
                if not skill_dir.is_dir():
                    continue
                if skill_dir.name.startswith("_") or skill_dir.name.startswith("."):
                    continue
                for fname in ("SKILL.md", "skill.md"):
                    f = skill_dir / fname
                    if f.exists():
                        try:
                            st = f.stat()
                            file_count += 1
                            if st.st_mtime > max_mtime:
                                max_mtime = st.st_mtime
                            total_size += st.st_size
                        except OSError:
                            pass
                        break
        except OSError:
            pass

    return (file_count, max_mtime, total_size)


def invalidate_skills_cache() -> None:
    """手动失效 skills 缓存。插件启用/禁用后如需立即生效可调用。"""
    global _skills_cache, _skills_cache_key
    _skills_cache = None
    _skills_cache_key = ()


def _parse_skill_dir(skill_dir: Path, plugin_name: str | None = None,
                     is_system: bool = True) -> dict | None:
    """解析技能目录，返回技能信息字典"""
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.exists():
        skill_file = skill_dir / "skill.md"
    if not skill_file.exists():
        return None

    content = skill_file.read_text(encoding="utf-8")
    name = skill_dir.name
    description = ""

    # 解析 frontmatter
    if content.startswith("---"):
        try:
            frontmatter = content.split("---", 2)[1]
            meta = yaml.safe_load(frontmatter)
            if meta:
                name = meta.get("name", skill_dir.name)
                description = meta.get("description", "")
        except Exception:
            pass

    # 用户插件技能加命名空间前缀
    if plugin_name and not is_system:
        qualified_name = f"{plugin_name}:{name}"
    else:
        qualified_name = name

    return {
        "name": name,
        "qualified_name": qualified_name,
        "description": description,
        "plugin_name": plugin_name,
        "is_system": is_system,
    }


def get_skill_by_name(name: str) -> dict | None:
    """根据名称获取技能信息"""
    skills = get_local_skills()
    for skill in skills:
        if skill["name"] == name:
            return skill
    return None


def load_skill(name: str) -> tuple[bool, str, str]:
    """加载指定技能，返回 (成功, 内容, 工作目录)

    支持两种名称格式：
    - "skillname" — 按名称在所有路径中查找
    - "plugin:skillname" — 限定到指定插件

    Args:
        name: 技能名称（可选带 plugin: 前缀）

    Returns:
        (成功标志, 内容或错误信息, 技能工作目录路径)
    """
    # 解析命名空间前缀
    raw_name = name
    target_plugin = None
    if ":" in name:
        target_plugin, _, skill_name = name.partition(":")
        name = skill_name

    # 基础搜索路径
    search_paths = [
        Path(__file__).parent.parent / "skills" / name / "SKILL.md",
        Path(__file__).parent.parent / ".opencode" / "skills" / name / "SKILL.md",
        get_app_data_dir() / "skills" / name / "SKILL.md",
        Path.home() / ".agents" / "skills" / name / "SKILL.md",
    ]

    # 插件路径（PluginManager 已初始化时添加为最高优先级）
    try:
        from app.core.plugin_manager import PluginManager
        pm = PluginManager.get_instance()
        if pm.is_initialized():
            if target_plugin:
                # 限定到指定插件
                for plugin in pm._iter_enabled_plugins():
                    if plugin.name == target_plugin:
                        p = plugin.path / "skills" / name / "SKILL.md"
                        search_paths.insert(0, p)
                        break
            else:
                # 无前缀：按优先级扫描全部
                for item in pm.get_skills_with_plugin():
                    p = item["path"] / name / "SKILL.md"
                    search_paths.insert(0, p)
    except (ImportError, Exception):
        pass

    found_path = None
    for path in search_paths:
        if path.exists():
            found_path = path
            break

    if not found_path:
        return (False, f"Skill not found: {raw_name}", "")

    content = found_path.read_text(encoding="utf-8")
    # 去除 frontmatter
    content = content.split("---", 2)[-1].strip()
    workspace = str(found_path.parent.resolve())

    return (True, content, workspace)


def list_skills_with_intro() -> str:
    """获取技能列表，包含 SKILLS.md 介绍"""
    skills = get_local_skills()

    # 从插件路径查找 SKILLS.md（优先使用优先级最高的）
    skills_intro = ""
    try:
        from app.core.plugin_manager import PluginManager
        pm = PluginManager.get_instance()
        if pm.is_initialized():
            for item in pm.get_skills_with_plugin():
                readme = item["path"] / "SKILLS.md"
                if readme.exists():
                    skills_intro = readme.read_text(encoding="utf-8") + "\n\n"
                    break
    except (ImportError, Exception):
        pass

    # 回退：旧路径
    if not skills_intro:
        main_skills_dir = Path(__file__).parent.parent / "skills"
        skills_readme = main_skills_dir / "SKILLS.md"
        if skills_readme.exists():
            skills_intro = skills_readme.read_text(encoding="utf-8") + "\n\n"

    # 生成 XML 格式（使用 qualified_name 以示含前缀）
    skills_xml = "<available_skills>\n"
    for skill in skills:
        desc = skill.get("description", "").replace("<", "&lt;").replace(">", "&gt;")
        name = skill.get("qualified_name", skill["name"])
        skills_xml += f"  <skill>\n    <name>{name}</name>\n    <description>{desc}</description>\n  </skill>\n"
    skills_xml += "</available_skills>"

    return skills_intro + skills_xml


def get_canvas_font(size=10, bold=False):
    from app.utils.design_tokens import scale_font_size
    try:
        font_family = Settings.get_instance().llm_font_family.value
    except Exception:
        try:
            font_family = Settings.get_instance().canvas_font_selected.value
        except Exception:
            font_family = "Segoe UI"

    font = QFont(font_family, scale_font_size(size))
    if bold:
        font.setBold(True)
    return font


def get_unified_font(size=10, bold=False):
    """Get font with unified font family configured by user"""
    from app.utils.design_tokens import scale_font_size
    try:
        font_family = Settings.get_instance().llm_font_family.value
    except Exception:
        try:
            font_family = Settings.get_instance().canvas_font_selected.value
        except Exception:
            font_family = "Segoe UI"
    font = QFont(font_family, scale_font_size(size))
    if bold:
        font.setBold(True)
    return font


_cached_font_family_css: str | None = None


def get_font_family_css() -> str:
    """获取 CSS font-family 字符串，用于 stylesheet 中保持字体统一（缓存）

    字体设置仅在用户更改配置时变化，缓存后由 invalidate_font_cache() 失效。
    """
    global _cached_font_family_css
    if _cached_font_family_css is not None:
        return _cached_font_family_css
    try:
        font_family = Settings.get_instance().llm_font_family.value
    except Exception:
        try:
            font_family = Settings.get_instance().canvas_font_selected.value
        except Exception:
            font_family = "Segoe UI"
    _cached_font_family_css = f"font-family: '{font_family}';"
    return _cached_font_family_css


def invalidate_font_family_css_cache() -> None:
    """字体 CSS 缓存失效（与 design_tokens.invalidate_font_cache 配合使用）"""
    global _cached_font_family_css
    _cached_font_family_css = None


def str_to_bool(value):
    """可靠的布尔值转换"""
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("true", "1", "yes", "on")


def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def serialize_for_json(obj, large_list_threshold=1000):
    """递归将对象转换为 JSON 可序列化格式"""
    if isinstance(obj, dict):
        return {k: serialize_for_json(v) for k, v in obj.items()}
    elif hasattr(obj, "serialize") and callable(getattr(obj, "serialize")):
        try:
            return obj.serialize()
        except Exception:
            return str(obj)
    else:
        # 其他类型：尝试转为字符串
        try:
            json.dumps(obj)  # 测试是否可序列化
            return obj
        except (TypeError, ValueError):
            return None


def deserialize_from_json(obj):
    if isinstance(obj, dict):
        return {k: deserialize_from_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [deserialize_from_json(v) for v in obj]
    else:
        return obj


class DownloadThread(QThread):
    progress_signal = pyqtSignal(int)  # 进度信号
    finished_signal = pyqtSignal(str)  # 完成信号（返回文件路径）
    error_signal = pyqtSignal(str)  # 错误信号
    canceled_signal = pyqtSignal()  # 取消信号（新增）

    def __init__(self, url, file_path, token):
        super().__init__()
        self.url = url
        self.file_path = file_path
        self.headers = {"Authorization": token} if token else {}
        self.is_canceled = False  # 取消标志位
        self.session = requests.Session()  # 使用 Session 以便关闭连接

    def run(self):
        try:
            response = self.session.get(self.url, headers=self.headers, stream=True, timeout=10)
            response.raise_for_status()

            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0

            with open(self.file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=1024):
                    if self.is_canceled:  # 每次读取前检查取消标志
                        f.close()
                        os.remove(self.file_path)  # 删除不完整文件
                        self.canceled_signal.emit()
                        return
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size > 0:
                            progress = int((downloaded / total_size) * 100)
                            self.progress_signal.emit(progress)

            self.finished_signal.emit(self.file_path)
        except Exception as e:
            if not self.is_canceled:  # 非取消情况才触发错误信号
                self.error_signal.emit(str(e))
        finally:
            self.session.close()  # 确保释放网络资源


class AsyncUpdateChecker(QThread):
    check_finished = pyqtSignal(object)  # 返回 latest_release 或 None
    error = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        # 使用 weakref 避免循环引用导致 parent 被删除后仍持有引用
        self._parent_ref = weakref.ref(parent) if parent else None

    @property
    def repo(self):
        if self._parent_ref is not None:
            p = self._parent_ref()
            if p is not None:
                return p.repo
        return getattr(self, '_repo', None)

    @repo.setter
    def repo(self, value):
        self._repo = value

    @property
    def platform(self):
        if self._parent_ref is not None:
            p = self._parent_ref()
            if p is not None:
                return p.platform
        return getattr(self, '_platform', None)

    @platform.setter
    def platform(self, value):
        self._platform = value

    @property
    def token(self):
        if self._parent_ref is not None:
            p = self._parent_ref()
            if p is not None:
                return p.token
        return getattr(self, '_token', None)

    @token.setter
    def token(self, value):
        self._token = value

    async def fetch_github(self):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }
        headers = headers | {"Authorization": f"token {self.token}"} if self.token else headers
        url = f"https://api.github.com/repos/{self.repo}/releases/latest"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                logger.debug(f"GitHub API 响应: {resp.json()}")
                return resp.json()
            else:
                logger.debug(f"GitHub API 响应: {resp.text}")
                self.error.emit(f"GitHub API 请求失败：{resp.status_code}")
                return None

    async def fetch_gitee(self):
        headers = {"Authorization": self.token} if self.token else {}
        url = f"https://gitee.com/api/v5/repos/{self.repo}/releases/latest"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json()
            else:
                self.error.emit(f"Gitee API 请求失败：{resp.status_code}")
                return None

    async def fetch_gitcode(self):
        headers = {"Authorization": self.token} if self.token else {}
        url = f"https://gitcode.com/api/v5/repos/{self.repo}/releases/latest"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json()
            else:
                self.error.emit(f"Gitcode API 请求失败：{resp.status_code}")
                return None

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            if self.platform == "github":
                result = loop.run_until_complete(self.fetch_github())
            elif self.platform == "gitee":
                result = loop.run_until_complete(self.fetch_gitee())
            elif self.platform == "gitcode":
                result = loop.run_until_complete(self.fetch_gitcode())
            else:
                result = None
                self.error.emit("不支持的平台")
        except Exception as e:
            self.error.emit(str(e))
            result = None
        finally:
            self.check_finished.emit(result)
