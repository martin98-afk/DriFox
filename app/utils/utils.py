# -*- coding: utf-8 -*-
"""
通用工具函数模块
"""
import asyncio
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
from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtGui import QIcon, QFont

from app.utils.config import Settings

try:
    from pypinyin import pinyin, Style
except ImportError:
    pinyin = None

from app.utils.icon_name_map import ICON_NAME_TO_FILE

ANSI_COLOR_MAP = {
    "30": "#000000", "31": "#ff0000", "32": "#00ff00", "33": "#ffff00",
    "34": "#0000ff", "35": "#ff00ff", "36": "#00ffff", "37": "#ffffff",
    "90": "#808080", "91": "#ff5555", "92": "#50fa7b", "93": "#f1fa8c",
    "94": "#8be9fd", "95": "#ff79c6", "96": "#8be9fd", "97": "#ffffff",
}
_ICON_CACHE = {}

# 预编译正则
_ANSI_CURSOR_PATTERN = re.compile(r"\x1b\[[0-9;]*[ABCDHfJKmnsu]")
_ANSI_COLOR_PATTERN = re.compile(r"\x1b\[([0-9;]*)m")
_ANSI_RESET_PATTERN = re.compile(r"\x1b\[0m")


def get_app_data_dir() -> Path:
    """获取应用数据目录（跨平台兼容）"""
    if not hasattr(sys, '_MEIPASS') and not getattr(sys, 'frozen', False):
        return Path('.drifox')

    if sys.platform == 'darwin':
        from AppKit import NSApplicationSupportDirectory, NSUserDomainMask, NSFileManager
        paths = NSFileManager.defaultManager().URLsForDirectory_inDomains_(
            NSApplicationSupportDirectory, NSUserDomainMask
        )
        if paths:
            app_support = Path(paths[0].fileSystemRepresentation().decode('utf-8')) / 'Drifox'
            app_support.mkdir(parents=True, exist_ok=True)
            return app_support / '.drifox'

    return Path.home() / '.drifox'


_MIGRATED_FLAG = False


def _checkpoint_sqlite_db(db_path: Path):
    """对 SQLite 执行 WAL checkpoint"""
    import sqlite3
    try:
        conn = sqlite3.connect(str(db_path), timeout=1)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
    except Exception:
        pass


def migrate_app_data_if_needed():
    """迁移旧版本数据到用户可写目录（仅打包版需要）"""
    global _MIGRATED_FLAG
    if _MIGRATED_FLAG:
        return
    _MIGRATED_FLAG = True

    if not hasattr(sys, '_MEIPASS') and not getattr(sys, 'frozen', False):
        return

    from loguru import logger
    import shutil

    old_dir = Path(sys._MEIPASS).parent / '.drifox' if hasattr(sys, '_MEIPASS') else None
    if not old_dir or not old_dir.exists():
        logger.debug(f"[迁移] 旧目录不存在，跳过迁移: {old_dir}")
        return

    has_old_data = (old_dir / "app.config").exists() or (old_dir / "sessions.db").exists()
    if not has_old_data:
        logger.debug(f"[迁移] 旧目录无有效数据，跳过迁移: {old_dir}")
        return

    new_dir = get_app_data_dir()
    if new_dir.exists():
        has_new_data = (new_dir / "app.config").exists() or (new_dir / "sessions.db").exists()
        if has_new_data:
            logger.info(f"[迁移] 目标目录已有数据，跳过迁移: {new_dir}")
            return
        logger.info(f"[迁移] 目标目录存在但无有效数据，准备覆盖: {new_dir}")

    for db_file in old_dir.glob("*.db"):
        logger.info(f"[迁移] 检查点: {db_file.name}")
        _checkpoint_sqlite_db(db_file)

    logger.info(f"[迁移] 复制数据: {old_dir} → {new_dir}")
    new_dir.parent.mkdir(parents=True, exist_ok=True)
    if new_dir.exists():
        shutil.rmtree(str(new_dir))
    shutil.copytree(str(old_dir), str(new_dir), dirs_exist_ok=False)

    old_db, new_db = old_dir / "sessions.db", new_dir / "sessions.db"
    if old_db.exists() and new_db.exists():
        logger.info(f"[迁移] sessions.db: {old_db.stat().st_size} → {new_db.stat().st_size} bytes")
    elif new_db.exists():
        logger.info(f"[迁移] sessions.db 复制完成")
    else:
        logger.warning(f"[迁移] sessions.db 未找到，数据可能是空的")

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


