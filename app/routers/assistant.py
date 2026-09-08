"""In-product assistant. Offline and rule-based by default; swap ``answer()``
in ``help_content.py`` for an LLM call and nothing else changes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..help_content import PAGE_HELP, answer
from ..models import User
from ..security import current_user
from ..web import templates

router = APIRouter(prefix="/assistant")


@router.post("/ask")
def ask(request: Request, question: str = Form(""), context: str = Form(""),
        user: User = Depends(current_user), db: Session = Depends(get_db)):
    reply = answer(question)
    suggestions = [q for q, _ in (PAGE_HELP.get(context, {}) or {}).get("tips", [])][:3]
    return templates.TemplateResponse(request, "partials/assistant_reply.html",
                                      {"request": request, "question": question,
                                       "reply": reply, "suggestions": suggestions})
