from __future__ import annotations

import logging
import os
import socket
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from kubernetes import client, config
from kubernetes.client import CoreV1Api

logger = logging.getLogger("kubesmartservice")


class KubeSmartServiceError(Exception):
    """Base error for kubeSmartService."""


class KubeAPIUnavailableError(KubeSmartServiceError):
    pass


class ServiceNotFoundError(KubeSmartServiceError):
    pass


class NoHealthyEndpointsError(KubeSmartServiceError):
    pass


@dataclass
class SmartService:
    """Resolved Kubernetes service endpoint with health and metrics.

    Attributes:
        host: Selected pod IP or service cluster IP/hostname.
        port: Selected port (int).
        active_pod: Name of the active pod chosen.
        available_pods: List of candidate pod names.
    """

    host: str
    port: int
    active_pod: Optional[str]
    available_pods: List[str]

    # internal metrics
    _latency_ms: float = 0.0
    _failover_count: int = 0

    def status(self) -> dict:
        """Return metrics about the last resolution.

        Returns:
            dict with keys: latency_ms, pods, failover_count, active_pod
        """

        return {
            "latency_ms": round(self._latency_ms, 2),
            "pods": len(self.available_pods),
            "failover_count": self._failover_count,
            "active_pod": self.active_pod,
        }


def _detect_environment() -> str:
    """Detect current Kubernetes environment.

    Returns one of: "minikube", "staging", "prod", or "unknown".
    Heuristics:
    - KUBERNETES_SERVICE_HOST env => in-cluster (staging/prod)
    - MINIKUBE_ACTIVE_DOCKERD or context name containing "minikube" => minikube
    - Otherwise unknown.
    """

    if os.getenv("KUBERNETES_SERVICE_HOST"):
        env = os.getenv("APP_ENV") or os.getenv("ENV") or "prod"
        return "staging" if env.lower().startswith("stag") else "prod"
    try:
        contexts, active = config.list_kube_config_contexts()  # type: ignore[call-arg]
        if active and "minikube" in (active.get("name") or ""):
            return "minikube"
    except Exception:
        if os.getenv("MINIKUBE_ACTIVE_DOCKERD"):
            return "minikube"
    return "unknown"


def _load_kube_api() -> CoreV1Api:
    """Load Kubernetes configuration and return CoreV1Api.

    Tries in-cluster first, then falls back to local kubeconfig.
    """

    try:
        config.load_incluster_config()
        logger.debug("Loaded in-cluster Kubernetes config")
    except Exception:
        try:
            config.load_kube_config()
            logger.debug("Loaded local kubeconfig")
        except Exception as e:
            raise KubeAPIUnavailableError("Could not load Kubernetes configuration") from e
    return client.CoreV1Api()


def _service_endpoints(api: CoreV1Api, service_name: str, namespace: str) -> Tuple[List[str], int]:
    """Get pod IPs and port for a service. Returns (pod_ips, port)."""

    try:
        svc = api.read_namespaced_service(service_name, namespace)
    except Exception as e:
        raise ServiceNotFoundError(f"Service {service_name!r} not found in ns={namespace}") from e

    ports = svc.spec.ports or []
    if not ports:
        raise KubeSmartServiceError(f"Service {service_name} has no ports defined")
    port = int(ports[0].port)

    try:
        endpoints = api.read_namespaced_endpoints(service_name, namespace)
    except Exception:
        endpoints = None

    pod_ips: List[str] = []
    if endpoints and endpoints.subsets:
        for subset in endpoints.subsets:
            if subset.addresses:
                for addr in subset.addresses:
                    if addr.ip:
                        pod_ips.append(addr.ip)
    return pod_ips, port


