"""Uploading and downloading the documents on an auction.

A buyer attaches drawings, specifications and terms for the bidders. A bidder
attaches their own paperwork, and only the buyer ever sees it.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import config, documents
from ..audit import record
from ..db import get_db
from ..errors import ActionError
from ..models import Attachment, Auction, AuctionStatus, Item, Participant, User
from ..security import current_user
from ..web import client_ip, redirect

router = APIRouter(prefix="/auctions")

#: A bidder may only add or remove their own paperwork while there is still an
#: auction to add it to.
OPEN_TO_UPLOAD = (AuctionStatus.SCHEDULED, AuctionStatus.LIVE, AuctionStatus.CLOSED)


def _auction_for(db: Session, auction_id: int, user: User) -> Auction:
    auction = db.get(Auction, auction_id)
    if not auction or auction.org_id != user.org_id:
        raise HTTPException(404, "That auction does not exist.")
    if user.is_buyer_side:
        return auction
    part = db.query(Participant).filter_by(auction_id=auction.id,
                                           vendor_id=user.vendor_id).first()
    if not part or auction.status == AuctionStatus.DRAFT:
        raise HTTPException(403, "This auction is not open to you.")
    return auction


def may_download(doc: Attachment, user: User) -> bool:
    if user.is_buyer_side:
        return True
    if doc.audience == "bidders" and doc.vendor_id is None:
        return True
    return doc.vendor_id is not None and doc.vendor_id == user.vendor_id


@router.post("/{auction_id}/documents")
async def upload(auction_id: int, request: Request,
                 files: list[UploadFile] = File(default=[]),
                 item_id: str = Form(""), note: str = Form(""),
                 user: User = Depends(current_user), db: Session = Depends(get_db)):
    auction = _auction_for(db, auction_id, user)
    back = f"/auctions/{auction_id}?tab=documents"
    if not user.is_buyer_side and auction.status not in OPEN_TO_UPLOAD:
        return redirect(back, "This auction is finished, so documents can no longer be added.",
                        kind="error")

    picked = [f for f in files if f and f.filename]
    if not picked:
        return redirect(back, "Choose a file to attach first.", kind="error")

    existing = db.query(Attachment).filter(Attachment.auction_id == auction.id).count()
    if existing + len(picked) > config.MAX_ATTACHMENTS:
        return redirect(back, f"That would be more than {config.MAX_ATTACHMENTS} documents on "
                              "one auction. Remove something first, or send a ZIP.", kind="error")

    line_item = None
    if item_id.strip():
        if not item_id.strip().isdigit():
            return redirect(back, "That item was not one of the choices on the page.",
                            kind="error")
        line_item = db.get(Item, int(item_id))
        if not line_item or not any(line.item_id == line_item.id for line in auction.lines):
            return redirect(back, "That item is not on this auction.", kind="error")

    saved: list[str] = []
    written: list[str] = []
    try:
        for upload_file in picked:
            data = await upload_file.read()
            display, stored, content_type, size = documents.save(
                auction.id, upload_file.filename, data)
            written.append(stored)
            doc = Attachment(
                auction_id=auction.id,
                item_id=line_item.id if line_item else None,
                vendor_id=None if user.is_buyer_side else user.vendor_id,
                audience="bidders" if user.is_buyer_side else "buyer",
                filename=display, stored_name=stored, content_type=content_type,
                size_bytes=size, note=note.strip()[:300], uploaded_by_id=user.id)
            db.add(doc)
            saved.append(display)
    except ActionError as exc:
        # One bad file rejects the whole batch, so nothing is half-attached.
        # The rows are rolled back; take the files back off the disk too.
        db.rollback()
        for stored in written:
            documents.delete_file(auction.id, stored)
        return redirect(back, str(exc), kind="error")

    record(db, action="document.add", entity_type="auction", entity_id=auction.id,
           actor=user, auction_id=auction.id, ip=client_ip(request),
           detail={"files": saved, "for_item": line_item.name if line_item else None,
                   "audience": "bidders" if user.is_buyer_side else "buyer"})
    db.commit()
    who = ("Bidders can download " if user.is_buyer_side
           else "Only the buyer can see ")
    return redirect(back, f"Attached {len(saved)} document(s). {who}"
                          f"{'them' if len(saved) > 1 else 'it'}.")


@router.get("/{auction_id}/documents/{doc_id}")
def download(auction_id: int, doc_id: int, user: User = Depends(current_user),
             db: Session = Depends(get_db)):
    auction = _auction_for(db, auction_id, user)
    doc = db.get(Attachment, doc_id)
    if not doc or doc.auction_id != auction.id:
        raise HTTPException(404, "That document is not on this auction.")
    if not may_download(doc, user):
        raise HTTPException(403, "That document is not shared with you.")
    path = documents.path_for(auction.id, doc.stored_name)
    if not path.is_file():
        raise HTTPException(404, "That document is no longer on the server.")
    return FileResponse(path, media_type=doc.content_type, filename=doc.filename,
                        headers=documents.download_headers(doc.filename, doc.content_type))


@router.post("/{auction_id}/documents/{doc_id}/remove")
def remove(auction_id: int, doc_id: int, request: Request,
           user: User = Depends(current_user), db: Session = Depends(get_db)):
    auction = _auction_for(db, auction_id, user)
    back = f"/auctions/{auction_id}?tab=documents"
    doc = db.get(Attachment, doc_id)
    if not doc or doc.auction_id != auction.id:
        raise HTTPException(404, "That document is not on this auction.")
    # A buyer looks after the auction's documents; a supplier looks after
    # their own, and only while the auction is still running.
    if user.is_buyer_side:
        allowed = True
    else:
        allowed = (doc.vendor_id == user.vendor_id
                   and auction.status in OPEN_TO_UPLOAD)
    if not allowed:
        return redirect(back, "You can only remove your own documents, and only while the "
                              "auction is still open.", kind="error")
    name = doc.filename
    documents.delete_file(auction.id, doc.stored_name)
    db.delete(doc)
    record(db, action="document.remove", entity_type="auction", entity_id=auction.id,
           actor=user, auction_id=auction.id, ip=client_ip(request), detail={"file": name})
    db.commit()
    return redirect(back, f"“{name}” removed.")
