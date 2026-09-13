---
description: Themes（主题配色）开发指南——文件位置、最小模板、关键约束、排障与样例
---

# Themes（主题配色）组件开发

### 文件位置

```
<plugin>/themes/<name>/*.yaml
```

### 模板

```yaml
# <theme-name>.yaml
name: "<theme-name>"
description: "主题描述"
type: "dark"  # dark / light

colors:
  window_bg: "#1e1e2e"
  card_bg: "#2a2a3e"
  text_primary: "#cdd6f4"
  text_secondary: "#a6adc8"
  accent: "#89b4fa"
  border: "#313244"
  button_bg: "#3a3a4e"
  button_text: "#cdd6f4"
  success: "#a6e3a1"
  warning: "#f9e2af"
  error: "#f38ba8"
```

### 颜色 Token 说明

Token 定义取决于 DriFox 主题系统支持的字段。参考现有主题了解完整 token 列表。

### 参考

- 完整规范：[docs/themes.md](https://github.com/martin98-afk/drifox-plugins/blob/main/docs/themes.md)
- 系统案例：`plugins/system-themes/themes/`（11 个主题）

---
