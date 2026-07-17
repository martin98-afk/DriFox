---
description: 8 類組件的詳細開發指南與代碼模板
---

# 組件開發詳細指南

> 按 §3 決策樹選擇對應章節加載。

---

## Commands

### 文件位置

```
<plugin>/commands/<name>.md → 註冊為 /<name>
```

### 最小模板

```markdown
---
description: 一句話說明命令做什麼
type: prompt
parameters:
  - name: "--flag"
    description: "開關參數"
    param_type: flag
  - name: "--value="
    description: "帶值參數"
    param_type: value
prompt_sections:
  --flag: "flag"
  --value: "value"
---

# /<name> 命令

你正在處理 `/<name>` 命令。用戶參數：`$ARGUMENTS`

## 行為

1. 第一步做什麼
2. 第二步做什麼

<!-- section:flag -->
## Flag 模式
此段僅在使用 `--flag` 時追加……
<!-- end -->

<!-- section:value -->
## Value 模式
此段僅在使用 `--value=xxx` 時追加……
<!-- end -->
```

### 三種 type

| type | 說明 | 觸發方式 |
|------|------|---------|
| `prompt` | 提示詞替換，body + 選中段發送給 AI | 用戶輸入 `/xx` |
| `function` | 函數型，觸發 Python 處理器，不發給 AI | 用戶輸入 `/xx` |
| `agent` | 同 prompt，額外支援 `--subagent` 模式 | 用戶輸入 `/xx` |

### 完整 frontmatter 字段

| 字段 | 類型 | 必填 | 說明 |
|------|------|------|------|
| `description` | string | ✅ | 命令簡介 |
| `type` | enum | ✅ | prompt / function / agent |
| `parameters` | list | 選填 | 結構化參數定義（推薦新格式） |
| `argument-hint` | dict | 選填 | 舊式參數提示（兼容） |
| `mutex_groups` | dict | 選填 | 互斥組，同組參數只能選其一 |
| `prompt_sections` | dict | 選填 | 參數→提示詞分段映射 |
| `shortcut` | string | 選填 | 快捷鍵，如 `Ctrl+Shift+C` |
| `tools` | list | 選填 | 工具白名單 |
| `permission` | dict | 選填 | 權限配置（deny 模式） |
| `hidden` | bool | 選填 | true 時不顯示在命令卡片 |

### 參數定義

```yaml
parameters:
  - name: "--quick"
    description: "快速模式"
    param_type: flag
    mutex: mode

  - name: "--save-to="
    description: "輸出路徑"
    param_type: value
    value_options:     # 自動補全候選值
      - json
      - yaml
      - toml

  - name: "<query>"
    description: "搜索關鍵詞"
    param_type: positional
```

### 模板變量

| 變量 | 含義 |
|------|------|
| `$ARGUMENTS` | 用戶在命令後輸入的完整參數 |
| `$PLUGIN_NAME` | 當前插件名 |
| `$PLUGIN_DIR` | 插件根目錄的絕對路徑 |
| `$PROJECT_ROOT` | 當前工作項目根目錄 |

### 參考

