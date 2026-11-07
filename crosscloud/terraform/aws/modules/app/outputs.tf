output "app" {
  description = "앱 층 식별자. 제어 어댑터·트래픽 명령·DNS 모듈 입력."
  value = {
    service_name        = aws_ecs_service.app.name
    task_definition_arn = aws_ecs_task_definition.app.arn
    task_role_arn       = aws_iam_role.task.arn
    alb_arn             = aws_lb.this.arn
    alb_dns_name        = aws_lb.this.dns_name
    alb_zone_id         = aws_lb.this.zone_id
    listener_arn        = aws_lb_listener.http.arn
    target_group_arn    = aws_lb_target_group.app.arn
    security_group_id   = aws_security_group.app.id
  }
}
