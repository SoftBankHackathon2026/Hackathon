# EC2 한 대에 플랫폼 띄우기

EKS 밖에서 플랫폼을 돌리는 구성이에요. 도쿄 리전(`ap-northeast-1`) t3.small 한 대에 `docker compose`로 DB·API·수집기·화면을 올리고, Caddy가 Let's Encrypt 인증서로 HTTPS를 붙여요. main에 플랫폼 변경이 들어오면 [`platform-ec2`](../../../.github/workflows/platform-ec2.yml) 워크플로가 이미지를 서울 ECR에 올리고 SSM으로 이 인스턴스에 배포해요.

서울 계정의 온디맨드 vCPU 한도(8)를 EKS 노드와 Tailscale 라우터가 다 쓰고 있어서, 한도가 따로 매겨지는 도쿄에 온디맨드로 띄워요. 스팟처럼 회수될 걱정이 없어요. 홈랩까지 Tailscale 직접 연결로 40ms 안팎이에요.

```
GitHub Actions ──OIDC──▶ 배포 역할 ──▶ ECR push
                                   └─▶ SSM send-command ──▶ EC2 deploy.sh ──▶ docker compose up
EC2 수집기 ──Tailscale──▶ 온프렘 k3s
          ──공개 엔드포인트(허용 IP)──▶ EKS (서울)
          ──공개 엔드포인트──▶ GKE
```

## AWS 자원

| 자원 | 이름 | 비고 |
|---|---|---|
| 인스턴스 | 태그 `Name=oneport-platform`, `project=oneport` | 도쿄 기본 VPC, t3.small 온디맨드, Amazon Linux 2023, gp3 20GiB 암호화, IMDSv2 필수. user-data 는 [`user-data.sh`](user-data.sh) |
| 고정 IP | 태그 `Name=oneport-platform` | 도메인 A 레코드가 가리키는 주소예요. 인스턴스를 멈췄다 켜도 바뀌지 않아요 |
| 보안그룹 | `oneport-platform-sg` | 443은 전체에 열어요. 누구나 화면에 들어올 수 있어요. 80도 전체에 열지만 Caddy가 Let's Encrypt 인증(HTTP-01) 응답과 HTTPS 리다이렉트만 해요. SSH는 열지 않고 SSM으로 들어가요 |
| EKS 공개 엔드포인트 | 허용 IP(`publicAccessCidrs`) | 이 인스턴스 고정 IP를 더해요. 리전이 달라 VPC 비공개 엔드포인트는 쓰지 않아요 |
| EC2 역할 | `ghost-oneport-platform-ec2` | `AmazonSSMManagedInstanceCore` + 서울 `ghost/oneport-platform-*` ECR 읽기 |
| 배포 역할 | `ghost-oneport-platform-deploy` | 아래 정책. GitHub OIDC 공급자가 있어야 만들 수 있어요 |

팀 SSO 권한(`GhostInfraEngineer`)으로는 이름이 `ghost-`로 시작하고 권한 경계 `GhostWorkloadBoundary`를 붙인 역할만 만들 수 있어요. GitHub OIDC 공급자(`token.actions.githubusercontent.com`)는 계정 관리자가 한 번 만들어요.

### 배포 역할 정책

