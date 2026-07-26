# Tab Mode Gitee Account Row Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an always-visible Gitee account row below the Tab mode Settings button, with direct bind, unbind, and DriFox upload repository actions.

**Architecture:** Add a focused `GiteeAccountRow` widget beside the existing `GiteeCard` so both interfaces reuse the same avatar, repository-visibility dialog, OAuth backend, config-sync service, uploader reset, confirmation dialog, and InfoBar patterns. The row observes the three existing Gitee `ConfigItem.valueChanged` signals, while `TabPanel` only owns placement and theme refresh.

**Tech Stack:** Python 3.14+, PyQt5, PyQt-Fluent-Widgets, pytest, pytest-qt, unittest.mock, ruff

---

## File Map

- Modify `app/widgets/cards/settings/gitee_card.py`: define the compact row, state rendering, OAuth actions, repository opening, unbind flow, and theme refresh.
- Modify `app/widgets/tab_panel.py`: place the compact row below Settings and forward theme/font refresh.
- Create `tests/widgets/test_gitee_account_row.py`: unit-test row rendering, config-driven updates, repository URL, OAuth state, and unbind behavior.
- Modify `tests/widgets/test_tab_panel.py`: verify TabPanel mounts the row in the intended bottom position without starting real account synchronization.
- Reference `docs/superpowers/specs/2026-07-26-tab-gitee-account-row-design.md`: approved behavior and acceptance criteria; no implementation edits are required unless implementation reveals a contradiction.

### Task 1: Render and synchronize the compact account row

**Files:**
- Modify: `app/widgets/cards/settings/gitee_card.py:15-20, 45-60, after _RepoVisibilityDialog`
- Create: `tests/widgets/test_gitee_account_row.py`

- [ ] **Step 1: Write the failing state-rendering tests**

Create `tests/widgets/test_gitee_account_row.py` with a signal-capable fake config so tests never read or write the real application config:

```python
# -*- coding: utf-8 -*-
"""Tab 模式 Gitee 账户快捷栏测试"""

from unittest.mock import MagicMock, patch

import pytest
from PyQt5.QtCore import QObject, pyqtSignal

from app.widgets.cards.settings.gitee_card import GiteeAccountRow


class _FakeConfigItem(QObject):
    valueChanged = pyqtSignal(object)

    def __init__(self, value):
        super().__init__()
        self._value = value

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        if value == self._value:
            return
        self._value = value
        self.valueChanged.emit(value)


class _FakeSettings:
    def __init__(self, *, bound=False, owner="", repo="DriFox_uploads"):
        self.gitee_bound = _FakeConfigItem(bound)
        self.gitee_user_owner = _FakeConfigItem(owner)
        self.gitee_user_repo = _FakeConfigItem(repo)
        self.load = MagicMock()


@pytest.fixture
def row_factory(qtbot):
    created = []

    def _create(*, bound=False, owner="", repo="DriFox_uploads"):
        cfg = _FakeSettings(bound=bound, owner=owner, repo=repo)
        sync_service = MagicMock()
        sync_service._state = "idle"
        with (
            patch(
                "app.widgets.cards.settings.gitee_card.Settings.get_instance",
                return_value=cfg,
            ),
            patch(
                "app.core.config_sync.ConfigSyncService.get_instance",
                return_value=sync_service,
            ),
        ):
            row = GiteeAccountRow()
        qtbot.addWidget(row)
        created.append(row)
        return row, cfg, sync_service

    return _create


def test_unbound_state_shows_bind_action(row_factory):
    row, _, _ = row_factory()

    assert row._name_label._full_text == "Gitee 未绑定"
    assert row._repo_label._full_text == "绑定后可备份与分享"
    assert row._action_btn.text() == "绑定"
    assert row._bound_owner == ""
    assert row._avatar.toolTip() == "未绑定"


def test_bound_state_shows_owner_and_repository(row_factory):
    row, _, _ = row_factory(bound=True, owner="martin98-afk")

    assert row._name_label._full_text == "martin98-afk"
    assert row._repo_label._full_text == "DriFox_uploads ↗"
    assert row._action_btn.text() == "解绑"
    assert row._bound_owner == "martin98-afk"
    assert row._bound_repo == "DriFox_uploads"
    assert row._name_label.toolTip() == "martin98-afk"
    assert row._repo_label.toolTip() == "DriFox_uploads"


def test_config_changes_refresh_existing_row(row_factory):
    row, cfg, _ = row_factory()

    cfg.gitee_user_owner.value = "new-owner"
    cfg.gitee_user_repo.value = "DriFox_uploads"
    cfg.gitee_bound.value = True

    assert row._name_label._full_text == "new-owner"
    assert row._repo_label._full_text == "DriFox_uploads ↗"
    assert row._action_btn.text() == "解绑"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
pytest tests/widgets/test_gitee_account_row.py -v
```

