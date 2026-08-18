.DEFAULT_GOAL := help
SHELL := /bin/bash

BACKEND := backend
FRONTEND := frontend
COMPOSE := docker compose

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ------------------------------------------------------------------ local ---
.PHONY: env
env: ## Create .env from the example
	@test -f .env || (cp .env.example .env && echo "Created .env — add your API keys")

.PHONY: up
up: env ## Start the whole stack (db + api + web) on :8080
	$(COMPOSE) up --build -d
	@echo "Web  http://localhost:8080"
	@echo "API  http://localhost:8000/docs"

.PHONY: down
down: ## Stop the stack
	$(COMPOSE) down

.PHONY: clean
clean: ## Stop the stack and delete all data
	$(COMPOSE) down -v

.PHONY: logs
logs: ## Tail backend logs
	$(COMPOSE) logs -f backend

.PHONY: db
db: ## Start only Postgres (for running the API on the host)
	$(COMPOSE) up -d db

.PHONY: psql
psql: ## Open a psql shell
	$(COMPOSE) exec db psql -U postgres -d agentic_rag

# ---------------------------------------------------------------- backend ---
.PHONY: migrate
migrate: ## Apply migrations
	$(COMPOSE) run --rm migrate

.PHONY: revision
revision: ## Autogenerate a migration: make revision m="add x"
	@test -n "$(m)" || (echo 'Usage: make revision m="describe the change"' && exit 1)
	cd $(BACKEND) && alembic revision --autogenerate -m "$(m)" && ruff format alembic/versions

.PHONY: api
api: ## Run the API on the host with reload
	cd $(BACKEND) && uvicorn app.main:app --reload --port 8000

.PHONY: test
test: ## Run backend tests
	cd $(BACKEND) && pytest -q

.PHONY: lint
lint: ## Lint + format-check the backend
	cd $(BACKEND) && ruff check app tests && ruff format --check app tests

.PHONY: fmt
fmt: ## Auto-format the backend
	cd $(BACKEND) && ruff check --fix app tests && ruff format app tests

# --------------------------------------------------------------- frontend ---
.PHONY: web
web: ## Run the Vite dev server on :5173
	cd $(FRONTEND) && npm run dev

.PHONY: web-install
web-install: ## Install frontend dependencies
	cd $(FRONTEND) && npm install

.PHONY: web-build
web-build: ## Typecheck and build the frontend
	cd $(FRONTEND) && npm run build

# ------------------------------------------------------------------- aws ----
.PHONY: tf-init
tf-init: ## terraform init
	terraform -chdir=infra/terraform init

.PHONY: tf-plan
tf-plan: ## terraform plan
	terraform -chdir=infra/terraform plan

.PHONY: tf-apply
tf-apply: ## terraform apply
	terraform -chdir=infra/terraform apply

.PHONY: deploy
deploy: ## Build, migrate and roll out to AWS
	./scripts/deploy.sh all
