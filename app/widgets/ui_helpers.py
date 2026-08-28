# -*- coding: utf-8 -*-
"""
UI 辅助模块 - 从 main_widget.py 提取的 UI 辅助方法

职责划分：
1. 样式常量：窗口、滚动条、按钮等样式定义
2. 卡片管理：卡片的创建、删除、回收逻辑
3. 消息处理：消息格式化、导出、过滤
4. Diff 辅助：生成文件对比 HTML

使用注意：
- 这些方法独立于主类，可以安全使用
- 部分函数依赖 MessageCard，需确保已导入
- 循环导入通过延迟导入解决
"""

import re
import time
from datetime import datetime
from typing import Any, Callable, List, Optional, Tuple

from loguru import logger
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QWidget

from app.utils.design_tokens import Colors, font_size_css
from app.widgets import MessageCard
from app.widgets.elided_label import _ElidedLabel

__all__ = [
    # 代码保存辅助
    "LANG_EXT_MAP",
    "get_language_extension",
    "extract_code_suggested_filename",
    "get_default_save_filename",
    "export_messages_to_markdown",
    # 样式常量
    "WINDOW_STYLE",
    "CHAT_SCROLL_STYLE",
    "TITLE_STYLE",
    "TitleEditWidget",
    "MODEL_BTN_STYLE",
    "MODEL_BTN_TEXT_STYLE",
    # UI 辅助函数
    "setup_background_label",
    "is_widget_alive",
    "sanitize_user_message_for_display",
    "get_default_timestamp",
    "filter_alive_cards",
    "cleanup_stale_card_cache",
    # 卡片管理辅助
    "normalize_lines",
    "truncate_text",
    "collect_tool_call_ids",
    "format_file_list",
    # 动作颜色辅助
    "ACTION_COLORS",
    "DEFAULT_ACTION_COLOR",
    "get_action_color",
    # Diff 辅助
    "read_backup_files",
    "generate_diff_html",
    "generate_multi_file_diff_html",
    # Node Preview 辅助
    "build_node_preview_data",
    # 卡片删除辅助
    "find_widgets_to_remove_for_round",
    "find_widgets_to_remove_from_round",
    "deduplicate_operations",
    "find_last_assistant_card",
    "count_user_cards_in_layout",
    "collect_message_cards_from_layout",
    "collect_user_card_widgets",
    "create_session_from_record",
    "collect_operations_for_round",
    "get_round_message_indices",
    "create_new_session_state",
    "is_session_empty",
    "truncate_messages_at_round",
    "get_session_compaction_info",
]

# 延迟导入 content_to_text（避免循环导入）
_content_to_text_getter: Optional[Callable] = None


def _get_content_to_text() -> Callable:
    """延迟获取 content_to_text 函数"""
    global _content_to_text_getter
    if _content_to_text_getter is None:
        from app.core import content_to_text

        _content_to_text_getter = content_to_text
    return _content_to_text_getter


# ========== 性能优化：预编译正则表达式 ==========
_CLASS_PATTERN = re.compile(r"class\s+(\w+)")
_FUNC_PATTERN = re.compile(r"def\s+(\w+)|function\s+(\w+)")


# ==================== 代码保存辅助 ====================

LANG_EXT_MAP = {
    "python": ".py",
    "py": ".py",
    "javascript": ".js",
    "js": ".js",
    "typescript": ".ts",
    "ts": ".ts",
    "html": ".html",
    "htm": ".html",
    "css": ".css",
    "scss": ".scss",
    "sass": ".sass",
    "less": ".less",
    "json": ".json",
    "yaml": ".yaml",
    "yml": ".yaml",
    "xml": ".xml",
    "markdown": ".md",
    "md": ".md",
    "shell": ".sh",
    "bash": ".sh",
    "sh": ".sh",
    "sql": ".sql",
    "go": ".go",
    "java": ".java",
    "c": ".c",
    "cpp": ".cpp",
    "c++": ".cpp",
    "csharp": ".cs",
    "cs": ".cs",
    "rust": ".rs",
    "ruby": ".rb",
    "php": ".php",
    "swift": ".swift",
    "kotlin": ".kt",
    "scala": ".scala",
    "r": ".r",
    "lua": ".lua",
    "perl": ".pl",
    "powershell": ".ps1",
    "dockerfile": "Dockerfile",
    "makefile": "Makefile",
    "toml": ".toml",
    "ini": ".ini",
    "cfg": ".cfg",
    "conf": ".conf",
    "txt": ".txt",
    "csv": ".csv",
    "vue": ".vue",
    "jsx": ".jsx",
    "tsx": ".tsx",
    "graphql": ".graphql",
    "proto": ".proto",
    "docker": "Dockerfile",
}


def get_language_extension(lang: str) -> str:
    """获取语言对应的文件扩展名"""
    return LANG_EXT_MAP.get(lang.lower() if lang else "", ".txt")


def extract_code_suggested_filename(code: str, ext: str) -> str:
    """从代码中提取建议的文件名"""
    class_match = _CLASS_PATTERN.search(code)
    func_match = _FUNC_PATTERN.search(code)
    if class_match:
        return class_match.group(1) + ext
    elif func_match:
        return (func_match.group(1) or func_match.group(2)) + ext
    return "code" + ext


def get_default_save_filename(lang: str, code: str) -> str:
    """
    获取默认保存文件名

    Args:
        lang: 语言名称
        code: 代码内容

    Returns:
        默认文件名
    """
    lang_lower = lang.lower() if lang else ""
    ext = get_language_extension(lang)

    # 特殊文件名
    if lang_lower in ("dockerfile", "makefile"):
        return lang_lower

    return extract_code_suggested_filename(code, ext)


def export_messages_to_markdown(messages: list, timestamp: str = None) -> str:
    """
    将消息列表导出为 Markdown 格式

    Args:
        messages: 消息列表
        timestamp: 时间戳，默认为当前时间

    Returns:
        Markdown 格式的对话内容
    """
    from datetime import datetime

    from app.core import content_to_text

    if timestamp is None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = ["# 对话记录\n\n", f"导出时间: {timestamp}\n\n"]

    for msg in messages:
        role = msg.get("role")

        if role == "user":
            content = content_to_text(msg.get("content", ""))
            lines.append(f"## 用户\n\n{content}\n\n")
        elif role == "assistant":
            content = content_to_text(msg.get("content", ""))
            if content:
                lines.append(f"## 助手\n\n{content}\n\n")
        elif role == "tool":
            # 工具调用信息
            tool_name = msg.get("name", "unknown")
            tool_call_id = msg.get("tool_call_id", "")
            arguments = msg.get("arguments", {})
            content = msg.get("content", "")
            success = msg.get("success", True)

            lines.append(f"## 工具调用: {tool_name}\n\n")
            if tool_call_id:
                lines.append(f"**Call ID**: `{tool_call_id}`\n\n")
            if arguments:
                lines.append(f"**参数**:\n```json\n{arguments}\n```\n\n")
            if content:
                lines.append(f"**结果** ({'成功' if success else '失败'}):\n```\n{content}\n```\n\n")
            lines.append("---\n\n")

    return "".join(lines)