Expected: collection fails with `ImportError: cannot import name 'GiteeAccountRow'`.

- [ ] **Step 3: Add the compact widget and state rendering**

In `app/widgets/cards/settings/gitee_card.py`, extend the widget imports and add the eliding-label import:

```python
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout

from app.widgets.elided_label import _ElidedLabel
```

Add a clickable eliding label beside `_ClickableAvatar`:

```python
class _ClickableElidedLabel(_ElidedLabel):
    clicked = pyqtSignal()

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)
```

Add the following class after `_RepoVisibilityDialog` and before `GiteeCard`:

```python
class GiteeAccountRow(QFrame):
    """Tab 模式底部的紧凑 Gitee 账户快捷栏。"""

    oauthResult = pyqtSignal(bool, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("giteeAccountRow")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.cfg = Settings.get_instance()
        self._binding = False
        self._bound_owner = ""
        self._bound_repo = ""

        from app.core.config_sync import ConfigSyncService

        self._sync_svc = ConfigSyncService.get_instance()
        self._setup_ui()
        self._connect_config_signals()
        self._refresh_ui()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 5, 8, 7)
        layout.setSpacing(8)

        avatar_size = scale_font_size(28)
        self._avatar = _ClickableAvatar(self)
        self._avatar.setFixedSize(avatar_size, avatar_size)
        self._avatar.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._avatar)

        text_container = QVBoxLayout()
        text_container.setContentsMargins(0, 0, 0, 0)
        text_container.setSpacing(0)

        self._name_label = _ClickableElidedLabel("", self)
        self._repo_label = _ClickableElidedLabel("", self)
        text_container.addWidget(self._name_label)
        text_container.addWidget(self._repo_label)
        layout.addLayout(text_container, 1)

        self._action_btn = QPushButton("绑定", self)
        self._action_btn.setFixedWidth(scale_font_size(58))
        self._action_btn.setMinimumHeight(scale_font_size(28))
        self._action_btn.setCursor(Qt.PointingHandCursor)
        layout.addWidget(self._action_btn)

    def _connect_config_signals(self):
        for item in (
            self.cfg.gitee_bound,
            self.cfg.gitee_user_owner,
            self.cfg.gitee_user_repo,
        ):
            item.valueChanged.connect(self._refresh_ui)

    def _refresh_ui(self, _value=None):
        is_bound = bool(self.cfg.gitee_bound.value)
        owner = str(self.cfg.gitee_user_owner.value or "")
        repo = str(self.cfg.gitee_user_repo.value or "")
        avatar_size = scale_font_size(28)

        if is_bound and owner:
            self._bound_owner = owner
            self._bound_repo = repo
            self._avatar.setPixmap(_make_avatar_pixmap(owner, avatar_size))
            self._avatar.setToolTip(f"点击打开仓库 {owner}/{repo}")
            self._name_label.setText(owner)
            self._name_label.setToolTip(owner)
            self._repo_label.setText(f"{repo} ↗")
            self._repo_label.setToolTip(repo)
        else:
            self._bound_owner = ""
            self._bound_repo = ""
            self._avatar.setPixmap(_make_avatar_pixmap("?", avatar_size))
            self._avatar.setToolTip("未绑定")
            self._name_label.setText("Gitee 未绑定")
            self._name_label.setToolTip("Gitee 未绑定")
            self._repo_label.setText("绑定后可备份与分享")
            self._repo_label.setToolTip("绑定后可备份与分享")

        if self._binding:
            self._action_btn.setText("授权中…")
            self._action_btn.setEnabled(False)
        else:
            self._action_btn.setEnabled(True)
            self._action_btn.setText("解绑" if self._bound_owner else "绑定")
        self._apply_style()

    def _apply_style(self):
        self.setStyleSheet(f"""
            QFrame#giteeAccountRow {{
                background: transparent;
                border: none;
            }}
        """)
        self._name_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(12)}; font-weight: 600;"
        )
        self._repo_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(10)};"
        )
        if self._bound_owner:
            self._action_btn.setStyleSheet(f"""
                QPushButton {{
                    color: #fa5151;
                    background: transparent;
                    border: 1px solid #fa5151;
                    border-radius: 5px;
                    {font_size_css(11)}
                }}
                QPushButton:hover {{ background: rgba(250, 81, 81, 0.10); }}
            """)
        else:
            self._action_btn.setStyleSheet(f"""
                QPushButton {{
                    color: #ffffff;
                    background: {Colors.INFO};
                    border: none;
                    border-radius: 5px;
                    {font_size_css(11)}
                }}
                QPushButton:hover {{ background: {Colors.BORDER_ACCENT}; }}
                QPushButton:disabled {{ color: {Colors.TEXT_MUTED}; background: {Colors.HOVER_BG}; }}
            """)

    def refresh_style(self):
        """主题或字号变化后重建头像、尺寸和样式。"""
        avatar_size = scale_font_size(28)
        self._avatar.setFixedSize(avatar_size, avatar_size)
        self._action_btn.setFixedWidth(scale_font_size(58))
        self._action_btn.setMinimumHeight(scale_font_size(28))
        self._refresh_ui()
```

