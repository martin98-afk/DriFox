---
description: 一站式安装 LSP 服务依赖并自动创建对应插件
type: prompt
argument-hint:
  "--list": "列出所有支持的语言及其 LSP 信息"
  "--language=<lang>": "安装指定语言的 LSP（如 --language=python）"
  "--all": "批量安装所有支持的 LSP 服务"
mutex_groups:
  mode: ["--list", "--all", "--language="]
parameters:
  - name: "--list"
    description: "列出所有支持的语言及其 LSP 安装信息"
    param_type: flag
    mutex: mode
  - name: "--language="
    description: "安装指定语言的 LSP（可多次使用安装多个）"
    param_type: value
    mutex: mode
    value_options:
      - python
      - typescript
      - rust
      - go
      - cpp
      - lua
      - json
      - html
      - css
      - yaml
      - bash
      - markdown
      - vue
      - svelte
      - zig
      - dart
      - toml
      - docker
  - name: "--all"
    description: "批量安装所有支持的 LSP 服务（可能耗时较长）"
    param_type: flag
    mutex: mode
prompt_sections:
  --list: "list"
  --all: "all"
  --language=:
    python: "lang-python"
    typescript: "lang-typescript"
    rust: "lang-rust"
    go: "lang-go"
    cpp: "lang-cpp"
    lua: "lang-lua"
    json: "lang-json"
    html: "lang-html"
    css: "lang-css"
    yaml: "lang-yaml"
    bash: "lang-bash"
    markdown: "lang-markdown"
    vue: "lang-vue"
    svelte: "lang-svelte"
    zig: "lang-zig"
    dart: "lang-dart"
    toml: "lang-toml"
    docker: "lang-docker"
---

# /lsp-install 命令 — LSP 一站式安装器

你正在处理 `/lsp-install` 命令。用户输入 `$ARGUMENTS` 是 `/lsp-install` 后的所有文本。

## 📋 核心职责

1. **解析参数**，确定要安装的目标语言
2. **检测并安装** LSP 服务器所需的二进制依赖
3. **创建插件目录**（`～/.drifox/plugins/lsp-<lang>/`），包含 `plugin.json`、`.lsp.json`、`README.md`
4. **通知用户**安装结果，watchfiles 热重载将在 1-3 秒内自动加载新插件

## ⚠️ 铁律

- **所有 `write` 操作 → 立即执行 `lsp(path="<文件路径>", operation="diagnostics")` 验证文件被正确写入**
- **每安装完一个语言后，立即验证 LSP 二进制在 PATH 中可用**：`bash("which <command>")` 或 `bash("<command> --version")`
- **如果安装失败，明确告知用户失败原因和替代方案**
- **使用 `todowrite` 跟踪多步骤任务**
- **不要修改已有插件文件，只创建新的**

## 📁 插件目录结构规范

每个 LSP 插件创建在 `～/.drifox/plugins/lsp-<lang>/` 下，结构如下：

```
～/.drifox/plugins/lsp-<lang>/
├── .drifox-plugin/
│   └── plugin.json          # 插件清单
├── .lsp.json                # LSP 服务器配置
└── README.md                # 插件说明文档
```

### plugin.json 模板

```json
{
  "name": "lsp-<lang>",
  "description": "LSP support for <Language> — auto-installed by /lsp-install",
  "version": "1.0.0",
  "author": {
    "name": "DriFox LSP Installer"
  },
  "homepage": "https://github.com/martin98-afk/DriFox",
  "license": "MIT",
  "type": "user",
  "components": {
    "lsp": true
  }
}
```

### .lsp.json 模板

```json
{
  "<server-name>": {
    "command": "<binary-name>",
    "args": ["<arg1>", "<arg2>"],
    "extensionToLanguage": {
      ".ext1": "langId1",
      ".ext2": "langId2"
    },
    "initializationOptions": {},
    "settings": {},
    "startupTimeout": 10000,
    "maxRestarts": 3,
    "transport": "stdio",
    "installHint": "<install-command>"
  }
}
```

## 🛠 安装流程（通用步骤）

对每个要安装的语言，按以下流程执行：

### 步骤 1：检测环境
```bash
# 检测操作系统
uname -s  # Darwin=macOS, Linux=Linux, MINGW*/MSYS*=Windows
# 检测已有的包管理器
which brew npm pip pip3 cargo go rustup 2>/dev/null
```

