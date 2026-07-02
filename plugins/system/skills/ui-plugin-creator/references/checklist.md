# UI 插件验证清单

> 本文是 `SKILL.md §7` 的完整展开。
> 开发流程见 `references/workflow.md`，常见模式见 `references/patterns.md`。

---

## 1. 基础验证（每个 UI 插件必查）

### 1.1 代码质量

- [ ] `ruff check plugins/<plugin-name>/ui/` 通过
- [ ] `ruff format --check plugins/<plugin-name>/ui/` 通过（或已 format）

### 1.2 插件清单

- [ ] `.drifox-plugin/plugin.json` 声明了 `"ui": true`
- [ ] `name` 字段与目录名一致
- [ ] `version` 字段符合 semver（`X.Y.Z`）

### 1.3 注册入口

- [ ] `ui/__init__.py` 有 `register_ui(registry)` 函数
- [ ] 热重载兼容：清理了 `sys.modules` 中 `ui_plugin_<safe_name>.` 前缀的子模块
- [ ] 没主动删除 `__pycache__/`（让 Python 自己判断）

### 1.4 浮动卡片（如果用了 `register_floating_card`）

- [ ] 实现了 `closed` 信号
- [ ] 没有 `setMinimumHeight(400)` 之类的固定高度
- [ ] 实现了 `sizeHint()` 返回窗口高度的 85%
- [ ] 实现了 `showEvent` 安装窗口 resize 事件过滤器
- [ ] 实现了 `eventFilter` 监听 Resize 并 `updateGeometry()`
- [ ] 实现了 `set_context_provider(provider)` 方法
- [ ] 实现了 `show_card()` 调用 `_apply_latest_theme` + `_async_load_data`

### 1.5 插件闭包

- [ ] 没导入 `app.core` 或 `app.widgets` 内部的任何模块
- [ ] 用 stdlib（`pathlib` / `shutil` / `sqlite3`）做文件操作
- [ ] 用 `loguru` 做日志

### 1.6 异步操作（如果用了 worker）

- [ ] `_Worker` 类有 `finished` / `error` 两个 pyqtSignal
- [ ] 每次新任务前调用 `_cleanup_worker()`
- [ ] `worker.deleteLater` / `thread.deleteLater` 都连接到 finish/error
- [ ] 重写了 `deleteLater()` 调用 `_cleanup_worker()`
- [ ] `_cleanup_worker()` 用 `wait(500)` 给超时
- [ ] `_fetch_data` 是同步函数（worker 在后台线程调）

---

## 2. 主题色验证

### 2.1 上下文注入

- [ ] 实现了 `set_context_provider(provider)`
- [ ] `show_card` 时调用 `_apply_latest_theme`
- [ ] 用 `ctx.get("colors", {}).get(key, fallback)` 而不是 `ctx["colors"][key]`
- [ ] 异常时 fallback 到 `_default_chart_colors()`

### 2.2 浮动卡片背景适配

- [ ] `_default_chart_colors()` 的 `text` / `text_secondary` 固定白色（避免黑字）
- [ ] 主题色变化时调 `show_card` 能刷新（重新拉 ctx）
- [ ] 子 widget（`_StatCard` / 图表）都实现 `set_colors(colors)`

### 2.3 字体注入（重要 — 最容易出 bug 的地方 ⚠️）

#### 三层策略验证

- [ ] **第 1 层（QFont 级联）**：`self.setFont(QFont(family, size))` — 参数用 `font_size if font_size else 14`，**不是** `0 if font_size else 0`
- [ ] **第 2 层（QSS 替换）**：`_retheme()` 中用 `re.sub` 替换 QSS 中的 `font-size` 为 `{fs}px`，因为 QSS 优先级高于 QFont
- [ ] **第 3 层（FluentLabelBase 覆盖）**：对 `StrongBodyLabel` 等，`isinstance(child, FluentLabelBase)` 时直接 `setFont(QFont(ff, fs))`

> 三层缺一不可！只做第 1 层 → QSS 的 `font-size: 11px` 保持极小；
> 只做第 1+2 层 → StrongBodyLabel 内部 `setFont()` 覆盖父级；
> 只做第 1+3 层 → 普通 QLabel 的 QSS 覆盖 QFont 级联。

#### 常见陷阱

