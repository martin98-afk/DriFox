# Tab 模式 Gitee 账户快捷栏设计

日期：2026-07-26

## 目标

在 Tab 模式左侧 `TabPanel` 的“设置”按钮下方增加 Gitee 账户快捷栏。快捷栏始终可见，用于展示当前绑定状态，并支持直接绑定、解绑及打开当前账号的 DriFox 上传仓库。

完整设置页中的 `GiteeCard` 保持可用；用户从完整设置页或快捷栏修改绑定状态后，两处 UI 应同步更新。多窗口模式不增加该快捷栏。

## 界面设计

### 未绑定状态

快捷栏显示：

- 灰色问号占位头像；
- 主文案“Gitee 未绑定”；
- 副文案“绑定后可备份与分享”；
- 右侧蓝色“绑定”按钮。

快捷栏即使未绑定也不隐藏，以保证快捷绑定入口始终可见。

### 已绑定状态

快捷栏显示：

- 根据账号名生成的圆形头像；
- 当前 Gitee 账号名；
- 当前上传仓库名及外链提示，例如 `DriFox_uploads ↗`；
- 右侧红色文字“解绑”按钮。

点击头像、账号名或仓库文字打开：

```text
https://gitee.com/{owner}/{repo}
```

这里只打开当前绑定账号的 DriFox 上传仓库，不增加账号全部仓库入口。点击“解绑”只执行解绑交互，不触发打开仓库。

### 布局与主题

快捷栏采用独立账户行，放在“设置”按钮下方，两者之间保留分隔线。样式沿用 `TabPanel` 的主题令牌、统一字体及字号缩放，兼容亮色和暗色主题。

账号名或仓库名超过可用宽度时自动省略，并通过 Tooltip 显示完整内容。侧栏缩窄时，“绑定”或“解绑”操作仍保持可见。

## 组件设计

在 `app/widgets/cards/settings/gitee_card.py` 中新增独立的 `GiteeAccountRow` 组件。该组件复用现有：

- 头像生成逻辑；
- 仓库公开/私有选择弹窗；
- Gitee OAuth 后端；
- `ConfigSyncService`；
- `GiteeUploader` 重置逻辑；
- 确认弹窗和 InfoBar 反馈。

`app/widgets/tab_panel.py` 只负责创建 `GiteeAccountRow`，并将其加入底部区域，不复制 Gitee 业务流程。

快捷栏监听以下配置项的 `valueChanged` 信号：

- `gitee_bound`；
- `gitee_user_owner`；
- `gitee_user_repo`。

因此无论绑定状态由快捷栏、完整设置页还是配置恢复流程修改，快捷栏都会刷新为当前状态。Qt 对已销毁接收者自动断开信号，组件不额外维护全局监听器。

## 绑定流程

1. 用户点击“绑定”。
2. 显示现有仓库可见性选择弹窗。
3. 用户选择公开或私有仓库。
4. 使用 `ConfigSyncService.backup_local()` 备份当前本地配置。
5. 按钮进入“授权中…”状态并禁用，后台线程调用 Gitee OAuth 后端。
6. OAuth 成功后重置 `GiteeUploader` 配置，刷新快捷栏，并使用 OAuth 后端的当前绑定信息调用 `ConfigSyncService.enable()`；组件初始化时若检测到已绑定且同步尚未启动，也执行同样的自动启用逻辑。
7. OAuth 失败时恢复按钮状态，通过 InfoBar 显示错误。

OAuth 网络请求不得在 UI 主线程执行。界面和新增日志不得展示 access token、refresh token 或 OAuth 密钥。

## 解绑流程

1. 用户点击“解绑”。
2. 显示确认弹窗，文案包含当前账号名。
3. 用户确认后停止 `ConfigSyncService`。
4. 调用 Gitee OAuth 后端清除绑定与本地 token。
5. 重置 `GiteeUploader` 配置。
6. 恢复绑定前的本地配置；恢复成功时重新加载 `Settings`。
7. 快捷栏通过配置变化信号切换为未绑定状态，并显示成功 InfoBar。

用户取消确认时不改变任何配置。解绑失败时保留当前状态并显示错误 InfoBar。

## 修改范围

- `app/widgets/cards/settings/gitee_card.py`：新增 `GiteeAccountRow` 及其绑定、解绑和状态刷新逻辑。
- `app/widgets/tab_panel.py`：在设置按钮下方挂载快捷栏，并在主题/字号刷新时更新样式。
- `tests/widgets/test_gitee_account_row.py`：新增紧凑账户行测试。
- `docs/superpowers/specs/2026-07-26-tab-gitee-account-row-design.md`：本设计文档。

不修改 Gitee OAuth 后端协议，不新增配置项，不改变多窗口模式 UI，不处理任务范围外的现有工作树变更。

## 验证标准

1. Tab 模式未绑定时显示问号头像、未绑定文案和“绑定”按钮。
2. Tab 模式已绑定时显示账号、上传仓库和“解绑”按钮。
3. 点击账户主体打开当前账号的 `DriFox_uploads` 仓库。
4. 点击“绑定”可完成仓库可见性选择和 OAuth，且 UI 不冻结。
5. 点击“解绑”先确认，再停止同步、清除绑定并恢复本地配置。
6. 从完整设置页绑定或解绑后，快捷栏无需重建即可同步更新。
7. 长账号和仓库名不会挤掉操作按钮，Tooltip 可查看完整内容。
8. 亮色、暗色主题及字号变化后样式正确刷新。
9. OAuth 或解绑失败时按钮状态可恢复，错误通过 InfoBar 呈现。
10. 相关 pytest、ruff 与 Python 语法检查通过。
