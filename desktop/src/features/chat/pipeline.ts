/**
 * 块渲染管线（T9）：fence 状态机 + 公式流式安全 + markdown-it + shiki 子集 + KaTeX + LRU 缓存。
 *
 * 安全模型：markdown-it html:false（内联 HTML 全转义，sanitize 语义等价前置）；
 * 公式经 KaTeX throwOnError:false 渲染，未闭合不渲染（原样文本）。
 * shiki：fine-grained 子集 8 语言 + createJavaScriptRegexEngine（免 WASM），dracula，
 *       初始化完成前降级 escapeHtml（首帧可接受）。
 */

import MarkdownIt from "markdown-it";
import { createHighlighterCore, type HighlighterCore } from "shiki/core";
import { createJavaScriptRegexEngine } from "shiki/engine/javascript";
import katex from "katex";
import "katex/dist/katex.min.css";
import { fingerprint } from "./fingerprint";

// ---------- shiki 单例（构建期单例，异步初始化同步用） ----------

const SHIKI_LANGS = [
  import("shiki/langs/javascript.mjs"),
  import("shiki/langs/typescript.mjs"),
  import("shiki/langs/python.mjs"),
  import("shiki/langs/json.mjs"),
  import("shiki/langs/bash.mjs"),
  import("shiki/langs/markdown.mjs"),
  import("shiki/langs/yaml.mjs"),
  import("shiki/langs/html.mjs"),
] as const;

let highlighter: HighlighterCore | null = null;
void createHighlighterCore({
  themes: [import("shiki/themes/dracula.mjs")],
  langs: [...SHIKI_LANGS],
  engine: createJavaScriptRegexEngine(),
}).then((h) => {
  highlighter = h;
});

/** shiki 支持的语言别名表（fence lang → shiki lang id） */
const LANG_ALIASES: Record<string, string> = {
  js: "javascript", jsx: "javascript", javascript: "javascript",
  ts: "typescript", tsx: "typescript", typescript: "typescript",
  py: "python", python: "python",
  json: "json", sh: "bash", shell: "bash", bash: "bash", zsh: "bash",
  md: "markdown", markdown: "markdown",
  yml: "yaml", yaml: "yaml",
  html: "html", vue: "html",
};

function highlightCode(code: string, lang: string): string {
  const langId = LANG_ALIASES[lang.toLowerCase()] ?? "";
  if (!langId || !highlighter) return escapeHtml(code);
  try {
    return highlighter.codeToHtml(code, { lang: langId, theme: "dracula" });
  } catch {
    return escapeHtml(code);
  }
}

// ---------- markdown-it 单例 ----------

const md = new MarkdownIt({
  html: false,
  breaks: true,
  linkify: false,
  highlight(code, lang) {
    return highlightCode(code, lang ?? "");
  },
});

// ---------- 基础工具 ----------

function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/**
 * fence 状态机行扫：``` 与 ~~~ 同 marker 计数（行内反引号不计数）。
 * 返回 complete（所有 fence 已闭合的安全渲染区）与 pending（未闭合尾段）。
 */
export function scanFence(text: string): { complete: string; pending: { lang: string; content: string } | null } {
  const lines = text.split("\n");
  const openRe = /^\s*(`{3,}|~{3,})\s*([^`\s]*)/;
  let marker: string | null = null;
  let lang = "";
  let fenceStart = -1;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (marker === null) {
      const m = openRe.exec(line);
      if (m) {
        marker = m[1];
        lang = m[2] ?? "";
        fenceStart = i;
      }
    } else if (line.trimStart().startsWith(marker)) {
      marker = null;
      lang = "";
    }
  }
  if (marker === null) return { complete: text, pending: null };
  const complete = lines.slice(0, fenceStart).join("\n");
  const pending = { lang, content: lines.slice(fenceStart + 1).join("\n") };
  return { complete, pending };
}

/** 特殊 fence 语言：流式期间渲染等高骨架占位（半截渲染必坏） */
const SPECIAL_FENCE_LANGS = new Set(["mermaid", "echarts", "html", "widget"]);

// ---------- 公式流式安全 ----------