### 步骤 2：安装 LSP 二进制
根据下方 **LSP 语言参考表** 中的 `install_cmd` 执行安装。
- **macOS 优先 brew**，其次 npm/pip
- **Linux 优先 apt**，其次 npm/pip/cargo
- **Windows 优先 winget/choco**，其次 npm
- **跨平台回退**：始终可用 npm 或 pip

### 步骤 3：验证安装
```bash
which <binary-name> && <binary-name> --version 2>&1 || echo "NOT_FOUND"
```

### 步骤 4：创建插件文件
1. 创建目录：`mkdir -p ～/.drifox/plugins/lsp-<lang>/.drifox-plugin`
2. 按模板写入 `plugin.json`、`.lsp.json`、`README.md`
3. 验证写入：对每个文件执行 `lsp(path="～/.drifox/plugins/lsp-<lang>/.lsp.json", operation="diagnostics")`

### 步骤 5：通知用户
安装完成告知：
- ✅ 安装成功
- 📂 插件路径：`～/.drifox/plugins/lsp-<lang>/`
- 🔄 watchfiles 将在 1-3 秒内自动加载
- 🧪 可立即测试：编辑对应语言文件，LSP 诊断自动生效

---

## 📚 LSP 语言参考表

以下是所有支持的编程语言及其 LSP 配置。**body 中的表格是唯一数据源。**

