"""원가연 전환 job 의 단계 읽기와 배타 잠금 — 읽기만 한다.

ingress-start 처럼 원가연 `migration.cloud run` 과 겹치면 안 되는 명령이 쓴다.

  * 잠금: 원가연과 같은 `<state-dir>/cloud.lock` 을 같은 방식(fcntl.flock
    LOCK_EX|LOCK_NB, migration/cloud/__main__.py)으로 잡는다. 못 잡으면 `run`
    이 도는 중이므로 거부한다. 잠금 파일은 지우지 않는다.
  * 단계: `<state-dir>/cutover.sqlite3` 를 읽기 전용(sqlite3 URI mode=ro)으로
    열어 `cutover_jobs` 한 줄만 SELECT 한다. 원가연 CutoverStore 생성자는 WAL
    설정·스키마 생성을 하므로 쓰지 않고, CLI `status` 는 같은 잠금을 잡으므로
    쓰지 않는다(설계 2.3 read_cutover_stage, 원가연 답 ⑥).

  with cutover_stage.cloud_lock(state_dir):
      stage, failed = cutover_stage.read_cutover_stage(job_id, state_dir)
      if not cutover_stage.cutover_not_begun(stage, failed): ...
"""

import collections.abc
import contextlib
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
# 승인(run --approve) 전 단계. 원본 DB 가 아직 막히지 않았다.
_BEFORE_APPROVAL = frozenset({"not_started", "PRECHECK", "COPYING", "READY"})
# 원가연 migration/state.py 의 파일 이름.
_STORE_FILE = "cutover.sqlite3"
_LOCK_FILE = "cloud.lock"


class StageUnreadableError(RuntimeError):
    """단계를 읽지 못했다(디렉토리·형식·값). 부르는 쪽은 거부로 다룬다."""


class NoCutoverStateError(StageUnreadableError):
    """상태 파일이 아직 없다. 명시적으로 허용할 때만 not_started 로 본다."""


class CloudLockBusyError(RuntimeError):
    """migration.cloud 가 cloud.lock 을 쥐고 실행 중이다."""


def read_cutover_stage(
    job_id: str, state_dir: pathlib.Path
) -> tuple[CutoverStage, CutoverStage | None]:
    """원가연 상태 저장소에서 (단계, FAILED 면 실패한 단계)를 읽는다.

    Args:
        job_id: config.cloud.yaml 의 job.job_id.
        state_dir: migration.cloud 의 --state-dir.

    Returns:
        job 행이 없으면 ("not_started", None).

    Raises:
        NoCutoverStateError: 상태 파일이 없을 때.
        StageUnreadableError: 디렉토리가 없거나 형식·값이 다를 때.
    """
    if not state_dir.is_dir():
        raise StageUnreadableError(f"state-dir 가 없다: {state_dir}")
    store = state_dir / _STORE_FILE
    if not store.exists():
        raise NoCutoverStateError(f"원가연 상태 파일이 없다: {store}")
    row = _select_job(store, job_id)
    if row is None:
        return "not_started", None
    stage, checkpoint_text = row
    if stage not in _STAGES:
        raise StageUnreadableError(f"모르는 단계: {stage!r}")
    if stage != "FAILED":
        return stage, None
    try:
        failed_stage = json.loads(checkpoint_text).get("failed_stage")
    except (ValueError, AttributeError) as error:
        raise StageUnreadableError("checkpoint 를 읽지 못했다") from error
    if failed_stage not in _STAGES:
        raise StageUnreadableError(f"FAILED 의 failed_stage: {failed_stage!r}")
    return stage, failed_stage


@contextlib.contextmanager
def cloud_lock(state_dir: pathlib.Path) -> collections.abc.Iterator[None]:
    """migration.cloud 와 같은 잠금을 잡는다. 블록이 끝나면 푼다.

    Raises:
        StageUnreadableError: state-dir 가 없을 때. 경로 오타를 새 job 으로
            오인하지 않게 새로 만들지 않는다.
        CloudLockBusyError: 다른 프로세스가 쥐고 있을 때.
    """
    if not state_dir.is_dir():
        raise StageUnreadableError(f"state-dir 가 없다: {state_dir}")
    with (state_dir / _LOCK_FILE).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise CloudLockBusyError(
                "migration.cloud 가 실행 중이다(cloud.lock)"
            ) from error
        yield


def cutover_not_begun(
    stage: CutoverStage, failed_stage: CutoverStage | None
) -> bool:
    """실효 단계(FAILED 면 failed_stage)가 승인 전인가.

    FAILED(FENCING) 은 거짓이다 — 접수 큐가 멈췄고 원본이 막혔을 수 있다.
    """
    effective = failed_stage if stage == "FAILED" else stage
    return effective in _BEFORE_APPROVAL


def _select_job(store: pathlib.Path, job_id: str) -> tuple[str, str] | None:
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
        raise StageUnreadableError("상태 저장소를 읽지 못했다") from error