Do not connect the action or repository clicks yet; Task 2 adds them under failing interaction tests.

- [ ] **Step 4: Run the state-rendering tests**

Run:

```bash
pytest tests/widgets/test_gitee_account_row.py -v
```

Expected: 3 tests pass.

- [ ] **Step 5: Commit the state-rendering slice**

```bash
git add app/widgets/cards/settings/gitee_card.py tests/widgets/test_gitee_account_row.py
git commit -m "feat: gitee-account-row - add compact binding state"
```

### Task 2: Add repository, bind, and unbind actions

**Files:**
- Modify: `app/widgets/cards/settings/gitee_card.py:GiteeAccountRow`
- Modify: `tests/widgets/test_gitee_account_row.py`

- [ ] **Step 1: Add failing interaction tests**

Append these tests to `tests/widgets/test_gitee_account_row.py`:

```python
def test_clicking_account_opens_bound_repository(row_factory):
    row, _, _ = row_factory(bound=True, owner="martin98-afk")

    with patch("app.widgets.cards.settings.gitee_card.webbrowser.open") as open_url:
        row._open_repository()

    open_url.assert_called_once_with("https://gitee.com/martin98-afk/DriFox_uploads")


def test_start_oauth_disables_button_and_starts_worker(row_factory):
    row, _, _ = row_factory()

    with patch("app.widgets.cards.settings.gitee_card.threading.Thread") as thread_cls:
        row._start_oauth(repo_private=True)

    assert row._binding is True
    assert row._action_btn.text() == "授权中…"
    assert row._action_btn.isEnabled() is False
    thread_cls.assert_called_once_with(target=row._do_oauth, args=(True,), daemon=True)
    thread_cls.return_value.start.assert_called_once_with()


def test_oauth_failure_restores_bind_action(row_factory):
    row, _, _ = row_factory()
    row._binding = True
    row._refresh_ui()

    with patch("app.widgets.cards.settings.gitee_card.InfoBar.error") as show_error:
        row._on_oauth_result(False, "授权超时")

    assert row._binding is False
    assert row._action_btn.isEnabled() is True
    assert row._action_btn.text() == "绑定"
    show_error.assert_called_once()


def test_unbind_requires_confirmation(row_factory):
    row, _, _ = row_factory(bound=True, owner="martin98-afk")
    dialog = MagicMock()

    with patch(
        "app.widgets.cards.settings.gitee_card.ConfirmDialog",
        return_value=dialog,
    ):
        row._on_unbind()

    dialog.confirmed.connect.assert_called_once_with(row._do_unbind)
    dialog.exec_.assert_called_once_with()


def test_do_unbind_stops_sync_and_resets_state(row_factory):
    row, cfg, sync_service = row_factory(bound=True, owner="martin98-afk")
    backend = MagicMock()
    uploader = MagicMock()

    def clear_binding():
        cfg.gitee_bound.value = False
        cfg.gitee_user_owner.value = ""
        cfg.gitee_user_repo.value = ""
        return True, "已解绑 Gitee 账号"

    backend.unbind.side_effect = clear_binding
    sync_service.restore_local.return_value = True

    with (
        patch("app.gateway.auth.get_oauth_backend", return_value=backend),
        patch(
            "app.gateway.utils.gitee_uploader.GiteeUploader.get_instance",
            return_value=uploader,
        ),
        patch("app.widgets.cards.settings.gitee_card.InfoBar.success"),
    ):
        row._do_unbind()

    sync_service.disable.assert_called_once_with()
    backend.unbind.assert_called_once_with()
    uploader.reset_config.assert_called_once_with()
    sync_service.restore_local.assert_called_once_with()
    cfg.load.assert_called_once_with()
    assert row._action_btn.text() == "绑定"


def test_unbind_failure_preserves_bound_state(row_factory):
    row, _, sync_service = row_factory(bound=True, owner="martin98-afk")
    backend = MagicMock()
    backend.unbind.return_value = False, "配置保存失败"

    with (
        patch("app.gateway.auth.get_oauth_backend", return_value=backend),
        patch("app.widgets.cards.settings.gitee_card.InfoBar.error") as show_error,
    ):
        row._do_unbind()

    sync_service.disable.assert_called_once_with()
    assert row._action_btn.text() == "解绑"
    assert row._bound_owner == "martin98-afk"
    show_error.assert_called_once()
```

