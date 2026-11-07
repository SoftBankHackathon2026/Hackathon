"""terraform init·plan·apply·destroy 를 감싸는 얇은 실행기.

루트 모듈 하나(작업 디렉토리)를 변수 파일 하나와 state 파일 하나로 돌린다.
루트 모듈은 `terraform { backend "local" {} }` 를 선언해야 한다 — state 경로는
init 때 -backend-config 로 넘긴다. provider 캐시·백엔드 설정(TF_DATA_DIR)과
plan 파일은 state 파일 옆에 state 파일 이름 전체 + 접미사로 둬서, 같은 모듈을
실행(run)마다 따로 돌려도 서로 섞이지 않는다. 셸에서 물려받은 TF_* 환경
변수(TF_WORKSPACE·TF_CLI_ARGS·TF_VAR_* 등)는 실행을 다른 state·인자로 돌릴 수
있어 넘기지 않는다.

  runner = terraform.Terraform(
      working_dir=module_dir, var_file=var_file, state_path=state_path)
  runner.init()
  changes = runner.plan()
  runner.apply()
  remaining = runner.destroy()

비밀값을 밖으로 내지 않는다. terraform 의 표준 출력(plan 본문)과 `show -json`
원문은 버리고, 자원 주소·종류·동작만 돌려준다. `output -json` 은 sensitive 로
표시된 값을 빼고 돌려준다. 실패해도 표준 출력·표준 오류의 내용은 예외에 담지
않는다 — provider 오류 문구에 비밀번호·토큰이 섞일 수 있어서다. 원인은 같은
명령을 사람이 직접 돌려 확인한다. 변수 값은 명령줄(-var)로 넘기지 않고 변수
파일로만 넘긴다.
"""

import dataclasses
import json
import os
import pathlib
import subprocess
from collections.abc import Callable
from typing import Any

# 셸에서 물려받아도 되는 TF_* 환경 변수. 실행 대상을 바꾸지 않고 provider·모듈
# 내려받기 방식과 레지스트리 인증(TF_TOKEN_<호스트>)만 정한다. 그 밖의 TF_* 는
# 넘기지 않는다.
_INHERITABLE_TF_ENV = frozenset({"TF_PLUGIN_CACHE_DIR", "TF_CLI_CONFIG_FILE"})
_INHERITABLE_TF_ENV_PREFIX = "TF_TOKEN_"

Run = Callable[..., subprocess.CompletedProcess[str]]


class TerraformError(RuntimeError):
    """terraform 명령이 실패했거나 결과를 읽을 수 없다.

    Attributes:
        subcommand: 실패한 하위 명령(init·plan·apply 등).
        returncode: 종료 코드. 결과를 읽지 못한 경우는 0 일 수 있다.
    """

    def __init__(self, subcommand: str, returncode: int, detail: str) -> None:
        """예외를 만든다. detail 에는 terraform 출력 원문을 넣지 않는다."""
        super().__init__(
            f"terraform {subcommand} 실패(종료 코드 {returncode}): {detail}"
        )
        self.subcommand = subcommand
        self.returncode = returncode