def _tcp_healthcheck(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


class _Resolver:
    """Resolver with caching, retry/backoff, and optional mock/fallback."""

    def __init__(
        self,
        *,
        cache_ttl_seconds: float = 8.0,
        health_timeout: float = 1.5,
        retries: int = 2,
        backoff_base: float = 0.2,
        label_selector: Optional[str] = None,
        mock_mode: bool = False,
        fallback_host: Optional[str] = None,
        fallback_port: Optional[int] = None,
    ) -> None:
        self.cache_ttl_seconds = cache_ttl_seconds
        self.health_timeout = health_timeout
        self.retries = retries
        self.backoff_base = backoff_base
        self.label_selector = label_selector
        self.mock_mode = mock_mode
        self.fallback_host = fallback_host or os.getenv("KUBE_FALLBACK_HOST")
        fp = os.getenv("KUBE_FALLBACK_PORT")
        self.fallback_port = fallback_port if fallback_port is not None else (int(fp) if fp else None)

        self._cache: Dict[Tuple[str, str], Tuple[float, List[str], int, List[str]]] = {}
        self._lock = threading.Lock()
        self._api: Optional[CoreV1Api] = None

    def _get_api(self) -> CoreV1Api:
        if self._api is None:
            self._api = _load_kube_api()
        return self._api

    def _list_pod_names(self, namespace: str, service_name: str) -> List[str]:
        selector = self.label_selector or f"app={service_name}"
        try:
            pods = self._get_api().list_namespaced_pod(namespace, label_selector=selector)
            return [p.metadata.name for p in pods.items if p.metadata and p.metadata.name]
        except Exception:
            # Avoid requiring extra permissions; it's optional.
            return []

    def _refresh_cache(self, namespace: str, service_name: str) -> Tuple[List[str], int, List[str]]:
        api = self._get_api()
        pod_ips, port = _service_endpoints(api, service_name, namespace)
        pod_names = self._list_pod_names(namespace, service_name) if pod_ips else []
        with self._lock:
            self._cache[(namespace, service_name)] = (time.time(), pod_ips, port, pod_names)
        return pod_ips, port, pod_names

    def _get_cached(self, namespace: str, service_name: str) -> Optional[Tuple[List[str], int, List[str]]]:
        with self._lock:
            entry = self._cache.get((namespace, service_name))
        if not entry:
            return None
        ts, pod_ips, port, pod_names = entry
        if (time.time() - ts) > self.cache_ttl_seconds:
            return None
        return pod_ips, port, pod_names

    def resolve(self, service_name: str, namespace: str, failover: bool) -> SmartService:
        if self.mock_mode:
            host = self.fallback_host or "127.0.0.1"
            port = int(self.fallback_port or 8000)
            ss = SmartService(host=host, port=port, active_pod=None, available_pods=[])
            ss._latency_ms = 0.0
            ss._failover_count = 0
            logger.info("[mock] Using %s:%s for %s/%s", host, port, namespace, service_name)
            return ss

        try:
            cached = self._get_cached(namespace, service_name)
            if cached is None:
                pod_ips, port, pod_names = self._refresh_cache(namespace, service_name)
            else:
                pod_ips, port, pod_names = cached
        except KubeSmartServiceError:
            if self.fallback_host and self.fallback_port:
                logger.error("Kubernetes API unavailable. Falling back to %s:%s", self.fallback_host, self.fallback_port)
                ss = SmartService(host=str(self.fallback_host), port=int(self.fallback_port), active_pod=None, available_pods=[])
                ss._latency_ms = 0.0
                ss._failover_count = 0
                return ss
            raise

        candidates = list(pod_ips)
        if not candidates:
            if self.fallback_host and self.fallback_port:
                logger.warning("No endpoints for %s/%s. Fallback to %s:%s", namespace, service_name, self.fallback_host, self.fallback_port)
                ss = SmartService(host=str(self.fallback_host), port=int(self.fallback_port), active_pod=None, available_pods=[])
                ss._latency_ms = 0.0
                ss._failover_count = 0
                return ss
            raise NoHealthyEndpointsError(f"No endpoints found for {service_name} in {namespace}")

        failover_count = 0
        chosen_host: Optional[str] = None
        start = time.time()
        for ip in candidates:
            # retry with backoff
            attempt_ok = False
            for attempt in range(self.retries + 1):
                timeout = self.health_timeout * (1.0 + 0.1 * attempt)
                if _tcp_healthcheck(ip, port, timeout=timeout):
                    attempt_ok = True
                    break
                sleep_s = (self.backoff_base * (2 ** attempt))
                time.sleep(min(sleep_s, 2.0))
            if attempt_ok:
                chosen_host = ip
                logger.debug("Selected pod %s:%s for %s/%s", ip, port, namespace, service_name)
                break
            if failover:
                failover_count += 1
                logger.warning("Health check failed for %s:%s, failing over (%d)", ip, port, failover_count)
            else:
                break

        latency_ms = (time.time() - start) * 1000.0

        if chosen_host is None:
            if self.fallback_host and self.fallback_port:
                logger.error("No healthy endpoints for %s/%s. Falling back to %s:%s", namespace, service_name, self.fallback_host, self.fallback_port)
                chosen_host = str(self.fallback_host)
                port = int(self.fallback_port)
            else:
                raise NoHealthyEndpointsError(f"No healthy endpoints for {service_name} in {namespace}")

        active_pod = None
        if pod_names and chosen_host in pod_ips:
            idx = pod_ips.index(chosen_host)
            if idx < len(pod_names):
                active_pod = pod_names[idx]

        ss = SmartService(host=chosen_host, port=port, active_pod=active_pod, available_pods=pod_names)
        ss._latency_ms = latency_ms
        ss._failover_count = failover_count
        return ss


_GLOBAL_RESOLVER: Optional[_Resolver] = None
_RESOLVER_LOCK = threading.Lock()


def init_resolver(
    *,
    cache_ttl_seconds: float = 8.0,
    health_timeout: float = 1.5,
    retries: int = 2,
    backoff_base: float = 0.2,
    label_selector: Optional[str] = None,
    mock_mode: Optional[bool] = None,
    fallback_host: Optional[str] = None,
    fallback_port: Optional[int] = None,
) -> None:
    """Initialize a global resolver for app startup.

    Use in Django settings.py or Flask app factory to avoid per-request initialization.
    """

    mm = mock_mode if mock_mode is not None else (os.getenv("KUBESMARTSERVICE_MOCK", "").lower() in ("1", "true", "yes"))
    resolver = _Resolver(
        cache_ttl_seconds=cache_ttl_seconds,
        health_timeout=health_timeout,
        retries=retries,
        backoff_base=backoff_base,
        label_selector=label_selector,
        mock_mode=mm,
        fallback_host=fallback_host,
        fallback_port=fallback_port,
    )
    with _RESOLVER_LOCK:
        global _GLOBAL_RESOLVER
        _GLOBAL_RESOLVER = resolver
    logger.info("kubeSmartService resolver initialized (mock=%s, ttl=%ss)", mm, cache_ttl_seconds)


def kube_service(
    service_name: str,
    namespace: str = "default",
    failover: bool = True,
    *,
    cache_ttl_seconds: Optional[float] = None,
    health_timeout: Optional[float] = None,
    retries: Optional[int] = None,
    backoff_base: Optional[float] = None,
    label_selector: Optional[str] = None,
    mock_mode: Optional[bool] = None,
    fallback_host: Optional[str] = None,
    fallback_port: Optional[int] = None,
) -> SmartService:
    """Resolve a Kubernetes service to a healthy pod endpoint.

    Safe defaults: cached discovery, TCP health check, optional failover and fallback.
    If you called init_resolver() at startup, this will reuse it.
    """

    resolver = _GLOBAL_RESOLVER
    if resolver is None:
        resolver = _Resolver(
            cache_ttl_seconds=cache_ttl_seconds or 8.0,
            health_timeout=health_timeout or 1.5,
            retries=retries if retries is not None else 2,
            backoff_base=backoff_base or 0.2,
            label_selector=label_selector,
            mock_mode=(mock_mode if mock_mode is not None else (os.getenv("KUBESMARTSERVICE_MOCK", "").lower() in ("1", "true", "yes"))),
            fallback_host=fallback_host,
            fallback_port=fallback_port,
        )
    return resolver.resolve(service_name, namespace, failover)



