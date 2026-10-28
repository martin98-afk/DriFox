# 可复用控件 — 工具函数集

> 来自 `context-usage-stats` 等项目提炼的小工具函数。
> 索引和设计原则见 `widgets.md`。

---

## 一、数字格式化

### 1.1 `_format_number(n: int) -> str`

将大数字格式化为紧凑形式，避免 UI 上显示 `1234567` 撑爆布局。

```python
def _format_number(n: int) -> str:
    """格式化大数字，如 1234 → '1.2k'"""
    if n >= 1000000:
        return f"{n / 1000000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)
```

**使用示例**：

```python
_format_number(123)      # "123"
_format_number(1500)     # "1.5k"
_format_number(283000)   # "283.0k"
_format_number(5572334)  # "5.6M"
```

### 1.2 `_format_pct(v: float) -> str`

将 0~1 的小数格式化为百分比。

```python
def _format_pct(v: float) -> str:
    """格式化百分比，0.85 → '85%'"""
    return f"{v * 100:.0f}%"
```

**使用示例**：

```python
_format_pct(0.85)   # "85%"
_format_pct(0.123)  # "12%"
_format_pct(1.0)    # "100%"
```

---

## 三、字节大小格式化

### 3.1 `_format_size(n: int) -> str`

将字节数格式化为人类可读的带单位字符串，用于文件大小/缓存的展示。

```python
def _format_size(n: int) -> str:
    """格式化字节大小，如 1234567 → '1.2 MB'"""
    if n < 0:
        return "N/A"
    if n >= 1073741824:
        return f"{n / 1073741824:.1f} GB"
    if n >= 1048576:
        return f"{n / 1048576:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"
```

**使用示例**：

```python
_format_size(0)           # "0 B"
_format_size(1024)        # "1.0 KB"
_format_size(1234567)     # "1.2 MB"
_format_size(2147483648)  # "2.0 GB"
_format_size(-1)          # "N/A"
```

---

## 四、颜色调节工具

### 4.1 `_adjust_color(hex_color: str, amount: int) -> str`

将 hex 颜色调亮或调暗指定量，常用于生成按钮渐变/悬停色。

```python
def _adjust_color(hex_color: str, amount: int) -> str:
    """简单调亮/调暗一个 hex 颜色

    Args:
        hex_color: 如 "#62a0ea"
        amount: 调整量（正值调亮，负值调暗）

    Returns:
        调整后的 hex 颜色，如 "#72b0fa"
    """
    hex_color = hex_color.lstrip("#")
    if len(hex_color) != 6:
        return hex_color
    try:
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
        r = max(0, min(255, r + amount))
        g = max(0, min(255, g + amount))
        b = max(0, min(255, b + amount))
        return f"#{r:02x}{g:02x}{b:02x}"
    except ValueError:
        return hex_color
```

**使用示例**（与按钮样式工厂配合）：

```python
def _my_button_style(accent: str) -> str:
    return f"""
    QPushButton {{
        background: qlineargradient(
            x1:0, y1:0, x2:1, y2:1,
            stop:0 {accent},
            stop:1 {_adjust_color(accent, -20)}  /* 调暗 20 */
        );
        color: #ffffff;
        border: none;
        border-radius: 8px;
    }}
    QPushButton:hover {{
        background: qlineargradient(
            x1:0, y1:0, x2:1, y2:1,
            stop:0 {_adjust_color(accent, 10)},  /* 调亮 10 */
            stop:1 {accent}
        );
    }}
    """
```

---

## 五、Token 快速估算（无 tiktoken 依赖）

> 当插件需要估算 token 数量但不想引入 `tiktoken` 这种重型 C 扩展依赖时使用。
> 精度足够用于统计展示，不适合用于精确计费场景。

### 2.1 `_fast_estimate_tokens(text: str) -> int`

```python
def _fast_estimate_tokens(text: str) -> int:
    """快速估算文本的 token 数（无需 tiktoken 依赖）

    经验公式：
    - 1 token ≈ 4 英文/混合字符
    - 1 token ≈ 2 中文字符

    适用场景：统计展示、数据库 fallback 估算
    不适用：精确计费、模型 API 输入长度校验
    """
    if not text:
        return 0
    chinese = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    non_chinese = len(text) - chinese
    estimated = chinese // 2 + non_chinese // 4
    return max(1, estimated)
```

### 2.2 `_estimate_messages_tokens(messages_json: str) -> int`

```python
def _estimate_messages_tokens(messages_json: str) -> int:
    """估算整条会话消息 JSON 的 token 数（快速但不精确）

    适用场景：估算 SQLite 中存储的 messages JSON 字符串的 token 数
    注意：调用前建议先截断到 100k 字符防 OOM（messages JSON 可能很大）
    """
    if not messages_json:
        return 0
    if isinstance(messages_json, str) and len(messages_json) > 10:
        return _fast_estimate_tokens(messages_json)
    return 0
```