<!-- section:lang-python -->
### Python
| 字段 | 值 |
|------|-----|
| **插件名** | `lsp-python` |
| **LSP 服务器** | `pyright` |
| **二进制名** | `pyright-langserver` |
| **安装命令 (macOS)** | `brew install pyright` |
| **安装命令 (Linux)** | `pip install pyright` 或 `uv pip install pyright` |
| **安装命令 (Windows)** | `npm install -g pyright` |
| **安装命令 (跨平台)** | `pip install pyright` |
| **文件扩展名** | `.py`, `.pyi` |
| **语言 ID** | `python` |
| **设置 (settings)** | `{"python.analysis.typeCheckingMode": "basic", "python.analysis.autoSearchPaths": true, "python.analysis.useLibraryCodeForTypes": true}` |
| **初始化选项** | `{}` |
| **启动参数** | `["--stdio"]` |
<!-- end -->
<!-- section:lang-typescript -->
### TypeScript / JavaScript
| 字段 | 值 |
|------|-----|
| **插件名** | `lsp-typescript` |
| **LSP 服务器** | `typescript-language-server` |
| **二进制名** | `typescript-language-server` |
| **安装命令 (跨平台)** | `npm install -g typescript-language-server typescript` |
| **文件扩展名** | `.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.cjs`, `.mts`, `.cts` |
| **语言 ID** | `typescript` (`.ts`, `.tsx`, `.mts`, `.cts`), `javascript` (`.js`, `.jsx`, `.mjs`, `.cjs`) |
| **设置 (settings)** | `{}` |
| **初始化选项** | `{}` |
| **启动参数** | `["--stdio"]` |
<!-- end -->
<!-- section:lang-rust -->
### Rust
| 字段 | 值 |
|------|-----|
| **插件名** | `lsp-rust` |
| **LSP 服务器** | `rust-analyzer` |
| **二进制名** | `rust-analyzer` |
| **安装命令 (跨平台)** | `rustup component add rust-analyzer` |
| **安装命令 (macOS 备选)** | `brew install rust-analyzer` |
| **文件扩展名** | `.rs` |
| **语言 ID** | `rust` |
| **设置 (settings)** | `{"rust-analyzer.checkOnSave.command": "clippy"}` |
| **初始化选项** | `{}` |
| **启动参数** | `[]` |
<!-- end -->
<!-- section:lang-go -->
### Go
| 字段 | 值 |
|------|-----|
| **插件名** | `lsp-go` |
| **LSP 服务器** | `gopls` |
| **二进制名** | `gopls` |
| **安装命令 (跨平台)** | `go install golang.org/x/tools/gopls@latest` |
| **安装命令 (macOS 备选)** | `brew install gopls` |
| **文件扩展名** | `.go` |
| **语言 ID** | `go` |
| **设置 (settings)** | `{"gopls.staticcheck": true}` |
| **初始化选项** | `{}` |
| **启动参数** | `[]` |
<!-- end -->
<!-- section:lang-cpp -->
### C / C++
| 字段 | 值 |
|------|-----|
| **插件名** | `lsp-cpp` |
| **LSP 服务器** | `clangd` |
| **二进制名** | `clangd` |
| **安装命令 (macOS)** | `brew install llvm`（安装后 clangd 在 `/usr/local/opt/llvm/bin/`） |
| **安装命令 (Linux)** | `apt install clangd-12` 或 `apt install clang-tools` |
| **安装命令 (Windows)** | `winget install LLVM.LLVM` |
| **文件扩展名** | `.c`, `.cpp`, `.cc`, `.cxx`, `.h`, `.hpp`, `.hxx`, `.m`, `.mm` |
| **语言 ID** | `c` (`.c`, `.h`), `cpp` (`.cpp`, `.cc`, `.cxx`, `.hpp`, `.hxx`, `.m`, `.mm`) |
| **设置 (settings)** | `{}` |
| **初始化选项** | `{}` |
| **启动参数** | `[]` |
<!-- end -->
<!-- section:lang-lua -->
### Lua
| 字段 | 值 |
|------|-----|
| **插件名** | `lsp-lua` |
| **LSP 服务器** | `lua-language-server` |
| **二进制名** | `lua-language-server` |
| **安装命令 (macOS)** | `brew install lua-language-server` |
| **安装命令 (Linux)** | `npm install -g lua-language-server` |
| **安装命令 (跨平台)** | `npm install -g lua-language-server` |
| **文件扩展名** | `.lua` |
| **语言 ID** | `lua` |
| **设置 (settings)** | `{}` |
| **初始化选项** | `{}` |
| **启动参数** | `[]` |
<!-- end -->
<!-- section:lang-json -->
### JSON
| 字段 | 值 |
|------|-----|
| **插件名** | `lsp-json` |
| **LSP 服务器** | `vscode-json-language-server` |
| **二进制名** | `vscode-json-languageserver` |
| **安装命令 (跨平台)** | `npm install -g vscode-langservers-extracted` |
| **文件扩展名** | `.json`, `.jsonc`, `.json5` |
| **语言 ID** | `json` (`.json`), `jsonc` (`.jsonc`, `.json5`) |
| **设置 (settings)** | `{"json.schemas": [], "json.format.enable": true}` |
| **初始化选项** | `{"provideFormatter": true}` |
| **启动参数** | `["--stdio"]` |
<!-- end -->
<!-- section:lang-html -->
### HTML
| 字段 | 值 |
|------|-----|
| **插件名** | `lsp-html` |
| **LSP 服务器** | `vscode-html-language-server` |
| **二进制名** | `vscode-html-languageserver` |
| **安装命令 (跨平台)** | `npm install -g vscode-langservers-extracted` |
| **文件扩展名** | `.html`, `.htm` |
| **语言 ID** | `html` |
| **设置 (settings)** | `{}` |
| **初始化选项** | `{"provideFormatter": true}` |
| **启动参数** | `["--stdio"]` |
<!-- end -->
<!-- section:lang-css -->
### CSS
| 字段 | 值 |
|------|-----|
| **插件名** | `lsp-css` |
| **LSP 服务器** | `vscode-css-language-server` |
| **二进制名** | `vscode-css-languageserver` |
| **安装命令 (跨平台)** | `npm install -g vscode-langservers-extracted` |
| **文件扩展名** | `.css`, `.scss`, `.less` |
| **语言 ID** | `css` (`.css`), `scss` (`.scss`), `less` (`.less`) |
| **设置 (settings)** | `{}` |
| **初始化选项** | `{"provideFormatter": true}` |
| **启动参数** | `["--stdio"]` |
<!-- end -->
<!-- section:lang-yaml -->
### YAML
| 字段 | 值 |
|------|-----|
| **插件名** | `lsp-yaml` |
| **LSP 服务器** | `yaml-language-server` |
| **二进制名** | `yaml-language-server` |
| **安装命令 (跨平台)** | `npm install -g yaml-language-server` |
| **文件扩展名** | `.yaml`, `.yml` |
| **语言 ID** | `yaml` |
| **设置 (settings)** | `{"yaml.schemas": {}, "yaml.format.enable": true}` |
| **初始化选项** | `{}` |
| **启动参数** | `["--stdio"]` |

