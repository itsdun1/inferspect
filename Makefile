.DEFAULT_GOAL := help

COMPOSE := docker compose -f infra/docker-compose.yml --env-file .env

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

.PHONY: env
env: ## Copy .env.example to .env if missing
	@[ -f .env ] || cp .env.example .env && echo ".env ready"

.PHONY: up
up: env ## Bring up infra (postgres, valkey, clickhouse, grafana)
	$(COMPOSE) up -d
	@echo ""
	@echo "Postgres   : localhost:5432  (ollive/ollivepass)"
	@echo "Valkey     : localhost:6379"
	@echo "ClickHouse : localhost:8123  (ollive/ollivepass)"
	@echo "Grafana    : localhost:3001  (admin/admin)"

.PHONY: down
down: ## Stop infra
	$(COMPOSE) down

.PHONY: nuke
nuke: ## Stop infra and delete all volumes (destructive)
	$(COMPOSE) down -v

.PHONY: logs
logs: ## Tail compose logs
	$(COMPOSE) logs -f

.PHONY: ps
ps: ## List running containers
	$(COMPOSE) ps

.PHONY: install
install: ## Install Python deps via uv (workspace)
	uv sync

.PHONY: web-install
web-install: ## Install Next.js workspace deps (web-chat + web-insights + web-shared)
	npm install

.PHONY: web-chat-dev
web-chat-dev: ## Run web-chat dev server (Next.js, default port 3000)
	npm run dev:chat

.PHONY: web-insights-dev
web-insights-dev: ## Run web-insights dev server (Next.js, default port 3000)
	npm run dev:insights

.PHONY: test
test: ## Run Python test suite
	uv run pytest -q

.PHONY: lint
lint: ## Lint Python
	uv run ruff check .

.PHONY: fmt
fmt: ## Format Python
	uv run ruff format .

.PHONY: psql
psql: ## psql shell into the running postgres
	$(COMPOSE) exec postgres psql -U ollive ollive

.PHONY: ch
ch: ## clickhouse-client shell
	$(COMPOSE) exec clickhouse clickhouse-client -u ollive --password ollivepass

.PHONY: valkey-cli
valkey-cli: ## valkey-cli shell
	$(COMPOSE) exec valkey valkey-cli

# ─── Phase G — eBPF agent ─────────────────────────────────────────────
COMPOSE_PROD := docker compose -f infra/docker-compose.prod.yml --env-file .env

.PHONY: agent-build
agent-build: ## Build the inferspect-agent Docker image
	$(COMPOSE_PROD) --profile agent build inferspect-agent

.PHONY: agent-up
agent-up: ## Start the agent alongside the prod stack (Linux / Docker Desktop)
	$(COMPOSE_PROD) --profile agent up -d inferspect-agent
	@echo "Agent logs: docker logs -f inferspect-agent"

.PHONY: agent-down
agent-down: ## Stop the agent (leaves the rest of the stack running)
	$(COMPOSE_PROD) --profile agent stop inferspect-agent
	$(COMPOSE_PROD) --profile agent rm -f inferspect-agent

.PHONY: agent-logs
agent-logs: ## Tail the agent logs
	docker logs -f inferspect-agent

.PHONY: agent-demo
agent-demo: ## End-to-end: prod stack + agent + a smoke chat (manual kill from /agents UI)
	$(COMPOSE_PROD) up -d
	$(COMPOSE_PROD) --profile agent up -d inferspect-agent
	@echo ""
	@echo "Stack + agent are running. Open https://chat.$$DOMAIN and start a conversation."
	@echo "Then https://insights.$$DOMAIN/agents to see the host + issue a kill."
