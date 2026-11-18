/**
 * ChatArea（T10）：@tanstack/react-virtual 虚拟列表 + wheel 上滚门控锚定。
 * 锚定规则（card_render_core.py 移植）：默认贴底；wheel 上滚解除锚定；距底 <40px 自动恢复；
 * wheel 同步派发排除程序滚动污染；骨架/流式卡是列表普通条目。
 */

import { useCallback, useEffect, useRef } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useSessionStore } from "../../stores/sessionStore";
import { useActiveView } from "./useActiveView";
import { MessageCard } from "./MessageCard";
import { StreamingMessage } from "./BlockRenderer";
import { SkeletonCard } from "./render/SkeletonCard";

const STICK_THRESHOLD_PX = 40;
const OVERSCAN = 5;

export function ChatArea() {
  const currentSid = useSessionStore((st) => st.currentSid);
  const { items, streamingState } = useActiveView(currentSid);

  const scrollRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);

  const virtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 110,
    overscan: OVERSCAN,
    getItemKey: (i) => {
      const it = items[i];
      return it.msg?.id ?? `${it.kind}:${it.index}`;
    },
  });

  // wheel 上滚门控：仅用户滚轮/触控板（同步派发），程序滚动不触发
  const onWheel = useCallback((e: React.WheelEvent) => {
    const el = scrollRef.current;
    if (!el || e.deltaY >= 0) return;
    if (el.scrollHeight > el.clientHeight) stickRef.current = false;
  }, []);

  // 距底 <40px 自动恢复跟随
  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (dist < STICK_THRESHOLD_PX) stickRef.current = true;
  }, []);

  // 🔴2：LiveTail write 后回调，贴底跟随（纯 delta 帧不走 React 重渲，锚定 effect 依赖不到）
  const handleGrow = useCallback(() => {
    const el = scrollRef.current;
    if (el && stickRef.current) el.scrollTop = el.scrollHeight;
  }, []);

  // 锚定 on rAF：贴底时新内容到达后跟到底
  useEffect(() => {
    if (!stickRef.current) return;
    const raf = requestAnimationFrame(() => {
      const el = scrollRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    });
    return () => cancelAnimationFrame(raf);
  }, [items.length, streamingState.settledText]);

  return (
    <section className="ChatArea">
      <div className="chat-scroll" ref={scrollRef} onWheel={onWheel} onScroll={onScroll}>
        <div style={{ height: virtualizer.getTotalSize(), position: "relative", width: "100%" }}>
          {virtualizer.getVirtualItems().map((vi) => {
            const item = items[vi.index];
            return (
              <div
                key={vi.key}
                data-index={vi.index}
                ref={virtualizer.measureElement}
                style={{ position: "absolute", top: 0, left: 0, width: "100%", transform: `translateY(${vi.start}px)` }}
              >
                {item.kind === "msg" && item.msg && <MessageCard msg={item.msg} index={vi.index} />}
                {item.kind === "skeleton" && <SkeletonCard />}
                {item.kind === "streaming" && currentSid && (
                  <StreamingMessage
                    sid={currentSid}
                    view={{
                      sid: currentSid,
                      reasoning: "",
                      settledText: streamingState.settledText,
                      activeText: "",
                      awaitingFirstDelta: streamingState.awaitingFirstDelta,
                    }}
                    tools={streamingState.tools}
                    settledLenRef={streamingState.settledLenRef}
                    onGrow={handleGrow}
                  />
                )}
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}
