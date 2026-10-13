import os, json, time, imaplib, email, requests
from email.message import EmailMessage
from email.utils import parseaddr
from email.header import decode_header
from datetime import datetime, timedelta, timezone
from google.oauth2 import service_account
from googleapiclient.discovery import build
import pytz

KEY = os.getenv("ANTHROPIC_KEY")
EMAIL = os.getenv("GMAIL_ADDRESS")
PWD = os.getenv("GMAIL_PASSWORD")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_ID = os.getenv("TELEGRAM_CHAT_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT")
FORCE_BRIEFING = os.getenv("FORCE_BRIEFING") == "1"

MY_NAME = "Jett Groshinski"
MY_FIRST = MY_NAME.split()[0]
ASSISTANT_NAME = "Jennifer Clarke"
ASSISTANT_FIRST = ASSISTANT_NAME.split()[0]
ASSISTANT_TITLE = "Executive Assistant"
DRAFTS_FOLDER = '"[Gmail]/Drafts"'
SENT_FOLDER = '"[Gmail]/Sent Mail"'
SKIP_SENDERS = ["noreply", "no-reply", "donotreply", "do-not-reply", "notifications", "notify", "mailer-daemon", "railway.app"]
LOOKBACK_DAYS = 3
MAX_PER_RUN = 15
LABELS = {"Urgent": "Urgent", "Needs Reply": "Needs Reply", "FYI": "FYI",
          "Follow-up": "Follow-up", "Course/Admin": "Course-Admin"}

PROCESSED_FILE = "processed_uids.txt"
DAILY_DIGEST_FILE = "daily_digest.json"
NZ_TZ = pytz.timezone('Pacific/Auckland')
sent_cache = {}

ANALYSIS_TOOL = {
    "name": "record_analysis",
    "description": "Record the analysis of the email and the draft reply.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "1-2 sentence summary of the email"},
            "category": {"type": "string", "enum": ["Urgent", "Needs Reply", "FYI", "Follow-up", "Course/Admin"]},
            "needs_reply": {"type": "boolean"},
            "draft_body": {"type": "string", "description": "Full reply text, or empty string if no reply is needed"},
        },
        "required": ["summary", "category", "needs_reply", "draft_body"],
    },
}

def get_calendar_service():
    try:
        creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict, scopes=['https://www.googleapis.com/auth/calendar.readonly'])
        return build('calendar', 'v3', credentials=creds)
    except Exception as e:
        print(f"Calendar service error: {e}")
        return None

def check_calendar_availability():
    try:
        service = get_calendar_service()
        if not service:
            return "Calendar unavailable"
        now = datetime.now(timezone.utc)
        end = now + timedelta(days=3)
        events = service.events().list(
            calendarId='primary', timeMin=now.isoformat(), timeMax=end.isoformat(),
            singleEvents=True, orderBy='startTime').execute().get('items', [])
        if not events:
            return "No meetings in the next 3 days"
        lines = []
        for ev in events[:8]:
            start = ev['start'].get('dateTime', ev['start'].get('date'))
            lines.append(f"{ev.get('summary', 'Busy')} at {start}")
        return "Upcoming: " + "; ".join(lines)
    except Exception as e:
        print(f"Calendar check error: {e}")
        return "Calendar unavailable"

def load_processed():
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE, 'r') as f:
            return set(f.read().strip().split('\n'))
    return set()

def save_processed(key):
    with open(PROCESSED_FILE, 'a') as f:
        f.write(f"{key}\n")

def load_daily_digest():
    if os.path.exists(DAILY_DIGEST_FILE):
        with open(DAILY_DIGEST_FILE, 'r') as f:
            return json.load(f)
    return {"emails": [], "last_sent": None}

def save_daily_digest(digest):
    with open(DAILY_DIGEST_FILE, 'w') as f:
        json.dump(digest, f)

def get_email_body(msg):
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode(errors='ignore')[:1500]
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode(errors='ignore')[:1500]
    return body

def decode_subject(raw):
    try:
        out = ""
        for text, enc in decode_header(raw):
            out += text.decode(enc or "utf-8", errors="ignore") if isinstance(text, bytes) else text
        return out
    except Exception:
        return raw

