# 测试 / 构建 / 发版 / 提 PR

> 跑测试、打包、发版、提 PR 时加载。

---

## 一、常用命令

```bash
# 依赖（Windows 全量；其它平台见下）
uv sync --all-groups

# Lint / 类型
ruff check .                      # 必须干净
ruff format --check <改动文件>     # 只查改动文件
pyright .                         # 类型检查

# 测试（asyncio_mode = auto）
pytest tests/ -x
pytest tests/test_xxx.py -v
pytest tests/test_xxx.py::test_name -v
pytest tests/ -m perf             # 性能基准
pytest tests/ -m perf_long        # ≥30s 长基准（需 QApplication）
pytest tests/ -m stress           # 稳定性

# 运行
python main.py                    # GUI
python cli.py --version           # CLI

# 行尾守卫（提交前必跑）
python tools/eol_guard.py check
python tools/eol_guard.py fix <file>       # 误翻转时按 HEAD 风格还原
git config core.hooksPath scripts/hooks    # 每个克隆执行一次

# 打包
python build.py                   # Windows + Linux
```

**平台依赖组**：Windows `--all-groups`；mac `build + mac-build`（额外 dmgbuild + Pillow）；Linux `build + linux-build`。

**注意**：全项目 `ruff format --check` 本来就不通过（121 个文件带 BOM，见 P026），所以只对自己改动的文件做格式检查；提交门禁是 `ruff check` + 单测。

## 二、什么改动跑什么测试

| 改动 | 必跑 |
|------|------|
| 工具 / registry | 对应 tools 单测 + 别名映射测试 |
| Hook | 事件触发与阻断（最小 fixture）+ 热重载顺序测试 |
| 消息渲染 / 滚动 | `tests/widgets/` 下 message_card 相关（fence、diff tail loss、user bubble resize 等专项回归） |
| 性能 / 卡顿 | `pytest -m perf` + `scripts/measure_perf.py`（记录前后毫秒数） |
| 插件 | 启动 → 加载 → 热重载 → 卸载全流程 |
| 打包 | 打包后**实机启动**（无桌面 GL 的机器尤其要试）+ 联网功能（SSL） |
| 多窗口 | 起 2+ 窗口验证状态隔离（工作台显隐 / 页签 / 工作路径） |

测试规模参考：`tests/core/` ~87、`tests/widgets/` ~117、`tests/plugins/` ~87，顶层 ~80。优先复用现成 fixture（`tests/conftest.py`），不要重新发明。

## 三、打包要点（`build.py`）

- 模式：**`--onedir --windowed --name=Drifox`**（不是 onefile；`pyproject.toml` 里的 `[tool.pyinstaller]` onefile 标记与 build.py 不一致，以 build.py 为准）。
- `--add-data`：`plugins`、mermaid vendor（`chromium83-polyfill.js` + `mermaid.min.js`，无 CDN 降级）、KaTeX 整目录（js+css+fonts，相对路径引字体）。
- `--hidden-import`：gateway adapter（importlib 动态加载，PyInstaller 扫不到）。
- `--exclude-module`：插件自包含依赖（由插件 `deps/` 运行时提供）。
- **绝不能删的 DLL**（删了都是血案）：
  - ANGLE 三件套 `libEGL.dll` / `libGLESv2.dll` / `d3dcompiler_47.dll` —— main.py 强制 `AA_UseOpenGLES`，缺 libGLESv2 在无桌面 GL 的机器上启动即崩。
  - `libcrypto-1_1-x64.dll` —— Qt SSL(OpenSSL 1.1) 依赖，缺了 HTTPS 全失败（插件市场 icon 变占位）。
- 备用打包：`build_cxfreeze.py`；mac dmg 用 `create_dmg.py`；规格文件 `Drifox.spec`。

## 四、发版清单

版本号同步（漏一处就是不完整发版）：

| 文件 | 字段 |
|------|------|
| `app/utils/config.py` | 版本常量 |
| `pyproject.toml` | version |
| `dist/installer.iss` | 安装包版本 |
| `README.md` | 版本徽章 / 说明 |
| `CHANGELOG.md` | 新增条目 |

流程：`chore: update version to vX.Y.Z` → `docs: add vX.Y.Z changelog` → 打包实机验证 → 推 `dev` → 合 `main` 触发 release。

## 五、提 PR / 文档同步

| 范围 | 同步到 |
|------|--------|
| 功能 / 命令 / 配置变化 | `README.md` + `CHANGELOG.md` + 相关 `docs/` |
| Skill | 该技能 `SKILL.md` 的 description + README |
| Plugin | `plugins/<plugin>/README.md` + `marketplace.json`（CI 自动重生成） |
| 架构 | `docs/`（四大知识库：`perf/` `plugins/` `superpowers/` `research/`） |
| 设计文档 | `docs/superpowers/specs/YYYY-MM-DD-<topic>.md` |

提交格式：`feat|fix|docs|chore|refactor|test|perf: scope - summary`（summary 中文）。
