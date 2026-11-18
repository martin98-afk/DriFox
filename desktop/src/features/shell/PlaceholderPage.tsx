/**
 * PlaceholderPage：五菜单占位壳（页头标题+描述+骨架区，右上 × 返回 chat）。
 */

import { useUiStore, type PageId } from "../../stores/uiStore";

const PAGES: Record<Exclude<PageId, "chat">, { title: string; desc: string }> = {
  shares: { title: "分享记录", desc: "管理已生成的分享链接与导出记录" },
  history: { title: "历史会话", desc: "检索与回看全部会话归档" },
  shortcuts: { title: "快捷键管理", desc: "自定义全局与局部快捷键" },
  marketplace: { title: "插件市场", desc: "浏览与安装社区插件" },
  cleanup: { title: "系统清理", desc: "清理缓存与冗余数据" },
};

export function PlaceholderPage({ page }: { page: Exclude<PageId, "chat"> }) {
  const setPage = useUiStore((st) => st.setPage);
  const meta = PAGES[page];
  return (
    <section className="PlaceholderPage">
      <header className="placeholder-head">
        <div>
          <h1 className="placeholder-title">{meta.title}</h1>
          <p className="placeholder-desc">{meta.desc}</p>
        </div>
        <button className="placeholder-close" title="返回对话" onClick={() => setPage("chat")} aria-label="返回对话">
          ×
        </button>
      </header>
      <div className="placeholder-skeleton">
        <div className="placeholder-skel-line" style={{ width: "40%" }} />
        <div className="placeholder-skel-line" style={{ width: "72%" }} />
        <div className="placeholder-skel-line" style={{ width: "58%" }} />
        <div className="placeholder-skel-line" style={{ width: "66%" }} />
      </div>
    </section>
  );
}
