# 자격 증명 없이 plan 만으로 처음 가중치와 출력 형식을 확인한다.

mock_provider "aws" {}

variables {
  run_id           = "r1"
  vpc_id           = "vpc-0123"
  aws_alb_dns_name = "ccx-r1-alb-1.ap-northeast-2.elb.amazonaws.com"
  aws_alb_zone_id  = "ZWKZPGTI48KDX"
  gcp_lb_ip        = "34.120.0.10"
}

run "aws_to_gcp_starts_all_traffic_on_aws" {
  command = plan

  variables {
    direction = "aws_to_gcp"
  }

  assert {
    condition     = aws_route53_record.aws.weighted_routing_policy[0].weight == 100
    error_message = "원본(AWS) 레코드는 100 으로 시작해야 한다."
  }

  assert {
    condition     = aws_route53_record.gcp.weighted_routing_policy[0].weight == 0
    error_message = "대상(GCP) 레코드는 0 으로 시작해야 한다."
  }

  assert {
    condition     = output.records.source.set_identifier == "aws" && output.records.target.set_identifier == "gcp"
    error_message = "aws_to_gcp 에서 원본은 aws, 대상은 gcp 레코드다."
  }

  assert {
    condition     = aws_route53_zone.this.name == "ccx-r1.internal"
    error_message = "영역 이름은 잔여 검사가 찾는 접두사(ccx-<run>.)로 시작해야 한다."
  }

  assert {
    condition     = one(aws_route53_zone.this.vpc).vpc_id == "vpc-0123"
    error_message = "영역은 주어진 VPC 에 붙은 비공개 영역이어야 한다."
  }

  assert {
    condition     = aws_route53_record.gcp.records == toset(["34.120.0.10"])
    error_message = "GCP 레코드는 GCP LB IP 를 가리켜야 한다."
  }
}

run "gcp_to_aws_starts_all_traffic_on_gcp" {
  command = plan

  variables {
    direction = "gcp_to_aws"
  }

  assert {
    condition     = aws_route53_record.gcp.weighted_routing_policy[0].weight == 100
    error_message = "원본(GCP) 레코드는 100 으로 시작해야 한다."
  }

  assert {
    condition     = aws_route53_record.aws.weighted_routing_policy[0].weight == 0
    error_message = "대상(AWS) 레코드는 0 으로 시작해야 한다."
  }

  assert {
    condition     = output.records.source.set_identifier == "gcp" && output.records.target.initial_weight == 0
    error_message = "gcp_to_aws 에서 원본은 gcp 레코드다."
  }
}

run "rejects_non_ipv4_gcp_address" {
  command = plan

  variables {
    direction = "aws_to_gcp"
    gcp_lb_ip = "lb.example.com"
  }

  expect_failures = [var.gcp_lb_ip]
}
