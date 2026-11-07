"""원가연 전환 job(migration.cloud)과의 경계 — 단계 읽기·배타 잠금·경계 규칙.

트래픽 명령과 예열은 job 단계에 따라 허용 여부가 갈린다. 그래서

  * 실행하는 동안 원가연 `<state-dir>/cloud.lock` 을 원가연과 같은
    방식(fcntl.flock LOCK_EX|LOCK_NB, migration/cloud/__main__.py)으로 잡아
    `run --approve` 와 직렬화한다. 못 잡으면 거부한다.
  * 단계는 원가연 상태 저장소를 읽기만 해서 얻는다. `python -m migration.cloud
    status` 는 같은 잠금을 잡으므로 우리가 잠금을 쥔 동안에는 부를 수 없다.
    읽는 내용(job 행의 stage·checkpoint.failed_stage)은 status 출력과 같다.
    저장 형식이 바뀌면 read_cutover_status 하나만 바꾼다.

  with cutover_job.migration_job_lock(state_dir):
      status = cutover_job.read_cutover_status(state_dir, job_id)
      if not cutover_job.routing_change_allowed(status, adapter_state): ...
"""

import collections.abc
import contextlib
import dataclasses
import fcntl
import json
import pathlib
import sqlite3
from typing import Literal

CutoverStage = Literal[
    "not_started",
    "PRECHECK",
    "COPYING",
    "READY",
    "FENCING",
    "DRAINING",
    "STOPPING",
    "SWITCHING",
    "COMPLETED",
    "FAILED",
    "NEEDS_RECOVERY",
]
_STAGES = frozenset(CutoverStage.__args__)
# 승인(run --approve) 전 단계. 이 구간은 원본 DB 가 아직 막히지 않았다.
_BEFORE_APPROVAL = frozenset({"not_started", "PRECHECK", "COPYING", "READY"})
# 원가연 migration/state.py 의 파일 이름.
_STORE_FILE = "cutover.sqlite3"
_LOCK_FILE = "cloud.lock"


class StatusUnreadableError(RuntimeError):
    """job 단계를 읽지 못했다. 부르는 쪽은 거부로 다룬다."""


class JobBusyError(RuntimeError):
    """migration.cloud 가 잠금을 쥐고 실행 중이다."""


@dataclasses.dataclass(frozen=True)
class CutoverStatus:
    """job 단계. FAILED 면 failed_stage 에 실패한 단계가 든다."""

    stage: CutoverStage
    failed_stage: CutoverStage | None


def read_cutover_status(state_dir: pathlib.Path, job_id: str) -> CutoverStatus:
    """원가연 상태 저장소에서 job 단계를 읽는다(쓰지 않는다).

    Args:
        state_dir: migration.cloud 의 --state-dir.
        job_id: config.cloud.yaml 의 job.job_id.

    Returns:
        저장소나 job 행이 없으면 not_started.

    Raises:
        StatusUnreadableError: 디렉토리가 없거나, 저장소를 못 읽거나, 모르는
            단계일 때.
    """
    if not state_dir.is_dir():
        raise StatusUnreadableError(f"state-dir 가 없다: {state_dir}")
    store = state_dir / _STORE_FILE
    if not store.exists():
        return CutoverStatus("not_started", None)
    row = _read_job_row(store, job_id)
    if row is None:
        return CutoverStatus("not_started", None)
    stage, checkpoint_text = row
    try:
        failed_stage = json.loads(checkpoint_text).get("failed_stage")
    except (ValueError, AttributeError) as error:
        raise StatusUnreadableError("checkpoint 를 읽지 못했다") from error
    for value in (stage, failed_stage):
        if value is not None and value not in _STAGES:
            raise StatusUnreadableError(f"모르는 단계: {value!r}")
    return CutoverStatus(stage, failed_stage)


@contextlib.contextmanager
def migration_job_lock(
    state_dir: pathlib.Path,
) -> collections.abc.Iterator[None]:
    """migration.cloud 와 같은 잠금을 잡는다. 블록이 끝나면 푼다.

    Raises:
        StatusUnreadableError: state-dir 가 없을 때. 경로 오타를 '아직 시작
            안 한 job' 으로 오인하지 않게 새로 만들지 않는다.
        JobBusyError: migration.cloud 등 다른 프로세스가 쥐고 있을 때.
    """
    if not state_dir.is_dir():
        raise StatusUnreadableError(f"state-dir 가 없다: {state_dir}")
    with (state_dir / _LOCK_FILE).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise JobBusyError(
                "migration.cloud 가 실행 중이다(cloud.lock)"
            ) from error
        yield


def routing_change_allowed(
    status: CutoverStatus, adapter_state: str | None
) -> bool:
    """트래픽 가중치를 바꿔도 되는 단계인지 판정한다.

    승인 전(또는 승인 전 단계의 FAILED)만 허용한다. FENCING 부터는 원본 DB 가
    막히므로 원본 방향 라우팅은 쓸모가 없고 위험하다. 예외 하나: FENCING 에서
    실패했는데 어댑터가 아직 RUNNING 이면 pause 가 반영되지 않은 것이라(원본
    차단은 pause 뒤에 설치된다) 허용한다. 대상 100 이 아니어서 /pause 가
    거절된 경우가 여기에 든다.

    Args:
        status: job 단계.
        adapter_state: 제어 어댑터 /status 의 state. 못 읽었으면 None.
    """
    if status.stage in _BEFORE_APPROVAL:
        return True
    if status.stage != "FAILED":
        return False
    if status.failed_stage in _BEFORE_APPROVAL:
        return True
    return status.failed_stage == "FENCING" and adapter_state == "RUNNING"


def _read_job_row(store: pathlib.Path, job_id: str) -> tuple[str, str] | None:
    try:
        connection = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
        try:
            return connection.execute(
                "SELECT stage, checkpoint FROM cutover_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise StatusUnreadableError("상태 저장소를 읽지 못했다") from error
