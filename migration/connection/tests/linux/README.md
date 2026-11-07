# 격리된 Linux 연결 시험

`test_wireguard.py`는 실제 네트워크 보조 프로그램을 Docker 컨테이너 두 개에서 실행합니다. 호스트 네트워크·실제 클러스터·고객 자격증명을 사용하지 않습니다. 임시 WireGuard 키는 표준입력으로만 전달합니다.

Linux용 `migration-net-helper`를 먼저 빌드한 후 명시적으로 실행합니다. Docker 실행 권한과 컨테이너의 `NET_ADMIN` 권한이 필요합니다. 실행 파일과 컨테이너 이미지의 CPU 아키텍처가 같아야 합니다.

```sh
docker build -f migration/connection/tests/linux/Dockerfile \
  -t migration-connection-network-test migration/connection/tests/linux
python3 migration/connection/tests/linux/test_wireguard.py \
  --image migration-connection-network-test \
  --binaries migration/connection/agent/target/linux/debug
```

시험은 고유 이름과 표식의 네트워크·컨테이너만 만들고 종료 시 삭제합니다. 서로의 터널 주소만 허용하는 실제 WireGuard 연결과 양방향 확인 메시지, 기본 경로 유지, 허용한 작업 포트 연결, 다른 포트 차단, 갱신하지 않은 커널 방화벽 임대의 만료 후 차단, 소유 인터페이스 제거를 검사합니다. 임대 만료 확인에는 약 62초가 필요합니다.

이 시험은 실제 제어 서버의 단말 승인부터 환경 확정까지를 모두 실행하는 시험이 아닙니다. 해당 흐름의 통합 시험, 실제 Kubernetes 읽기 권한, Ubuntu 24.04와 두 CPU 아키텍처의 게시된 배포 묶음 실행은 별도로 검증해야 합니다.
