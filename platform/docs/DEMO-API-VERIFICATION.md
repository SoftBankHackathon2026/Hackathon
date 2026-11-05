# 시연 래퍼 코드 재검토 및 실제 API 검증 — 2026-10-04

검증 브랜치: `feat/github-multi-provider-deploy`, 수정 커밋 `42e3907`.
기존 서비스와 분리한 검증용 앱 `demo-api-624f2c`를 사용했다.
로컬 API `http://127.0.0.1:18430`에 HTTP 요청을 보내고, 실제 AWS·GCP·Helm Worker를 실행했다.
클라우드 호출과 앱 HTTP 응답을 가짜 응답으로 대체하지 않았다.

## 실제 배포 API 결과

기존 `POST /api/v1/releases`에 세 환경의 요청을 각각 전달했다.
모두 HTTP 202로 접수되고, 최종 상태 `succeeded`, 앱 `/health` HTTP 200,
`X-Launchpad-Release`와 요청한 Release ID 일치를 확인했다.

| 환경 | Release ID | 확인한 URL |
|---|---|---|
| AWS ECS | `971f41610fa1b55df2605342` | https://demo-api-624f2c.app.yubin.dev |
| GCP Cloud Run | `d9c4e5d19776def4b7634ea2` | https://oneport-api-624f2c-q37hkgfc2a-du.a.run.app |
| 온프렘 Helm | `9cc4670be3b2492dda093695` | https://demo-api-624f2c.yubin.website |

검증 이미지: `docker.io/inhoman/oneport-release-demo@sha256:155a6e8597f135c2832499479711787e8aed53901170d457c4b839a57ad772a9`.
세 앱의 응답 본문에서 `version=api-baseline-624f2c`, `healthy=true`도 확인했다.
위 URL은 검증 시점의 임시 리소스이며 이후 정리될 수 있다.

## 새 래퍼 API 결과와 남은 검증

`POST /api/v1/demo/deployments`도 실제 HTTP로 호출했다.
저장소는 `https://github.com/softbank-hackathon-2026-term1-azalea/oneport-platform`,
Dockerfile은 `platform/examples/release-demo/Dockerfile`, context는 `platform/examples/release-demo`다.

- HTTP 202, 실행 ID `8f1f2363e853aa1029f1d924`.
- commit `d6aafa943ad086ad76a20cebf33ccfab1b095cf1` 고정.
- 같은 idempotency key 재전송 → 같은 실행 ID.
- 기존 빌드 큐에 `53e8e999d300bbae2b356cc4` 한 건 접수.
- 래퍼 상태 `building`, 실제 빌드 상태는 시작 전 대기. CodeBuild 작업은 시작하지 못했다.

로컬 개발용 AWS 키는 CodeBuild 프로젝트 조회에서 AccessDeniedException을 받았다.
기존 `default`·`launchpad` 로그인 프로필은 LoginRefreshRequired 상태였다.
AWS 로그인을 갱신한 뒤 기존 Build Worker를 실행해야 나머지 검증을 이어갈 수 있다.
따라서 **GitHub 빌드 → 같은 digest로 세 환경 배포 전체가 실검증 완료된 것은 아니다.**
위 표는 사전에 등록한 이미지로 기존 배포 API를 검증한 결과다.

## 코드 재검토에서 수정한 문제

1. 운영자 요청의 팀 값이 null이라 팀 소유 대상의 이미지 승인이 거부되던 문제:
   요청자와 대상 소유 팀을 분리하고 대상 소유 팀을 고정한다.
2. 재시도가 새 Release 접수 전에 거절되면 그다음 재시도도 막히던 문제:
   현재 Release ID뿐 아니라 같은 실행의 이전 시도 이력을 대조한다.
3. 재시작 시 빌드의 요청·commit·소유 팀 검사가 부족하던 문제:
   고정한 빌드 요청 전체와 commit을 대조한다.
4. 같은 키·이미지지만 환경변수 등이 다른 Release를 같은 실행으로 연결할 수 있던 문제:
   프로바이더가 정규화한 전체 요청도 대조한다.

추가로 사전 확인 이후 다른 배포가 생기면 빌드 전에 거절하고,
공통 이미지 입력을 받지 않는 GCP repository bridge는 대상에서 제외한다.
실제 AWS·native GCP·Helm 어댑터를 사용하는 로컬 통합 테스트도 추가했다.

자동 테스트: **549 passed, skip 없음**. 시연 관련 테스트는 31개.
DB는 실제 PostgreSQL의 격리 스키마를 사용했다. 자동 테스트의 클라우드 경계는 테스트 더블이며,
위 실제 API 검증과 구분한다. Ruff 검사도 통과했다.

민감한 런타임 설정·토큰은 Git에 포함하지 않는다. 로컬 재개 정보와 API 증거는
`platform/.data/demo-live/`에 있으며, 인증 갱신 후 같은 실행 기록을 이어서 확인할 수 있다.
