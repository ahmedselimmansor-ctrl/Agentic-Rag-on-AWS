resource "aws_lb" "main" {
  name               = substr("${local.name}-alb", 0, 32)
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  # Must exceed the longest expected streamed answer, or the ALB severs the SSE
  # connection mid-response.
  idle_timeout               = 600
  drop_invalid_header_fields = true
  enable_http2               = true
  enable_deletion_protection = var.environment == "prod"

  tags = { Name = "${local.name}-alb" }
}

resource "aws_lb_target_group" "backend" {
  name        = substr("${local.name}-tg", 0, 32)
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.main.id

  health_check {
    enabled             = true
    path                = "/api/health/live"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  # Give in-flight streams time to finish before the target is removed.
  deregistration_delay = 60

  stickiness {
    enabled = false
    type    = "lb_cookie"
  }

  lifecycle { create_before_destroy = true }
}

# A shared secret CloudFront injects, so the ALB cannot be reached directly even
# though it holds a public IP.
resource "random_password" "origin_verify" {
  length  = 40
  special = false
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "fixed-response"

    fixed_response {
      content_type = "text/plain"
      message_body = "Direct access is not permitted."
      status_code  = "403"
    }
  }
}

resource "aws_lb_listener_rule" "http_from_cloudfront" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 10

  condition {
    http_header {
      http_header_name = "X-Origin-Verify"
      values           = [random_password.origin_verify.result]
    }
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.backend.arn
  }
}

# Optional direct HTTPS entry point — only when a certificate is supplied.
resource "aws_lb_listener" "https" {
  count = var.acm_certificate_arn != "" ? 1 : 0

  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.acm_certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.backend.arn
  }
}
