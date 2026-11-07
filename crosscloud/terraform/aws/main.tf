# AWS 쪽 이전 자원 한 벌. 방향(direction)에 따라 같은 구성이 두 역할을 맡는다.
#
#   aws_to_gcp: base + app 이 원본 고정물(source_fixture)이다.
#   gcp_to_aws: base 가 대상 기반(target_base), app 이 대상 앱(target_app)이다.
#               대상 앱은 Secret 값을 넣은 뒤 include_app = true 로 더한다.

locals {
  is_source = var.direction == "aws_to_gcp"
  base_role = local.is_source ? "source_fixture" : "target_base"
  app_role  = local.is_source ? "source_fixture" : "target_app"
}

module "base" {
  source = "./modules/base"

  name_prefix       = var.name_prefix
  vpc_cidr          = var.vpc_cidr
  db_engine_version = var.db_engine_version
  db_instance_class = var.db_instance_class
  db_name           = var.db_name
  db_username       = var.db_username
  secret_names      = var.secret_names
}

module "app" {
  source = "./modules/app"
  count  = var.include_app ? 1 : 0

  name_prefix             = var.name_prefix
  vpc_id                  = module.base.vpc_id
  subnet_ids              = module.base.public_subnet_ids
  cluster_arn             = module.base.cluster_arn
  log_group_name          = module.base.log_group_name
  task_execution_role_arn = module.base.task_execution_role_arn
  image                   = var.app_image
  port                    = var.app_port
  health_path             = var.app_health_path
  env                     = var.app_env
  secret_env              = { for env, name in var.app_secret_env : env => module.base.secret_arns[name] }
  desired_count           = var.app_desired_count
}