`<ACCOUNT_ID>`는 배포할 때 채워요. 신뢰 정책은 이 저장소의 main만 받아요. 이 저장소는 OIDC `sub`에 바뀌지 않는 형식(`repo:<조직>@<조직 ID>/<저장소>@<저장소 ID>:...`)을 써서 그 값을 넣어요. 형식은 `gh api repos/SoftBankHackathon2026/Hackathon/actions/oidc/customization/sub`의 `sub_claim_prefix`로 확인해요. 이름 형식도 같이 둬서 설정이 바뀌어도 막히지 않게 해요.

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Federated": "arn:aws:iam::<ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com"},
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {"StringEquals": {
      "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub": [
        "repo:SoftBankHackathon2026@350755067/Hackathon@1440930975:ref:refs/heads/main",
        "repo:SoftBankHackathon2026/Hackathon:ref:refs/heads/main"
      ]
    }}
  }]
}
```

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
    {"Effect": "Allow",
     "Action": ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
                "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage"],
     "Resource": "arn:aws:ecr:ap-northeast-2:<ACCOUNT_ID>:repository/ghost/oneport-platform-*"},
    {"Effect": "Allow", "Action": "ssm:SendCommand",
     "Resource": "arn:aws:ssm:ap-northeast-1::document/AWS-RunShellScript"},
    {"Effect": "Allow", "Action": "ssm:SendCommand",
     "Resource": "arn:aws:ec2:ap-northeast-1:<ACCOUNT_ID>:instance/*",
     "Condition": {"StringEquals": {"ssm:resourceTag/project": "oneport"}}},
    {"Effect": "Allow", "Action": ["ssm:ListCommands", "ssm:ListCommandInvocations", "ssm:GetCommandInvocation"],
     "Resource": "*"}
  ]
}
```

역할을 만든 뒤 ARN을 저장소 secret `ONEPORT_DEPLOY_ROLE_ARN`에 넣고, 저장소 변수 `ONEPORT_EC2_CD=enabled`로 켜요(Settings → Secrets and variables → Actions). 공개 저장소라 ARN을 변수로 두면 계정 번호가 Actions 로그에 찍혀서 secret 으로 둬요.

## 처음 한 번 준비 (인스턴스 안, root)

SSM으로 들어가요.

```sh
aws ssm start-session --region ap-northeast-1 --target <인스턴스 ID>
sudo -i
```

**1. Tailscale**. 온프렘 k3s 로 가는 길이에요. 출력된 주소를 tailnet 관리자가 승인해요.

```sh
tailscale up --accept-routes --accept-dns=false --hostname=oneport-platform
```

**2. 비밀값**. 화면에 값을 출력하지 않아요. 파일마다 그 파일을 읽는 컨테이너 사용자만 읽게 해요(postgres 70, API·수집기 10001).

```sh
cd /opt/oneport && umask 077
pw=$(openssl rand -hex 24)
printf '%s' "$pw" > secrets/postgres-password
printf 'postgresql://oneport:%s@postgres:5432/oneport' "$pw" > secrets/database-url
printf '%s' "$(openssl rand -hex 32)" > secrets/ingest-token
pw=
chown 70:70 secrets/postgres-password
chown 10001:10001 secrets/database-url secrets/ingest-token
chmod 0400 secrets/*
```

**3. 화면 주소**. 도메인의 A 레코드를 고정 IP로 먼저 맞춰요. Caddy가 처음 뜰 때 그 도메인으로 인증서를 받아요.

```sh
cat > settings.env <<EOF
ONEPORT_DOMAIN=<도메인>
ONEPORT_AUTH_MODE=demo
ONEPORT_ALLOWED_HOSTS=<도메인>,localhost
ONEPORT_ALLOWED_ORIGINS=https://<도메인>
EOF
```

**4. 읽기 전용 kubeconfig** 세 개를 `/opt/oneport/kube/`에 넣고 `chown 10001:10001`, `chmod 0400` 해요.

| 파일 | 만드는 법 |
|---|---|
| `onprem.kubeconfig` | `infra/install/onprem-k8s/generate-kubeconfig.sh`의 `hackathon-user` |
| `aws.kubeconfig` | EKS에 `deploy/reader/reader.yaml`을 적용하고 `deploy/reader/make-kubeconfig.sh oneaction-eks aws.kubeconfig`. 주소는 EKS 공개 엔드포인트예요 |
| `gcp.kubeconfig` | GKE에 `deploy/reader/reader.yaml`을 적용하고 `deploy/reader/make-kubeconfig.sh oneaction-gke gcp.kubeconfig`. GKE 승인 네트워크를 좁혀 두었다면 고정 IP를 더해요 |