@dataclasses.dataclass(frozen=True)
class ResourceChange:
    """plan 이 바꿀 자원 하나. 값(before·after)은 담지 않는다.

    Attributes:
        address: 자원 주소(예: google_sql_database_instance.db).
        resource_type: 자원 종류(예: google_sql_database_instance).
        actions: terraform 동작 목록(create·update·delete·read·no-op).
    """

    address: str
    resource_type: str
    actions: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Terraform:
    """루트 모듈 하나를 정해진 변수 파일·state 경로로 돌리는 실행기.

    Attributes:
        working_dir: 루트 모듈 디렉토리.
        var_file: 변수 파일(.tfvars 또는 .tfvars.json).
        state_path: local 백엔드 state 파일 경로.
        binary: terraform 실행 파일.
        run: 명령 실행 함수. 시험에서 바꿔 끼운다.
    """

    working_dir: pathlib.Path
    var_file: pathlib.Path
    state_path: pathlib.Path
    binary: str = "terraform"
    run: Run = subprocess.run

    def init(self) -> None:
        """필요한 provider 를 받고 local 백엔드를 state_path 로 맞춘다."""
        self._call(
            "init",
            "-input=false",
            "-no-color",
            f"-backend-config=path={self._state()}",
        )

    def plan(self) -> tuple[ResourceChange, ...]:
        """계획(plan) 파일을 저장하고, 바뀔 자원의 주소·종류·동작을 돌려준다.

        plan 이나 결과 읽기가 실패하면 plan 파일(변수 값 평문)을 지운다.

        Returns:
            state 와 설정에 있는 자원마다 하나. 바뀌지 않는 자원도
            actions=("no-op",) 으로 들어 있다. state·설정 모두 비었으면 빈
            tuple.

        Raises:
            TerraformError: plan·show 가 실패했거나 show 결과를 읽지 못할 때.
        """
        plan_file = self._plan_file()
        try:
            self._call(
                "plan",
                "-input=false",
                "-no-color",
                f"-var-file={self._var_file()}",
                f"-out={plan_file}",
            )
            shown = self._call("show", "-json", "-no-color", str(plan_file))
            return _resource_changes(_load_json("show", shown.stdout))
        except BaseException:
            # 종료 코드 오류만이 아니라 실행 자체 실패(OSError 등)도 포함한다.
            plan_file.unlink(missing_ok=True)
            raise

    def apply(self) -> None:
        """직전 plan 이 저장한 plan 파일을 그대로 적용하고 그 파일을 지운다.

        plan 파일에는 변수 값이 평문으로 들어 있어, 성공·실패와 관계없이
        적용 뒤 지운다.

        Raises:
            TerraformError: 저장된 plan 이 없거나 apply 가 실패할 때.
        """
        plan_file = self._plan_file()
        if not plan_file.exists():
            raise TerraformError("apply", 0, "저장된 plan 이 없다 — 먼저 plan")
        try:
            self._call("apply", "-input=false", "-no-color", str(plan_file))
        finally:
            plan_file.unlink(missing_ok=True)

    def destroy(self) -> tuple[str, ...]:
        """모든 자원을 지우고, state 에 남은 자원 주소를 돌려준다.

        Returns:
            destroy 뒤 state 에 남은 주소. 다 지웠으면 빈 tuple.

        Raises:
            TerraformError: destroy·state list 가 실패할 때.
        """
        self._call(
            "destroy",
            "-auto-approve",
            "-input=false",
            "-no-color",
            f"-var-file={self._var_file()}",
        )
        listed = self._call("state", "list")
        return tuple(line for line in listed.stdout.splitlines() if line)

    def outputs(self) -> dict[str, Any]:
        """출력값 가운데 sensitive 가 아닌 것만 이름→값으로 돌려준다.

        Raises:
            TerraformError: output 이 실패했거나 결과를 읽지 못할 때.
        """
        shown = self._call("output", "-json", "-no-color")
        document = _load_json("output", shown.stdout)
        return {
            name: output["value"]
            for name, output in document.items()
            if not output.get("sensitive", False)
        }

    def _call(self, *args: str) -> subprocess.CompletedProcess[str]:
        command = [self.binary, f"-chdir={self._working_dir()}", *args]
        env = {
            **{
                name: value
                for name, value in os.environ.items()
                if _inheritable(name)
            },
            "TF_DATA_DIR": str(self._sibling(".terraform")),
            "TF_IN_AUTOMATION": "1",
        }
        result = self.run(
            command, env=env, capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            stderr_lines = len(result.stderr.strip().splitlines())
            raise TerraformError(
                args[0],
                result.returncode,
                f"표준 오류 {stderr_lines}줄(비밀값이 섞일 수 있어 싣지 않음)",
            )
        return result

    def _working_dir(self) -> pathlib.Path:
        return self.working_dir.resolve()

    def _var_file(self) -> pathlib.Path:
        return self.var_file.resolve()

    def _state(self) -> pathlib.Path:
        return self.state_path.resolve()

    def _plan_file(self) -> pathlib.Path:
        return self._sibling(".tfplan")

    def _sibling(self, suffix: str) -> pathlib.Path:
        """State 파일 이름 전체 뒤에 접미사를 붙인 같은 자리의 경로."""
        state = self._state()
        return state.with_name(state.name + suffix)


def _resource_changes(document: dict[str, Any]) -> tuple[ResourceChange, ...]:
    """Show -json 문서의 resource_changes 를 읽는다. 값은 예외에 넣지 않는다."""
    try:
        return tuple(
            ResourceChange(
                address=change["address"],
                resource_type=change["type"],
                actions=tuple(change["change"]["actions"]),
            )
            for change in document.get("resource_changes", ())
        )
    except (KeyError, TypeError):
        raise TerraformError(
            "show", 0, "resource_changes 형식이 예상과 다르다"
        ) from None


def _inheritable(name: str) -> bool:
    """셸 환경 변수 name 을 terraform 실행에 물려줘도 되는지."""
    return (
        not name.startswith("TF_")
        or name in _INHERITABLE_TF_ENV
        or name.startswith(_INHERITABLE_TF_ENV_PREFIX)
    )


def _load_json(subcommand: str, text: str) -> dict[str, Any]:
    """JSON 형식(-json) 출력을 읽는다. 실패해도 원문은 예외에 넣지 않는다."""
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise TerraformError(
            subcommand, 0, f"JSON 이 아니다(위치 {error.pos})"
        ) from None
    if not isinstance(document, dict):
        raise TerraformError(subcommand, 0, "JSON 객체가 아니다")
    return document
