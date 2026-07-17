# 插件图标支持设计

## 背景

当前 DriFox 插件系统在 UI 展示（插件管理卡片、快捷入口菜单）中使用 `SquircleAvatar`（缩写+哈希色）作为插件视觉标识。需要支持自定义插件图标，让插件拥有更具辨识度的视觉标识。

## 目标

1. 插件 manifest（`plugin.json`）支持声明图标
2. 图标支持深/浅双主题 SVG
3. 在插件管理列表、快捷入口菜单中显示图标
4. 无图标插件 fallback 到当前缩写头像
5. 为 6 个系统内置插件生成简约符号风 SVG 图标

## 设计

### 1. plugin.json icon 字段

```jsonc
// 单文件通用
{
  "icon": "icon.svg"
}

// 深浅分离（推荐）
{
  "icon": {
    "light": "icon.svg",
    "dark": "icon_dark.svg"
  }
}
```

**约定兜底**：manifest 无 `icon` 字段时，自动检测 `{plugin_root}/icon.svg`，存在即使用。

### 2. PluginInfo 扩展

`app/core/plugin_manager.py` 中 `PluginInfo` 新增属性：

```python
@property
def icon_config(self) -> Optional[dict]:
    """返回 {"light": Path|None, "dark": Path|None} 或 None"""
    raw = self.manifest.get("icon")
    if not raw:
        default = self.path / "icon.svg"
        return {"light": default, "dark": default} if default.exists() else None
    if isinstance(raw, str):
        p = self.path / raw
        return {"light": p, "dark": p} if p.exists() else None
    if isinstance(raw, dict):
        result = {}
        for theme in ("light", "dark"):
            path_str = raw.get(theme)
            if path_str:
                p = (self.path / path_str).resolve()
                if p.exists():
                    result[theme] = p
        # 单主题补齐
        if "light" not in result and "dark" in result:
            result["light"] = result["dark"]
        if "dark" not in result and "light" in result:
            result["dark"] = result["light"]
        return result if result else None
```

### 3. 图标显示组件

#### 3.1 插件内部组件（plugin-manager / plugin-marketplace）

各自在本地的 `_squircle_avatar.py` 中新增 `PluginIconWidget`：

```
PluginIconWidget(QWidget)
  ├─ 构造：接受 (plugin_dir, manifest, font_size, parent)
  ├─ 加载 SVG 路径（根据 isDarkTheme() 选择 light/dark）
  ├─ SVG 文件存在 → QSvgWidget 渲染
  ├─ 无 SVG → SquircleAvatar（缩写+哈希色 fallback）
  └─ 主题变化时自动切换
```

#### 3.2 系统组件（Edge Launcher）

`app/widgets/plugin_icon_widget.py` 中实现相同组件，供 `app/widgets/` 使用。

Edge Launcher 菜单：`QAction.setIcon(QIcon(svg_path))`，主题切换时重建菜单。

### 4. 主题切换策略

所有显示图标的地方，在 show / theme change 时重新检查 `isDarkTheme()`：
- `PluginIconWidget`：重绘时切换 SVG 源文件
- 菜单图标：`_open_menu()` 时根据当前主题选对应路径

### 5. 系统插件图标分配

| 插件名 | 符号设计思路 |
|---|---|
| `system` | 齿轮 ⚙ + 核心节点（系统核心） |
| `plugin-manager` | 拼图块 🧩（管理插件） |
| `plugin-marketplace` | 购物袋 🛍 + 下载箭头（市场） |
| `context-usage-stats` | 条形统计图 📊（用量统计） |
| `file-tree` | 文件树 🌳（目录结构） |
| `system-cleaner` | 扫帚 🧹 + 垃圾箱（清理） |

每个插件产出一对 SVG：`icon.svg`（浅色主题用色块+深色线稿）、`icon_dark.svg`（深色主题用暗底+亮色线稿）。

### 6. 边界情况

- 插件目录被删除 / SVG 文件不存在 → 回退 SquircleAvatar（完全无感降级）
- manifest 中 `icon` 路径不存在 → 同上级 fallback
- 市场插件未安装时，市场列表使用 `marketplace.json` 中的 `icon` URL
- 用户自定义插件未配 icon → fallback（无需强制要求）

## 修改文件清单

| 文件 | 改动 |
|---|---|
| `app/core/plugin_manager.py` | `PluginInfo.icon_config` 属性 |
| `app/widgets/plugin_icon_widget.py` | **新建**：系统级 PluginIconWidget |
| `app/widgets/ui_plugin_edge_launcher.py` | 菜单项设置 QIcon |
| `plugins/plugin-manager/ui/_squircle_avatar.py` | 新增 PluginIconWidget |
| `plugins/plugin-manager/ui/cards.py` | _PluginRow 使用 PluginIconWidget |
| `plugins/plugin-marketplace/ui/_squircle_avatar.py` | 新增 PluginIconWidget |
| `plugins/plugin-marketplace/ui/cards.py` | _PluginRow 使用 PluginIconWidget |
| `plugins/system/.drifox-plugin/plugin.json` | + icon 字段 |
| `plugins/plugin-manager/.drifox-plugin/plugin.json` | + icon 字段 |
| `plugins/plugin-marketplace/.drifox-plugin/plugin.json` | + icon 字段 |
| `plugins/context-usage-stats/.drifox-plugin/plugin.json` | + icon 字段 |
| `plugins/file-tree/.drifox-plugin/plugin.json` | + icon 字段 |
| `plugins/system-cleaner/.drifox-plugin/plugin.json` | + icon 字段 |
| `plugins/{name}/icon.svg` | 浅色主题 SVG（新建 ×6） |
| `plugins/{name}/icon_dark.svg` | 深色主题 SVG（新建 ×6） |
