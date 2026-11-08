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

variable "readable_secrets" {
  description = <<-EOT
    secret_env 밖에서 실행 신원에 읽기 권한만 줄 비밀 ID. 나중 리비전(전환 때
    대상 프로필로 재배포)이 읽을 비밀을 첫 apply 때 미리 연다.
  EOT
  type        = list(string)
  default     = []
}

variable "secret_env" {
  description = <<-EOT
    Secret Manager 참조 env. 키는 env 이름, secret 은 같은 프로젝트의 비밀 ID
    (target_base 출력 secret_ids 의 값), version 은 버전 번호(이관이 쓴
    버전 이름 …/versions/N 의 N). latest 는 받지 않는다 — 이관을 되돌리면
    파기된 버전이 가장 최근에 만든 버전으로 남아, latest 를 읽는 소비자가
    FAILED_PRECONDITION 으로 깨진다. 실행 서비스 계정에 이 비밀들의 읽기
    권한을 준다.
  EOT
  type = map(object({
    secret  = string
    version = string
  }))
  default = {}

  validation {
    condition = alltrue([
      for ref in values(var.secret_env) : can(regex("^[1-9][0-9]*$", ref.version))
    ])
    error_message = "secret_env 의 version 은 버전 번호(1 이상 정수)여야 한다. latest 는 되돌리기 뒤 파기된 버전을 가리킨다."
  }
}