# ==================== 样式常量 ====================

WINDOW_STYLE = """
    OpenAIChatToolWindow {
        background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
            stop:0 rgba(10, 14, 22, 255),
            stop:1 rgba(15, 20, 30, 255));
    }
"""

CHAT_SCROLL_STYLE = """
    SingleDirectionScrollArea {
        background-color: transparent;
        border: none;
        border-radius: 18px;
    }
"""


def _get_title_style():
    """获取标题样式（响应主题）"""
    Colors.refresh()
    return f"""
        QLabel {{
            color: {Colors.TEXT_PRIMARY};
            {font_size_css(15)}
            font-weight: bold;
            padding: 6px 4px;
            border-radius: 10px;
            background-color: transparent;
        }}
        QLabel:hover {{
            background-color: {Colors.HOVER_BG};
        }}
    """


def _get_model_btn_style():
    """获取模型按钮样式（响应主题）"""
    Colors.refresh()
    return f"""
        QWidget {{
            background-color: transparent;
            border: none;
            border-radius: 8px;
            padding: 0px;
        }}
        QWidget:hover {{
            background-color: {Colors.HOVER_BG_STRONG};
        }}
    """


def _get_model_btn_text_style():
    """获取模型按钮文字样式（响应主题）"""
    Colors.refresh()
    return f"color: {Colors.TEXT_PRIMARY}; {font_size_css(13)} font-weight: bold; background: transparent;"


# 兼容旧引用
TITLE_STYLE = _get_title_style()
MODEL_BTN_STYLE = _get_model_btn_style()
MODEL_BTN_TEXT_STYLE = _get_model_btn_text_style()


# ==================== 预编译正则 ====================

# 用户消息清理正则
_USER_MESSAGE_PATTERN = re.compile(
    r"^\[Task Stage:.*?\]\n\[Current Goal:.*?\]\n\[Verification:.*?\]\n\n",
    re.DOTALL,
)


# ==================== UI 辅助函数 ====================


def setup_background_label(viewport: QLabel, parent: Optional[object] = None) -> QLabel:
    """
    创建背景图片标签

    Args:
        viewport: 父控件的 viewport
        parent: 父对象

    Returns:
        配置好的背景标签
    """
    from PyQt5.QtWidgets import QGraphicsOpacityEffect

    bg_label = QLabel(viewport)
    bg_label.setPixmap(QPixmap(":/icons/fox_bg.png"))
    bg_label.setScaledContents(True)

    opacity_effect = QGraphicsOpacityEffect(bg_label)
    opacity_effect.setOpacity(0.1)
    bg_label.setGraphicsEffect(opacity_effect)
    bg_label.lower()
    bg_label.setAttribute(Qt.WA_TransparentForMouseEvents)
    bg_label.resize(viewport.size())
    bg_label.show()

    return bg_label


def is_widget_alive(widget: Optional[object]) -> bool:
    """检查 widget 是否仍然存活"""
    if widget is None:
        return False
    try:
        import sip

        return not sip.isdeleted(widget)
    except Exception:
        return True


def sanitize_user_message_for_display(content: str) -> str:
    """
    清理用户消息用于显示

    移除消息开头的任务阶段标记。
    """
    if not isinstance(content, str):
        return content
    return _USER_MESSAGE_PATTERN.sub("", content, count=1)


def get_default_timestamp() -> str:
    """获取默认时间戳"""
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def filter_alive_cards(cards: List[Any]) -> Tuple[List[Any], bool]:
    """
    过滤存活的卡片

    Returns:
        (存活的卡片列表, 是否有卡片被移除)
    """
    alive = [c for c in cards if is_widget_alive(c)]
    removed = len(alive) != len(cards)
    return alive, removed


# ==================== 卡片管理辅助 ====================


def cleanup_stale_card_cache(session_card_cache: dict, all_session_ids: set, max_size: int = 10) -> None:
    """
    清理过期的卡片缓存

    Args:
        session_card_cache: 会话卡片缓存字典
        all_session_ids: 所有有效的会话 ID 集合
        max_size: 最大缓存数量
    """
    # 移除不存在的会话缓存，并清理卡片对象
    stale_ids = set(session_card_cache.keys()) - all_session_ids
    for sid in stale_ids:
        cache_entry = session_card_cache.pop(sid, None)
        # 清理缓存中的卡片对象
        if cache_entry and isinstance(cache_entry, dict):
            cards = cache_entry.get("cards", [])
            for card in cards:
                if hasattr(card, "cleanup"):
                    try:
                        card.cleanup()
                    except Exception:
                        pass
                if hasattr(card, "deleteLater"):
                    try:
                        card.deleteLater()
                    except Exception:
                        pass

    # 如果缓存过大，移除最旧的缓存
    if len(session_card_cache) <= max_size:
        return

    current_ids = all_session_ids & set(session_card_cache.keys())
    for sid in list(session_card_cache.keys()):
        if sid not in current_ids:
            cache_entry = session_card_cache.pop(sid, None)
            # 清理缓存中的卡片对象
            if cache_entry and isinstance(cache_entry, dict):
                cards = cache_entry.get("cards", [])
                for card in cards:
                    if hasattr(card, "cleanup"):
                        try:
                            card.cleanup()
                        except Exception:
                            pass
                    if hasattr(card, "deleteLater"):
                        try:
                            card.deleteLater()
                        except Exception:
                            pass
            if len(session_card_cache) <= max_size:
                break


# ==================== Diff 辅助 ====================


def normalize_lines(content: str) -> list:
    """
    规范化文本行，确保每行都有换行符

    Args:
        content: 原始文本内容

    Returns:
        行列表
    """
    lines = content.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    return lines


def truncate_text(text: str, max_length: int = 300) -> str:
    """
    截断过长的文本

    Args:
        text: 原始文本
        max_length: 最大长度

    Returns:
        截断后的文本
    """
    if len(text) <= max_length:
        return text
    return text[:max_length] + "..."


def collect_tool_call_ids(messages: list, start_idx: int, end_idx: int) -> list:
    """
    收集指定范围内的 tool_call_id

    Args:
        messages: 消息列表
        start_idx: 起始索引
        end_idx: 结束索引

    Returns:
        tool_call_id 列表
    """
    call_ids = []
    for i in range(start_idx, end_idx):
        if i >= len(messages):
            break
        msg = messages[i]
        role = msg.get("role")
        if role == "assistant":
            tool_calls = msg.get("tool_calls", [])
            for tc in tool_calls:
                if isinstance(tc, dict):
                    tid = tc.get("id")
                    if tid and tid not in call_ids:
                        call_ids.append(tid)
        elif role == "tool":
            tid = msg.get("tool_call_id")
            if tid and tid not in call_ids:
                call_ids.append(tid)
    return call_ids


def format_file_list(files: list, max_count: int = 5) -> str:
    """
    格式化文件列表用于显示

    Args:
        files: 文件列表
        max_count: 最大显示数量

    Returns:
        格式化后的字符串
    """
    if not files:
        return ""
    display = files[:max_count]
    result = "\n".join(f"  - {f}" for f in display)
    if len(files) > max_count:
        result += f"\n  ... 还有 {len(files) - max_count} 个文件"
    return result


