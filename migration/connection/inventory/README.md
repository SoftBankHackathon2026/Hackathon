# 온프레미스 환경정보 수집

기존 Kubernetes 접근 권한을 사용하는 읽기 전용 모듈입니다. 계정·역할·제품을 설치하지 않으며, Keycloak·OpenBao 설치 모듈과 기존 앱 분석기를 변경하지 않습니다. 이 모듈 자체가 원격 요청을 승인하지는 않습니다. 호출하는 단말 프로그램이 작업 서명·조직·승인 단말·현재 터널·작업 기한을 검사해야 합니다.

## 명령과 결과

설치된 `migration_connection` 패키지에서 다음 명령을 사용합니다. 식별자는 서버가 승인한 작업의 값입니다.

```sh
python -m migration_connection.inventory discover --request-id REQUEST --device-id DEVICE --epoch 1
python -m migration_connection.inventory collect --request-id REQUEST --device-id DEVICE --epoch 1 --target-id SELECTED_TARGET
```

`discover`는 로컬 kubeconfig의 문맥 목록과 공개 신뢰 자료만 확인하며 Kubernetes 서버에 요청하지 않습니다. 사용자 토큰·개인키·인증 명령 설정을 출력하도록 요청하지 않습니다. 인증서 검증을 끈 문맥, 인증정보가 포함된 주소, 프록시 지정 문맥은 지원하지 않으며 명시적인 오류로 중단합니다. 공개 인증기관 자료가 없으면 기존 운영체제 신뢰 저장소를 사용하는 문맥으로 기록합니다.

사용자가 명시적으로 선택한 대상 식별자에 대해서만 `collect`를 실행합니다. 대상 식별자는 문맥 이름·HTTPS 서버 주소·공개 인증기관 자료 해시·TLS 서버 이름에 결합됩니다. 선택 후 로컬 설정이 바뀌면 새 후보 선택이 필요합니다. 수집 전후 설정을 대조하지만 관리자에 의한 실행 도중의 일시적인 로컬 파일 변조까지 방어하는 보안 경계는 아닙니다.

필수 조회는 `kube-system` 네임스페이스의 고유 식별자와 서버 버전입니다. 이를 읽지 못하면 실제 클러스터 신원을 확인하지 못한 것으로 처리합니다. 추가로 노드 수, 네임스페이스 수, 스토리지 클래스 이름·제공자·기본 여부만 수집합니다. 선택하지 않은 문맥의 서버에는 접속하지 않습니다.

노드·네임스페이스·스토리지 조회 실패는 자원 수 0으로 표시하지 않습니다. `coverage: unavailable`과 값 `null`을 반환하며 화면은 미확인으로 표시합니다. 추가 권한을 자동 생성하지 않습니다. 조회 결과에서 Secret·ConfigMap 내용, 환경변수, 임의 annotation, 인증정보는 결과에 포함하지 않습니다. Kubernetes 원본 응답은 제한된 메모리에서만 처리하고 파일·로그에 기록하지 않습니다.

수집 결과는 다음 두 필드로 반환합니다.

- `report`: 요청·단말·터널 키 세대·선택 대상·수집 시각, 허용된 환경 요약과 조회 범위
- `inventory_digest`: 보고서 전체의 정규화 자료에 대한 SHA-384 해시

서버 연결부는 `validate_report(report, expected_request_id=..., expected_device_id=..., expected_epoch=..., expected_target_id=...)`로 바인딩과 필드 구조를 검사하고 `inventory_digest(report)`를 재계산해야 합니다. 서명·현재 권한·키 세대·작업 유효기간·중복 응답 검증은 호출 계층 책임입니다. 단말 시각은 권한·만료 판단 근거가 아닙니다.

정규화는 고정 ASCII 필드 이름, 문자열, 안전한 정수, 불리언, null만 사용하는 JCS 호환 부분집합입니다. 소수·알 수 없는 필드·잘못된 유니코드·정수 범위 초과는 거부합니다. JCS는 JSON 자료의 표현 순서를 고정하는 규격입니다.

## 실행 경계와 검증

명령은 셸 없이 인자 배열로 실행합니다. 호출당 20초, 서버 요청당 15초, 응답 최대 8MiB, 최대 문맥 100개로 제한합니다. Kubernetes 인증 플러그인은 사용자의 기존 kubeconfig 신뢰 범위에 속합니다. 인증 플러그인의 별도 로그까지 이 모듈이 통제하지는 않습니다.

단위 시험은 비밀 제거, 선택 전 네트워크 요청 부재, 선택 대상 고정, 변경된 신뢰 자료 거절, 부분 조회 실패, 보고서 변조·다른 작업 바인딩 거절, 프로세스 시간·출력 제한을 확인합니다. 실환경 시험은 `CONNECTION_INVENTORY_LIVE_CONTEXT`와 `CONNECTION_INVENTORY_LIVE_KUBECONFIG`를 모두 명시할 때만 실행합니다. Kubernetes 객체 변경은 하지 않지만 해당 시험용 설정은 명시한 문맥 하나만 포함해야 합니다.