def get_new_emails():
    try:
        m = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        m.login(EMAIL, PWD)
        m.select("INBOX")
        since = (datetime.now(NZ_TZ) - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")
        _, data = m.uid("search", None, "UNSEEN", "SINCE", since)
        processed = load_processed()
        new_emails = []
        for uid in data[0].split():
            uid_s = uid.decode()
            if f"u{uid_s}" in processed:
                continue
            if len(new_emails) >= MAX_PER_RUN:
                break
            _, d = m.uid("fetch", uid, "(BODY.PEEK[])")  # PEEK keeps the email unread
            msg = email.message_from_bytes(d[0][1])
            new_emails.append({
                'uid': uid_s,
                'from': msg.get("From", "unknown"),
                'reply_to': msg.get("Reply-To") or msg.get("From", ""),
                'subject': decode_subject(msg.get("Subject", "(no subject)")),
                'message_id': msg.get("Message-ID", ""),
                'references': msg.get("References", ""),
                'body': get_email_body(msg),
            })
        m.close()
        m.logout()
        return new_emails
    except Exception as e:
        print(f"Email error: {e}")
        return []

def apply_label(uid, label):
    try:
        m = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        m.login(EMAIL, PWD)
        m.select("INBOX")
        status, _ = m.uid("store", uid, "+X-GM-LABELS", f'("{label}")')
        m.logout()
        print(f"Label '{label}' applied ({status})")
    except Exception as e:
        print(f"Label error: {e}")

def emailed_before(addr):
    """True if there is any email from me to this address in Sent Mail."""
    addr = addr.lower()
    if addr in sent_cache:
        return sent_cache[addr]
    try:
        m = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        m.login(EMAIL, PWD)
        status, _ = m.select(SENT_FOLDER, readonly=True)
        if status != "OK":
            print(f"Sent check failed: could not open {SENT_FOLDER}")
            m.logout()
            return False
        _, data = m.uid("search", None, "TO", f'"{addr}"')
        m.logout()
        result = bool(data[0].split())
        sent_cache[addr] = result
        return result
    except Exception as e:
        print(f"Sent check failed: {e}")
        return False

def parse_json_loose(text):
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found")
    return json.loads(text[start:end + 1], strict=False)

def clean_analysis(data):
    """Make sure every field exists and has the right type."""
    return {
        "summary": str(data.get("summary", "")).strip() or "No summary",
        "category": data.get("category") if data.get("category") in LABELS else "Unanalyzed",
        "needs_reply": bool(data.get("needs_reply", False)),
        "draft_body": str(data.get("draft_body", "") or "").strip(),
    }

def analyze_email(fr, subj, body, tone):
    calendar_status = check_calendar_availability()
    formal = ("Write in a formal, professional register: open with 'Dear [first name],' (or 'Dear Sir/Madam,' if the name is unknown), "
              "use complete, courteous sentences, no contractions, no slang, no exclamation marks, and no emojis. "
              "Keep it concise and clear.")
    if tone == "secretary":
        style = (f"Write draft_body as {ASSISTANT_NAME}, {ASSISTANT_TITLE} to {MY_NAME}, writing on his behalf in the third person about {MY_FIRST}. "
                 f"{formal} "
                 f"Briefly introduce yourself in the first line, e.g. 'I am writing on behalf of {MY_NAME}.' "
                 f"Then reply, e.g. 'Thank you for your email. {MY_FIRST} would be pleased to...'. "
                 f"Always use the name {ASSISTANT_NAME} and never any other assistant name. "
                 f"Sign off with 'Kind regards,' then '{ASSISTANT_NAME}' on one line and '{ASSISTANT_TITLE} to {MY_NAME}' on the next.")
    else:
        style = (f"Write draft_body in the first person as {MY_FIRST}. {formal} "
                 f"Sign off with 'Kind regards,' then '{MY_FIRST}'.")
    prompt = f"""Analyze this email and call the record_analysis tool with your result.

{style}
If a meeting or time is proposed, use the calendar below to accept, decline or suggest alternatives. Never invent facts, commitments or attachments. If you lack information, say you will follow up.
If subject or body contains URGENT, ASAP, EMERGENCY, CRITICAL, IMMEDIATE or HELP NOW, categorize as Urgent.
Newsletters, receipts, automated notifications and marketing never need a reply: set needs_reply to false and draft_body to an empty string.
Set category to "Needs Reply" only when needs_reply is true.

Subject: {subj}
From: {fr}
Body: {body}
Calendar: {calendar_status}"""

    for attempt in (1, 2):
        try:
            r = requests.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key": KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={
                    "model": "claude-haiku-5-5",
                    "max_tokens": 1500,
                    "tools": [ANALYSIS_TOOL],
                    "tool_choice": {"type": "tool", "name": "record_analysis"},
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=45)
            if r.status_code != 200:
                print(f"Claude API error {r.status_code} (attempt {attempt}): {r.text[:200]}")
                time.sleep(2)
                continue

            resp = r.json()
            if resp.get("stop_reason") == "max_tokens":
                print(f"Reply was cut off for length (attempt {attempt})")

            blocks = resp.get("content", [])
            for b in blocks:
                if b.get("type") == "tool_use" and isinstance(b.get("input"), dict):
                    return clean_analysis(b["input"])

            text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            try:
                return clean_analysis(parse_json_loose(text))
            except Exception as pe:
                print(f"PARSE FAILED (attempt {attempt}) ({pe}): {text[:300]}")
        except Exception as e:
            print(f"Claude error (attempt {attempt}): {e}")
            time.sleep(2)

    return {"summary": "Could not analyze this email", "category": "Unanalyzed", "needs_reply": False, "draft_body": ""}

