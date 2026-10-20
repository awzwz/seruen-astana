IMAGE ?= registry.digitalocean.com/seruen/seruen
TAG ?= $(shell git rev-parse --short HEAD)
PY := .venv/bin/python

.PHONY: dev ingest eval eval-cluster test build push deploy

dev:            ## whole system in one process (modular monolith) on http://localhost:8000
	SERUEN_MODE=local SERVICE=orchestrator PYTHONPATH=. $(PY) -m seruen.run

ingest:         ## collect → normalize → dedup → quality → describe → embed → data/catalog
	PYTHONPATH=. $(PY) -m seruen.ingest.run --out data/catalog

eval:           ## run the test scenarios locally → docs/TEST_SCENARIOS.md
	PYTHONPATH=. $(PY) tools/eval_scenarios.py

eval-cluster:   ## same scenarios against the deployed cluster
	PYTHONPATH=. $(PY) tools/eval_scenarios.py --url https://$(HOST) --out docs/TEST_SCENARIOS_cluster.md

test:
	PYTHONPATH=. $(PY) -m pytest -q tests

build:          ## one amd64 image for all services, workers and jobs
	docker buildx build --platform linux/amd64 -t $(IMAGE):$(TAG) -t $(IMAGE):latest --push .

deploy:         ## roll the new image out to every deployment in namespace seruen
	kubectl -n seruen set image deployment --all '*=$(IMAGE):$(TAG)'
	kubectl -n seruen rollout status deployment --timeout=300s
