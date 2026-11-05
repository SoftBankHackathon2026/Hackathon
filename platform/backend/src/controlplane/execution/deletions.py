import logging

from controlplane.providers.contracts import (
    ProviderConflict,
    ProviderRejected,
    ProviderUnavailable,
    SubmissionUncertain,
)
from controlplane.repositories.deletions import DeletionStore

LOG = logging.getLogger(__name__)
MAX_RETRIES = 8


class Deletion:
    """What a provider step sees: the row, its checkpoint and how to move on."""

    def __init__(self, conn, row, poll_seconds: float):
        self.conn, self.row, self.poll_seconds = conn, row, poll_seconds
        self.cp = dict(row["checkpoint"])

    @property
    def state(self) -> str:
        return self.row["state"]

    @property
    def delete_data(self) -> bool:
        return self.row["delete_data"]

    def move(self, state: str, message: str):
        self.cp["retries"] = 0
        DeletionStore.transition(self.conn, self.row, state, message, checkpoint=self.cp)

    def wait(self, message: str | None = None):
        """Still in progress: look again later; record a new message only once."""
        self.cp["retries"] = 0
        if message and self.cp.get("message") != message:
            self.cp["message"] = message
            DeletionStore.add_event(self.conn, self.row, message)
        DeletionStore.defer(self.conn, self.row, seconds=self.poll_seconds, checkpoint=self.cp)

    def done(self, message: str):
        self.move("deleted", message)


def tick_deletion(conn, environment_id: str, step, *, poll_seconds: float = 5) -> bool:
    """Run one step of this environment's active deletion. True when one was pending,
    so the caller skips release work for this tick (the environment is not deployable)."""
    row = DeletionStore.next(conn, environment_id)
    if row is None:
        return DeletionStore.has_active(conn, environment_id)
    deletion = Deletion(conn, row, poll_seconds)
    try:
        step(deletion)
    except (ProviderUnavailable, SubmissionUncertain) as exc:
        retries = deletion.cp.get("retries", 0) + 1
        deletion.cp["retries"] = retries
        if retries <= MAX_RETRIES:
            DeletionStore.defer(conn, row, seconds=min(2**retries, 60), checkpoint=deletion.cp)
        else:
            DeletionStore.transition(
                conn,
                row,
                "failed",
                "클라우드 상태를 계속 확인하지 못해 삭제를 멈췄습니다. 잠시 후 다시 삭제하세요.",
                checkpoint=deletion.cp,
                error_code=type(exc).__name__,
            )
    except (ProviderConflict, ProviderRejected) as exc:
        DeletionStore.transition(
            conn,
            row,
            "failed",
            f"삭제를 멈췄습니다: {exc}",
            checkpoint=deletion.cp,
            error_code=type(exc).__name__,
        )
    except Exception as exc:  # noqa: BLE001 -- process boundary, no payload/credential logging
        LOG.error("deletion=%s state=%s exception=%s", row["id"], row["state"], type(exc).__name__)
        conn.rollback()
        DeletionStore.transition(
            conn,
            row,
            "failed",
            "예상하지 못한 오류로 삭제를 멈췄습니다. 이미 지운 리소스는 그대로입니다. 다시 삭제하면 남은 것부터 이어서 지웁니다.",
            checkpoint=deletion.cp,
            error_code=type(exc).__name__,
        )
    return True
