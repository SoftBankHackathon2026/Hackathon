"""Public plan errors: allowlisted guidance, never raw provider/validation input."""

from pydantic import ValidationError

MESSAGES = {
    "Wrong Helm binding": "선택한 환경에 등록된 앱 이름을 사용하세요.",
    "Spec does not belong to this provider binding": "선택한 환경에 등록된 앱 이름을 사용하세요.",
    "Image not approved for this binding": "이 환경에 등록된 이미지 주소를 선택하거나 앱 화면에서 GitHub 빌드를 실행하세요.",
    "Image digest is not registered": "이 환경에 등록된 이미지 주소를 선택하세요.",
    "Image digest is not registered for this environment": "이 환경에 등록된 이미지 주소를 선택하세요.",
    "Resource limits exceeded": "포트는 1024 이상, CPU·메모리·레플리카는 선택한 환경의 한도 안으로 설정하세요.",
    "This application requires PostgreSQL": "이 앱에는 PostgreSQL이 필요합니다. PostgreSQL 사용을 유지하세요.",
    "Existing data configuration cannot be changed during app deployment": "기존 앱의 PostgreSQL·Redis 설정은 재배포 중 변경할 수 없습니다. 등록된 설정을 유지하세요.",
    "Existing Helm strategy is immutable; use a new application": "현재 Helm 차트에서는 기존 앱의 배포 전략을 변경할 수 없습니다. 현재 앱의 전략을 유지하세요.",
    "Canary requires automatic promotion and rollback": "현재 온프렘 카나리는 자동 전환·자동 복구를 사용합니다.",
    "Canary hold must be 5..120 seconds and retention 30..300 seconds": "카나리 단계 대기는 5~120초, 이전 버전 보존은 30~300초로 설정하세요.",
    "Blue-green rollout settings are not supported": "온프렘 블루그린에는 카나리 단계 설정을 보내지 마세요.",
    "Helm database mode must be container": "온프렘 DB는 앱 전용 컨테이너 모드를 사용하세요.",
    "Request does not match registered DB mode": "선택한 환경에 등록된 DB 할당 방식을 유지하세요.",
    "Request does not match registered app": "등록된 앱의 포트·상태 확인 경로·DB·Redis 설정을 유지하세요.",
    "Migration must preserve compatibility with the active app": "마이그레이션은 기존 버전과 호환되는 명령으로 설정하세요.",
}


def plan_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        fields = sorted({str(e["loc"][0]) for e in exc.errors(include_input=False) if e["loc"]})
        names = {
            "source": "이미지 주소 또는 저장소",
            "port": "포트",
            "health_path": "상태 확인 경로",
            "cpu": "CPU",
            "memory_mib": "메모리",
            "replicas": "레플리카",
            "rollout": "배포 단계",
            "migration": "마이그레이션",
            "resources": "DB·Redis",
            "secret_bindings": "Secret 참조",
        }
        labels = [names[f] for f in fields if f in names]
        if labels:
            return "입력 형식을 확인하세요: " + ", ".join(labels) + "."
    return MESSAGES.get(
        str(exc),
        "이 배포 설정을 처리할 수 없습니다. 등록된 앱 설정으로 다시 시도하거나 운영자에게 환경 연결 상태를 확인하세요.",
    )