def ansi_to_html(text):
    """将 ANSI 颜色代码转换为 HTML span 标签"""
    if not text:
        return ""

    # 移除光标控制序列
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
        base_path = sys._MEIPASS
    else:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


def get_port_node(port):
    """安全获取端口所属节点"""
    node = port.node
    return node() if callable(node) else node


def get_icon(icon_name: str) -> QIcon:
    """从 Qt 资源系统加载图标（高性能、无磁盘 I/O）"""
    if icon_name in _ICON_CACHE:
        return _ICON_CACHE[icon_name]

    filename = ICON_NAME_TO_FILE.get(icon_name)
    if filename:
        icon = QIcon(f":/icons/{filename}")
        if not icon.isNull():
            _ICON_CACHE[icon_name] = icon
            return icon

    try:
        from qfluentwidgets import FluentIcon
        icon = FluentIcon.APPLICATION.icon()
        _ICON_CACHE[icon_name] = icon
        return icon
    except Exception:
        pass

    return QIcon()


def get_local_skills() -> list:
    """获取本地技能列表（支持插件路径）"""
    results = []
    seen = set()

    # Phase 1: PluginManager 路径
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
                    if skill_dir.is_dir() and not skill_dir.name.startswith(("_", ".")):
                        entry = _parse_skill_dir(skill_dir, plugin_name, is_system)
                        if entry and entry["name"] not in seen:
                            seen.add(entry["name"])
                            results.append(entry)
    except (ImportError, Exception):
        pass

    # Phase 2: 旧路径回退
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
            if skill_dir.is_dir() and not skill_dir.name.startswith(("_", ".")):
                entry = _parse_skill_dir(skill_dir, plugin_name=None, is_system=True)
                if entry and entry["name"] not in seen:
                    seen.add(entry["name"])
                    results.append(entry)

    return results


def _parse_skill_dir(skill_dir: Path, plugin_name: str | None = None, is_system: bool = True) -> dict | None:
    """解析技能目录，返回技能信息字典"""
    for name in ["SKILL.md", "skill.md"]:
        skill_file = skill_dir / name
        if skill_file.exists():
            break
    else:
        return None

    content = skill_file.read_text(encoding="utf-8")
    name = skill_dir.name
    description = ""

    if content.startswith("---"):
        try:
            frontmatter = content.split("---", 2)[1]
            meta = yaml.safe_load(frontmatter)
            if meta:
                name = meta.get("name", skill_dir.name)
                description = meta.get("description", "")
        except Exception:
            pass

    qualified_name = f"{plugin_name}:{name}" if plugin_name and not is_system else name

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
    """加载指定技能，返回 (成功, 内容, 工作目录)"""
    raw_name = name
    target_plugin = None
    if ":" in name:
        target_plugin, _, name = name.partition(":")

    search_paths = [
        Path(__file__).parent.parent / "skills" / name / "SKILL.md",
        Path(__file__).parent.parent / ".opencode" / "skills" / name / "SKILL.md",
        get_app_data_dir() / "skills" / name / "SKILL.md",
        Path.home() / ".agents" / "skills" / name / "SKILL.md",
    ]

    try:
        from app.core.plugin_manager import PluginManager
        pm = PluginManager.get_instance()
        if pm.is_initialized():
            if target_plugin:
                for plugin in pm._iter_enabled_plugins():
                    if plugin.name == target_plugin:
                        search_paths.insert(0, plugin.path / "skills" / name / "SKILL.md")
                        break
            else:
                for item in pm.get_skills_with_plugin():
                    search_paths.insert(0, item["path"] / name / "SKILL.md")
    except (ImportError, Exception):
        pass

    for path in search_paths:
        if path.exists():
            content = path.read_text(encoding="utf-8")
            content = content.split("---", 2)[-1].strip()
            return (True, content, str(path.parent.resolve()))

    return (False, f"Skill not found: {raw_name}", "")


