.PHONY: install run seed reset test lint fmt docker clean

install:
	python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt

run:
	uvicorn roomloop.api:app --reload --host 0.0.0.0 --port 8000

seed:
	python -m roomloop.seed --db roomloop.db

reset:
	python -m roomloop.seed --db roomloop.db --reset

test:
	python -m pytest

lint:
	ruff check roomloop tests

fmt:
	ruff check --fix roomloop tests

docker:
	docker build -t roomloop .
	docker run --rm -p 8000:8000 roomloop

clean:
	rm -f roomloop.db roomloop.db-wal roomloop.db-shm
	rm -rf .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
