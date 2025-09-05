# kubeSmartService

Developed and maintained by Yassir AIT EL AIZZI.

Smart Kubernetes service resolver for Python apps (Django, Flask, any WSGI/ASGI). Connects via the official Kubernetes Python client, performs health checks and automatic failover, and exposes simple metrics.

## Installation
```bash
pip3 install kubeSmartService
```

## Quick API
```python
from kubesmartservice import kube_service, init_resolver

# Initialize once at startup (recommended)
init_resolver(cache_ttl_seconds=8.0, retries=2, backoff_base=0.2)

svc = kube_service("my-backend", namespace="default", failover=True)
print(svc.host, svc.port)
print(svc.available_pods)
print(svc.status())  # {"latency_ms": ..., "pods": N, "failover_count": X, "active_pod": "..."}
```

## Features
- Caching of service/pod discovery (default TTL ~8s) to minimize Kubernetes API calls
- TCP health check with retry and exponential backoff
- Automatic failover across pods; fallback endpoint if Kubernetes is unavailable
- Startup initialization for Django/Flask to avoid per-request setup
- Mock mode for local development without a cluster
- Minimal permissions usage; robust error handling and structured logging

## Django example (startup-safe)
```python
# views.py
from django.http import JsonResponse
from kubesmartservice import kube_service, init_resolver

# recommended: in settings.py at startup
# from kubesmartservice import init_resolver
# init_resolver(cache_ttl_seconds=8.0, retries=2, backoff_base=0.2)

# Resolve a backend service and proxy a call (pseudo-code)
def health(request):
    svc = kube_service("my-backend", namespace="default")
    data = {
        "endpoint": f"http://{svc.host}:{svc.port}",
        "metrics": svc.status(),
    }
    return JsonResponse(data)
```

## Flask example (app init)
```python
from flask import Flask, jsonify
from kubesmartservice import kube_service, init_resolver

app = Flask(__name__)

@app.before_first_request
def setup_resolver():
    init_resolver(cache_ttl_seconds=8.0, retries=2, backoff_base=0.2)

@app.get("/health")
def health():
    svc = kube_service("my-backend", namespace="default")
    return jsonify({
        "endpoint": f"http://{svc.host}:{svc.port}",
        "metrics": svc.status(),
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
```

## How it works
- Loads Kubernetes config: in-cluster if available, otherwise local kubeconfig.
- Fetches Service ports and Endpoints, lists pods by label `app=<service_name>`.
- Health checks candidates via TCP connect with retry/backoff; fails over to the next IP if unreachable.
- Tracks latency and failover count; exposes via `status()`.
- Detects environment (minikube/staging/prod) heuristically. Mock mode via `KUBESMARTSERVICE_MOCK=1`.

## API Reference

### init_resolver(...)
Initialize the global resolver once at application startup.

Parameters:
- cache_ttl_seconds: float (default 8.0) – cache duration for discovery results
- health_timeout: float (default 1.5) – TCP connect timeout per attempt
- retries: int (default 2) – number of additional attempts per candidate pod
- backoff_base: float (default 0.2) – exponential backoff base (seconds)
- label_selector: str | None – override default `app=<service_name>` selector
- mock_mode: bool | None – force mock mode; default from env `KUBESMARTSERVICE_MOCK`
- fallback_host: str | None – fallback endpoint host if Kubernetes unavailable
- fallback_port: int | None – fallback endpoint port

Environment variables:
- KUBESMARTSERVICE_MOCK=1 – enable mock mode (uses fallback or 127.0.0.1:8000)
- KUBE_FALLBACK_HOST / KUBE_FALLBACK_PORT – default fallback endpoint

### kube_service(service_name, namespace="default", failover=True, ...)
Resolve a Service to a healthy pod endpoint using cached discovery.

Returns: SmartService with attributes `host`, `port`, `active_pod`, `available_pods` and method `status()`.

Optional keyword overrides (when not using init_resolver): `cache_ttl_seconds`, `health_timeout`, `retries`, `backoff_base`, `label_selector`, `mock_mode`, `fallback_host`, `fallback_port`.

### SmartService.status()
Returns a dict: `{ "latency_ms": float, "pods": int, "failover_count": int, "active_pod": str|None }`.

## Logging & Debugging
- Library logger name: `kubesmartservice`
- Logs selected pod and failover attempts at DEBUG/WARNING

Example:
```python
import logging
logging.basicConfig(level=logging.INFO)
logging.getLogger("kubesmartservice").setLevel(logging.DEBUG)
```

## Error Handling
Possible exceptions:
- `KubeAPIUnavailableError` – cluster config cannot be loaded
- `ServiceNotFoundError` – Service not found in the target namespace
- `NoHealthyEndpointsError` – no reachable pods and no fallback configured

Always wrap calls in try/except in critical paths and use a fallback when appropriate.

## Security & Permissions
- Uses read-only access to Services, Endpoints, and optionally Pods (for names). If listing pods is denied, resolution still works; pod names will be empty.
- Prefer binding a minimal Role/ClusterRole to the service account running your app.

## Requirements
- Python 3.8+
- Kubernetes Python client (`kubernetes`)

## License
MIT
