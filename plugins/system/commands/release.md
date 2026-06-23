---
description: 自动发布新版本：生成更新日志、打 tag、推送触发 CI
type: prompt
argument-hint:
  "<version>": "版本号（如 v0.2.10，必填）"
  "[--dry-run]": "试运行模式：只生成 changelog 不推送"
---

## Release 发布工作流

`$ARGUMENTS` 是用户输入的完整字符串（不含 `/release` 前缀）。

**参数解析**：
- 第一个参数为 `<version>`（必填），格式如 `v0.2.10`
- `--dry-run` 可选，仅生成 changelog 预览，不提交不推送

---

### 阶段 A：准备工作（验证环境）

```
1. 确认当前在 dev 分支上
   → git branch --show-current | 如果不是 dev 就报错退出
2. 确认工作区干净
   → git status --short | 如果有未提交变更就报错退出
3. 确认新版本号格式正确（以 v 开头，如 v0.2.10）
4. 确认该版本号尚未使用
   → git tag --list <version> | 如果已存在就报错退出
```

---

### 阶段 B：版本号升级（统一修改）

版本号涉及 **3 个文件**，必须全部更新，保持统一：

| 文件 | 位置 | 修改内容 | 示例 |
|------|------|---------|------|
| `pyproject.toml` | 第 3 行 | `version = "0.2.9"` → `version = "0.2.10"`（**不带 v 前缀**） |
| `app/utils/config.py` | 第 242 行 | `current_version = "v0.2.9"` → `current_version = "v0.2.10"`（**带 v 前缀**） |
| `dist/installer.iss` | 第 7 行 | `#define MyAppVersion "v0.2.9"` → `#define MyAppVersion "v0.2.10"`（**带 v 前缀**） |

**执行步骤**：
```
1. 用新版号替换 3 个文件中的旧版本字符串
2. 注意格式差异：
   - pyproject.toml：不带 v（0.2.10）
   - config.py：带 v（v0.2.10）
   - installer.iss：带 v（v0.2.10）
3. 提交版本升级：
   git add pyproject.toml app/utils/config.py dist/installer.iss
   git commit -m "chore: update version to <version> in config and installer files"
   git push origin dev
```

> ⚠️ **经验**：如果忘了改某个文件，会导致版本号显示不一致，或 CI 打包后版本号错乱。

---

### 阶段 C：获取变更历史

```
1. 找到上一个 tag（最新存在的 tag）
   → git tag --list 'v*' --sort=-v:refname | head -1
   → 记为 $PREV_TAG
2. 计算两个 tag 间的 commits
   → git log $PREV_TAG..HEAD --oneline --no-merges --format="%s"
3. 按 Conventional Commits 前缀分类：
   feat:     → 「✨ 新功能」
   fix:      → 「🐛 问题修复」
   refactor: → 「♻️ 代码重构」
   perf:     → 「⚡ 性能优化」
   style:    → 「🎨 样式改进」
   docs:     → 「📚 文档」
   chore:    → 「🔧 其他」
   无前缀/其他 → 归入「🔄 其他变更」

4. 获取 commit 统计信息
   → git log $PREV_TAG..HEAD --stat --no-merges
   → 统计：总提交数、变更文件数、+行数/-行数
5. 获取贡献者列表
   → git log $PREV_TAG..HEAD --format="%an" --no-merges | sort -u
   → 排序取唯一值
```

---

### 阶段 D：生成 CHANGELOG.md

按以下模板生成当前版本的更新日志：

```markdown
## [版本号] - 发布日期

自上一版本以来的变更 | 提交数：N · 文件变更：N · +N/-N | 贡献者：<name1>, <name2>

### ✨ 新功能 (New Features)
### 🐛 问题修复 (Bug Fixes)
### ♻️ 代码重构 (Refactoring)
### ⚡ 性能优化 (Performance)
### 🎨 样式改进 (Style)
### 🔧 其他 (Chores & Build)
```

**格式规范**（参考之前 v0.2.8 的手写风格）：
- 每个分类下列出具体功能点，用 `-` 列表
- 功能描述用中文，简洁但完整
- 同类 commit 合并成一条描述
- 生成后**追加到 CHANGELOG.md 文件头部**（新版本在最上面）

> ✅ 版本号已在阶段 B 统一更新，这里直接使用即可

---

### 阶段 E：提交 changelog

```
1. git add CHANGELOG.md
2. 如果有顺手改的版本文件，一并 git add
3. git commit -m "docs: add v<version> changelog"
4. git push origin dev
```

---

### 阶段 F：打 tag 并推送

```
1. git tag <version> -m "<version>"
2. git push origin <version>
   → 这会自动触发 GitHub Actions 的 Build & Release 工作流
3. 提示用户："✅ tag <version> 已推送，CI/CD 正在构建..."
```

---

### 阶段 G：验证 CI/CD 状态

```
1. 等待约 2-3 分钟后检查 Actions 状态
   → 可以用以下 API 查询（不需要认证，但有速率限制）：
     curl -s https://api.github.com/repos/martin98-afk/DriFox/actions/runs?event=push&branch=<version>
   → 或者让用户去 https://github.com/martin98-afk/DriFox/actions 查看
```

**常见 CI 失败预判与修复**（v0.2.9 踩过的坑，记牢）：

| 问题 | 症状 | 修复 |
|------|------|------|
| ❌ uv sync 失败 | `unknown field 'required-environments'` | `pyproject.toml` 中把 `[tool.uv] required-environments` 改为 `environments`，然后 `uv lock` |
| ❌ pyqt5-qt5 安装失败 | `no wheel for win_amd64` | 上面修完 `uv lock` 会自动修复跨平台标记 |
| ❌ build.py 中文报错 | `UnicodeEncodeError: 'charmap' codec` | `build.py` 中的 `print("中文")` 改为 `print("English")` |
| ❌ CI 触发但没 Release | tag 推送后 Actions 没跑 | 检查是否在 dev 分支上打的 tag |

---

### 阶段 H：验证 Release 内容

CI 构建成功后，GitHub Release 会被自动创建，Release Body 会自动从 `CHANGELOG.md` 提取。

> ⚠️ CI 读取的是**推送 tag 时 repo 中已提交的 CHANGELOG.md**，所以阶段 D-E（生成并提交 changelog）必须在打 tag 之前完成。

**验证方式**：
- 自动提取的内容即阶段 D 生成的 CHANGELOG 条目
- 确认 Release 页面已包含更新日志和构建产物
  → https://github.com/martin98-afk/DriFox/releases
- 如果需要手动调整，直接去 Release 页面编辑

---

### 阶段 I：清理收尾

```
1. 确认 Release 页面有更新日志和构建产物
   → https://github.com/martin98-afk/DriFox/releases
2. 确认没问题后报告完成
```
