---
description: 10 類組件的詳細開發指南與代碼模板
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

## Tools（工具插件化）

> 工具作為插件的一部分註冊：schema / impl / 圖標 / 中文名 / 危險級別 / 分組 / 別名
> 全部由插件聲明，主程序（ToolRegistry）只負責聚合與分發。

### 文件位置

\`\`\`
<plugin>/
├── tools/
│   ├── my_tool.py        ← 每個工具文件暴露 register(registry)
│   └── icons/            ← 深色圖標（tools/icons/*.svg）
│       └── icons_light/  ← 淺色圖標（tools/icons_light/*.svg，可選）
└── .drifox-plugin/
    └── plugin.json       ← components.tools = true
\`\`\`

### 最小模板

\`\`\`python
# tools/my_tool.py
from app.tools.result import ToolResult

def _my_impl(tool_ctx, **kwargs):
    """impl 簽名：impl(tool_ctx, **kwargs)
    tool_ctx: workdir / session_id / call_id / env / services
    """
    return ToolResult(True, content=f"結果: {kwargs.get('text', '')}")

def register(registry):
    registry.register(
        "my_tool",
        {"type": "function", "function": {"name": "my_tool", "description": "描述", "parameters": {"type": "object", "properties": {}}}},
        impl=_my_impl,
        danger="safe",        # 必填：safe | dangerous（未聲明拒絕註冊）
        icon="my_tool",       # SVG 文件名（tools/icons/ 下）
        cn_name="我的工具",    # 中文顯示名
        group="工具組",        # 權限卡片分組
        description="權限卡片描述",
        aliases=["MyTool"],   # 可選：Claude Code 風格別名
    )
\`\`\`

### 註冊元數據（registry.register 參數）

| 參數 | 必填 | 說明 |
|------|------|------|
| name | ✓ | 工具名（小寫，LLM 可見） |
| schema | ✓ | OpenAI function schema（description 給 LLM） |
| impl | 平台工具✓ | 執行函數 impl(tool_ctx, **kwargs) → ToolResult/str/dict |
| danger | ✓ | safe / dangerous（插件工具強制聲明） |
| icon | 建議 | SVG 文件名（不含擴展名） |
| cn_name | 建議 | 中文顯示名（消息卡片/權限卡片） |
| group | 建議 | 權限卡片分組（**同時是能力分組**，見下） |
| description | 建議 | 權限卡片行內描述 |
| aliases | 可選 | Claude Code 風格別名（hook/命令解析用） |
| render | 可選 | body 渲染閉包：render(result, tool_name, tool_args, success) -> str\|None |
| render_mode | 可選 | `""`=默認摺疊卡 / `"inline"`=單行緊湊(無body) / `"expand"`=無摺疊展開 / `"none"`=不渲染 |
| preview | 可選 | 自然語言預覽閉包：preview(tool_args) -> str（inline 卡/摺疊頭） |
| summarize | 可選 | 壓縮摘要閉包：summarize(tool_name, tool_args, content) -> str（歷史壓縮） |
| metadata | 可選 | 行為標記 dict（permission_arg/protect/interactive/ui_managed/...） |

### 渲染三閉包（主程序零工具名硬編碼）

> 工具的**渲染完全由插件聲明**：主程序 `render_helpers` 只做閉包路由 + 通用兜底。
> 參考 `plugins/system/tools/`（bash 終端塊、question 彈窗、screenshot 圖片、
> codegraph 結構化、edit diff 均為插件閉包實現）。

```python
def _render_body(result, tool_name, tool_args, success):
    """完成框 body 渲染：返回 HTML 字符串；None 回退默認渲染（文本/表格/diff/echarts）"""
    from app.widgets.render_helpers import _get_global_font, escape, scale_font_size
    raw = getattr(result, "content", "") or ""
    return f'<pre style="...">{escape(raw)}</pre>'

def _preview(tool_args: dict) -> str:
    """自然語言參數預覽（inline 卡/摺疊頭標題）；空串回退 key=value"""
    return f'處理 "{tool_args.get("path", "")}"'

def _summarize(tool_name, tool_args, tool_content):
    """歷史壓縮 1 行摘要（壓縮器優先調用插件閉包）"""
    return f"[{tool_name}] {_preview(tool_args)} ({len(tool_content)} chars)"