- [ ] **Step 2: Run the interaction tests to verify they fail**

Run:

```bash
pytest tests/widgets/test_gitee_account_row.py -v
```

Expected: the three state tests pass and interaction tests fail because `GiteeAccountRow` has no action methods or signal connections.

- [ ] **Step 3: Connect clicks and implement the existing OAuth workflow**

At the end of `GiteeAccountRow._setup_ui()`, add:

```python
        self._avatar.setCursor(Qt.PointingHandCursor)
        self._name_label.setCursor(Qt.PointingHandCursor)
        self._repo_label.setCursor(Qt.PointingHandCursor)
        self._avatar.clicked.connect(self._open_repository)
        self._name_label.clicked.connect(self._open_repository)
        self._repo_label.clicked.connect(self._open_repository)
        self._action_btn.clicked.connect(self._on_action_clicked)
        self.oauthResult.connect(self._on_oauth_result)
```

At the end of the bound branch in `_refresh_ui()`, after setting the repository Tooltip, add:

```python
            self._auto_enable_sync()
```

Add these methods to `GiteeAccountRow` before `refresh_style()`:

```python
    def _open_repository(self):
        if self._bound_owner and self._bound_repo:
            webbrowser.open(f"https://gitee.com/{self._bound_owner}/{self._bound_repo}")

    def _on_action_clicked(self):
        if self.cfg.gitee_bound.value:
            self._on_unbind()
        else:
            self._on_bind()

    def _on_bind(self):
        if self._binding:
            return
        dialog = _RepoVisibilityDialog(self.window())
        dialog.chosen.connect(self._start_oauth_with_backup)
        dialog.exec_()

    def _start_oauth_with_backup(self, repo_private: bool):
        self._sync_svc.backup_local()
        self._start_oauth(repo_private)

    def _start_oauth(self, repo_private: bool):
        self._binding = True
        self._refresh_ui()
        worker = threading.Thread(
            target=self._do_oauth,
            args=(repo_private,),
            daemon=True,
        )
        worker.start()

    def _do_oauth(self, repo_private: bool):
        try:
            from app.gateway.auth import get_oauth_backend

            success, message = get_oauth_backend("gitee").bind(
                repo_private=repo_private,
            )
            self.oauthResult.emit(success, message)
        except Exception as error:
            logger.error(f"[GiteeAccountRow] OAuth 异常: {error}")
            self.oauthResult.emit(False, f"绑定异常：{error}")

    def _on_oauth_result(self, success: bool, message: str):
        self._binding = False
        if success:
            from app.gateway.utils.gitee_uploader import GiteeUploader

            GiteeUploader.get_instance().reset_config()
            self._refresh_ui()
            InfoBar.success(
                title="绑定成功",
                content=message,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
                parent=self.window(),
            )
            return

        self._refresh_ui()
        InfoBar.error(
            title="绑定失败",
            content=message,
            position=InfoBarPosition.TOP_RIGHT,
            duration=5000,
            parent=self.window(),
        )

    def _auto_enable_sync(self):
        if self._sync_svc._state != "disabled":
            return
        from app.gateway.auth import get_oauth_backend

        bound_info = get_oauth_backend("gitee").get_bound_info()
        if bound_info and bound_info.get("token") and bound_info.get("owner"):
            logger.info("[GiteeAccountRow] 检测到已绑定，自动启动配置同步")
            self._sync_svc.enable(bound_info["token"], bound_info["owner"])

    def _on_unbind(self):
        owner = str(self.cfg.gitee_user_owner.value or "")
        dialog = ConfirmDialog(
            title="确认解绑",
            content=f"解绑后上传将恢复使用共享图床仓库。\n当前绑定：{owner}",
            confirm_text="确定解绑",
            cancel_text="取消",
            parent=self.window(),
        )
        dialog.confirmed.connect(self._do_unbind)
        dialog.exec_()

    def _do_unbind(self):
        try:
            from app.gateway.auth import get_oauth_backend
            from app.gateway.utils.gitee_uploader import GiteeUploader

            self._sync_svc.disable()
            success, message = get_oauth_backend("gitee").unbind()
            if not success:
                raise RuntimeError(message)
            GiteeUploader.get_instance().reset_config()

            if self._sync_svc.restore_local():
                self.cfg.load()

            self._refresh_ui()
            InfoBar.success(
                title="已解绑",
                content="Gitee 账号已解绑，配置已恢复",
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
                parent=self.window(),
            )
        except Exception as error:
            logger.error(f"[GiteeAccountRow] 解绑异常: {error}")
            self._refresh_ui()
            InfoBar.error(
                title="解绑失败",
                content=str(error),
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
                parent=self.window(),
            )
```

