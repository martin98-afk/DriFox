/**
 * bridge：浮动卡事件 → floatingStore 接线（mock emit 与 live ws.on 共用同一入口）。
 * retry 走 streamStore（F1 已有 retry/resolved 归约），不在此桥接。
 */

import { useFloatingStore } from "./floatingStore";
import type { ToastItem } from "./floatingStore";

/** 事件名 → 浮动卡 action。返回 true 表示该事件由浮动层消费 */
export function wireFloatingEvent(name: string, payload: unknown): boolean {
  const store = useFloatingStore.getState();
  switch (name) {
    case "permission.requested": {
      const p = payload as { tool_call_id?: string };
      if (!p?.tool_call_id) return false;
      store.showCard("permission", "bottom", p.tool_call_id);
      return true;
    }
    case "question.asked": {
      const p = payload as { id?: string };
      if (!p?.id) return false;
      store.showCard("question", "bottom", p.id);
      return true;
    }
    case "toast.show": {
      const p = payload as { title?: string; message?: string; level?: string };
      const level = p?.level === "error" ? "error" : p?.level === "warn" ? "warn" : "info";
      store.pushToast({ title: p?.title ?? "", message: p?.message ?? "", level: level as ToastItem["level"] });
      return true;
    }
    case "queue.updated": {
      // M2b b 表定稿：items:[{msg_id, summary}]；floatingStore QueueItem.text 承载摘要
      const p = payload as { items?: Array<{ msg_id?: string; summary?: string }> };
      const items = (p?.items ?? [])
        .filter((it): it is { msg_id: string; summary: string } => Boolean(it?.msg_id))
        .map((it) => ({ msg_id: it.msg_id, text: it.summary ?? "" }));
      store.setQueue(items);
      // 队列空 → 整卡隐藏（cards 里移除 queue 卡）
      if (items.length === 0) {
        store.closeCard("queue");
      } else if (!store.cards.some((c) => c.id === "queue" && !c.closing)) {
        store.showCard("queue", "bottom", "queue");
      }
      return true;
    }
    case "interject.recovered":
      // 计数事件：队列内容以 queue.updated 为准，此处仅确认消费
      return true;
    default:
      return false;
  }
}
