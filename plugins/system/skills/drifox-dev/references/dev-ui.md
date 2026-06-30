# UI 组件开发要点

> SKILL.md 在任务涉及「卡片/消息/设置 UI、PyQt 组件、主题」时分派到这里。

---

## 一、核心规则

| 要点 | 说明 |
|------|------|
| **继承 QObject** | 需要信号 / 槽的类必须继承 QObject |
| **信号定义** | 后端定义 → 前端在 `setup_ui` 中连接 |
| **线程安全** | UI 操作必须在主线程；后台用信号通知 UI |
| **卡片系统** | 设置类 UI 用 `widgets/cards/` |
| **设计令牌** | 使用 `app/utils/design_tokens.py` |
| **主题兼容** | 支持亮 / 暗切换，**不硬编码颜色** |
| **懒渲染** | 大量卡片时用 BATCH_SIZE=3 + 60ms 让 Chromium 喘息 |
| **WebEngine** | 用 `get_shared_web_profile()` 共享 Chromium 进程池，**不要**为每卡片创建 transient profile |

## 二、典型坑（避开）

| 坑点 | 触发条件 | 规避 |
|------|---------|------|
| 流式文本渲染慢 | `append_text` 永远走 `_schedule_render(immediate=False)` | 大块文本 (>3 字符) 且定时器未激活时 `immediate=True` |
| 消息卡片加载慢 | 每卡片独立 WebEngine profile | 用 `get_shared_web_profile()` 共享 |
| 跨线程 UI 卡死 | 后台线程直接操作 widget | 改用 signal → 主线程 slot |
| `setHtml` 中文乱码 | 使用 `QString` 默认编码 | `setHtml(html, baseUrl)` 显式指定 UTF-8 |

## 三、自检清单（修改 UI 后跑一遍）

- [ ] 在 `ui/engine.py` 找到对应信号，没漏连接
- [ ] 新 widget 主题切换不闪烁 / 不漏色
- [ ] 后台线程没有直接调用 `widget.xxx()`，都走信号
- [ ] 长消息输入验证流式渲染不会卡顿
