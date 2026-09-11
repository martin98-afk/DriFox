---
description: Tools（工具插件化）开发指南——文件位置、最小模板、关键约束、排障与样例
---

# Tools（工具插件化）组件开发

> 工具作为插件的一部分注册：schema / impl / 图标 / 中文名 / 危险级别 / 分组 / 别名
> 全部由插件声明，主程序（ToolRegistry）只负责聚合与分发。

### 文件位置

```
<plugin>/
├── tools/
│   ├── my_tool.py        ← 每个工具文件暴露 register(registry)
│   └── icons/            ← 深色图标（tools/icons/*.svg）
│       └── icons_light/  ← 浅色图标（可选，缺省回退深色版）
└── .drifox-plugin/
    └── plugin.json       ← components.tools = true
```

### 最小模板

```python
# tools/my_tool.py
from app.tools.result import ToolResult

def _my_impl(tool_ctx, **kwargs):
    """impl 签名：impl(tool_ctx, **kwargs)
    tool_ctx: workdir / session_id / call_id / env / services
    """
    return ToolResult(True, content=f"结果: {kwargs.get('text', '')}")

def register(registry):
    registry.register(
        "my_tool",
        {"type": "function", "function": {"name": "my_tool", "description": "描述", "parameters": {"type": "object", "properties": {}}}},
        impl=_my_impl,
        danger="safe",        # 必填：safe | dangerous（未声明拒绝注册）
        icon="my_tool",       # SVG 文件名（tools/icons/ 下）
        cn_name="我的工具",    # 中文显示名
        group="工具组",        # 权限卡片分组
        description="权限卡片描述",
        aliases=["MyTool"],   # 可选：Claude Code 风格别名
    )
```

### 注册元数据（registry.register 参数）

| 参数 | 必填 | 说明 |
|------|------|------|
| name | ✓ | 工具名（小写，LLM 可见） |
| schema | ✓ | OpenAI function schema（description 给 LLM） |
| impl | 平台工具✓ | 执行函数 impl(tool_ctx, **kwargs) → ToolResult/str/dict |
| danger | ✓ | safe / dangerous（插件工具强制声明） |
| icon | 建议 | SVG 文件名（不含扩展名） |
| cn_name | 建议 | 中文显示名（消息卡片/权限卡片） |
| group | 建议 | 权限卡片分组（**同时是能力分组**，见下） |
| description | 建议 | 权限卡片行内描述 |
| aliases | 可选 | Claude Code 风格别名（hook/命令解析用） |
| render | 可选 | body 渲染闭包：render(result, tool_name, tool_args, success) -> str\|None |
| render_mode | 可选 | `""`=默认折叠卡 / `"inline"`=单行紧凑(无body) / `"expand"`=无折叠展开 / `"none"`=不渲染 |
| preview | 可选 | 自然语言预览闭包：preview(tool_args) -> str（inline 卡/折叠头） |
| summarize | 可选 | 压缩摘要闭包：summarize(tool_name, tool_args, content) -> str（历史压缩） |
| metadata | 可选 | 行为标记 dict（见下表） |

### 渲染三闭包（主程序零工具名硬编码）

> 工具的**渲染完全由插件声明**：主程序 `render_helpers` 只做闭包路由 + 通用兜底。
> 参考 `plugins/system-tools/tools/`（bash 终端块、question 弹窗、screenshot 图片、
> codegraph 结构化、edit diff 均为插件闭包实现）。

```python
def _render_body(result, tool_name, tool_args, success):
    """完成框 body 渲染：返回 HTML 字符串；None 回退默认渲染（文本/表格/diff/echarts）"""
    from app.widgets.render_helpers import _get_global_font, escape, scale_font_size
    raw = getattr(result, "content", "") or ""
    return f'<pre style="...">{escape(raw)}</pre>'

def _preview(tool_args: dict) -> str:
    """自然语言参数预览（inline 卡/折叠头标题）；空串回退 key=value"""
    return f'处理 "{tool_args.get("path", "")}"'

def _summarize(tool_name, tool_args, content) -> str:
    """历史压缩的 1 行摘要；未注册回退通用 [name] args (N chars)"""
    return f"处理了 {tool_args.get('path', '')}"
```

### metadata 行为标记

| 标记 | 值 | 效果 |
|------|-----|------|
| `permission_arg` | str | 权限检查提取该参数（`PermissionResolver.resolve(name, arg)`） |
| `permission_task` | true | 子智能体分发权限（`resolve_task(首个 agent)`） |
| `protect` | true | 压缩时结果完整保留（历史压缩跳过裁剪） |
| `interactive` | true | 交互式工具：UI 弹窗处理、子智能体禁用执行 |
| `ui_managed` | true | 专属 UI 工具：不创建通用流式工具块 |
| `operation_icons` | dict | 按参数值切换图标（如 lsp 的 operation→图标） |
| `subagent_task` | true | 子智能体任务卡：表格渲染 + 日志按钮 |

### group 能力分组

工具注册的 `group` 同时是权限卡片分组与**能力分组**，主程序按 group 驱动能力判定（不写死工具名）：

- 「文件写入」分组（write/edit/multi_edit）→ 团队 `can_write`、文件备份跟踪、自动 LSP 诊断
- 新写工具注册到该 group 即自动获得备份/诊断能力

### impl 签名与 tool_ctx

```python
def _impl(tool_ctx, **kwargs):
    # tool_ctx 键：
    #   workdir     当前工作目录
    #   session_id / call_id   会话上下文
    #   env         api_keys / app_data_dir / desktop_automation_enabled
    #   services    平台能力接口（todo/terminal/subagent/team/lsp/codegraph/
    #               mcp/ask_user/skills/gitee/diagnostics）— 仅平台工具需要
    ...
```

- 纯逻辑工具（文件/网络/桌面）：impl 用标准库/第三方库独立实现，不依赖主程序
- 平台工具：通过 `tool_ctx["services"]` 调用平台能力，不直接访问主程序内部

### 图标自包含

`tools/icons/*.svg`（深色）+ `tools/icons_light/*.svg`（浅色），渲染按主题加载（缺浅色版回退深色/qrc）。

### 热插拔

`tools/*.py` 文件增/删/改自动热生效（后台 watcher 轮询），无需重启；同名工具先注册者优先（工作树 plugins/ 优先于用户插件目录）。

### 参考

- `plugins/system-tools/tools/`（33 个系统工具真实案例）
- `app/tools/registry.py`（ToolRegistration 字段定义）
- `app/tools/plugin_tool_loader.py`（扫描/热重载实现）
- 最小可运行样例：本技能 `examples/tool-plugin/`

---
