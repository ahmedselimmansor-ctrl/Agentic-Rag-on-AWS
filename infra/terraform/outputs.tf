output "app_url" {
  description = "Public entry point."
  value       = var.domain_name != "" ? "https://${var.domain_name}" : "https://${aws_cloudfront_distribution.main.domain_name}"
}

output "cloudfront_distribution_id" {
  description = "Needed to invalidate the cache after a frontend deploy."
  value       = aws_cloudfront_distribution.main.id
}

output "frontend_bucket" {
  description = "Sync the Vite `dist/` directory here."
  value       = aws_s3_bucket.frontend.bucket
}

output "uploads_bucket" {
  value = aws_s3_bucket.uploads.bucket
}

output "ecr_repository_url" {
  value = aws_ecr_repository.backend.repository_url
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "ecs_service_name" {
  value = aws_ecs_service.backend.name
}

output "migrate_task_definition" {
  description = "Run this task before rolling out a schema change."
  value       = aws_ecs_task_definition.migrate.family
}

output "private_subnet_ids" {
  description = "Needed when invoking the migrate task with `aws ecs run-task`."
  value       = aws_subnet.private[*].id
}

output "ecs_security_group_id" {
  value = aws_security_group.ecs.id
}

output "db_endpoint" {
  value = aws_db_instance.main.address
}

output "secret_arn" {
  value = aws_secretsmanager_secret.app.arn
}

output "alb_dns_name" {
  value = aws_lb.main.dns_name
}

output "ingestion_queue_url" {
  value = aws_sqs_queue.ingestion.url
}

output "ingestion_dlq_url" {
  description = "Messages here are documents that permanently failed ingestion."
  value       = aws_sqs_queue.ingestion_dlq.url
}

output "worker_service_name" {
  value = aws_ecs_service.worker.name
}

output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}

output "dashboard_url" {
  value = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards:name=${aws_cloudwatch_dashboard.main.dashboard_name}"
}
