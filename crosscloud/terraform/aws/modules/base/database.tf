# 시험용 RDS PostgreSQL. 사용자 결정(2026-11-07): 공개 엔드포인트 +
# sslmode=require + IP 허용 목록 없음 + 강한 무작위 비밀번호, 시험 뒤 바로 해체.
# 그래서 백업·삭제 보호·다중 AZ 는 끈다.

resource "aws_db_parameter_group" "this" {
  name   = "${var.name_prefix}-pg"
  family = "postgres${split(".", var.db_engine_version)[0]}"

  # CDC(논리 복제)에 필요하다. 재시작 플래그라 생성 때 붙여 둔다.
  parameter {
    name         = "rds.logical_replication"
    value        = "1"
    apply_method = "pending-reboot"
  }
}

resource "aws_db_subnet_group" "this" {
  name       = "${var.name_prefix}-db"
  subnet_ids = aws_subnet.public[*].id
}

resource "aws_security_group" "db" {
  name        = "${var.name_prefix}-db"
  description = "PostgreSQL from anywhere (test only, TLS required)"
  vpc_id      = aws_vpc.this.id
}

resource "aws_vpc_security_group_ingress_rule" "db_postgres" {
  security_group_id = aws_security_group.db.id
  description       = "PostgreSQL"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 5432
  to_port           = 5432
}

resource "aws_db_instance" "this" {
  identifier     = "${var.name_prefix}-db"
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = var.db_name
  username = var.db_username
  # 비밀번호는 RDS 가 만들어 Secrets Manager 에 둔다 — state 에 평문이 남지 않는다.
  manage_master_user_password = true

  parameter_group_name   = aws_db_parameter_group.this.name
  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = true
  multi_az               = false

  backup_retention_period  = 0
  delete_automated_backups = true
  deletion_protection      = false
  skip_final_snapshot      = true
  apply_immediately        = true
}
