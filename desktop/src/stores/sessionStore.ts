/**
 * 会话列表 / 当前会话 store。
 *
 * - sessions: id → Session 缓存；order 为展示顺序（列表接口为权威快照）
 * - currentSid: 当前会话；切换的完整收口（含 backlog 回放）在 streamStore.switchSession
 */

import { create } from "zustand";
import { restApi } from "../api/rest";
import type { Session } from "../api/rest";

export interface LoadSessionsOpts {
  project?: string;
  limit?: number;
  offset?: number;
}

export interface CreateSessionPayload {
  title?: string;
  project?: string;
  agent?: string;
}

interface SessionState {
  sessions: Record<string, Session>;
  order: string[];
  currentSid: string | null;
  loading: boolean;
  /** 全量拉取会话列表（快照合并：返回顺序为主，快照外既有条目保尾） */
  loadSessions: (opts?: LoadSessionsOpts) => Promise<void>;
  setCurrent: (sid: string | null) => void;
  /** WS session.created/changed 增量入库 */
  upsert: (session: Session) => void;
  /** 本地移除（WS session.deleted / 删除后调用）；清 currentSid 若指向被删会话 */
  removeLocal: (id: string) => void;
  createSession: (payload?: CreateSessionPayload) => Promise<Session>;
  renameSession: (id: string, title: string) => Promise<void>;
  deleteSession: (id: string) => Promise<void>;
}

export const useSessionStore = create<SessionState>((set, get) => ({
  sessions: {},
  order: [],
  currentSid: null,
  loading: false,

  loadSessions: async (opts) => {
    set({ loading: true });
    try {
      const list = await restApi.listSessions(opts);
      set((st) => {
        const sessions = { ...st.sessions };
        for (const s of list) sessions[s.id] = s;
        const listed = new Set(list.map((s) => s.id));
        const tail = st.order.filter((id) => !listed.has(id) && sessions[id]);
        return { sessions, order: [...list.map((s) => s.id), ...tail], loading: false };
      });
    } catch (e) {
      set({ loading: false });
      throw e;
    }
  },

  setCurrent: (sid) => set({ currentSid: sid }),

  upsert: (session) =>
    set((st) => ({
      sessions: { ...st.sessions, [session.id]: session },
      order: st.order.includes(session.id) ? st.order : [...st.order, session.id],
    })),

  removeLocal: (id) => {
    const { [id]: _removed, ...sessions } = get().sessions;
    set((st) => ({
      sessions,
      order: st.order.filter((x) => x !== id),
      currentSid: st.currentSid === id ? null : st.currentSid,
    }));
  },

  createSession: async (payload) => {
    const session = await restApi.createSession(payload);
    get().upsert(session);
    return session;
  },

  renameSession: async (id, title) => {
    const session = await restApi.updateSession(id, { title });
    get().upsert(session);
  },

  deleteSession: async (id) => {
    await restApi.deleteSession(id);
    get().removeLocal(id);
  },
}));