# ==================== 动作颜色辅助 ====================

ACTION_COLORS = {
    "jump": "#FFA500",
    "create": "#9370DB",
    "generate": "#32CD32",
    "ask": "#FF6347",
    "view": "#4169E1",
}
DEFAULT_ACTION_COLOR = "#888888"


def get_action_color(action: str) -> str:
    """获取动作对应的颜色"""
    return ACTION_COLORS.get(action.lower(), DEFAULT_ACTION_COLOR)


# ==================== Diff 辅助 ====================


def read_backup_files(backup_path: str) -> tuple:
    """
    读取编辑前后的备份文件

    Args:
        backup_path: 编辑前备份文件路径

    Returns:
        (old_content, new_content, backup_file) 或抛出异常
    """
    from pathlib import Path

    backup_file = Path(backup_path)
    after_backup_path = str(backup_file.with_suffix(".after.bak"))

    # 检查编辑后备份是否存在
    after_backup_file = Path(after_backup_path)
    if not after_backup_file.exists():
        raise FileNotFoundError("编辑后备份文件不存在")

    # 读取文件
    with open(backup_path, "r", encoding="utf-8", errors="replace") as f:
        old_content = f.read()
    with open(after_backup_path, "r", encoding="utf-8", errors="replace") as f:
        new_content = f.read()

    return old_content, new_content, backup_file


def generate_diff_html(old_content: str, new_content: str, backup_file) -> str:
    """
    生成 diff HTML 报告

    Args:
        old_content: 原始内容
        new_content: 新内容
        backup_file: 文件路径对象

    Returns:
        HTML 报告字符串
    """
    import difflib

    from app.utils.diff_viewer import DiffHtmlGenerator

    old_lines = normalize_lines(old_content)
    new_lines = normalize_lines(new_content)

    diff = difflib.unified_diff(
        old_lines, new_lines, fromfile=backup_file.name, tofile=backup_file.name, lineterm="\n", n=10
    )

    diff_output = "".join(diff)
    return DiffHtmlGenerator.generate_html_report(diff_output, "")


def generate_multi_file_diff_html(operations: list) -> str:
    """
    为多个文件生成合并的 diff HTML 报告

    Args:
        operations: 文件操作列表，每个元素包含 backup_path

    Returns:
        HTML 报告字符串
    """
    import difflib

    from app.utils.diff_viewer import DiffHtmlGenerator

    diff_parts = []

    for op in operations:
        backup_path = op.get("backup_path", "")
        if not backup_path:
            continue

        try:
            old_content, new_content, backup_file = read_backup_files(backup_path)
        except Exception:
            continue

        old_lines = normalize_lines(old_content)
        new_lines = normalize_lines(new_content)

        diff = difflib.unified_diff(
            old_lines, new_lines, fromfile=f"a/{backup_file.name}", tofile=f"b/{backup_file.name}", lineterm="\n", n=10
        )
        diff_output = "".join(diff)
        if diff_output:
            diff_parts.append(diff_output)

    combined_diff = "".join(diff_parts)
    return DiffHtmlGenerator.generate_html_report(combined_diff, "")


# ==================== Node Preview 辅助 ====================


def build_node_preview_data(messages: list, content_getter: Optional[Callable] = None, max_len: int = 30) -> list:
    """
    从消息列表构建 node preview 数据

    Args:
        messages: 消息列表
        content_getter: 获取消息内容的函数，默认为 content_to_text
        max_len: 最大内容长度

    Returns:
        [(content, timestamp), ...] 列表
    """
    if content_getter is None:
        content_getter = _get_content_to_text()

    node_data = []
    current_user_msg = None

    for msg in messages:
        if msg.get("role") == "user":
            # 跳过 hook 合成消息（如 Stop block 续命注入的 user 消息），
            # 不在时间线显示为节点。与 group_messages_for_display 的
            # _is_hook_message 过滤保持一致，避免节点数与批次映射错位。
            # TeamMail 是团队邮件消息（R7 标记），须放行与问题列表 index 对齐。
            if msg.get("_hook_event") and msg.get("_hook_event") != "TeamMail":
                continue
            content = content_getter(msg.get("content", ""))[:max_len]
            current_user_msg = content
        elif msg.get("role") == "assistant" and current_user_msg:
            timestamp = msg.get("timestamp", "")
            timestamp_short = timestamp[-5:] if timestamp else ""
            node_data.append((current_user_msg, timestamp_short))
            current_user_msg = None

    # 处理最后未配对的 user message
    if current_user_msg:
        node_data.append((current_user_msg, ""))

    return node_data


# ==================== 卡片删除辅助 ====================


def find_widgets_to_remove_for_round(
    chat_layout,
    round_index: int,
    user_card_count: int,
) -> list:
    """
    找出指定 round 需要删除的消息卡片

    Args:
        chat_layout: 聊天布局
        round_index: 目标 round 索引
        user_card_count: 用户消息卡片总数

    Returns:
        需要删除的卡片列表
    """
    if round_index >= user_card_count:
        return []

    widgets_to_remove = []
    user_card_idx = 0
    removing = False

    for i in range(chat_layout.count()):
        item = chat_layout.itemAt(i)
        if not item or not item.widget():
            continue
        widget = item.widget()

        if not hasattr(widget, "role"):
            continue
        if getattr(widget, "_is_welcome", False):
            continue
        if widget.role not in ("user", "assistant"):
            continue

        if widget.role == "user":
            if user_card_idx == round_index:
                widgets_to_remove.append(widget)
                removing = True
            else:
                removing = False
            user_card_idx += 1
        elif widget.role == "assistant" and removing:
            widgets_to_remove.append(widget)

    return widgets_to_remove


def find_widgets_to_remove_from_round(
    chat_layout,
    round_index: int,
    cards_to_remove_hint: int = 0,
) -> list:
    """
    找出从指定 round 到末尾的所有消息卡片（用于撤销操作）

    Args:
        chat_layout: 聊天布局
        round_index: 起始 round 索引
        cards_to_remove_hint: 预期删除的卡片数量（来自 session 数据）

    Returns:
        需要删除的卡片列表
    """
    from loguru import logger

    widgets_to_remove = []
    user_card_idx = 0
    removing = False

    for i in range(chat_layout.count()):
        item = chat_layout.itemAt(i)
        if not item or not item.widget():
            continue
        widget = item.widget()

        if not hasattr(widget, "role"):
            continue
        if getattr(widget, "_is_welcome", False):
            continue
        if widget.role not in ("user", "assistant"):
            continue

        if widget.role == "user":
            if user_card_idx >= round_index:
                widgets_to_remove.append(widget)
                removing = True
            user_card_idx += 1
        elif widget.role == "assistant" and removing:
            widgets_to_remove.append(widget)

    # 关键修复：检查删除数量是否与预期相符
    if cards_to_remove_hint > 0 and len(widgets_to_remove) < cards_to_remove_hint:
        logger.warning(
            f"[UNDO] Partial card removal: found {len(widgets_to_remove)} cards, "
            f"expected {cards_to_remove_hint}. round_index={round_index}. "
            f"This may indicate some cards are not in current layout (lazy loaded)."
        )

    return widgets_to_remove


