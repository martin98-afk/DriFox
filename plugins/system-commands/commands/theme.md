---
description: 生成新主题颜色
type: prompt
---
首先加载 brainstorming 技能，然后以 DriFox 的 AI 主题设计师身份进行工作。你的任务是基于当前软件的主题系统，生成一套全新的、视觉上和谐统一的深色（或浅色）主题颜色方案，并保存为用户主题。

DriFox 的主题系统基于 YAML 文件，每个主题包含 `name`、`id`、`mode`、`window`、`background`、`colors` 等顶层字段。颜色值使用 hex (`#rrggbb`) 或 rgba 格式。

以下是完整的主题结构说明（==代表你需要生成的值）：

---
## 主题结构

基本信息
```yaml
name: 主题显示名（中文，如"紫罗兰"）
id: 主题唯一 ID（小写英文，如 "violet"）
mode: dark     # 可选 light/dark，不写则自动检测 text_primary 亮度
```

### pet（像素宠物图片，可选）
主题可以自定义像素宠物形象（在对话中显示的助手头像角色）：

```yaml
pet:
  image: pet.png    # 放在主题文件夹中的图片，相对路径
```

将图片放入主题文件夹即可：
```
~/.drifox/plugins/user-custom/themes/{theme_id}/
├── {theme_id}.yaml
└── pet.png          # 你的像素宠物图片
```

如果不声明 `pet` 段，则使用内嵌默认的像素宠物形象。

### window（窗口渐变背景）
- `gradient_start` / `gradient_end`: 窗口左上到右下的线性渐变，两个 rgba(...,255) 颜色

### background（背景图片，可选）
**背景图片可自定义，也可使用内置默认图片。**

默认使用内置图片（无需创建文件）：
```yaml
background:
  chat_list:            # 消息列表背景（推荐）
    image: :/icons/fox_bg.png
    opacity: 0.1
    enabled: true
```

如果用户想自定义背景图片：
1. 将图片放入主题文件夹（相对路径引用）：
   ```
   ~/.drifox/plugins/user-custom/themes/{theme_id}/
   ├── {theme_id}.yaml
   └── user_bg.png        # 你的背景图片
   ```
2. 在 YAML 中引用：
   ```yaml
   background:
     chat_list:
       image: user_bg.png
       opacity: 0.15      # 可调整透明度
       enabled: true
   ```
3. 如果不想用背景图片：`enabled: false`

### colors（颜色系统）

#### 1. 基础色（由 accent 主导整个色彩方向）
- `accent`: **核心强调色**，是整个主题的"灵魂色"。选择一个饱和度适中、有辨识度的颜色（如金色、青色、玫红、翠绿等），所有其它颜色围绕它派生
- `accent_warm`: 暖色强调，通常偏橙黄，与 accent 互补
- `border`: 边框色，比背景稍亮，通常比 accent 暗许多
- `border_accent`: 带强调色的边框，在 accent 的基础上降低饱和度/提亮
- `text_primary`: 主文字色，深色主题接近白色 `#f3f6fc`，浅色主题接近黑色
- `text_secondary`: 次要文字，白色带透明度 `rgba(..., ..., ..., 0.7x)` 或黑色带透明度
- `text_muted`: 弱化文字，较暗的灰色 `#xxxxxx` hex
- `card_bg`: 卡片背景色 `rgba(r, g, b, 230)` 半透明
- `card_bg_solid`: 卡片实色背景 `rgba(r, g, b, 250)`
- `content_bg`: 内容区纯色背景 `#xxxxxx`
- `hover_bg`: 悬停高亮半透明层 `rgba(r, g, b, 0.12)` — r,g,b 使用 accent 的颜色
- `selected_bg`: 选中高亮半透明层 `rgba(r, g, b, 0.32)` — 同上但透明度更高
- `capsule_bg`: 胶囊（标签）背景 `rgba(r, g, b, 180)`
- `capsule_border`: 胶囊边框 `rgba(r, g, b, 200)`

#### 2. 全局 UI 基底（窗口级通用色）
- `toolbar_bg`: 工具栏背景半透明层 `rgba(255, 255, 255, 0.05)`
- `divider_color`: 分割线色 `rgba(255, 255, 255, 0.06)`
- `hover_bg_strong`: 强悬停高亮（比 hover_bg 更明显）`rgba(255, 255, 255, 0.10)`
- `scrollbar_handle_bg`: 滚动条滑块色 `rgba(255, 255, 255, 0.20)`
- `scrollbar_handle_hover_bg`: 滚动条滑块悬停色 `rgba(255, 255, 255, 0.30)`
- `card_placeholder_text`: 卡片占位文字色（如空状态提示）`#8FA4C2`

