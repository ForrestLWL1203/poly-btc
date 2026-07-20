export function ObsMask({ label }) {
  return (
    <div className="mask">
      <span className="spin" style={{ width: 34, height: 34, borderWidth: 3 }} />
      <h2 style={{ marginTop: 22 }}>{label}</h2>
      <div className="sub">正在等待引擎确认…</div>
      <div className="mask-lock">⚠ 页面已锁定 · 操作进行中</div>
    </div>
  );
}
