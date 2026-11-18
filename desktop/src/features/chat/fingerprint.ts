/**
 * 块 identity 指纹：cyrb53（T9 定案，会话内稳定即可；sha1-12 对齐调试备开关后置）。
 */

/** cyrb53 字符串哈希（速度优先，会话内稳定） */
export function cyrb53(str: string, seed = 0): number {
  let h1 = 0xdeadbeef ^ seed;
  let h2 = 0x41c6ce57 ^ seed;
  for (let i = 0; i < str.length; i++) {
    const ch = str.charCodeAt(i);
    h1 = Math.imul(h1 ^ ch, 2654435761);
    h2 = Math.imul(h2 ^ ch, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return 4294967296 * (2097151 & h2) + (h1 >>> 0);
}

/** fingerprint(parts)：sha1 前 12 位等价物 = cyrb53 hex，会话内稳定 */
export function fingerprint(...parts: unknown[]): string {
  return cyrb53(parts.map((p) => (typeof p === "string" ? p : JSON.stringify(p))).join("\x1f")).toString(16);
}

/** identity 三元组（kind, home, key）序列化，全等比较用 */
export function blockIdentity(kind: string, home: string, key: string): string {
  return `${kind}\x1f${home}\x1f${key}`;
}

/** 流式纯文本尾态专用稳定 key */
export function liveKey(index: number): string {
  return `live:${index}`;
}
