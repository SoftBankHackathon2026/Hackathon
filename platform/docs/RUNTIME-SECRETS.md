# 플랫폼 서버 인증정보와 비밀 파일

이 문서는 플랫폼 API·Worker가 사용하는 인증정보를 다룬다. 배포되는 앱의 DB 비밀번호는 별도 앱 Secret으로 관리한다. 코드·Docker 이미지·배포 요청·릴리즈 이력에는 인증 원문을 넣지 않는다.

## 현재 서버와 변경 범위

서버는 [EC2 운영 구성](../../infra/aws/platform-server/README.md)을 따른다. AWS는 이미 인스턴스 Role을 source로 사용하는 profile로 배포 Role을 Assume한다. 장기 AWS access key를 추가할 필요가 없다.

이번 변경은 다음 두 입력을 지원한다. 기존 직접 값도 호환되지만, 직접 값과 파일 경로를 동시에 지정하면 시작을 거부한다.

| 입력 | 기존 | 파일 방식 |
|---|---|---|
| API 운영자 토큰 | `ONEPORT_OPERATOR_TOKEN` | `ONEPORT_OPERATOR_TOKEN_FILE` |
| 플랫폼 DB 접속 문자열 | runtime JSON의 `database_url` | `database_url_file` |

DB 파일 방식은 플랫폼 bundle, AWS, native GCP, GCP bridge, 온프렘 설정에 동일하게 적용한다. 모든 API·Worker가 같은 플랫폼 DB를 읽어야 한다. 비밀 파일 위치를 바꿔도 기존 릴리즈의 환경 fingerprint는 바뀌지 않는다.

**AWS Secrets Manager에서 파일을 가져오는 자동화·IAM 정책과 GCP Workload Identity Federation은 이번 코드에 포함되지 않는다.** 우선 운영자가 제공한 제한된 파일로 시작할 수 있다. 비밀 저장소를 연결할 때도 아래 파일 계약을 그대로 사용한다.

## EC2 설정 전환

1. 서버 운영자가 `/etc/oneport/secrets`를 `root:oneport`, 0750으로 준비한다.
2. DB 접속 문자열과 운영자 토큰을 각각 `database-url`, `operator-token`에 한 줄로 저장한다. 파일은 `root:oneport`, 0640으로 두고 터미널·CI 로그에 값을 출력하지 않는다.
3. `/etc/oneport/platform.json`과 각 Provider runtime JSON의 `database_url`을 제거하고 다음 항목으로 바꾼다.

```json
{"database_url_file": "/etc/oneport/secrets/database-url"}
```

4. `/etc/oneport/platform.env`에서 `ONEPORT_OPERATOR_TOKEN`을 제거하고 다음 경로를 지정한다. 같은 이름의 환경변수가 상위 프로세스에도 남아 있지 않은지 확인한다.

```env
ONEPORT_OPERATOR_TOKEN_FILE=/etc/oneport/secrets/operator-token
```

5. 새 코드를 배포하고 API·해당 Worker를 재시작한다. API health 확인에 더해 인증된 이력 조회와 Worker의 플랫폼 DB 연결도 확인한다.

위 JSON은 추가/교체할 필드만 보여준다. providers·application 등 기존 설정은 유지한다. DB 파일에는 **플랫폼 DB** 접속 문자열을 넣는다. 앱용 `DATABASE_URL`과 다르다.

파일 경로는 절대 경로여야 한다. 파일 누락·빈 값·여러 줄·64 KiB 초과·다른 사용자에게 공개된 권한·그룹 쓰기 권한은 거부한다. 0400/0600 또는 읽기 그룹을 제한한 0440/0640을 사용한다. Kubernetes projected Secret의 symlink도 지원한다. 환경변수 값으로 파일 내용을 출력해서 전달할 필요가 없다.

## Provider 인증정보