### Bash / Shell
| 字段 | 值 |
|------|-----|
| **插件名** | `lsp-bash` |
| **LSP 服务器** | `bash-language-server` |
| **二进制名** | `bash-language-server` |
| **安装命令 (跨平台)** | `npm install -g bash-language-server` |
| **文件扩展名** | `.sh`, `.bash`, `.zsh` |
| **语言 ID** | `bash` (`.sh`, `.bash`), `zsh` (`.zsh`) |
| **设置 (settings)** | `{}` |
| **初始化选项** | `{}` |
| **启动参数** | `["start"]` |
<!-- end -->
<!-- section:lang-markdown -->
### Markdown
| 字段 | 值 |
|------|-----|
| **插件名** | `lsp-markdown` |
| **LSP 服务器** | `marksman` |
| **二进制名** | `marksman` |
| **安装命令 (macOS)** | `brew install marksman` |
| **安装命令 (Linux)** | `npm install -g marksman` |
| **安装命令 (跨平台)** | `npm install -g marksman` |
| **文件扩展名** | `.md`, `.mdx`, `.markdown` |
| **语言 ID** | `markdown` |
| **设置 (settings)** | `{}` |
| **初始化选项** | `{}` |
| **启动参数** | `["server"]` |
<!-- end -->
<!-- section:lang-vue -->
### Vue
| 字段 | 值 |
|------|-----|
| **插件名** | `lsp-vue` |
| **LSP 服务器** | `vue-language-server` |
| **二进制名** | `vue-language-server` |
| **安装命令 (跨平台)** | `npm install -g @vue/language-server` |
| **文件扩展名** | `.vue` |
| **语言 ID** | `vue` |
| **设置 (settings)** | `{}` |
| **初始化选项** | `{}` |
| **启动参数** | `["--stdio"]` |
<!-- end -->
<!-- section:lang-svelte -->
### Svelte
| 字段 | 值 |
|------|-----|
| **插件名** | `lsp-svelte` |
| **LSP 服务器** | `svelte-language-server` |
| **二进制名** | `svelteserver` |
| **安装命令 (跨平台)** | `npm install -g svelte-language-server` |
| **文件扩展名** | `.svelte` |
| **语言 ID** | `svelte` |
| **设置 (settings)** | `{}` |
| **初始化选项** | `{}` |
| **启动参数** | `["--stdio"]` |
<!-- end -->
<!-- section:lang-zig -->
### Zig
| 字段 | 值 |
|------|-----|
| **插件名** | `lsp-zig` |
| **LSP 服务器** | `zls` |
| **二进制名** | `zls` |
| **安装命令 (macOS)** | `brew install zls` |
| **安装命令 (跨平台)** | `请从 https://github.com/zigtools/zls/releases 下载对应平台的二进制，放入 PATH` |
| **文件扩展名** | `.zig`, `.zon` |
| **语言 ID** | `zig` (`.zig`), `zon` (`.zon`) |
| **设置 (settings)** | `{}` |
| **初始化选项** | `{}` |
| **启动参数** | `[]` |
<!-- end -->
<!-- section:lang-dart -->
### Dart
| 字段 | 值 |
|------|-----|
| **插件名** | `lsp-dart` |
| **LSP 服务器** | `dart-language-server` |
| **二进制名** | `dart` |
| **安装命令 (跨平台)** | `请从 https://dart.dev/get-dart 安装 Dart SDK，LSP 内置在 SDK 中` |
| **文件扩展名** | `.dart` |
| **语言 ID** | `dart` |
| **设置 (settings)** | `{}` |
| **初始化选项** | `{}` |
| **启动参数** | `["language-server"]` |
<!-- end -->
<!-- section:lang-toml -->
### TOML
| 字段 | 值 |
|------|-----|
| **插件名** | `lsp-toml` |
| **LSP 服务器** | `taplo` |
| **二进制名** | `taplo` |
| **安装命令 (macOS)** | `brew install taplo` |
| **安装命令 (跨平台)** | `npm install -g @taplo/cli` |
| **文件扩展名** | `.toml` |
| **语言 ID** | `toml` |
| **设置 (settings)** | `{}` |
| **初始化选项** | `{}` |
| **启动参数** | `["lsp", "stdio"]` |
<!-- end -->
<!-- section:lang-docker -->
### Dockerfile
| 字段 | 值 |
|------|-----|
| **插件名** | `lsp-docker` |
| **LSP 服务器** | `dockerfile-language-server` |
| **二进制名** | `docker-langserver` |
| **安装命令 (跨平台)** | `npm install -g dockerfile-language-server-nodejs` |
| **文件扩展名** | `Dockerfile`, `.dockerfile` |
| **语言 ID** | `dockerfile` |
| **设置 (settings)** | `{}` |
| **初始化选项** | `{}` |
| **启动参数** | `["--stdio"]` |
<!-- end -->

