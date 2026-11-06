# EC2 한 대에 플랫폼 띄우기

EKS 밖에서 플랫폼을 돌리는 구성이에요. t3.small 한 대에 `docker compose`로 DB·API·수집기·화면을 올려요. main에 플랫폼 변경이 들어오면 [`platform-ec2`](../../../.github/workflows/platform-ec2.yml) 워크플로가 이미지를 ECR에 올리고 SSM으로 이 인스턴스에 배포해요.

```
GitHub Actions ──OIDC──▶ 배포 역할 ──▶ ECR push
                                   └─▶ SSM send-command ──▶ EC2 deploy.sh ──▶ docker compose up
EC2 수집기 ──Tailscale──▶ 온프렘 k3s
          ──VPC 비공개 엔드포인트──▶ EKS
          ──공개 엔드포인트──▶ GKE
```

## AWS 자원

| 자원 | 이름 | 비고 |
|---|---|---|
| 인스턴스 | 태그 `Name=oneport-platform`, `project=oneport` | t3.small, Amazon Linux 2023, gp3 20GiB 암호화, IMDSv2 필수, public 서브넷. user-data 는 [`user-data.sh`](user-data.sh). 온디맨드 vCPU 한도가 차 있으면 스팟(`persistent`, 회수 시 `stop`)으로 띄워요 |
| 고정 IP | 태그 `Name=oneport-platform` | 화면 주소와 `ONEPORT_ALLOWED_HOSTS`가 바뀌지 않게 고정해요 |
| 보안그룹 | `oneport-platform-sg` | 인바운드는 80 하나, 화면을 볼 사람 IP만 열어요. SSH는 열지 않고 SSM으로 들어가요. macOS에서 iCloud 비공개 릴레이를 켜 두면 HTTP 요청이 Apple 릴레이 IP로 나가서 막혀요. 그 네트워크에서 'IP 주소 추적 제한'을 꺼요 |
| EKS 클러스터 보안그룹 | 규칙 하나 | `oneport-platform-sg` → 443. 수집기가 EKS 비공개 엔드포인트로 들어가요 |
| EC2 역할 | `ghost-oneport-platform-ec2` | `AmazonSSMManagedInstanceCore` + `ghost/oneport-platform-*` ECR 읽기 |
| 배포 역할 | `ghost-oneport-platform-deploy` | 아래 정책. GitHub OIDC 공급자가 있어야 만들 수 있어요 |

팀 SSO 권한(`GhostInfraEngineer`)으로는 이름이 `ghost-`로 시작하고 권한 경계 `GhostWorkloadBoundary`를 붙인 역할만 만들 수 있어요. GitHub OIDC 공급자(`token.actions.githubusercontent.com`)는 계정 관리자가 한 번 만들어요.

### 배포 역할 정책

`<ACCOUNT_ID>`는 배포할 때 채워요. 신뢰 정책은 이 저장소의 main만 받아요.

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Federated": "arn:aws:iam::<ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com"},
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {"StringEquals": {
      "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub": "repo:SoftBankHackathon2026/Hackathon:ref:refs/heads/main"
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
     "Resource": "arn:aws:ssm:ap-northeast-2::document/AWS-RunShellScript"},
    {"Effect": "Allow", "Action": "ssm:SendCommand",
     "Resource": "arn:aws:ec2:ap-northeast-2:<ACCOUNT_ID>:instance/*",
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
aws ssm start-session --target <인스턴스 ID>
sudo -i
```

**1. Tailscale**. 온프렘 k3s 로 가는 길이에요. 출력된 주소를 tailnet 관리자가 승인해요. AWS 라우터가 VPC 대역(`10.0.0.0/16`)을 광고해서, `user-data.sh`의 `oneport-vpc-bypass.service`가 VPC 안 트래픽(EKS 비공개 엔드포인트, VPC DNS)은 Tailscale 로 보내지 않게 해요.

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

**3. 화면 주소**. 고정 IP를 넣어요.

```sh
cat > settings.env <<EOF
ONEPORT_AUTH_MODE=demo
ONEPORT_ALLOWED_HOSTS=<고정 IP>,localhost
ONEPORT_ALLOWED_ORIGINS=http://<고정 IP>
EOF
```

**4. 읽기 전용 kubeconfig** 세 개를 `/opt/oneport/kube/`에 넣고 `chown 10001:10001`, `chmod 0400` 해요.

| 파일 | 만드는 법 |
|---|---|
| `onprem.kubeconfig` | `infra/install/onprem-k8s/generate-kubeconfig.sh`의 `hackathon-user` |
| `aws.kubeconfig` | EKS에 `deploy/reader/reader.yaml`을 적용하고 `deploy/reader/make-kubeconfig.sh oneaction-eks aws.kubeconfig`. 주소는 EKS 엔드포인트 그대로(VPC 안에서는 비공개 주소로 풀려요) |
| `gcp.kubeconfig` | GKE에 `deploy/reader/reader.yaml`을 적용하고 `deploy/reader/make-kubeconfig.sh oneaction-gke gcp.kubeconfig`. GKE 승인 네트워크를 좁혀 두었다면 고정 IP를 더해요 |

워크플로가 `deploy/aws/fleet.json`에서 AWS 환경만 `in_cluster` 대신 `aws.kubeconfig`(context `oneaction-eks`)를 쓰게 바꿔서 보내요.

## 배포

- 자동: main에 `platform/backend`, `platform/frontend`, `platform/deploy/ec2`, `platform/deploy/aws/fleet.json`이 바뀌면 돌아요. 이미지 태그는 커밋 해시예요.
- 수동: Actions에서 `platform-ec2`를 `Run workflow`.
- 인스턴스에서는 `/opt/oneport/releases/<커밋>`에 풀린 `deploy.sh`가 `docker compose pull` → `up --wait` → `/healthz` 확인 순서로 돌아요. 최근 5개 릴리즈만 남겨요.

되돌리기는 예전 커밋으로 워크플로를 다시 돌리거나, 인스턴스에서 그 커밋 폴더의 `deploy.sh`를 다시 부르면 돼요.

```sh
bash /opt/oneport/releases/<커밋>/deploy.sh /opt/oneport/releases/<커밋> <레지스트리> <커밋>
```
