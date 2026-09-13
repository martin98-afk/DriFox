---
description: 自动发布新版本：生成更新日志、打 tag、推送触发 CI
type: prompt
argument-hint:
  "<version>": "版本号（如 v0.4.2，可选，不传则自动自增一个小版本）"
  "[--dry-run]": "试运行模式：只生成 changelog 不推送"
  "[--republish]": "重新发布已有版本（tag 已存在时跳过确认问句，按双删重建走）"
  "[--force]": "跳过所有确认问句（包括 tag 删除、tag -f、push -f 等破坏性操作）"
---

## Release 发布工作流

`$ARGUMENTS` 是用户输入的完整字符串（不含 `/release` 前缀）。

**参数解析**：
- `<version>`：可选，格式 `v0.4.2`；未传则从 `app/utils/config.py` 读取 `current_version` 并自增末位（如 `v0.4.1` → `v0.4.2`）
- `--dry-run`：可选，仅生成 changelog 预览，不提交不推送
- `--republish`：可选，明确意图"重新发布已有版本"，跳过重新发布场景的确认问句
- `--force`：可选，跳过所有确认问句（包括 tag 删除与重新发布），与 `--republish` 互斥可叠加（`--republish --force` 等价于一次性直接重建）

**平台差异**：本工作流示例以 Windows + PowerShell/Git Bash 双兼容写法呈现。Windows `cmd` 不识别 `head`/`wc`/`grep`/`sort -u`/`$(...)`（注意不是 `findstr` 但常被认错），示例里给出的命令均用 `git`/`PowerShell` 原生方式，**禁止**依赖 Unix 工具链。

---

### 0. 破坏性操作门控

下列操作执行前**必须**先经用户确认（除非带了 `--force`）：
- 删除本地或远程 tag（`git tag -d` / `git push origin :refs/tags/<v>`）
- 重新发布同名版本（重建 tag + 同步旧 Release）
- `git push -f` 或 `git tag -f`（强制覆盖）

> 💡 用 `question` 工具列出 2~3 个候选方案（双删重建 / 用新版本号 / `git tag -f`），让用户点击选择。`--force` 仅在你判定用户意图已经清晰（如显式说"重建 tag 不管 Release"）时才跳过。

---

### 1. 前置检查

```bash
git branch --show-current          # 必须是 dev，否则中止
git status --short                 # 必须为空，否则中止
git tag --list <version>           # 必须无输出，否则进入"重新发布"分支
```

- 如未传版本：自动自增（读取 `app/utils/config.py` 中 `current_version`，去掉 `v` 前缀，按 `.` 分割，末位 +1，重组）
- 版本格式校验：以 `v` 开头 + 语义化版本（如 `v0.4.2`）

**重新发布分支（`git tag --list <version>` 有输出时进入）**：

1. 记录旧 tag 指向的 commit（如 `git log -1 <version> --format=%H`）；
2. 列出旧 tag 与 HEAD 之间的新 commits（`git log <version>..HEAD --oneline --no-merges`）；
3. 用 `question` 询问用户三个候选方案：
   - **A. 双删重建**（默认）：`git push origin :refs/tags/<v>` + `git tag -d <v>` → 走完后续步骤 → 在 HEAD 重新打 tag。注意 GitHub Release 页面**不会**随 tag 删除自动消失，步骤 5 收尾时提示用户手工 `gh release delete <v> --yes`。
   - **B. 用新版本号**：自增末位成 `<v+1>`，按正常流程发。
   - **C. `git tag -f`**：保留旧 tag 名、强制推到 HEAD（`git tag -f <v> HEAD && git push origin <v> --force`）。CHANGELOG 仍需更新到 HEAD，但 GitHub Release 关联不变（指向新 commit）。

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

获取上一个 tag 与 HEAD 间的变更（Windows + Git Bash 双兼容）：

```bash
# 上一个 tag —— 用 git describe 或 rev-parse 替代 $(...)
PREV_TAG=$(git describe --tags --abbrev=0 2>nul || git tag --list 'v*' | Select-Object -Last 1)

# 提交列表
git log "$PREV_TAG"..HEAD --oneline --no-merges

# 文件/行变更统计
git log "$PREV_TAG"..HEAD --shortstat --no-merges

# 贡献者（git 自带去重 + 排序，避免依赖 sort -u）
git log "$PREV_TAG"..HEAD --pretty="%an" --no-merges | Sort-Object -Unique
```

> ⚠️ 若宿主是纯 Windows cmd（无 PowerShell），把 `Sort-Object` 换成 `git log --pretty="%an" --no-merges` 直接人工读输出去重；或者用 `python -c "import sys; [print(l,end='') for l in sorted(set(sys.stdin))]"`。**禁止**直接 `sort -u`（cmd 不识别 `-u`，会把 `-u` 当文件名）。

**CHANGELOG 已有同名条目的处理**（重新发布场景常见）：

`CHANGELOG.md` 顶部若已存在 `## [<version>] - <date> (...)`，按下列规则选一处理：

| 历史后缀 | 处置 |
|---------|------|
| 无后缀或 `(重新发布)` | 在该条目最末尾（`## [下个版本]` 之前）的"### 🔧 其他 (Chores & Build)"后**追加**新增章节，标题改为 `(重新发布 #2)`，并按下方"统计行公式"累加计数 |
| `(重新发布 #N)` | 同上追加，标题改为 `(重新发布 #(N+1))` |
| `(重新发布 v2)` 之类非数字后缀 | 询问用户：保留原标题追加，还是改成统一数字后缀 |

> 永远不要把同一个版本的两段变更拆成两个 `## [<version>]` 标题，否则 GitHub Release 页面的"Auto-generated changelog"会重复渲染。

**统计行公式**（重新发布时累加）：

```
提交数 N'    = 原条目 N + 新增 commits 数
文件变更 F'  = 原条目 F + (新增 commits 触及的文件去重数)
+/- 行数 L'  = 原条目 +/- + 新增 commits +/- 
贡献者       = 原贡献者 ∪ 新增贡献者（同名不重复）
```

文件与行数取自 `git log PREV_TAG..HEAD --shortstat --no-merges`，用 `awk`（Git Bash）或 `python -c`（cmd）做累加，**禁止**靠肉眼看。

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

**重新发布场景**：若选了 A 方案，旧本地 tag 已在步骤 1 删除，此处正常 `git tag` 即可。若选了 C 方案用 `git tag -f`，命令改为：

```bash
git tag -f <version> HEAD -m "<version>"
git push origin <version> --force
```

---

### 5. 验证

等待约 2-3 分钟，检查 Actions 状态：
- https://github.com/martin98-afk/DriFox/actions
- 或 API：`curl -s https://api.github.com/repos/martin98-afk/DriFox/actions/runs?event=push&branch=<version>`

CI 成功后检查 Release 页面：https://github.com/martin98-afk/DriFox/releases

**重新发布场景的 Release 页面同步**：

若步骤 1 选了 A 方案（双删重建），删除 tag 不会删除 GitHub Release 页面本身。CI 跑完后**额外**执行：

```bash
gh release delete <version> --yes --repo martin98-afk/DriFox
```

然后让 CI 自动重新创建 Release（CI 工作流里通常有 `actions/create-release` 或 `softprops/action-gh-release` 步骤）。若 CI 不会自动创建，则需要 `gh release create <version> --generate-notes`。

> 💡 若没装 `gh` CLI，提示用户去 https://github.com/martin98-afk/DriFox/releases 手工点 Delete。

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