- [ ] ❌ `QFont(family, 0)` — 字号极小，必须用真实值
- [ ] ❌ 动态创建 `_PluginRow` 等子控件后忘调 `_retheme()`
- [ ] ❌ `_make_style` 的 `font-family` 宏尾号（末尾需要分号 `;`）
- [ ] ❌ `_retheme()` 只替换 `color` 不替换 `font-size` — 文字颜色对了但字号还是 11px

#### 验证方法

```
启动程序 → 打开插件卡片
1. 插件名（StrongBodyLabel）和主界面标题字体大小一致？  → 检查第 3 层
2. 说明文字（QLabel）字号和主界面正文一致？              → 检查第 2 层
3. 动态刷新/搜索后新出来的行字号也正确？                  → 检查 _retheme() 是否调用了
```

> 详见 `patterns.md §1.4`（三层字体策略）。

### 2.4 按钮样式工厂模式

- [ ] 每个按钮类型有独立的 `_xxx_btn_style(accent)` 方法
- [ ] 按钮样式用 `_adjust_color()` 生成渐变/悬停色
- [ ] `_apply_latest_theme` 中通过 `_xxx_btn_style()` 更新按钮颜色
- [ ] 紧凑按钮（`fixedSize` < 40px 高度）用 `padding: 0 Xpx` 而非 `padding: 16px 0`
  > ❌ `padding: 16px 0` + `setFixedHeight(32)` → 文字不可见（32-16-16=0）
  > ✅ `padding: 0 10px` + `setFixedHeight(32)` → 正确

### 2.5 常见场景

| 场景 | 应该 |
|------|------|
| 深色主题 + 浮动卡片 | ✅ 文字清晰可见（白色） |
| 浅色主题 + 浮动卡片 | ✅ 文字清晰可见（白色，因为卡片背景仍偏暗） |
| 主程序主题切换 | ✅ 重新进入卡片后颜色更新 |
| ctx 缺少某 key | ✅ fallback 到默认色 |
| 插件字体和主程序不一致 | ✅ 字体由 _apply_latest_theme 注入 |

详见 `widgets-theme.md` 和 `patterns.md §1.4`。

---

## 3. 外部依赖验证（如果用了 `_vendor/`）

### 3.1 目录与文件

- [ ] `ui/_vendor/<package>/` 下每个包都有 `__init__.py`
- [ ] 没有 `.dist-info/` 目录
- [ ] 删除了 `__pycache__/` 和 `.pyc`（减小体积）
- [ ] 是纯 Python 包，或 C 扩展包的目标平台/Python 版本完全匹配

### 3.2 register_ui 配置

- [ ] 用 `Path(__file__).parent / "_vendor"` 而非硬编码路径
- [ ] `register_ui` 开头**先清理 sys.modules** 中已缓存的 vendored 包（避免被提前 import 的同名包绕过 _vendor/）
- [ ] `register_ui` 末尾加 `assert "_vendor" in <pkg>.__file__` 或日志，确认实际加载来源
- [ ] 显式指定了 `vendor_packages` 列表（替换 `<package-a>` / `<package-b>`）

### 3.3 热重载兼容性

- [ ] 热重载后 `vendor_dir` 仍生效（`Path(__file__)` 在热重载时指向新位置，`sys.path.insert` 重复执行是安全的）

### 3.4 打包测试

- [ ] 用 PyInstaller 打包后测试：把整个 `plugins/<plugin-name>/` 复制到 `dist/<exe>/`，运行后能 `import` 第三方包并调用 API

完整模板与陷阱见 `templates.md §五`。

---

## 4. 可复用 widgets 验证（如果用了 widgets）

- [ ] 从 `widgets-*.md` 复制的代码**没改核心逻辑**（改了就违背了"可复用"初衷）
- [ ] 控件的 `set_colors(colors)` 都从父卡片的 `_chart_style` 注入
- [ ] 图表 `set_data(...)` 后调用了 `self.update()` 触发 paintEvent
- [ ] `paintEvent` 中每个提前 `return` 前都有 `painter.end()`

---

## 5. SQLite 验证（如果读了数据库）

- [ ] `_find_db()` 兜底到 `~/.drifox/` 用户目录
- [ ] `sqlite3.connect(..., timeout=3)` 设了超时
- [ ] `conn.row_factory = sqlite3.Row` 设为字典式访问
- [ ] SQL 用 `COALESCE(col, 0)` 包了 NULL 值
- [ ] 大字段（`messages` JSON）截断到 100k 字符防 OOM
- [ ] `finally: conn.close()` 关闭连接
- [ ] `except Exception` 返回 `{"error": str(e)}` 不抛到 UI 线程
- [ ] 14 天窗口查询用了"建标签 → 初始化 map → 查询 → 归类"模板

