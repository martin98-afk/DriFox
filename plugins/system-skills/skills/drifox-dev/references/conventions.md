# 编码规范与项目铁律

> 命名 / 风格 / 提交 / 行尾 / 范围控制。

---

## 一、命名约定

| 对象 | 风格 |
|------|------|
| 文档 / 注释 / 日志 / 提交 summary | **中文** |
| 代码符号 | 英文，语义直白 |
| 文件名 | 小写 + 下划线（snake_case） |
| 类名 | PascalCase |
| 函数 / 变量 | snake_case |

## 二、质量标准

- 格式化 ruff（行宽 120，双引号）；类型 pyright。
- 函数短小、单一职责；优先消除分支与重复。
- Python 3.14+：可用 PEP 758 无括号多异常 `except A, B:`，但**项目既有元组风格不要被 formatter 展开**（当前 ruff 版本会这么干，见 P026）。
- Lint：`ruff check .`；格式只查改动文件（全项目 `--check` 因历史 BOM 不过）。

## 三、提交

```
<type>: <scope> - <summary>
type:    feat | fix | docs | chore | refactor | test | perf
scope:   受影响模块/目录
summary: 一句中文
```

示例：`fix: message_card - 流式渲染 fence 哨兵覆盖 worker 两分支`

## 四、行尾（EOL）纪律 —— 提交前必查

Windows 上 `open(p, "w")` 会把 `\n` 静默转成 `\r\n`。一次误写 = 整文件行尾翻转 = diff 声明上千行、真实改动被淹没、blame 全毁（2026-09-06 一次评审连中 3 个文件）。

- **写文件一律用** `tools/eol_guard.py` 的 `write_text_keep_eol(path, text)`（内部 `newline=""`）。禁止裸 `open(p, "w")` 写源码。
- 提交前 `python tools/eol_guard.py check`（`--cached` 查暂存区）。
- `scripts/hooks/pre-commit` 自动拦截（噪声占比 ≥80% 且改动 ≥20 行）；确属整文件重写时用 `git commit --no-verify`。
- 误翻转修复：`python tools/eol_guard.py fix <file>`。
- 全仓归一化（`normalize --eol lf`，默认 dry-run）属破坏性大动作，需独立窗口期提交。

## 五、范围铁律

- 只改任务指定文件 / 新功能必须创建的新文件。
- 不顺手改进相邻代码、不重构没坏的东西、不删改动前就存在的死代码（发现就提一句）。
- 每行变更都要能直接追溯到用户请求。
- 无关文件的变更：跳过，不撤销（谁改谁负责）。

## 六、其它禁止项

| ❌ 禁止 | ✅ 允许 |
|--------|--------|
| 修改 `.github/workflows/*.yml`（除非明确要求） | 读 / 跑 CI 看结果 |
| 修改 `LICENSE` / `CODE_OF_CONDUCT.md` | — |
| 硬编码密钥 / Token | 环境变量 / 配置 |
| 大范围顺手重构 | 精确修改 |

**文档同步强制**：功能 / 命令 / 配置 / 目录 / 工作流变化必须同步 README、CHANGELOG、相关 docs，否则视为不完整提交。
