# 이 VPC 에서만 보이는 비공개 호스팅 영역과 가중치 레코드 2개(AWS ALB · GCP LB).
# 원본 쪽 100, 대상 쪽 0 으로 시작한다. 가중치를 옮기는 일은 트래픽 명령이
# Route 53 API 로 하고, Terraform 은 처음 값만 정한다(재 apply 가 이동을 되돌리지
# 않게 weighted_routing_policy 는 무시한다).
#
# 상태 확인(evaluate_target_health)은 끈다 — 어느 쪽으로 보낼지는 가중치만으로
# 정해져야 트래픽 명령의 판정과 실제 흐름이 어긋나지 않는다.

locals {
  zone_name  = "ccx-${var.run_id}.internal"
  aws_weight = var.direction == "aws_to_gcp" ? 100 : 0
}

resource "aws_route53_zone" "this" {
  name = local.zone_name
  # 트래픽 명령이 더한 레코드가 있어도 해체한다.
  force_destroy = true

  vpc {
    vpc_id     = var.vpc_id
    vpc_region = var.region
  }
}

resource "aws_route53_record" "aws" {
  zone_id        = aws_route53_zone.this.zone_id
  name           = var.record_name
  type           = "A"
  set_identifier = "aws"

  weighted_routing_policy {
    weight = local.aws_weight
  }

  alias {
    name                   = var.aws_alb_dns_name
    zone_id                = var.aws_alb_zone_id
    evaluate_target_health = false
  }

  lifecycle {
    ignore_changes = [weighted_routing_policy]
  }
}

resource "aws_route53_record" "gcp" {
  zone_id        = aws_route53_zone.this.zone_id
  name           = var.record_name
  type           = "A"
  set_identifier = "gcp"
  ttl            = var.ttl
  records        = [var.gcp_lb_ip]

  weighted_routing_policy {
    weight = 100 - local.aws_weight
  }

  lifecycle {
    ignore_changes = [weighted_routing_policy]
  }
}