Keep all network-bound OAuth work inside `_do_oauth()` on the background thread. Do not log or render tokens.

- [ ] **Step 4: Run the interaction tests**

Run:

```bash
pytest tests/widgets/test_gitee_account_row.py -v
```

Expected: 9 tests pass.

- [ ] **Step 5: Commit the action slice**

```bash
git add app/widgets/cards/settings/gitee_card.py tests/widgets/test_gitee_account_row.py
git commit -m "feat: gitee-account-row - add repository and binding actions"
```

### Task 3: Mount the row below Tab mode Settings

**Files:**
- Modify: `app/widgets/tab_panel.py:imports, TabPanel.__init__, TabPanel._setup_ui, TabPanel.refresh_style`
- Modify: `tests/widgets/test_tab_panel.py:imports, panel fixture, TestTabPanel`

- [ ] **Step 1: Write the failing TabPanel integration test**

In `tests/widgets/test_tab_panel.py`, add the import:

```python
from unittest.mock import patch
```

Change the `panel` fixture so constructing a test panel cannot activate a real bound account's sync service:

```python
@pytest.fixture
def panel(qtbot):
    with patch(
        "app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"
    ):
        p = TabPanel()
    qtbot.addWidget(p)
    return p
```

Add this test to `TestTabPanel`:

```python
    def test_gitee_account_row_is_below_settings(self, panel):
        from app.widgets.cards.settings.gitee_card import GiteeAccountRow

        assert isinstance(panel._gitee_account_row, GiteeAccountRow)
        panel_layout = panel.layout()
        settings_bar_index = panel_layout.indexOf(panel._settings_btn.parentWidget())
        account_row_index = panel_layout.indexOf(panel._gitee_account_row)
        assert account_row_index > settings_bar_index
```

- [ ] **Step 2: Run the integration test to verify it fails**

Run:

```bash
pytest tests/widgets/test_tab_panel.py::TestTabPanel::test_gitee_account_row_is_below_settings -v
```

Expected: FAIL with `AttributeError: 'TabPanel' object has no attribute '_gitee_account_row'`.

- [ ] **Step 3: Add the row to TabPanel**