---

## 🔧 多语言同时安装

如果用户指定了多个 `--language=` 参数（如 `--language=python --language=rust --language=go`），**依次执行每个语言的安装流程**，使用 `todowrite` 跟踪进度。

如果 `--all` 被指定，按上述表格中的顺序依次安装所有语言。

## 🚫 已安装检测

在安装前，检查目标插件目录是否已存在：
```bash
test -d ～/.drifox/plugins/lsp-<lang> && echo "EXISTS" || echo "NOT_FOUND"
```

如果 `EXISTS`，**跳过安装**并告知用户：
> ⏭️ lsp-<lang> 已存在，跳��。如需重装请先卸载：`/plugin --uninstall=lsp-<lang>`

## 🧹 安装失败回退

如果 LSP 二进制安装失败：
1. **不要创建插件文件**（避免无效插件）
2. 告知用户失败原因
3. 给出手动安装的替代方案

---

<!-- section:list -->
### `--list`
列出所有支持的编程语言，格式如下：

| 语言 | 插件名 | LSP 服务器 | 安装方式 | 支持的扩展名 |
|------|--------|-----------|----------|-------------|
| Python | lsp-python | pyright | pip/brew/npm | .py, .pyi |
| TypeScript/JS | lsp-typescript | typescript-language-server | npm | .ts, .tsx, .js, .jsx, .mjs, .cjs, .mts, .cts |
| Rust | lsp-rust | rust-analyzer | rustup/brew | .rs |
| Go | lsp-go | gopls | go install/brew | .go |
| C/C++ | lsp-cpp | clangd | brew/apt/winget | .c, .cpp, .cc, .cxx, .h, .hpp, .hxx, .m, .mm |
| Lua | lsp-lua | lua-language-server | brew/npm | .lua |
| JSON | lsp-json | vscode-json-language-server | npm | .json, .jsonc, .json5 |
| HTML | lsp-html | vscode-html-language-server | npm | .html, .htm |
| CSS | lsp-css | vscode-css-language-server | npm | .css, .scss, .less |
| YAML | lsp-yaml | yaml-language-server | npm | .yaml, .yml |
| Bash/Shell | lsp-bash | bash-language-server | npm | .sh, .bash, .zsh |
| Markdown | lsp-markdown | marksman | brew/npm | .md, .mdx, .markdown |
| Vue | lsp-vue | vue-language-server | npm | .vue |
| Svelte | lsp-svelte | svelte-language-server | npm | .svelte |
| Zig | lsp-zig | zls | brew/手动下载 | .zig, .zon |
| Dart | lsp-dart | dart (内置 LSP) | Dart SDK | .dart |
| TOML | lsp-toml | taplo | brew/npm | .toml |
| Dockerfile | lsp-docker | dockerfile-language-server | npm | Dockerfile, .dockerfile |

使用方式：`/lsp-install --language=<语言名>`（如 `--language=python`）

当前已安装的 LSP 插件：
```bash
ls -d ～/.drifox/plugins/lsp-* 2>/dev/null || echo "(尚未安装任何 LSP 插件)"
```
<!-- end -->

<!-- section:all -->
### `--all`
安装所有支持的 LSP 服务。按语言参考表顺序依次安装，使用 `todowrite` 跟踪进度。

⚠️ **注意**：
- 安装全部语言可能需要 5-15 分钟
- 某些 LSP 需要特定的 SDK（如 Dart 需要 Dart SDK、Rust 需要 rustup）
- 遇到安装失败的语言，跳过继续安装下一个
- 最终输出成功/失败统计

流程：
1. 先执行 `--list` 查看当前状态
2. 对表中每种语言，检查插件是否已存在
3. 不存在则按安装流程执行
4. 汇总结果表格
<!-- end -->
