locals {
  database_url = format(
    "postgresql+asyncpg://%s:%s@%s:%s/%s",
    var.db_username,
    urlencode(random_password.db.result),
    aws_db_instance.main.address,
    aws_db_instance.main.port,
    var.db_name,
  )
}

# Generated rather than supplied: one less secret for a human to mishandle,
# and rotating it is a `terraform taint` away.
resource "random_password" "jwt_secret" {
  length  = 64
  special = false
}

resource "aws_secretsmanager_secret" "app" {
  name                    = "${local.name}/app"
  description             = "Model provider keys and the database DSN"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "app" {
  secret_id = aws_secretsmanager_secret.app.id

  secret_string = jsonencode({
    DATABASE_URL      = local.database_url
    OPENAI_API_KEY    = var.openai_api_key
    DASHSCOPE_API_KEY = var.dashscope_api_key
    TAVILY_API_KEY    = var.tavily_api_key
    JWT_SECRET        = random_password.jwt_secret.result
  })
}
