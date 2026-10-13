"""复现：切换历史输入后发送只剩附件（文字丢失）

模拟链路：
1. SendableTextEdit + 模拟 host（main_widget 的附件管理角色）
2. load_history → ↑ 进入历史 → 切换条目 → ↓ 退出
3. 每一步校验 toPlainText() 与 host._attachments 的一致性
4. 最后跑发送侧 _build_user_text_with_attachments 等价逻辑，看文字是否被吃
"""

import os
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)

from app.widgets.bottom_input_area import SendableTextEdit


class FakeHost:
    """模拟 main_widget 的附件管理角色（_on_history_* 三个槽）"""

    def __init__(self, input_area):
        self.input_area = input_area
        self._attachments: list[str] = []
        self._history_working_attachments: list[str] = []
        input_area.enteringHistoryMode.connect(self._on_entering_history_mode)
        input_area.historyAttachmentsRestored.connect(self._on_history_attachments_restored)
        input_area.historyModeExited.connect(self._on_history_mode_exited)

    def _on_entering_history_mode(self):
        self._history_working_attachments = self._attachments.copy()

    def _on_history_attachments_restored(self, paths: list):
        self._clear_attachments()
        for p in paths or []:
            if os.path.exists(p):
                self._add_attachment(p)
        print(f"    [host] 附件恢复: {self._attachments} (请求 {paths})")

    def _on_history_mode_exited(self):
        self._on_history_attachments_restored(self._history_working_attachments)
        self.input_area.convert_placeholders_to_mentions(self._history_working_attachments)

    def _add_attachment(self, path: str) -> bool:
        if not path or path in self._attachments:
            return False
        self._attachments.append(path)
        return True

    def _clear_attachments(self):
        self._attachments.clear()


def build_user_text_with_attachments(user_text: str, attachments: list[str]) -> str:
    """与 main_widget._build_user_text_with_attachments 逻辑一致"""
    if not attachments:
        return re.sub(r"\[\[[^\]]*\]\]", "", user_text).replace("  ", " ").strip()

    referenced = set()
    for p in attachments:
        basename = os.path.basename(p)
        placeholder = f"[[{basename}]]"
        if placeholder in user_text:
            user_text = user_text.replace(placeholder, f" {p} ", 1)
            referenced.add(p)

    remaining = [p for p in attachments if p not in referenced]
    if remaining:
        parts = [user_text]
        for p in remaining:
            parts.append(p)
        user_text = "\n".join(parts)

    user_text = re.sub(r"\[\[[^\]]*\]\]", "", user_text)
    user_text = re.sub(r"[^\S\n]{2,}", " ", user_text).strip()
    return user_text


def make_real_files(dirpath: str, names: list[str]) -> list[str]:
    os.makedirs(dirpath, exist_ok=True)
    paths = []
    for n in names:
        p = os.path.join(dirpath, n)
        with open(p, "w", encoding="utf-8") as f:
            f.write("x")
        paths.append(p)
    return paths


def check(step: str, area: SendableTextEdit, host: FakeHost, expect_text: str):
    got = area.toPlainText()
    ok = got == expect_text
    sent = build_user_text_with_attachments(got, list(host._attachments))
    print(f"[{step}]")
    print(f"    toPlainText: {got!r}")
    print(f"    附件: {host._attachments}")
    print(f"    发送文本: {sent!r}")
    if not ok:
        print(f"    ❌ 文本与期望不符！期望 {expect_text!r}")
    elif not got.strip():
        print("    ❌ 文本为空（即症状：只剩附件）")
    else:
        print("    ✅ 文本完整")
    print()
    return ok, sent


def main():
    tmp = os.path.join(os.path.dirname(__file__), "_tmp_repro_files")
    files_a = make_real_files(tmp, ["a.txt"])
    files_b = make_real_files(tmp, ["b.txt"])

    area = SendableTextEdit()
    host = FakeHost(area)

    # ── 场景1：单条历史（文字+附件），切入后发送 ──
    history = [
        {"text": "帮我分析这个文件 [[a.txt]] 谢谢", "attachments": list(files_a)},
        {"text": "纯文字消息没有附件", "attachments": []},
    ]
    area.load_history(history)

    area.setPlainText("我正在编辑的草稿")
    host._add_attachment(files_b[0])

    area._navigate_history(1)  # ↑ 进入历史 → 条目0
    check("1.↑ 进入历史(条目0: 文字+附件)", area, host, "帮我分析这个文件 [[a.txt]] 谢谢")

    # ── 场景2：继续切到条目1（纯文字） ──
    area._navigate_history(1)
    check("2.继续↑(条目1: 纯文字)", area, host, "纯文字消息没有附件")

    # ── 场景3：↓ 回到条目0 ──
    area._navigate_history(-1)
    check("3.↓ 回条目0", area, host, "帮我分析这个文件 [[a.txt]] 谢谢")

    # ── 场景4：↓ 退出历史 → 恢复草稿 ──
    area._navigate_history(-1)
    check("4.↓ 退出历史(恢复草稿)", area, host, "我正在编辑的草稿")

    # ── 场景5：历史模式中编辑文本（触发 _reset_history_mode）后发送 ──
    area._navigate_history(1)  # 进入条目0
    area.setPlainText("帮我分析这个文件 [[a.txt]] 谢谢，补充一点")  # 模拟用户编辑
    check("5.历史模式中编辑→退出模式", area, host, "帮我分析这个文件 [[a.txt]] 谢谢，补充一点")

    # ── 场景6：附件文件已被删除的历史条目 ──
    ghost = os.path.join(tmp, "ghost.txt")
    history2 = [
        {"text": "引用已删除附件 [[ghost.txt]]", "attachments": [ghost]},
    ]
    area.load_history(history2)
    area._navigate_history(1)
    check("6.附件已删除的条目", area, host, "引用已删除附件 [[ghost.txt]]")

    # ── 场景7：占位符数量多于附件数量（同名消耗殆尽） ──
    history3 = [
        {"text": "[[a.txt]] 和 [[a.txt]] 再和 [[a.txt]]", "attachments": list(files_a)},
    ]
    area.load_history(history3)
    area._navigate_history(1)
    check("7.占位符多于附件", area, host, "[[a.txt]] 和 [[a.txt]] 再和 [[a.txt]]")

    # ── 场景8：连续快速切换（模拟偶现按键） ──
    # 注：load_history 不清输入框，场景7残留文本成为 working_line，
    # ↑↓ 循环退出后恢复的就是它（预期行为），关键是不再出现格式污染
    area.load_history(history)
    for i in range(6):
        area._navigate_history(1 if i % 2 == 0 else -1)
    ok8, _ = check("8.连续快速切换6次", area, host, "[[a.txt]] 和 [[a.txt]] 再和 [[a.txt]]")

    print("=" * 60)
    print("全部场景跑完")

    # 清理
    for p in files_a + files_b:
        try:
            os.remove(p)
        except OSError:
            pass


if __name__ == "__main__":
    main()
