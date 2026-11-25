.PHONY: verify test live lint fmt fmt-check typecheck mdlint schemas

verify: lint fmt-check typecheck test mdlint schemas

test:
	uv run pytest

# Needs real models and credentials; run locally, never in CI.
live:
	uv run pytest -m live

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

fmt-check:
	uv run ruff format --check .

typecheck:
	uv run pyright

mdlint:
	uv run pymarkdown --config .pymarkdown.json scan -r --respect-gitignore .

# The same checks CI runs: every schema is valid JSON Schema, config.example.yaml matches its schema.
schemas:
	uv run check-jsonschema --check-metaschema schemas/*.json schemas/*/*.json
	uv run check-jsonschema --schemafile schemas/config.json config.example.yaml
