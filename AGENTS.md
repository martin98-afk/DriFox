# 项目开发规范

AI Agent 操作手册与约束。

---

## 1. 目标与边界

**允许**: 读写顶层/docs/skills等；执行 lint/构建/测试；增改功能修复问题。
**禁止**: 改 CI 配置（除非明确要求）、改 LICENSE、硬编码密钥、大范围重构。
**敏感**: `.github/workflows/*.yml`、`.env*`。

---

## 2. 推荐执行路径

```bash
git pull --rebase origin develop
uv sync --group dev
ruff check . && ruff format --check .    # 代码检查
pytest tests/ -x                         # 测试
# 修改...
pytest tests/ -x                         # 复测
git add -A && git commit -m "feat|fix|docs|chore: scope - summary"
git push origin develop
```

**单测**: `pytest tests/test_xxx.py -v` **打包**: `python build.py`

---

## 3. 项目结构

| 目录 | 职责 |
|---|---|
| `app/core/` | 引擎：backend、chat_session、hook_manager、workers |
| `app/gateway/` | 多平台网关（钉钉/Telegram/Discord/飞书） |
| `app/tools/` | 35+ 工具（读/写/搜索/MCP） |
| `app/widgets/` | UI 组件、设置卡片、像素宠物 |
| `plugins/system/` | 插件：hooks、skills、themes、commands |
| `tests/` | 测试 |

---

## 4. 关键依赖

Python 3.14+、PyQt5、PyQt-Fluent-Widgets、openai、loguru、httpx、mcp、pygls。可选组：gateway、dev、build。

---

## 5. 风格规范

- **格式化**: ruff（行宽120，双引号）
- **导入**: 标准→三方→本地
- **命名**: 代码英文；文件名小写中划线；注释中文
- **设计**: 函数短小单一职责

---

## 6. 提交规范

```
feat|fix|docs|chore|refactor|test: scope - summary
```

**强制同步**: 任何变化必须同步更新文档。不确定用 TODO。
