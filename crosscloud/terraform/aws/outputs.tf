output "name_prefix" {
  description = "모든 자원 이름 접두사. 잔여 검사(ResidueSelector.name_prefix)에 쓴다."
  value       = local.name_prefix
}

output "roles" {
  description = "층별 placement 역할."
  value = {
    base = local.base_role
    app  = var.include_app ? local.app_role : null
  }
}

output "vpc_id" {
  description = "전용 VPC. 비공개 호스팅 영역을 여기에 붙인다."
  value       = module.base.vpc_id
}

output "public_subnet_ids" {
  description = "퍼블릭 서브넷 2개. 봇 작업을 띄울 때도 쓴다."
  value       = module.base.public_subnet_ids
}

output "db" {
  description = "RDS 접속 정보(비밀값 없음). 비밀번호는 master_user_secret_arn 의 Secret 에 있다."
  value       = module.base.db
}

output "secret_arns" {
  description = "Secret 그릇 이름 → ARN."
  value       = module.base.secret_arns
}

output "ecr_repository_url" {
  description = "앱 이미지 저장소."
  value       = module.base.ecr_repository_url
}

output "cluster_arn" {
  description = "ECS 클러스터."
  value       = module.base.cluster_arn
}

output "bot" {
  description = "트래픽 봇 실행에 필요한 값. 봇 이미지가 없으면 null."
  value       = module.base.bot
}

output "app" {
  description = "앱 층(ECS 서비스·ALB). include_app 이 false 면 null."
  value       = var.include_app ? module.app[0].app : null
}