def deduplicate_operations(operations: list) -> list:
    """
    对文件操作列表去重

    Args:
        operations: 文件操作列表

    Returns:
        去重后的列表
    """
    seen = set()
    unique_ops = []
    for op in operations:
        key = (op.get("id"), op.get("file_path"), op.get("call_id"))
        if key not in seen:
            seen.add(key)
            unique_ops.append(op)
    return unique_ops


def find_last_assistant_card(chat_layout) -> Any:
    """
    找到布局中最后一个 assistant 卡片

    Args:
        chat_layout: 聊天布局

    Returns:
        最后一个 assistant 卡片，或 None
    """
    # 延迟导入避免循环依赖

    for i in range(chat_layout.count() - 1, -1, -1):
        item = chat_layout.itemAt(i)
        if not item or not item.widget():
            continue
        widget = item.widget()
        if not isinstance(widget, MessageCard):
            continue
        if getattr(widget, "_is_welcome", False):
            continue
        if widget.role == "assistant":
            return widget
    return None


def count_user_cards_in_layout(chat_layout) -> int:
    """
    计算布局中的用户消息卡片数量（不包括欢迎卡片）

    Args:
        chat_layout: 聊天布局

    Returns:
        用户消息卡片数量
    """
    # 延迟导入避免循环依赖

    count = 0
    for i in range(chat_layout.count()):
        item = chat_layout.itemAt(i)
        if not item or not item.widget():
            continue
        widget = item.widget()
        if isinstance(widget, MessageCard) and widget.role == "user" and not getattr(widget, "_is_welcome", False):
            count += 1
    return count


def collect_message_cards_from_layout(
    chat_layout,
    filter_func=None,
) -> list:
    """
    从布局中收集消息卡片

    Args:
        chat_layout: 聊天布局
        filter_func: 可选的过滤函数，接受 (widget) 返回 True/False

    Returns:
        卡片列表
    """
    # 延迟导入避免循环依赖

    cards = []
    for i in range(chat_layout.count()):
        item = chat_layout.itemAt(i)
        if not item or not item.widget():
            continue
        widget = item.widget()
        if not isinstance(widget, MessageCard):
            continue
        if getattr(widget, "_is_welcome", False):
            continue
        if filter_func and not filter_func(widget):
            continue
        cards.append(widget)
    return cards


def collect_user_card_widgets(chat_layout) -> list:
    """
    收集布局中的用户消息卡片 widgets

    Args:
        chat_layout: 聊天布局

    Returns:
        用户卡片 widget 列表
    """
    # 延迟导入避免循环依赖

    widgets = []
    for i in range(chat_layout.count()):
        item = chat_layout.itemAt(i)
        if not item or not item.widget():
            continue
        widget = item.widget()
        if isinstance(widget, MessageCard) and widget.role == "user":
            widgets.append(widget)
    return widgets


def create_session_from_record(session_record: dict, messages: list, title: str = None) -> Any:
    """
    从历史记录创建 ChatSession

    Args:
        session_record: 会话记录字典
        messages: 消息列表
        title: 会话标题

    Returns:
        ChatSession 实例
    """
    from app.core import ChatSession

    session_id = session_record.get("session_id")
    title = title or session_record.get("name") or "历史对话"

    return ChatSession.from_dict(
        {
            "session_id": session_id,
            "name": title,
            "messages": messages,
            "topic_summary": title,
            "compaction_state": session_record.get("compaction_state", {}),
            "compaction_cache": session_record.get("compaction_cache", {}),
        }
    )


def collect_operations_for_round(file_recorder, session_id: str, call_ids: list) -> list:
    """
    收集指定 round 的所有文件操作

    Args:
        file_recorder: 文件记录器
        session_id: 会话 ID
        call_ids: tool call ID 列表

    Returns:
        操作列表（已去重）
    """
    operations = []
    for call_id in call_ids:
        ops = file_recorder.get_operations_for_preview(session_id, call_id)
        operations.extend(ops)
    return deduplicate_operations(operations)


def compute_diff_stats(operations: list) -> dict:
    """从操作列表计算差异统计（文件数、新增行、删除行）

    对比备份文件（编辑前）与当前文件内容，统计新增和删除的行数。
    多个操作涉及同一文件时只计算一次（按文件路径去重）。

    Args:
        operations: 从 file_recorder 获取的操作列表

    Returns:
        {"files": int, "additions": int, "deletions": int}
    """
    import difflib
    from pathlib import Path

    # 文件写入组工具（registry 派生，与 tool_executor._is_write_group 同源；异常回退旧名单）
    try:
        from app.tools.registry import ToolRegistry

        write_tools = ToolRegistry.get_instance().tools_in_group("文件写入")
    except Exception:
        write_tools = ["write", "edit", "multi_edit"]

    seen_files = set()
    total_files = 0
    total_additions = 0
    total_deletions = 0

    for op in operations:
        file_path = op.get("file_path", "")
        backup_path = op.get("backup_path", "")
        tool_name = op.get("tool_name", "")

        # 跳过非编辑类操作
        if tool_name not in write_tools:
            continue
        # 按文件路径去重
        if file_path in seen_files:
            continue
        seen_files.add(file_path)

        try:
            old_content = ""
            new_content = ""

            # 读取备份（编辑前）
            if backup_path and Path(backup_path).exists():
                with open(backup_path, "r", encoding="utf-8", errors="replace") as f:
                    old_content = f.read()

            # 读取当前文件（编辑后）
            resolved = Path(file_path)
            if resolved.exists():
                with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                    new_content = f.read()

            old_lines = old_content.splitlines(True) if old_content else []
            new_lines = new_content.splitlines(True) if new_content else []

            diff = list(
                difflib.unified_diff(
                    old_lines,
                    new_lines,
                    fromfile="a",
                    tofile="b",
                    n=0,  # 不显示上下文
                )
            )

            additions = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
            deletions = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))

            if additions > 0 or deletions > 0:
                total_files += 1
                total_additions += additions
                total_deletions += deletions

        except Exception:
            continue

    return {
        "files": total_files,
        "additions": total_additions,
        "deletions": total_deletions,
    }


def get_round_message_indices(session, round_index: int) -> tuple:
    """
    获取指定 round 的消息索引范围

    Args:
        session: ChatSession 对象
        round_index: round 索引

    Returns:
        (start_idx, end_idx) 或 (None, None)
    """
    from app.core import consolidate_messages, get_user_round_ranges

    canonical_messages = consolidate_messages(session.messages)
    round_ranges = get_user_round_ranges(canonical_messages)

    if round_index < 0 or round_index >= len(round_ranges):
        return None, None

    return round_ranges[round_index]


