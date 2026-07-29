.PHONY: run test

run:
	uv run --project backend python backend/src/ai-customer-assistant/main.py

test:
	uv run --project backend pytest backend/tests