PY := .venv/bin/python

setup:
	python3 -m venv .venv && $(PY) -m pip install -q -e '.[dev]'

serve:
	$(PY) -m agentloop serve

test:
	$(PY) -m pytest -q tests
