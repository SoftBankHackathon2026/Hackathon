from controlplane.domain.models import Step, Strategy

PLAN_OWNER = "provider"

FIRST_DEPLOYMENT_STEPS = [
    ("validate", "새 앱 환경 준비", "앱 전용 실행 계정과 리소스 소유 정보 확인"),
    ("deploy", "첫 Cloud Run 서비스 생성", "선택한 이미지와 앱별 설정으로 첫 버전 생성"),
    (
        "verify",
        "첫 버전 확인",
        "상태 확인 주소와 Release ID 검사. 이전 버전이 없어 최초 배포는 트래픽 분할 없이 진행",
    ),
]
DATA_STEP = (
    "data",
    "앱 DB 준비",
    "전용 Cloud SQL·Secret 생성 또는 재사용 후 DATABASE_URL·사설 연결 적용",
)
CACHE_STEP = (
    "cache",
    "앱 캐시 준비",
    "Memorystore 생성 또는 재사용 후 CACHE_CONFIG·REDIS_URL·TLS CA·사설 연결 적용",
)
MIGRATE_STEP = (
    "migrate",
    "DB Migration",
    "후보 이미지로 Cloud Run Job 실행. 성공 전에는 후보 배포를 시작하지 않음",
)


def _rollout_steps(request):
    policy = request.rollout
    promote = "수동 100% 전환 대기" if policy.promotion == "manual" else "자동 100% 전환"
    if request.strategy == Strategy.CANARY:
        details = f"새 버전 {policy.canary_percent}%로 {policy.hold_seconds}초 유지 후 {promote}"
    else:
        details = f"후보 검증 후 {promote}"
    auto_rollback = "켜짐" if policy.auto_rollback else "꺼짐"
    return [
        ("validate", "등록 환경 확인", "기존 서비스 UID·허용 이미지·자원 확인"),
        ("deploy", "후보 배포", "기존 연결 설정을 유지한 Cloud Run revision 생성"),
        ("verify", "후보 검증", "후보 주소의 응답과 Release ID 확인"),
        ("promote", "설정에 따른 트래픽 전환", details),
        ("bake", "전환 후 확인", f"{policy.bake_seconds}초 확인 · 자동 복귀 {auto_rollback}"),
    ]


def plan_steps(request, *, first_deployment: bool) -> list[Step]:
    steps = list(FIRST_DEPLOYMENT_STEPS) if first_deployment else _rollout_steps(request)
    if request.resources.database == "postgresql":
        steps.insert(1, DATA_STEP)
    if request.resources.redis:
        steps.insert(2 if request.resources.database == "postgresql" else 1, CACHE_STEP)
    if request.migration:
        steps.insert(2 + int(request.resources.redis), MIGRATE_STEP)
    return [Step(id=k, title=t, owner=PLAN_OWNER, detail=d) for k, t, d in steps]
