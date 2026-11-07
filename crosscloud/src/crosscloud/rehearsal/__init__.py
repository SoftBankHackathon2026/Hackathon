"""실클라우드 왕복 리허설(AWS→GCP→AWS) 배선.

사람이 단계별로 돌린다. 단계마다 상태 디렉터리에 산출물(Terraform 출력·인계
파일·완료 표시)을 남겨 멈춘 곳에서 다시 시작한다. 비밀값은 파일·로그에 쓰지
않는다. 순서와 명령은 deploy/rehearsal/README.md 에 있다.

  python -m crosscloud.rehearsal --state-dir DIR init --run r1107a \
      --source-image docker.io/kty1021/sample-app@sha256:...
  python -m crosscloud.rehearsal --state-dir DIR aws-source --dry-run
"""