def create_new_session_state(old_session_manager=None, old_chat_engine=None) -> dict:
    """
    创建新会话所需的状态初始化

    Args:
        old_session_manager: 旧的会话管理器
        old_chat_engine: 旧的聊天引擎

    Returns:
        dict 包含 new_session, new_session_id
    """
    # 延迟导入避免循环依赖
    from app.core import SessionManager

    session_manager = SessionManager()
    session_manager.create_new_session()
    new_session = session_manager.get_current_session()
    new_session_id = new_session.session_id

    if old_chat_engine and hasattr(old_chat_engine, "set_session_manager"):
        old_chat_engine.set_session_manager(session_manager)

    return {
        "session_manager": session_manager,
        "new_session": new_session,
        "new_session_id": new_session_id,
    }


def is_session_empty(session) -> bool:
    """
    检查会话是否为空

    Args:
        session: ChatSession 对象

    Returns:
        是否为空
    """
    return not session or not session.messages


def truncate_messages_at_round(session, round_index: int, round_ranges: list) -> bool:
    """
    截断会话消息到指定 round 之前

    Args:
        session: ChatSession 对象
        round_index: round 索引
        round_ranges: round 范围列表

    Returns:
        是否成功
    """
    if round_index < 0 or round_index >= len(round_ranges):
        return False

    cutoff_index = round_ranges[round_index][0]
    session.set_messages(session.messages[:cutoff_index], preserve_compaction=False)
    return True


def get_session_compaction_info(session) -> dict:
    """
    获取会话的压缩信息

    Args:
        session: ChatSession 对象

    Returns:
        dict 包含 compaction_state 和 compaction_cache
    """
    return {
        "compaction_state": getattr(session, "compaction_state", {}),
        "compaction_cache": getattr(session, "compaction_cache", {}),
    }


def save_or_archive_session(
    history_manager,
    session,
    current_session_id,
    compaction_info=None,
    project_fallback: str = None,
) -> str:
    """
    保存或归档会话

    Args:
        history_manager: 历史管理器
        session: ChatSession 对象
        current_session_id: 当前会话 ID
        compaction_info: 压缩信息字典
        project_fallback: 项目归属兜底值（当该会话首次落库且无任何已知归属时使用）；
            通常为 main_widget._current_project。**警告**：仅在已知会话从未保存过时
            才会被采用——对已有 SQLite 记录的会话，永远沿用其原有 project，避免
            "项目切换后老会话被错误划归新项目"或"被默认项目兜底覆盖"。

    Returns:
        新的会话 ID
    """
    if compaction_info is None:
        compaction_info = get_session_compaction_info(session)

    # 🛡️ 优先沿用会话已存在的 project 归属，避免被 "默认项目" 兜底覆盖
    # 优先级（从高到低）：
    #   1. session.originating_project：会话级首发项目快照（用户在哪个项目下首发的对话）
    #   2. 内存缓存 / SQLite 中该 session_id 的已有 project
    #   3. project_fallback（通常是当前 main_widget._current_project）
    resolved_project = None
    if session is not None:
        op = getattr(session, "originating_project", "") or ""
        if op:
            resolved_project = op
    target_session_id = current_session_id or (session.session_id if session else None)
    if resolved_project is None and target_session_id and history_manager:
        try:
            existing = history_manager.get_session_by_session_id(target_session_id)
            if existing:
                existing_project = existing.get("project")
                # 仅当字段确实非空才采用（避免空字符串污染）
                if existing_project:
                    resolved_project = existing_project
        except Exception:
            # 查询失败不影响主流程，走 fallback
            pass
    if resolved_project is None:
        resolved_project = project_fallback

    save_kwargs = dict(compaction_info)
    if resolved_project is not None:
        save_kwargs["project"] = resolved_project

    if current_session_id is not None:
        idx = history_manager.find_index_by_session_id(current_session_id)
        if idx is not None:
            history_manager.update_session(idx, session.messages, **save_kwargs)
            return current_session_id
        else:
            history_manager.save_session(session.messages, session_id=session.session_id, **save_kwargs)
            return session.session_id
    else:
        history_manager.save_session(session.messages, session_id=session.session_id, **save_kwargs)
        return session.session_id


def truncate_and_remove_round(session, round_index, round_ranges, remove_cards_func=None) -> tuple:
    """
    截断并删除指定 round 的消息

    Args:
        session: ChatSession 对象
        round_index: round 索引
        round_ranges: round 范围列表（基于规范消息的索引）
        remove_cards_func: 删除卡片的函数

    Returns:
        (success, old_count, new_count)
    """
    if round_index < 0 or round_index >= len(round_ranges):
        return False, 0, 0

    from app.core import consolidate_messages

    start_idx, end_idx = round_ranges[round_index]
    canonical = consolidate_messages(session.messages)
    old_count = len(canonical)
    new_messages = canonical[:start_idx] + canonical[end_idx:]
    new_count = len(new_messages)

    session.set_messages(new_messages, preserve_compaction=False)

    return True, old_count, new_count


def show_diff_viewer(parent, html, title: str = "文件差异对比") -> Any:
    """
    显示差异查看器（内嵌卡片模式，覆盖右侧对话区域；无全局卡片容器时回退弹窗）

    Args:
        parent: 父控件
        html: HTML 内容
        title: 窗口标题

    Returns:
        DiffViewerWindow 实例 或 GlobalCardController 实例
    """
    logger.debug(f"[DiffViewer] show_diff_viewer title={title}, html_len={len(html or '')}")

    # 内嵌卡片模式：与系统设置一致，覆盖右侧对话区域
    try:
        from app.widgets.cards.global_card_controller import get_global_card_controller

        controller = get_global_card_controller()
        if controller is not None:
            controller.show_diff_viewer(html, title)
            return controller
    except Exception as e:
        logger.warning(f"[DiffViewer] 内嵌模式失败，回退到弹窗: {e}")

    # 回退：弹窗模式
    from app.utils.diff_viewer import DiffViewerWindow

    viewer = DiffViewerWindow(parent=parent, title=title)
    viewer.load_html(html)
    viewer.show()
    return viewer


# 预编译 hook 内容格式正则（与 message_content.py 中的 _is_hook_message 保持一致）
_HOOK_CONTENT_PATTERN = re.compile(
    r"<system-reminder>\s*<[a-z0-9-]+-hook>.*?</[a-z0-9-]+-hook>\s*</system-reminder>", re.DOTALL
)


def _is_hook_message_ui(msg: dict) -> bool:
    """判断消息是否为 hook 内部通知消息（UI 层兜底检查）

    与 message_content._is_hook_message 保持逻辑一致，
    用于渲染路径的防御性检查（正常情况下 batch 已被 group_messages_for_display 过滤）。
    TeamMail 标记放行（团队邮件是用户可见消息，非 hook 内部通知）。
    """
    if msg.get("_hook_event") and msg.get("_hook_event") != "TeamMail":
        return True
    content = msg.get("content", "")
    if isinstance(content, str) and _HOOK_CONTENT_PATTERN.search(content):
        return True
    return False


