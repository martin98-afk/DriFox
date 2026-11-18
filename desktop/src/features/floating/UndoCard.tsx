/**
 * UndoCard（§一 5）：单行「已删除 · [撤销]」+ 60s TTL 进度条。
 * 撤销 → 乐观回滚（会话重新入库）；到期 → 真删除生效 + 卡淡出。
 */

import { useEffect, useState } from "react";
import { useSessionStore } from "../../stores/sessionStore";
import { useFloatingStore, UNDO_TTL_MS } from "./floatingStore";

export function UndoCard() {
  const undo = useFloatingStore((st) => st.undo);
  const clearUndo = useFloatingStore((st) => st.clearUndo);
  const upsert = useSessionStore((st) => st.upsert);
  const setCurrent = useSessionStore((st) => st.setCurrent);
  const [leftMs, setLeftMs] = useState(UNDO_TTL_MS);
  const [expired, setExpired] = useState(false);

  const sid = undo?.sid;

  useEffect(() => {
    if (!undo) return;
    setExpired(false);
    setLeftMs(UNDO_TTL_MS);
    const timer = window.setInterval(() => {
      const rest = undo.deadline - Date.now();
      setLeftMs(Math.max(0, rest));
      if (rest <= 0) {
        clearInterval(timer);
        setExpired(true);
        // 到期真删除：调注册的执行器（live=restApi.deleteSession / mock=默认无操作本地已删）
        const store = useFloatingStore.getState();
        if (sid) store.deleteExecutor?.(sid);
        setTimeout(() => useFloatingStore.getState().clearUndo(), 300);
      }
    }, 200);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sid]);

  if (!undo || !sid) return null;

  const onUndo = () => {
    if (expired) return;
    // 乐观回滚：会话重新入库并设为当前
    upsert(undo.snapshot);
    setCurrent(undo.snapshot.id);
    localStorage.setItem("drifox.lastSid", undo.snapshot.id);
    clearUndo();
  };

  const pct = Math.max(0, Math.min(100, (leftMs / UNDO_TTL_MS) * 100));

  return (
    <div className={`FloatingCard UndoCard${expired ? " is-expired" : ""}`} role="status">
      <span className="undo-text">已删除「{undo.snapshot.title ?? sid}」</span>
      <button type="button" className="undo-btn" onClick={onUndo} disabled={expired}>
        撤销
      </button>
      <span className="undo-count">{Math.ceil(leftMs / 1000)}s</span>
      <div className="undo-ttl-bar" aria-hidden>
        <div className="undo-ttl-fill" style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}