```

### metadata 行為標記

| 標記 | 值 | 效果 |
|------|-----|------|
| `permission_arg` | str | 權限檢查提取該參數（`resolve(name, arg)`；bash→command、read→filePath） |
| `permission_task` | true | 子智能體分發權限（`resolve_task(首個 agent)`） |
| `protect` | true | 歷史壓縮時結果完整保留（skill/todowrite 即此標記） |
| `interactive` | true | 交互式工具：UI 彈窗處理、子智能體禁用執行（question 即此標記） |
| `ui_managed` | true | 專屬 UI 工具：不創建通用流式工具塊 |
| `operation_icons` | dict | 按參數值切換圖標（lsp 的 operation→圖標） |
| `subagent_task` | true | 子智能體任務卡：表格渲染 + 日誌按鈕 |

### group 能力分組

工具註冊的 `group` 同時是權限卡片分組與**能力分組**，主程序按 group 驅動
能力判定（不寫死工具名）：
- **「文件寫入」**（write/edit/multi_edit）→ 團隊 `can_write` 判定、文件備份跟踪、
  自動 LSP 診斷
- **「終端與進程」**（bash/bg_*）→ 終端能力歸組

新寫工具註冊到對應 group 即自動獲得該組的備份/診斷/權限語義。

### impl 簽名與 tool_ctx

\`\`\`python
def impl(tool_ctx, **kwargs):
    workdir = tool_ctx.get("workdir")        # 當前工作目錄
    session_id = tool_ctx.get("session_id")  # 會話上下文
    env = tool_ctx.get("env", {})            # api_keys / app_data_dir / desktop_automation_enabled
    services = tool_ctx.get("services", {})  # 平台能力接口
\`\`\`

- **純邏輯工具**（文件/網絡/桌面）：只用 workdir/env，標準庫/第三方庫獨立實現
- **平台工具**（bash/子智能體/MCP/LSP/CodeGraph/團隊/todo/question/skill/上傳）：
  通過 `services` 能力接口調用（window_state/lsp/codegraph/mcp/gitee 等），
  不直接訪問主程序內部
- 返回：ToolResult（推薦，可帶 diff/image_data 擴展字段）或 str（自動包裝）

### 窗口級狀態（services["window_state"]）

需要**窗口隔離狀態**的工具（每窗口獨立、不跨窗口共享）經通用鍵值容器存取，
無需修改主程序：

```python
ws = tool_ctx.get("services", {}).get("window_state", {})
ws["set"]("my_state", {...})     # 寫入（窗口級）
data = ws["get"]("my_state", {}) # 讀取（窗口級，缺省值）
ws["delete"]("my_state")         # 刪除
```

- 存儲由 tool_executor 按窗口持有（多窗口互不影響），線程安全
- 任意 key 自定義（todo 工具用 key="todo"）；無注入（測試/無窗口）場景需插件自備兜底
- 參考：`plugins/system/tools/task_tools.py`（_todo_state 讀寫模式）

### 圖標自包含

- 深色版：\`tools/icons/<icon>.svg\`（亮色/白色描邊，深色主題可見）
- 淺色版：\`tools/icons_light/<icon>.svg\`（深色描邊，淺色主題可見；缺省回退深色版）
- 渲染自動按主題選擇（data URI 加載），無需註冊到 qrc

### 熱插拔

- \`tools/*.py\` 增/刪/改 → 後台 watcher 自動重掃（1-3 秒生效）
- 執行中的工具調用不受影響（快照機制）
- 同名工具：先註冊者優先（工作樹 plugins/ > 用戶插件目錄）；同插件熱更新可覆蓋

### 關鍵約束

- \`danger\` 未聲明 → registry 拒絕註冊
- \`source\` 由 loader 強制注入 plugin:<name>，插件無法偽裝 builtin
- 修改 tools 後重啟或等 watcher 生效；manifest 更新 components.tools = true

### 參考

- 系統工具真實案例：\`plugins/system/tools/\`（file_tools 自包含、subagent_tools 平台服務）
- 註冊表實現：\`app/tools/registry.py\`
- 掃描/熱重載：\`app/tools/plugin_tool_loader.py\`

## Providers（服務商插件化）

> 服務商支持已全面插件化（萬物為插件）：服務商的一切——圖標、API URL、默認參數、
> 模型列表、models.dev 白名單、family 能力、用量查詢額外配置、餘額/套餐用量查詢——
> 全部由 providers 插件聲明，主程序不再硬編碼任何服務商數據。

### 文件位置

```
plugins/<name>/
├── providers/
│   ├── deepseek.py          # 一個文件註冊一個（或多個）服務商
│   ├── icons/               # 深色主題圖標（<icon>.svg / <icon>.png）
│   └── icons_light/         # 淺色主題圖標（可選；缺省回退深色）
└── .drifox-plugin/
    └── plugin.json          # components 聲明 "providers": true（自動檢測，可選）
