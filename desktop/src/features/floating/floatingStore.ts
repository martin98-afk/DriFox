/**
 * floatingStore：浮动卡状态与 CardHost 队列机制（§二）。
 *
 * 为什么独立于 pluginStore：pluginStore 语义是插件清单/槽位注册物；浮动卡是独立交互域
 * （优先级 gate / 关闭动画 / TTL / undo），混入会让两域生命周期耦合。
 *
 * 优先级 gate（showCard reducer）：
 * 1. question 强制覆盖一切——question 在场时其他卡拒绝入队；question 到达时非 question 卡全部让位
 * 2. permission 一次一张（同 kind 替换）
 * 3. 容器四向可共存（不同 container 互不关闭）；同容器内栈式
 * 关闭动画先行：closeCard 置 closing=true，200ms 后卸载（_stop_card_animations 语义）。
 */

import { create } from "zustand";

export type CardKind = "permission" | "question" | "queue" | "undo" | "settings";
export type FloatingContainer = "top" | "left" | "right" | "bottom";

export interface FloatingCard {
  id: string;
  kind: CardKind;
  container: FloatingContainer;
  /** 关闭动画先行标记：true 播出场动画，动画结束后卸载 */
  closing: boolean;
}

export interface ToastItem {
  id: string;
  title: string;
  message: string;
  level: "info" | "warn" | "error";
}

export interface QueueItem {
  msg_id: string;
  text: string;
}

export interface UndoEntry {
  sid: string;
  /** 会话快照（撤销回滚用） */
  snapshot: { id: string; title?: string; project?: string; agent?: string };
  deadline: number;
}

const CLOSE_ANIM_MS = 200;
const UNDO_TTL_MS = 60_000;

export type ShowResult = "shown" | "replaced" | "rejected";

interface FloatingState {
  cards: FloatingCard[];
  toasts: ToastItem[];
  queue: QueueItem[];
  undo: UndoEntry | null;
  /** undo 到期真删除执行器（依赖注入：mock 无操作本地已删 / live 注册 restApi.deleteSession，接线层 live.ts） */
  deleteExecutor: ((sid: string) => void) | null;
  registerDeleteExecutor: (fn: (sid: string) => void) => void;
  /** showCard 过优先级 gate */
  showCard: (kind: CardKind, container: FloatingContainer, id: string) => ShowResult;
  /** 关闭动画先行、卸载延后 */
  closeCard: (id: string) => void;
  /** 动画结束后物理卸载（Layer 动画回调/超时调用） */
  removeCard: (id: string) => void;
  pushToast: (t: Omit<ToastItem, "id">) => string;
  dismissToast: (id: string) => void;
  setQueue: (items: QueueItem[]) => void;
  removeFromQueue: (msgId: string) => void;
  setUndo: (entry: Omit<UndoEntry, "deadline">) => void;
  clearUndo: () => void;
}

export const useFloatingStore = create<FloatingState>((set, get) => ({
  cards: [],
  toasts: [],
  queue: [],
  undo: null,
  deleteExecutor: null,

  registerDeleteExecutor: (fn) => set({ deleteExecutor: fn }),

  showCard: (kind, container, id) => {
    const cards = get().cards;
    const active = cards.filter((c) => !c.closing);
    // gate 1：question 强制覆盖一切——question 在场时其他卡拒绝
    const hasQuestion = active.some((c) => c.kind === "question");
    if (kind !== "question" && hasQuestion) return "rejected";

    // gate 2：settings 系统卡一次一张互斥；打开时压制 bottom 命令卡（queue），
    // 但不随系统卡关闭非命令卡（permission/undo 保留，show_card:433-454 精确规则）
    const hasSettings = active.some((c) => c.kind === "settings");
    if (kind === "queue" && hasSettings) return "rejected";

    const next: FloatingCard[] = [];
    let result: ShowResult = "shown";
    for (const c of cards) {
      if (c.id === id && c.kind === kind) {
        continue; // 同卡幂等（settings 重复打开 = 已开则关闭语义由触发方处理）
      }
      // settings 互斥切换：旧 settings 让位
      if (kind === "settings" && c.kind === "settings" && !c.closing) {
        next.push({ ...c, closing: true });
        result = "replaced";
        continue;
      }
      // settings 打开：压制命令卡（queue），其他卡保留
      if (kind === "settings" && c.kind === "queue" && !c.closing) {
        next.push({ ...c, closing: true });
        continue;
      }
      // question 到达：非 question 卡全部让位（关闭动画先行）
      if (kind === "question" && !c.closing) {
        next.push({ ...c, closing: true });
        continue;
      }
      // permission 一次一张：新 permission 替换旧（让位动画）
      if (kind === "permission" && c.kind === "permission" && !c.closing) {
        next.push({ ...c, closing: true });
        result = "replaced";
        continue;
      }
      next.push(c);
    }
    next.push({ id, kind, container, closing: false });
    set({ cards: next });
    return result;
  },

  closeCard: (id) => {
    set((st) => ({
      cards: st.cards.map((c) => (c.id === id && !c.closing ? { ...c, closing: true } : c)),
    }));
    window.setTimeout(() => get().removeCard(id), CLOSE_ANIM_MS);
  },

  removeCard: (id) => set((st) => ({ cards: st.cards.filter((c) => c.id !== id) })),

  pushToast: (t) => {
    const id = `toast-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
    set((st) => ({ toasts: [...st.toasts, { ...t, id }] }));
    return id;
  },

  dismissToast: (id) => set((st) => ({ toasts: st.toasts.filter((t) => t.id !== id) })),

  setQueue: (items) => set({ queue: items }),

  removeFromQueue: (msgId) => set((st) => ({ queue: st.queue.filter((q) => q.msg_id !== msgId) })),

  setUndo: (entry) => set({ undo: { ...entry, deadline: Date.now() + UNDO_TTL_MS } }),

  clearUndo: () => set({ undo: null }),
}));

export { UNDO_TTL_MS, CLOSE_ANIM_MS };
