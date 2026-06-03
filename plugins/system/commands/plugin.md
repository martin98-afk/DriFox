---
description: 管理插件（安装、列出、卸载、启用/禁用、添加市场）
type: prompt
argument-hint:
  "[--list]": "列出所有已安装的插件（含系统插件和已禁用的）"
  "[--install=]": "从市场安装插件，如 --install=code-review@official"
  "[--uninstall=]": "卸载已安装的插件"
  "[--enable=]": "启用已禁用的插件"
  "[--disable=]": "禁用已启用的插件"
  "[--marketplace=]": "添加第三方插件市场，如 --marketplace=owner/repo"
---

# /plugin 命令 — 插件管理器

你正在处理 `/plugin` 命令。用户输入的参数（`$ARGUMENTS`）是 `/plugin` 后的所有文本。

## 📋 执行规则

1. **解析 `--xxx=value` 参数**，从中提取子命令和参数值。
2. **每做一个操作，必须给用户明确的反馈**。
3. **使用 `todo_write` 跟踪多步骤任务**（如安装流程）。
4. **如果 `git` 不可用**，提示并给出替代方案。

## 子命令与参数映射

| 用户输入 | 子命令 | 参数值 |
|----------|--------|--------|
| `--list` | list | 无 |
| `--install=xxx@yyy` | install | `xxx@yyy`（插件名@市场名） |
| `--uninstall=xxx` | uninstall | `xxx`（插件名） |
| `--enable=xxx` | enable | `xxx`（插件名） |
| `--disable=xxx` | disable | `xxx`（插件名） |
| `--marketplace=owner/repo` | marketplace add | `owner/repo` |

如果用户输入多个参数（如 `--install=a --install=b`），分别处理。
如果用户只输入了 `--install=` 但没有值，提示用户输入插件名。
如果用户没有输入任何参数，列出所有子命令的使用说明。

## 子命令详情

### 1. `--list`
列出所有已安装的插件：

1. 用 `glob` 搜索插件清单文件：
   - `plugins/system/**/.drifox-plugin/plugin.json` — 系统插件（始终启用）
   - `.drifox/plugins/**/.drifox-plugin/plugin.json` — 用户已启用的插件（DriFox 格式）
   - `.drifox/plugins/**/.claude-plugin/plugin.json` — 用户已启用的插件（Claude 格式）
   - `.drifox/plugins-disabled/**/.drifox-plugin/plugin.json` — 已禁用的用户插件
   - `.drifox/plugins-disabled/**/.claude-plugin/plugin.json` — 已禁用的用户插件
2. 对每个插件，读取其 `plugin.json` 获取 `name` 和 `description`
3. 输出表格：

| 插件名 | 描述 | 类型 | 状态 |
|--------|------|------|------|
| system | DriFox 系统内置插件 | 系统 | ✅ 启用 |
| code-review | Automated code review... | 用户 | ✅ 启用 |
| my-plugin | ... | 用户 | ⛔ 已禁用 |

### 2. `--install=<name>[@marketplace]`
从插件市场安装插件：

**① 确定市场来源**
- 如果指定了 `@marketplace`（如 `--install=typescript-lsp@official`），使用该市场
- 如果未指定，默认使用 **official** 市场
- 市场列表存储在 `.drifox/app.config` 的 `Plugin.Marketplaces` 字段（可能不存在）

**② 获取市场清单**
- 官方市场（`official`）：`https://raw.githubusercontent.com/anthropics/claude-plugins-official/main/.claude-plugin/marketplace.json`
- 第三方市场（`owner/repo`）：`https://raw.githubusercontent.com/{owner}/{repo}/main/.claude-plugin/marketplace.json`
  - 如果 404，尝试根目录：`https://raw.githubusercontent.com/{owner}/{repo}/main/marketplace.json`
- 使用 `webfetch` 工具下载

**③ 查找插件**
- 在 `marketplace.json` 的 `plugins` 数组中匹配 `name` 字段（精确匹配）
- 找到后提取该插件的元数据和 `source` 字段
- **没找到**：列出该市场前 20 个可用插件名，让用户选择

**④ 下载插件**
根据 `source.source` 类型操作：

**类型 `git-subdir`**（最常见）：
```bash
set TEMP_DIR=%TEMP%\drifox_plugin_%RANDOM%
mkdir %TEMP_DIR%
git clone --depth=1 --filter=blob:none --no-checkout %source.url% %TEMP_DIR%\repo
cd %TEMP_DIR%\repo
git sparse-checkout set %source.path%
git checkout %source.ref% 2>nul || git checkout main 2>nul || git checkout master
xcopy /E /I /Y %TEMP_DIR%\repo\%source.path%\* .drifox\plugins\%name%\
rd /S /Q %TEMP_DIR%
```

**类型 `url`**：
```bash
set TEMP_DIR=%TEMP%\drifox_plugin_%RANDOM%
git clone --depth=1 %source.url% %TEMP_DIR%
if "%source.path%"=="" (
  xcopy /E /I /Y %TEMP_DIR%\* .drifox\plugins\%name%\
) else (
  xcopy /E /I /Y %TEMP_DIR%\%source.path%\* .drifox\plugins\%name%\
)
rd /S /Q %TEMP_DIR%
```

**类型 `./local-path`**（市场仓库内的本地路径）：
- 用 `webfetch` 查看该路径下的目录结构
- 从 GitHub raw 文件下载每个文件