```

- 系統內置服務商：`plugins/system/providers/*.py`
- 用戶插件：`<app_data>/plugins/<name>/providers/*.py`
- 熱重載：ProviderWatcher 後台輪詢（path, mtime, size），變更全量重掃；user 插件可覆蓋 system 同名服務商

### 最小模板

```python
# providers/deepseek.py
from app.plugins.registries.provider_registry import (
    ProviderDef,
    make_bearer_balance_fetcher,
)


def register(registry):
    registry.register(
        ProviderDef(
            name="DeepSeek",
            icon="deepseek",
            api_url="https://api.deepseek.com",
            auth_type="bearer",                 # bearer / bce / none / anthropic
            default_model="deepseek-chat",
            default_params={"溫度": 0.7, "最大Token": 200000, "思考等級": "high"},
            register_url="https://platform.deepseek.com/api_keys",
            models=["deepseek-v4-flash", "deepseek-v4-pro"],
            models_dev_id="deepseek",
            family="deepseek",
            capabilities={
                "context_limit": 320000,
                "supports_thinking": True,
                "thinking_param": "thinking",
            },
            extra_quota_fields=[                # 用量查詢額外配置（可選）
                QuotaField(key="server_id", label="Server ID:", placeholder="..."),
            ],
            balance_fetcher=make_bearer_balance_fetcher(   # 餘額查詢（可選）
                url="https://api.deepseek.com/user/balance",
                balance_key="total_balance",
                currency="¥",
            ),
            coding_plan_fetcher=_fetch_coding_plan,        # 套餐用量查詢（可選）
        )
    )
```

### ProviderDef 字段

| 字段 | 說明 | 對應舊硬編碼 |
|------|------|------------|
| `name` | 服務商唯一名 | `FREE_PROVIDERS` key |
| `icon` | 圖標 key（icons/ 文件名或 qrc） | `PROVIDER_ICONS` |
| `icon_dir` / `icon_dir_light` | 插件圖標目錄（**自動注入**，勿手寫） | — |
| `api_url` | 默認 API URL | `FREE_PROVIDERS.API_URL` |
| `auth_type` | 認證方式 bearer/bce/none/anthropic | `FREE_PROVIDERS` 認證方式 |
| `default_model` | 默認模型名 | `FREE_PROVIDERS` 模型名稱 |
| `default_params` | 溫度/最大Token/思考模式等 | `FREE_PROVIDERS` 其餘鍵 |
| `register_url` | 獲取 API Key 地址 | `FREE_PROVIDERS` 獲取地址 |
| `models` | 模型列表 | `PROVIDER_MODELS` |
| `models_dev_id` | models.dev provider id | `MODELS_DEV_PROVIDER_MAP` |
| `family` | 能力族 | `detect_provider_family` |
| `capabilities` | family 能力 | `PROVIDER_CAPABILITIES` |
| `extra_quota_fields` | 用量查詢額外字段（不進 API 請求） | `QUOTA_EXCLUDE_KEYS` + 編輯卡片硬編碼 |
| `balance_fetcher` | 餘額查詢函數 | `BALANCE_APIS` |
| `coding_plan_fetcher` | 套餐用量查詢函數 | coding_plan_fetcher 註冊表 |

### 查詢函數簽名

**餘額 fetcher**：`(config: dict) -> dict | None`

```python
{"balance": 123.4, "currency": "¥"}     # 成功
{"hide": True, "tooltip": "失敗原因"}   # 失敗/無餘額
None                                    # 無 API key 等（不請求）
```

簡單 Bearer GET 場景直接用工廠 `make_bearer_balance_fetcher(url, balance_key, currency="¥")`
（自動處理 `balance_infos[0][key]` / `data.data[key]` / `data[key]` 層級）。

**套餐用量 fetcher**：`(config: dict) -> dict | None`

```python
{"rolling": {"percent": 60, "reset_sec": 123}, "weekly": ..., "monthly": ...}
```

返回 None 表示該服務商暫不支持用量查詢。

### extra_quota_fields 與 QUOTA_EXCLUDE_KEYS

`extra_quota_fields` 聲明的 key 會：

1. 匯入 `ProviderRegistry.quota_exclude_keys()`（全局聚合），這些字段**不會**被當作模型參數發送到 API
   （chat_worker / subagent_worker / model_config_card 均按該集合排除）。
2. 在服務商編輯卡片「套餐用量查詢（可選）」區動態渲染（label + placeholder）。

### 熱插拔

- \`providers/*.py\` 增/刪/改 → ProviderWatcher 後台輪詢（path, mtime, size）變更全量重掃（1-3 秒生效）
- 同名服務商：user 插件優先於 system 內置（覆蓋是預期行為）
- 修改 providers 後重啟或等 watcher 生效；manifest 更新 components.providers = true

### 關鍵約束

- 每個 `providers/*.py` 必須暴露 `register(registry)`，否則 loader 不掃描
- `name` 必須唯一；重複會導致後加載者覆蓋先加載者
- `icon_dir` / `icon_dir_light` 由 loader 自動注入，**勿手寫**
- 修改 providers 後重啟或等 watcher 生效；manifest 更新 components.providers = true

### 參考

- 完整開發指南：\`plugins/system/providers/README.md\`
- 系統服務商真實案例：\`plugins/system/providers/*.py\`
- 註冊表實現：\`app/plugins/registries/provider_registry.py\`
- 測試：\`python -m pytest tests/core/test_provider_registry.py -v\`
