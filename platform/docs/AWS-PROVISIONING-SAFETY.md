# AWS 배포 결함 검토와 수정 — 2026-10-03

대상: `platform/backend/src/oneport_aws`. AWS SDK 호출은 테스트 대역으로 장애를 주입했고, 잠금과 작업 상태는 실제 PostgreSQL에서 검증했다. 이 검증을 실제 RDS 장애 복구 시험으로 간주하지 않는다.

## 1. 삭제 예약 Secret 때문에 비밀번호를 반복 변경

기존 `creating` 단계는 RDS 비밀번호 변경 후 Secret을 기록했다. Secret 저장에 실패하면 상태가 `creating`으로 남아 다음 호출에서 새로운 비밀번호를 생성했다.

수정:

- `DescribeSecret.DeletedDate`를 확인해 삭제 예약 Secret이면 RDS 생성/비밀번호 변경 전에 중단한다. 플랫폼이 임의로 삭제 예약을 취소하지 않는다.
- AWS 쓰기 전에 `provisioning` 또는 `resetting` 의도를 PostgreSQL에 커밋한다.
- 쓰기 응답 유실이나 쓰기 시작 후 오류는 `uncertain`으로 저장하고 자동 재실행하지 않는다.
- 프로세스 종료로 의도만 남으면 재기동 후 `uncertain`으로 전환한다.
- 조회만 실패한 경우에는 재시도할 수 있다. 이미 `failed`/`uncertain`인 앱을 다시 등록해도 비밀번호 변경을 다시 시작하지 않는다.
- 비밀번호 원문은 작업 저널에 기록하지 않는다.

AWS와 PostgreSQL 사이의 분산 트랜잭션을 구현한 것은 아니다. 불확실성을 숨기지 않고 중단하는 정책이다. `uncertain`은 성공도 자동 복구 완료도 아니다. 운영자는 RDS 상태, Secret 삭제 예약/버전, 실제 연결을 확인해야 한다. 강제로 `creating`으로 돌리거나 Secret을 재생성하면 기존 릴리즈의 고정 버전 참조가 깨질 수 있다. 자동 복구/재개 API는 아직 없다.

## 2. worker와 `db-create --wait` 동시 실행

기존 행 잠금은 정상 트랜잭션 동안 중복 실행을 막지만, AWS 쓰기와 상태 저장 사이의 프로세스 종료/응답 유실을 처리하지 못했다. 동시 실행만으로 항상 장애가 발생한다고 단정하지 않는다.

앱·환경·DB 스키마로 범위를 한정한 PostgreSQL 세션 advisory lock을 사용한다. 의도 커밋 후에도 잠금이 유지된다. 다른 실행자는 클라우드 요청 없이 대기하며, 작업 중 앱 재등록도 거절한다. 연결 종료 시 잠금이 해제된다.

회귀 테스트는 비밀번호 변경 호출을 중간에서 멈춘 상태로 두 번째 실행자와 앱 재등록을 실행한다. 비밀번호 변경이 한 번만 실행되고, 완료 상태가 `configuring`으로 저장되는지 검증한다.

## 3. 병렬 이미지 빌드 인증 충돌

공용 `docker login`을 제거했다. 작업마다 권한 0700인 임시 디렉터리를 만들고 ECR 인증을 권한 0600의 전용 Docker 설정에 기록한다. `docker --config <job-directory> buildx ...`만 사용한다.

- 다른 레지스트리 인증, `credsStore`, `credHelpers`를 복사하지 않는다.
- `buildx` 탐색에 필요한 플러그인 디렉터리만 유지한다.
- 성공/실패 시 임시 인증 설정을 정리한다.
- 테스트는 병렬 두 빌드가 서로 다른 설정을 사용하고, 종료 후 삭제되는지 확인한다.
- 로컬 Docker에서 전용 설정으로 `buildx version` 실행을 별도로 확인했다.

Worker의 Docker daemon은 기본 소켓 또는 운영자가 명시한 `DOCKER_HOST`로 지정한다. 사용자 Docker context 저장소를 작업 설정에 복사하지 않는다. 강제 종료/SIGKILL은 Python 정리 구문을 실행하지 못하므로 운영 환경에서는 작업용 임시 디렉터리의 만료 청소도 필요하다.

## 4. DB 필수 앱이 DB 없이 등록/배포됨

이미지 URL만으로 임의의 애플리케이션이 DB를 반드시 요구하는지 알아낼 수 없다. 앱 계약과 실제 DB 연결 선택을 분리했다.

운영자가 AWS 앱 runtime 설정에 다음을 지정한다:

```json
{
  "required_database": "postgresql"
}
```

- DB 미선택 앱 등록은 즉시 거절한다.
- 배포 계획/접수와 worker 검증에서도 요구사항을 확인한다.
- UI는 DB 필수 안내와 PostgreSQL 기본 선택을 제공하고 `사용하지 않음`을 막는다.
- Shared PostgreSQL은 기존과 같이 앱 전용 `secret_refs.db`가 있어야 등록된다.

기존 설정 호환을 위해 기본값은 `none`이다. 따라서 **DB가 필요한 앱의 runtime 설정에 요구사항을 등록해야 한다.** 이미지 내용으로 DB 사용을 자동 추론하는 기능은 구현하지 않았다. 이미지 버전마다 요구사항이 바뀌면 앱 계약도 함께 갱신해야 한다. Secret 참조 등록은 DB의 실제 읽기·쓰기 연결 검증과 별개다.

## 운영 범위

공개 웹 API는 Shared DB 연결만 지원한다. Dedicated RDS 자동 생성은 `legacy_dedicated_provisioning=true`를 명시한 기존 CLI/worker 경로에만 남아 있다. 이번 안전성 수정은 그 경로에도 적용된다. 이 안전성 회귀 테스트는 새 AWS 리소스 생성 없이 수행했다. 이후 별도로 수행한 Fargate 실배포 시험은 [실제 검증 기록](LIVE-DEPLOYMENT-TEST.md)에 구분해 기록한다.

## 근거

- [AWS DescribeSecret — DeletedDate](https://docs.aws.amazon.com/secretsmanager/latest/apireference/API_DescribeSecret.html)
- [Docker CLI — --config](https://docs.docker.com/reference/cli/docker/)
