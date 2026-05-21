#!/usr/bin/env bash
set -e

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
DOCKER_DIR="$PROJECT_ROOT/docker"

# Docker Compose command with project name
COMPOSE_CMD="docker compose --env-file ../.env -p deer-flow-dev -f docker-compose-dev.yaml"

detect_sandbox_mode() {
    local config_file="$PROJECT_ROOT/config.yaml"
    local sandbox_use=""
    local provisioner_url=""

    if [ ! -f "$config_file" ]; then
        echo "local"
        return
    fi

    sandbox_use=$(awk '
        /^[[:space:]]*sandbox:[[:space:]]*$/ { in_sandbox=1; next }
        in_sandbox && /^[^[:space:]#]/ { in_sandbox=0 }
        in_sandbox && /^[[:space:]]*use:[[:space:]]*/ {
            line=$0
            sub(/^[[:space:]]*use:[[:space:]]*/, "", line)
            print line
            exit
        }
    ' "$config_file")

    provisioner_url=$(awk '
        /^[[:space:]]*sandbox:[[:space:]]*$/ { in_sandbox=1; next }
        in_sandbox && /^[^[:space:]#]/ { in_sandbox=0 }
        in_sandbox && /^[[:space:]]*provisioner_url:[[:space:]]*/ {
            line=$0
            sub(/^[[:space:]]*provisioner_url:[[:space:]]*/, "", line)
            print line
            exit
        }
    ' "$config_file")

    if [[ "$sandbox_use" == *"deerflow.sandbox.local:LocalSandboxProvider"* ]]; then
        echo "local"
    elif [[ "$sandbox_use" == *"deerflow.community.aio_sandbox:AioSandboxProvider"* ]]; then
        if [ -n "$provisioner_url" ]; then
            echo "provisioner"
        else
            echo "aio"
        fi
    else
        echo "local"
    fi
}

# Cleanup function for Ctrl+C
cleanup() {
    echo ""
    echo -e "${YELLOW}Operation interrupted by user${NC}"
    exit 130
}

# Set up trap for Ctrl+C
trap cleanup INT TERM

docker_available() {
    # Check that the docker CLI exists
    if ! command -v docker >/dev/null 2>&1; then
        return 1
    fi

    # Check that the Docker daemon is reachable
    if ! docker info >/dev/null 2>&1; then
        return 1
    fi

    return 0
}

env_file_value() {
    local key="$1"

    if [ ! -f "$PROJECT_ROOT/.env" ]; then
        return 0
    fi

    awk -F= -v key="$key" '
        $1 == key {
            sub(/\r$/, "", $2)
            print $2
            exit
        }
    ' "$PROJECT_ROOT/.env"
}

is_truthy() {
    case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

identity_enabled() {
    local raw="${ENABLE_IDENTITY:-$(env_file_value ENABLE_IDENTITY)}"
    is_truthy "$raw"
}

append_env_default() {
    local key="$1"
    local value="$2"

    if grep -Eq "^${key}=" "$PROJECT_ROOT/.env" 2>/dev/null; then
        return 0
    fi

    printf "%s=%s\n" "$key" "$value" >> "$PROJECT_ROOT/.env"
    echo -e "${BLUE}Added ${key} to .env${NC}"
}

ensure_identity_env_defaults() {
    append_env_default "ENABLE_IDENTITY" "true"
    append_env_default "DEERFLOW_DATABASE_URL" "postgresql+asyncpg://deerflow:deerflow@postgres:5432/deerflow"
    append_env_default "DEERFLOW_REDIS_URL" "redis://redis:6379/0"
    append_env_default "DEERFLOW_BOOTSTRAP_ADMIN_EMAIL" "admin@local.deerflow"
    append_env_default "DEERFLOW_BOOTSTRAP_ADMIN_PASSWORD" "DeerFlow123!"
    append_env_default "DEERFLOW_BOOTSTRAP_PASSWORD_TOKEN" "deerflow-bootstrap-local"
    append_env_default "DEERFLOW_COOKIE_SECURE" "false"
    append_env_default "DEERFLOW_INTERNAL_SIGNING_KEY" "deerflow-local-hmac-key"
    append_env_default "POSTGRES_DB" "deerflow"
    append_env_default "POSTGRES_USER" "deerflow"
    append_env_default "POSTGRES_PASSWORD" "deerflow"
    append_env_default "POSTGRES_PORT" "5432"
    append_env_default "REDIS_PORT" "6379"
}

ensure_identity_config_file() {
    mkdir -p "$PROJECT_ROOT/config"

    if [ ! -f "$PROJECT_ROOT/config/identity.yaml" ]; then
        cat > "$PROJECT_ROOT/config/identity.yaml" <<'EOF'
oidc:
  providers: {}
EOF
        echo -e "${BLUE}Created config/identity.yaml with empty OIDC providers${NC}"
    fi
}

wait_for_service_ready() {
    local service="$1"
    local status=""
    local container_id=""
    local attempt=0
    local max_attempts=60

    echo -e "${BLUE}Waiting for ${service} to become ready...${NC}"
    while [ "$attempt" -lt "$max_attempts" ]; do
        container_id=$(cd "$DOCKER_DIR" && $COMPOSE_CMD ps -q "$service")
        if [ -n "$container_id" ]; then
            status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id" 2>/dev/null || true)
            if [ "$status" = "healthy" ] || [ "$status" = "running" ]; then
                echo -e "${GREEN}✓ ${service} is ${status}${NC}"
                return 0
            fi
        fi
        sleep 2
        attempt=$((attempt + 1))
    done

    echo -e "${YELLOW}✗ Timed out waiting for ${service}${NC}"
    return 1
}

run_identity_setup() {
    echo -e "${BLUE}Preparing identity database schema and JWT keys...${NC}"
    cd "$DOCKER_DIR" && $COMPOSE_CMD run --rm --no-deps gateway sh -lc "
        cd backend &&
        (uv sync || (echo '[identity] uv sync failed; recreating .venv and retrying once' && uv venv --allow-existing .venv && uv sync)) &&
        make db-upgrade &&
        make identity-bootstrap &&
        make identity-keys &&
        PYTHONPATH=. uv run python - <<'PY'
import os
from sqlalchemy import select

from app.gateway.identity.auth.passwords import hash_password
from app.gateway.identity.db import create_engine_and_sessionmaker
from app.gateway.identity.models.user import User
from app.gateway.identity.settings import get_identity_settings

settings = get_identity_settings()
password = os.environ.get('DEERFLOW_BOOTSTRAP_ADMIN_PASSWORD', '').strip()
email = (settings.bootstrap_admin_email or '').strip().lower()

if not password or not email:
    print('identity bootstrap password skipped: missing admin email or password')
    raise SystemExit(0)

async def main():
    engine, maker = create_engine_and_sessionmaker(settings.database_url)
    try:
        async with maker() as session:
            user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
            if user is None:
                print(f'identity bootstrap password skipped: user {email!r} not found')
                return
            if user.password_hash:
                print(f'identity bootstrap password already initialized for {email}')
                return
            user.password_hash = hash_password(password)
            await session.commit()
            print(f'initialized bootstrap password for {email}')
    finally:
        await engine.dispose()

import asyncio
asyncio.run(main())
PY
    "
}

# Initialize: pre-pull the sandbox image so first Pod startup is fast
init() {
    echo "=========================================="
    echo "  DeerFlow Init — Pull Sandbox Image"
    echo "=========================================="
    echo ""

    SANDBOX_IMAGE="enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest"

    # Detect sandbox mode from config.yaml
    local sandbox_mode
    sandbox_mode="$(detect_sandbox_mode)"

    # Skip image pull for local sandbox mode (no container image needed)
    if [ "$sandbox_mode" = "local" ]; then
        echo -e "${GREEN}Detected local sandbox mode — no Docker image required.${NC}"
        echo ""

        if docker_available; then
            echo -e "${GREEN}✓ Docker environment is ready.${NC}"
            echo ""
            echo -e "${YELLOW}Next step: make docker-start${NC}"
        else
            echo -e "${YELLOW}Docker does not appear to be installed, or the Docker daemon is not reachable.${NC}"
            echo "Local sandbox mode itself does not require Docker, but Docker-based workflows (e.g., docker-start) will fail until Docker is available."
            echo ""
            echo -e "${YELLOW}Install and start Docker, then run: make docker-init && make docker-start${NC}"
        fi

        return 0
    fi

    if ! docker images --format '{{.Repository}}:{{.Tag}}' | grep -q "^${SANDBOX_IMAGE}$"; then
        echo -e "${BLUE}Pulling sandbox image: $SANDBOX_IMAGE ...${NC}"
        echo ""

        if ! docker pull "$SANDBOX_IMAGE" 2>&1; then
            echo ""
            echo -e "${YELLOW}⚠ Failed to pull sandbox image.${NC}"
            echo ""
            echo "This is expected if:"
            echo "  1. You are using local sandbox mode (default — no image needed)"
            echo "  2. You are behind a corporate proxy or firewall"
            echo "  3. The registry requires authentication"
            echo ""
            echo -e "${GREEN}The Docker development environment can still be started.${NC}"
            echo "If you need AIO sandbox (container-based execution):"
            echo "  - Ensure you have network access to the registry"
            echo "  - Or configure a custom sandbox image in config.yaml"
            echo ""
            echo -e "${YELLOW}Next step: make docker-start${NC}"
            return 0
        fi
    else
        echo -e "${GREEN}Sandbox image already exists locally: $SANDBOX_IMAGE${NC}"
    fi

    echo ""
    echo -e "${GREEN}✓ Sandbox image is ready.${NC}"
    echo ""
    echo -e "${YELLOW}Next step: make docker-start${NC}"
}

# Start Docker development environment
# Usage: start [--gateway]
start() {
    local sandbox_mode
    local services
    local gateway_mode=false
    local identity_mode=false

    # Check for --gateway flag
    for arg in "$@"; do
        if [ "$arg" = "--gateway" ]; then
            gateway_mode=true
        fi
    done

    echo "=========================================="
    echo "  Starting DeerFlow Docker Development"
    echo "=========================================="
    echo ""

    sandbox_mode="$(detect_sandbox_mode)"

    if $gateway_mode; then
        services="frontend gateway nginx"
        if [ "$sandbox_mode" = "provisioner" ]; then
            services="frontend gateway provisioner nginx"
        fi
    else
        services="frontend gateway langgraph nginx"
        if [ "$sandbox_mode" = "provisioner" ]; then
            services="frontend gateway langgraph provisioner nginx"
        fi
    fi

    if $gateway_mode; then
        echo -e "${BLUE}Runtime: Gateway mode (experimental) — no LangGraph container${NC}"
    fi
    echo -e "${BLUE}Detected sandbox mode: $sandbox_mode${NC}"
    if [ "$sandbox_mode" = "provisioner" ]; then
        echo -e "${BLUE}Provisioner enabled (Kubernetes mode).${NC}"
    else
        echo -e "${BLUE}Provisioner disabled (not required for this sandbox mode).${NC}"
    fi
    echo ""
    
    # Set DEER_FLOW_ROOT for provisioner if not already set
    if [ -z "$DEER_FLOW_ROOT" ]; then
        export DEER_FLOW_ROOT="$PROJECT_ROOT"
        echo -e "${BLUE}Setting DEER_FLOW_ROOT=$DEER_FLOW_ROOT${NC}"
        echo ""
    fi
    
    # Ensure config.yaml exists before starting.
    if [ ! -f "$PROJECT_ROOT/config.yaml" ]; then
        if [ -f "$PROJECT_ROOT/config.example.yaml" ]; then
            cp "$PROJECT_ROOT/config.example.yaml" "$PROJECT_ROOT/config.yaml"
            echo ""
            echo -e "${YELLOW}============================================================${NC}"
            echo -e "${YELLOW}  config.yaml has been created from config.example.yaml.${NC}"
            echo -e "${YELLOW}  Please edit config.yaml to set your API keys and model   ${NC}"
            echo -e "${YELLOW}  configuration before starting DeerFlow.                  ${NC}"
            echo -e "${YELLOW}============================================================${NC}"
            echo ""
            echo -e "${YELLOW}  Recommended: run 'make setup' before starting Docker.    ${NC}"
            echo -e "${YELLOW}  Edit the file:  $PROJECT_ROOT/config.yaml${NC}"
            echo -e "${YELLOW}  Then run:        make docker-start${NC}"
            echo ""
            exit 0
        else
            echo -e "${YELLOW}✗ config.yaml not found and no config.example.yaml to copy from.${NC}"
            exit 1
        fi
    fi

    # Ensure extensions_config.json exists as a file before mounting.
    # Docker creates a directory when bind-mounting a non-existent host path.
    if [ ! -f "$PROJECT_ROOT/extensions_config.json" ]; then
        if [ -f "$PROJECT_ROOT/extensions_config.example.json" ]; then
            cp "$PROJECT_ROOT/extensions_config.example.json" "$PROJECT_ROOT/extensions_config.json"
            echo -e "${BLUE}Created extensions_config.json from example${NC}"
        else
            echo "{}" > "$PROJECT_ROOT/extensions_config.json"
            echo -e "${BLUE}Created empty extensions_config.json${NC}"
        fi
    fi

    if [ ! -f "$PROJECT_ROOT/.env" ]; then
        if [ -f "$PROJECT_ROOT/.env.example" ]; then
            cp "$PROJECT_ROOT/.env.example" "$PROJECT_ROOT/.env"
            echo -e "${BLUE}Created .env from .env.example${NC}"
        else
            : > "$PROJECT_ROOT/.env"
            echo -e "${BLUE}Created empty .env${NC}"
        fi
    fi

    if [ ! -f "$PROJECT_ROOT/frontend/.env" ]; then
        if [ -f "$PROJECT_ROOT/frontend/.env.example" ]; then
            cp "$PROJECT_ROOT/frontend/.env.example" "$PROJECT_ROOT/frontend/.env"
            echo -e "${BLUE}Created frontend/.env from frontend/.env.example${NC}"
        else
            mkdir -p "$PROJECT_ROOT/frontend"
            : > "$PROJECT_ROOT/frontend/.env"
            echo -e "${BLUE}Created empty frontend/.env${NC}"
        fi
    fi

    if identity_enabled; then
        identity_mode=true
        ensure_identity_env_defaults
        ensure_identity_config_file
    fi

    # Set nginx routing for gateway mode (envsubst in nginx container)
    if $gateway_mode; then
        export LANGGRAPH_UPSTREAM=gateway:8001
        export LANGGRAPH_REWRITE=/api/
    fi

    if $identity_mode; then
        echo -e "${BLUE}Identity mode enabled — starting Postgres and Redis first${NC}"
        cd "$DOCKER_DIR" && $COMPOSE_CMD up -d --remove-orphans postgres redis
        wait_for_service_ready postgres
        wait_for_service_ready redis
        run_identity_setup
    fi

    echo "Building and starting containers..."
    cd "$DOCKER_DIR" && $COMPOSE_CMD up --build -d --remove-orphans $services
    echo ""
    echo "=========================================="
    echo "  DeerFlow Docker is starting!"
    echo "=========================================="
    echo ""
    echo "  🌐 Application: http://localhost:2026"
    echo "  📡 API Gateway: http://localhost:2026/api/*"
    if $gateway_mode; then
        echo "  🤖 Runtime:     Gateway embedded"
        echo "  API:            /api/langgraph/* → Gateway (compat)"
    else
        echo "  🤖 LangGraph:   http://localhost:2026/api/langgraph/*"
    fi
    echo ""
    if $identity_mode; then
        echo "  🔐 Identity:    enabled"
        echo "  👤 Admin email: $(env_file_value DEERFLOW_BOOTSTRAP_ADMIN_EMAIL)"
        echo "  🔑 Password:    $(env_file_value DEERFLOW_BOOTSTRAP_ADMIN_PASSWORD)"
        echo "  🔁 Reset token: $(env_file_value DEERFLOW_BOOTSTRAP_PASSWORD_TOKEN)"
        echo ""
    fi
    echo "  📋 View logs: make docker-logs"
    echo "  🛑 Stop:      make docker-stop"
    echo ""
}

# View Docker development logs
logs() {
    local service=""
    
    case "$1" in
        --frontend)
            service="frontend"
            echo -e "${BLUE}Viewing frontend logs...${NC}"
            ;;
        --gateway)
            service="gateway"
            echo -e "${BLUE}Viewing gateway logs...${NC}"
            ;;
        --nginx)
            service="nginx"
            echo -e "${BLUE}Viewing nginx logs...${NC}"
            ;;
        --provisioner)
            service="provisioner"
            echo -e "${BLUE}Viewing provisioner logs...${NC}"
            ;;
        "")
            echo -e "${BLUE}Viewing all logs...${NC}"
            ;;
        *)
            echo -e "${YELLOW}Unknown option: $1${NC}"
            echo "Usage: $0 logs [--frontend|--gateway|--nginx|--provisioner]"
            exit 1
            ;;
    esac
    
    cd "$DOCKER_DIR" && $COMPOSE_CMD logs -f $service
}

