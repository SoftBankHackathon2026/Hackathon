# 보안 탭 클라우드 실측 권한

보안 탭의 일부 통제는 `fleet.json` 의 신고값(`declared`) 대신 클라우드 API 에서 읽은 실측값을 먼저 써요.
이 문서는 수집기(`oneport-collector`)에 붙일 **읽기 전용** 권한과 설정을 정리해요. 수집기는 설정을 바꾸는 API 를 부르지 않아요.

## 실측하는 통제

| 통제 | AWS | GCP |
|---|---|---|
| NET-02 DB 비공개 | RDS `PubliclyAccessible` | Cloud SQL `settings.ipConfiguration.ipv4Enabled`, `authorizedNetworks`(근거에만) |
| DATA-01 DB 전송 암호화 | DB 파라미터 그룹 `rds.force_ssl`(PostgreSQL·SQL Server), `require_secure_transport`(MySQL·MariaDB). Aurora 는 클러스터 파라미터 그룹 | Cloud SQL `settings.ipConfiguration.sslMode`(`ENCRYPTED_ONLY`, `TRUSTED_CLIENT_CERTIFICATE_REQUIRED` 면 통과). 없으면 `requireSsl` |
| DATA-02 DB 저장 암호화 | RDS `StorageEncrypted` | 항상 통과(Cloud SQL 기본 암호화). CMEK 키 이름은 근거에만 남김 |
| DATA-03 DB 백업 | RDS `BackupRetentionPeriod > 0` | Cloud SQL `settings.backupConfiguration.enabled` |
| DET-01 위협 탐지 | GuardDuty 디텍터 `Status == ENABLED` | SCC Event Threat Detection `effectiveEnablementState == ENABLED` |
| DET-04 흐름 로그 | EKS 클러스터 VPC(또는 클러스터 서브넷 전부)에 `ACTIVE` 흐름 로그 | GKE 클러스터 서브넷 `logConfig.enable` |

나머지 신고값(감사 로그, WAF, TLS 진입점 등)은 아직 `fleet.json` 값을 그대로 써요. 온프렘 환경은 실측하지 않아요.

## AWS: 수집기 역할에 붙일 정책

EKS 에서는 `platform` 네임스페이스의 ServiceAccount `oneport-collector` 에 **EKS Pod Identity** 로 아래 정책을 가진 역할을 연결해요
(비용 수집과 같은 역할에 더해도 돼요. [COST-ESTIMATION.md](COST-ESTIMATION.md#수집과-권한)).

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "OneportSecurityObserveRead",
      "Effect": "Allow",
      "Action": [
        "rds:DescribeDBInstances",
        "rds:DescribeDBParameters",
        "rds:DescribeDBClusters",
        "rds:DescribeDBClusterParameters",
        "guardduty:ListDetectors",
        "guardduty:GetDetector",
        "eks:DescribeCluster",
        "ec2:DescribeFlowLogs"
      ],
      "Resource": "*"
    }
  ]
}
```

| 액션 | 쓰는 곳 |
|---|---|
| `rds:DescribeDBInstances` | NET-02, DATA-02, DATA-03, DATA-01 의 엔진·파라미터 그룹 이름 |
| `rds:DescribeDBParameters` | DATA-01 (RDS 인스턴스 파라미터 그룹) |
| `rds:DescribeDBClusters`, `rds:DescribeDBClusterParameters` | DATA-01 (Aurora 만). Aurora 를 쓰지 않으면 빼도 돼요 |
| `guardduty:ListDetectors`, `guardduty:GetDetector` | DET-01 |
| `eks:DescribeCluster` | DET-04 (클러스터 VPC·서브넷 찾기) |
| `ec2:DescribeFlowLogs` | DET-04 |

- 모두 읽기 전용이에요. `guardduty:ListDetectors`, `ec2:DescribeFlowLogs` 는 자원 단위로 좁힐 수 없어 `*` 가 필요해요.
  RDS·EKS 액션은 필요하면 해당 DB·클러스터 ARN 으로 좁혀도 돼요.
- 같은 효과를 내는 AWS 관리형 정책은 `SecurityAudit` 이지만 범위가 훨씬 넓어요. 위 정책을 권장해요.

## GCP: 수집기 서비스 계정에 붙일 역할

`cloud_scope.project` 프로젝트에 아래 역할을 줘요. 수집기는 Application Default Credentials 로 인증해요
(GKE 안이면 Workload Identity, EKS 에서 GCP 를 읽으면 Workload Identity Federation 또는 서비스 계정 키 파일).

| 역할 | 필요한 권한 | 쓰는 곳 |
|---|---|---|
| `roles/cloudsql.viewer` | `cloudsql.instances.get` | NET-02, DATA-01~03 |
| `roles/container.clusterViewer` | `container.clusters.get` | DET-04 (클러스터 서브넷 찾기) |
| `roles/compute.networkViewer` | `compute.subnetworks.get` | DET-04 |
| `roles/securitycentermanagement.viewer` | `securitycentermanagement.securityCenterServices.get` | DET-01 |

읽는 프로젝트(또는 인증 주체의 quota project)에서 다음 API 가 켜져 있어야 해요. 꺼져 있으면 HTTP 403 `SERVICE_DISABLED` 가 오고
화면에는 "API 비활성"으로 표시돼요(권한 없음과 구분).

- `sqladmin.googleapis.com`, `container.googleapis.com`, `compute.googleapis.com`, `securitycentermanagement.googleapis.com`

최소 권한이 필요하면 위 네 권한만 담은 커스텀 역할을 만들어도 돼요.

```sh
gcloud iam roles create oneportSecurityObserve --project <PROJECT_ID> \
  --title "Oneport security observe" \
  --permissions cloudsql.instances.get,container.clusters.get,compute.subnetworks.get,securitycentermanagement.securityCenterServices.get
