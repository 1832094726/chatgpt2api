from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from fastapi import APIRouter, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict

from api.support import require_admin


DEFAULT_HLOOL_API_BASE = "https://email.hlool.cc"


class HLOOLMailRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    api_base: str = DEFAULT_HLOOL_API_BASE
    api_key: str
    payload: dict[str, Any] | None = None
    page: int | str | None = None
    per_page: int | str | None = None
    q: str | None = None
    id: int | str | None = None
    email: str | None = None


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _build_hlool_url(api_base: str, path: str, query: dict[str, str] | None = None) -> str:
    base = _clean_text(api_base) or DEFAULT_HLOOL_API_BASE
    parsed = urlparse(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail={"error": "api_base 必须是有效的 http/https 地址"})
    target = base.rstrip("/") + path
    if query:
        target = f"{target}?{urlencode({k: v for k, v in query.items() if _clean_text(v)})}"
    return target


def _call_hlool(method: str, url: str, api_key: str, payload: Any | None = None) -> Any:
    data = None
    headers = {
        "X-API-Key": api_key,
        "Accept": "application/json",
        "User-Agent": "chatgpt2api-hlool-toolbox/1.0",
    }
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")
            if not raw:
                return {"success": True, "data": {}}
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"raw": raw}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw)
        except json.JSONDecodeError:
            detail = {"error": raw or exc.reason}
        raise HTTPException(status_code=exc.code, detail=detail) from exc
    except URLError as exc:
        raise HTTPException(status_code=502, detail={"error": f"HLOOL Mail 请求失败: {exc.reason}"}) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail={"error": "HLOOL Mail 请求超时"}) from exc


async def _proxy_hlool(body: HLOOLMailRequest, method: str, path: str, query: dict[str, str] | None = None, payload: Any | None = None) -> Any:
    api_key = _clean_text(body.api_key)
    if not api_key:
        raise HTTPException(status_code=400, detail={"error": "api_key 不能为空"})
    url = _build_hlool_url(body.api_base, path, query)
    return await run_in_threadpool(_call_hlool, method, url, api_key, payload)


def create_router() -> APIRouter:
    router = APIRouter(prefix="/api/hlool-mail", tags=["hlool-mail"])

    @router.post("/domains")
    async def list_domains(body: HLOOLMailRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await _proxy_hlool(body, "GET", "/api/domains/available")

    @router.post("/generate")
    async def generate_mailbox(body: HLOOLMailRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await _proxy_hlool(body, "POST", "/api/generate-email", payload=body.payload or {})

    @router.post("/mailboxes")
    async def list_mailboxes(body: HLOOLMailRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        query = {
            "page": _clean_text(body.page) or "1",
            "per_page": _clean_text(body.per_page) or "20",
        }
        if _clean_text(body.q):
            query["q"] = _clean_text(body.q)
        return await _proxy_hlool(body, "GET", "/api/mailboxes", query=query)

    @router.post("/mailboxes/delete")
    async def delete_mailbox(body: HLOOLMailRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        mailbox_id = _clean_text(body.id)
        if not mailbox_id:
            raise HTTPException(status_code=400, detail={"error": "id 不能为空"})
        return await _proxy_hlool(body, "DELETE", f"/api/mailboxes/{mailbox_id}")

    @router.post("/emails")
    async def list_emails(body: HLOOLMailRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        email = _clean_text(body.email)
        if not email:
            raise HTTPException(status_code=400, detail={"error": "email 不能为空"})
        query = {
            "email": email,
            "page": _clean_text(body.page) or "1",
            "per_page": _clean_text(body.per_page) or "20",
        }
        return await _proxy_hlool(body, "GET", "/api/emails", query=query)

    @router.post("/emails/next")
    async def next_email(body: HLOOLMailRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        email = _clean_text(body.email)
        if not email:
            raise HTTPException(status_code=400, detail={"error": "email 不能为空"})
        return await _proxy_hlool(body, "GET", "/api/emails/next", query={"email": email})

    @router.post("/emails/read")
    async def read_email(body: HLOOLMailRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        email_id = _clean_text(body.id)
        if not email_id:
            raise HTTPException(status_code=400, detail={"error": "id 不能为空"})
        return await _proxy_hlool(body, "GET", f"/api/email/{email_id}")

    @router.post("/emails/clear")
    async def clear_emails(body: HLOOLMailRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        email = _clean_text(body.email)
        if not email:
            raise HTTPException(status_code=400, detail={"error": "email 不能为空"})
        return await _proxy_hlool(body, "DELETE", "/api/emails/clear", query={"email": email})

    return router
