"""단계가 밖과 닿는 길 — 실제 실행(RealRunner)과 계획 출력(DryRunner).

단계 코드는 이 둘 중 무엇이 오든 같은 순서로 부른다. dry-run 은 명령과
Terraform 변수를 찍기만 하고, 클라우드·DB·상태 파일을 건드리지 않는다. 아직
없는 출력은 이름이 드러나는 자리표시(Placeholder)로 돌려준다.

명령줄 인자에는 비밀값을 넣지 않는다(단계가 지킨다). 그래서 명령을 그대로
찍는다. 비밀값이 오가는 파이프(레지스트리 로그인)는 pipe 로만 잇는다.
"""

import json
import os
import pathlib
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol, TypeVar

from crosscloud.provision import terraform

T = TypeVar("T")
Echo = Callable[[str], None]


class StepError(RuntimeError):
    """단계가 멈췄다. 메시지는 사람이 읽는 한 줄이고 비밀값이 없다."""


class Placeholder(str):
    """dry-run 에서 아직 없는 값. 키로 더 들어가면 경로가 늘어난다."""

    def __new__(cls, path: str) -> "Placeholder":
        """`<경로>` 문자열을 만든다."""
        made = super().__new__(cls, f"<{path}>")
        made.path = path
        return made

    def __getitem__(self, key: object) -> "Placeholder":  # type: ignore[override]
        """하위 출력 자리표시."""
        return Placeholder(f"{self.path}.{key}")

    def items(self) -> tuple[()]:
        """출력 전체를 훑는 자리에서는 빈 출력으로 본다."""
        return ()


class Runner(Protocol):
    """단계가 쓰는 바깥 동작."""

    def sh(
        self,
        argv: Sequence[str],
        *,
        capture: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> str:
        """명령을 돌린다. capture 면 표준 출력을, env 는 더할 환경 변수."""

    def pipe(self, producer: Sequence[str], consumer: Sequence[str]) -> None:
        """Producer 표준 출력을 consumer 표준 입력으로 잇는다(값은 안 본다)."""

    def call(self, title: str, action: Callable[[], T], placeholder: T) -> T:
        """프로세스 안 동작(DB·Secret API)을 부른다."""

    def apply(
        self, stack: str, module: pathlib.Path, tfvars: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Terraform 스택을 적용하고 출력을 돌려준다."""

    def destroy(self, stack: str, module: pathlib.Path) -> tuple[str, ...]:
        """Terraform 스택을 지우고 state 에 남은 주소를 돌려준다."""

    def write(self, path: pathlib.Path, text: str) -> None:
        """상태 파일을 쓴다."""


class RealRunner:
    """실제로 실행한다."""

    def __init__(
        self,
        state: pathlib.Path,
        echo: Echo = print,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        """상태 디렉터리(Terraform state·변수·출력)와 출력 함수를 받는다."""
        self._tf = state / "tf"
        self._echo = echo
        self._run = run

    def sh(
        self,
        argv: Sequence[str],
        *,
        capture: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> str:
        """Runner.sh. env 값은 찍지 않는다."""
        self._echo("$ " + shlex.join(argv))
        result = self._run(
            list(argv),
            capture_output=capture,
            text=True,
            check=False,
            env=None if env is None else {**os.environ, **env},
        )
        if result.returncode != 0:
            raise StepError(f"{argv[0]} 실패(종료 코드 {result.returncode})")
        return result.stdout if capture else ""

    def pipe(self, producer: Sequence[str], consumer: Sequence[str]) -> None:
        """Runner.pipe."""
        self._echo(f"$ {shlex.join(producer)} | {shlex.join(consumer)}")
        with subprocess.Popen(list(producer), stdout=subprocess.PIPE) as first:
            second = subprocess.run(
                list(consumer), stdin=first.stdout, check=False
            )
            first.stdout.close()  # type: ignore[union-attr]
        if first.returncode != 0 or second.returncode != 0:
            raise StepError(f"{producer[0]} | {consumer[0]} 실패")

    def call(self, title: str, action: Callable[[], T], placeholder: T) -> T:
        """Runner.call."""
        del placeholder
        self._echo(f"· {title}")
        return action()

    def apply(
        self, stack: str, module: pathlib.Path, tfvars: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Runner.apply. 변수·출력은 상태 디렉터리에 남긴다(비밀값 없음)."""
        self._echo(f"$ terraform apply {stack} ({module})")
        self._tf.mkdir(parents=True, exist_ok=True)
        var_file = self._tf / f"{stack}.tfvars.json"
        var_file.write_text(json.dumps(tfvars, indent=2))
        runner = self._terraform(stack, module)
        runner.init()
        changes = runner.plan()
        self._echo(f"  계획: 자원 {len(changes)}개")
        runner.apply()
        outputs = runner.outputs()
        (self._tf / f"{stack}.outputs.json").write_text(
            json.dumps(outputs, indent=2)
        )
        return outputs

    def destroy(self, stack: str, module: pathlib.Path) -> tuple[str, ...]:
        """Runner.destroy. state 가 없으면(적용한 적 없음) 건너뛴다."""
        if not (self._tf / f"{stack}.tfstate").exists():
            self._echo(f"  {stack}: state 없음 — 건너뜀")
            return ()
        self._echo(f"$ terraform destroy {stack}")
        return self._terraform(stack, module).destroy()

    def write(self, path: pathlib.Path, text: str) -> None:
        """Runner.write."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def _terraform(
        self, stack: str, module: pathlib.Path
    ) -> terraform.Terraform:
        return terraform.Terraform(
            working_dir=module,
            var_file=self._tf / f"{stack}.tfvars.json",
            state_path=self._tf / f"{stack}.tfstate",
        )


class DryRunner:
    """찍기만 한다. 밖을 부르지 않고 파일을 쓰지 않는다."""

    def __init__(self, echo: Echo = print) -> None:
        """출력 함수를 받는다."""
        self._echo = echo

    def sh(
        self,
        argv: Sequence[str],
        *,
        capture: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> str:
        """Runner.sh."""
        del env
        self._echo("$ " + shlex.join(argv))
        return Placeholder(f"{argv[0]} 출력") if capture else ""

    def pipe(self, producer: Sequence[str], consumer: Sequence[str]) -> None:
        """Runner.pipe."""
        self._echo(f"$ {shlex.join(producer)} | {shlex.join(consumer)}")

    def call(self, title: str, action: Callable[[], T], placeholder: T) -> T:
        """Runner.call."""
        del action
        self._echo(f"· {title}")
        return placeholder

    def apply(
        self, stack: str, module: pathlib.Path, tfvars: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Runner.apply. 변수만 찍고 자리표시를 돌려준다."""
        self._echo(f"$ terraform apply {stack} ({module})")
        self._echo("  " + json.dumps(tfvars, ensure_ascii=False, default=str))
        return Placeholder(stack)  # type: ignore[return-value]

    def destroy(self, stack: str, module: pathlib.Path) -> tuple[str, ...]:
        """Runner.destroy."""
        del module
        self._echo(f"$ terraform destroy {stack}")
        return ()

    def write(self, path: pathlib.Path, text: str) -> None:
        """Runner.write."""
        del text
        self._echo(f"  (쓰기) {path}")
