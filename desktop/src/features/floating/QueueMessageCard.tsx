/**
 * QueueMessageCard（§一 4）：排队消息卡（bottom 容器）。
 * 每条一行（摘要单行截断+压缩空白）+ [插入][编辑][删除]；队列空整卡隐藏。
 * insert → 回填输入框（drifox:queue-fill 事件）；edit → 填入并移出；remove → mock 本地移除（live 走 queue.remove 上行，联调波次）。
 */

import { useFloatingStore } from "./floatingStore";
import { isMockMode } from "../chat/mockDriver";
import { liveRemoveQueued } from "../../api/live";

function compact(text: string): string {
  return text.replace(/\s+/g, " ").trim().slice(0, 60);
}

function fillInput(text: string): void {
  window.dispatchEvent(new CustomEvent("drifox:queue-fill", { detail: { text } }));
}

export function QueueMessageCard() {
  const queue = useFloatingStore((st) => st.queue);
  const removeFromQueue = useFloatingStore((st) => st.removeFromQueue);

  /** 删除：mock 本地移除（一字不动）；live 走 ws queue.remove 上行，ack ok 后本地移除 */
  const removeItem = (msgId: string) => {
    if (isMockMode()) {
      removeFromQueue(msgId);
      return;
    }
    void liveRemoveQueued(msgId);
  };

  if (queue.length === 0) return null;

  return (
    <div className="FloatingCard QueueMessageCard" role="region" aria-label="排队消息">
      <div className="queue-head">排队消息（{queue.length}）</div>
      {queue.map((it) => (
        <div key={it.msg_id} className="queue-row">
          <span className="queue-text" title={it.text}>{compact(it.text)}</span>
          <span className="queue-actions">
            <button
              type="button"
              className="queue-btn"
              title="插入输入框（保留排队）"
              onClick={() => fillInput(it.text)}
            >
              ⇥
            </button>
            <button
              type="button"
              className="queue-btn"
              title="编辑（填入并移出队列）"
              onClick={() => {
                fillInput(it.text);
                removeFromQueue(it.msg_id);
              }}
            >
              ✎
            </button>
            <button
              type="button"
              className="queue-btn is-danger"
              title="删除"
              onClick={() => removeItem(it.msg_id)}
            >
              ×
            </button>
          </span>
        </div>
      ))}
    </div>
  );
}
