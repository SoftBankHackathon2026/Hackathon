"""Codex 사이드카. 비밀값 없는 컨테이너에서 공유 폴더의 작업을 실행한다.

이 컨테이너에는 CODEX_API_KEY 와 공유 폴더만 있다. OpenBao·DB·Slack 토큰과 ServiceAccount 토큰이 없고
PID 네임스페이스도 나뉘어 에이전트 프로세스를 볼 수 없다. Codex 자체 샌드박스는 제한된 파드에서 돌지 않으므로
(bubblewrap·user namespace 필요) danger-full-access 로 실행하고 이 컨테이너를 경계로 삼는다.

요청 파일에서는 prompt 와 model 만 읽는다. 실행 인자는 proposer.codex_command 가 고정한다.
  python -m agent.codex_runner --dir /work
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Mapping

from .proposer import (
    DONE_FILE, REQUEST_FILE, STATUS_FILE, TRACE_FILE, WORKSPACE_DIR, child_env, codex_command, run_codex, scrub,
)

log = logging.getLogger("agent.codex_runner")
EXIT_TIMEOUT, EXIT_START_FAILED, EXIT_BAD_REQUEST, EXIT_INTERNAL = 124, 127, 2, 70


def _execute(job_dir: Path, *, bin_path: str, timeout: float, env: Mapping[str, str]) -> int:
    try:
        request = json.loads((job_dir / REQUEST_FILE).read_text(encoding="utf-8"))
        prompt, model = request["prompt"], request.get("model")
        if not isinstance(prompt, str) or not (model is None or isinstance(model, str)):
            return EXIT_BAD_REQUEST
    except (OSError, ValueError, KeyError, TypeError):
        return EXIT_BAD_REQUEST
    cmd = codex_command(job_dir, prompt, bin_path=bin_path, sandbox="danger-full-access", model=model)
    with tempfile.TemporaryDirectory(prefix="codex-home-") as home:  # 공유 폴더 밖이다
        try:
            proc = run_codex(cmd, cwd=job_dir / WORKSPACE_DIR, env=child_env(env, Path(home)), timeout=timeout)
        except subprocess.TimeoutExpired:
            log.error("codex 시간 초과 %s", job_dir.name)
            return EXIT_TIMEOUT
        except OSError as exc:
            log.error("codex 실행 실패: %s", type(exc).__name__)
            return EXIT_START_FAILED
    (job_dir / TRACE_FILE).write_text(proc.stdout or "", encoding="utf-8")
    if proc.returncode != 0:
        log.error("codex 비정상 종료 code=%s stderr=%s", proc.returncode, scrub(proc.stderr, env)[-500:])
    return proc.returncode


class RunnerStatus:
    """heartbeat 와 끝난 작업의 종료 코드를 상태 파일에 쓴다. 작업이 도는 동안에도 멈추지 않게 스레드로 돈다.

    done.json 과 별개의 경로다. 에이전트는 둘 중 하나만 보여도 끝나고, heartbeat 가 멈추면 runner 가 죽은 것으로 본다.
    """

    KEEP_FINISHED = 20

    def __init__(self, root: Path) -> None:
        self.path = root / STATUS_FILE
        self._lock = threading.Lock()
        self._beat = 0
        self._finished: dict[str, int] = {}

    def record(self, job: str, code: int) -> None:
        with self._lock:
            self._finished[job] = code
            for old in list(self._finished)[: -self.KEEP_FINISHED]:
                del self._finished[old]
        self.write()

    def write(self) -> None:
        with self._lock:
            self._beat += 1
            body = json.dumps({"beat": self._beat, "finished": self._finished})
            pending = self.path.with_name(self.path.name + ".tmp")
            try:
                pending.write_text(body, encoding="utf-8")
                pending.rename(self.path)
            except OSError as exc:
                log.error("상태 파일 쓰기 실패: %s", type(exc).__name__)

    def start(self, interval: float = 1.0) -> None:
        def loop() -> None:
            while True:
                self.write()
                time.sleep(interval)

        threading.Thread(target=loop, daemon=True, name="runner-status").start()


def _write_done(job_dir: Path, code: int) -> None:
    try:
        pending = job_dir / (DONE_FILE + ".tmp")
        pending.write_text(json.dumps({"exit_code": code}), encoding="utf-8")
        pending.rename(job_dir / DONE_FILE)
    except FileNotFoundError:  # 에이전트가 시간 초과로 폴더를 지웠다
        log.warning("작업 %s 폴더가 이미 없다", job_dir.name)
    except OSError as exc:  # 상태 파일에 종료 코드가 남아 있어 에이전트는 끝난다
        log.error("작업 %s 결과 쓰기 실패: %s", job_dir.name, type(exc).__name__)


def process_pending(
    root: Path, *, bin_path: str, timeout: float, env: Mapping[str, str], status: RunnerStatus | None = None
) -> None:
    for job_dir in sorted(root.glob("job-*")):
        if not (job_dir / REQUEST_FILE).is_file() or (job_dir / DONE_FILE).exists():
            continue
        try:
            code = _execute(job_dir, bin_path=bin_path, timeout=timeout, env=env)
        except Exception as exc:  # noqa: BLE001 - 어떤 실패든 에이전트가 기다리지 않게 실패로 알린다
            log.error("작업 %s 처리 중 오류: %s", job_dir.name, type(exc).__name__)
            code = EXIT_INTERNAL
        log.info("작업 %s 종료 code=%d", job_dir.name, code)
        if status is not None:
            status.record(job_dir.name, code)
        _write_done(job_dir, code)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent.codex_runner", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", default="/work")
    parser.add_argument("--bin", default="codex")
    parser.add_argument("--timeout", type=float, default=290.0, help="에이전트 쪽 codex.timeout 보다 짧게 둔다")
    parser.add_argument("--poll-interval", type=float, default=0.5)
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    root = Path(args.dir)
    status = RunnerStatus(root)
    status.start()
    log.info("대기 시작: %s", root)
    while True:
        try:
            process_pending(root, bin_path=args.bin, timeout=args.timeout, env=os.environ, status=status)
        except OSError as exc:  # 폴더 목록을 못 읽어도 runner 는 죽지 않는다
            log.error("작업 폴더 처리 실패: %s", type(exc).__name__)
        time.sleep(args.poll_interval)


if __name__ == "__main__":
    sys.exit(main())
