# -*- coding: utf-8 -*-
"""引用到输入框 —— 文件胶囊 / 选区引用

两条通路（都走能力池，不 import app.core / app.widgets）：

1. ``window.host`` → 宿主窗口 → ``input_area.insert_file_mention()``：
   插原生 @ 文件胶囊（与用户手打 @ 的呈现完全一致）
2. ``input.write`` / ``input.read``：写纯文本（胶囊不可用，或引用要带
   选中内容与行号时走这里）

能力缺失一律返回 False，由调用方决定提示文案，不静默吞掉。
"""

import os
from typing import Optional, Tuple

from loguru import logger

PLUGIN = "file-tree"

# 引用块之间留空行，连续引用多段时不粘连
_SEPARATOR = "\n"


def _cap(cap_id: str):
    """取能力池里的可调用对象；未声明 / 未注册 / 异常一律 None"""
    try:
        from app.plugins.sdk import caps_for

        return caps_for(cap_id, plugin=PLUGIN, surface="ui")
    except Exception as e:  # noqa: BLE001 - 能力面缺失不应打断右键动作
        logger.debug(f"[file-tree] 取能力 {cap_id} 失败: {e}")
        return None


def _resolve_input_area():
    """解析当前活跃窗口的输入框控件（SendableTextEdit）

    解析链与宿主 ``input.write`` 同口径：host.input_area → Tab 模式下活跃
    子窗口的 input_area → 任一登记窗口的 input_area。插件只读这些公开属性，
    不导入宿主模块。
    """
    cap = _cap("window.host")
    if cap is None:
        return None
    try:
        host = cap()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[file-tree] window.host 解析失败: {e}")
        return None
    if host is None:
        return None

    area = getattr(host, "input_area", None)
    if area is not None:
        return area

    # Tab 模式：host 是 TabManagerWindow，输入区在活跃对话子窗口上
    try:
        wins = getattr(host, "_windows", None) or []
        panel = getattr(host, "_tab_panel", None)
        idx = getattr(panel, "_active_index", 0) if panel is not None else 0
        if 0 <= idx < len(wins):
            area = getattr(wins[idx], "input_area", None)
            if area is not None:
                return area
        for win in wins:
            area = getattr(win, "input_area", None)
            if area is not None:
                return area
    except Exception:
        pass
    return None


def _current_input_text() -> Optional[str]:
    """读输入框现有文本；拿不到时返回 None（区别于「空输入框」）

    优先用自己解析到的 input_area 读取：宿主 ``input.read`` 只认
    ``host.input_area``，Tab 模式下 host 是 TabManagerWindow，会返回空串，
    把「读不到」误当成「输入框是空的」→ 追加变覆盖，用户已敲的内容被清掉。
    """
    area = _resolve_input_area()
    reader = getattr(area, "toPlainText", None) if area is not None else None
    if callable(reader):
        try:
            return reader() or ""
        except Exception:
            pass
    cap = _cap("input.read")
    if cap is None:
        return None
    try:
        data = cap()
    except Exception:
        return None
    if isinstance(data, dict):
        return data.get("text", "") or ""
    return None


def write_input_text(text: str, append: bool = True) -> bool:
    """把文本写进输入框；输入框为空时不产生前导换行"""
    if not text:
        return False
    writer = _cap("input.write")
    if writer is None:
        logger.warning("[file-tree] input.write 不可用，引用失败")
        return False
    existing = _current_input_text() if append else None
    # 读不到现有文本（input.read 不可用）时保守追加：宁可多一个空行，
    # 也不能用 append=False 把用户已经敲进去的内容覆盖掉
    append_flag = True if existing is None else bool(append and existing)
    try:
        writer(text, append=append_flag)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[file-tree] 写输入框失败: {e}")
        return False


def insert_file_chip(path: str) -> bool:
    """插一枚原生 @ 文件胶囊（输入框 / 能力不可用时返回 False）"""
    area = _resolve_input_area()
    insert = getattr(area, "insert_file_mention", None) if area is not None else None
    if not callable(insert):
        return False
    try:
        insert(path)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[file-tree] 插文件胶囊失败: {e}")
        return False
    # 正文里的胶囊只是占位符：发送时宿主靠 _attachments 把 [[basename]] 换成
    # 真实路径（图片才真正附给模型）。@ 提及链路是输入框折叠完胶囊后 emit
    # fileMentionConfirmed 让上层登记附件，这里同样发一次，效果等价。
    signal = getattr(area, "fileMentionConfirmed", None)
    if signal is not None:
        try:
            signal.emit(path)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[file-tree] 附件登记信号发射失败: {e}")
    return True


def display_path(path: str, root: str = "") -> str:
    """展示用路径：在项目根内给相对路径，否则给绝对路径"""
    if not path:
        return ""
    if root:
        try:
            rel = os.path.relpath(path, root)
            if rel and not rel.startswith(".."):
                return rel.replace(os.sep, "/")
        except ValueError:
            pass  # 跨盘符
    return path.replace(os.sep, "/")


def fence_language(path: str) -> str:
    """代码围栏语言标识（pygments 别名优先，退回后缀）"""
    try:
        from .highlight import find_lexer

        lexer, _label = find_lexer(path)
        if lexer is not None and getattr(lexer, "aliases", None):
            return str(lexer.aliases[0])
    except Exception:
        pass
    return os.path.splitext(path or "")[1].lstrip(".").lower()


def build_quote_text(
    path: str,
    start: int = 0,
    end: int = 0,
    text: str = "",
    root: str = "",
    language: str = "",
) -> str:
    """拼引用文本：位置行 + （有选中时）带语言标记的内容块

    形如::

        引用 src/app.py:12-20
        ```python
        ...
        ```
    """
    shown = display_path(path, root)
    if start > 0:
        location = f"{shown}:{start}" if end <= start else f"{shown}:{start}-{end}"
    else:
        location = shown
    lines = [f"引用 {location}"]
    if text:
        lang = language or fence_language(path)
        lines.append(f"```{lang}")
        lines.append(text.rstrip("\n"))
        lines.append("```")
    return "\n".join(lines) + _SEPARATOR


def quote_to_input(
    path: str,
    start: int = 0,
    end: int = 0,
    text: str = "",
    root: str = "",
    language: str = "",
) -> Tuple[bool, str]:
    """引用到输入框，返回 ``(成功, 方式)``；方式为 ``"chip"`` / ``"text"`` / ``""``

    - 带选中内容：必须走文本（内容与行号是引用主体）
    - 无内容（整文件 / 图片）：优先原生胶囊，退化到 ``@路径`` 纯文本
    """
    if not path:
        return False, ""
    if text:
        ok = write_input_text(build_quote_text(path, start, end, text, root, language))
        return ok, ("text" if ok else "")
    if insert_file_chip(path):
        return True, "chip"
    ok = write_input_text(f"@{display_path(path, root)}" + _SEPARATOR)
    return ok, ("text" if ok else "")
