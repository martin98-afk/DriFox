# -*- coding: utf-8 -*-
"""服务商编辑结束后释放一次性表单，不影响隐藏后继续编辑的场景。"""

from types import SimpleNamespace

from PyQt5.QtCore import QCoreApplication, QEvent
from PyQt5.QtWidgets import QVBoxLayout, QWidget

from app.widgets.cards.global_card_controller import GlobalCardController


class _CardShell(QWidget):
    def __init__(self):
        super().__init__()
        self.content_layout = QVBoxLayout(self)
        self.save_handler = None

    def clear_save_button(self):
        self.save_handler = None


def _controller(form, shell):
    controller = GlobalCardController.__new__(GlobalCardController)
    controller._tab_manager = SimpleNamespace(close_replace_card=lambda card_id: None)
    controller._card_manager = SimpleNamespace(
        hide_card=lambda *args: None,
        show_card=lambda *args: None,
    )
    controller._provider_edit_popup = form
    controller._provider_picker_card = shell
    controller._provider_edit_card = None
    controller._provider_view = "edit"
    return controller


def test_close_releases_transient_provider_form(qapp):
    """关闭不是保留草稿：下一次本就重建，不能继续持有整棵模型表单。"""
    from app.widgets.cards.settings.provider_edit_card import ProviderEditCard
    import sip

    shell = _CardShell()
    form = ProviderEditCard(parent=shell)
    shell.content_layout.addWidget(form)
    shell.save_handler = form._on_save
    controller = _controller(form, shell)
    child_count = len(form.findChildren(QWidget))
    assert child_count > 10, "必须覆盖真实表单，不能仅删除一个空壳"

    controller._close_edit_card("provider_picker")
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    assert controller._provider_edit_popup is None, "关闭后控制器仍持有一次性表单"
    assert sip.isdeleted(form), "关闭后模型行及控件树应完成销毁"
    assert shell.content_layout.count() == 0
    assert shell.save_handler is None, "保存按钮不可继续持有已关闭表单"
    shell.deleteLater()


def test_dispose_cancels_login_and_drops_late_model_results(qapp):
    """关闭时协作取消登录，后台取模型晚返回也不发信号、不重建行。"""
    import threading

    from app.widgets.cards.settings.provider_edit_card import ProviderEditCard

    form = ProviderEditCard()
    cancel = threading.Event()
    form._login_cancel = cancel
    generation = form._login_generation
    delivered = []
    form.fetchSuccess.connect(lambda models: delivered.append(models))
    form.fetchFailed.connect(lambda reason: delivered.append(reason))

    def finish_after_close():
        form.dispose()
        return ["late-model"]

    form._do_fetch_thread(finish_after_close)
    assert cancel.is_set()
    assert form._login_generation != generation
    assert delivered == [], "后台返回不应继续投递已关闭表单"
    before = form.modelListEditor.get_models()
    form._on_fetch_success(["queued-before-close"])
    assert form.modelListEditor.get_models() == before
    form.dispose()  # 重复释放须幂等
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
