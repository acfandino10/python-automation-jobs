.PHONY: install test lint format typecheck ci

install:
	python -m pip install -e ".[dev]"

test:
	pytest

lint:
	ruff check .
	ruff format --check .

format:
	ruff format .
	ruff check --fix .

typecheck:
	mypy

ci: lint typecheck test
