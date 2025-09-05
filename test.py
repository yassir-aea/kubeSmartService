"""
Examples for using kubeSmartService with Flask and Django.

Run Flask example locally (no cluster needed):
    export KUBESMARTSERVICE_MOCK=1
    export KUBE_FALLBACK_HOST=127.0.0.1
    export KUBE_FALLBACK_PORT=8080
    python3 test.py

Django example is provided as code snippets below (settings.py & views.py).
"""

from __future__ import annotations

from kubesmartservice import init_resolver, kube_service


def run_flask_example() -> None:
    try:
        from flask import Flask, jsonify
    except Exception:  # Flask not installed
        print("[Flask example] Install Flask to run: pip install Flask")
        return

    app = Flask(__name__)

    @app.before_first_request
    def setup_resolver() -> None:
        # Initialize resolver once at app startup
        init_resolver(cache_ttl_seconds=8.0, retries=2, backoff_base=0.2)

    @app.get("/health")
    def health():  # type: ignore[no-redef]
        svc = kube_service("my-backend", namespace="default", failover=True)
        return jsonify(
            {
                "endpoint": f"http://{svc.host}:{svc.port}",
                "metrics": svc.status(),
            }
        )

    print("[Flask] Serving on http://127.0.0.1:5000/health")
    app.run(host="127.0.0.1", port=5000)


DJANGO_SETTINGS_EXAMPLE = """
# settings.py
from kubesmartservice import init_resolver

# Initialize once at startup (safe defaults)
init_resolver(cache_ttl_seconds=8.0, retries=2, backoff_base=0.2)
"""


DJANGO_VIEW_EXAMPLE = """
# views.py
from django.http import JsonResponse
from kubesmartservice import kube_service

def health(request):
    svc = kube_service("my-backend", namespace="default", failover=True)
    return JsonResponse({
        "endpoint": f"http://{svc.host}:{svc.port}",
        "metrics": svc.status(),
    })
"""


def print_django_examples() -> None:
    print("\n[Django] settings.py example:\n")
    print(DJANGO_SETTINGS_EXAMPLE)
    print("\n[Django] views.py example:\n")
    print(DJANGO_VIEW_EXAMPLE)


if __name__ == "__main__":
    # Print Django snippets first
    print_django_examples()
    # Then try running the Flask demo (if Flask installed)
    run_flask_example()


