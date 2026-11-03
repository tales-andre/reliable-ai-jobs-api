.PHONY: up chaos test down local-test local-chaos

# With Docker (nothing else to install)
up:
	docker compose up -d --build
chaos:
	docker compose run --rm chaos
test:
	docker compose run --rm tests
down:
	docker compose down -v

# Without Docker for the Python side (Postgres still from: docker compose up -d db)
local-test:
	python -m pytest
local-chaos:
	python -m chaos.run