# Stop Docker development environment
stop() {
    # DEER_FLOW_ROOT is referenced in docker-compose-dev.yaml; set it before
    # running compose down to suppress "variable is not set" warnings.
    if [ -z "$DEER_FLOW_ROOT" ]; then
        export DEER_FLOW_ROOT="$PROJECT_ROOT"
    fi
    echo "Stopping Docker development services..."
    cd "$DOCKER_DIR" && $COMPOSE_CMD down
    echo "Cleaning up sandbox containers..."
    "$SCRIPT_DIR/cleanup-containers.sh" deer-flow-sandbox 2>/dev/null || true
    echo -e "${GREEN}✓ Docker services stopped${NC}"
}

# Restart Docker development environment
restart() {
    echo "========================================"
    echo "  Restarting DeerFlow Docker Services"
    echo "========================================"
    echo ""
    echo -e "${BLUE}Restarting containers...${NC}"
    cd "$DOCKER_DIR" && $COMPOSE_CMD restart
    echo ""
    echo -e "${GREEN}✓ Docker services restarted${NC}"
    echo ""
    echo "  🌐 Application: http://localhost:2026"
    echo "  📋 View logs: make docker-logs"
    echo ""
}

# Show help
help() {
    echo "DeerFlow Docker Management Script"
    echo ""
    echo "Usage: $0 <command> [options]"
    echo ""
    echo "Commands:"
    echo "  init              - Pull the sandbox image (speeds up first Pod startup)"
    echo "  start             - Start Docker services (auto-detects sandbox mode from config.yaml)"
    echo "  start --gateway   - Start without LangGraph container (Gateway mode, experimental)"
    echo "  restart           - Restart all running Docker services"
    echo "  logs [option] - View Docker development logs"
    echo "                  --frontend   View frontend logs only"
    echo "                  --gateway    View gateway logs only"
    echo "                  --nginx      View nginx logs only"
    echo "                  --provisioner View provisioner logs only"
    echo "  stop          - Stop Docker development services"
    echo "  help          - Show this help message"
    echo ""
}

main() {
    # Main command dispatcher
    case "$1" in
        init)
            init
            ;;
        start)
            shift
            start "$@"
            ;;
        restart)
            restart
            ;;
        logs)
            logs "$2"
            ;;
        stop)
            stop
            ;;
        help|--help|-h|"")
            help
            ;;
        *)
            echo -e "${YELLOW}Unknown command: $1${NC}"
            echo ""
            help
            exit 1
            ;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