In `app/widgets/tab_panel.py`, add this import with the other application widget imports:

```python
from app.widgets.cards.settings.gitee_card import GiteeAccountRow
```

In `TabPanel.__init__()`, initialize the typed field before `_setup_ui()`:

```python
        self._gitee_account_row: Optional[GiteeAccountRow] = None
```

In `TabPanel._setup_ui()`, immediately after `layout.addWidget(bottom_bar)`, add:

```python
        account_separator = QFrame(self)
        account_separator.setFrameShape(QFrame.HLine)
        account_separator.setStyleSheet(self._SEPARATOR_STYLE)
        layout.addWidget(account_separator)

        self._gitee_account_row = GiteeAccountRow(self)
        layout.addWidget(self._gitee_account_row)
```

At the end of `TabPanel.refresh_style()`, after `_refresh_plugin_style()`, add:

```python
        if self._gitee_account_row is not None:
            self._gitee_account_row.refresh_style()
```

- [ ] **Step 4: Run TabPanel and account-row tests**

Run:

```bash
pytest tests/widgets/test_tab_panel.py tests/widgets/test_gitee_account_row.py -v
```

Expected: all tests in both files pass.

- [ ] **Step 5: Commit the TabPanel integration**

```bash
git add app/widgets/tab_panel.py tests/widgets/test_tab_panel.py
git commit -m "feat: tab-panel - add Gitee account shortcut"
```

### Task 4: Validate behavior, style, and scope

**Files:**
- Verify: `app/widgets/cards/settings/gitee_card.py`
- Verify: `app/widgets/tab_panel.py`
- Verify: `tests/widgets/test_gitee_account_row.py`
- Verify: `tests/widgets/test_tab_panel.py`
- Reference: `docs/superpowers/specs/2026-07-26-tab-gitee-account-row-design.md`

- [ ] **Step 1: Run focused tests**

```bash
pytest tests/widgets/test_gitee_account_row.py tests/widgets/test_tab_panel.py -v
```

Expected: all focused tests pass with no real browser, OAuth, config-file write, or remote synchronization.

- [ ] **Step 2: Run static checks on only task-scope Python files**

```bash
ruff check app/widgets/cards/settings/gitee_card.py app/widgets/tab_panel.py tests/widgets/test_gitee_account_row.py tests/widgets/test_tab_panel.py
python -m py_compile app/widgets/cards/settings/gitee_card.py app/widgets/tab_panel.py tests/widgets/test_gitee_account_row.py tests/widgets/test_tab_panel.py
```

Expected: both commands exit with code 0.

- [ ] **Step 3: Review the exact task diff without touching unrelated worktree changes**

```bash
git diff -- app/widgets/cards/settings/gitee_card.py app/widgets/tab_panel.py tests/widgets/test_gitee_account_row.py tests/widgets/test_tab_panel.py
git status --short
```

Expected: every changed line in the four task files maps to the approved account-row feature. Existing unrelated modifications in `message_card.py`, `main.py`, `uv.lock`, or other files remain untouched and are not staged.

- [ ] **Step 4: Perform a local UI smoke check**

Run:

```bash
python main.py
```

Then verify this exact checklist without exposing credentials:

1. Enable Tab mode and confirm the account row is below Settings.
2. In an unbound test profile, confirm `?`, “Gitee 未绑定”, the backup/share hint, and “绑定” are visible.
3. In a bound profile, confirm the generated avatar, owner, `DriFox_uploads ↗`, and “解绑” are visible.
4. Resize the Tab panel to its 120 px minimum and confirm the action remains visible while long labels elide; hover shows complete Tooltips.
5. Toggle light/dark theme and font size; confirm the row refreshes without hard-coded light backgrounds.
6. Click the account body only when using a non-sensitive test account and confirm the expected repository URL opens.
7. Cancel the unbind confirmation and confirm no state changes.

Expected: all seven checks pass. Do not complete a live bind or unbind against the user's production account solely for smoke testing.

- [ ] **Step 5: Confirm documentation coverage**

Compare the final behavior against `docs/superpowers/specs/2026-07-26-tab-gitee-account-row-design.md`. If implementation matches, no documentation edit is needed because the design spec and this implementation plan already document the feature. If a behavior had to change, update only that spec section and commit it with the corresponding code change.
