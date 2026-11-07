# Secret 그릇과 이미지 저장소. Secret 값은 Terraform 이 쓰지 않는다 — 버전 없는
# 그릇만 만들고 crosscloud 가 값을 넣는다(state 에 비밀값을 두지 않는다).

resource "aws_secretsmanager_secret" "this" {
  for_each = toset(var.secret_names)

  name = "${var.name_prefix}-${each.key}"
  # 해체 즉시 지운다. 복구 대기 중인 Secret 은 이름을 막고 잔여로 남는다.
  recovery_window_in_days = 0
}

resource "aws_ecr_repository" "app" {
  name = "${var.name_prefix}-app"
  # 이미지가 남아 있어도 해체한다.
  force_delete = true
}