/** 预处理：裸井号编号转义（`#56` 会被当 ATX 标题）；表格前补空行 */
function preprocess(text: string): string {
  const lines = text.split("\n");
  const out: string[] = [];
  for (let i = 0; i < lines.length; i++) {
    let line = lines[i];
    // 行首 ATX 误判：# 后紧跟数字 → 转义井号
    line = line.replace(/^(\s*)(#{1,6})(\d)/, "$1\\$2$3");
    // 表格行前无空行 → 补空行
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      const prev = out.length > 0 ? out[out.length - 1] : "";
      if (prev.trim() !== "") out.push("");
    }
    out.push(line);
  }
  return out.join("\n");
}

/**
 * 公式切段：优先级 $$ → \[ → \( → $；fence 内原样由调用方保证（先 scanFence 后走本函数）。
 * 未闭合不渲染：保留原文本交 markdown-it。
 */
export function splitMath(text: string): Array<{ kind: "md" | "math"; text: string; display?: boolean }> {
  const segments: Array<{ kind: "md" | "math"; text: string; display?: boolean }> = [];
  const rules: Array<[RegExp, boolean]> = [
    [/^\$\$([\s\S]+?)\$\$/, true],
    [/^\\\[([\s\S]+?)\\\]/, true],
    [/^\\\(([\s\S]+?)\\\)/, false],
    [/^\$([^$\n]+?)\$/, false],
  ];
  let rest = text;
  let plain = "";
  while (rest.length > 0) {
    let matched = false;
    for (const [re, display] of rules) {
      const m = re.exec(rest);
      if (!m) continue;
      if (plain) {
        segments.push({ kind: "md", text: plain });
        plain = "";
      }
      segments.push({ kind: "math", text: m[1], display });
      rest = rest.slice(m[0].length);
      matched = true;
      break;
    }
    if (!matched) {
      plain += rest[0];
      rest = rest.slice(1);
    }
  }
  if (plain) segments.push({ kind: "md", text: plain });
  return segments;
}

function renderMath(tex: string, display: boolean): string {
  try {
    return katex.renderToString(tex, { displayMode: display, throwOnError: false, output: "html" });
  } catch {
    return escapeHtml(tex);
  }
}

// ---------- 渲染缓存（BlockParseCache，LRU 128） ----------

const cache = new Map<string, string>();
const CACHE_MAX = 128;

function cacheGet(key: string): string | undefined {
  const hit = cache.get(key);
  if (hit !== undefined) {
    cache.delete(key);
    cache.set(key, hit); // refresh LRU 位置
  }
  return hit;
}

function cacheSet(key: string, html: string): void {
  if (cache.size >= CACHE_MAX) {
    const oldest = cache.keys().next().value;
    if (oldest !== undefined) cache.delete(oldest);
  }
  cache.set(key, html);
}

// ---------- 主入口 ----------

/** 未闭合 fence：等高骨架占位（特殊语言改标 <lang>-streaming） */
function pendingFenceHtml(lang: string): string {
  const special = SPECIAL_FENCE_LANGS.has(lang.toLowerCase());
  const label = special ? `${lang.toLowerCase()}-streaming` : "code-streaming";
  return `<pre class="fence-pending" data-lang="${escapeHtml(label)}"><span class="fence-pending-label">${escapeHtml(label)}</span><span class="fence-pending-lines"></span></pre>`;
}

/**
 * 完整 markdown 管线：预处理 → fence 扫描 → 公式切段 → md 渲染 → 拼接。
 * 缓存 key = fingerprint(text, completed)；内容不变即命中。
 */
export function renderMarkdown(text: string, completed = true): string {
  const key = fingerprint(text, completed);
  const hit = cacheGet(key);
  if (hit !== undefined) return hit;

  const { complete, pending } = scanFence(text);
  let html = "";

  if (complete.trim().length > 0) {
    const preprocessed = preprocess(complete);
    for (const seg of splitMath(preprocessed)) {
      if (seg.kind === "md") {
        html += md.render(seg.text);
      } else {
        html += `<p class="math-block">${renderMath(seg.text, seg.display ?? false)}</p>`;
      }
    }
  }
  if (pending) {
    html += pendingFenceHtml(pending.lang);
  }

  cacheSet(key, html);
  return html;
}

/** 清缓存（shiki inline style 不随主题变；供调试用） */
export function clearPipelineCache(): void {
  cache.clear();
}
