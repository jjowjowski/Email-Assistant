import os, json, time, imaplib, email, requests
from email.header import decode_header
from datetime import datetime, timedelta

KEY = os.getenv("ANTHROPIC_KEY")
EMAIL = os.getenv("GMAIL_ADDRESS")
PWD = os.getenv("GMAIL_PASSWORD")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_ID = os.getenv("TELEGRAM_CHAT_ID")

PROCESSED_FILE = "processed_uids.txt"

def load_processed_uids():
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE, 'r') as f:
            return set(f.read().strip().split('\n'))
    return set()

def save_processed_uid(uid):
    with open(PROCESSED_FILE, 'a') as f:
        f.write(f"{uid}\n")

def get_email_body(msg):
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode(errors='ignore')[:500]
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode(errors='ignore')[:500]
    return body

def get_new_emails():
    try:
        m = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        m.login(EMAIL, PWD)
        m.select("INBOX")
        _, msgs = m.search(None, "UNSEEN")
        
        processed = load_processed_uids()
        new_emails = []
        
        for eid in msgs[0].split():
            eid_str = eid.decode()
            if eid_str not in processed:
                _, d = m.fetch(eid, "(RFC822)")
                msg = email.message_from_bytes(d[0][1])
                subj = msg.get("Subject", "?")
                fr = msg.get("From", "?")
                body = get_email_body(msg)
                new_emails.append({'uid': eid_str, 'from': fr, 'subject': subj, 'body': body})
        
        m.close()
        m.logout()
        return new_emails
    except Exception as e:
        print(f"Email error: {e}")
        return []

def summarize_with_claude(fr, subj, body):
    try:
        prompt = f"Summarize this email in 1 sentence. From: {fr}, Subject: {subj}, Body: {body}"
        r = requests.post("https://api.anthropic.com/v1/messages", 
            headers={"x-api-key": KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}, 
            json={"model": "claude-haiku-5-5", "max_tokens": 100, "messages": [{"role": "user", "content": prompt}]},
            timeout=30)
        if r.status_code == 200:
            return r.json()["content"][0]["text"]
        return "Summary error"
    except Exception as e:
        print(f"Claude error: {e}")
        return "Claude error"

def send_telegram(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_ID, "text": msg})
        print("Telegram sent")
    except Exception as e:
        print(f"Telegram error: {e}")

print("Email assistant started")

while True:
    try:
        emails = get_new_emails()
        if emails:
            print(f"Found {len(emails)} emails")
            for e in emails:
                print(f"Processing: {e['subject']}")
                summary = summarize_with_claude(e['from'], e['subject'], e['body'])
                msg = f"<b>Email from {e['from']}</b>\n<b>Subject:</b> {e['subject']}\n<b>Summary:</b> {summary}"
                send_telegram(msg)
                save_processed_uid(e['uid'])
                time.sleep(1)
        else:
            print(f"No emails at {datetime.now()}")
        
        print("Waiting 20 minutes...")
        time.sleep(1200)
    except KeyboardInterrupt:
        print("Stopped")
        break
    except Exception as e:
        print(f"Error: {e}")
        time.sleep(60)
