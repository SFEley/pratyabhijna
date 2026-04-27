VPS     := serah@vesper.you
REMOTE  := /opt/pratyabhijna
SERVICE := pratyabhijna

.PHONY: deploy

deploy:
	@if ! git diff --quiet || ! git diff --cached --quiet; then \
		echo "error: uncommitted changes — commit or stash before deploying"; \
		exit 1; \
	fi
	@git fetch --quiet origin main
	@if git log --oneline origin/main..HEAD | grep -q .; then \
		echo "error: unpushed commits — push before deploying"; \
		exit 1; \
	fi
	@echo "→ deploying to $(VPS)"
	@ssh $(VPS) ' \
		set -e; \
		cd $(REMOTE); \
		git pull --ff-only origin main; \
		venv/bin/pip install -e . -q; \
		sudo systemctl restart $(SERVICE); \
		sleep 10; \
		systemctl is-active $(SERVICE) \
			&& echo "✓ $(SERVICE) is running" \
			|| { sudo systemctl status $(SERVICE) --no-pager -n 20; exit 1; } \
	'
