---
name: plugin-creator
description: "DriFox 插件全生命周期开发技能。涵盖全部 8 类组件（commands/agents/skills/hooks/mcp/lsp/themes/ui），从脚手架生成 → 本地开发/调试 → 验证 → 发布到 drifox-plugins 官方市场的完整流程。UI 组件开发桥接 ui-plugin-creator 技能。"
---

# plugin-creator — DriFox 插件開發技能

> **從需求到發布，一鍵生成 DriFox 插件。**
>
> 本技能取代舊的 `/plugin` 管理命令（已被 plugin-manager/plugin-marketplace UI 取代），
> 專注於**插件開發**——從零創建、修改、除錯、發布 DriFox 插件。

---

## 📋 目錄

- [0. 前置準備](#0-前置準備)
- [1. 加載流程](#1-加載流程)
- [2. 插件架構速覽](#2-插件架構速覽)
- [3. 決策樹](#3-決策樹)
- [4. 開發工作流](#4-開發工作流)
- [5. 組件開發指引](#5-組件開發指引)
- [6. 測試與驗證](#6-測試與驗證)
- [7. 發布到市場](#7-發布到市場)
- [8. 常見陷阱](#8-常見陷阱)
- [9. references/ 索引](#9-references-索引)

---

## 0. 前置準備

開發前確保掌握以下工具：

| 項目 | 說明 |
|------|------|
| **drifox-dev** 技能 | DriFox 項目開發基礎環境、編碼規範（**先加載它**） |
| **ui-plugin-creator** 技能 | UI 插件開發（`components.ui=true` 時必用） |
| **drifox-plugins** 倉庫 | `https://github.com/martin98-afk/drifox-plugins` — 官方市場原始碼、文檔、schema、驗證工具 |

> ⚠️ 沒有加載 `drifox-dev` → 先加載它，否則可能違反項目約定。
> ⚠️ UI 插件請使用 `ui-plugin-creator` 技能，本技能僅提供 UI 組件的架構參考。

---

## 1. 加載流程

```
Step 1  讀 SKILL.md 本體 ← 你正在看的這個文件
        ├─ 理解插件生態 → §2
        ├─ 理解要做什麼 → §3 決策樹
        └─ 跟著 §4 工作流走

Step 2  按 §3 決策樹分派任務：
        ├─ 新建插件、加組件 → 讀 references/components.md
        ├─ 改 manifest      → 讀 references/manifest.md
        ├─ 測試/驗證        → 讀 references/testing.md
        ├─ 發布到市場       → 讀 references/publishing.md
        ├─ 常見問題         → 讀 references/troubleshooting.md 或 §8
        └─ UI 插件          → 調用 ui-plugin-creator 技能

Step 3  推進中更新 manifest、跑驗證
Step 4  完成 → 按 §6 跑完整驗證
```

---

## 2. 插件架構速覽

### 2.1 一個插件長這樣（位於 `~/.drifox/plugins/<name>/` 下）

```
your-plugin/
├── .drifox-plugin/
│   └── plugin.json          ← 插件 manifest（必需！插件身份證）
│
├── commands/                ← 斜杠命令 /xxx（*.md）
│   ├── hello.md
│   └── scan.md
│
├── agents/                  ← @name 智能體（*.md）
│   └── assistant.md
│
├── skills/<name>/           ← AI 技能（SKILL.md）
│   └── SKILL.md
│
├── hooks/                   ← 事件鉤子
│   ├── hooks.json           ← 事件聲明
│   └── myplugin_hook.py     ← Python 實現
│
├── themes/<name>/           ← 主題配色（*.yaml）
│   └── dracula.yaml
│
├── ui/                      ← UI 組件（浮動卡片/渲染器/工廠）
│   ├── __init__.py          ← 必須：register_ui(registry)
│   └── my_card.py
│
├── .mcp.json                ← MCP 伺服器配置（插件根目錄）
├── .lsp.json                ← LSP 語言伺服器配置（插件根目錄）
├── README.md                ← 插件說明
└── __init__.py              ← Python 包標記（可選）
```

### 2.2 8 類組件速查

| # | 組件 | manifest flag | 必備文件 | 觸發方式 | 適用場景 |
|---|------|--------------|---------|---------|---------|
| 1 | **Commands** | `commands: true` | `commands/*.md` | 用戶輸入 `/xxx` | 斜杠命令（prompt/function/agent） |
| 2 | **Agents** | `agents: true` | `agents/*.md` | 用戶輸入 `@xxx` | 限定任務域的 AI 角色 |
| 3 | **Skills** | `skills: true` | `skills/<name>/SKILL.md` | AI 自動匹配 | 注入領域知識與最佳實踐 |
| 4 | **Hooks** | `hooks: true` | `hooks/hooks.json` + `*.py` | DriFox 事件觸發 | 自動攔截/記錄/增強 |
| 5 | **MCP** | `mcp: true` | `.mcp.json`（插件根） | DriFox 啟動 | 註冊外部 MCP 伺服器 |
| 6 | **LSP** | `lsp: true` | `.lsp.json`（插件根） | DriFox 啟動 | 註冊語言伺服器 |
| 7 | **Themes** | `themes: true` | `themes/<name>/*.yaml` | 用戶 `/theme xx` | 配色方案 |
| 8 | **UI** | `ui: true` | `ui/__init__.py` + widgets | DriFox 啟動 + 命令 | 浮動卡片/內容渲染器/消息工廠 |

### 2.3 官方資源

| 資源 | 位置 |
|------|------|
| **官方市場倉庫** | [github.com/martin98-afk/drifox-plugins](https://github.com/martin98-afk/drifox-plugins) |
| **插件 Schema** | [schemas/plugin.schema.json](https://github.com/martin98-afk/drifox-plugins/blob/main/schemas/plugin.schema.json) |
| **完整文檔** | [docs/](https://github.com/martin98-afk/drifox-plugins/tree/main/docs)（plugin-manifest / commands / agents / skills / hooks / mcp / lsp / themes / architecture） |
| **最小參考實現** | [plugins/example-plugin/](https://github.com/martin98-afk/drifox-plugins/tree/main/plugins/example-plugin)（**最佳起點**） |
| **生產 UI 參考** | [plugins/context-usage-stats/](https://github.com/martin98-afk/drifox-plugins/tree/main/plugins/context-usage-stats)（浮動卡片實際案例） |
| **驗證工具** | [tools/validate_plugins.py](https://github.com/martin98-afk/drifox-plugins/blob/main/tools/validate_plugins.py) |
| **marketplace 生成** | [tools/generate_marketplace.py](https://github.com/martin98-afk/drifox-plugins/blob/main/tools/generate_marketplace.py) |
| **DriFox 系統插件** | `plugins/system/`（system type 參考，**不要手動修改**） |

> 插件運行的權威實現在 DriFox 內置的 `plugins/system/`（system 插件），本技能文檔僅為開發指引。

---

## 3. 決策樹

| 你說 | 任務類型 | 執行 |
|------|---------|------|
| "做個新插件""創建插件" | **新建** | §4.1 Scaffold → 按組件類型加載指引 |
| "加個 /xx 命令" | **Commands** | §5.1 或 `references/components.md §Commands` |
| "做個 @xx 智能體" | **Agents** | §5.2 或 `references/components.md §Agents` |
| "做個技能""寫 SKILL.md" | **Skills** | §5.3 或 `references/components.md §Skills` |
| "加個鉤子""事件驅動" | **Hooks** | §5.4 或 `references/components.md §Hooks` |
| "配置 MCP 伺服器" | **MCP** | §5.5 或 `references/components.md §MCP` |
| "配置 LSP 語言伺服器" | **LSP** | §5.6 或 `references/components.md §LSP` |
| "做個主題""改配色" | **Themes** | §5.7 或 `references/components.md §Themes` |
| "做個 UI 插件""浮動卡片" | **UI** | → **調用 `ui-plugin-creator` 技能** |
| "改 plugin.json" | **Manifest** | `references/manifest.md` |
| "驗證""跑測試" | **驗證** | §6 或 `references/testing.md` |
| "發布到市場""提 PR" | **發布** | §7 或 `references/publishing.md` |
| "不工作""報錯" | **除錯** | `references/troubleshooting.md` 或 §8 |
| "改現有插件" | **修改** | 跳過 Scaffold，直接改對應組件 + 更新 version |

> ⚠️ **UI 插件** → 不在此技能處理，調用 `ui-plugin-creator`。
> ⚠️ **新建插件** → 先 Scaffold（§4.1），再按組件類型逐一實現。
> ⚠️ **需求不明確時** → 先用 `brainstorming` 技能釐清。

---

## 4. 開發工作流

### 4.1 Scaffold — 新建插件骨架

所有插件建立在 `~/.drifox/plugins/` 下，DriFox 的 watchfiles 會自動熱加載。

最快的起點是下載 `example-plugin`，它展示了全部 8 類組件的標準寫法：

```
① 從官方市場 GitHub 倉庫獲取 example-plugin：
   git clone --depth=1 --filter=blob:none --no-checkout \
     https://github.com/martin98-afk/drifox-plugins.git /tmp/dfp
   cd /tmp/dfp
   git sparse-checkout set plugins/example-plugin
   git checkout main
   cp -r plugins/example-plugin ~/.drifox/plugins/<your-plugin>
   rm -rf /tmp/dfp

② 修改 manifest：
   編輯 ~/.drifox/plugins/<your-plugin>/.drifox-plugin/plugin.json →
   - name:        "<your-plugin>"（小寫 kebab-case，與目錄名一致）
   - description: "一句話描述"
   - version:     "0.1.0"
   - author:      你的名字
   - components:  只保留你需要的 flag

③ 清理不需要的組件目錄與文件：
   用不到的組件直接刪除對應目錄，並在 plugin.json 中設為 false

④ 按 §5 開發各組件
```

> 💡 也可以不複製，直接在 `~/.drifox/plugins/<your-plugin>/` 下手動建目錄 + 寫 plugin.json（參考 [manifest 完整字段](https://github.com/martin98-afk/drifox-plugins/blob/main/docs/plugin-manifest.md)）。

### 4.2 迭代開發循環

```
修改文件 → DriFox watchfiles 熱更新（1-3秒） → 測試效果 → 再改
```

- **commands** 和 **skills** 修改後立即生效
- **hooks** / **mcp** / **lsp** 修改後可能需要重啟 DriFox
- **themes** 修改後用 `/theme <name>` 切換查看
- **ui** 修改後卡片自動重新載入
- 用 `/plugin-manager` 檢查插件狀態

### 4.3 版本管理

```json
// 開發階段：0.1.x
"version": "0.1.0"

// 首次發布：1.0.0
// 遵循 SemVer：major.minor.patch
// 破壞性變更 → 升 major
// 新增功能 → 升 minor
// Bug 修復 → 升 patch
```

---

## 5. 組件開發指引

每類組件的關鍵約束與快速參考。完整模板見 `references/components.md`。

### 5.1 Commands

```
commands/<name>.md → 註冊為 /<name> 斜杠命令
```

**關鍵約束**：
- 文件名 = 命令名，必須 `^[a-z][a-z0-9-]*\.md$`
- frontmatter 必含 `description` + `type`（prompt/function/agent）
- 參數用 `parameters` 或 `argument-hint` 定義
- 分段提示詞用 `<!-- section:id -->` / `<!-- end -->` 包裹
- 可用 `$ARGUMENTS`、`$PLUGIN_NAME`、`$PLUGIN_DIR`、`$PROJECT_ROOT` 模板變量

**參考**：
- [docs/commands.md](https://github.com/martin98-afk/drifox-plugins/blob/main/docs/commands.md) — 完整規範
- [plugins/example-plugin/commands/](https://github.com/martin98-afk/drifox-plugins/tree/main/plugins/example-plugin/commands) — 最小示例
- `plugins/system/commands/` — 系統命令真實案例

### 5.2 Agents

```
agents/<name>.md → 註冊為 @<name> 智能體
```

**關鍵約束**：
- 定義 AI 角色、行為邊界、可用工具
- 支持 `role`、`tools`、`permission` 等字段

**參考**：
- [docs/agents.md](https://github.com/martin98-afk/drifox-plugins/blob/main/docs/agents.md)
- [plugins/example-plugin/agents/](https://github.com/martin98-afk/drifox-plugins/tree/main/plugins/example-plugin/agents)
- `plugins/system/agents/`

### 5.3 Skills

```
skills/<name>/SKILL.md → AI 可檢索的技能
```

**關鍵約束**：
- frontmatter 必含 `name` + `description`
- 結構自由，但建議含 # 標題 + 章節
- AI 自動匹配 `description` 關鍵詞

**參考**：
- [docs/skills.md](https://github.com/martin98-afk/drifox-plugins/blob/main/docs/skills.md)
- [plugins/example-plugin/skills/](https://github.com/martin98-afk/drifox-plugins/tree/main/plugins/example-plugin/skills)
- `plugins/system/skills/`（25+ 技能真實案例）

### 5.4 Hooks

```
hooks/
├── hooks.json          ← 事件聲明（哪個事件觸發哪個函數）
└── <plugin>_hook.py    ← Python 實現
```

**關鍵約束**：
- `hooks.json` 格式：`{ "事件名": "函數引用路徑" }`
- Python 文件必須能 `python -m py_compile` 通過
- 支持事件：`SessionStart`、`Stop`、`UserPromptSubmit`、`PreUserMessage`、`PostUserMessage`、`PreAssistantMessage`、`PostAssistantMessage`、`PreToolUse`、`PostToolUse`

**參考**：
- [docs/hooks.md](https://github.com/martin98-afk/drifox-plugins/blob/main/docs/hooks.md)
- [plugins/example-plugin/hooks/](https://github.com/martin98-afk/drifox-plugins/tree/main/plugins/example-plugin/hooks)
- `plugins/system/hooks/hooks.json`

### 5.5 MCP（Model Context Protocol）

```
.mcp.json（插件根目錄） → 注入 MCP 伺服器
```

**關鍵約束**：
- JSON 格式：MCP 伺服器配置陣列
- 每個伺服器含 `name`、`command`/`url`、`args`、`env` 等

**參考**：
- [docs/mcp.md](https://github.com/martin98-afk/drifox-plugins/blob/main/docs/mcp.md)
- [plugins/example-plugin/.mcp.json](https://github.com/martin98-afk/drifox-plugins/blob/main/plugins/example-plugin/.mcp.json)
- `plugins/system/.mcp.json`

### 5.6 LSP（Language Server Protocol）

```
.lsp.json（插件根目錄） → 注入 LSP 語言伺服器
```

**關鍵約束**：
- JSON 格式：LSP 伺服器配置陣列
- 每個伺服器含 `language`、`command`、`args` 等

**參考**：
- [docs/lsp.md](https://github.com/martin98-afk/drifox-plugins/blob/main/docs/lsp.md)
- [plugins/example-plugin/.lsp.json](https://github.com/martin98-afk/drifox-plugins/blob/main/plugins/example-plugin/.lsp.json)
- `plugins/system/.lsp.json`

### 5.7 Themes

```
themes/<name>/*.yaml → 配色方案
```

**關鍵約束**：
- YAML 格式，定義顏色 token
- token 涵蓋：窗口、背景、卡片、文本、按鈕、邊框等

**參考**：
- [docs/themes.md](https://github.com/martin98-afk/drifox-plugins/blob/main/docs/themes.md)
- [plugins/example-plugin/themes/](https://github.com/martin98-afk/drifox-plugins/tree/main/plugins/example-plugin/themes)
- `plugins/system/themes/`（11 個主題真實案例）

### 5.8 UI

> 🟡 **UI 插件開發請調用 `ui-plugin-creator` 技能。**
> 本技能僅提供架構上下文。

```
ui/
├── __init__.py          ← 必須定義 register_ui(registry) 函數
└── *.py                 ← widget 模組
```

**3 類 UI 擴展點**：

| 擴展點 | 註冊方法 | 用途 |
|--------|---------|------|
| 浮動卡片 | `registry.register_floating_card(...)` | 獨立卡片 widget + 自動註冊 `/<card_id>` 命令 |
| 內容塊渲染器 | `registry.register_content_renderer(...)` | 在消息流中渲染自定義 HTML 內容塊 |
| 消息元素工廠 | `registry.register_message_factory(...)` | 接管特定消息結構，返回自定義 QWidget |

**參考**：
- [docs/architecture.md §ui 組件](https://github.com/martin98-afk/drifox-plugins/blob/main/docs/architecture.md)
- [plugins/context-usage-stats/](https://github.com/martin98-afk/drifox-plugins/tree/main/plugins/context-usage-stats)（浮動卡片真實案例）
- [plugins/plugin-marketplace/](https://github.com/martin98-afk/drifox-plugins/tree/main/plugins/plugin-marketplace)（完整 UI 生態入口）
- [plugins/plugin-manager/](https://github.com/martin98-afk/drifox-plugins/tree/main/plugins/plugin-manager)（啟/禁/卸 UI）

---

## 6. 測試與驗證

### 6.1 本地快速測試

```bash
# DriFox watchfiles 熱更新
# 修改插件文件後等待 1-3 秒自動生效

# 用 /plugin-manager 查看插件加載狀態
# 用 /theme 測試主題切換
```

### 6.2 完整驗證（提 PR 前必做）

當你的插件在 `~/.drifox/plugins/<name>/` 下開發完成後，要發布到官方市場前需跑完整驗證：

```bash
# 第一步：clone 官方市場倉庫
git clone https://github.com/martin98-afk/drifox-plugins.git /tmp/dfp

# 第二步：把你的插件複製到倉庫中
cp -r ~/.drifox/plugins/<name> /tmp/dfp/plugins/<name>

# 第三步：在倉庫中跑驗證
cd /tmp/dfp
python tools/validate_plugins.py
python tools/generate_marketplace.py

# 第四步：確認全部 OK 後，清掉暫存
# （正式提交 PR 的流程見 §7）
rm -rf /tmp/dfp
```

### 6.3 驗證清單

- [ ] `plugin.json` 能通過 `schemas/plugin.schema.json` 校驗
- [ ] `name` 與目錄名一致，小寫 kebab-case
- [ ] `components` 中每個 `true` 的 flag 都有對應目錄與文件
- [ ] 每個 `commands/*.md` 有完整 frontmatter（description + type）
- [ ] 每個 `skills/*/SKILL.md` 有 frontmatter（name + description）
- [ ] hooks 的 Python 文件能 `python -m py_compile` 通過
- [ ] 已跑過 `validate_plugins.py` 全部 OK
- [ ] 已跑過 `generate_marketplace.py` 更新 marketplace.json

---

## 7. 發布到市場 — 任何人都可以發布自己的插件！

插件在 `~/.drifox/plugins/<name>/` 下開發完成後，可以提交到官方市場讓所有 DriFox 用戶安裝使用。

### 7.1 工作流

```
① Fork 官方市場倉庫 → https://github.com/martin98-afk/drifox-plugins（點右上角 Fork）
② Clone 你的 fork → git clone https://github.com/<你的帳號>/drifox-plugins.git
③ 把你的插件複製到倉庫中 → cp -r ~/.drifox/plugins/<name> plugins/<name>
④ 跑驗證 → python tools/validate_plugins.py + generate_marketplace.py
⑤ Commit & Push 到你的 fork
⑥ 在 GitHub 上提交 PR（你的 fork → martin98-afk/drifox-plugins main）
⑦ CI 自動校驗，通過後 maintainer 合併 → 你的插件上架 🎉
```

### 7.2 完整提交流程

```bash
# 1. 先在 GitHub 上 Fork 官方市場倉庫
#    網址：https://github.com/martin98-afk/drifox-plugins → 點右上角 Fork

# 2. clone 你的 fork
git clone https://github.com/<你的GitHub帳號>/drifox-plugins.git /tmp/dfp
cd /tmp/dfp

# 3. 把官方倉庫設為上游（便於同步）
git remote add upstream https://github.com/martin98-afk/drifox-plugins.git

# 4. 建立特性分支
git checkout -b feat/<plugin-name>

# 5. 把你的插件從本地開發目錄複製進來
cp -r ~/.drifox/plugins/<plugin-name> plugins/<plugin-name>

# 6. 跑驗證
python tools/validate_plugins.py
python tools/generate_marketplace.py

# 7. commit 並推送
git add plugins/<plugin-name>/ marketplace.json
git commit -m "feat(<plugin-name>): 添加 xx 插件"
git push origin feat/<plugin-name>

# 8. 到 GitHub 上創建 Pull Request
#    你的 fork → martin98-afk/drifox-plugins main
#    連結：https://github.com/martin98-afk/drifox-plugins/pulls
```

### 7.3 PR 合併後

- marketplace.json 自動更新
- 你的插件名稱出現在官方市場中
- 所有 DriFox 用戶可透過 plugin-marketplace UI 瀏覽和安裝你的插件 🎉

### 7.4 市場清單

marketplace.json 中每條記錄的結構由 `tools/generate_marketplace.py` 自動從 `plugin.json` 生成，無需手動編輯。

**參考**：
- `references/publishing.md` — 完整發布流程
- [CONTRIBUTING.md](https://github.com/martin98-afk/drifox-plugins/blob/main/CONTRIBUTING.md) — 貢獻指南
- [GitHub 倉庫](https://github.com/martin98-afk/drifox-plugins) — 官方插件市場

---

## 8. 常見陷阱

### 🚫 Manifest 命名不一致
插件目錄名與 `plugin.json` 的 `name` 字段必須一致。
```json
// ❌ 目錄是 my-cool-plugin，name 是 my-cool-plugin-v2
// ✅ 目錄是 my-cool-plugin，name 是 my-cool-plugin
```

### 🚫 Components flag 開了但沒文件
```json
// ❌ "components": { "commands": true } 但沒有 commands/ 目錄
// ✅ 每個 true 的 flag 必須有對應目錄或文件
```

### 🚫 UI 插件走了本技能
UI 插件開發請調用 `ui-plugin-creator` 技能。

### 🚫 修改了 system 插件
`plugins/system/` 下的內容不要手動修改——它們是 DriFox 內置的。

### 🚫 跳過驗證直接提 PR
提 PR 前一定要跑 `validate_plugins.py`，否則 CI 會失敗。

### 🚫 version 忘記更新
每次修改後記得更新 `plugin.json` 的 `version` 字段。

---

## 9. references/ 索引

> 以下文件按需加載，不要一次性全讀。

| 文件 | 何時讀 | 內容 |
|------|-------|------|
| `references/components.md` | 開發各類組件時 | 8 類組件的詳細開發指南 + 代碼模板 |
| `references/manifest.md` | 新建/修改 plugin.json 時 | manifest 字段定義、校驗規則、完整示例 |
| `references/workflow.md` | 需要完整開發流程時 | 從需求→scaffold→開發→測試→發布的完整指引 |
| `references/testing.md` | 驗證/除錯時 | validate_plugins.py 用法、熱更新測試、除錯技巧 |
| `references/publishing.md` | 準備發布到市場時 | PR 流程、CI 說明、版本策略 |
| `references/troubleshooting.md` | 遇到報錯/不工作時 | 常見問題與解決方案 |

---

## 附：與其他技能的銜接

```
plugin-creator（本技能）
├─ 🟡 需求不明確 → brainstorming
├─ 🟡 UI 插件     → ui-plugin-creator
├─ 🟡 編碼規範     → drifox-dev/references/conventions.md
├─ 🟡 修 Bug      → diagnose
└─ 🟡 複雜任務     → subagent-driven-development
```
