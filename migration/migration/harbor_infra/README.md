# Harbor 기반 인프라의 탐색과 설치

플랫폼이 선택한 Ubuntu 노드에 Harbor를 설치하고 Keycloak 로그인·비공개 프로젝트·용도별 기계 계정과 지정 노드의 인증서 신뢰 설정을 준비합니다. 실제 업무 이미지 복사는 별도 도구의 책임입니다.

- [설치·호출·운영 안내](../../docs/harbor-infra/README.md)
- [입력 예제](../../config.harbor-infra.example.yaml)
- [검증 기록](../../docs/harbor-infra/VALIDATION.md)
- [공식 배포물과 고정 버전](ansible/releases.json)

`config.py`는 입력 검증, `cluster.py`는 읽기 전용 Kubernetes 조사, `runner.py`는 보호된 실행 공간과 Ansible 호출·결과 조합, `handoff.py`는 비밀 인계를 담당합니다. 서버 변경과 Harbor·Keycloak 제어는 `ansible/library/`와 `ansible/module_utils/`의 고정 작업으로 실행합니다. 실제 서버 적용 여부는 검증 기록과 구분해 확인하십시오.
