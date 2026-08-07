const { useEffect, useRef, useState } = React;

const MOBILE_QUERY = "(max-width: 860px)";
const REFRESH_THRESHOLD = 68;
const MAX_PULL = 108;
const IGNORED_TARGETS = [
  "button", "a", "input", "textarea", "select", "[contenteditable='true']",
  ".drawer", ".modal", ".mask", ".tbl-wrap", ".range-tabs", ".hfilters", ".tabs",
].join(",");

const pullDistance = distance => Math.min(MAX_PULL, Math.max(0, distance) * 0.48);

export function PullToRefresh({ scrollRef }) {
  const [distance, setDistance] = useState(0);
  const [phase, setPhase] = useState("idle");
  const gesture = useRef({ tracking: false, startX: 0, startY: 0 });
  const phaseRef = useRef("idle");

  const updatePhase = next => {
    phaseRef.current = next;
    setPhase(next);
  };

  useEffect(() => {
    const scroll = scrollRef.current;
    if (!scroll) return;

    const reset = () => {
      gesture.current.tracking = false;
      setDistance(0);
      updatePhase("idle");
    };
    const mobile = () => !window.matchMedia || window.matchMedia(MOBILE_QUERY).matches;
    const ignored = target => target instanceof Element && !!target.closest(IGNORED_TARGETS);

    const onTouchStart = event => {
      if (!mobile() || event.touches.length !== 1 || scroll.scrollTop > 0 || ignored(event.target)) return;
      const touch = event.touches[0];
      gesture.current = { tracking: true, startX: touch.clientX, startY: touch.clientY };
    };
    const onTouchMove = event => {
      if (!gesture.current.tracking || event.touches.length !== 1) return;
      const touch = event.touches[0];
      const deltaX = touch.clientX - gesture.current.startX;
      const deltaY = touch.clientY - gesture.current.startY;
      if (scroll.scrollTop > 0 || deltaY <= 0 || Math.abs(deltaX) > Math.abs(deltaY) * 0.8) {
        reset();
        return;
      }
      event.preventDefault();
      const nextDistance = pullDistance(deltaY);
      setDistance(nextDistance);
      updatePhase(nextDistance >= REFRESH_THRESHOLD ? "armed" : "pulling");
    };
    const onTouchEnd = () => {
      if (!gesture.current.tracking) return;
      gesture.current.tracking = false;
      if (phaseRef.current !== "armed") {
        reset();
        return;
      }
      setDistance(54);
      updatePhase("refreshing");
      window.setTimeout(() => window.location.reload(), 140);
    };

    scroll.addEventListener("touchstart", onTouchStart, { passive: true });
    scroll.addEventListener("touchmove", onTouchMove, { passive: false });
    scroll.addEventListener("touchend", onTouchEnd, { passive: true });
    scroll.addEventListener("touchcancel", reset, { passive: true });
    return () => {
      scroll.removeEventListener("touchstart", onTouchStart);
      scroll.removeEventListener("touchmove", onTouchMove);
      scroll.removeEventListener("touchend", onTouchEnd);
      scroll.removeEventListener("touchcancel", reset);
    };
  }, [scrollRef]);

  const progress = Math.min(1, distance / REFRESH_THRESHOLD);
  const label = phase === "refreshing" ? "正在刷新" : phase === "armed" ? "松开刷新" : "下拉刷新";
  return (
    <div className={`pull-refresh ${phase}`} style={{ "--pull-distance": `${distance}px`, "--pull-progress": progress }}
      role="status" aria-live="polite" aria-hidden={phase === "idle"}>
      <span className="pull-refresh-glyph" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 4v13M7 12l5 5 5-5" />
        </svg>
      </span>
      <span>{label}</span>
    </div>
  );
}
