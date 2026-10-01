PY ?= python3
VENV = backend/.venv

.PHONY: setup run

setup:            ## create venv, install deps, create .env
	$(PY) -m venv $(VENV)
	$(VENV)/bin/pip install -q --upgrade pip
	$(VENV)/bin/pip install -q -r backend/requirements.txt
	@test -f .env || (cp .env.example .env && echo "Created .env - put your OPENAI_API_KEY in it")

run:              ## start backend + frontend on http://localhost:8000
	cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000