워크플로가 `deploy/aws/fleet.json`에서 AWS 환경만 `in_cluster` 대신 `aws.kubeconfig`(context `oneaction-eks`)를 쓰게 바꿔서 보내요. 비용 설정 `deploy/aws/cost.json`은 그대로 보내요.

## 배포

- 자동: main에 `platform/backend`, `platform/frontend`, `platform/deploy/ec2`, `platform/deploy/aws/fleet.json`, `platform/deploy/aws/cost.json`이 바뀌면 돌아요. 이미지 태그는 커밋 해시예요.
- 수동: Actions에서 `platform-ec2`를 `Run workflow`.
- 배포 전에 [`platform-ci`](../../../.github/workflows/platform-ci.yml)의 백엔드·프론트 검사를 같은 커밋으로 먼저 돌려요(`workflow_call`). 검사가 실패하면 이미지도 올리지 않고 배포도 하지 않아요.
- ECR 저장소는 태그를 덮어쓸 수 없어요. 같은 커밋을 다시 돌리면 이미 올라간 이미지는 빌드·push 를 건너뛰고 배포만 다시 해요.
- 인스턴스에서는 `/opt/oneport/releases/<커밋>`에 풀린 `deploy.sh`가 `docker compose pull` → `up --wait` → `/healthz` 확인 순서로 돌아요. 최근 5개 릴리즈만 남겨요.
- 이 중 하나라도 실패하면 `deploy.sh`가 덮어쓰기 전에 남겨 둔 직전 `ONEPORT_TAG`와 `compose.yaml`·`Caddyfile`·`fleet.json`·`cost.json`으로 되돌려 다시 띄우고, 실패로 끝나요. 워크플로도 실패로 표시돼요. 처음 배포처럼 직전 기록(`.env`)이 없으면 되돌리지 않아요.
- `up --wait`는 API·DB·수집기 healthcheck 가 모두 healthy 가 될 때까지 기다려요. 수집기는 tick 마다 `/tmp/collector-heartbeat`를 갱신하고, 10분 넘게 갱신이 없으면 unhealthy 예요. docker 는 unhealthy 컨테이너를 스스로 재시작하지 않으니 `docker compose ps`로 보고 직접 재시작해요.
- 컨테이너마다 `mem_limit`을 둬서(합계 약 1.3GiB) 하나가 메모리를 다 써도 t3.small(2GiB) 전체가 멈추지 않게 해요.

되돌리기는 예전 커밋으로 워크플로를 다시 돌리거나, 인스턴스에서 그 커밋 폴더의 `deploy.sh`를 다시 부르면 돼요.

```sh
bash /opt/oneport/releases/<커밋>/deploy.sh /opt/oneport/releases/<커밋> <레지스트리> <커밋>
```

## GCP 관리형 서비스 읽기 (Workload Identity Federation)

비용 화면의 GCP 관리형 서비스(Cloud SQL, Managed Kafka, LB, Cloud NAT, VM, GCS)는 수집기가 GCP API 로 읽는다. 키 파일 없이 EC2 인스턴스 역할로 GCP 서비스 계정을 대신 쓴다.

```sh
GCP_PROJECT=<프로젝트 ID> AWS_PROFILE=<프로필> bash platform/deploy/ec2/gcp-wif.sh
```

