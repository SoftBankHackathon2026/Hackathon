output "app" {
  description = "앱 층 식별자. 전환 단계(앱 멈춤·작업 정의 교체)와 운영자 ingress 가 쓴다."
  value = {
    service_name        = aws_ecs_service.app.name
    task_definition_arn = aws_ecs_task_definition.app.arn
    alb_arn             = aws_lb.this.arn
    alb_dns_name        = aws_lb.this.dns_name
    listener_arn        = aws_lb_listener.http.arn
    target_group_arn    = aws_lb_target_group.app.arn
    security_group_id   = aws_security_group.app.id
  }
}
