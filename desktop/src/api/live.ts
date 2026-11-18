/**
 * live 模式接线层（F10）：ws 单例 + 事件总分发 + live 上行。
 *
 * isMockMode 分流集中在 initApp 一处：mock → startMock（现行为不变）；live → initLive。
 * 事件总分发与 mock emit 同构双路：wireFloatingEvent（浮动域）+ streamStore.applyEvent（流式域）
 * + session/message 域桥接（streamStore 不覆盖的 sessionStore/chatLog 直写）。
 */

import { WsClient } from "./ws";
import type { MessageInfo, SessionInfo } from "./ws";
import { restApi } from "./rest";
import { useStreamStore } from "../stores/streamStore";
import { useSessionStore } from "../stores/sessionStore";
import { useChatLog } from "../features/chat/chatLog";
import { useFloatingStore } from "../features/floating/floatingStore";
import { wireFloatingEvent } from "../features/floating/bridge";
import { startMock } from "../features/chat/mockDriver";
import type { ChatMessage } from "../features/chat/blocks";

const MODE = String(import.meta.env.VITE_API_MODE ?? "mock");

export function isLiveMode(): boolean {
  return MODE === "live";
}

let client: WsClient | null = null;

/** live 单例访问（mock 下 null；QueueMessageCard 等挂点判空） */
export function getWsClient(): WsClient | null {
  return client;
}

function toast(title: string, message: string, level: "info" | "warn" | "error" = "info"): void {
  useFloatingStore.getState().pushToast({ title, message, level });
}

/** App 挂载唯一入口：mock/live 分流集中此处 */
export function initApp(): void {
  if (MODE !== "live") {
    startMock();
    return;
  }
  initLive();
}

function initLive(): void {
  client = new WsClient();

  // 全量刷新信号（服务端 resync / hello.seq 回退 / 4401）
  client.onResyncNeeded((reason) => {
    if (reason === "auth-rejected-4401") {
      toast("连接被拒绝", "token 校验失败，请检查 VITE_SERVER_TOKEN", "error");
      return;
    }
    void sessionStoreReload(reason);
  });

  // 初载会话列表（hello 后）
  client.on("hello", () => {
    void sessionStoreReload("initial");
  });

  // 事件总分发：与 mock emit 同构双路
  client.onAny((env) => {
    wireFloatingEvent(env.name, env.payload);
    useStreamStore.getState().applyEvent(env);
    bridgeSessionDomain(env.name, env.payload, env.sid, env.ts);
  });

  // undo 到期真删除执行器（依赖注入：floatingStore 保持纯状态域）
  useFloatingStore.getState().registerDeleteExecutor((sid) => {
    void restApi
      .deleteSession(sid)
      .catch(() => toast("删除失败", `会话 ${sid} 删除请求失败`, "error"));
  });

  client.connect();
}

async function sessionStoreReload(reason: string): Promise<void> {
  try {
    await useSessionStore.getState().loadSessions();
  } catch {
    toast("会话列表刷新失败", `reason=${reason}`, "warn");
  }
}

/** session 域与消息域桥接（streamStore default 分支不覆盖的部分） */
function bridgeSessionDomain(name: string, payload: unknown, sid: string | null, ts: number): void {
  switch (name) {
    case "session.created":
    case "session.changed": {
      useSessionStore.getState().upsert(payload as SessionInfo);
      break;
    }
    case "session.deleted": {
      const p = payload as { session_id?: string };
      if (p?.session_id) useSessionStore.getState().removeLocal(p.session_id);
      break;
    }
    case "message.received": {
      if (!sid) break;
      const m = payload as MessageInfo;
      const msg: ChatMessage = {
        id: m.id ?? `msg-${sid}-${ts}`,
        role: m.role === "user" ? "user" : "assistant",
        text: m.content,
        model: undefined,
        ts: typeof m.ts === "number" && m.ts > 1e12 ? m.ts : Date.now(),
      };
      useChatLog.getState().appendMessage(sid, msg);
      break;
    }
    default:
      break;
  }
}

// ---------- live 上行 ----------

/** live 发送：ws client_cmd send；ack ok:false 时 toast 错误 */
export async function liveSend(text: string): Promise<void> {
  const sid = useSessionStore.getState().currentSid;
  if (!sid || !client) {
    toast("发送失败", "未连接服务端", "error");
    return;
  }
  try {
    const ack = await client.request("send", { session_id: sid, content: text }, sid);
    if (ack.ok === false) {
      toast("发送失败", String(ack.message ?? ack.error ?? "服务端拒绝"), "error");
    }
  } catch (e) {
    toast("发送失败", String((e as Error).message), "error");
  }
}

/** live 停止：ws client_cmd stop */
export async function liveStop(): Promise<void> {
  const sid = useSessionStore.getState().currentSid;
  if (!sid || !client) return;
  try {
    const ack = await client.request("stop", { session_id: sid }, sid);
    if (ack.ok === false) {
      toast("停止失败", String(ack.message ?? ack.error ?? "服务端拒绝"), "warn");
    }
  } catch (e) {
    toast("停止失败", String((e as Error).message), "warn");
  }
}

/** live 排队消息删除：ws client_cmd queue.remove；成功后本地移除，失败 toast 保留 */
export async function liveRemoveQueued(msgId: string): Promise<void> {
  if (!client) return;
  try {
    const ack = await client.request("queue.remove", { msg_id: msgId });
    if (ack.ok === false) {
      toast("删除失败", String(ack.message ?? "服务端拒绝"), "warn");
      return;
    }
    useFloatingStore.getState().removeFromQueue(msgId);
  } catch (e) {
    toast("删除失败", String((e as Error).message), "warn");
  }
}

/** live 会话切换：清流式态双清 + REST 载入会话消息进 chatLog */
export async function liveSwitchSession(sid: string): Promise<void> {
  const prev = useSessionStore.getState().currentSid;
  if (prev && prev !== sid) useStreamStore.getState().clearSession(prev);
  useStreamStore.getState().clearSession(sid);
  useSessionStore.getState().setCurrent(sid);
  localStorage.setItem("drifox.lastSid", sid);
  try {
    const s = (await restApi.getSession(sid)) as SessionInfo & { messages?: MessageInfo[] };
    const msgs = s.messages ?? [];
    const log = useChatLog.getState();
    for (const m of msgs) {
      log.appendMessage(sid, {
        id: m.id ?? `msg-${sid}-${m.ts ?? ""}`,
        role: m.role === "user" ? "user" : "assistant",
        text: m.content,
        ts: typeof m.ts === "number" && m.ts > 1e12 ? m.ts : Date.now(),
      });
    }
  } catch (e) {
    toast("会话载入失败", String((e as Error).message), "warn");
  }
}
