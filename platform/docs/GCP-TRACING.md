# GCP 요청 추적

플랫폼에서 AWS와 같은 요청 목록, 오류·느린 요청 필터, 호출별 시간, SQL·예외와 관련 로그를 확인합니다.
수집된 요청을 보여주며 모든 요청이 반드시 보관되는 것은 아닙니다.

| 데이터 | 수집·조회 경로 |
| --- | --- |
| 요청·DB·외부 API 호출 | 앱 OpenTelemetry → 같은 Cloud Run 인스턴스의 Collector → Cloud Trace |
| 관련 앱 로그 | stdout/stderr의 Trace ID → Cloud Logging → 플랫폼 |
| 요청 수·4xx·5xx·p95 | Cloud Run 기본 지표 → Cloud Monitoring → 플랫폼 |

## 설정

1. `infra/gcp`를 적용합니다. 정적 앱과 프로비저닝 Workflow의 앱 실행 계정에 `roles/cloudtrace.agent`를 추가합니다. 이미 존재하는 동적 앱은 Workflow를 다시 실행합니다.
2. Python 자동 계측용 공식 OpenTelemetry Operator 이미지를 프로젝트의 Artifact Registry로 복사합니다. Cloud Run에서 사용할 **linux/amd64 이미지의 digest**를 등록합니다. GHCR 주소를 그대로 Cloud Run에 전달하지 않습니다.
3. 기존 GCP runtime JSON에 다음 설정을 추가하고 API·GCP Worker를 재시작합니다.
   API와 Worker의 환경 변수가 다르면 최상위 `credentials_file`에 기존 서비스 계정 JSON 파일 경로를 지정합니다.
   시연 서버는 `/etc/oneport/secrets/gcp-credentials.json`을 두 프로세스가 함께 사용합니다.
   이 값이 없으면 기존 ADC 설정을 사용합니다. 새 키나 impersonation은 필요하지 않습니다. 운영자 설정 파일이며 HTTP 배포 요청으로 이미지·수집기 목적지를 변경할 수 없습니다.

```json
"tracing": {
  "sample_ratio": 1.0,
  "python_platform": "glibc",
  "python_image": "REGION-docker.pkg.dev/PROJECT/REPOSITORY/otel-python@sha256:AMD64_DIGEST"
}
```

Collector는 저장소에 고정한 OpenTelemetry Contrib 0.162.0 이미지와 Google Cloud exporter를 사용합니다.
Python 패키지는 공식 Operator 0.66b0 이미지를 사용합니다. Alpine 계열 Python 앱은 `python_platform: "musl"`로 설정합니다.
앱의 Python 버전·라이브러리가 해당 자동 계측 이미지와 호환되어야 합니다. 지원되지 않는 언어는 앱 이미지에 OpenTelemetry SDK를 넣고 OTLP를 사용합니다.
`python_image`가 없으면 이미 계측된 앱을 위한 Collector만 준비하고 Python 자동 계측 옵션을 표시하지 않습니다.

4. 앱 배포 화면에서 **요청 추적 켜기**를 선택하고 재배포합니다. 설정 등록만으로 현재 실행 중인 리비전이 바뀌지는 않습니다.

플랫폼은 Collector와 Python 패키지 준비용 사이드카를 추가합니다. Python 패키지를 공유 볼륨에 복사한 후
시작 검사를 통과해야 앱을 시작합니다. Cloud Run에는 init container가 없으므로 준비용 사이드카는 계속 실행됩니다.
Secret, DB 연결, 실행 계정과 VPC 설정은 유지하며 새로운 리비전에서도 같은 설정을 사용합니다.
Python 추적을 끄면 해당 사이드카·볼륨·플랫폼 PYTHONPATH를 제거합니다.

앱은 `traceparent`를 이어받고 앱 Span을 `sample_ratio` 비율로 기록합니다. Cloud Run의 기본 요청 Span 수집 비율은 바뀌지 않습니다.
기본 비율 1.0은 데모 확인용이며, 운영에서는 트래픽에 맞게 줄일 수 있습니다.
응답을 보낸 뒤에도 batch export가 동작하도록 모든 컨테이너의 CPU를 항상 할당합니다.
따라서 Cloud Run 인스턴스 기반 과금과 Collector·준비용 사이드카의 추가 자원이 적용됩니다.

## 조회와 격리

기존 공통 API를 사용합니다.

- `GET /api/v1/environments/{id}/traffic?minutes=60`
- `GET /api/v1/environments/{id}/traces?minutes=60&kind=all` (`errors`, `slow`도 지원)
- `GET /api/v1/environments/{id}/traces/{traceId}`
- `GET /api/v1/environments/{id}/logs?minutes=60`

Collector가 각 Span에 등록된 서비스·환경·앱·릴리즈 정보를 붙입니다. 상세 조회에서도 Span마다 앱 소속을 확인합니다.
Cloud Run 자동 Span은 서비스·리전 라벨로 확인합니다. 로그는 서비스 범위와 정확한 Trace ID를 모두 확인합니다.
플랫폼 사용자 접근은 기존 환경 소유 팀 검사에 따릅니다. 앱에는 Trace 조회 권한이 없습니다.

Python 기본 logging 레코드에 Trace ID를 기록합니다. 앱이 자체 로그 formatter를 사용하면
`otelTraceID`/`trace_id` 필드 또는 `trace_id=<32자리 ID>`를 출력해야 연결됩니다.
Cloud Logging의 표준 `trace` 필드가 있는 구조화 로그도 지원합니다.
Span 정보에는 계측 라이브러리가 기록한 SQL·예외만 표시합니다. SQL 파라미터·인증 헤더 등 비밀값을 직접 기록하지 않습니다.

## 확인

헬스체크 경로는 앱 계측에서 제외합니다. `/notes`처럼 실제 API를 호출한 후 모니터링을 새로고침합니다.
요청을 선택하면 서버 호출과 DB 호출의 시간, 응답 코드, 릴리즈와 같은 Trace ID의 로그를 확인할 수 있습니다.
오류 요청은 4xx/5xx 또는 계측된 예외로 분류하고, 느린 요청은 앱의 호출 구간이 1초 이상인 경우입니다.
지표는 Trace보다 늦게 반영될 수 있으며 인스턴스 수 미수집을 장애나 0대로 표시하지 않습니다.

브라우저 렌더링·클릭 시점은 이 설정의 범위가 아닙니다. 여러 서비스의 호출을 한 화면에 합치는 경우에도
현재 플랫폼은 등록된 앱에 속한 Span만 보여줍니다.
