/**
 * InputArea（T10）：键盘全行为 + 历史导航 + IME 守卫。
 * 键盘（bottom_input_area.py 对齐）：Enter 发送 / Shift+Enter 换行 / Ctrl+Enter 反向发送（不清空）；
 * 空框↑进历史浏览态（保存 working_line），↑↓ 持续导航，↓ 走到末尾退出恢复 working_line，
 * 恢复条目光标置尾（非全选）；IME isComposing 守卫：组合中 Enter 不发送、↑↓ 不切历史。
 * 左右区视觉元素为静态占位（数据源端点服务端未就绪）。
 */

import { memo, useCallback, useEffect, useRef, useState } from "react";
import { useContextMenuStore } from "../shell/contextMenuStore";
import { useFloatingStore } from "../floating/floatingStore";

interface Props {
  onSend: (text: string) => void;
  onSendInverse?: (text: string) => void;
  /** 历史条目最新在前；缺省用本地轻量历史 */
  history?: string[];
  /** 流式进行中（live 模式下发送钮变停止钮，F10） */
  streaming?: boolean;
  /** live 停止回调（提供时 streaming 态显示停止钮） */
  onStop?: () => void;
}

const PLACEHOLDER = "输入框为空时按 ↑ / ↓ 切换历史输入";

export const InputArea = memo(function InputArea({ onSend, onSendInverse, history, streaming = false, onStop }: Props) {
  const [value, setValue] = useState("");
  const [browsing, setBrowsing] = useState(false);
  const localHistoryRef = useRef<string[]>([]);
  const histIndexRef = useRef(-1);
  const workingLineRef = useRef<string | null>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  // R4 🟡5：WKWebView 确认汉字时 compositionend 先于 keydown(Enter) 且 isComposing 已翻转 false，
  // 仅靠 isComposing 会误发送；记时窗 <150ms 内 Enter 忽略（业界通行补丁）
  const compositionEndAtRef = useRef(0);

  const currentHistory = history ?? localHistoryRef.current;

  const caretToEnd = useCallback(() => {
    requestAnimationFrame(() => {
      const el = taRef.current;
      if (!el) return;
      el.selectionStart = el.selectionEnd = el.value.length;
    });
  }, []);

  const submit = useCallback(
    (clearInput = true) => {
      const text = taRef.current?.value ?? value;
      const trimmed = text.trim();
      if (!trimmed) return;
      // 去重头插历史
      const h = localHistoryRef.current.filter((x) => x !== trimmed);
      h.unshift(trimmed);
      localHistoryRef.current = h.slice(0, 100);
      histIndexRef.current = -1;
      workingLineRef.current = null;
      setBrowsing(false);
      if (clearInput) setValue("");
      onSend(trimmed);
    },
    [onSend, value],
  );

  const navHistory = useCallback(
    (dir: 1 | -1) => {
      const h = currentHistory;
      if (h.length === 0) return;
      let idx = histIndexRef.current;
      if (idx < 0) {
        if (dir !== 1) return;
        workingLineRef.current = taRef.current?.value ?? "";
        idx = 0;
        setBrowsing(true);
      } else {
        idx += dir;
      }
      if (idx >= h.length) idx = h.length - 1;
      if (idx < 0) {
        // 走到末尾：退出浏览态，恢复 working_line
        const restore = workingLineRef.current ?? "";
        histIndexRef.current = -1;
        workingLineRef.current = null;
        setBrowsing(false);
        setValue(restore);
        caretToEnd();
        return;
      }
      histIndexRef.current = idx;
      setValue(h[idx]);
      caretToEnd();
    },
    [currentHistory, caretToEnd],
  );

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      // IME 守卫 1：组合中 Enter 不发送、↑↓ 不切历史
      if (e.nativeEvent.isComposing) return;
      // IME 守卫 2（R4 🟡5）：确认汉字后短时窗内 Enter 忽略（WKWebView compositionend 先于 keydown）
      if (Date.now() - compositionEndAtRef.current < 150) return;
      if (e.key === "Enter") {
        if (e.shiftKey) return; // 换行
        e.preventDefault();
        if (e.ctrlKey || e.metaKey) {
          // 反向发送：发送但不清空输入
          onSendInverse?.(value);
          return;
        }
        submit(true);
        return;
      }
      if (e.key === "ArrowUp") {
        const empty = (taRef.current?.value ?? "") === "";
        if (empty || histIndexRef.current >= 0) {
          e.preventDefault();
          navHistory(1);
        }
        return;
      }
      if (e.key === "ArrowDown") {
        if (histIndexRef.current >= 0) {
          e.preventDefault();
          navHistory(-1);
        }
      }
    },
    [navHistory, onSendInverse, submit, value],
  );

  void browsing; // 预留：浏览态 UI 标识

  // 新建对话等场景派发 drifox:focus-input 事件后聚焦输入框
  useEffect(() => {
    const onFocus = () => taRef.current?.focus();
    window.addEventListener("drifox:focus-input", onFocus);
    return () => window.removeEventListener("drifox:focus-input", onFocus);
  }, []);

  // 排队消息 insert/edit 回填（QueueMessageCard 派发）
  useEffect(() => {
    const onFill = (e: Event) => {
      const text = (e as CustomEvent<{ text?: string }>).detail?.text ?? "";
      if (!text) return;
      setValue((v) => (v ? `${v} ${text}` : text));
      taRef.current?.focus();
    };
    window.addEventListener("drifox:queue-fill", onFill);
    return () => window.removeEventListener("drifox:queue-fill", onFill);
  }, []);

  // 历史导航事件（editor 右键「历史输入」等价空框 ↑）
  useEffect(() => {
    const onNav = (e: Event) => {
      const dir = (e as CustomEvent<{ dir?: number }>).detail?.dir === -1 ? -1 : 1;
      navHistory(dir as 1 | -1);
    };
    window.addEventListener("drifox:history-nav", onNav);
    return () => window.removeEventListener("drifox:history-nav", onNav);
  }, [navHistory]);

  // editor 右键（§四）：粘贴/清空/历史输入
  const onContextMenu = (e: React.MouseEvent) => {
    e.preventDefault();
    const pushToast = useFloatingStore.getState().pushToast;
    const items = [
      {
        key: "paste",
        label: "粘贴",
        icon: "📋",
        action: () => {
          void navigator.clipboard
            .readText()
            .then((t) => {
              if (t) setValue((v) => v + t);
            })
            .catch(() => pushToast({ title: "粘贴", message: "剪贴板不可读（权限）", level: "warn" }));
        },
      },
      { key: "clear", label: "清空", danger: true, action: () => setValue("") },
      {
        key: "history",
        label: "历史输入",
        action: () => window.dispatchEvent(new CustomEvent("drifox:history-nav", { detail: { dir: 1 } })),
      },
 ];
    useContextMenuStore.getState().openMenu(
      { x: e.clientX, y: e.clientY, target: "editor", targetId: "input-main" },
      items,
    );
  };

  return (
    <section className="InputArea">
      <textarea
        ref={taRef}
        className="input-textarea"
        value={value}
        placeholder={PLACEHOLDER}
        rows={3}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={onKeyDown}
        onCompositionEnd={() => {
          compositionEndAtRef.current = Date.now();
        }}
        onContextMenu={onContextMenu}
      />
      <div className="input-toolbar">
        <div className="toolbar-left">
          <button className="model-select" type="button" title="模型选择">
            <span className="model-z">Z</span>
            <span className="model-name">glm-5.3-flash</span>
          </button>
          <span className="badge-max">max</span>
          <span className="ring" title="编码套餐用量">
            <span className="ring-arc" style={{ background: "conic-gradient(#5aa9ff 65%, var(--ts-pill-bg) 0)" }} />
          </span>
          <span className="ring" title="上下文占用">
            <span className="ring-arc" style={{ background: "conic-gradient(#2ecc71 32%, var(--ts-pill-bg) 0)" }} />
          </span>
        </div>
        <div className="toolbar-right">
          <span className="badge-tools" title="工具开关">0/5</span>
          <span className="toolbar-icon" title="刷新">⟳</span>
          <span className="toolbar-icon" title="截图">⛶</span>
          <span className="toolbar-icon" title="麦克风">🎙</span>
          <span className="toolbar-icon" title="编辑">✎</span>
          {streaming && onStop ? (
            <button className="send-btn is-stop" type="button" title="停止生成" onClick={onStop} aria-label="停止生成" />
          ) : (
            <button className="send-btn" type="button" title="发送" onClick={() => submit(true)} aria-label="发送" />
          )}
        </div>
      </div>
    </section>
  );
});
