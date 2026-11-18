/**
 * MemberList：成员状态列表（圆角描边容器；彩色字母头像+名字+青色「生成中...」或 idle 无文字）。
 * v1 mock 固定 5 成员（L橙黄/P红/B蓝/R青/F粉）。
 */

const MEMBERS = [
  { name: "leader", char: "L", color: "#e8a33e", busy: true },
  { name: "plan", char: "P", color: "#e5534b", busy: true },
  { name: "build", char: "B", color: "#4c8dff", busy: true },
  { name: "review", char: "R", color: "#2ec7c7", busy: true },
  { name: "frontend-developer", char: "F", color: "#e07ba0", busy: false },
];

export function MemberList() {
  return (
    <div className="MemberList">
      {MEMBERS.map((m) => (
        <div key={m.name} className="member-row" title={m.name}>
          <span className="member-avatar" style={{ background: m.color }} aria-hidden>
            {m.char}
          </span>
          <span className="member-name">{m.name}</span>
          {m.busy && <span className="member-status">生成中...</span>}
        </div>
      ))}
    </div>
  );
}
