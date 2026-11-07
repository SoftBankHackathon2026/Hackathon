output "zone_id" {
  description = "비공개 호스팅 영역 ID."
  value       = aws_route53_zone.this.zone_id
}

output "fqdn" {
  description = "트래픽 봇이 부를 이름."
  value       = aws_route53_record.aws.fqdn
}

output "records" {
  description = "가중치 레코드. 트래픽 명령이 set_identifier 로 골라 가중치를 바꾼다."
  value = {
    source = var.direction == "aws_to_gcp" ? local.aws_record : local.gcp_record
    target = var.direction == "aws_to_gcp" ? local.gcp_record : local.aws_record
  }
}

locals {
  aws_record = {
    id             = aws_route53_record.aws.id
    set_identifier = aws_route53_record.aws.set_identifier
    type           = aws_route53_record.aws.type
    initial_weight = local.aws_weight
  }
  gcp_record = {
    id             = aws_route53_record.gcp.id
    set_identifier = aws_route53_record.gcp.set_identifier
    type           = aws_route53_record.gcp.type
    initial_weight = 100 - local.aws_weight
  }
}
