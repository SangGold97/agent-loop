PY := .venv/bin/python

setup:
	python3 -m venv .venv && $(PY) -m pip install -q -e '.[dev]'

serve:
	$(PY) -m agentloop serve

# The same server as the agentloop user service (remote/server-finish.sh), so it outlives the terminal
start:
	systemctl --user start agentloop

stop:
	systemctl --user stop agentloop

logs:
	journalctl --user -u agentloop -f

test:
	$(PY) -m pytest -q tests
