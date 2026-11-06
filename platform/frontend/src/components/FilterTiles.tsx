/**
 * 목록 위 필터 타일(Datadog Security Inbox 방식). 묶음마다 작은 타일 몇 개.
 * 타일 = 숫자 + 라벨. 누르면 아래 목록에 조건이 걸리고, 다시 누르면 풀린다.
 * 색은 문제가 있는 타일의 숫자에만.
 */
export type FilterTile = {
  id: string;
  label: string;
  value: number;
  tone?: "bad" | "warn" | "ok" | "none";
};

export function FilterTiles({
  groups,
  active,
  onSelect,
}: {
  groups: { title: string; note?: string; tiles: FilterTile[] }[];
  active: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <div className="ftiles" role="group" aria-label="빠른 필터">
      {groups.map((group) => (
        <section key={group.title} className="ftile-group">
          <header>
            <h2>{group.title}</h2>
            {group.note && <span>{group.note}</span>}
          </header>
          <div className="ftile-row">
            {group.tiles.map((tile) => (
              <button
                key={tile.id}
                type="button"
                className={`ftile tone-${tile.value > 0 ? (tile.tone ?? "none") : "none"}`}
                aria-pressed={active === tile.id}
                onClick={() => onSelect(tile.id)}
              >
                <b>{tile.value.toLocaleString("ko-KR")}</b>
                <span>{tile.label}</span>
              </button>
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
