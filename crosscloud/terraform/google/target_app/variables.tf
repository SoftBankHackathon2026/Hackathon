variable "image" {
  description = "배포할 컨테이너 이미지(digest 고정 권장, 예: <저장소 주소>/app@sha256:...)."
  type        = string
}

variable "container_port" {
  description = "컨테이너가 HTTP 를 받는 포트."
  type        = number
  default     = 8080
}

variable "env" {
  description = <<-EOT
    평문 env. 비밀이 아닌 값만 넣는다. DB 접속 대상처럼 전환 때 바뀌는 키는
    여기 두지 않고 secret_env 로 넘긴다 — 평문이면 재 apply 가 전환 결과를
    되돌린다.
  EOT
  type        = map(string)
  default     = {}
}

variable "secret_env" {
  description = <<-EOT
    Secret Manager 참조 env. 키는 env 이름, secret 은 같은 프로젝트의 비밀 ID
    (target_base 출력 secret_ids 의 값), version 은 버전 번호 또는 latest.
    실행 서비스 계정에 이 비밀들의 읽기 권한을 준다.
  EOT
  type = map(object({
    secret  = string
    version = string
  }))
  default = {}
}
