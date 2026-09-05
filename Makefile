.PHONY: help setup install-cli build-wheels build-venvs build-docker build-all \
	test test-team lint format clean build-team build-lib build-app

# Color output
RED=\033[0;31m
GREEN=\033[0;32m
YELLOW=\033[1;33m
NC=\033[0m # No Color

help:
	@echo "$(GREEN)vynmatrix - Developer Commands$(NC)"
	@echo ""
	@echo "Setup:"
	@echo "  make setup           - Initial setup (install CLI + dependencies)"
	@echo "  make install-cli     - Install developer CLI tool"
	@echo ""
	@echo "Build:"
	@echo "  make build-wheels    - Build all configured library/strategy wheels"
	@echo "  make build-venvs     - Create all virtual environments"
	@echo "  make build-docker    - Build all Docker images"
	@echo "  make build-all       - Build everything"
	@echo "  make build-lib LIB=lib_common - Build one library wheel"
	@echo "  make build-app APP=scoring_engine - Build one application virtualenv"
	@echo ""
	@echo "Development:"
	@echo "  make test            - Run all tests"
	@echo "  make lint            - Run linting"
	@echo "  make format          - Format code"
	@echo "  make clean           - Clean build artifacts"
	@echo ""
	@echo "Team-specific (using CLI):"
	@echo "  make build-team TEAM=platform - Build all platform team components"
	@echo "  make test-team TEAM=quant     - Test quant team components"
	@echo ""
	@echo "For more commands, run: vmdev --help"

setup:
	@echo "$(GREEN)Setting up the vynmatrix platform development environment...$(NC)"
	@echo "$(YELLOW)Creating build directories...$(NC)"
	@mkdir -p build/wheels build/venvs build/docker
	@mkdir -p Data
	@echo "$(YELLOW)Installing developer CLI...$(NC)"
	@$(MAKE) install-cli
	@echo "$(YELLOW)Installing git MR workflow...$(NC)"
	@cd tools/dev_cli && python -m dev_cli.main git install
	@echo ""
	@echo "$(GREEN)=====================================$(NC)"
	@echo "$(GREEN)✅ Setup complete!$(NC)"
	@echo "$(GREEN)=====================================$(NC)"
	@echo ""
	@echo "Next steps:"
	@echo "  Continue with SETUP.md; bootstrap through vmdev db after private configuration."
	@echo ""
	@echo "For all commands:    vmdev --help"

install-cli:
	@echo "$(YELLOW)Installing developer CLI...$(NC)"
	@cd tools/dev_cli && pip install -e .
	@echo "$(GREEN)CLI installed! Command: vmdev$(NC)"

build-wheels:
	@echo "$(YELLOW)Building configured library and strategy wheels...$(NC)"
	@vmdev build libs
	@vmdev build strategies

build-venvs:
	@echo "$(YELLOW)Creating virtual environments...$(NC)"
	@vmdev build venvs

build-docker:
	@echo "$(YELLOW)Building Docker images...$(NC)"
	@vmdev build docker --from-config

build-all:
	@$(MAKE) build-wheels
	@$(MAKE) build-venvs
	@$(MAKE) build-docker

test:
	@echo "$(YELLOW)Running all tests...$(NC)"
	@vmdev test all

test-team:
	@echo "$(YELLOW)Running tests for team: $(TEAM)$(NC)"
	@vmdev test team --team=$(TEAM)

lint:
	@echo "$(YELLOW)Running linters...$(NC)"
	@vmdev lint

format:
	@echo "$(YELLOW)Formatting code...$(NC)"
	@vmdev format

clean:
	@echo "$(RED)Cleaning build artifacts...$(NC)"
	@vmdev clean
	@echo "$(GREEN)Clean complete!$(NC)"

build-team:
	@echo "$(YELLOW)Building components for team: $(TEAM)$(NC)"
	@vmdev build team --team=$(TEAM)

build-lib:
	@echo "$(YELLOW)Building library: $(LIB)$(NC)"
	@test -n "$(LIB)" || (echo "$(RED)LIB is required$(NC)" && exit 2)
	@vmdev build libs --component "$(LIB)"

build-app:
	@echo "$(YELLOW)Building application virtualenv: $(APP)$(NC)"
	@test -n "$(APP)" || (echo "$(RED)APP is required$(NC)" && exit 2)
	@vmdev build venvs --app "$(APP)"
