"""crosscloud 명령줄 — 전환 연결 명령.

  crosscloud ingress-start --cloud-config config.cloud.yaml \
      --ingress-env ingress.env --ingress-control-url http://127.0.0.1:9092
  crosscloud serve-adapter --cloud-config ... --ingress-env ... \
      --ingress-control-url ...

ingress-start 는 새로 뜬 접수 큐를 원본 라벨로 돌리고 전달을 연다(전환 전 한
번). serve-adapter 는 config.cloud.yaml 의 control_url(127.0.0.1)에서 앱 제어
API 를 연다. 접수 큐 제어 토큰은 INGRESS_CONTROL_TOKEN env 에서만 읽는다.
"""

import argparse
import http.server
import json
import os
import pathlib
import sys

from crosscloud import control_adapter, handoff, ingress_control


def build_parser() -> argparse.ArgumentParser:
    """명령 파서를 만든다."""
    parser = argparse.ArgumentParser(prog="crosscloud")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("ingress-start", "접수 큐를 원본 라벨로 돌리고 전달을 연다"),
        ("serve-adapter", "앱 제어 API 어댑터를 127.0.0.1 에서 연다"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--cloud-config", required=True, type=pathlib.Path)
        command.add_argument("--ingress-env", required=True, type=pathlib.Path)
        command.add_argument("--ingress-control-url", required=True)
        command.add_argument(
            "--target-environment",
            help="대상 앱 DEPLOY_ENV(주면 /target 에서 함께 대조)",
        )
    return parser


def build_adapter(
    args: argparse.Namespace,
) -> tuple[control_adapter.AdapterConfig, control_adapter.ControlAdapter]:
    """인계 파일과 env 로 어댑터 설정과 어댑터를 만든다."""
    config = control_adapter.load_adapter_config(
        args.cloud_config,
        args.ingress_env,
        target_environment=args.target_environment,
    )
    ingress = ingress_control.IngressControl(
        args.ingress_control_url,
        token=os.environ.get(handoff.INGRESS_CONTROL_TOKEN_ENV) or None,
    )
    return config, control_adapter.ControlAdapter(config, ingress)


def build_server(args: argparse.Namespace) -> http.server.ThreadingHTTPServer:
    """serve-adapter 의 서버를 만든다(아직 serve 하지 않음)."""
    config, adapter = build_adapter(args)
    return control_adapter.make_server(adapter, config.port)


def main(argv: list[str] | None = None) -> int:
    """명령을 실행하고 종료 코드를 돌려준다."""
    args = build_parser().parse_args(argv)
    if args.command == "ingress-start":
        code, body = build_adapter(args)[1].start_on_source()
        print(json.dumps(body, ensure_ascii=False))
        return 0 if code == 200 else 1
    server = build_server(args)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
