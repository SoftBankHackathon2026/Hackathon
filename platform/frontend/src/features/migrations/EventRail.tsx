import { Skeleton } from "../../components/QueryBoundary";
import type { MigrationEvent } from "../../api/migrations";
import { formatDateTime } from "../../lib/format";
import { eventTitle } from "./gates";

export function EventRail({
  events,
}: {
  events: MigrationEvent[] | undefined;
}) {
  return (
    <aside className="event-rail" aria-label="타임라인">
      <h2>타임라인</h2>
      {!events ? (
        <Skeleton rows={2} />
      ) : events.length === 0 ? (
        <p className="empty-line">아직 기록된 이벤트가 없어요.</p>
      ) : (
        <ol>
          {events.map((event, i) => (
            <li
              key={`${event.at}-${i}`}
              className={event.kind === "warning" ? "warning" : undefined}
              title={event.detail || undefined}
            >
              <i className="dot" aria-hidden="true" />
              <time dateTime={event.at}>{formatDateTime(event.at)}</time>
              <span className="event-title">{eventTitle(event.title)}</span>
            </li>
          ))}
        </ol>
      )}
    </aside>
  );
}
