# 测试 / 构建 / 部署

> SKILL.md 在任务涉及「跑测试 / 打包 / 提 PR」时分派到这里。

---

## 一、测试要点（什么改动必跑什么）

| 修改内容 | 必跑测试 |
|---------|---------|
| 工具执行逻辑 | 该工具所有受影响调用 + 涉及 tools 模块的单测 |
| 信号链 | 端到端：从 emit 到 slot，确认前端能收到 |
| 多窗口组件 | 验证窗口间状态隔离（启动 2+ 窗口） |
| Hook 系统 | 事件触发 / 阻断逻辑（写最小 fixture） |
| Plugin | 启动 → 加载 → 卸载全流程 |
| Gateway | 网关模拟器测试（钉钉 / Telegram / Discord） |

> 优先用 `tests/` 下现成 fixture，不要每次都重新发明。

## 二、构建与运行

```bash
# 安装依赖
uv sync                          # 核心
uv sync --group dev              # 开发工具
uv sync --all-groups             # 全部

# 跑应用
python main.py                   # GUI
python cli.py --version          # CLI

# 测试 / Lint
ruff check app/                  # Lint
ruff format app/ --check         # 格式检查
pytest                           # 测试
pytest tests/test_xxx.py -v      # 单文件

# 打包（PyInstaller）
python build.py
```

## 三、提 PR / 同步文档

每次功能 / 配置 / 目录变化必须同步：

| 范围 | 文档 |
|------|------|
| Skill | `plugins/system/skills/<name>/README.md` 或 SKILL.md description |
| Plugin | `plugins/<plugin>/README.md` |
| UI 改动 | `README.md` 截图 / 章节 |
| 命令 | `commands/<cmd>.md` 自带说明 |
| 配置 | `config.example.yaml`（如有） |
| 架构 | `docs/superpowers/specs/<date>-<topic>.md` |