- GCP 에 워크로드 ID 풀 `oneport-aws`, AWS 공급자 `oneport-ec2`, 서비스 계정 `oneport-cost-reader` 를 만든다. 공급자 조건은 인스턴스 역할 `ghost-oneport-platform-ec2` 하나만 받는다.
- 서비스 계정 역할: `roles/cloudsql.viewer`, `roles/managedkafka.viewer`, `roles/compute.viewer`, `roles/storage.bucketViewer`, `roles/monitoring.viewer` (모두 읽기 전용).
- 자격 증명 설정 파일(비밀값 없음, IMDSv2 로 AWS 자격 증명을 얻는 방법만 적힘)을 SSM 으로 `/opt/oneport/secrets/gcp/credentials.json` 에 두고 수집기를 다시 띄운다. compose 는 이 폴더를 `/etc/oneport/gcp` 로 붙이고 `GOOGLE_APPLICATION_CREDENTIALS` 를 가리킨다.
- 파일이 없으면 수집기는 "관리형 서비스: GCP 읽기 실패 (DefaultCredentialsError)" 로 남기고 나머지는 그대로 계산한다.
- 돌리는 사람은 GCP 프로젝트 IAM 관리 권한과, AWS 쪽 `sts:GetCallerIdentity`·`ec2:DescribeInstances`·`ssm:SendCommand` 권한이 있어야 한다.

## 온프렘 환경 연결 (#48)

화면의 「환경 연결 추가」에서 복사한 명령을 단말에서 실행하면, 단말이 키 중계(broker)와 TLS 1.3·ML-DSA 로 붙고 WireGuard 터널로 Kubernetes 환경정보를 보내요. 데모 모드에서도 켜지고, 그때는 설정한 tenant 하나로 동작해요.

| 구성 | 위치 |
|---|---|
| API 연결 층 | `migration/connection/platform_bridge/Dockerfile` 이 API 이미지 위에 연결 패키지와 OpenSSL 3.5 를 얹어요 |
| Redis, 키 중계 | compose profile `connection`. 중계는 TCP 8443, UDP 51820 을 써요(보안그룹에서 열어 둬야 해요) |
| 단말 배포본 | Caddy 가 `/opt/oneport/connection/public` 을 `https://<도메인>/releases/` 로 내요 |
| 키·설정 | `/opt/oneport/connection/` 의 `server.env`, `server/`(작업 서명키, 런타임 토큰), `broker/`(중계 설정, 인증서, 키, 네트워크 정책) |

CD 는 저장소 변수 `ONEPORT_CONNECTION_RELEASE_MLDSA_PUBLIC_KEY`, `ONEPORT_CONNECTION_RELEASE_ED25519_PUBLIC_KEY`(공개 검증키)가 있을 때 중계 이미지를 빌드하고, 인스턴스에 `server.env` 와 `broker/broker.json` 이 있으면 profile 을 켜요. 없으면 지금처럼 연결 기능 없이 떠요.

키와 서명한 배포본은 CI 에 두지 않고 로컬에서 만들어요. 개인키는 로컬 0700 폴더에만 두고 레포·이미지·로그에 넣지 않아요.

```sh
mkdir -m 700 ~/.oneport-connection
python3 platform/deploy/ec2/connection.py --dir ~/.oneport-connection --domain <도메인> --openssl <OpenSSL 3.5 이상> trust
# 같은 공개 검증키로 amd64·arm64 단말 프로그램을 빌드한 뒤 (broker.Dockerfile 의 build 단계)
python3 platform/deploy/ec2/connection.py --dir ~/.oneport-connection --domain <도메인> --openssl <OpenSSL> \
  release --version <버전> --sequence <이전보다 큰 수> --days 30 --endpoint <고정 IP>:51820 --amd64 <폴더> --arm64 <폴더>
python3 platform/deploy/ec2/connection.py --dir ~/.oneport-connection --domain <도메인> config --tenant <tenant> --gateway <gateway>
```

`server.env`, `server/`, `broker/` 는 SSM SecureString 임시 파라미터로 옮기고 바로 지워요. `release/<버전>` 은 인스턴스의 `/opt/oneport/connection/public/<버전>` 에 두어요(0755/0644). 배포본 서명 기한(기본 30일)이 지나거나 중계 인증서(397일)가 끝나기 전에 다시 만들어요.

- 단말은 Ubuntu 24.04(amd64, arm64)만 받아요. 온프렘 노드(22.04)에서는 설치 스크립트가 멈춰요.
- 중계는 한 번에 단말 하나와 터널을 열어요.
