"""Pantry: the only read-write surface in the web UI."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from pantry_cooking_vibes.mcp_server import tools
from pantry_cooking_vibes.web.deps import get_db_path, render

router = APIRouter(prefix="/pantry")


@router.get("")
def pantry_page(
    request: Request,
    search: str = "",
    error: str = "",
    added: str = "",
    removed: str = "",
    updated: str = "",
    db_path: Path = Depends(get_db_path),
) -> object:
    items = tools.list_pantry(db_path=db_path)
    suggestions = (
        tools.find_canonical_ingredient(search, limit=20, db_path=db_path) if search.strip() else []
    )
    return render(
        request,
        "pantry/list.html",
        {
            "items": items,
            "search": search,
            "suggestions": suggestions,
            "error": error,
            "added": added,
            "removed": removed,
            "updated": updated,
        },
    )


@router.post("/add")
def pantry_add(
    canonical_id: int = Form(...),
    quantity: float = Form(...),
    unit: str = Form(""),
    expires_at: str = Form(""),
    note: str = Form(""),
    db_path: Path = Depends(get_db_path),
) -> RedirectResponse:
    unit_val: str | None = unit.strip() or None
    expires_val: str | None = expires_at.strip() or None
    note_val: str | None = note.strip() or None
    try:
        row = tools.add_pantry_item(
            canonical_id=canonical_id,
            quantity=quantity,
            unit=unit_val,
            expires_at=expires_val,
            note=note_val,
            db_path=db_path,
        )
    except ValueError as e:
        return RedirectResponse(url=f"/pantry?error={_q(str(e))}", status_code=303)
    return RedirectResponse(
        url=f"/pantry?added=item%20{row['id']}",
        status_code=303,
    )


@router.post("/{item_id}/update")
def pantry_update(
    item_id: int,
    quantity: float = Form(...),
    unit: str = Form(""),
    db_path: Path = Depends(get_db_path),
) -> RedirectResponse:
    unit_val: str | None = unit.strip() or None
    try:
        tools.update_pantry_item(
            item_id=item_id,
            quantity=quantity,
            unit=unit_val,
            db_path=db_path,
        )
    except ValueError as e:
        return RedirectResponse(url=f"/pantry?error={_q(str(e))}", status_code=303)
    return RedirectResponse(
        url=f"/pantry?updated=item%20{item_id}",
        status_code=303,
    )


@router.post("/{item_id}/delete")
def pantry_delete(
    item_id: int,
    db_path: Path = Depends(get_db_path),
) -> RedirectResponse:
    result = tools.remove_pantry_item(item_id, db_path=db_path)
    if result["removed"]:
        return RedirectResponse(url=f"/pantry?removed=item%20{item_id}", status_code=303)
    return RedirectResponse(
        url=f"/pantry?error=item%20{item_id}%20not%20found",
        status_code=303,
    )


def _q(s: str) -> str:
    from urllib.parse import quote

    return quote(s, safe="")