def save_draft(e, body_text):
    try:
        to_addr = parseaddr(e['reply_to'])[1]
        if not to_addr or any(s in to_addr.lower() for s in SKIP_SENDERS):
            print(f"Draft skipped (automated sender): {to_addr}")
            return False
        msg = EmailMessage()
        msg["From"] = EMAIL
        msg["To"] = to_addr
        subj = e['subject']
        msg["Subject"] = subj if subj.lower().startswith("re:") else f"Re: {subj}"
        if e['message_id']:
            msg["In-Reply-To"] = e['message_id']
            msg["References"] = (e['references'] + " " + e['message_id']).strip()
        msg.set_content(body_text)
        m = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        m.login(EMAIL, PWD)
        status, _ = m.append(DRAFTS_FOLDER, '\\Draft', imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        m.logout()
        print(f"Draft saved ({status}) for: {subj}")
        return status == "OK"
    except Exception as ex:
        print(f"Draft error: {ex}")
        return False

def is_urgent(subj, category):
    keywords = ["urgent", "asap", "emergency", "critical", "immediate", "help now", "need help"]
    return any(k in subj.lower() for k in keywords) or category == "Urgent"

def send_telegram(msg):
    chunks = [msg[i:i + 3900] for i in range(0, len(msg), 3900)] or [msg]
    for chunk in chunks:
        try:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_ID, "text": chunk}, timeout=10)
            print("Telegram sent" if r.status_code == 200 else f"Telegram error: {r.status_code} {r.text[:200]}")
        except Exception as ex:
            print(f"Telegram error: {ex}")
        time.sleep(1)

def briefing_due(digest):
    nz_now = datetime.now(NZ_TZ)
    return nz_now.hour >= 8 and digest.get("last_sent") != nz_now.strftime("%Y-%m-%d")

def draft_note(draft_saved, tone):
    if not draft_saved:
        return ""
    voice = f"written by {ASSISTANT_FIRST}, new contact" if tone == "secretary" else "your voice, you've emailed them before"
    return f"✍️ Draft waiting in Gmail Drafts ({voice})"

def send_daily_briefing(digest):
    nz_today = datetime.now(NZ_TZ).strftime("%Y-%m-%d")
    if digest["emails"]:
        msg = "📧 DAILY EMAIL BRIEFING\n\n"
        for idx, e in enumerate(digest["emails"], 1):
            msg += f"{idx}. From: {e['from']}\n   Subject: {e['subject']}\n"
            msg += f"   Summary: {e['analysis'].get('summary', 'N/A')}\n"
            msg += f"   Category: {e['analysis'].get('category', 'N/A')}\n"
            note = draft_note(e.get("draft_saved"), e.get("tone"))
            if note:
                msg += f"   {note}\n"
            msg += "\n"
    else:
        msg = "📧 DAILY EMAIL BRIEFING\n\nNo new non-urgent emails since the last briefing."
    send_telegram(msg)
    digest["emails"] = []
    digest["last_sent"] = nz_today
    save_daily_digest(digest)

print("Email Assistant started")
send_telegram(f"✅ Email assistant is online. {ASSISTANT_FIRST} is checking your inbox every 20 minutes.")

first_loop = True

while True:
    try:
        emails = get_new_emails()
        digest = load_daily_digest()

        if emails:
            print(f"Found {len(emails)} emails")
            for e in emails:
                print(f"Processing: {e['subject']}")
                sender_addr = parseaddr(e['reply_to'])[1]
                tone = "personal" if emailed_before(sender_addr) else "secretary"
                print(f"Tone: {tone} (sender: {sender_addr})")

                analysis = analyze_email(e['from'], e['subject'], e['body'], tone)
                category = analysis.get("category", "Unanalyzed")

                draft_saved = False
                draft_body = (analysis.get("draft_body") or "").strip()
                if analysis.get("needs_reply") and draft_body:
                    draft_saved = save_draft(e, draft_body)

                apply_label(e['uid'], LABELS.get(category, "Unanalyzed"))

                if is_urgent(e['subject'], category):
                    note = draft_note(draft_saved, tone)
                    note = f"\n{note}" if note else ""
                    send_telegram(f"🚨 URGENT EMAIL\nFrom: {e['from']}\nSubject: {e['subject']}\n\nSummary: {analysis.get('summary', 'N/A')}{note}")
                else:
                    digest["emails"].append({"from": e['from'], "subject": e['subject'],
                                             "analysis": analysis, "draft_saved": draft_saved, "tone": tone})

                save_processed(f"u{e['uid']}")
                time.sleep(1)
            save_daily_digest(digest)

        if (FORCE_BRIEFING and first_loop) or briefing_due(digest):
            send_daily_briefing(digest)
        else:
            print(f"Checked at {datetime.now(NZ_TZ)}")

        first_loop = False
        time.sleep(1200)

    except KeyboardInterrupt:
        print("Stopped")
        break
    except Exception as ex:
        print(f"Error: {ex}")
        time.sleep(60)
