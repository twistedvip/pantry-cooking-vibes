"""Import inbox: stage a parsed batch, triage it, then save into the library.

This is the funnel for every import. Pasting URLs or uploading a JSON-LD file
creates a batch (parsed via ``inbox_ingest``); the inbox lists the parsed items
by status so the user can save the good ones and discard the rest. The only
write into ``recipes`` happens when an item is saved (promoted).
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import RedirectResponse

from pantry_cooking_vibes.importers import import_inbox as inbox, inbox_ingest
from pantry_cooking_vibes.web.deps import get_db_path, render, safe_redirect
from pantry_cooking_vibes.web.routes.recipes import _page_window

log = logging.getLogger(__name__)

router = APIRouter(prefix="/imports")

PAGE_SIZE = 50

# Filter pills, in display order. 'all' is the unresolved union; the four
# buckets mirror import_items' triage statuses. Labels stay short so the pills
# don't wrap the toolbar.
_FILTERS: tuple[tuple[str, str], ...] = (
    ("ready", "ready"),
    ("review", "needs review"),
    ("dup", "duplicate"),
    ("failed", "failed"),
    ("all", "all"),
)
_FILTER_VALUES = {f[0] for f in _FILTERS}

_MAX_URLS = 500


def _split_urls(raw: str) -> list[str]:
    """One URL per line or whitespace-separated; blanks dropped."""
    return [u.strip() for u in raw.replace(",", "\n").split() if u.strip()]


# ---------------------------------------------------------------------------
# entry: start a batch / land on the latest
# ---------------------------------------------------------------------------


@router.get("")
def imports_home(request: Request, db_path: Path = Depends(get_db_path)) -> object:
    """Land on the most recent batch, or the empty 'start a batch' state."""
    batch_id = inbox.latest_batch_id(db_path=db_path)
    if batch_id is not None:
        return RedirectResponse(url=f"/imports/{int(batch_id)}", status_code=303)
    return render(request, "imports/empty.html", {"error": None})


@router.get("/new")
def new_batch_form(request: Request) -> object:
    """The start-a-batch form, reachable even when a recent batch exists."""
    return render(request, "imports/empty.html", {"error": None})


@router.post("")
def create_batch(
    request: Request,
    urls: str = Form(""),
    file: UploadFile | None = File(None),
    db_path: Path = Depends(get_db_path),
) -> object:
    """Parse pasted URLs and/or an uploaded JSON-LD file into a new batch.

    Runs synchronously in FastAPI's threadpool: the request returns once parsing
    finishes and redirects to the batch. (Live background progress is a planned
    follow-up; the staging model already supports a 'parsing' state.)
    """
    url_list = _split_urls(urls)
    file_text: str | None = None
    file_name: str | None = None
    if file is not None and file.filename:
        raw = file.file.read()
        file_text = raw.decode("utf-8", errors="replace")
        file_name = file.filename

    if not url_list and file_text is None:
        response = render(
            request,
            "imports/empty.html",
            {"error": "Paste at least one URL, or choose a JSON-LD file to import."},
        )
        response.status_code = 422
        return response
    if len(url_list) > _MAX_URLS:
        response = render(
            request,
            "imports/empty.html",
            {"error": f"That's {len(url_list)} URLs; import {_MAX_URLS} or fewer at a time."},
        )
        response.status_code = 422
        return response

    batch_id = inbox_ingest.build_batch(
        urls=url_list, file_text=file_text, file_name=file_name, db_path=db_path
    )
    return RedirectResponse(url=f"/imports/{int(batch_id)}", status_code=303)


# ---------------------------------------------------------------------------
# the inbox
# ---------------------------------------------------------------------------


@router.get("/{batch_id}")
def batch_inbox(
    request: Request,
    batch_id: int,
    status: str = Query("all"),
    q: str = Query(""),
    page: int = Query(1, ge=1),
    saved: str = Query(""),
    db_path: Path = Depends(get_db_path),
) -> object:
    batch = inbox.get_batch(batch_id, db_path=db_path)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"Import batch {batch_id} not found")

    status_val = status if status in _FILTER_VALUES else "all"
    page_data = inbox.list_items(
        batch_id,
        status=status_val,
        query=q,
        limit=PAGE_SIZE,
        offset=(page - 1) * PAGE_SIZE,
        db_path=db_path,
    )
    total = page_data["total"]
    page_val = page_data["offset"] // PAGE_SIZE + 1
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    counts = page_data["counts"]

    return render(
        request,
        "imports/inbox.html",
        {
            "batch": batch,
            "items": page_data["items"],
            "counts": counts,
            "filters": _FILTERS,
            "status": status_val,
            "q": q,
            "total": total,
            "page": page_val,
            "total_pages": total_pages,
            "page_window": _page_window(page_val, total_pages),
            "page_start": page_data["offset"] + 1,
            "page_end": page_data["offset"] + len(page_data["items"]),
            "saved_flash": saved,
        },
    )


def _redirect_to_batch(batch_id: int, status: str, q: str, *, saved: str = "") -> RedirectResponse:
    """Back to the inbox, preserving the active filter/search (and an optional
    flash). int() + safe_redirect keep this off CodeQL's open-redirect path."""
    params = {"status": status}
    if q:
        params["q"] = q
    if saved:
        params["saved"] = saved
    dest = f"/imports/{int(batch_id)}?{urlencode(params)}"
    return RedirectResponse(url=safe_redirect(dest, f"/imports/{int(batch_id)}"), status_code=303)


