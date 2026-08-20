variable "project" {
  description = "Name prefix for every resource."
  type        = string
  default     = "agentic-rag"
}

variable "environment" {
  description = "Deployment environment."
  type        = string
  default     = "prod"
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

# ------------------------------------------------------------- network -----
variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "az_count" {
  description = "Availability zones to span. RDS Multi-AZ needs at least 2."
  type        = number
  default     = 2
}

variable "single_nat_gateway" {
  description = "One NAT gateway instead of one per AZ. Cheaper; not HA."
  type        = bool
  default     = true
}

# ----------------------------------------------------------- database ------
variable "db_instance_class" {
  type    = string
  default = "db.t4g.medium"
}

variable "db_allocated_storage" {
  type    = number
  default = 50
}

variable "db_max_allocated_storage" {
  description = "Ceiling for storage autoscaling."
  type        = number
  default     = 500
}

variable "db_name" {
  type    = string
  default = "agentic_rag"
}

variable "db_username" {
  type    = string
  default = "ragadmin"
}

variable "db_multi_az" {
  type    = bool
  default = false
}

variable "db_backup_retention_days" {
  type    = number
  default = 7
}

variable "db_deletion_protection" {
  type    = bool
  default = true
}

# ---------------------------------------------------------------- ecs ------
variable "backend_cpu" {
  description = "Fargate CPU units (1024 = 1 vCPU)."
  type        = number
  default     = 1024
}

variable "backend_memory" {
  type    = number
  default = 2048
}

variable "backend_desired_count" {
  type    = number
  default = 2
}

variable "backend_min_capacity" {
  type    = number
  default = 2
}

variable "backend_max_capacity" {
  type    = number
  default = 10
}

variable "backend_image_tag" {
  type    = string
  default = "latest"
}

# ------------------------------------------------------------ secrets ------
# Values are written to Secrets Manager. Pass them via a tfvars file that is
# NOT committed, or set them out-of-band and import.
variable "openai_api_key" {
  type      = string
  sensitive = true
}

variable "dashscope_api_key" {
  type      = string
  sensitive = true
}

variable "tavily_api_key" {
  description = "Only needed when web_search_provider is tavily."
  type        = string
  sensitive   = true
  default     = ""
}

variable "web_search_provider" {
  description = "openai (model's built-in search) | tavily | serper | none."
  type        = string
  default     = "openai"

  validation {
    condition     = contains(["openai", "tavily", "serper", "none"], var.web_search_provider)
    error_message = "web_search_provider must be openai, tavily, serper, or none."
  }
}

variable "openai_web_search_tool" {
  description = "Hosted tool type. Providers rename this between releases."
  type        = string
  default     = "web_search"
}

# -------------------------------------------------------------- models -----
variable "generation_model" {
  type    = string
  default = "gpt-5.6-luna"
}

variable "embedding_model" {
  type    = string
  default = "tongyi-embedding-vision-flash"
}

variable "embedding_dim" {
  description = "Must match the embedding model's output. Changing it requires re-embedding everything."
  type        = number
  default     = 1024
}

variable "rerank_model" {
  type    = string
  default = "qwen3-rerank"
}

variable "dashscope_base_url" {
  type    = string
  default = "https://dashscope-intl.aliyuncs.com"
}

# --------------------------------------------------------------- dns -------
variable "domain_name" {
  description = "Optional custom domain for the frontend. Leave empty to use the CloudFront domain."
  type        = string
  default     = ""
}

variable "acm_certificate_arn" {
  description = "us-east-1 ACM certificate ARN. Required when domain_name is set."
  type        = string
  default     = ""
}

variable "log_retention_days" {
  type    = number
  default = 30
}

# ---------------------------------------------------------------- auth -----
variable "allow_registration" {
  description = "Set false to freeze sign-ups once your users have accounts."
  type        = bool
  default     = true
}

variable "max_messages_per_hour" {
  type    = number
  default = 120
}

variable "max_uploads_per_hour" {
  type    = number
  default = 60
}

# --------------------------------------------------------------- worker ----
variable "worker_cpu" {
  type    = number
  default = 1024
}

variable "worker_memory" {
  type    = number
  default = 2048
}

variable "worker_desired_count" {
  type    = number
  default = 1
}

variable "worker_min_capacity" {
  description = "Zero is allowed, but the first upload after idle then waits for a cold start."
  type        = number
  default     = 1
}

variable "worker_max_capacity" {
  type    = number
  default = 6
}

variable "ingestion_visibility_timeout" {
  description = "Must exceed the slowest realistic document, or SQS redelivers a job still in flight."
  type        = number
  default     = 900
}

# ---------------------------------------------------------------- email ----
variable "email_backend" {
  description = "ses | log | none. SES requires a verified identity in this region."
  type        = string
  default     = "log"
}

variable "email_from" {
  description = "Must be an SES-verified address or domain when email_backend = ses."
  type        = string
  default     = ""
}

variable "require_email_verification" {
  type    = bool
  default = false
}

# ------------------------------------------------------------ monitoring ---
variable "alert_email" {
  description = "Subscribed to the alerts SNS topic. AWS sends a confirmation you must click."
  type        = string
  default     = ""
}
