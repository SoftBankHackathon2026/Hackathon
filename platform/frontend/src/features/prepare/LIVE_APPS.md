# 실제 환경의 앱 선택

작업 등록에서 출발·도착 환경을 지정한 뒤 출발 환경의 앱을 선택한다. 연결로 수집한 환경은 목록에 저장해야 출발·도착 후보로 표시된다. 고정 시연 목록은 미리보기에서만 사용한다.

## 데이터 경로

서명된 environment.inspect 작업 → 선택한 kubeconfig context의 읽기 전용 수집 → inventory report의 workloads → 서버 검증/소유자 접근제어 → ConnectionSnapshot.workloads → LiveAppSelect.

Deployment, StatefulSet, DaemonSet을 수집하며 Argo Rollout은 별도로 조회한다. 네임스페이스/종류/이름/복제수/이미지와 직접 참조 Secret·ConfigMap·PVC 이름만 전송한다. Secret/ConfigMap 객체를 조회하지 않고 env 값·명령·주석은 전송하지 않는다. projected volume, claim template 및 외부 DB 의존성 분석은 이 목록의 범위 밖이다. 목록은 이전 가능 판정이 아니며 선택은 화면 안에서만 유지된다. 선택을 확정하면 마이그레이션 작업이 등록되고 작업 상세가 열린다.

등록 후 재수집은 동일 target_id에만 허용한다. 재수집 동안 이전 워크로드 목록을 비우며 revoked 또는 다른 epoch 자료를 표시하지 않는다. 수집 결과 변경 시 선택을 초기화한다. 구버전 보고서도 허용하지만 시연값으로 대신 채우지 않고 도구 업데이트/재수집을 안내한다. 권한 부족과 실제 빈 목록을 구분한다.

## 배포 순서

1. 변경 검토 및 push/PR/머지 후 platform-ec2 자동배포로 웹/API 갱신.
2. platform/deploy/ec2/README.md의 기존 연결 도구 release 절차로 새 version/증가한 sequence의 서명된 배포본 생성. 변경된 inventory.pyz가 포함되어야 한다. 기존 배포본을 같은 버전으로 덮어쓰지 않는다.
3. 서버의 release metadata/게이트웨이 구성과 배포본을 해당 절차에 맞춰 갱신하고 단말에 새 도구를 적용한다. 기존 세션/티켓 재사용 가능 여부를 확인하고 불가능하면 새 연결을 등록한다. 비밀키는 Git에 포함하지 않는다.
4. 사용자 등록 환경에서 앱 목록 재수집 → 실제 이름/namespace/image 대조 → 앱 체크 확인. 사용자는 스크린샷의 k3s-local이 별도 온프렘 환경임을 확인했다. 기존 시연 환경과 매핑하지 않는다. 로컬 kubeconfig 시험은 그 별도 환경의 E2E 검증이 아니다.

## 검증

- connection 테스트: 93 passed, 13 skipped, 37 subtests passed. 외부 서비스/암호화 전제 skip을 통합 검증 완료로 보지 않는다.
- frontend 단위 테스트 242 passed, TypeScript/Vite build 성공.
- live-connections Chromium API 계약 테스트 1 passed: 실제모드 진행, legacy 안내, 실제 응답 앱 선택, preview 데이터 미노출. 백엔드는 mock이므로 실배포 E2E가 아니다.
- 현재 로컬 kubeconfig 클러스터 읽기 시험: 워크로드54개, hackathon/sample-app Rollout 포함. 클러스터 변경 없음.

웹/API 배포만으로 이미 설치된 구버전 단말 수집기가 자동 갱신되지는 않는다.
