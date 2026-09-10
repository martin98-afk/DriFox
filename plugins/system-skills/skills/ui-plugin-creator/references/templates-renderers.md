# UI 插件代码模板 — 内容块渲染器 / 消息元素工厂

> 何时读：在聊天消息流中渲染自定义 HTML 内容、接管特定消息结构时。
> 前置依赖：architecture.md（渲染链路）；渲染器只做展示，交互走 data 属性桥接。
> 产出：register_content_renderer / register_message_factory 注册代码。

## 二、内容块渲染器模板

### 2.1 入口

在 `ui/__init__.py` 中注册：

```python
def register_ui(registry):
    # 清理旧子模块缓存（热重载兼容）
    import sys
    prefix = "ui_plugin_<plugin_name>."
    stale = [k for k in sys.modules if k.startswith(prefix)]
    for k in stale:
        del sys.modules[k]

    from .renderers import render_my_content

    registry.register_content_renderer(
        plugin_name="<plugin-name>",
        type_name="<custom_type>",       # content 中 custom_type 字段
        render_func=render_my_content,
        priority=10,
        metadata={"description": "自定义内容渲染"},
    )
```

### 2.2 渲染器函数

```python
# -*- coding: utf-8 -*-
"""内容块渲染器"""
from html import escape
from typing import Any, Dict


def render_my_content(data: Dict[str, Any], context) -> str:
    """渲染自定义内容块

    Args:
        data: 内容数据（来自消息的 data 字段）
        context: 可选上下文

    Returns:
        HTML 字符串
    """
    # 从 data 中提取数据
    items = data.get("items", [])
    if not items:
        return '<div class="my-empty">无数据</div>'

    cards_html = ""
    for item in items:
        name = escape(item.get("name", ""))
        desc = escape(item.get("description", ""))
        cards_html += f"""
        <div class="my-card">
            <h4>{name}</h4>
            <p>{desc}</p>
            <button class="my-btn" data-id="{escape(item.get('id', ''))}">操作</button>
        </div>
        """

    return f"""
    <div class="my-container">
        {cards_html}
    </div>
    <style>
        .my-container {{ ... }}
        .my-card {{ ... }}
        .my-btn {{ ... }}
    </style>
    """
```

> 交互按钮可以用 `data-*` 属性，通过 WebView 的 bridge 透传到 Python 侧。

---

## 三、消息元素工厂模板

```python
# ui/__init__.py
def register_ui(registry):
    from .factories import my_message_condition, my_message_factory

    registry.register_message_factory(
        plugin_name="<plugin-name>",
        name="my_custom_message",
        condition_func=my_message_condition,
        factory_func=my_message_factory,
        priority=10,
    )


# ui/factories.py
def my_message_condition(message: dict) -> bool:
    """判断消息是否由此工厂处理"""
    return message.get("role") == "assistant" and "my_flag" in message


def my_message_factory(message: dict, parent) -> QWidget:
    """创建自定义消息 widget"""
    # 返回 QWidget，兜底走默认 MessageCard
    return None  # 暂时回退到默认渲染
```

---

