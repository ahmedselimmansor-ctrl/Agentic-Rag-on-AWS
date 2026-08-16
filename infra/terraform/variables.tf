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
  type      = string
  sensitive = true
  default   = ""
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
