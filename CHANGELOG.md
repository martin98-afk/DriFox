# Changelog
All notable changes to this project will be documented in this file.

## [v0.2.10] - 2026-06-23

### 4 commits since v0.2.9 | 1 contributor (dingma)

---

### ✨ 新功能 (New Features)

- **`/release` 工作流命令**: 新增自动化发布命令 (`plugins/system/commands/release.md`)
  - 支持自动生成 CHANGELOG、打 tag、推送触发 CI、更新 Release Notes
  - 参数 `--dry-run` 仅预览不推送
- **CI/CD 流水线增强**: 改进 `.github/workflows/release.yml`
  - 新增 lint 和 import check job，失败则跳过后续构建
  - release job 自动从 `CHANGELOG.md` 提取内容作为 Release Body
- **drifox-dev 开发技能**: 新增 `plugins/system/skills/drifox-dev/SKILL.md`，提供开发指南文档

### 🐛 问题修复 (Bug Fixes)

- **build.py**: 优化清理脚本的 print 语句，提升日志清晰度

### ♻️ 代码重构 (Refactoring)

- **pyproject.toml**: 重整依赖组（`[dependency-groups]` 为唯一依赖组定义），添加 ruff 配置；移除 `[project.optional-dependencies]`

### 📚 文档 (Documentation)

- **release.md**: 澄清 release content 验证流程

### 🔧 其他 (Chores & Build)

- **`/release` 工作流**: 增加版本号升级阶段（阶段 B），统一修改 3 个版本号文件
- **版本号升级**: v0.2.9 → v0.2.10
  - `pyproject.toml` 第 3 行（不带 v）
  - `app/utils/config.py` 第 242 行（带 v）
  - `dist/installer.iss` 第 7 行（带 v）
