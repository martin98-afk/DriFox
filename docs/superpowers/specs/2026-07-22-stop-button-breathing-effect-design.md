# 停止按钮呼吸动效设计

> 2026-07-22 | 状态: 待实现

## 概述

当前发送按钮在 AI 响应期间切换为停止按钮（`FluentIcon.PAUSE`），样式单调。本设计将其替换为一个带**缩放呼吸**动效的自绘 SVG 风格按钮，提升交互品质。

## 设计目标

- 停止按钮不再是静态图标，而是有生命感的呼吸动效
- 与现有发送按钮的金色渐变背景融合，不破坏整体风格
- 深色/浅色主题自动适配
- 性能轻量，不阻塞 UI 线程

## 视觉效果

### 动效：缩放呼吸

实心方块自身微微放大缩小，模拟呼吸起伏：

| 参数 | 值 |
|---|---|
| 缩放幅度 | ~12%（相对按钮尺寸的 ±6%） |
| 呼吸周期 | 2.5s 完整一呼一吸 |
| 圆角变化 | rx 随缩放同步微变（5px ↔ 7px） |
| 缓动曲线 | 正弦缓动（smooth in-out） |

### 配色方案

| 主题 | 方块颜色 | 按钮背景 |
|---|---|---|
| 🌙 深色 | `#FFFFFF` 白色 | 金色渐变（与发送按钮一致） |
| ☀️ 浅色 | `#C0392B` 深红 | 浅灰色背景 `#f0f0f0` |

### 交互

- 点击按钮 → 触发停止逻辑（与现有行为一致）
- hover 时背景色加深（保持与发送按钮一致的 hover 反馈）
- 动画仅在停止模式下运行，切回发送模式后停止

## 架构设计

```
┌─────────────────────────────────────────────┐
│              BottomInputArea                  │
│  ┌──────────────┐   ┌──────────────────────┐ │
│  │  SendButton   │ ←→ │ AnimatedStopButton  │ │
│  │ (Transparent  │   │  (QWidget 自绘动画)  │ │
│  │  ToolButton)  │   │                      │ │
│  └──────────────┘   └──────────────────────┘ │
│     发送模式             停止模式 (toggle)     │
└─────────────────────────────────────────────┘
```

### 新增文件

**`app/widgets/stop_button.py`**

```python
class AnimatedStopButton(QWidget):
    """缩放呼吸动效的停止按钮，QPainter 自绘"""

    clicked = pyqtSignal()

    - _anim_progress: float  # 0.0→1.0 动画进度
    - _timer: QTimer         # ~33ms 驱动刷新（≈30fps）
    - _color_scheme: dict    # 当前主题色
```

### 改动文件

**`app/widgets/bottom_input_area.py`**

- `toggle_send_button(enable: bool)`:
  - `enable=True` → 隐藏 AnimatedStopButton，显示 SendButton
  - `enable=False` → 隐藏 SendButton，创建/显示 AnimatedStopButton
- 停止按钮的定位 / 尺寸与现有发送按钮一致（34×34，右下角）

### 依赖

- `QPainter` / `QTimer` / `QWidget`（PyQt5 内置，无新增依赖）
- `theme_manager` 的 `theme_changed` 信号（已有）

## 动画循环

```
QTimer interval=33ms
  │
  ├─ _anim_progress += (1/76)  # 76帧 ≈ 2.5秒（2.5s÷33ms）
  │
  ├─ if _anim_progress > 1.0:
  │     _anim_progress -= 1.0
  │
  └─ update()  # 触发 paintEvent
```

### Paint 逻辑

```
paintEvent:
  1. 绘制圆形背景（渐变 / 纯色，取决于主题）
  2. 计算缩放因子 scale = 1.0 + 0.06 * sin(_anim_progress * 2π)
  3. 计算方块尺寸 = base_size * scale
  4. 计算圆角 rx = base_rx * scale
  5. 居中绘制实心方块
```

## 主题适配

监听 `theme_manager.theme_changed` 信号：

```python
def _on_theme_changed(self):
    Colors.refresh()
    is_light = theme_manager.is_light_theme()
    self._square_color = "#C0392B" if is_light else "#FFFFFF"
    self.update()
```

## 测试要点

- 按钮从发送模式切换到停止模式时动画正确启动
- 从停止模式切回发送模式时动画停止、资源释放
- 深色/浅色主题切换后颜色正确更新
- 缩放呼吸视觉效果流畅（无跳帧）
- 点击停止按钮正确触发停止逻辑
- 窗口 resize 后按钮位置正确

## 未涵盖（未来可能）

- 点击停止时的触感反馈动效（如按钮缩放弹跳）
- 自定义呼吸速度配置
- 其他呼吸动效风格（明暗呼吸、光晕呼吸等）
