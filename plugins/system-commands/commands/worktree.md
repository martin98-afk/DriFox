---
description: 设置隔离工作区
type: prompt
argument-hint:
  "[--name=]": "指定工作树分支名"
  "[<功能描述>]": "要实现的功能描述"
---

## 参数说明

`$ARGUMENTS` 是功能描述。

- **`--name=<分支名>`**：指定工作树分支名（可选，自动生成）
- **`$ARGUMENTS`** 其他文本作为功能描述

## 核心原则

**检测现有隔离优先。然后使用原生工具。然后回退到 git。永远不要对抗 harness。**

## Step 0: 检测现有隔离

**在创建任何内容前，先检查是否已在隔离工作区。**

```bash
GIT_DIR=$(cd "$(git rev-parse --git-dir)" 2>/dev/null && pwd -P)
GIT_COMMON=$(cd "$(git rev-parse --git-common-dir)" 2>/dev/null && pwd -P)
BRANCH=$(git branch --show-current)
```

**子模块防护：** `GIT_DIR != GIT_COMMON` 在 git 子模块内部也为真。在得出"已在 worktree"的结论前，验证不是子模块：

```bash
# 如果返回路径，则在子模块中，不是 worktree — 当作普通仓库处理
git rev-parse --show-superproject-working-tree 2>/dev/null
```

**如果 `GIT_DIR != GIT_COMMON`（且不是子模块）：** 已在链接 worktree。跳到 Step 3（项目设置）。不要创建另一个 worktree。

报告分支状态：
- 在分支上："已在 `<路径>` 的隔离工作区，分支 `<名称>`。"
- 分离 HEAD："已在 `<路径>` 的隔离工作区（分离 HEAD，外部管理）。完成时需要创建分支。"

**如果 `GIT_DIR == GIT_COMMON`（或在子模块中）：** 在普通仓库检出。

询问用户是否需要隔离工作区：

> "需要我设置隔离 worktree 吗？它保护当前分支不受变更影响。"

如果有明确的 worktree 偏好，使用它而不询问。如果用户拒绝，在当前位置工作并跳到 Step 3。

## Step 1: 创建隔离工作区

**有两种机制。按此顺序尝试。**

### 1a. 原生 Worktree 工具（优先）

用户请求隔离工作区（Step 0 同意）。是否已有创建 worktree 的方式？它可能是名为 `EnterWorktree`、`WorktreeCreate`、`/worktree` 命令或 `--worktree` 标志的工具。如果有，使用它并跳到 Step 3。

原生工具自动处理目录放置、分支创建和清理。当有原生工具时使用 `git worktree add` 会创建 harness 无法看到或管理的幻影状态。

只有当 1a 不适用时（没有原生 worktree 工具可用）才继续到 Step 1b。

### 1b. Git Worktree 回退

**仅当 Step 1a 不适用时使用** — 没有原生 worktree 工具可用。手动使用 git 创建 worktree。

#### 目录选择

遵循此优先级顺序。明确的用户偏好优先于观察到的文件系统状态。

1. **检查指令文件中声明的 worktree 目录偏好。** 如果用户已指定，使用它而不询问。

2. **检查现有项目本地 worktree 目录：**
   ```bash
   ls -d .worktrees 2>/dev/null     # 优先（隐藏）
   ls -d worktrees 2>/dev/null      # 替代
   ```
   如果存在，使用它。如果两者都存在，`.worktrees` 优先。

3. **检查现有全局目录：**
   ```bash
   project=$(basename "$(git rev-parse --show-toplevel)")
   ls -d ~/.config/superpowers/worktrees/$project 2>/dev/null
   ```
   如果存在，使用它（与遗留全局路径向后兼容）。

4. **如果没有其他指导**，默认为项目根目录的 `.worktrees/`。

#### 安全验证（仅项目本地目录）

**创建 worktree 前必须验证目录被忽略：**

```bash
git check-ignore -q .worktrees 2>/dev/null || git check-ignore -q worktrees 2>/dev/null
```

**如果没有被忽略：** 添加到 .gitignore，提交变更，然后继续。

**为什么关键：** 防止意外将 worktree 内容跟踪到仓库。

全局目录（`~/.config/superpowers/worktrees/`）不需要验证。

#### 创建 Worktree

```bash
project=$(basename "$(git rev-parse --show-toplevel)")

# 根据选择的位置确定路径
# 对于项目本地：path="$LOCATION/$BRANCH_NAME"
# 对于全局：path="~/.config/superpowers/worktrees/$project/$BRANCH_NAME"

git worktree add "$path" -b "$BRANCH_NAME"
cd "$path"
```

**沙箱回退：** 如果 `git worktree add` 因权限错误失败（沙箱拒绝），告诉用户沙箱阻止了 worktree 创建，改为在当前目录工作。然后在原地运行设置和基线测试。

## Step 3: 项目设置

自动检测并运行适当的设置：

```bash
# Node.js
if [ -f package.json ]; then npm install; fi

# Rust
if [ -f Cargo.toml ]; then cargo build; fi

# Python
if [ -f requirements.txt ]; then pip install -r requirements.txt; fi
if [ -f pyproject.toml ]; then poetry install; fi

# Go
if [ -f go.mod ]; then go mod download; fi
```

## Step 4: 验证干净基线

运行测试以确保工作区启动干净：

```bash
# 使用项目适当的命令
npm test / cargo test / pytest / go test ./...
```

**如果测试失败：** 报告失败，询问是否继续或调查。

**如果测试通过：** 报告就绪。

### 报告

```
Worktree 就绪于 <完整路径>
测试通过（<N> 个测试，0 失败）
准备好实现 <功能名称>
```

## 快速参考

| 情况 | 操作 |
|------|------|
| 已在链接 worktree | 跳过创建（Step 0） |
| 在子模块中 | 当作普通仓库处理（Step 0 防护） |
| 有原生 worktree 工具 | 使用它（Step 1a） |
| 无原生工具 | Git worktree 回退（Step 1b） |
| `.worktrees/` 存在 | 使用它（验证被忽略） |
| `worktrees/` 存在 | 使用它（验证被忽略） |
| 两者都存在 | 使用 `.worktrees/` |
| 都不存在 | 检查指令文件，然后默认 `.worktrees/` |
| 全局路径存在 | 使用它（向后兼容） |
| 目录未被忽略 | 添加到 .gitignore + 提交 |
| 创建时权限错误 | 沙箱回退，在原地工作 |
| 基线测试期间失败 | 报告失败 + 提问 |
| 无 package.json/Cargo.toml | 跳过依赖安装 |

## 禁止事项

**永远不要：**
- 在 Step 0 检测到现有隔离时创建 worktree
- 当有原生 worktree 工具时使用 `git worktree add`
- 跳过 Step 1a 直接跳到 Step 1b 的 git 命令
- 创建 worktree 前不验证被忽略（项目本地）
- 跳过基线测试验证
- 在未询问的情况下继续进行失败的测试

**始终：**
- 首先运行 Step 0 检测
- 优先使用原生工具而非 git 回退
- 遵循目录优先级：现有 > 全局遗留 > 指令文件 > 默认
- 验证项目本地的目录被忽略
- 自动检测并运行项目设置
- 验证干净的测试基线