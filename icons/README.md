# 界面图标

主程序和随附插件使用 Lucide 0.545.0 的 24 × 24 图形。深色主题用白色图标，浅色主题用 `#333333`。应用、托盘和安装程序继续使用原版 DriFox ICO。许可见 [Lucide 声明](Lucide-NOTICE.txt)。

主程序的 Qt 控件使用 `get_icon("复制")`，Fluent 控件使用 `AppIcon.COPY`。两者读取同一份 SVG。需要 Fluent 控件按状态着色的图标使用填充路径表达描边轮廓。

修改本目录的 SVG 后，在仓库根目录运行 `python generate_icon_qrc.py`，同步浅色变体、资源映射和 Qt 编译资源。独立分发的插件在清单中通过 `icon.light` 和 `icon.dark` 指定随包携带的 SVG。

插件在清单中声明 `ui.icons`，通过能力池读取 Fluent 图标集，支持 `.qicon()` 和按主题着色。能力不可用时可回退到 qfluentwidgets 的 `FluentIcon`。
