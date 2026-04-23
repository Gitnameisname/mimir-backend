"""
Regression test for global exception→HTTP status mapping.

Background
----------
The S2-5 runtime regression review (docs/개발문서/S2_5/UI_Admin_GoldenSets_런타임회귀리뷰.md)
observed that a raw ``ValueError`` propagating out of a sync endpoint appeared on
the wire as HTTP 503 on at least one occurrence.  The reviewer flagged this as a
deviation from the intended 500 mapping and asked for a follow-up investigation
(#32).

Our exception-handler policy (see ``app/api/errors/handlers.py``):

* ``ApiError``                   → ``exc.http_status`` (typed mapping)
* ``RequestValidationError``     → 400
* ``StarletteHTTPException``     → ``exc.status_code``
* ``Exception`` (catch-all)      → **500** ``internal_server_error``

This test locks that contract in place so future middleware/order changes cannot
silently re-map 500 to 503 (or anything else) without a test failure.  Any
infrastructure-dependent 503 response MUST go through the explicit
``ApiServiceUnavailableError`` path so the intent is visible in the code.
"""
from __future__ import annotations

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from app.api.errors.exceptions import (
    ApiNotFoundError,
    ApiPermissionDeniedError,
    ApiServiceUnavailableError,
)
from app.api.errors.handlers import register_exception_handlers


def _build_probe_app() -> FastAPI:
    """Build a minimal FastAPI app with our real exception handlers + probe routes.

    We rebuild the app (rather than reusing ``app.main.app``) so this test stays
    isolated from business middleware side effects (DB init, metrics, slowapi
    limiter bootstrap) and only exercises the handler→status contract.
    """
    app = FastAPI()
    register_exception_handlers(app)

    router = APIRouter()

    @router.get("/raise/value-error")
    def _raise_value_error():
        # Matches the shape of the real #30 bug: Pydantic v2 strict setattr
        # rejects unknown fields with ValueError.
        raise ValueError(
            '"GoldenSet" object has no field "item_count"'
        )

    @router.get("/raise/runtime-error")
    def _raise_runtime_error():
        raise RuntimeError("synthetic runtime error")

    @router.get("/raise/type-error")
    def _raise_type_error():
        raise TypeError("synthetic type error")

    @router.get("/raise/key-error")
    def _raise_key_error():
        raise KeyError("missing_key")

    @router.get("/raise/service-unavailable")
    def _raise_service_unavailable():
        # This is the *only* code path that should produce 503.
        raise ApiServiceUnavailableError(
            message="Upstream dependency unreachable",
            internal_detail="simulated",
        )

    @router.get("/raise/not-found")
    def _raise_api_not_found():
        raise ApiNotFoundError(message="Sample not found")

    @router.get("/raise/permission-denied")
    def _raise_api_permission_denied():
        raise ApiPermissionDeniedError(message="Blocked by scope profile")

    app.include_router(router)
    return app


@pytest.fixture(scope="module")
def probe_client() -> TestClient:
    app = _build_probe_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


# ---------------------------------------------------------------------------
# Core contract: any bare unhandled Exception subclass → 500
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path, exc_label",
    [
        ("/raise/value-error",   "ValueError"),
        ("/raise/runtime-error", "RuntimeError"),
        ("/raise/type-error",    "TypeError"),
        ("/raise/key-error",     "KeyError"),
    ],
)
def test_unhandled_exception_is_500_not_503(probe_client: TestClient, path: str, exc_label: str) -> None:
    resp = probe_client.get(path)

    assert resp.status_code == 500, (
        f"{exc_label} at {path} should map to 500 via unhandled_exception_handler, "
        f"got {resp.status_code}. If this is 503 the handler chain or a middleware "
        f"is rewriting the status — restore the 500 mapping."
    )

    body = resp.json()
    assert body["error"]["code"] == "internal_server_error"
    # The body must not leak the raw exception message (copy of default msg).
    assert body["error"]["message"] == "예기치 않은 오류가 발생했습니다"


# ---------------------------------------------------------------------------
# 503 is reserved for explicit ApiServiceUnavailableError only
# ---------------------------------------------------------------------------

def test_api_service_unavailable_error_is_503(probe_client: TestClient) -> None:
    resp = probe_client.get("/raise/service-unavailable")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "service_unavailable"


def test_typed_api_errors_preserve_status(probe_client: TestClient) -> None:
    assert probe_client.get("/raise/not-found").status_code == 404
    assert probe_client.get("/raise/permission-denied").status_code == 403
