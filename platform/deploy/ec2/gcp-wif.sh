#!/usr/bin/env bash
# 플랫폼 EC2 수집기가 키 파일 없이 GCP 관리형 서비스를 읽게 한다 (AWS → GCP Workload Identity Federation).
#   GCP_PROJECT=<프로젝트 ID> AWS_PROFILE=<프로필> bash platform/deploy/ec2/gcp-wif.sh
# 하는 일
#   1. GCP 에 워크로드 ID 풀·AWS 공급자를 만든다. EC2 인스턴스 역할(ghost-oneport-platform-ec2)만 들어올 수 있다.
#   2. 읽기 전용 서비스 계정을 만들고 비용 수집에 필요한 viewer 역할 5개를 준다.
#   3. 그 역할이 서비스 계정을 대신 쓰도록 허용한다.
#   4. 자격 증명 설정 파일(비밀값 없음)을 만들어 SSM 으로 EC2 /opt/oneport/secrets/gcp/credentials.json 에 둔다.
# 여러 번 돌려도 된다. 이미 있는 것은 건너뛴다.
set -euo pipefail

project=${GCP_PROJECT:?GCP_PROJECT 에 프로젝트 ID 를 넣으세요}
aws_role=${AWS_ROLE:-ghost-oneport-platform-ec2}
instance_tag=${INSTANCE_TAG:-oneport-platform}
instance_region=${INSTANCE_REGION:-ap-northeast-1}
pool=oneport-aws
provider=oneport-ec2
account_name=oneport-cost-reader
roles=(roles/cloudsql.viewer roles/managedkafka.viewer roles/compute.viewer roles/storage.bucketViewer roles/monitoring.viewer)

gcloud projects describe "$project" --format='value(projectId)' >/dev/null
number=$(gcloud projects describe "$project" --format='value(projectNumber)')
aws_account=$(aws sts get-caller-identity --query Account --output text)
service_account="$account_name@$project.iam.gserviceaccount.com"
role_arn="arn:aws:sts::$aws_account:assumed-role/$aws_role"

retry() {
  local attempt
  for attempt in 1 2 3 4 5 6; do
    "$@" >/dev/null && return 0
    echo "새로 만든 서비스 계정이 아직 보이지 않아 10초 뒤 다시 합니다 ($attempt/6)." >&2
    sleep 10
  done
  "$@" >/dev/null
}

gcloud services enable iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com --project "$project"

gcloud iam workload-identity-pools describe "$pool" --project "$project" --location global >/dev/null 2>&1 ||
  gcloud iam workload-identity-pools create "$pool" --project "$project" --location global \
    --display-name "Oneport platform on AWS"

gcloud iam workload-identity-pools providers describe "$provider" --project "$project" --location global \
  --workload-identity-pool "$pool" >/dev/null 2>&1 ||
  gcloud iam workload-identity-pools providers create-aws "$provider" --project "$project" --location global \
    --workload-identity-pool "$pool" --account-id "$aws_account" \
    --attribute-mapping "google.subject=assertion.arn,attribute.aws_role=assertion.arn.contains('assumed-role') ? assertion.arn.extract('{account_arn}assumed-role/') + 'assumed-role/' + assertion.arn.extract('assumed-role/{role_name}/') : assertion.arn" \
    --attribute-condition "attribute.aws_role == '$role_arn'"

gcloud iam service-accounts describe "$service_account" --project "$project" >/dev/null 2>&1 ||
  gcloud iam service-accounts create "$account_name" --project "$project" \
    --display-name "Oneport cost collector (read only)"

for role in "${roles[@]}"; do
  retry gcloud projects add-iam-policy-binding "$project" --member "serviceAccount:$service_account" \
    --role "$role" --condition None
done

retry gcloud iam service-accounts add-iam-policy-binding "$service_account" --project "$project" \
  --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/$number/locations/global/workloadIdentityPools/$pool/attribute.aws_role/$role_arn"

config=$(mktemp)
trap 'rm -f "$config"' EXIT
gcloud iam workload-identity-pools create-cred-config \
  "projects/$number/locations/global/workloadIdentityPools/$pool/providers/$provider" \
  --service-account "$service_account" --aws --enable-imdsv2 --output-file "$config"

instance=$(aws ec2 describe-instances --region "$instance_region" \
  --filters "Name=tag:Name,Values=$instance_tag" "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].InstanceId' --output text)
encoded=$(base64 < "$config" | tr -d '\n')
script="install -d -m 0755 /opt/oneport/secrets/gcp && echo $encoded | base64 -d > /opt/oneport/secrets/gcp/credentials.json && chown 10001:10001 /opt/oneport/secrets/gcp/credentials.json && chmod 0444 /opt/oneport/secrets/gcp/credentials.json && cd /opt/oneport && docker compose restart oneport-collector >/dev/null && echo placed"
command=$(aws ssm send-command --region "$instance_region" --instance-ids "$instance" \
  --document-name AWS-RunShellScript --parameters "commands=[\"$script\"]" \
  --query Command.CommandId --output text)
aws ssm wait command-executed --region "$instance_region" --command-id "$command" --instance-id "$instance"
aws ssm get-command-invocation --region "$instance_region" --command-id "$command" --instance-id "$instance" \
  --query StandardOutputContent --output text
echo "완료: $service_account 를 $aws_role 역할이 키 없이 씁니다."
