terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Configure a remote backend before running this in a team setting.
  # backend "s3" {
  #   bucket         = "your-tfstate-bucket"
  #   key            = "agentic-rag/terraform.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "terraform-locks"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# CloudFront requires its ACM certificate in us-east-1 regardless of app region.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
}