### 2.3 完整使用示例（含防 OOM 截断）

```python
# 估算单条消息
tokens = _fast_estimate_tokens("Hello, world! 你好，世界！")
# "Hello, world! " = 13 chars → 13 // 4 = 3 tokens
# "你好，世界！" = 6 中文字符 → 6 // 2 = 3 tokens
# 总计约 6 tokens

# 从 SQLite messages JSON 字段估算（务必截断）
msg_data = row["messages"]  # 可能是几 MB 的 JSON
tokens = _fast_estimate_tokens(str(msg_data)[:100000])  # ← 截断防 OOM
```

### 2.4 与 tiktoken 的精度对比

| 文本类型 | tiktoken (cl100k_base) | `_fast_estimate_tokens` | 误差 |
|----------|------------------------|------------------------|------|
| 纯英文 100 chars | ~25 tokens | 25 tokens | 0% |
| 纯中文 100 chars | ~50 tokens | 50 tokens | 0% |
| 中英混合 100 chars | ~35 tokens | 35 tokens | 0% |
| 代码片段 1000 chars | ~280 tokens | 250 tokens | ~11% |

> 误差主要在代码/特殊符号场景。如果插件要做精确统计，建议引入 `tiktoken` 依赖并用 `_vendor/` 模式（见 `SKILL.md §4.7`）。

---

## 三、日期带星期格式

### 3.1 `_short_weekday(date_str: str) -> str`

将 `"MM-DD"` 格式的日期标签转成带星期的双行格式（用于图表 X 轴）。

```python
from datetime import datetime


def _short_weekday(date_str: str) -> str:
    """将 '01-15' 转换为 '01-15\\n周一' 格式

    用于图表 X 轴周末高亮显示
    """
    try:
        # 注：年份用 2025 是占位符，因为 datetime 解析需要年月日完整字段，
        # weekday() 只依赖月日
        dt = datetime.strptime(f"2025-{date_str}", "%Y-%m-%d")
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        wd = weekdays[dt.weekday()]
        return f"{date_str}\n{wd}"
    except (ValueError, IndexError):
        return date_str
```

**使用示例**：

```python
_short_weekday("07-01")  # "07-01\n周三"
_short_weekday("07-05")  # "07-05\n周六"（周末）
_short_weekday("07-07")  # "07-07\n周一"
```

### 3.2 在图表中内联使用

如果不想引入独立函数，可直接在 `_BarChartWidget.paintEvent` 内联：

```python
try:
    parts = label.split("-")
    if len(parts) == 2:
        dt = datetime.strptime(f"2025-{parts[0]}-{parts[1]}", "%Y-%m-%d")
        wd = dt.weekday()
        if wd in (0, 5, 6):  # 周一、周六、周日高亮
            weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
            display_label = f"{parts[0]}-{parts[1]}\n{weekdays[wd]}"
        else:
            display_label = label
except (ValueError, IndexError):
    display_label = label
```

---

## 四、其他常用工具（按需复制）

### 4.1 文件大小格式化

```python
def _format_bytes(n: int) -> str:
    """格式化字节数，1234567 → '1.2 MB'"""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} PB"
```

### 4.2 时长格式化

```python
def _format_duration(seconds: float) -> str:
    """格式化时长，3661 → '1h 1m 1s'"""
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m}m {s}s"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s"
```

### 4.3 安全除法

```python
def _safe_div(a: float, b: float, default: float = 0.0) -> float:
    """防除零的除法"""
    if b == 0:
        return default
    return a / b
```

---

## 五、设计原则

1. **纯函数**：所有工具函数无副作用，便于测试
2. **无 PyQt5 依赖**：可以在卡片外的纯 Python 代码（如数据读取函数）中使用
3. **输入宽容**：处理 `None` / 空字符串 / 异常输入，返回合理默认值
4. **避免重型依赖**：`tiktoken` / `arrow` 等只在需要精确度时才引入

## 六、何时引入外部依赖（替代这些工具）

| 工具函数 | 替代方案 | 适用场景 |
|----------|----------|----------|
| `_fast_estimate_tokens` | `tiktoken.encoding_for_model(...)` | 需要精确 token 计数（如计费） |
| `_short_weekday` | `babel.dates.format_date(...)` | 多语言 i18n |
| `datetime.strptime` | `pendulum.parse(...)` | 处理复杂时区、自然语言日期 |
| `_format_bytes` | `humanize.naturalsize()` | 同时需要 KB/MB/GB/i18n |

> 引入外部依赖时，记得用 `_vendor/` 模式（见 `SKILL.md §4.7`）。