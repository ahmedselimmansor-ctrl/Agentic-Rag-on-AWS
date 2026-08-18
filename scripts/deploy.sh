#!/usr/bin/env bash
# Build, migrate, and roll out to AWS.
#
# Order matters: the schema must be ahead of the code, so migrations run as a
# one-off ECS task and must succeed before the service is updated.
#
#   ./scripts/deploy.sh              # backend + frontend
#   ./scripts/deploy.sh backend
#   ./scripts/deploy.sh frontend
set -euo pipefail

TARGET="${1:-all}"
TF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../infra/terraform" && pwd)"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

tf() { terraform -chdir="$TF_DIR" output -raw "$1"; }

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

command -v terraform >/dev/null || die "terraform is not installed"
command -v aws >/dev/null || die "aws cli is not installed"
command -v docker >/dev/null || die "docker is not installed"

terraform -chdir="$TF_DIR" output -raw ecr_repository_url >/dev/null 2>&1 \
  || die "No Terraform state found. Run: terraform -chdir=infra/terraform apply"

REGION="$(aws configure get region || echo us-east-1)"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
ECR_URL="$(tf ecr_repository_url)"
CLUSTER="$(tf ecs_cluster_name)"
SERVICE="$(tf ecs_service_name)"
TAG="$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)"

deploy_backend() {
  log "Logging in to ECR ($ACCOUNT.dkr.ecr.$REGION.amazonaws.com)"
  aws ecr get-login-password --region "$REGION" \
    | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"

  log "Building backend image :$TAG"
  docker build --platform linux/amd64 -t "${ECR_URL}:${TAG}" -t "${ECR_URL}:latest" "$ROOT/backend"

  log "Pushing image"
  docker push "${ECR_URL}:${TAG}"
  docker push "${ECR_URL}:latest"

  log "Running database migrations"
  local subnets sg task_arn exit_code
  subnets="$(terraform -chdir="$TF_DIR" output -json private_subnet_ids | jq -r 'join(",")')"
  sg="$(tf ecs_security_group_id)"

  task_arn="$(aws ecs run-task \
    --cluster "$CLUSTER" \
    --task-definition "$(tf migrate_task_definition)" \
    --launch-type FARGATE \
    --network-configuration "awsvpcConfiguration={subnets=[${subnets}],securityGroups=[${sg}],assignPublicIp=DISABLED}" \
    --query 'tasks[0].taskArn' --output text)"

  log "Waiting for migration task to finish"
  aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$task_arn"

  exit_code="$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$task_arn" \
    --query 'tasks[0].containers[0].exitCode' --output text)"
  [ "$exit_code" = "0" ] || die "Migration failed (exit $exit_code). Check /ecs/*-backend logs; service not updated."

  log "Rolling out the API service"
  aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" --force-new-deployment >/dev/null

  # The worker runs the same image, so it must roll too or it keeps executing
  # the previous release against the new schema.
  local worker
  worker="$(terraform -chdir="$TF_DIR" output -raw worker_service_name 2>/dev/null || true)"
  if [ -n "$worker" ]; then
    log "Rolling out the ingestion worker"
    aws ecs update-service --cluster "$CLUSTER" --service "$worker" --force-new-deployment >/dev/null
  fi

  aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE" ${worker:+"$worker"}
  log "Backend deployed at :$TAG"
}

deploy_frontend() {
  local bucket dist_id
  bucket="$(tf frontend_bucket)"
  dist_id="$(tf cloudfront_distribution_id)"

  log "Building frontend"
  (cd "$ROOT/frontend" && npm ci --no-audit --no-fund && VITE_API_BASE=/api npm run build)

  log "Syncing to s3://$bucket"
  # Hashed assets first with a long TTL, then index.html with no-cache. Doing it
  # in this order means a client never sees new HTML pointing at absent assets.
  aws s3 sync "$ROOT/frontend/dist" "s3://$bucket" \
    --delete --exclude index.html --cache-control "public,max-age=31536000,immutable"
  aws s3 cp "$ROOT/frontend/dist/index.html" "s3://$bucket/index.html" \
    --cache-control "no-store,must-revalidate" --content-type text/html

  log "Invalidating CloudFront"
  aws cloudfront create-invalidation --distribution-id "$dist_id" --paths "/index.html" "/" >/dev/null
  log "Frontend deployed"
}

case "$TARGET" in
  backend)  deploy_backend ;;
  frontend) deploy_frontend ;;
  all)      deploy_backend; deploy_frontend ;;
  *)        die "Usage: $0 [all|backend|frontend]" ;;
esac

log "Live at $(tf app_url)"
