from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

from . import __version__
from .store import IdentityStore


class IdentityLookupRequest(BaseModel):
    org_id: str
    platform: str
    room_name: str = ""
    alias_value: str

    @field_validator("org_id", "platform", "alias_value")
    @classmethod
    def required_text(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("required")
        return cleaned


class IdentityRecord(BaseModel):
    org_id: str
    platform: str
    room_name: str = ""
    platform_nickname: str
    km_name: str
    dxl_nickname: str = ""
    km_identity_type: str = "mapped"
    last_tid: str = ""


class BlacklistAddRequest(BaseModel):
    org_id: str
    wangwang_id: str
    days: int = Field(default=7, ge=1, le=3650)


class BlacklistRemoveRequest(BaseModel):
    org_id: str
    wangwang_id: str


def _check_api_key(expected: str, supplied: str | None) -> None:
    if expected and (supplied or "") != expected:
        raise HTTPException(status_code=403, detail="invalid_api_key")


def create_app(store: IdentityStore | None = None) -> FastAPI:
    db_path = Path(os.getenv("IDENTITY_DB_PATH", "data/identity.db")).expanduser()
    service_store = store or IdentityStore(db_path)
    api_key = str(os.getenv("IDENTITY_API_KEY", "") or "").strip()
    app = FastAPI(title="kefu-identity-service", version=__version__)
    app.state.identity_store = service_store

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "kefu-identity-service", "version": __version__}

    @app.post("/statistic/lookup_user_identity")
    def lookup_user_identity(
        request: IdentityLookupRequest,
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _check_api_key(api_key, x_api_key)
        items = service_store.lookup(
            org_id=request.org_id,
            platform=request.platform,
            room_name=request.room_name,
            alias_value=request.alias_value,
        )
        if not items:
            result = "not_found"
        elif len(items) == 1:
            result = "resolved"
        else:
            result = "ambiguous"
        return {"code": 0, "content": {"result": result, "items": items}}

    @app.post("/admin/identities")
    def upsert_identity(
        request: IdentityRecord,
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _check_api_key(api_key, x_api_key)
        service_store.upsert_identity(request.model_dump())
        return {"ok": True, "message": "identity_saved"}

    @app.post("/zhibo/add_black_house")
    def add_black_house(
        request: BlacklistAddRequest,
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _check_api_key(api_key, x_api_key)
        expires_at_ms = int(time.time() * 1000) + request.days * 86_400_000
        service_store.blacklist_add(
            org_id=request.org_id.strip(),
            wangwang_id=request.wangwang_id.strip(),
            expires_at_ms=expires_at_ms,
        )
        return {"ok": True, "message": "blacklist_added"}

    @app.post("/zhibo/black_house_delete")
    def black_house_delete(
        request: BlacklistRemoveRequest,
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _check_api_key(api_key, x_api_key)
        removed = service_store.blacklist_remove(
            org_id=request.org_id.strip(),
            wangwang_id=request.wangwang_id.strip(),
        )
        return {"ok": True, "message": "blacklist_removed", "removed": removed}

    return app