详见 `widgets-sqlite.md`。

---

## 6. 数据展示验证

- [ ] 数字格式化用 `_format_number()`（如 1.2k）
- [ ] 百分比格式化用 `_format_pct()`（如 85%）
- [ ] token 估算用 `_fast_estimate_tokens()`（无 tiktoken 依赖）
- [ ] 日期带星期用 `_short_weekday()` 或内联实现
- [ ] 浮点除法用 `if total > 0 else 0.0` 防除零

详见 `widgets-utils.md`。

---

## 7. UI/UX 验证

### 7.1 布局

- [ ] 卡片宽度变化时统计卡片/图表自适应（参考 `if w >= 420` 分支）
- [ ] 标签不被裁剪（折线图 `top_margin = max_val * 0.3`）
- [ ] 折线图区域填充闭合路径正确（`path.lineTo(..., margin_top + chart_h)`）
- [ ] 周末 X 轴标签高亮（周一/周六/周日显示星期）

### 7.2 加载状态

- [ ] 加载中显示"读取中…"
- [ ] 加载完成隐藏加载提示
- [ ] 加载失败显示错误信息（截断到 60 字符）
- [ ] 空数据状态显示友好提示
- [ ] 刷新按钮在加载中被禁用

### 7.3 关闭流程

- [ ] 点关闭按钮 → 卡片隐藏
- [ ] 卡片隐藏后 `closed` 信号被发射
- [ ] CardManager 状态同步（重新打开能正常显示）

---

## 8. 性能验证

### 8.1 UI 线程不卡

- [ ] 所有可能 > 100ms 的操作都在 `_Worker` 后台线程
- [ ] 数据库查询在后台线程
- [ ] HTTP 请求在后台线程
- [ ] 大文件扫描在后台线程

### 8.2 内存不泄漏

- [ ] `worker.deleteLater` / `thread.deleteLater` 连接
- [ ] 重写 `deleteLater()` 调用 `_cleanup_worker()`
- [ ] 卡片销毁时清空 `_content_layout`（`item.widget().deleteLater()`）

### 8.3 启动不慢

- [ ] register_ui 不做阻塞操作（不做网络请求/大文件读取）
- [ ] 字体、图标等资源在首次使用时加载

---

## 9. 跨环境验证

- [ ] dev 环境（`python main.py`）能加载
- [ ] 打包后（PyInstaller exe）能加载
- [ ] 用户插件路径（`~/.drifox/plugins/<name>/`）能加载
- [ ] 主程序主题切换不破坏卡片显示
- [ ] 主程序语言切换不破坏卡片显示

---

## 10. 提交前最终检查

```bash
# 1. ruff
ruff check plugins/<plugin-name>/ui/
ruff format --check plugins/<plugin-name>/ui/

# 2. 触发热重载
# 修改 plugin.json 或 ui/ 下任一文件
# 观察日志无 ImportError / SyntaxError

# 3. 实际测试
# 打开卡片 → 检查主题色 → 检查数据加载 → 检查关闭
# 多刷新几次 → 检查 worker 内存（用 memory_profiler 或观察内存）

# 4. 提交
git add plugins/<plugin-name>/
git commit -m "feat(<plugin-name>): <功能描述>"
```

---

## 11. 故障排查速查

| 症状 | 可能原因 | 见哪一节 |
|------|---------|---------|
| 卡片不显示 | `container` 错 / `default_visible` 错 | §1.4 |
| 修改不生效 | sys.modules 缓存没清 | §1.3 |
| 主题色不变 | `_apply_latest_theme` 没调 / `set_colors` 漏调用 | §2.1 / §4 |
| Worker 卡死 | `deleteLater` 没连 / `wait()` 超时太长 | §1.6 |
| 数据库锁 | `timeout=3` + 重试逻辑缺失 | §5 |
| 文字看不见 | 浮动卡片背景暗 + 黑字 | §2.2 |
| 图表不刷新 | `set_data` 后忘了 `update()` | §4 |
| 打包后 ImportError | `_vendor/` 没配置 / sys.modules 缓存 | §3 |
| 标签被裁剪 | 没算 `top_margin` / `label_y < margin_top` 没处理 | §7.1 |
| 内存泄漏 | widget 没 `deleteLater` / worker 没清理 | §8.2 |