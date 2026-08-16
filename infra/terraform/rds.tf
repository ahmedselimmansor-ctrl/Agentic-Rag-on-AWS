resource "aws_db_subnet_group" "main" {
  name       = "${local.name}-db"
  subnet_ids = aws_subnet.private[*].id
  tags       = { Name = "${local.name}-db" }
}

# pgvector ships with RDS PostgreSQL 15.2+ but must be enabled per-database;
# the Alembic migration issues CREATE EXTENSION on first run.
resource "aws_db_parameter_group" "main" {
  name        = "${local.name}-pg16"
  family      = "postgres16"
  description = "Agentic RAG tuning"

  parameter {
    name  = "shared_preload_libraries"
    value = "pg_stat_statements"
    # Static parameter: requires an instance reboot to take effect.
    apply_method = "pending-reboot"
  }

  parameter {
    name         = "max_connections"
    value        = "300"
    apply_method = "pending-reboot"
  }

  # Log anything slower than a second — vector scans that miss the HNSW index
  # are the usual culprit and are otherwise invisible.
  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }
}

resource "random_password" "db" {
  length  = 32
  special = true
  # RDS rejects these in a master password.
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

resource "aws_db_instance" "main" {
  identifier     = "${local.name}-pg"
  engine         = "postgres"
  engine_version = "16.4"

  instance_class        = var.db_instance_class
  allocated_storage     = var.db_allocated_storage
  max_allocated_storage = var.db_max_allocated_storage
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = var.db_name
  username = var.db_username
  password = random_password.db.result
  port     = 5432

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  parameter_group_name   = aws_db_parameter_group.main.name
  publicly_accessible    = false

  multi_az                = var.db_multi_az
  backup_retention_period = var.db_backup_retention_days
  backup_window           = "03:00-04:00"
  maintenance_window      = "sun:04:30-sun:05:30"
  copy_tags_to_snapshot   = true

  auto_minor_version_upgrade = true
  deletion_protection        = var.db_deletion_protection
  skip_final_snapshot        = !var.db_deletion_protection
  final_snapshot_identifier  = var.db_deletion_protection ? "${local.name}-final-${formatdate("YYYYMMDDhhmmss", timestamp())}" : null

  performance_insights_enabled    = true
  monitoring_interval             = 60
  monitoring_role_arn             = aws_iam_role.rds_monitoring.arn
  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]

  tags = { Name = "${local.name}-pg" }

  lifecycle {
    ignore_changes = [final_snapshot_identifier]
  }
}

resource "aws_iam_role" "rds_monitoring" {
  name = "${local.name}-rds-monitoring"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "monitoring.rds.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "rds_monitoring" {
  role       = aws_iam_role.rds_monitoring.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}
