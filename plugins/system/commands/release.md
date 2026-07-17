---
description: 自动发布新版本：生成更新日志、打 tag、推送触发 CI
type: prompt
argument-hint:
  "<version>": "版本号（如 v0.4.2，可选，不传则自动自增一个小版本）"
  "[--dry-run]": "试运行模式：只生成 changelog 不推送"
---

## Release 发布工作流

`$ARGUMENTS` 是用户输入的完整字符串（不含 `/release` 前缀）。

**参数解析**：
- `<version>`：可选，格式 `v0.4.2`；未传则从 `app/utils/config.py` 读取 `current_version` 并自增末位（如 `v0.4.1` → `v0.4.2`）
- `--dry-run`：可选，仅生成 changelog 预览，不提交不推送

---

### 1. 前置检查

```bash
git branch --show-current          # 必须是 dev，否则中止
git status --short                 # 必须为空，否则中止
```

- 如未传版本：自动自增（读取 `app/utils/config.py` 中 `current_version`，去掉 `v` 前缀，按 `.` 分割，末位 +1，重组）
- 版本格式校验：以 `v` 开头 + 语义化版本（如 `v0.4.2`）
- 版本去重：`git tag --list <version>` 必须无输出

---

### 2. 版本号升级

涉及 **4 个文件**，**注意 `v` 前缀差异**：

| 文件 | 查找 | 替换 |
|------|------|------|
| `pyproject.toml` 第 3 行 | `version = "0.4.1"` | `version = "0.4.2"`（**无 v**） |
| `app/utils/config.py` → 搜索 `current_version` | `current_version = "v0.4.1"` | `current_version = "v0.4.2"`（**带 v**） |
| `dist/installer.iss` → 搜索 `MyAppVersion` | `#define MyAppVersion "v0.4.1"` | `#define MyAppVersion "v0.4.2"`（**带 v**） |
| `README.md` 共 3 处：标题、徽章、架构图 | `v0.4.1` / `0.4.1` | `v0.4.2` / `0.4.2`（徽章无 v，其余带 v） |

> ⚠️ 四个文件缺一不可！漏改会导致版本号显示不一致或 CI 产物版本错乱。README.md 中历史版本记录（更新日志表格）不要动。

```bash
git add pyproject.toml app/utils/config.py dist/installer.iss README.md
git commit -m "chore: update version to <version> in config, installer and readme files"
git push origin dev
```

---

### 3. 生成并提交 CHANGELOG

获取上一个 tag 与 HEAD 间的变更：

```bash
PREV_TAG=$(git tag --list 'v*' --sort=-v:refname | head -1)
git log "$PREV_TAG"..HEAD --oneline --no-merges --format="%s"    # 提交列表
git log "$PREV_TAG"..HEAD --stat --no-merges                     # 文件/行变更统计
git log "$PREV_TAG"..HEAD --format="%an" --no-merges | sort -u   # 贡献者
```

按 Conventional Commits 前缀分类，同类 commit 合并为一条描述：

| 前缀 | 分类标题 |
|------|---------|
| `feat:` | ✨ 新功能 |
| `fix:` | 🐛 问题修复 |
| `refactor:` | ♻️ 代码重构 |
| `perf:` | ⚡ 性能优化 |
| `style:` | 🎨 样式改进 |
| `docs:` | 📚 文档 |
| `chore:` | 🔧 其他 |
| 其他/无前缀 | 🔄 其他变更 |

在 `CHANGELOG.md` 头部插入（参考已有格式风格）：

```markdown
## [v0.4.2] - 2026-07-17

自上一版本以来的变更 | 提交数：N · 文件变更：N · +N/-N | 贡献者：<name1>, <name2>

### ✨ 新功能 (New Features)
### 🐛 问题修复 (Bug Fixes)
### ♻️ 代码重构 (Refactoring)
### ⚡ 性能优化 (Performance)
### 🎨 样式改进 (Style)
### 🔧 其他 (Chores & Build)
```

```bash
git add CHANGELOG.md
git commit -m "docs: add v<version> changelog"
git push origin dev
```

---

### 4. 打 tag 并推送

```bash
git tag <version> -m "<version>"
git push origin <version>
```

推送 tag 后自动触发 GitHub Actions 的 Build & Release 工作流。

> CI 读取推送时的 CHANGELOG.md，因此**步骤 3 必须在步骤 4 之前完成**。

---

### 5. 验证

等待约 2-3 分钟，检查 Actions 状态：
- https://github.com/martin98-afk/DriFox/actions
- 或 API：`curl -s https://api.github.com/repos/martin98-afk/DriFox/actions/runs?event=push&branch=<version>`

CI 成功后检查 Release 页面：https://github.com/martin98-afk/DriFox/releases

**历史踩坑——常见 CI 失败修复：**

| 问题 | 症状 | 修复 |
|------|------|------|
| uv sync 失败 | `unknown field 'required-environments'` | `pyproject.toml` → `[tool.uv] required-environments` 改为 `environments`，再 `uv lock` |
| pyqt5-qt5 安装失败 | `no wheel for win_amd64` | 修复后 `uv lock` 自动修复跨平台标记 |
| build.py 中文报错 | `UnicodeEncodeError: 'charmap'` | `build.py` 中中文 print 改为英文 |
| CI 触发但没 Release | tag 推送后 Actions 未激活 | 确认 tag 打在 dev 分支 |

---

### 6. 收尾

确认 Release 页面包含更新日志和构建产物后，向用户报告完成。

---

### 干运行模式（`--dry-run`）

若用户指定了 `--dry-run`：
- 执行**步骤 1-3**（生成 changelog 预览）
- **跳过**所有 `git commit` / `git push` / `git tag` 操作
- **跳过**步骤 4-6
- 输出 changelog 内容供用户确认