def list_skills_with_intro() -> str:
    """获取技能列表，包含 SKILLS.md 介绍"""
    skills = get_local_skills()
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

    if not skills_intro:
        main_skills_dir = Path(__file__).parent.parent / "skills"
        skills_readme = main_skills_dir / "SKILLS.md"
        if skills_readme.exists():
            skills_intro = skills_readme.read_text(encoding="utf-8") + "\n\n"

    skills_xml = "<available_skills>\n"
    for skill in skills:
        desc = skill.get("description", "").replace("<", "&lt;").replace(">", "&gt;")
        name = skill.get("qualified_name", skill["name"])
        skills_xml += f"  <skill>\n    <name>{name}</name>\n    <description>{desc}</description>\n  </skill>\n"
    skills_xml += "</available_skills>"

    return skills_intro + skills_xml


# 字体工具函数
def _get_font_family() -> str:
    """获取用户配置的字体"""
    try:
        return Settings.get_instance().llm_font_family.value
    except Exception:
        try:
            return Settings.get_instance().canvas_font_selected.value
        except Exception:
            return "Segoe UI"


def get_canvas_font(size=10, bold=False):
    """获取画布字体"""
    from app.utils.design_tokens import scale_font_size
    font = QFont(_get_font_family(), scale_font_size(size))
    if bold:
        font.setBold(True)
    return font


def get_unified_font(size=10, bold=False):
    """获取统一字体"""
    return get_canvas_font(size, bold)


def get_font_family_css() -> str:
    """获取 CSS font-family 字符串"""
    return f"font-family: '{_get_font_family()}';"


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
    elif hasattr(obj, "serialize") and callable(obj.serialize):
        try:
            return obj.serialize()
        except Exception:
            return str(obj)
    else:
        try:
            json.dumps(obj)
            return obj
        except (TypeError, ValueError):
            return None


def deserialize_from_json(obj):
    """递归将 JSON 对象反序列化"""
    if isinstance(obj, dict):
        return {k: deserialize_from_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [deserialize_from_json(v) for v in obj]
    return obj


class AsyncUpdateChecker(QThread):
    finished = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._parent_ref = weakref.ref(parent) if parent else None
        self._repo = None
        self._platform = None
        self._token = None

    def _get_parent_attr(self, name: str, default=None):
        if self._parent_ref is not None:
            p = self._parent_ref()
            if p is not None:
                return getattr(p, name, default)
        return getattr(self, f"_{name}", default)

    @property
    def repo(self):
        return self._get_parent_attr("repo")

    @repo.setter
    def repo(self, value):
        self._repo = value

    @property
    def platform(self):
        return self._get_parent_attr("platform")

    @platform.setter
    def platform(self, value):
        self._platform = value

    @property
    def token(self):
        return self._get_parent_attr("token")

    @token.setter
    def token(self, value):
        self._token = value

    async def fetch_github(self):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }
        if self.token:
            headers["Authorization"] = f"token {self.token}"
        url = f"https://api.github.com/repos/{self.repo}/releases/latest"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json()
            self.error.emit(f"GitHub API 请求失败：{resp.status_code}")
            return None

    async def fetch_gitee(self):
        headers = {"Authorization": self.token} if self.token else {}
        url = f"https://gitee.com/api/v5/repos/{self.repo}/releases/latest"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json()
            self.error.emit(f"Gitee API 请求失败：{resp.status_code}")
            return None

    async def fetch_gitcode(self):
        headers = {"Authorization": self.token} if self.token else {}
        url = f"https://gitcode.com/api/v5/repos/{self.repo}/releases/latest"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json()
            self.error.emit(f"Gitcode API 请求失败：{resp.status_code}")
            return None

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            fetchers = {"github": self.fetch_github, "gitee": self.fetch_gitee, "gitcode": self.fetch_gitcode}
            result = loop.run_until_complete(fetchers.get(self.platform, lambda: None)())
            if result is None and self.platform not in fetchers:
                self.error.emit("不支持的平台")
        except Exception as e:
            self.error.emit(str(e))
            result = None
        finally:
            self.finished.emit(result)