| 대상 | 서버에 공급할 정보 | 범위·갱신 |
|---|---|---|
| AWS | `/etc/oneport/aws-config` profile, AWS role ARN 등 비밀이 아닌 설정 | 기존 EC2 Role + AssumeRole 사용. SDK가 임시 자격증명을 갱신 |
| GCP | `GOOGLE_APPLICATION_CREDENTIALS`가 가리키는 ADC 파일 | 당장은 배포 서비스 계정 JSON. 이후 AWS Workload Identity Federation 설정 파일로 전환 가능. GCP Worker에만 공급 |
| 온프렘 | runtime의 `kubeconfig` 절대 경로 | 제한된 플랫폼 계정만 사용. 전달받은 토큰 만료 전에 파일 교체·Worker 재시작. 관리자 kubeconfig 사용 금지 |
| Docker Hub push | 빌드 프로세스의 전용 push 인증 | public image pull Worker에는 필요 없음 |

SDK의 토큰 자동 갱신과 JSON 개인 키의 교체는 다르다. GCP SDK가 access token을 갱신하더라도 개인 키 자체를 교체해주는 것은 아니다.

현재 EC2 서비스는 같은 `oneport` OS 사용자로 실행된다. 따라서 파일 마운트를 나눈 것만으로 프로세스 간 강한 격리가 완성되지는 않는다. GCP·온프렘 Worker를 서버에 추가할 때 별도 사용자 또는 컨테이너로 실행하고 각 프로세스에 필요한 파일만 전달하는 구성을 권장한다. DB Secret은 모두 필요하지만 운영자 토큰은 API에만 필요하다.

## Secret Manager를 연결할 때

권장 흐름은 `EC2 Role → 지정한 플랫폼 Secret만 읽기 → 제한된 런타임 파일 생성 → API/Worker 시작`이다. 현재 앱 배포 Role은 시크릿 원문 조회를 금지하고 있으므로 앱 배포 Role의 권한을 넓혀 우회하지 않는다. 인프라 담당자가 플랫폼 시작용 Role에 플랫폼 비밀값의 ARN만 읽도록 별도 권한을 부여한다.

업데이트 시에는 새 파일에 완전한 값을 쓴 뒤 원자적으로 교체하고, 파일 권한 확인 후 프로세스를 재시작한다. 파일을 읽을 수 없으면 오래된 값이나 빈 값으로 대체하지 않고 시작을 실패시킨다. Secret Manager를 도입해도 서버 시작에 쓰는 IAM Role이 먼저 필요하다.

## 교체와 회수

- GCP 키: 새 키로 인증·조회 확인 → Worker 재시작 및 배포 확인 → 기존 키 폐기.
- kubeconfig: 새 토큰으로 namespace 조회 확인 → Worker 재시작 → 만료 시각 기록.
- 플랫폼 DB: DB 측 인증 변경과 파일 교체·모든 프로세스 재시작을 함께 계획. 파일 교체만으로 DB 비밀번호가 변경되지는 않는다.
- 운영자 토큰: API 재시작 후 이전 토큰 거부·새 토큰 허용 확인. 현재는 단일 토큰이라 전환 시 사용자 재입력이 필요하다.

파일은 프로세스 시작 시 읽는다. 실행 중 자동 재로딩이나 다중 버전 토큰 유예는 구현하지 않았다. `SecretStr`는 출력 마스킹이지 파일 암호화가 아니다. `.gitignore`도 서버의 접근 제어를 대신하지 않는다.

## Helm 차트로 플랫폼을 실행할 경우

차트의 values에는 `existingSecret` 이름·key·마운트 경로만 넣는다. Secret 원문을 values, `--set`, ConfigMap, Git에 넣지 않는다. API는 운영자 토큰과 플랫폼 DB 파일, 각 Worker는 플랫폼 DB 파일과 해당 Provider 인증만 마운트한다. Kubernetes Secret volume의 `defaultMode: 0440`과 실행 UID/그룹을 맞춘다.

현재 온프렘의 `oneport-apps`는 사용자 앱 namespace이다. 플랫폼 자체 배포용 차트라면 플랫폼 namespace와 ServiceAccount는 인프라 담당자가 별도로 준비해야 한다. 앱 배포 계정으로 Namespace·RBAC·CRD를 설치하지 않는다.

공식 참고: [EC2 Role](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_use_switch-role-ec2.html), [GCP AWS federation](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-other-clouds), [Kubernetes Secret 관리](https://kubernetes.io/docs/concepts/security/secrets-good-practices/).
