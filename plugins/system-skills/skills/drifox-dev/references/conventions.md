# 编码规范

> SKILL.md 在任务涉及「命名/风格/提交」时分派到这里。

---

## 一、命名约定

| 对象 | 风格 |
|------|------|
| 文档 / 注释 / 日志 | **中文** |
| 代码符号 | 英文，语义直白 |
| 文件名 | 小写加下划线（snake_case） |
| 类名 | PascalCase |
| 函数 / 变量 | snake_case |

## 二、质量标准

- **格式化**：ruff（行宽 120，双引号）
- **设计品味**：优先消除分支与重复，函数单一职责且短小
- **禁止**：顺手重构相邻代码（除非任务明确要求）
- **范围控制**：每一行变更应能直接追溯到用户的请求
- **Lint**：`ruff check app/`
- **格式检查**：`ruff format app/ --check`

## 三、提交规范

```
<type>: <scope> - <summary>

type:    feat | fix | docs | chore | refactor | test | perf
scope:   受影响的模块 / 目录
summary: 一句话中文说明
```

示例：
```
fix: hook - 编辑/新增 hook 后自动保存预设 agent_identity
feat: core - 引入上下文自动压缩机制
```

## 四、AGENTS.md 铁律（项目根 .work/DriFox/AGENTS.md）

| ❌ 禁止 | ✅ 允许 |
|--------|--------|
| 修改 `.github/workflows/*.yml`（除非明确要求） | 正常读 / 跑 CI 查看结果 |
| 修改 `LICENSE` / `CODE_OF_CONDUCT.md` | — |
| 硬编码密钥、Token、敏感凭证 | 用环境变量 / 配置中心 |
| 大范围顺手重构 | 精确修改任务范围内文件 |

**强制同步规则**：任何功能 / 命令 / 配置 / 目录 / 工作流变化必须同步更新相关文档，否则视为不完整提交。