- 完整規範：[docs/commands.md](https://github.com/martin98-afk/drifox-plugins/blob/main/docs/commands.md)
- 系統命令案例：`plugins/system/commands/`

---

## Agents

### 文件位置

```
<plugin>/agents/<name>.md → 註冊為 @<name>
```

### 關鍵點

- 定義 AI 角色：行為模式、知識邊界、可用工具
- 支援 `role`、`tools`、`permission`、`description` 等字段
- frontmatter 必含 `description`

### 參考

- 完整規範：[docs/agents.md](https://github.com/martin98-afk/drifox-plugins/blob/main/docs/agents.md)
- 系統 agent 案例：`plugins/system/agents/`

---

## Skills

### 文件位置

```
<plugin>/skills/<name>/SKILL.md → AI 可檢索技能
```

### 最小模板

```markdown
---
name: <skill-name>
description: 一句話描述技能用途，AI 會匹配此字段
---

# <skill-name> — 技能標題

> 技能說明

## 行為

1. 步驟一
2. 步驟二
```

### 關鍵點

- `name` 在 frontmatter 中定義，與目錄名一致
- `description` 是 AI 匹配的唯一依據——**精準描述**
- 結構自由，建議用 ## 分章節
- 可含代碼塊、表格、列表

### 參考

- 完整規範：[docs/skills.md](https://github.com/martin98-afk/drifox-plugins/blob/main/docs/skills.md)
- 最小示例：[plugins/example-plugin/skills/](https://github.com/martin98-afk/drifox-plugins/tree/main/plugins/example-plugin/skills)
- 系統案例：`plugins/system/skills/`（25+ 技能）

---

## Hooks

### 文件結構

```
<plugin>/hooks/
├── hooks.json          ← 事件→處理器映射
└── <plugin>_hook.py    ← Python 實現
```

### hooks.json 格式

```json
{
    "SessionStart": "myplugin_hook.on_session_start",
    "PostUserMessage": "myplugin_hook.on_user_message",
    "PostToolUse": "myplugin_hook.on_tool_use"
}
```

### Python 實現模板

```python
"""<plugin> hook 實現。"""

import logging

logger = logging.getLogger(__name__)


def on_session_start(ctx):
    """會話開始時觸發。"""
    logger.info("Session started")


def on_user_message(ctx):
    """用戶發送消息後觸發。"""
    pass


def on_tool_use(ctx):
    """工具調用後觸發。"""
    pass
```

### 支持的事件

`SessionStart`、`Stop`、`UserPromptSubmit`、`PreUserMessage`、`PostUserMessage`、`PreAssistantMessage`、`PostAssistantMessage`、`PreToolUse`、`PostToolUse`

### 關鍵約束

- Python 文件必須能 `python -m py_compile` 通過
- 函數簽名接收 `ctx` 上下文參數
- 不要阻塞主線程
- 不處理的事件不需要定義

### 參考

- 完整規範：[docs/hooks.md](https://github.com/martin98-afk/drifox-plugins/blob/main/docs/hooks.md)
- 最小示例：[plugins/example-plugin/hooks/](https://github.com/martin98-afk/drifox-plugins/tree/main/plugins/example-plugin/hooks)
- 系統案例：`plugins/system/hooks/hooks.json`

---

## MCP

### 文件位置

```
<plugin>/.mcp.json
```

### 模板

```json
{
    "servers": [
        {
            "name": "my-server",
            "command": "python",
            "args": ["-m", "my_mcp_server"],
            "env": {
                "API_KEY": "${API_KEY}"
            },
            "description": "MCP 伺服器說明"
        }
    ]
}
```

### 參考

- 完整規範：[docs/mcp.md](https://github.com/martin98-afk/drifox-plugins/blob/main/docs/mcp.md)
- example-plugin：[plugins/example-plugin/.mcp.json](https://github.com/martin98-afk/drifox-plugins/blob/main/plugins/example-plugin/.mcp.json)
- 系統案例：`plugins/system/.mcp.json`

---

## LSP

### 文件位置

```
<plugin>/.lsp.json
```

### 模板

```json
{
    "servers": [
        {
            "language": "python",
            "command": "pyright-langserver",
            "args": ["--stdio"],
            "description": "Python 語言伺服器"
        }
    ]
}
```

### 參考

- 完整規範：[docs/lsp.md](https://github.com/martin98-afk/drifox-plugins/blob/main/docs/lsp.md)
- example-plugin：[plugins/example-plugin/.lsp.json](https://github.com/martin98-afk/drifox-plugins/blob/main/plugins/example-plugin/.lsp.json)
- 系統案例：`plugins/system/.lsp.json`

---

## Themes

### 文件位置

```
<plugin>/themes/<name>/*.yaml
```

### 模板

```yaml
# <theme-name>.yaml
name: "<theme-name>"
description: "主題描述"
type: "dark"  # dark / light

colors:
  window_bg: "#1e1e2e"
  card_bg: "#2a2a3e"
  text_primary: "#cdd6f4"
  text_secondary: "#a6adc8"
  accent: "#89b4fa"
  border: "#313244"
  button_bg: "#3a3a4e"
  button_text: "#cdd6f4"
  success: "#a6e3a1"
  warning: "#f9e2af"
  error: "#f38ba8"
```

### 顏色 Token 說明

Token 定義取決於 DriFox 主題系統支持的字段。參考現有主題了解完整 token 列表。

### 參考

- 完整規範：[docs/themes.md](https://github.com/martin98-afk/drifox-plugins/blob/main/docs/themes.md)
- 最小示例：[plugins/example-plugin/themes/](https://github.com/martin98-afk/drifox-plugins/tree/main/plugins/example-plugin/themes)
- 系統案例：`plugins/system/themes/`（11 個主題）

---

## UI

> 🟡 **UI 插件開發請調用 `ui-plugin-creator` 技能。**
> 此處僅提供架構參考與雙技能橋接資訊。

### 文件位置

```
<plugin>/ui/
├── __init__.py          ← 必須定義 register_ui(registry)
└── *.py                 ← widget 模塊
```

### register_ui 模板

```python
"""UI 插件入口。"""

from drifox.ui.registry import UIPluginRegistry


def register_ui(registry: UIPluginRegistry):
    """由 DriFox 啟動時調用。"""

    # 註冊浮動卡片
    registry.register_floating_card(
        card_id="my-card",
        title="我的卡片",
        widget_class=MyCardWidget,
    )

    # 註冊內容塊渲染器
    registry.register_content_renderer(
        custom_type="my-content",
        renderer=MyContentRenderer,
    )
```

### 3 類 UI 擴展點

| 擴展點 | 使用方法 | 場景 |
|--------|---------|------|
| 浮動卡片 | `register_floating_card(id, title, widget_class)` | 獨立面板、儀表板、管理介面 |
| 內容渲染器 | `register_content_renderer(type, renderer)` | 自定義消息渲染（HTML/表格/圖表） |
| 消息工廠 | `register_message_factory(matcher, factory)` | 高級：接管整個消息結構 |

### 參考

- 架構說明：[docs/architecture.md §ui 組件](https://github.com/martin98-afk/drifox-plugins/blob/main/docs/architecture.md)
- 浮動卡片案例：[plugins/context-usage-stats/](https://github.com/martin98-afk/drifox-plugins/tree/main/plugins/context-usage-stats)
- 完整 UI 插件：[plugins/plugin-marketplace/](https://github.com/martin98-afk/drifox-plugins/tree/main/plugins/plugin-marketplace)
- **開發 UI 插件**：調用 `ui-plugin-creator` 技能