```

SCC 를 조직 단위로 켰다면 프로젝트 권한만으로 상태를 못 읽을 수 있어요. 그때 DET-01 은 "권한 없음"으로 표시되고 신고값을 써요.

## 설정

`fleet.json` 의 클라우드 환경에 `cloud_scope` 를 둬요. 없으면 실측하지 않고 "실측 범위 미설정"으로 신고값을 써요.

```json
"cloud_scope": {"cluster": "oneaction", "databases": ["oneaction-review-db"]}
```

```json
"cloud_scope": {
  "cluster": "oneaction-dev",
  "cluster_location": "asia-northeast3",
  "project": "<GCP 프로젝트 ID>",
  "databases": ["oneaction-dev-review"]
}
```

- `cluster`: EKS/GKE 클러스터 이름. `cluster_location` 은 GKE 위치(비우면 환경 `region`).
- `databases`: RDS DB 인스턴스 식별자 / Cloud SQL 인스턴스 이름.
- 수집 주기: `ONEPORT_SECURITY_CLOUD_INTERVAL_SECONDS`(기본 300초, 60~86400). 결과는 환경마다 `fleet_cloud_security` 테이블에 마지막 한 건만 남아요.
- 의존성: AWS 는 `aws` 추가 의존성(boto3)이 필요해요. GCP 는 기본 의존성(google-auth, httpx)만 써요.

## 화면에서 보이는 결과

| 상황 | 결과 | 출처 배지 | 상세 |
|---|---|---|---|
| 실측 성공 | 실측값으로 통과/실패 | 관측 | 근거(예: `oneaction-review-db PubliclyAccessible=False`) |
| 실측 실패, 신고값 있음 | 신고값으로 통과/실패 | 신고 | "실측 못 함: 권한 없음: rds:DescribeDBInstances" (그 밖의 이유: 응답 시간 초과, 자격 증명 없음, API 비활성, 대상 자원 없음, 실측 범위 미설정) |
| 실측 실패, 신고값 없음 | 미확인 | 없음 | 실패 이유 |
| 온프렘 | 신고값 | 신고 | - |

권한 부족(AccessDenied, HTTP 403)은 실패가 아니라 실측 실패로 다뤄요. 한 API 의 오류는 그 항목만 실측 실패로 남기고 다른 항목은 계속 읽어요.