@router.post("/{batch_id}/save")
def save_or_discard(
    batch_id: int,
    action: str = Form("save"),
    status: str = Form("all"),
    q: str = Form(""),
    item_ids: list[int] = Form(default_factory=list),
    replace_ids: list[int] = Form(default_factory=list),
    db_path: Path = Depends(get_db_path),
) -> object:
    """Apply a bulk action to the batch. ``action`` is one of:
    ``save`` (the checked items, honoring per-row replace), ``discard`` (the
    checked items), or ``save-all`` (every item matching the current filter)."""
    if inbox.get_batch(batch_id, db_path=db_path) is None:
        raise HTTPException(status_code=404, detail=f"Import batch {batch_id} not found")
    status_val = status if status in _FILTER_VALUES else "all"

    if action == "save-all":
        result = inbox.save_all(batch_id, status=status_val, query=q, db_path=db_path)
        flash = f"Saved {result['saved'] + result['replaced']} recipes."
    elif action == "discard":
        n = inbox.discard_items(item_ids, db_path=db_path)
        flash = f"Discarded {n} item{'' if n == 1 else 's'}."
    else:
        result = inbox.save_items(item_ids, replace_ids=set(replace_ids), db_path=db_path)
        total_saved = result["saved"] + result["replaced"]
        flash = f"Saved {total_saved} recipe{'' if total_saved == 1 else 's'}."

    return _redirect_to_batch(batch_id, status_val, q, saved=flash)


# ---------------------------------------------------------------------------
# fix an item before saving
# ---------------------------------------------------------------------------


def _item_form_values(item: dict) -> dict:
    return {
        "name": item["name"] or "",
        "cooking_time_min": ""
        if item["cooking_time_min"] is None
        else str(item["cooking_time_min"]),
        "servings": "" if item["servings"] is None else str(item["servings"]),
        "image_url": item["image_url"] or "",
        "tags": ", ".join(item["tags"]),
        "ingredients": "\n".join(item["ingredients"]),
        "instructions": item["instructions_md"] or "",
    }


@router.get("/items/{item_id}/edit")
def edit_item_form(
    request: Request,
    item_id: int,
    db_path: Path = Depends(get_db_path),
) -> object:
    item = inbox.get_item(item_id, db_path=db_path)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Import item {item_id} not found")
    return render(
        request,
        "imports/edit_item.html",
        {"item": item, "form": _item_form_values(item), "error": None},
    )


@router.post("/items/{item_id}/edit")
def edit_item_submit(
    request: Request,
    item_id: int,
    name: str = Form(""),
    cooking_time_min: str = Form(""),
    servings: str = Form(""),
    image_url: str = Form(""),
    tags: str = Form(""),
    ingredients: str = Form(""),
    instructions: str = Form(""),
    db_path: Path = Depends(get_db_path),
) -> object:
    item = inbox.get_item(item_id, db_path=db_path)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Import item {item_id} not found")

    error: str | None = None
    time_val = _opt_int(cooking_time_min)
    servings_val = _opt_int(servings)
    try:
        inbox.update_item(
            item_id,
            name=name,
            cooking_time_min=time_val,
            servings=servings_val,
            image_url=image_url or None,
            instructions=instructions.splitlines(),
            tags=tags.split(","),
            ingredients=ingredients.splitlines(),
            db_path=db_path,
        )
    except ValueError as e:
        error = str(e)
    else:
        return RedirectResponse(url=f"/imports/{int(item['batch_id'])}", status_code=303)

    response = render(
        request,
        "imports/edit_item.html",
        {
            "item": item,
            "form": {
                "name": name,
                "cooking_time_min": cooking_time_min,
                "servings": servings,
                "image_url": image_url,
                "tags": tags,
                "ingredients": ingredients,
                "instructions": instructions,
            },
            "error": error,
        },
    )
    response.status_code = 422
    return response


def _opt_int(raw: str) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None
