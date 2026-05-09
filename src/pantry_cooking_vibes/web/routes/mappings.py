"""Ingredient mapping review UI.

Read/write surface like pantry: list pending queue rows, drill into one,
approve (with optional canonical override) or reject. SQL lives in
``mcp_server.tools``; approve/reject reuse the existing
``importers.normalize`` helpers.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

# `importers.normalize` is imported lazily in approve/reject below to keep rapidfuzz off the web boot path.
from pantry_cooking_vibes.mcp_server import tools
from pantry_cooking_vibes.web.deps import get_db_path, render, url_quote as _q

router = APIRouter(prefix="/mappings")


@router.get("")
def mappings_page(
    request: Request,
    status: str = "proposed",
    source: str = "",
    limit: int = 50,
    offset: int = 0,
    error: str = "",
    approved: str = "",
    rejected: str = "",
    db_path: Path = Depends(get_db_path),
) -> object:
    try:
        data = tools.list_mapping_queue(
            status=status or "all",
            source=source.strip() or None,
            limit=limit,
            offset=offset,
            db_path=db_path,
        )
    except ValueError as e:
        return RedirectResponse(url=f"/mappings?error={_q(str(e))}", status_code=303)

    return render(
        request,
        "mappings/list.html",
        {
            "items": data["items"],
            "total": data["total"],
            "counts": data["counts"],
            "sources": data["sources"],
            "status": status,
            "source": source,
            "limit": limit,
            "offset": offset,
            "error": error,
            "approved": approved,
            "rejected": rejected,
        },
    )


@router.get("/{queue_id}")
def mapping_detail(
    request: Request,
    queue_id: int,
    search: str = "",
    error: str = "",
    db_path: Path = Depends(get_db_path),
) -> object:
    item = tools.get_mapping_queue_item(queue_id, db_path=db_path)
    if item is None:
        raise HTTPException(status_code=404, detail=f"queue id {queue_id} not found")

    suggestions = (
        tools.find_canonical_ingredient(search, limit=20, db_path=db_path) if search.strip() else []
    )
    return render(
        request,
        "mappings/detail.html",
        {
            "item": item,
            "search": search,
            "suggestions": suggestions,
            "error": error,
        },
    )


@router.post("/{queue_id}/approve")
def mapping_approve(
    queue_id: int,
    canonical_id: int | None = Form(None),
    db_path: Path = Depends(get_db_path),
) -> RedirectResponse:
    item = tools.get_mapping_queue_item(queue_id, db_path=db_path)
    if item is None:
        return RedirectResponse(
            url=f"/mappings?error={_q(f'queue id {queue_id} not found')}",
            status_code=303,
        )
    target = canonical_id if canonical_id is not None else item["proposed_canonical_id"]
    if target is None:
        return RedirectResponse(
            url=f"/mappings/{queue_id}?error={_q('pick a canonical ingredient first')}",
            status_code=303,
        )
    from pantry_cooking_vibes.importers.normalize import approve_mapping as _approve_mapping

    _approve_mapping(queue_id, int(target), db_path=db_path)
    return RedirectResponse(
        url=f"/mappings?approved={_q(str(queue_id))}",
        status_code=303,
    )


@router.post("/{queue_id}/reject")
def mapping_reject(
    queue_id: int,
    db_path: Path = Depends(get_db_path),
) -> RedirectResponse:
    item = tools.get_mapping_queue_item(queue_id, db_path=db_path)
    if item is None:
        return RedirectResponse(
            url=f"/mappings?error={_q(f'queue id {queue_id} not found')}",
            status_code=303,
        )
    from pantry_cooking_vibes.importers.normalize import reject_mapping as _reject_mapping

    _reject_mapping(queue_id, db_path=db_path)
    return RedirectResponse(
        url=f"/mappings?rejected={_q(str(queue_id))}",
        status_code=303,
    )
