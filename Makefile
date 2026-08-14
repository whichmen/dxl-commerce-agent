.PHONY: install check run run-policy run-identity run-wecom-hub openclaw-setup services smoke test eval lint format clean

install:
	python -m pip install -e '.[dev]'

check:
	bash scripts/check_requirements.sh

run:
	bash scripts/run_dev.sh

run-policy:
	CLAWBOT_DECISION_MODE=policy bash scripts/run_dev.sh

run-identity:
	bash scripts/run_identity.sh

run-wecom-hub:
	bash scripts/run_wecom_hub.sh

openclaw-setup:
	bash openclaw/workspace-kefu-ops/scripts/setup_openclaw_kefu_ops.sh
	bash scripts/sync_openclaw_gateway_env.sh

services:
	bash scripts/install_decision_user_services.sh

smoke:
	bash scripts/smoke_test.sh

test:
	python -m pytest

eval:
	python -m dxl_agent.eval_runner

lint:
	ruff check . --select E9,F63,F7,F82
	python -m compileall -q src workers

format:
	ruff check --fix .
	ruff format .

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov
