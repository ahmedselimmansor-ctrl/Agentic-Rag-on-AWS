# Ingestion work queue. Embedding a large document is minutes of provider
# latency; running it in the API process makes it compete with streaming turns.

resource "aws_sqs_queue" "ingestion_dlq" {
  name                      = "${local.name}-ingestion-dlq"
  message_retention_seconds = 1209600 # 14 days — long enough to investigate
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "ingestion" {
  name = "${local.name}-ingestion"

  # Must exceed the slowest realistic document, or SQS redelivers a job that is
  # still being processed. The worker also extends this via a heartbeat.
  visibility_timeout_seconds = var.ingestion_visibility_timeout
  message_retention_seconds  = 345600 # 4 days
  receive_wait_time_seconds  = 20     # long polling
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.ingestion_dlq.arn
    # 3 attempts: enough to ride out a transient provider error, few enough
    # that a genuinely poisonous message stops burning worker time.
    maxReceiveCount = 3
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  queue_url = aws_sqs_queue.ingestion_dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.ingestion.arn]
  })
}

# The API only sends; the worker receives and deletes. Splitting the two means a
# compromised API task cannot drain the queue.
resource "aws_iam_role_policy" "task_sqs_send" {
  name = "ingestion-queue-send"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["sqs:SendMessage", "sqs:GetQueueAttributes"]
      Resource = aws_sqs_queue.ingestion.arn
    }]
  })
}

resource "aws_iam_role" "worker_task" {
  name = "${local.name}-worker-task"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "worker_permissions" {
  name = "worker"
  role = aws_iam_role.worker_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:ChangeMessageVisibility",
          "sqs:GetQueueAttributes",
        ]
        Resource = aws_sqs_queue.ingestion.arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.uploads.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.uploads.arn
      },
      {
        # OCR fallback for PDFs with no text layer.
        Effect   = "Allow"
        Action   = ["textract:StartDocumentTextDetection", "textract:GetDocumentTextDetection"]
        Resource = "*"
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/ecs/${local.name}-worker"
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name}-worker"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.worker_task.arn

  container_definitions = jsonencode([
    {
      name      = "worker"
      image     = local.backend_image
      essential = true
      command   = ["python", "-m", "app.worker"]

      environment = concat(local.backend_environment, [
        { name = "INGESTION_MODE", value = "sqs" },
        { name = "INGESTION_QUEUE_URL", value = aws_sqs_queue.ingestion.url },
        { name = "INGESTION_VISIBILITY_TIMEOUT", value = tostring(var.ingestion_visibility_timeout) },
      ])
      secrets = local.backend_secrets

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.worker.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "worker"
        }
      }

      # Long enough for the in-flight document to finish after SIGTERM, so the
      # job is not redelivered and re-embedded.
      stopTimeout = 120
    }
  ])
}

resource "aws_ecs_service" "worker" {
  name            = "${local.name}-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = false
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  enable_execute_command = true

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }
}

# Scale on queue depth per worker. CPU is a poor signal here: a worker blocked
# on the embedding provider is idle but very much busy.
resource "aws_appautoscaling_target" "worker" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.worker.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.worker_min_capacity
  max_capacity       = var.worker_max_capacity
}

resource "aws_appautoscaling_policy" "worker_queue_depth" {
  name               = "${local.name}-worker-backlog"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.worker.service_namespace
  resource_id        = aws_appautoscaling_target.worker.resource_id
  scalable_dimension = aws_appautoscaling_target.worker.scalable_dimension

  target_tracking_scaling_policy_configuration {
    target_value = 5 # messages visible per running worker
    # Scale in slowly: a worker killed mid-document loses up to a full
    # visibility timeout before the job is retried.
    scale_in_cooldown  = 600
    scale_out_cooldown = 60

    customized_metric_specification {
      metrics {
        id    = "backlog"
        label = "Visible messages per worker"

        return_data = true
        expression  = "IF(workers > 0, visible / workers, visible)"
      }

      metrics {
        id          = "visible"
        return_data = false

        metric_stat {
          stat = "Average"
          metric {
            namespace   = "AWS/SQS"
            metric_name = "ApproximateNumberOfMessagesVisible"
            dimensions {
              name  = "QueueName"
              value = aws_sqs_queue.ingestion.name
            }
          }
        }
      }

      metrics {
        id          = "workers"
        return_data = false

        metric_stat {
          stat = "Average"
          metric {
            namespace   = "ECS/ContainerInsights"
            metric_name = "RunningTaskCount"
            dimensions {
              name  = "ClusterName"
              value = aws_ecs_cluster.main.name
            }
            dimensions {
              name  = "ServiceName"
              value = aws_ecs_service.worker.name
            }
          }
        }
      }
    }
  }
}
