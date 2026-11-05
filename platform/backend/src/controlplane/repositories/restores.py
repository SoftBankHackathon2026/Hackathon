from controlplane.repositories.releases import ReleaseStore


def record(store: ReleaseStore, release_id: str, restore_of: str) -> None:
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO release_restores(release_id,restore_of) VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (release_id, restore_of),
        )


def restore_of(store: ReleaseStore, release_id: str) -> str | None:
    with store.connect() as conn:
        row = conn.execute(
            "SELECT restore_of FROM release_restores WHERE release_id=%s", (release_id,)
        ).fetchone()
    return row["restore_of"] if row else None
