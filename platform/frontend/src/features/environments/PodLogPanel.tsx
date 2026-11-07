import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  getPodLogs,
  type ComponentPod,
  type EnvComponent,
  type FleetEnvironment,
  type PodLogLine,
} from "../../api/migrations";
import { FilterSelect, StatusLabel } from "../../components/PageKit";
import { Segmented } from "../../components/Segmented";
import { SidePanel } from "../../components/SidePanel";
import { errorMessage } from "../../api/client";
import { podAge, podLabel, podTone } from "./podModel";
import {
  FOLLOW_MS,
  TAIL_OPTIONS,
  atBottom,
  highlight,
  levelCounts,
  logFileName,
  logLevel,
  logText,
  logTime,
  matches,
  keepsWheel,
  newLineCount,
} from "./podLogs";

export function PodLogPanel({
  env,
  component,
  pod,
  onBack,
  onClose,
}: {
  env: FleetEnvironment;
  component: EnvComponent;
  pod: ComponentPod;
  onBack: () => void;
  onClose: () => void;
}) {
  const names = pod.containers.map((c) => c.name);
  const [container, setContainer] = useState(names[0] ?? "");
  const [previous, setPrevious] = useState(false);
  const [tail, setTail] = useState<number>(TAIL_OPTIONS[0]);
  const [query, setQuery] = useState("");
  const [follow, setFollow] = useState(true);
  const [wrap, setWrap] = useState(true);
  const [copied, setCopied] = useState(false);
  const [unseen, setUnseen] = useState(0);
  const scroller = useRef<HTMLDivElement | null>(null);
  const search = useRef<HTMLInputElement | null>(null);
  const stuck = useRef(true);
  const shown = useRef<PodLogLine[]>([]);
  const live = follow && !previous;
  const logs = useQuery({
    queryKey: [
      "pod-logs",
      env.id,
      component.id,
      pod.name,
      container,
      tail,
      previous,
    ],
    queryFn: ({ signal }) =>
      getPodLogs(
        env.id,
        component.id,
        pod.name,
        { container: container || undefined, tail, previous },
        signal,
      ),
    refetchInterval: live ? FOLLOW_MS : false,
    placeholderData: keepPreviousData,
    retry: false,
  });
  const lines = logs.data?.lines ?? [];
  const refetch = logs.refetch;
  const visible = lines.filter((line) => matches(line.message, query));
  const counts = levelCounts(lines);

  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el) return;
    if (stuck.current) {
      el.scrollTop = el.scrollHeight;
      setUnseen(0);
    } else {
      setUnseen((n) => n + newLineCount(shown.current, lines));
    }
    shown.current = lines;
  }, [logs.data]);

  useEffect(() => {
    stuck.current = true;
    shown.current = [];
  }, [container, tail, previous]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target;
      if (
        target instanceof Element &&
        target.closest("input, select, textarea")
      )
        return;
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.key === "/") {
        event.preventDefault();
        search.current?.focus();
      } else if (event.key === "r" || event.key === "R") {
        event.preventDefault();
        void refetch();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [refetch]);

  useEffect(() => {
    const onWheel = (event: WheelEvent) => {
      const target = event.target as HTMLElement | null;
      if (!target?.closest(".fp-wide")) return;
      if (Math.abs(event.deltaX) > Math.abs(event.deltaY)) return;
      if (!keepsWheel(target, event.deltaY)) event.preventDefault();
    };
    document.addEventListener("wheel", onWheel, { passive: false });
    return () => document.removeEventListener("wheel", onWheel);
  }, []);

  const toBottom = () => {
    const el = scroller.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    stuck.current = true;
    setUnseen(0);
  };
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(logText(visible));
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  };
  const download = () => {
    const url = URL.createObjectURL(
      new Blob([logText(visible) + "\n"], { type: "text/plain" }),
    );
    const a = document.createElement("a");
    a.href = url;
    a.download = logFileName(pod.name, container, previous);
    a.click();
    URL.revokeObjectURL(url);
  };

  const now = Date.now();
  const age = podAge(pod.started_at);
  return (
    <SidePanel
      kind="파드 로그"
      id={component.name}
      wide
      onBack={onBack}
      onClose={onClose}
      title={
        <>
          {pod.name}
          <StatusLabel tone={podTone(pod)}>{podLabel(pod)}</StatusLabel>
        </>
      }
      facts={[
        { label: "환경", value: env.name },
        { label: "노드", value: pod.node ?? "-" },
        {
          label: "재시작",
          value: String(pod.restarts),
          tone: pod.restarts > 0 ? "bad" : undefined,
        },
        { label: "시작", value: age ? `${age} 전` : "-" },
      ]}
    >
      <div className="pl">
        <div className="pl-tools">
          <div className="canvas-tools">
            {pod.restarts > 0 && (
              <Segmented
                label="실행"
                value={previous ? "previous" : "current"}
                options={[
                  { value: "current", label: "지금" },
                  { value: "previous", label: "재시작 전" },
                ]}
                onChange={(v) => setPrevious(v === "previous")}
              />
            )}
            {names.length > 1 && (
              <FilterSelect
                label="컨테이너"
                value={container}
                options={names.map((n) => ({ value: n, label: n }))}
                onChange={setContainer}
              />
            )}
            <FilterSelect
              label="최근"
              value={String(tail)}
              options={TAIL_OPTIONS.map((n) => ({
                value: String(n),
                label: `${n}줄`,
              }))}
              onChange={(v) => setTail(Number(v))}
            />
            <button
              type="button"
              className={`tool-toggle${live ? " on" : ""}`}
              aria-pressed={live}
              disabled={previous}
              title={
                previous
                  ? "재시작 전 로그는 더 늘지 않아요"
                  : "5초마다 새 로그를 불러와요"
              }
              onClick={() => setFollow((v) => !v)}
            >
              <span className="tool-switch" aria-hidden="true" />
              실시간
            </button>
            <button
              type="button"
              className={`tool-toggle${wrap ? " on" : ""}`}
              aria-pressed={wrap}
              onClick={() => setWrap((v) => !v)}
            >
              <span className="tool-switch" aria-hidden="true" />
              줄바꿈
            </button>
          </div>
          <div className="canvas-tools">
            <input
              ref={search}
              type="search"
              placeholder="로그에서 찾기 ( / )"
              aria-label="로그에서 찾기"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") {
                  e.stopPropagation();
                  if (query) setQuery("");
                  else e.currentTarget.blur();
                }
              }}
            />
            <span className="pl-count">
              {query
                ? `${visible.length} / ${lines.length}줄`
                : `${lines.length}줄`}
              {counts.error > 0 && (
                <span className="text-bad">, 오류 {counts.error}</span>
              )}
              {counts.warn > 0 && (
                <span className="text-warn">, 경고 {counts.warn}</span>
              )}
            </span>
            <span className="pl-actions">
              <button
                type="button"
                className="cp-ep-copy"
                disabled={logs.isFetching}
                aria-busy={logs.isFetching}
                title="지금 로그를 다시 불러와요 (R)"
                onClick={() => void logs.refetch()}
              >
                {logs.isFetching ? "불러오는 중" : "새로 고침"}
              </button>
              <button
                type="button"
                className="cp-ep-copy"
                disabled={visible.length === 0}
                onClick={copy}
              >
                {copied ? "복사했어요" : "복사"}
              </button>
              <button
                type="button"
                className="cp-ep-copy"
                disabled={visible.length === 0}
                onClick={download}
              >
                내려받기
              </button>
            </span>
          </div>
        </div>
        <div
          ref={scroller}
          className={`pl-log${wrap ? " wrap" : ""}`}
          role="log"
          aria-label={`${pod.name} 로그`}
          tabIndex={0}
          onScroll={(e) => {
            stuck.current = atBottom(e.currentTarget);
            if (stuck.current) setUnseen(0);
          }}
        >
          {logs.isPending ? (
            <div
              className="skeleton pl-skeleton"
              role="status"
              aria-label="불러오는 중"
            >
              <span />
              <span />
              <span />
            </div>
          ) : logs.isError && !logs.data ? (
            <div className="pl-empty">
              <p>{errorMessage(logs.error, "로그를 불러오지 못했어요.")}</p>
              <button
                type="button"
                className="pk-button weak"
                onClick={() => logs.refetch()}
              >
                다시 불러오기
              </button>
            </div>
          ) : lines.length === 0 ? (
            <p className="pl-empty">
              {previous
                ? "재시작 전 실행이 남긴 로그가 없어요."
                : "아직 남긴 로그가 없어요. 실시간을 켜 두면 새 로그가 생길 때 보여요."}
            </p>
          ) : visible.length === 0 ? (
            <p className="pl-empty">“{query.trim()}”이 들어간 줄이 없어요.</p>
          ) : (
            visible.map((line, index) => {
              const level = logLevel(line.message);
              return (
                <div
                  key={`${line.at ?? ""}-${index}`}
                  className={`pl-line${level ? ` ${level}` : ""}`}
                >
                  <time dateTime={line.at ?? undefined} title={line.at ?? ""}>
                    {logTime(line.at, now)}
                  </time>
                  <span className="pl-msg">
                    {highlight(line.message, query).map((part, i) =>
                      part.hit ? (
                        <mark key={i}>{part.text}</mark>
                      ) : (
                        <span key={i}>{part.text}</span>
                      ),
                    )}
                  </span>
                </div>
              );
            })
          )}
        </div>
        {unseen > 0 && (
          <button
            type="button"
            className="pk-button pl-jump"
            onClick={toBottom}
          >
            새 로그 {unseen}줄
          </button>
        )}
        <p className="pl-status">
          {logs.data && (
            <>
              {logTime(logs.data.fetched_at, now)} 기준
              {logs.data.redacted > 0 &&
                `, 비밀번호와 토큰 ${logs.data.redacted}개를 가렸어요`}
              {logs.data.truncated && ", 길어서 앞부분을 잘랐어요"}
              {logs.isError && (
                <span className="text-bad">
                  , {errorMessage(logs.error, "새로 고치지 못했어요")}
                </span>
              )}
            </>
          )}
        </p>
      </div>
    </SidePanel>
  );
}
