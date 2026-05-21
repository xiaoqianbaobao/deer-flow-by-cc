"""``/metrics`` — Prometheus text-format endpoint for identity (spec §13).

The endpoint is included in the gateway app only when
``ENABLE_IDENTITY=true``. When the flag is off the identity subsystem is
inert and there is nothing useful to export; hiding the route keeps the
flag-off surface minimal (and keeps the regression guard at
``tests/identity/test_feature_flag_offline.py`` green).

The response type is ``text/plain; version=0.0.4`` — the canonical
Prometheus exposition content-type. We intentionally do NOT gate the
route on authentication: Prometheus scrapers are typically whitelisted
at the network layer (k8s NetworkPolicy, GCP LB rule) and requiring
auth would break the default Prometheus scrape contract.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from app.gateway.identity.metrics import get_metrics

router = APIRouter()


@router.get(
    "/metrics",
    include_in_schema=False,
    response_class=Response,
    # The openapi JSON doesn't need to advertise a plain-text endpoint.
)
async def metrics() -> Response:
    """Render the Prometheus exposition payload."""

    body = await get_metrics().render_prometheus()
    # Version 0.0.4 is the current text-format version that Prometheus
    # servers expect; emitting it explicitly avoids a content-type
    # negotiation warning in the scraper log.
    return Response(content=body, media_type="text/plain; version=0.0.4")
