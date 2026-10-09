ifeq ($(OS),Windows_NT)
PY ?= python
VENV_BIN = $(VENV)/Scripts
EXE = .exe
else
PY ?= python3
VENV_BIN = $(VENV)/bin
EXE =
endif

VENV = backend/.venv
VENV_PY = $(VENV_BIN)/python$(EXE)

.PHONY: setup run

setup:            ## create venv, install deps, create .env
	$(PY) -m venv $(VENV)
	$(VENV_PY) -m pip install -q --upgrade pip
	$(VENV_PY) -m pip install -q -r backend/requirements.txt
	$(PY) -c "import os, shutil; os.path.exists('.env') or (shutil.copy('.env.example', '.env'), print('Created .env - put your OPENAI_API_KEY in it'))"

run:
	$(VENV_PY) -m uvicorn app.main:app --reload --port 8000 --app-dir backend