#### 3. 卡片级语义色（按钮、状态、归档等）
- `button_text_on_accent`: 强调色按钮上的文字色（通常为深色 `#1A1F2B` 确保 contrast）
- `status_info`: 信息提示色 `#7FDBFF`
- `status_danger_bg`: 危险/删除状态背景 `rgba(255, 80, 80, 0.8)`
- `status_archive_bg`: 归档状态背景 `rgba(139, 92, 246, 0.8)`
- `card_bg_dim`: 极淡的卡片背景区分 `rgba(255, 255, 255, 0.04)`
- `archived_card_bg`: 归档卡片背景 `rgba(255, 180, 100, 0.08)`
- `archived_card_border`: 归档卡片边框 `rgba(255, 150, 80, 0.2)`

#### 4. 语法高亮色（消息内容中的代码高亮）
- `syntax_step`: 步骤关键词高亮 `#4EC9B0`
- `syntax_tool`: 工具名高亮 `#DCDCAA`
- `syntax_success`: 成功状态高亮 `#6A9955`
- `syntax_error`: 错误状态高亮 `#F14C4C`
- `syntax_result`: 结果值高亮 `#CE9178`

#### 5. 标签色（对话标签、分类标记）
- `tag_accent`: 标签主色（通常与 accent 同色系但更亮）
- `tag_accent_text`: 标签文字色
- `tag_purple`: 紫色标签 `#b388ff`
- `tag_purple_text`: 紫色标签文字 `#d1b3ff`
- `tag_orange`: 橙色标签 `#ffb366`
- `tag_orange_text`: 橙色标签文字 `#ffc999`

#### 6. 用户 / AI 卡片色（区分对话双方）
- `user_card_bg`: 用户消息卡片半透明背景 `rgba(..., ..., ..., 150)` — 偏蓝色调
- `user_card_accent`: 用户卡片强调色（较亮的蓝色系）
- `user_card_text`: 用户卡片文字（亮白）
- `user_card_muted`: 用户卡片次要文字
- `assistant_card_bg`: AI 消息卡片半透明背景 `rgba(..., ..., ..., 150)` — 偏暖色调
- `assistant_card_accent`: AI 卡片强调色（暖色，如橙/金）
- `assistant_card_text`: AI 卡片文字（暖白）
- `assistant_card_muted`: AI 卡片次要文字

#### 7. 智能体按钮
- `agent_btn_text`: 默认文字色（偏灰）
- `agent_btn_text_active`: 激活文字色（使用 accent 或同类亮色）
- `agent_btn_bg_active`: 激活背景 `rgba(accent_r, accent_g, accent_b, 0.2)`
- `agent_btn_separator`: 分隔线 `rgba(r, g, b, 150)`

#### 8. 输入框
- `input_bg_start` / `input_bg_end`: 输入框渐变背景（默认状态），较深 rgba(..., ..., ..., 150)
- `input_focus_bg_start` / `input_focus_bg_end`: 聚焦状态渐变背景，稍亮 rgba(..., ..., ..., 220)
- `input_text`: 默认文字色
- `input_focus_text`: 聚焦文字色（略亮）
- `input_border`: 默认边框色（暗）
- `input_focus_border`: 聚焦边框色（使用 accent）
- `input_placeholder`: 占位文字色 `rgba(r, g, b, 0.4)`

#### 8.5 输入框发光（halo cascade + glow preset）

输入框聚焦时会沿整个胶囊轮廓一次性绘制两层向内发光（halo cascade），可独立调色 + 强度：

- `input_focus_border`: 聚焦边框 + 发光色，halo 自动跟随此色
- `input_glow_primary_alpha` / `input_glow_primary_blur`: 主光（紧致核心），alpha 高、blur 小；设为 0 即关闭
- `input_glow_ambient_alpha` / `input_glow_ambient_blur`: 环境光（弥散底层），alpha 较低、blur 较大
- `input_glow_unfocused_ambient_alpha` / `input_glow_unfocused_ambient_blur`: 失焦态环境光；默认 0=失焦完全关闭，调大则保留"持续呼吸"的微光

此外，还支持**全局 UI 发光层级**（较老的 halo 系统，与 input_glow 并行生效）：
- `glow_primary_alpha` / `glow_primary_blur`: 主光源（输入卡等活跃控件），alpha ~195、blur ~26
- `glow_ambient_alpha` / `glow_ambient_blur`: 环境光晕（工具栏等次级区域），alpha ~110、blur ~36

> 这两个系统叠加工作：`input_glow_*` 控制输入卡本身的发光，`glow_*` 控制工具栏等周边区域的光效。颜色都跟随 `input_focus_border`。

##### 8.5.1 发光预设（推荐写法）

主题最简洁的做法是直接指定 `input_glow_preset`，由 DriFox 一次性填回 6 个 `input_glow_*` 强度 token。**预设只控制发光强度，不管颜色** —— 颜色由本主题的 `input_focus_border` 决定。这样主题可以自由组合"颜色 + 强度"，例如辐射绿 + ember 强度。

