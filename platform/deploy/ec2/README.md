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
| 보안그룹 | `oneport-platform-sg` | 443은 전체에 열어요. 누구나 화면에 들어올 수 있고, 들어오면 [접속 PIN](#접속-pin-임시) 화면부터 보여요. 80도 전체에 열지만 Caddy가 Let's Encrypt 인증(HTTP-01) 응답과 HTTPS 리다이렉트만 해요. SSH는 열지 않고 SSM으로 들어가요 |
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

## 접속 PIN (임시)

화면 맨 앞에 숫자 4자리 비밀번호 화면을 둬요. PIN 을 맞히기 전에는 API(`/api/*`)가 모두 401 을 돌려줘서, 개발자 도구로 화면만 바꿔서는 데이터를 볼 수 없어요.

- 맞히면 서명한 쿠키(HttpOnly·Secure·SameSite=Strict, 12시간)를 줘요. 서명 키는 API 프로세스가 뜰 때마다 새로 만들어서, 다시 배포하면 PIN 을 다시 입력해요.
- 10분 안에 10번 틀리면 10분 동안 맞는 PIN 도 받지 않아요(전체 기준).
- 보고기 ingest 와 클러스터 등록 스크립트 경로(`/api/v1/ingest/…`, `/api/v1/enrollments/scripts/…`, `/api/v1/enrollments/<id>/complete`)는 자기 토큰으로 인증해서 PIN 을 묻지 않아요.

켜기·바꾸기 (인스턴스에서 root)

```sh
cd /opt/oneport && umask 077
printf 'PIN 4자리: '; stty -echo; read -r pin; stty echo; echo
printf 'ONEPORT_ACCESS_PIN=%s\n' "$pin" > secrets/access.env; pin=
chmod 0400 secrets/access.env
docker compose up -d oneport-api
```

끄기는 `secrets/access.env` 를 지우고 `docker compose up -d oneport-api` 해요. 코드를 걷어낼 때는 `controlplane/api/access_pin.py`, `main.py` 의 `access_pin.install` 한 줄, 프론트 `features/access-pin/`, `App.tsx` 의 `PinGate` 감싸기를 지우면 돼요.

## 배포

- 자동: main에 `platform/backend`, `platform/frontend`, `platform/deploy/ec2`, `platform/deploy/aws/fleet.json`, `platform/deploy/aws/cost.json`이 바뀌면 돌아요. 이미지 태그는 커밋 해시예요.
- 수동: Actions에서 `platform-ec2`를 `Run workflow`.
- 인스턴스에서는 `/opt/oneport/releases/<커밋>`에 풀린 `deploy.sh`가 `docker compose pull` → `up --wait` → `/healthz` 확인 순서로 돌아요. 최근 5개 릴리즈만 남겨요.

되돌리기는 예전 커밋으로 워크플로를 다시 돌리거나, 인스턴스에서 그 커밋 폴더의 `deploy.sh`를 다시 부르면 돼요.

```sh
bash /opt/oneport/releases/<커밋>/deploy.sh /opt/oneport/releases/<커밋> <레지스트리> <커밋>
```
