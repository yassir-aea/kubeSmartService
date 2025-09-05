from __future__ import annotations

"""kubeSmartService public API."""

from .core import kube_service, SmartService, init_resolver, KubeSmartServiceError

__all__ = ["kube_service", "SmartService", "init_resolver", "KubeSmartServiceError"]
__version__ = "0.1.1"
__author__ = "Yassir AIT EL AIZZI"