def render_batch_to_assistant_card(assistant_card, batch: list) -> None:
    """
    将消息批次渲染到 assistant 卡片

    Args:
        assistant_card: Assistant 卡片
        batch: 消息批次列表
    """
    for msg in batch:
        if msg.get("role") == "assistant":
            # 跳过 hook 消息（内部通知不显示到 UI，仅用于 LLM 上下文）
            if _is_hook_message_ui(msg):
                continue

            # 处理思考内容：将 reasoning_content 转换成 <think> 标签格式
            reasoning_content = msg.get("reasoning_content", "")
            content = msg.get("content", "")
            combined_content = ""
            if reasoning_content:
                combined_content += f"<think>{reasoning_content}</think>"
            if content:
                if combined_content:
                    combined_content += "\n\n"
                combined_content += content
            if combined_content:
                assistant_card.append_text(combined_content)
        elif msg.get("role") == "tool" and msg.get("content", ""):
            assistant_card.append_tool_result(
                tool_name=msg.get("name", ""),
                arguments=msg.get("arguments", {}),
                result=msg.get("content", ""),
                success=bool(msg.get("success", True)),
                tool_call_id=msg.get("tool_call_id", ""),
                diff=msg.get("diff"),
                echarts=msg.get("echarts"),
            )
    assistant_card.finish_streaming(history=True)


_scroll_last_time = [0.0]  # 使用 list 实现可变闭包


def scroll_to_bottom_if_streaming(scroll_area, is_streaming: bool, suppress: bool = False) -> None:
    """
    如果正在流式输出则滚动到底部（带时间防抖，最高 20fps）

    Args:
        scroll_area: 滚动区域
        is_streaming: 是否正在流式输出
        suppress: True 时跳过滚底——用户已主动滚离底部（阅读历史），
            流式增量不得把视口拽回（位置保持）
    """
    if is_streaming and not suppress:
        now = time.time()
        if now - _scroll_last_time[0] < 0.05:
            return
        _scroll_last_time[0] = now
        scroll_area.verticalScrollBar().setValue(scroll_area.verticalScrollBar().maximum())


def log_deletion_stats(round_index: int, ui_deleted: bool, old_count: int, new_count: int) -> None:
    """
    记录删除操作的统计信息

    Args:
        round_index: round 索引
        ui_deleted: UI 删除是否成功
        old_count: 旧的消息数量
        new_count: 新的消息数量
    """
    from loguru import logger

    logger.info(f"[DELETE] Starting deletion: round_index={round_index}")
    logger.info(f"[DELETE] UI cards deleted: {ui_deleted}")
    logger.info(f"[DELETE] Session messages updated: {old_count} -> {new_count}")


def setup_user_card_signals(card, delete_callback, undo_callback, action_callback) -> None:
    """
    设置用户消息卡片的信号连接

    Args:
        card: 消息卡片
        delete_callback: 删除回调
        undo_callback: 撤销回调
        action_callback: 动作回调
    """
    card.deleteRequested.connect(lambda: delete_callback(card))
    card.undoRequested.connect(lambda: undo_callback(card))
    card.actionRequested.connect(action_callback)


def restore_input_from_card(input_area, card) -> None:
    """
    从卡片恢复输入框内容

    Args:
        input_area: 输入区域控件
        card: 消息卡片
    """
    from PyQt5.QtGui import QTextCursor

    user_input = card.get_plain_text()

    # 🛡️ 撤回的命令已经是完整命令，不需要再弹出命令卡片
    # setPlainText 会触发 textChanged → _on_slash_trigger_check，
    # 如果恢复的文本以 / 开头会导致命令卡片错误弹出
    if user_input.startswith("/"):
        input_area._suppress_slash_trigger = True

    input_area.setPlainText(user_input)
    input_area.moveCursor(QTextCursor.End)
    input_area._on_text_changed()
    input_area.setFocus()


def find_user_card_at_index(chat_layout, target_index: int) -> Any:
    """
    找到指定索引的 user 卡片

    Args:
        chat_layout: 聊天布局
        target_index: 目标索引

    Returns:
        找到的卡片或 None
    """
    # 延迟导入避免循环依赖
    pair_index = 0
    for i in range(chat_layout.count()):
        item = chat_layout.itemAt(i)
        if not item or not item.widget():
            continue
        widget = item.widget()
        if not isinstance(widget, MessageCard):
            continue
        if widget.role == "user":
            if pair_index == target_index:
                return widget
            pair_index += 1
    return None


def find_user_round_index(session, user_text: str, timestamp: str) -> int:
    """
    从 session 中找到 user 消息对应的 round_index。

    通过在 session.messages 中定位 user 消息，然后计算它是第几个 user。

    Args:
        session: ChatSession 对象
        user_text: 用户消息的纯文本内容
        timestamp: 用户消息的时间戳

    Returns:
        round_index (0-based)，如果找不到返回 -1
    """
    if not session or not hasattr(session, "messages"):
        return -1

    round_index = 0
    for msg in session.messages:
        # 口径与全仓统一（Bug1）：TeamMail 算作 user round（渲染为卡片），
        # 其他 hook（SessionStart 等）不算——与 _get_current_user_round_index 对齐，
        # 避免撤回/差异统计的 round 序号错位。
        if msg.get("_hook_event") and msg.get("_hook_event") != "TeamMail":
            continue
        if msg.get("role") == "user":
            # 检查是否匹配（通过文本内容或时间戳）
            content = msg.get("content", "")
            # 支持纯文本内容或结构化内容
            if isinstance(content, dict):
                content = content.get("text", "")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        content = item.get("text", "")
                        break

            # 通过时间戳或内容匹配
            msg_timestamp = msg.get("timestamp", "")
            if msg_timestamp == timestamp or (user_text and user_text in str(content)):
                return round_index
            round_index += 1

    return -1


def clear_and_show_welcome(
    session, session_card_cache, clear_chat_func, clear_preview_func, get_welcome_func, add_widget_func
) -> None:
    """
    清空聊天并显示欢迎卡片

    Args:
        session: 当前会话
        session_card_cache: 会话卡片缓存
        clear_chat_func: 清空聊天区域的函数
        clear_preview_func: 清空预览的函数
        get_welcome_func: 获取欢迎卡片的函数
        add_widget_func: 添加 widget 的函数
    """
    if session:
        session_card_cache.pop(session.session_id, None)
    clear_chat_func()
    clear_preview_func()
    if session:
        session.clear()
    welcome_card = get_welcome_func()
    add_widget_func(welcome_card)


def init_new_session_after_archive(
    self_widget, new_state, backend=None, clear_chat_func=None, show_welcome_func=None
) -> None:
    """
    归档后初始化新会话

    Args:
        self_widget: 自身 widget（用于访问 session_manager 等）
        new_state: create_new_session_state 返回的状态
        backend: ChatBackend 实例
        clear_chat_func: 清空聊天函数
        show_welcome_func: 显示欢迎函数
    """
    self_widget.session_manager = new_state["session_manager"]
    self_widget._current_session_id = new_state["new_session_id"]

    if backend:
        backend.set_session_context(self_widget._current_session_id)
        # 🛡️ 同步后端 SessionManager 引用，确保 backend.create_session()
        # 使用正确的 SessionManager，而非归档前已废弃的旧实例。
        backend._session_manager = new_state["session_manager"]

    if clear_chat_func:
        clear_chat_func()
    if show_welcome_func:
        show_welcome_func()
    self_widget.title_edit.setText("新对话")