预设有 4 档：

| 预设 | 风格（强度） | 适用 |
|------|------|------|
| `subtle`   | halo 最克制（聚焦 35/18，失焦 0），失焦完全关 | 追求低调 |
| `breath`   | halo 中等（聚焦 65/30，失焦 38/30），失焦保留微光 | 奢华感最强（"由弱到强"焦点切换） |
| `platinum` | halo 适中（聚焦 55/26，失焦 0），失焦关 | 冷峻现代 |
| `ember`    | halo 偏强（聚焦 70/30，失焦 18/20），四档中最亮 | 喜欢高调观感 |

写法示例：
```yaml
colors:
  input_focus_border: "#35f78a"   # 辐射绿（颜色由主题决定）
  input_glow_preset: "ember"      # ember 强度
```

> 💡 预设**不会**覆盖 `input_focus_border`，只覆盖 6 个 `input_glow_*` token。如需在预设基础上微调某个强度参数，请先复制预设的 6 个数值到对应 token 后**删除** `input_glow_preset` 行。

#### 9. 底部工具栏条（输入卡片下方的工具栏区域）
- `toolbar_strip_bg`: 工具栏背景 `rgba(r, g, b, 150)`
- `toolbar_strip_border`: 工具栏边框色

#### 10. 实时交互卡片
- `realtime_border`: 边框（用 accent 或相近色）
- `realtime_accent`: 强调色（accent 的亮化版）
- `realtime_accent_warm`: 暖色强调
- `realtime_success`: 成功绿（保持 `#34d399` 或类似）
- `realtime_error`: 错误红（保持 `#f87171` 或类似）
- `realtime_bg`: 背景 `rgba(r, g, b, 242)`
- `realtime_text`: 文字
- `realtime_text_secondary`: 次要文字 `rgba(r, g, b, 0.7)`
- `realtime_tag_bg`: 标签背景 `rgba(r, g, b, 0.15)` — r,g,b 来自 realtime_accent
- `realtime_tag_border`: 标签边框 `rgba(r, g, b, 0.3)`

#### 11. 系统卡片
- `system_border`: 系统卡片边框色
- `system_accent`: 系统卡片强调色

#### 12. 发送按钮渐变
- `send_btn_start` / `send_btn_end`: 正常状态渐变（使用 accent 及其变体）
- `send_btn_hover_start` / `send_btn_hover_end`: 悬停状态渐变（略亮）
- `send_btn_radius`: 按钮圆角半径（像素），如 `8` 或 `17`

#### 13. 时间线
- `timeline_node`: 节点默认色 `#5A5A5A`
- `timeline_node_hover`: 节点悬停（用 accent 或亮色）
- `timeline_node_visible`: 可见节点（用亮绿色系或 accent）
- `timeline_node_selected`: 选中节点 `#FFA500`（通常保持橙色）
- `timeline_line`: 连线默认 `#3A3A3A`
- `timeline_line_progress`: 进度连线（用 visible 相同颜色）

#### 14. 上下文圆环
- `ring_normal`: 正常色（用 accent 或类似）
- `ring_warning`: 警告黄 `#f6c453`
- `ring_danger`: 危险红 `#ff6b6b`
- `ring_compacted`: 压缩紫 `#9b59b6`

#### 15. 分支标签
- `branch_label_bg`: `rgba(accent_r, accent_g, accent_b, 0.15)`
- `branch_label_border`: `rgba(accent_r, accent_g, accent_b, 0.3)`
- `window_bg`: `rgba(accent_r, accent_g, accent_b, 0.04)`

---

## 设计原则
1. **色彩统一**: 选择一个主色调（accent），所有颜色围绕它派生产生，确保视觉一致性
2. **用户卡片偏冷色调**（蓝色系），AI 卡片**偏暖色调**（橙/金色系）— 这个对比结构请保持
3. **深色/浅色模式**：深色背景、亮色文字；或浅色背景、深色文字。rgba 半透明值用于卡片分层。浅色主题需显式声明 `mode: light`
4. **可读性优先**: text_primary 为最亮/最暗色，text_secondary 适当降低透明度，确保对比度足够
5. **和谐过渡**: 各颜色之间的过渡平滑，避免突兀的色块

## 输出要求

1. 生成一个完整的 YAML 主题文件，包含以上所有字段
2. 保存主题文件：放到用户主题目录，路径为 `~/.drifox/plugins/user-custom/themes/{theme_id}/{theme_id}.yaml`
   - 先创建目录 `~/.drifox/plugins/user-custom/themes/{theme_id}/`
   - 再写入 YAML 文件
   - 如果目录已存在，覆盖写入
3. 主题名称要有创意且有意义，围绕一个色彩主题（如"紫罗兰"、"琥珀光"、"极光绿"等）
4. 完成后告知用户：主题已保存，在设置中选择该主题即可生效
