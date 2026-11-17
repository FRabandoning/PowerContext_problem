"""Minimal client for a locally running PowerContext Server (1.2.x)."""

from __future__ import annotations

import os

import httpx


class PowerContextError(RuntimeError):
    pass


def base_url() -> str:
    return os.environ.get("POWERCONTEXT_URL", "http://127.0.0.1:8000").rstrip("/")


def _client() -> httpx.Client:
    # trust_env=False: NO_PROXY entries such as "[::1]" crash httpx 0.28 URLPattern
    return httpx.Client(base_url=base_url(), timeout=15.0, trust_env=False)


def health() -> dict:
    try:
        with _client() as client:
            response = client.get("/health/ready")
            return {"ok": response.status_code == 200, "detail": response.text[:200]}
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": str(exc)}


def _raise(response: httpx.Response) -> dict:
    if response.status_code >= 400:
        raise PowerContextError(f"{response.status_code}: {response.text[:300]}")
    return response.json()


def ensure_scope(learner_id: str) -> str:
    title = f"StudyLoop · {learner_id}"
    summary = f"复习教练为学习者 {learner_id} 保存的薄弱点、进度与偏好"
    with _client() as client:
        page = _raise(client.get("/v1/scopes"))
        for scope in page.get("items", []):
            if scope.get("title") == title:
                return scope["scope_id"]
        response = client.post(
            "/v1/scopes",
            json={
                "title": title,
                "summary": summary,
                "idempotency_key": f"studyloop-scope-{learner_id}",
            },
        )
        if response.status_code == 409:
            page = _raise(client.get("/v1/scopes"))
            for scope in page.get("items", []):
                if scope.get("title") == title:
                    return scope["scope_id"]
        return _raise(response)["scope_id"]


def remember(scope_id: str, kind: str, text: str, reason: str | None = None) -> dict:
    payload = {"scope_id": scope_id, "kind": kind, "text": text}
    if reason:
        payload["reason"] = reason
    with _client() as client:
        return _raise(client.post("/v1/memory/remember", json=payload))


def search(scope_id: str, query: str, limit: int = 20) -> dict:
    with _client() as client:
        return _raise(
            client.post(
                "/v1/memory/search",
                json={"scope_id": scope_id, "query": query, "limit": limit},
            )
        )


def list_entries(scope_id: str) -> list[dict]:
    with _client() as client:
        return _raise(client.post("/v1/memory/entries/list", json={"scope_id": scope_id}))[
            "entries"
        ]