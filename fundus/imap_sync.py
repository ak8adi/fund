import imaplib
import email
from email.header import decode_header
import logging
import re
from . import db, ai
from .config import get_config

log = logging.getLogger(__name__)

def _decode_str(s):
    if not s:
        return ""
    parts = decode_header(s)
    out = []
    for part, enc in parts:
        if isinstance(part, bytes):
            out.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(str(part))
    return "".join(out)

def _extract_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdispo = str(part.get("Content-Disposition"))
            if ctype == "text/plain" and "attachment" not in cdispo:
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return ""

def sync_seller_emails() -> int:
    cfg = get_config().get("imap", {})
    if not cfg.get("enabled") or not cfg.get("host") or not cfg.get("user") or not cfg.get("password"):
        return 0
    updated = 0
    try:
        mail = imaplib.IMAP4_SSL(cfg["host"], int(cfg.get("port", 993)))
        mail.login(cfg["user"], cfg["password"])
        mail.select("INBOX")
        status, messages = mail.search(None, "(UNSEEN)")
        if status != "OK" or not messages or not messages[0]:
            mail.logout()
            return 0

        all_hits = []
        for s in db.list_searches():
            for h in db.hits_for_search(s["id"]):
                all_hits.append((s["id"], h))

        for num in messages[0].split():
            try:
                res, data = mail.fetch(num, "(RFC822)")
                if res != "OK" or not data or not data[0]:
                    continue
                msg = email.message_from_bytes(data[0][1])
                subject = _decode_str(msg.get("Subject", ""))
                body = _extract_body(msg).strip()
                combined = f"{subject}\n{body}".lower()

                matched = None
                for sid, h in all_hits:
                    title_clean = (h.get("title") or "").strip().lower()
                    url_id = re.findall(r"\d{7,}", h.get("url") or "")
                    if (title_clean and len(title_clean) > 6 and title_clean in combined) or any(uid in combined for uid in url_id):
                        matched = (sid, h)
                        break

                if matched:
                    sid, h = matched
                    prev = h.get("seller_chat") or ""
                    clean_body = body[:1500]
                    new_chat = (prev + "\n\n[E-Mail-Antwort]: " + clean_body).strip()
                    db.update_listing_chat(h["key"], new_chat)
                    ai.reevaluate_single_listing(h["key"], sid)
                    updated += 1
            except Exception as e:
                log.warning("IMAP Einzelfehler: %s", e)
        mail.logout()
    except Exception as exc:
        log.warning("IMAP Sync Fehler: %s", exc)
    return updated
