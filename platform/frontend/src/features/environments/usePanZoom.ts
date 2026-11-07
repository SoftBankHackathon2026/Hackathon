import {
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type RefObject,
} from "react";
import type { Point } from "./canvasLayout";
import { zoomAt, type View } from "./iso";

/**
 * 다이어그램 끌어 옮기기, 휠/두 손가락 확대. 끌지 않고 바탕을 누르면 onBackground.
 * 블록 클릭 처리기는 moved() 로 끌기 끝의 클릭을 거른다.
 */
export function usePanZoom(
  svgRef: RefObject<SVGSVGElement | null>,
  onBackground: () => void,
) {
  const [view, setView] = useState<View>({ x: 0, y: 0, k: 1 });
  const pointers = useRef(new Map<number, Point>());
  const drag = useRef({ moved: false, startX: 0, startY: 0 });

  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const box = svg.getBoundingClientRect();
      const factor = Math.exp(-event.deltaY * (event.ctrlKey ? 0.01 : 0.0015));
      setView((prev) =>
        zoomAt(prev, factor, event.clientX - box.left, event.clientY - box.top),
      );
    };
    svg.addEventListener("wheel", onWheel, { passive: false });
    return () => svg.removeEventListener("wheel", onWheel);
  }, [svgRef]);

  const onPointerDown = (event: ReactPointerEvent<SVGSVGElement>) => {
    pointers.current.set(event.pointerId, [event.clientX, event.clientY]);
    drag.current = {
      moved: false,
      startX: event.clientX,
      startY: event.clientY,
    };
  };
  const onPointerMove = (event: ReactPointerEvent<SVGSVGElement>) => {
    const previous = pointers.current.get(event.pointerId);
    if (!previous) return;
    const next: Point = [event.clientX, event.clientY];
    if (!drag.current.moved) {
      if (
        Math.hypot(
          next[0] - drag.current.startX,
          next[1] - drag.current.startY,
        ) < 4
      )
        return;
      drag.current.moved = true;
      event.currentTarget.setPointerCapture(event.pointerId);
    }
    const others = [...pointers.current.entries()].filter(
      ([id]) => id !== event.pointerId,
    );
    if (others.length === 1) {
      const other = others[0][1];
      const before = Math.hypot(previous[0] - other[0], previous[1] - other[1]);
      const after = Math.hypot(next[0] - other[0], next[1] - other[1]);
      const box = event.currentTarget.getBoundingClientRect();
      if (before > 0)
        setView((prev) =>
          zoomAt(
            prev,
            after / before,
            (next[0] + other[0]) / 2 - box.left,
            (next[1] + other[1]) / 2 - box.top,
          ),
        );
    } else {
      setView((prev) => ({
        ...prev,
        x: prev.x + next[0] - previous[0],
        y: prev.y + next[1] - previous[1],
      }));
    }
    pointers.current.set(event.pointerId, next);
  };
  const onPointerUp = (event: ReactPointerEvent<SVGSVGElement>) => {
    pointers.current.delete(event.pointerId);
    if (drag.current.moved) return;
    // 블록, 표식처럼 누를 수 있는 것 위에서 뗀 경우는 바탕 클릭이 아니다(그 요소의 클릭이 처리한다).
    const target = event.target as Element | null;
    if (target?.closest('[role="button"], [data-clickable], button, a')) return;
    onBackground();
  };

  return {
    view,
    setView,
    moved: () => drag.current.moved,
    handlers: {
      onPointerDown,
      onPointerMove,
      onPointerUp,
      onPointerCancel: onPointerUp,
    },
  };
}
