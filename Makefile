.PHONY: install run test eval lint format clean

install:
	python -m pip install -e '.[dev]'

run:
	uvicorn dxl_agent.api:app --host 127.0.0.1 --port 8000 --reload

test:
	python -m pytest

eval:
	python -m dxl_agent.eval_runner

lint:
	ruff check .
	ruff format --check .

format:
	ruff check --fix .
	ruff format .

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov data
