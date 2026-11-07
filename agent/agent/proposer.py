"""Codex CLI 로 수정 제안을 받는다.

Codex 는 임시 디렉터리의 config/target.json 만 본다. 프로세스 환경도 허용 목록만 넘겨서
OpenBao 토큰·DB 비밀번호·Slack 토큰이 AI 쪽으로 가지 않는다. stdin 은 닫는다 (안 닫으면 EOF 를 기다리며 멈춘다).

실행 방식이 둘이다.
  local    이 프로세스가 --sandbox workspace-write 로 실행한다. Mac·개발용.
  sidecar  파드용. Codex 샌드박스는 Linux 에서 bubblewrap·user namespace·/proc 마운트가 필요해 제한된 파드에서 돌지 않는다.
           그래서 비밀값이 전혀 없는 옆 컨테이너(agent.codex_runner)가 실행하고 컨테이너 자체가 경계가 된다.
           이 프로세스는 공유 폴더에 작업을 놓고 끝나기를 기다린다. 결과 파일은 신뢰하지 않고 검사한다.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from .config import CodexSettings
from .scope import ALLOWED_KEYS, VISIBLE_KEYS, Change, Proposal

log = logging.getLogger("agent.proposer")

REQUEST_FILE, DONE_FILE, SCHEMA_FILE, OUTPUT_FILE, WORKSPACE_DIR = (
    "request.json", "done.json", "schema.json", "last-message.json", "workspace",
)
# 사이드카가 1초마다 고쳐 쓰는 상태 파일. job-* 와 겹치지 않는 이름이다.
STATUS_FILE = ".runner-status.json"
MAX_RESULT_BYTES = 256 * 1024

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["changes", "cause", "impact", "verification"],
    "properties": {
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "value"],
                "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
            },
        },
        "cause": {"type": "string"},
        "impact": {"type": "string"},
        "verification": {"type": "string"},
    },
}

# 값이 아니라 이름만 정한다. 비밀값은 여기 없다.
_ENV_ALLOWLIST: tuple[str, ...] = ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "TMPDIR", "CODEX_API_KEY")


class ProposalInvalid(ValueError):
    """Codex 출력이 고정 스키마를 어겼다."""


class ProposerError(RuntimeError):
    """Codex 실행 실패 (없음, 시간 초과, 비정상 종료)."""


class Proposer(Protocol):
    def propose(
        self, *, current: Mapping[str, Any], reasons: Sequence[Mapping[str, Any]], target_db: Mapping[str, Any]
    ) -> tuple[Proposal, dict[str, str]]:
        """(제안, 실제 파일에서 바뀐 키와 값) 을 돌려준다."""
        ...


def visible_config(current: Mapping[str, Any]) -> dict[str, str]:
    return {k: str(current[k]) for k in VISIBLE_KEYS if k in current}


def parse_output(text: str) -> Proposal:
    """Codex 최종 메시지를 엄격히 검사한다. 모르는 필드와 타입 오류는 거절한다."""
    try:
        doc = json.loads(text)
    except ValueError:
        raise ProposalInvalid("출력이 JSON 이 아니다") from None
    if not isinstance(doc, dict):
        raise ProposalInvalid("출력이 객체가 아니다")
    if set(doc) != {"changes", "cause", "impact", "verification"}:
        raise ProposalInvalid("출력 필드가 스키마와 다르다")
    for name in ("cause", "impact", "verification"):
        if not isinstance(doc[name], str) or not doc[name].strip():
            raise ProposalInvalid(f"{name} 는 비어 있지 않은 문자열이어야 한다")
    raw_changes = doc["changes"]
    if not isinstance(raw_changes, list):
        raise ProposalInvalid("changes 는 배열이어야 한다")
    changes: list[Change] = []
    for item in raw_changes:
        if not isinstance(item, dict) or set(item) != {"key", "value"}:
            raise ProposalInvalid("changes 항목은 key, value 만 가진다")
        if not isinstance(item["key"], str) or not isinstance(item["value"], str):
            raise ProposalInvalid("changes 의 key, value 는 문자열이다")
        changes.append(Change(item["key"], item["value"]))
    return Proposal(tuple(changes), doc["cause"], doc["impact"], doc["verification"])


def build_prompt(reasons: Sequence[Mapping[str, Any]], target_db: Mapping[str, Any]) -> str:
    return "\n".join([
        "온프렘 sample-app 이 대상 DB 에 연결하지 못해 이전 정책(OPA)이 막았다. 설정 파일을 고쳐라.",
        "",
        "[OPA 거절 사유]",
        json.dumps([{"code": r.get("code"), "message": r.get("message")} for r in reasons], ensure_ascii=False),
        "",
        "[계획서상 대상 DB (plan.target_db)]",
        json.dumps({"host": target_db["host"], "port": target_db["port"]}),
        "",
        "[작업]",
        "- ./config/target.json 을 읽고, 계획서와 다른 값을 계획서 값으로 고쳐 저장한다.",
        f"- 바꿔도 되는 키는 {', '.join(sorted(ALLOWED_KEYS))} 뿐이다. 다른 키와 다른 파일은 건드리지 않는다.",
        "- 네트워크 접속이나 명령 실행으로 원인을 더 찾지 않는다. 이 파일과 위 사유만 근거로 삼는다.",
        "- 값은 모두 문자열이다. DB_PORT 도 문자열로 둔다.",
        "",
        "[최종 응답]",
        "지정된 JSON 스키마로만 답한다. changes 에는 실제로 바꾼 키와 새 값만 넣는다. 바꿀 것이 없으면 빈 배열로 둔다.",
        "cause 는 원인, impact 는 이 변경의 영향, verification 은 검증 방법을 한국어 한두 문장으로 쓴다.",
    ])


def codex_command(job_dir: Path, prompt: str, *, bin_path: str, sandbox: str, model: str | None) -> list[str]:
    """인자는 여기서만 만든다. 사이드카도 이 함수를 쓰고, 요청 파일에서 임의의 인자를 받지 않는다."""
    cmd = [
        bin_path, "exec",
        "--sandbox", sandbox,
        "-c", "approval_policy='never'",
        "--ephemeral",
        "--ignore-user-config",
        "--skip-git-repo-check",  # 임시 디렉터리는 git 저장소가 아니다
        "--output-schema", str(job_dir / SCHEMA_FILE),
        "--output-last-message", str(job_dir / OUTPUT_FILE),
        "-C", str(job_dir / WORKSPACE_DIR),
    ]
    if model:
        cmd += ["--model", model]
    return [*cmd, prompt]


def run_codex(cmd: list[str], *, cwd: Path, env: Mapping[str, str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - 인자는 codex_command 가 고정, 셸을 거치지 않는다
        cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, cwd=cwd,
        env=dict(env), timeout=timeout, check=False,
    )


def child_env(source: Mapping[str, str], home: Path, codex_home: str | None = None) -> dict[str, str]:
    child = {k: source[k] for k in _ENV_ALLOWLIST if k in source}
    child["HOME"] = str(home)
    if codex_home:
        child["CODEX_HOME"] = os.path.expanduser(codex_home)
    return child


# OpenAI 401 메시지는 키 앞뒤를 남겨 "sk-abc***...1234" 처럼 돌려준다. 전체 키 치환으로는 잡히지 않는다.
# 짧은 키는 별표가 2개뿐이고 ("abcd**-key"), 메시지 형식이 바뀌어도 "provided:" 뒤는 지운다.
_MASKED_KEY_RE = re.compile(r"[^\s:,;'\"]*\*{2,}[^\s:,;.'\"]*")
_PROVIDED_KEY_RE = re.compile(r"(API key provided:\s*)[^\s.,;]+", re.IGNORECASE)


def scrub(text: str, source: Mapping[str, str]) -> str:
    key = source.get("CODEX_API_KEY")
    if key:
        text = text.replace(key, "***")
    return _PROVIDED_KEY_RE.sub(r"\1***", _MASKED_KEY_RE.sub("***", text))


@dataclass(frozen=True)
class CodexJob:
    job_dir: Path
    prompt: str


class LocalCodexRunner:
    def __init__(self, settings: CodexSettings, *, env: Mapping[str, str] | None = None) -> None:
        self.settings = settings
        self._env = os.environ if env is None else env

    def new_job_dir(self) -> Path:
        return Path(tempfile.mkdtemp(prefix="agent-codex-"))

    def run(self, job: CodexJob) -> None:
        home = job.job_dir / "home"
        home.mkdir()
        cmd = codex_command(job.job_dir, job.prompt, bin_path=self.settings.bin, sandbox="workspace-write", model=self.settings.model)
        try:
            proc = run_codex(cmd, cwd=job.job_dir / WORKSPACE_DIR, env=child_env(self._env, home, self.settings.home),
                             timeout=self.settings.timeout)
        except subprocess.TimeoutExpired:
            raise ProposerError(f"codex 시간 초과 ({self.settings.timeout:.0f}초)") from None
        except OSError as exc:
            raise ProposerError(f"codex 실행 실패: {type(exc).__name__}") from None
        if proc.returncode != 0:
            log.error("codex 비정상 종료 code=%s stderr=%s", proc.returncode, scrub(proc.stderr, self._env)[-500:])
            raise ProposerError(f"codex 종료 코드 {proc.returncode}")


class SidecarCodexRunner:
    """공유 폴더에 작업을 놓고 agent.codex_runner 가 끝내기를 기다린다."""

    def __init__(self, settings: CodexSettings, *, poll_interval: float = 0.5, stale_after: float = 30.0) -> None:
        if not settings.shared_dir:
            raise ProposerError("codex.runner=sidecar 에는 codex.shared_dir 가 필요하다")
        self.settings, self.poll_interval, self.stale_after = settings, poll_interval, stale_after
        self.shared_dir = Path(settings.shared_dir)

    def new_job_dir(self) -> Path:
        return Path(tempfile.mkdtemp(prefix="job-", dir=self.shared_dir))

    def run(self, job: CodexJob) -> None:
        # 작업을 다 쓴 뒤 이름을 바꿔 사이드카가 반쯤 쓴 요청을 집지 않게 한다.
        pending = job.job_dir / (REQUEST_FILE + ".tmp")
        pending.write_text(json.dumps({"prompt": job.prompt, "model": self.settings.model}), encoding="utf-8")
        pending.rename(job.job_dir / REQUEST_FILE)

        code = self._wait(job.job_dir)
        if code != 0:
            raise ProposerError(f"codex 종료 코드 {code}")

    def _wait(self, job_dir: Path) -> Any:
        """종료 코드를 돌려준다. 결과 파일이 없어도 상태 파일의 기록이나 heartbeat 정지로 바로 끝낸다."""
        deadline = time.monotonic() + self.settings.timeout
        done = job_dir / DONE_FILE
        last_beat, last_change = None, time.monotonic()
        while True:
            if done.exists():
                try:
                    return json.loads(read_untrusted(done, job_dir))["exit_code"]
                except (ProposalInvalid, ValueError, KeyError, TypeError):
                    raise ProposerError("codex 사이드카 결과 형식이 틀렸다") from None
            status = self._read_status()
            finished = status.get("finished")
            if isinstance(finished, dict) and job_dir.name in finished:
                log.warning("%s 가 없지만 사이드카가 종료를 기록했다: %s", DONE_FILE, job_dir.name)
                return finished[job_dir.name]
            if status.get("beat") != last_beat:
                last_beat, last_change = status.get("beat"), time.monotonic()
            now = time.monotonic()
            if now - last_change >= self.stale_after:
                raise ProposerError(f"codex 사이드카 응답 없음 ({self.stale_after:.0f}초)")
            if now >= deadline:
                raise ProposerError(f"codex 사이드카 시간 초과 ({self.settings.timeout:.0f}초)")
            time.sleep(self.poll_interval)

    def _read_status(self) -> dict[str, Any]:
        try:
            status = json.loads(read_untrusted(self.shared_dir / STATUS_FILE, self.shared_dir))
        except (ProposalInvalid, ValueError):  # 아직 없거나 깨졌다. heartbeat 정지로 판단한다
            return {}
        return status if isinstance(status, dict) else {}


SANDBOX_JOB_DIR = PurePosixPath("/work/job")
SANDBOX_HOME = "/tmp/codex-home"


def _sdk_sandbox(settings: CodexSettings, env: Mapping[str, str]) -> Any:
    try:
        from opensandbox import SandboxSync
        from opensandbox.config import ConnectionConfigSync
    except ImportError:
        raise ProposerError("opensandbox 패키지가 없다 (pip install 'agent[opensandbox]')") from None
    url = urlsplit(settings.sandbox_url or "")
    if url.scheme not in ("http", "https") or not url.netloc:
        raise ProposerError("codex.sandbox_url 형식이 틀렸다")
    config = ConnectionConfigSync(
        domain=url.netloc, protocol=url.scheme, api_key=env.get(settings.sandbox_api_key_env) or None,
        request_timeout=timedelta(seconds=60),
    )
    return SandboxSync.create(
        settings.sandbox_image, connection_config=config, timeout=timedelta(seconds=settings.timeout + 120),
        resource={"cpu": settings.sandbox_cpu, "memory": settings.sandbox_memory},
    )


class OpenSandboxCodexRunner:
    def __init__(
        self, settings: CodexSettings, *, env: Mapping[str, str] | None = None,
        create: Callable[[CodexSettings, Mapping[str, str]], Any] = _sdk_sandbox,
    ) -> None:
        if not settings.sandbox_url:
            raise ProposerError("codex.runner=opensandbox 에는 codex.sandbox_url 이 필요하다")
        self.settings = settings
        self._env = os.environ if env is None else env
        self._create = create

    def new_job_dir(self) -> Path:
        return Path(tempfile.mkdtemp(prefix="agent-codex-"))

    def run(self, job: CodexJob) -> None:
        key = self._env.get("CODEX_API_KEY")
        if not key:
            raise ProposerError("CODEX_API_KEY 가 없다")
        try:
            sandbox = self._create(self.settings, self._env)
        except ProposerError:
            raise
        except Exception as exc:
            raise ProposerError(f"샌드박스를 만들지 못했다: {type(exc).__name__}") from None
        try:
            self._run_in(sandbox, job, key)
        finally:
            for close in ("kill", "close"):
                try:
                    getattr(sandbox, close)()
                except Exception as exc:
                    log.warning("샌드박스 %s 실패: %s", close, type(exc).__name__)

    def _run_in(self, sandbox: Any, job: CodexJob, key: str) -> None:
        uid = self.settings.sandbox_uid
        remote = SANDBOX_JOB_DIR
        workspace = remote / WORKSPACE_DIR
        target = PurePosixPath("config") / "target.json"
        try:
            from opensandbox.models.execd import RunCommandOpts

            self._ok(sandbox.commands.run(["mkdir", "-p", str(workspace / "config"), SANDBOX_HOME]), "작업 폴더")
            sandbox.files.write_file(str(remote / SCHEMA_FILE), (job.job_dir / SCHEMA_FILE).read_text(encoding="utf-8"))
            sandbox.files.write_file(
                str(workspace / target), (job.job_dir / WORKSPACE_DIR / target).read_text(encoding="utf-8"),
            )
            self._ok(sandbox.commands.run(["chown", "-R", f"{uid}:{uid}", str(remote), SANDBOX_HOME]), "권한")
            cmd = codex_command(Path(str(remote)), job.prompt, bin_path=self.settings.bin,
                                sandbox="danger-full-access", model=self.settings.model)
            execution = sandbox.commands.run(cmd, opts=RunCommandOpts(
                working_directory=str(workspace), timeout=timedelta(seconds=self.settings.timeout),
                uid=uid, gid=uid, envs={"CODEX_API_KEY": key, "HOME": SANDBOX_HOME},
            ))
        except ProposerError:
            raise
        except Exception as exc:
            raise ProposerError(f"샌드박스 실행 실패: {type(exc).__name__}") from None
        if execution.exit_code != 0:
            stderr = "".join(m.text for m in execution.logs.stderr)
            log.error("codex 비정상 종료 code=%s stderr=%s", execution.exit_code, scrub(stderr, self._env)[-500:])
            raise ProposerError(f"codex 종료 코드 {execution.exit_code}")
        self._fetch(sandbox, remote / OUTPUT_FILE, job.job_dir / OUTPUT_FILE)
        self._fetch(sandbox, workspace / target, job.job_dir / WORKSPACE_DIR / target)

    @staticmethod
    def _ok(execution: Any, step: str) -> None:
        if execution.exit_code != 0:
            raise ProposerError(f"샌드박스 {step} 준비 실패 (종료 코드 {execution.exit_code})")

    @staticmethod
    def _fetch(sandbox: Any, remote: PurePosixPath, local: Path) -> None:
        try:
            data = sandbox.files.read_bytes(str(remote), limit=MAX_RESULT_BYTES + 1)
        except Exception:
            local.unlink(missing_ok=True)
            return
        local.write_bytes(data)


class CodexProposer:
    def __init__(self, settings: CodexSettings, *, env: Mapping[str, str] | None = None, runner: Any = None) -> None:
        self.settings = settings
        self.runner = runner or LocalCodexRunner(settings, env=env)

    def propose(
        self, *, current: Mapping[str, Any], reasons: Sequence[Mapping[str, Any]], target_db: Mapping[str, Any]
    ) -> tuple[Proposal, dict[str, str]]:
        original = visible_config(current)
        job_dir = self.runner.new_job_dir()
        try:
            workspace = job_dir / WORKSPACE_DIR
            (workspace / "config").mkdir(parents=True)
            target = workspace / "config" / "target.json"
            target.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")
            (job_dir / SCHEMA_FILE).write_text(json.dumps(OUTPUT_SCHEMA), encoding="utf-8")
            self.runner.run(CodexJob(job_dir, build_prompt(reasons, target_db)))
            output = job_dir / OUTPUT_FILE
            if not output.exists():
                raise ProposalInvalid("codex 가 최종 메시지를 남기지 않았다")
            return parse_output(read_untrusted(output, job_dir)), _file_changes(original, target, job_dir)
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)


def build_proposer(settings: CodexSettings) -> CodexProposer:
    if settings.runner == "sidecar":
        # 로그인 정보가 없는 컨테이너에서 모델을 안 주면 Codex 내장 기본 모델로 가는데, 그때 도구 목록이 비어
        # 파일을 읽지 못하고 빈 제안만 낸다 (codex-cli 0.154 에서 확인). 조용히 실패하지 않게 시작에서 막는다.
        if not settings.model:
            raise ProposerError("codex.runner=sidecar 에는 codex.model 이 필요하다")
        return CodexProposer(settings, runner=SidecarCodexRunner(settings))
    if settings.runner == "opensandbox":
        if not settings.model:
            raise ProposerError("codex.runner=opensandbox 에는 codex.model 이 필요하다")
        return CodexProposer(settings, runner=OpenSandboxCodexRunner(settings))
    if settings.runner != "local":
        raise ProposerError(f"알 수 없는 codex.runner: {settings.runner}")
    return CodexProposer(settings)


def read_untrusted(path: Path, root: Path) -> str:
    """AI 쪽이 쓴 파일을 읽는다. 심볼릭 링크로 이 컨테이너의 토큰 파일을 읽게 만드는 경로를 막는다."""
    try:
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ProposalInvalid(f"{path.name} 가 작업 폴더 밖을 가리킨다")
        with path.open(encoding="utf-8") as fh:
            text = fh.read(MAX_RESULT_BYTES + 1)
    except (OSError, UnicodeDecodeError):
        raise ProposalInvalid(f"{path.name} 를 읽지 못했다") from None
    if len(text) > MAX_RESULT_BYTES:
        raise ProposalInvalid(f"{path.name} 가 너무 크다")
    return text


def _file_changes(original: Mapping[str, str], target: Path, root: Path) -> dict[str, str]:
    """AI 가 파일을 실제로 어떻게 바꿨는지. 보고와 대조하는 근거다. 지운 키도 변경으로 잡는다."""
    text = read_untrusted(target, root)
    try:
        edited = json.loads(text)
    except ValueError:
        raise ProposalInvalid("config/target.json 이 JSON 이 아니다") from None
    if not isinstance(edited, dict):
        raise ProposalInvalid("config/target.json 이 객체가 아니다")
    changes = {str(k): str(v) for k, v in edited.items() if original.get(k) != v}
    changes.update({k: "<deleted>" for k in original if k not in edited})
    return changes