def init_after_loading_session(self_widget, session, session_id, title=None, backend=None) -> None:
    """
    加载会话后初始化

    Args:
        self_widget: 自身 widget
        session: 加载的会话
        session_id: 会话 ID
        title: 会话标题
        backend: ChatBackend 实例
    """
    self_widget.session_manager.set_current_session(session)
    self_widget._history_preview_messages = None
    self_widget._current_session_id = session_id
    self_widget.title_edit.setText(title or "历史对话")

    if backend:
        backend.set_session_context(session_id)


def post_append_user_message(self_widget, user_round_index, update_preview_func=None, sync_preview_func=None) -> None:
    """
    添加用户消息后的后处理

    Args:
        self_widget: 自身 widget
        user_round_index: 用户消息 round 索引（0-based）
        update_preview_func: 更新预览函数
        sync_preview_func: 同步预览函数
    """
    self_widget._current_assistant_round_index = user_round_index
    if update_preview_func:
        update_preview_func()


def build_node_preview_from_session(session, content_to_text_func, max_len: int = 30) -> list:
    """
    从会话构建节点预览数据

    Args:
        session: ChatSession 对象
        content_to_text_func: 内容转文本函数
        max_len: 最大长度

    Returns:
        节点预览数据列表
    """
    from app.core import consolidate_messages

    messages = consolidate_messages(session.messages)
    return build_node_preview_data(messages, content_to_text_func, max_len)


def get_first_file_operation(operations: list) -> tuple:
    """
    获取第一个文件操作

    Args:
        operations: 操作列表

    Returns:
        (success, backup_path, operation)
    """
    if not operations:
        return False, None, None

    op = operations[0]
    backup_path = op.get("backup_path", "")

    if not backup_path:
        return False, None, None

    return True, backup_path, op


def invalidate_session_card_cache(session, session_card_cache) -> None:
    """
    使会话卡片缓存失效

    Args:
        session: ChatSession 对象
        session_card_cache: 会话卡片缓存字典
    """
    if session:
        session_card_cache.pop(session.session_id, None)


def refresh_session_view(
    self_widget, invalidate_cache_func=None, display_session_func=None, refresh_context_func=None
) -> None:
    """
    刷新会话视图

    Args:
        self_widget: 自身 widget
        invalidate_cache_func: 使缓存失效的函数
        display_session_func: 显示会话的函数
        refresh_context_func: 刷新上下文的函数
    """
    if invalidate_cache_func:
        invalidate_cache_func()
    self_widget._history_preview_messages = None
    if display_session_func:
        display_session_func()
    if refresh_context_func:
        refresh_context_func()


def refresh_history_card_if_visible(history_card, refresh_func=None) -> None:
    """
    如果历史卡片可见则刷新

    Args:
        history_card: 历史卡片控件
        refresh_func: 刷新函数
    """
    if history_card and history_card.isVisible() and refresh_func:
        refresh_func()


def delete_widgets_from_layout(widgets_to_remove: list, chat_layout, call_cleanup: bool = True) -> int:
    """
    从布局中删除指定的 widgets

    Args:
        widgets_to_remove: 要删除的 widget 列表
        chat_layout: 聊天布局
        call_cleanup: 是否在删除前调用 cleanup 方法（默认 True，
                      但在需要保留卡片数据用于恢复的场景下设为 False）

    Returns:
        删除的数量
    """
    deleted = 0
    for widget in widgets_to_remove:
        if not is_widget_alive(widget):
            logger.warning(f"[DELETE] Widget already deleted: {widget}")
            continue

        # 调用清理方法（如果有的话）
        if call_cleanup and hasattr(widget, "cleanup"):
            try:
                widget.cleanup()
            except Exception as e:
                logger.warning(f"[DELETE] Widget cleanup failed: {e}")

        # 从 layout 移除：removeWidget + 解除父引用（removeItem/takeAt 仅摘除
        # 布局 item，widget 仍挂在父控件树下，QWebEngineView 的 renderer 进程
        # 不会自然退出；removeWidget 结束父引用后 deleteLater 才能释放 renderer）
        # 🛡️ 必须先 hide() 再 setParent(None)：可见卡片（含 QWebEngineView 原生
        # HWND）直接脱离父窗口树会变独立顶层窗口 → 白窗一闪（切换项目/新建
        # 标签页清理旧卡片时 Chromium 弹出原生窗口）。
        layout_removed = False
        for i in range(chat_layout.count()):
            item = chat_layout.itemAt(i)
            if item and item.widget() is widget:
                widget.hide()
                chat_layout.removeWidget(widget)
                try:
                    widget.setParent(None)
                except Exception:
                    pass
                layout_removed = True
                break

        if layout_removed:
            widget.deleteLater()
            deleted += 1
            logger.info(f"[DELETE] Widget deleted: role={widget.role}")
        else:
            logger.warning(f"[DELETE] Widget not found in layout: role={widget.role}")

    return deleted


def find_last_tool_call_id_after_round(messages: list, round_ranges: list, round_index: int) -> Optional[str]:
    """
    查找指定 round 之后最后一个 tool_call_id

    Args:
        messages: 消息列表
        round_ranges: round 范围列表
        round_index: 目标 round 索引

    Returns:
        最后一个 tool_call_id 或 None
    """
    if round_index < 0 or round_index >= len(round_ranges):
        return None

    # 获取该 round 之后的所有消息的 start index
    _, end_idx = round_ranges[round_index]

    # 查找 end_idx 之后的所有 tool_call_id
    last_call_id = None
    for i in range(end_idx, len(messages)):
        msg = messages[i]
        if msg.get("role") == "tool":
            call_id = msg.get("tool_call_id")
            if call_id:
                last_call_id = call_id

    return last_call_id


