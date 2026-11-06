#!/bin/sh
# Oneport 환경 연결 — AWS 계정
#
# aws 명령이 되는 셸(IAM 역할을 만들 수 있는 권한)에서 실행합니다. 이 스크립트가 하는 일:
#   1. 읽기 전용 IAM 역할을 만듭니다(이미 있으면 신뢰 정책만 갱신). 이 역할은 플랫폼의 AWS 주체만, 이 등록의
#      External ID 를 붙였을 때만 맡을 수 있습니다.
#   2. 역할에 AWS 관리형 정책을 붙입니다. 기본은 ViewOnlyAccess + SecurityAudit (설정·메타데이터 조회, 데이터 내용 조회 없음).
#      --policy readonly 를 주면 ReadOnlyAccess(S3 객체 등 데이터 읽기 포함)를 붙입니다.
#   3. 계정 번호·별칭과 리전의 EKS·RDS·VPC·MSK 개수를 읽습니다.
#   4. 역할 ARN 과 결과를 플랫폼의 등록 주소로 보냅니다. 액세스 키는 만들지도 보내지도 않습니다.
#      등록 토큰은 한 번만 쓸 수 있고 1시간 뒤 만료됩니다.
#
# 사용법:
#   sh oneport-enroll-aws.sh --platform <플랫폼 주소> --enrollment <등록 ID> --token <등록 토큰> \
#     --trust-principal <플랫폼 AWS 주체 ARN> --external-id <External ID> \
#     [--role-name oneport-readonly] [--permissions-boundary <정책 ARN>] [--region <리전>] [--policy viewonly|readonly]
set -eu

PLATFORM= ENROLLMENT= TOKEN= PRINCIPAL= EXTERNAL_ID= ROLE_NAME=oneport-readonly BOUNDARY= REGION= POLICY=viewonly
while [ $# -gt 0 ]; do
  case "$1" in
    --platform) PLATFORM=$2; shift 2 ;;
    --enrollment) ENROLLMENT=$2; shift 2 ;;
    --token) TOKEN=$2; shift 2 ;;
    --trust-principal) PRINCIPAL=$2; shift 2 ;;
    --external-id) EXTERNAL_ID=$2; shift 2 ;;
    --role-name) ROLE_NAME=$2; shift 2 ;;
    --permissions-boundary) BOUNDARY=$2; shift 2 ;;
    --region) REGION=$2; shift 2 ;;
    --policy) POLICY=$2; shift 2 ;;
    *) echo "알 수 없는 옵션: $1" >&2; exit 2 ;;
  esac
done
[ -n "$PLATFORM" ] && [ -n "$ENROLLMENT" ] && [ -n "$TOKEN" ] && [ -n "$PRINCIPAL" ] && [ -n "$EXTERNAL_ID" ] || {
  echo "--platform, --enrollment, --token, --trust-principal, --external-id 가 필요합니다." >&2; exit 2; }
case "$POLICY" in
  viewonly) POLICIES="arn:aws:iam::aws:policy/job-function/ViewOnlyAccess arn:aws:iam::aws:policy/SecurityAudit" ;;
  readonly) POLICIES="arn:aws:iam::aws:policy/ReadOnlyAccess" ;;
  *) echo "--policy 는 viewonly 또는 readonly 입니다." >&2; exit 2 ;;
esac
for tool in aws curl; do command -v "$tool" >/dev/null 2>&1 || { echo "필요한 명령이 없습니다: $tool" >&2; exit 1; }; done
REGION=${REGION:-$(aws configure get region 2>/dev/null || true)}
REGION=${REGION:-${AWS_REGION:-${AWS_DEFAULT_REGION:-}}}
[ -n "$REGION" ] || { echo "리전을 알 수 없습니다. --region 을 주세요." >&2; exit 2; }

echo "[1/4] 계정 확인"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
CALLER=$(aws sts get-caller-identity --query Arn --output text)
ALIAS=$(aws iam list-account-aliases --query 'AccountAliases[0]' --output text 2>/dev/null || true)
[ "$ALIAS" = "None" ] && ALIAS=
echo "      계정 $ACCOUNT${ALIAS:+ ($ALIAS)}, 리전 $REGION, 실행 주체 $CALLER"

umask 077
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT INT TERM
cat >"$WORK/trust.json" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"AWS": "$PRINCIPAL"},
    "Action": "sts:AssumeRole",
    "Condition": {"StringEquals": {"sts:ExternalId": "$EXTERNAL_ID"}}
  }]
}
JSON

echo "[2/4] 읽기 전용 역할 만들기 ($ROLE_NAME)"
if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  aws iam update-assume-role-policy --role-name "$ROLE_NAME" --policy-document "file://$WORK/trust.json"
  echo "      이미 있는 역할의 신뢰 정책을 이 등록으로 바꿨습니다."
else
  if [ -n "$BOUNDARY" ]; then
    aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document "file://$WORK/trust.json" \
      --permissions-boundary "$BOUNDARY" --description "Oneport platform read-only access" --query Role.Arn --output text >/dev/null
  else
    aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document "file://$WORK/trust.json" \
      --description "Oneport platform read-only access" --query Role.Arn --output text >/dev/null
  fi
fi
for policy in $POLICIES; do
  aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn "$policy"
done
ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text)

echo "[3/4] 환경 정보 읽기"
count() { aws "$@" --region "$REGION" --output text 2>/dev/null || echo 0; }
EKS=$(count eks list-clusters --query 'length(clusters)')
RDS=$(count rds describe-db-instances --query 'length(DBInstances)')
VPCS=$(count ec2 describe-vpcs --query 'length(Vpcs)')
MSK=$(count kafka list-clusters-v2 --query 'length(ClusterInfoList)')
echo "      EKS ${EKS}개, RDS ${RDS}개, VPC ${VPCS}개, MSK ${MSK}개"

if [ -n "$ALIAS" ]; then ALIAS_JSON="\"$ALIAS\""; else ALIAS_JSON=null; fi
printf '{"kind":"aws","account_id":"%s","account_alias":%s,"caller_arn":"%s","role_arn":"%s","external_id":"%s","region":"%s","eks_clusters":%s,"rds_instances":%s,"vpcs":%s,"msk_clusters":%s}' \
  "$ACCOUNT" "$ALIAS_JSON" "$CALLER" "$ROLE_ARN" "$EXTERNAL_ID" "$REGION" "$EKS" "$RDS" "$VPCS" "$MSK" >"$WORK/payload.json"

echo "[4/4] 플랫폼에 등록"
status=$(curl -sS -o "$WORK/response" -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  --data-binary @"$WORK/payload.json" "${PLATFORM%/}/api/v1/enrollments/$ENROLLMENT/complete")
if [ "$status" != "200" ]; then
  echo "등록하지 못했습니다 (HTTP $status): $(cat "$WORK/response")" >&2
  exit 1
fi
echo "등록을 마쳤습니다. 플랫폼 화면에서 환경 정보를 확인하세요."
