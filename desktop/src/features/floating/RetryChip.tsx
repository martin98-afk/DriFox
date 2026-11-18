/**
 * RetryChip：header 内联倒计时 chip（§一 3）。
 * min-width 定宽防 header 重排；本地倒数仅视觉（归零与重试由服务端事件驱动）；resolved 撤除。
 */

import { useEffect, useState } from "react";
import { useStreamStore } from "../../stores/streamStore";
import { useSessionStore } from "../../stores/sessionStore";

export function RetryChip() {
  const currentSid = useSessionStore((st) => st.currentSid);
  const retry = useStreamStore((st) => (currentSid ? st.streams[currentSid]?.retry ?? null : null));
  const [left, setLeft] = useState(0);

  useEffect(() => {
    if (!retry) return;
    setLeft(retry.waitTime);
    const timer = window.setInterval(() => {
      setLeft((v) => (v > 0 ? v - 1 : 0));
    }, 1000);
    return () => clearInterval(timer);
  }, [retry?.errorType, retry?.attempt, retry?.waitTime, retry]);

  if (!retry || !currentSid) return null;

  return (
    <span className={`RetryChip level-${retry.attempt >= retry.maxRetries ? "final" : "running"}`} role="status">
      重试 {retry.attempt}/{retry.maxRetries} · {left}s
    </span>
  );
}