def create_assistant_card_widget(
    parent,
    timestamp: str,
    round_index: int,
    model_name: str = None,
    provider_name: str = None,
    config_id: str = None,
    on_action=None,
    on_context_action=None,
    on_tool_diff=None,
    on_card_diff=None,
    on_save_file=None,
    on_subagent_log=None,
    on_review=None,
    immediate_render: bool = False,
) -> Any:
    """
    创建助手消息卡片（带标准配置）

    Args:
        parent: 父控件
        timestamp: 时间戳
        round_index: 轮次索引
        model_name: 模型名称（显示在卡片头部）
        provider_name: 服务商显示名（用于页脚）
        config_id: 服务商配置 UUID（用于精确导航）
        on_action: 动作回调
        on_context_action: 上下文动作回调
        on_tool_diff: 工具差异回调
        on_card_diff: 卡片差异回调
        on_save_file: 保存文件回调
        on_subagent_log: 子智能体日志回调
        on_review: 页脚 Review 按钮回调（收到信号时触发 code-reviewer 子智能体）
        immediate_render: 是否立即创建 QWebEngineView。流式输出需要 True；
                         会话加载设为 False，由懒渲染队列统一控制。

    Returns:
        配置好的 MessageCard
    """
    card = MessageCard(
        parent=parent,
        role="assistant",
        timestamp=timestamp,
        model_name=model_name,
        provider_name=provider_name,
        config_id=config_id,
    )
    card._round_index = round_index
    if immediate_render:
        # 流式输出需要立即渲染，否则内容无处写入
        card.ensure_rendered()
        if card.viewer is not None:
            card.viewer._install_dialog_filter()

    if on_action:
        card.actionRequested.connect(on_action)
    if on_context_action:
        card.contextActionRequested.connect(on_context_action)
    if on_tool_diff:
        card.toolDiffRequested.connect(on_tool_diff)
    if on_card_diff:
        card.cardDiffRequested.connect(on_card_diff)
    if on_save_file:
        card.saveFileRequested.connect(on_save_file)
    if on_subagent_log:
        card.subAgentLogRequested.connect(on_subagent_log)
    if on_review:
        card.reviewRequested.connect(on_review)

    return card


# ==================== 滚动位置辅助 ====================


def calculate_scroll_progress(visible_top: float, viewport_height: float, widget_tops: list) -> tuple:
    """
    计算滚动进度和可见索引

    Args:
        visible_top: 滚动条当前值（可见区域顶部）
        viewport_height: 视口高度
        widget_tops: 用户消息卡片顶部位置列表

    Returns:
        (progress, visible_index)
    """
    anchor_y = visible_top + max(viewport_height / 2, 1)

    if len(widget_tops) == 1:
        return 0.0, 0
    elif anchor_y <= widget_tops[0]:
        return 0.0, 0
    elif anchor_y >= widget_tops[-1]:
        return float(len(widget_tops) - 1), len(widget_tops) - 1
    else:
        progress = 0.0
        for idx in range(len(widget_tops) - 1):
            start_top = widget_tops[idx]
            end_top = widget_tops[idx + 1]
            if start_top <= anchor_y <= end_top:
                span = max(end_top - start_top, 1)
                ratio = (anchor_y - start_top) / span
                progress = idx + ratio
                break
        visible_index = min(max(int(round(progress)), 0), len(widget_tops) - 1)
        return progress, visible_index


def add_message_to_layout(widget, chat_layout, is_alive_func=None) -> None:
    """
    添加消息卡片到布局

    Args:
        widget: 要添加的 widget
        chat_layout: 聊天布局
        is_alive_func: 检查 widget 是否存活的函数
    """
    if is_alive_func and not is_alive_func(widget):
        return
    widget.show()
    if hasattr(widget, "role"):
        if widget.role == "user":
            chat_layout.addWidget(widget, 0, Qt.AlignRight)
        else:
            chat_layout.addWidget(widget, 0, Qt.AlignLeft)
    else:
        chat_layout.addWidget(widget)


# ==================== 标题编辑控件 ====================


class TitleEditWidget(QWidget):
    """标题编辑控件：显示时用 _ElidedLabel（中间省略），点击切换到 QLineEdit 行内编辑

    对外暴露 text() / setText() / setStyleSheet() 等兼容 QLineEdit 的 API，
    使得 main_widget.py 中 self.title_edit 的引用几乎无需修改。
    """

    returnPressed = pyqtSignal()
    editingFinished = pyqtSignal()

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full_text = text
        self._editing = False
        self._label_style_cache = ""  # 存 QSS 中 QLabel 部分
        self._edit_style_cache = ""  # 存 QSS 中 QLineEdit 部分

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

        # 显示标签 — 使用 _ElidedLabel 实现中间省略，与 TabItem 风格一致
        self._label = _ElidedLabel(text, self)
        self._label.setCursor(Qt.IBeamCursor)
        self._label.setMinimumWidth(0)  # 允许缩窄以触发省略
        self._layout.addWidget(self._label)

        # 编辑输入框 — 编辑模式使用
        self._edit = QLineEdit(text, self)
        self._edit.setCursor(Qt.IBeamCursor)
        self._edit.returnPressed.connect(self._on_edit_return)
        self._edit.editingFinished.connect(self._on_edit_finished)
        self._edit.setVisible(False)
        self._layout.addWidget(self._edit)

        self.setReadOnly(True)  # 默认显示模式
        self.setFixedHeight(32)

    # ── 对外兼容 API ──

    def text(self) -> str:
        return self._full_text

    def setText(self, text: str):
        self._full_text = text
        self._label.setText(text)
        self._edit.setText(text)

    def setReadOnly(self, readonly: bool):
        """True = 显示模式（QLabel），False = 编辑模式（QLineEdit）"""
        self._label.setVisible(readonly)
        self._edit.setVisible(not readonly)
        if not readonly:
            self._edit.setFocus()
            self._edit.selectAll()

    def isReadOnly(self) -> bool:
        return self._label.isVisible()

    def setFocus(self):
        if self._edit.isVisible():
            self._edit.setFocus()

    def selectAll(self):
        self._edit.selectAll()

    def clear(self):
        self.setText("")

    # ── 样式代理 ──

    def setStyleSheet(self, style_sheet: str):
        """解析 QSS：QLabel 部分给 label，QLineEdit 部分给 edit"""
        # 分离两种控件的样式
        label_part = ""
        edit_part = ""
        if "QLabel" in style_sheet:
            # 提取 QLabel 样式块
            idx = style_sheet.find("QLabel {")
            if idx >= 0:
                end = style_sheet.find("}", idx)
                if end >= 0:
                    label_part = style_sheet[idx : end + 1]
            # 其余部分给 QLineEdit
            rest = style_sheet.replace(label_part, "")
            if "QLineEdit" in rest or style_sheet:
                edit_part = style_sheet
        else:
            edit_part = style_sheet

        if label_part:
            self._label.setStyleSheet(label_part)
        if edit_part:
            self._edit.setStyleSheet(edit_part)

        self._label_style_cache = label_part or style_sheet
        self._edit_style_cache = edit_part

    # ── 事件处理 ──

    def mouseDoubleClickEvent(self, event):
        """双击标签进入编辑模式"""
        if event.button() == Qt.LeftButton:
            self._editing = True
            self.setReadOnly(False)
        super().mouseDoubleClickEvent(event)

    def _on_edit_return(self):
        self._editing = False
        changed = self._apply_edit()
        if changed:
            self.returnPressed.emit()

    def _on_edit_finished(self):
        self._editing = False
        changed = self._apply_edit()
        if changed:
            self.editingFinished.emit()

    def _apply_edit(self) -> bool:
        """应用编辑，返回 True 表示标题有实际变化"""
        new_text = self._edit.text().strip()
        if new_text and new_text != self._full_text:
            self._full_text = new_text
            self._label.setText(self._full_text)
            self.setReadOnly(True)
            return True
        # 空输入或无变化：恢复原标题，不触发信号
        self._label.setText(self._full_text)
        self.setReadOnly(True)
        return False
