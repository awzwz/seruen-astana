"""Online services. Each module registers handlers with seruen.common.clients.endpoint."""
import importlib

SERVICES = ["orchestrator", "session", "context", "nlu", "retrieval", "ranking", "routing", "explain", "guard",
            "llm-gateway", "catalog-api", "events-api", "feedback-api", "partner-portal"]


def module_name(service: str) -> str:
    return "seruen.services." + service.replace("-", "_")


def load_service_module(service: str):
    return importlib.import_module(module_name(service))


def load_all():
    for s in SERVICES:
        load_service_module(s)
