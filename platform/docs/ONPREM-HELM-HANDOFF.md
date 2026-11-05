# 온프렘 Helm 연동 인수인계

**플랫폼 팀에는 [Oneport 온프렘 Helm 연동 규격 v1](../../infra/onprem/docs/helm-platform-handoff.md) 링크 하나를 전달합니다.**

인수할 차트·패키지, 구현 현황, 플랫폼의 작업 순서, 완료 기준을 그 문서에서 함께 관리합니다.
values·Helm 명령·Kubernetes API 제어 등 상세 내용도 같은 문서에서 펼쳐 볼 수 있습니다.

인프라는 Helm 차트와 접근 권한을 제공하고 플랫폼은 Helm CLI와 Kubernetes API로 연동합니다.
별도 SDK 사용은 필수가 아닙니다. `ONPREM-HELM-SDK.md`는 기존 플랫폼 Python 코드를 재사용하는 개발자용 참고 자료입니다.
