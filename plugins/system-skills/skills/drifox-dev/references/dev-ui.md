# UI 组件开发要点

> 加卡片 / 改布局 / 调主题 / 加设置项时加载。
> **改消息渲染、流式、滚动、WebView → 读 `rendering-pipeline.md`**，本文件只覆盖通用 UI。

---

## 一、核心规则

| 要点 | 说明 |
|------|------|
| 继承 QObject | 需要信号 / 槽的类必须继承 |
| 线程安全 | UI 只在主线程动；后台一律 signal → slot |
| 卡片系统 | 设置类 UI 用 `app/widgets/cards/`（含 floating/ 浮动卡） |
| 主窗口拆分 | 新逻辑优先放 `app/widgets/modules/`，不要往 `main_widget.py` 堆 |
| 设计令牌 | 用 `app/utils/design_tokens.py`，不硬编码颜色 |
| 主题兼容 | 亮 / 暗双修；插件图标同时提供 `icon_path`（深）与 `icon_light_path`（浅） |
| WebEngine | 共享 profile：`app/core/webengine_profile.py`；实例复用走 `WebViewPool` |
| 懒渲染 | 大量条目分批渲染（BATCH_SIZE + 定时器让 Chromium 喘息） |

## 二、布局与尺寸的坑（真实环境才暴露）

| 坑 | 根因 | 规避 |
|----|------|------|
| 列表底部大段空白 | `QLabel(wordWrap)` 的 C++ `sizeHint` 按**理想宽度**算换行，比实际高 8~50% | 覆盖行 `sizeHint()` 返回 `layout.heightForWidth(当前宽度)`；`QScrollArea` 用 `setWidgetResizable(False)` + 手动 `_sync_content_size()` |
| 覆盖 `sizeHint()` 不生效 | PyQt5 的 `QWidgetItem::sizeHint` → C++ 内部调用**不派发 Python override** | 由容器主动调用，不依赖 Qt 自动派发 |
| 窗口 resize / 滚动条出现后重排错乱 | 行按新宽度重排，content 高度未重算 | 两阶段同步：先按 sizeHint 撑开 → resize 触发重排 → 按实际几何校正；监听 viewport resize |
| 异步显示覆盖过滤结果 | reveal 任务晚于过滤执行 | 异步 show 前重新判定当前过滤条件 |
| offscreen 测不出的问题 | 无字体 / 无样式环境 | 布局类改动必须开真实应用验证 |

## 三、卡片 / 设置项

- 设置卡片改完注意回环：批量同步状态用 `blockSignals`，写盘加防抖（500ms 量级）。
- 跨窗口共享的设置项：销毁前确认同 key 还有存活窗口，别误杀共享刷新。
- 卡片尺寸记忆按 `card_id` 存，不要容器级单值（不同卡片互相覆盖）。
- 常驻 tab（插件 titlebar tab）在 `remove_tab` / 关闭路径要排除，见 `known-pitfalls.md` §六。

## 四、自检清单

- [ ] 找到对应信号并连接（UI 横切事件走 `app/core/ui_event_bus.py`）
- [ ] 亮 / 暗切换不闪烁、不漏色、图标跟着换
- [ ] 后台线程没直接碰 widget
- [ ] 长列表 / 大消息实测不卡（有数字）
- [ ] 多窗口打开状态不串（工作台显隐 / 页签 / 工作路径）
- [ ] 真实应用环境跑一遍，不能只靠 offscreen 测试