**回退方案**（如果 git 不可用）：
1. 用 GitHub API 获取目录树：`https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}`
2. 递归下载每个文件到 `.drifox/plugins/<name>/`
3. 每个文件的 raw URL：`https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}`

**⑤ 确保插件能被识别**
- 检查 `.drifox/plugins/<name>/` 下是否有 `.drifox-plugin/plugin.json` 或 `.claude-plugin/plugin.json`
- 如果下载的插件结构良好（包含 manifests），无需额外操作
- 如果缺少 manifests，用 `write` 创建 `.drifox-plugin/plugin.json`：
  ```json
  {
    "name": "<插件名>",
    "description": "<从 marketplace 获取的描述>",
    "version": "1.0.0",
    "type": "user"
  }
  ```

**⑥ 通知用户**
安装完成后，**DriFox 的 watchfiles 热更新系统会自动检测并加载新插件**（约 1-3 秒）。
告知用户：
- 安装成功 ✓
- 插件已自动启用
- 可立即使用该插件的命令/功能
- 如需禁用，使用 `/plugin --disable=<name>`

### 3. `--uninstall=<name>`
卸载并删除插件：

1. **安全检查**：检查 `plugins/system/<name>/` 是否存在 → 如果存在则拒绝卸载（系统插件），提示用 `--disable=` 替代
2. **删除目录**：
   ```bash
   rd /S /Q .drifox\plugins\<name>\
   ```
3. 告知用户卸载完成（watchfiles 会自动移除运行时注册）

### 4. `--enable=<name>`
将已禁用的插件移回启用目录，立即生效。

**原理**：`.drifox/plugins-disabled/<name>/` → 移动到 → `.drifox/plugins/<name>/`
watchfiles 检测到新增目录 → 自动注册 + 启用。

1. 检查 `.drifox/plugins-disabled/<name>/` 是否存在（用 `glob`）
2. 如果存在，移动目录：
   ```bash
   move .drifox\plugins-disabled\<name> .drifox\plugins\<name>
   ```
3. 告知用户：插件已移回启用目录，watchfiles 热更新将在 1-3 秒内自动加载

### 5. `--disable=<name>`
将插件移出启用目录，使其被 DriFox 卸载，立即生效。

**原理**：`.drifox/plugins/<name>/` → 移动到 → `.drifox/plugins-disabled/<name>/`
watchfiles 检测到目录删除 → 自动从运行时移除。

1. **安全检查**：`plugins/system/<name>/` 是否存在？→ 系统插件不可禁用（目录只读）
2. 确保 `.drifox/plugins-disabled/` 目录存在（不存在则创建）
3. 检查 `.drifox/plugins/<name>/` 是否存在
4. 移动目录：
   ```bash
   mkdir .drifox\plugins-disabled 2>nul || ver>nul
   move .drifox\plugins\<name> .drifox\plugins-disabled\<name>
   ```
5. 告知用户：插件已移出，watchfiles 热更新将在 1-3 秒内自动卸载
6. 如需恢复，使用 `/plugin --enable=<name>`

### 6. `--marketplace=<owner/repo>`
注册一个新的插件市场：

1. 格式校验：参数应为 `owner/repo` 格式（GitHub 仓库）
2. 用 `webfetch` 测试：`https://raw.githubusercontent.com/{owner}/{repo}/main/.claude-plugin/marketplace.json`
   - 如果 404，尝试根目录：`https://raw.githubusercontent.com/{owner}/{repo}/main/marketplace.json`
3. 验证返回内容：JSON 必须有 `name` 和 `plugins` 字段
4. 读取 `.drifox/app.config`，在 `Plugin.Marketplaces` 中添加：
   ```json
   { "...existing...", "<名称>": "<owner/repo>" }
   ```
5. 写回文件
6. 列出该市场中前 10 个可安装的插件
7. 提示用户可用 `/plugin --install=<name>@<市场名>` 安装

## 📁 目录结构参考

```
DriFox/
├── plugins/system/              ← 系统插件（不可卸载/修改）
│   └── commands/plugin.md       ← 你正在阅读的这个文件
│
.drifox/
├── plugins/                     ← 用户插件（启用状态，watchfiles 自动加载）
│   ├── user-custom/
│   ├── code-review/
│   └── <新安装的插件>/
│
└── plugins-disabled/            ← 已禁用的插件（--disable 移入，--enable 移出）
    └── <被禁用的插件>/
```

## 🎯 格式兼容性

DriFox **原生支持** `.claude-plugin/plugin.json` 格式。市面上 270+ Claude 插件无需任何格式转换，直接可用。

## ⚠️ 安全与边界

- ❌ **不要卸载系统插件**（`plugins/system/` 下的）
- ❌ **不要修改 `plugins/system/` 目录内容**
- ❌ **不要修改 `app.config` 中其他功能区的字段**
- ✅ **安装前向用户展示插件信息确认**
- ✅ **`git clone` 失败时尝试回退方案或报错**

## 🌐 内置市场

| 名称 | GitHub 仓库 | 说明 |
|------|-------------|------|
| `official` | `anthropics/claude-plugins-official` | Anthropic 官方市场，270+ 插件，29.2k stars |
| `classmethod-marketplace` | `classmethod/claude-code-marketplace` | 第三方社区市场示例 |
