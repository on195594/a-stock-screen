.PHONY: setup demo check test-mobile

PYTHON := $(shell if [ -f .venv/bin/python ]; then echo .venv/bin/python; else echo python3; fi)

setup:
	@echo "==> Setting up environment with frozen lockfile..."
	uv sync --frozen --all-extras

demo:
	@echo "==> Setting up demo workspace..."
	@$(PYTHON) manage.py setup-demo --state-dir .local/demo --journal-mode DELETE
	@echo "==> Starting Flet Dynamic Web Workbench on 127.0.0.1:8550..."
	@APP_MODE=demo STATE_DIR=.local/demo PUBLIC_BASE_URL=http://127.0.0.1:8550 FLET_WEB_NO_CDN=true FLET_SESSION_TIMEOUT=3600 FLET_OAUTH_STATE_TIMEOUT=600 $(PYTHON) app.py

check:
	@echo "==> Running pytest checks..."
	@$(PYTHON) -m pytest test_screen.py test_workspace.py test_services.py test_app.py test_deploy.py -q -o cache_dir=/tmp/.pytest_cache
	@echo "==> Running ruff format checks..."
	@$(PYTHON) -m ruff format --check --cache-dir /tmp/.ruff_cache .
	@echo "==> Running ruff checks..."
	@$(PYTHON) -m ruff check --cache-dir /tmp/.ruff_cache .
	@echo "==> Running mypy checks..."
	@$(PYTHON) -m mypy --cache-dir /tmp/.mypy_cache workspace.py auth.py services.py app.py

test-mobile:
	@echo "==> Running mobile end-to-end browser test..."
	@$(PYTHON) test_mobile.py
