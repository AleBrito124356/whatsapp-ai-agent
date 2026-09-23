"""Staff HTTP API for human handoff and bookings (``/admin``).

Protected by a bearer token: set ``ADMIN_TOKEN`` and send
``Authorization: Bearer <ADMIN_TOKEN>``. Without ``ADMIN_TOKEN`` every endpoint
answers 503, so a fresh deployment never exposes customer data by accident.

    GET  /admin/handoffs                         open queue (+ last messages)
    GET  /admin/conversations/{wa_id}            transcript, state, bookings
    POST /admin/conversations/{wa_id}/reply      {"text": "..."}  -> WhatsApp
    POST /admin/handoffs/{wa_id}/resolve         {"notify": true} -> bot back on
    GET  /admin/bookings?date=YYYY-MM-DD&status= confirmed|cancelled|conflict|all
    POST /admin/bookings/{id}/cancel             {"notify": true}

The rules (24-hour window, opt-out, takeover) live in ``app/staff.py``.
"""

from __future__ import annotations

import hmac
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from .deps import Services
from .staff import StaffDesk, StaffError
from .webhook import get_services

router = APIRouter(prefix="/admin", tags=["admin"])


def staff_desk(
    authorization: Optional[str] = Header(default=None),
    services: Services = Depends(get_services),
) -> StaffDesk:
    settings = services.settings
    if not settings.admin_enabled:
        raise HTTPException(status_code=503, detail="Admin API disabled: set ADMIN_TOKEN to enable it.")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.strip().encode(), settings.admin_token.encode()):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return StaffDesk(services)


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except StaffError as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message})


class ReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=4096)


class NotifyIn(BaseModel):
    notify: bool = True


@router.get("/handoffs")
def list_handoffs(last: int = Query(5, ge=0, le=50), desk: StaffDesk = Depends(staff_desk)) -> dict:
    items = desk.queue(last_messages=last)
    return {"count": len(items), "handoffs": items}


@router.get("/conversations/{wa_id}")
def get_conversation(wa_id: str, limit: int = Query(50, ge=1, le=500), desk: StaffDesk = Depends(staff_desk)) -> dict:
    return _run(desk.conversation, wa_id, limit=limit)


@router.post("/conversations/{wa_id}/reply")
def reply(wa_id: str, body: ReplyIn, desk: StaffDesk = Depends(staff_desk)) -> dict:
    return _run(desk.reply, wa_id, body.text)


@router.post("/handoffs/{wa_id}/resolve")
def resolve(wa_id: str, body: Optional[NotifyIn] = None, desk: StaffDesk = Depends(staff_desk)) -> dict:
    return _run(desk.resolve, wa_id, notify=(body or NotifyIn()).notify)


@router.get("/bookings")
def bookings(
    date: Optional[str] = Query(None, description="YYYY-MM-DD; default: today onwards"),
    status: str = Query("confirmed", pattern="^(confirmed|cancelled|conflict|all)$"),
    desk: StaffDesk = Depends(staff_desk),
) -> dict:
    rows = _run(desk.bookings, date, status=status)
    return {"count": len(rows), "bookings": rows}


@router.post("/bookings/{booking_id}/cancel")
def cancel_booking(booking_id: int, body: Optional[NotifyIn] = None, desk: StaffDesk = Depends(staff_desk)) -> dict:
    return _run(desk.cancel_booking, booking_id, notify=(body or NotifyIn()).notify)
