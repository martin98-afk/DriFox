---
description: Hooks（事件钩子）开发指南——文件位置、最小模板、关键约束、排障与样例
---

# Hooks（事件钩子）组件开发

### 文件结构

```
<plugin>/hooks/
├── hooks.json          ← 事件→处理器映射
└── <plugin>_hook.py    ← Python 实现
```

### hooks.json 格式

```json
{
    "SessionStart": "myplugin_hook.on_session_start",
    "PostUserMessage": "myplugin_hook.on_user_message",
    "PostToolUse": "myplugin_hook.on_tool_use"
}
```

### Python 实现模板

```python
"""<plugin> hook 实现。"""

import logging

logger = logging.getLogger(__name__)


def on_session_start(ctx):
    """会话开始时触发。"""
    logger.info("Session started")


def on_user_message(ctx):
    """用户发送消息后触发。"""
    pass


def on_tool_use(ctx):
    """工具调用后触发。"""
    pass
```

### 支持的事件

`SessionStart`、`Stop`、`UserPromptSubmit`、`PreUserMessage`、`PostUserMessage`、`PreAssistantMessage`、`PostAssistantMessage`、`PreToolUse`、`PostToolUse`

### 关键约束

- Python 文件必须能 `python -m py_compile` 通过
- 函数签名接收 `ctx` 上下文参数
- 不要阻塞主线程
- 不处理的事件不需要定义

### 参考

- 完整规范：[docs/hooks.md](https://github.com/martin98-afk/drifox-plugins/blob/main/docs/hooks.md)
- 真实案例：`plugins/system-hooks/hooks/hooks.json`

---
