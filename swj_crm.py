#!/usr/bin/env python3
"""
swj_crm.py: the SWJ Search Team CRM (Supabase) for Claude's skills. Python 3, standard library only.

Skills call it from Bash and read the JSON it prints. Field names on the way in and out are the
camelCase names the skills have always used (nextDate, estFee, candConvId); the script maps them to
the snake_case columns, keeps any field with no column in the row's "extra" json, and brings extra
back out flat on reads. Every row read carries "_version" (its updated_at); pass it back as
ifVersion on update or delete so a concurrent change is caught instead of overwritten.

Credentials come from the environment only, never from arguments or files in a repo:
  SWJ_CRM_EMAIL, SWJ_CRM_PASSWORD   the "skills" team login
  SWJ_CRM_URL, SWJ_CRM_KEY          optional; default to the public project URL and publishable key

Commands (all print JSON to stdout; exit 0 ok, 1 error, 2 a batch with at least one failed write):
  swj_crm.py whoami
  swj_crm.py schema [collection]
  swj_crm.py get <collection> <id> [<id> ...]              not found: {"id":..., "found": false}
  swj_crm.py query <collection> [--where '[["status","in",["Drafted","Awaiting reply"]], ["owner","eq","Steven"]]']
                   [--order nextDate | --order -createdAt] [--limit N] [--select f1,f2] [--out FILE]
  swj_crm.py list <collection> [--limit N] [--out FILE]      whole table (clients, team); paged
  swj_crm.py set <collection> <id> --data '{...}'           create only; fails if the id exists
  swj_crm.py update <collection> <id> --data '{...}' [--if-version V]   patch; fails if missing or changed
  swj_crm.py upsert <collection> <id> --data '{...}'        create or merge (runs docs, control flags)
  swj_crm.py delete <collection> <id> [--if-version V]
  swj_crm.py batch <file.json>      [{"op":"set|update|upsert|delete","collection":..,"id":..,"data":{..},"ifVersion":..}, ...]
  swj_crm.py today | workday is <YYYY-MM-DD> | workday add <YYYY-MM-DD> <n>   England and Wales working days

Where operators: eq ne in not-in lt lte gt gte like ilike contains (text[] has the value) is-null not-null.
A field with no column is queried inside extra. Collections: the camelCase artifact names
(contacts, leads, roles, pipeline, candidates, clients, clientNotes, clientJobs, interviews, events,
signalContacts, todos, runs, archive, teamMembers). "runs/bd-2026-11-09" is accepted as collection runs, id bd-2026-11-09.
runs rows are free form: get returns the stored object flat (plus id and _version); update merges into it.
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

DEFAULT_URL = "https://uzizakzwcisicqwbbgtq.supabase.co"
DEFAULT_KEY = "sb_publishable_VH4czeqWxAXl5RwtvVvxzw__3LnkdK4"  # public by design, same as the web app
PAGE = 1000
LONDON = ZoneInfo("Europe/London")

# ---------------------------------------------------------------------------------------------
# Schema: columns per table (db-001 + db-002 + db-003). t text, d date, ts timestamptz, n numeric,
# i int, f float, b bool, j jsonb, a text[]. Everything else a skill writes goes into "extra".
# ---------------------------------------------------------------------------------------------
TABLES = {
    "clients": {"id": "t", "name": "t", "aliases": "a", "status": "t", "owner": "t", "sector": "t", "size": "t",
                "website": "t", "careers_url": "t", "address": "t", "fee_pct": "n", "payment_terms": "t",
                "terms_signed": "t", "invoice_contact": "t", "apollo_org_id": "t", "decision_makers": "j",
                "account_notes": "t", "watch": "t", "watch_note": "t", "jobs_note": "t", "last_watched_at": "ts",
                "extra": "j", "created_at": "ts", "updated_at": "ts"},
    "client_notes": {"id": "t", "client_id": "t", "note_date": "d", "text": "t", "by": "t", "created_at": "ts"},
    "client_jobs": {"id": "t", "client_id": "t", "title": "t", "town": "t", "url": "t", "status": "t", "sources": "a",
                    "note": "t", "first_seen": "d", "last_seen": "d", "drafted_at": "d", "extra": "j", "updated_at": "ts"},
    "roles": {"id": "t", "client_id": "t", "client": "t", "role": "t", "location": "t", "salary": "n", "salary_text": "t",
              "fee_pct": "n", "fee": "n", "stage": "t", "stage_at": "d", "owner": "t", "track": "t", "received_at": "d",
              "key_date": "t", "next_step": "t", "notes": "t", "spec": "t", "spec_file": "t", "scorecard": "t",
              "sheet_tab": "t", "from_contact": "t", "email_hold": "t", "extra": "j", "created_at": "ts", "updated_at": "ts"},
    "interviews": {"id": "t", "role_id": "t", "candidate": "t", "iv_date": "d", "iv_time": "t", "outcome": "t",
                   "location": "t", "interviewer": "t", "feedback_at": "d", "extra": "j", "updated_at": "ts"},
    "leads": {"id": "t", "company": "t", "role": "t", "location": "t", "salary": "n", "salary_text": "t", "advert_url": "t",
              "source": "t", "status": "t", "track": "t", "contact_id": "t", "notes": "t", "added_at": "d", "extra": "j",
              "updated_at": "ts"},
    "contacts": {"id": "t", "type": "t", "company": "t", "contact": "t", "title": "t", "email": "t", "phone": "t",
                 "linkedin": "t", "town": "t", "sector": "t", "size": "t", "role": "t", "salary": "n", "advert_url": "t",
                 "track": "t", "owner": "t", "status": "t", "step": "i", "contact_no": "i", "last_sent": "d",
                 "next_action": "t", "next_date": "d", "est_fee": "n", "est_fee_basis": "t", "lead_id": "t",
                 "apollo_org_id": "t", "source": "t", "draft_link": "t", "drafted_at": "d", "sent_message_id": "t",
                 "conversation_id": "t", "replied_at_step": "t", "call_date": "d", "called_at": "ts",
                 "advert_checked_at": "d", "needs_steven": "b", "do_not_contact": "b", "do_not_contact_at": "d",
                 "do_not_contact_reason": "t", "fee_pct": "n", "notes": "t", "extra": "j", "created_at": "ts",
                 "updated_at": "ts"},
    "signal_contacts": {"id": "t", "company": "t", "contact": "t", "title": "t", "email": "t", "linkedin": "t",
                        "town": "t", "signal": "t", "source_url": "t", "status": "t", "lead_id": "t", "apollo_org_id": "t",
                        "draft_link": "t", "last_sent": "d", "next_date": "d", "follow_up_sent": "b", "extra": "j",
                        "created_at": "ts", "updated_at": "ts"},
    "events": {"id": "t", "type": "t", "contact_id": "t", "owner": "t", "step": "i", "status": "t", "event_date": "d",
               "at": "ts", "extra": "j"},
    "candidates": {"id": "t", "name": "t", "town": "t", "postcode_area": "t", "region": "t", "geo": "t", "lat": "f",
                   "lng": "f", "commute_mins": "i", "family": "t", "current_title": "t", "current_employer": "t",
                   "tenure": "t", "target_titles": "a", "skills": "a", "best_score": "i", "best_role": "t", "scores": "j",
                   "salary_expect": "t", "notice": "t", "status": "t", "status_at": "d", "owner": "t", "cv_ref": "t",
                   "cv_updated": "t", "source_tab": "t", "spec_in_ok_at": "d", "ok_to_market": "b", "ok_source": "t",
                   "spec_ins": "j", "review_by": "d", "last_scored_at": "d", "notes": "t", "extra": "j", "added_at": "d",
                   "updated_at": "ts"},
    "pipeline": {"id": "t", "candidate": "t", "role_id": "t", "client": "t", "role": "t", "town": "t", "stage": "t",
                 "stage_at": "d", "next_action": "t", "next_date": "d", "owner": "t", "source": "t", "channel": "t",
                 "score": "i", "salary_expect": "t", "notice": "t", "interview_date": "d", "interview_time": "t",
                 "start_date": "d", "cand_first": "t", "cand_email": "t", "cand_email_at": "d", "cand_tab": "t",
                 "cand_conv_id": "t", "cand_draft_id": "t", "sam_cand": "t", "sam_cand_at": "d", "needs_steven": "b",
                 "history": "j", "notes": "t", "added_by": "t", "extra": "j", "created_at": "ts", "updated_at": "ts"},
    "todos": {"id": "t", "owner": "t", "text": "t", "priority": "t", "done": "b", "todo_date": "d", "done_at": "ts",
              "link": "t", "source": "t", "added_by": "t", "created_at": "ts", "updated_at": "ts"},
    "runs": {"id": "t", "data": "j", "updated_at": "ts"},
    "archive": {"id": "t", "kind": "t", "rows": "j", "row_count": "i", "created_at": "ts"},
    "team_members": {"user_id": "t", "name": "t", "email": "t", "role": "t", "active": "b", "created_at": "ts"},
}
# camelCase collection name -> table
COLLECTIONS = {"clients": "clients", "clientNotes": "client_notes", "clientJobs": "client_jobs", "roles": "roles",
               "interviews": "interviews", "leads": "leads", "contacts": "contacts", "signalContacts": "signal_contacts",
               "events": "events", "candidates": "candidates", "pipeline": "pipeline", "todos": "todos", "runs": "runs",
               "archive": "archive", "teamMembers": "team_members"}
# fields whose column name is not the plain snake_case of the skill's name
RENAME = {
    "client_notes": {"date": "note_date", "at": "created_at"},
    "interviews": {"date": "iv_date", "time": "iv_time"},
    "events": {"date": "event_date"},
    "todos": {"date": "todo_date"},
    "candidates": {"okAt": "spec_in_ok_at"},
}
READ_ONLY = {"updated_at", "row_count"}
NOT_NULL_DEFAULTED = {  # left out of an insert when blank so the database default applies
    "clients": {"aliases", "status", "decision_makers", "extra", "created_at"},
    "client_jobs": {"sources", "extra"}, "roles": {"stage", "extra", "created_at"}, "interviews": {"extra", "outcome"},
    "leads": {"status", "extra"},
    "contacts": {"type", "status", "step", "contact_no", "needs_steven", "do_not_contact", "extra", "created_at"},
    "signal_contacts": {"follow_up_sent", "extra", "created_at"}, "events": {"at", "extra"},
    "candidates": {"target_titles", "skills", "scores", "status", "spec_ins", "extra"},
    "pipeline": {"stage", "needs_steven", "history", "extra", "created_at"},
    "todos": {"priority", "done", "todo_date", "created_at"}, "runs": {"data"}, "archive": {"rows", "created_at"},
}

ENUMS = {  # mirrors config.js in the web app and the skills
    "contacts.status": ["Drafted", "Awaiting reply", "Replied", "Interested", "Won", "Filled", "No agencies", "Wrong person", "Bounced", "Closed"],
    "contacts.active": ["Drafted", "Awaiting reply", "Replied", "Interested", "Wrong person"],
    "contacts.type": ["BD", "Spec in"], "track": ["Engineering", "Office"],
    "roles.stage": ["Brief taken", "Sourcing", "CVs sent", "Interviewing", "Offer", "Placed", "Invoiced", "Paid", "Lost"],
    "interviews.outcome": ["Booked", "Attended", "2nd stage", "Offer", "Rejected", "Withdrawn"],
    "leads.status": ["New", "Contact found", "Drafted", "Skipped", "Signal", "Review"],
    "candidates.status": ["Available", "In process", "Placed", "Not looking", "Do not contact"],
    "candidates.family": ["Maintenance and engineering", "Production", "Quality", "Supply chain and logistics", "Senior operations",
                          "Sales and commercial", "Marketing and content", "Finance and accounts", "HR and people", "PA and office",
                          "Care and nursing", "Catering", "Other"],
    "pipeline.stage": ["Sourced", "Approached", "Interested", "Qualified", "CV sent", "Interview 1", "Interview 2", "Offer", "Accepted",
                       "Started", "Rebate clear", "Rejected", "Withdrawn", "Not interested"],
    "pipeline.candEmail": ["Queued", "Drafted", "Sent", "Follow up drafted", "Follow up sent", "Replied", "Bounced", "Closed"],
    "pipeline.samCand": ["Sent", "Replied", "Bounced"],
    "signalContacts.status": ["Drafted", "Awaiting reply", "Replied", "Interested", "Not now", "Closed"],
    "clientJobs.status": ["New", "Drafted", "Sent", "Ignored", "On books"], "clients.status": ["Active", "Dormant", "Ended"],
    "todos.priority": ["High", "Normal", "Low"], "owners": ["Steven", "Sam", "Oscar"],
}
ID_RULES = {
    "general": "lowercase slug, accents stripped, every run of characters other than letters and digits one hyphen, ends trimmed",
    "contacts": "company-firstname-lastname", "signalContacts": "company-firstname-lastname", "leads": "company-role (signals: company-signal-YYYY-MM-DD)",
    "roles": "client-role", "pipeline": "candidate full name, hyphen, roleId", "candidates": "name-town",
    "clientNotes": "client-YYYY-MM-DD-watch or -chase", "clientJobs": "client-role", "events": "leave blank, the database assigns one",
    "todos": "YYYY-MM-DD-owner-slugged text (40 chars max)", "runs": "bd-YYYY-MM-DD, sam-, cand-, specin-, signals-, screen-roleId-YYYY-MM-DD, md-update-, control, sync-last, cand-last, md-update-last",
    "archive": "archive-YYYY-MM-DD-n",
}
BANK_HOLIDAYS = {"2026-12-25", "2026-12-28", "2027-01-01", "2027-03-26", "2027-03-29", "2027-05-03", "2027-05-31", "2027-08-30",
                 "2027-12-27", "2027-12-28"}

# ---------------------------------------------------------------------------------------------
# Name mapping
# ---------------------------------------------------------------------------------------------
def snake(name):
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()


def camel(col):
    parts = col.split("_")
    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])


def table_for(collection):
    if "/" in collection:
        collection = collection.split("/", 1)[0]
    if collection in COLLECTIONS:
        return COLLECTIONS[collection]
    if collection in TABLES:
        return collection
    raise SystemExit(err(f"unknown collection {collection!r}; one of {', '.join(sorted(COLLECTIONS))}"))


def split_ref(collection, doc_id):
    """'runs/bd-2026-11-09' with no id -> ('runs', 'bd-2026-11-09')."""
    if "/" in collection and not doc_id:
        c, i = collection.split("/", 1)
        return c, i
    return collection, doc_id


def column_for(table, field):
    r = RENAME.get(table, {})
    if field in r:
        return r[field]
    col = snake(field)
    return col if col in TABLES[table] else None


def blank(v):
    return v is None or (isinstance(v, str) and v.strip() == "")


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$")


class Coerce(Exception):
    pass


def coerce(kind, v):
    if blank(v):
        return None
    if kind == "t":
        return v if isinstance(v, str) else json.dumps(v) if isinstance(v, (dict, list)) else str(v)
    if kind == "d":
        s = str(v).strip()
        if DATE_RE.match(s):
            return s
        if TS_RE.match(s):
            return s[:10]
        raise Coerce(f"not a date: {v!r}")
    if kind == "ts":
        s = str(v).strip()
        if TS_RE.match(s):
            return s
        raise Coerce(f"not a timestamp: {v!r}")
    if kind in ("n", "f"):
        if isinstance(v, bool):
            raise Coerce(f"not a number: {v!r}")
        if isinstance(v, (int, float)):
            return v
        try:
            return float(str(v).replace(",", "").replace("£", "").strip())
        except ValueError:
            raise Coerce(f"not a number: {v!r}")
    if kind == "i":
        if isinstance(v, bool):
            raise Coerce(f"not an integer: {v!r}")
        if isinstance(v, int):
            return v
        if isinstance(v, float) and v.is_integer():
            return int(v)
        if isinstance(v, str) and re.fullmatch(r"-?\d+", v.strip()):
            return int(v.strip())
        raise Coerce(f"not an integer: {v!r}")
    if kind == "b":
        if isinstance(v, bool):
            return v
        if isinstance(v, str) and v.strip().lower() in ("true", "yes", "1"):
            return True
        if isinstance(v, str) and v.strip().lower() in ("false", "no", "0"):
            return False
        raise Coerce(f"not true/false: {v!r}")
    if kind == "a":
        if isinstance(v, list):
            return [x if isinstance(x, str) else json.dumps(x) for x in v if not blank(x)]
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        raise Coerce(f"not a list: {v!r}")
    if kind == "j":
        return v
    raise Coerce(f"unknown type {kind}")


def money(v):
    """salary in roles and leads: number, 0 for unknown, or text such as 'Negotiable' -> (number, text)."""
    if blank(v):
        return None, None
    if isinstance(v, bool):
        return None, str(v)
    if isinstance(v, (int, float)):
        return (v if v > 0 else None), None
    s = str(v).strip()
    if re.fullmatch(r"£?\s*\d[\d,]*(\.\d+)?", s):
        n = float(s.replace("£", "").replace(",", ""))
        return (n if n > 0 else None), None
    return None, s


def to_row(table, doc, existing_extra=None, for_insert=False):
    """camelCase document -> table row. Unknown fields merge into extra. Blank known fields become null."""
    cols = TABLES[table]
    row, extra = {}, dict(existing_extra or {})
    src = {k: v for k, v in doc.items() if k not in ("id", "_version", "_table")}
    if table == "runs":
        return {"data": src}
    if table in ("roles", "leads") and "salary" in src:
        num, text = money(src.pop("salary"))
        row["salary"] = num
        given_text = src.pop("salaryText", None)
        row["salary_text"] = given_text if not blank(given_text) else text
    if table == "contacts" and "salary" in src:
        num, text = money(src.pop("salary"))
        row["salary"] = num
        if text:
            extra["salary"] = text
    if table in ("candidates", "pipeline") and "salaryExpect" in src:
        v = src.pop("salaryExpect")
        row["salary_expect"] = None if blank(v) or v == 0 else (str(int(v)) if isinstance(v, float) and v.is_integer() else str(v))
    for key, val in src.items():
        col = column_for(table, key)
        if col in READ_ONLY:
            continue
        if col and col != "extra":
            try:
                row[col] = coerce(cols[col], val)
            except Coerce as e:
                raise SystemExit(err(f"{table}.{key}: {e}"))
        elif key == "extra" and isinstance(val, dict):
            extra.update(val)
        else:
            if blank(val) and key in extra:
                extra.pop(key)
            elif not blank(val):
                extra[key] = val
    if "extra" in cols and (extra or existing_extra is not None or for_insert):
        row["extra"] = extra
    if for_insert:
        for col in NOT_NULL_DEFAULTED.get(table, ()):
            if row.get(col) is None:
                row.pop(col, None)
    return row


def from_row(table, row):
    """table row -> camelCase document with extra flattened and _version set."""
    if table == "runs":
        doc = dict(row.get("data") or {})
        doc["id"] = row["id"]
        doc["_version"] = row.get("updated_at")
        return doc
    rev = {v: k for k, v in RENAME.get(table, {}).items()}
    doc = {}
    for col, val in row.items():
        if col == "extra":
            continue
        if col == "updated_at":
            doc["_version"] = val
            continue
        name = rev.get(col, camel(col))
        doc[name] = val
    for k, v in (row.get("extra") or {}).items():
        doc.setdefault(k, v)
    return doc


# ---------------------------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------------------------
def err(msg, **more):
    return json.dumps({"error": msg, **more})


class Client:
    def __init__(self):
        self.url = os.environ.get("SWJ_CRM_URL", DEFAULT_URL).rstrip("/")
        self.key = os.environ.get("SWJ_CRM_KEY", DEFAULT_KEY)
        self.email = os.environ.get("SWJ_CRM_EMAIL") or os.environ.get("SUPABASE_EMAIL")
        self.password = os.environ.get("SWJ_CRM_PASSWORD") or os.environ.get("SUPABASE_PASSWORD")
        self.token_file = os.environ.get("SWJ_CRM_TOKEN_FILE") or os.path.join(os.environ.get("TMPDIR", "/tmp"), ".swj_crm_token.json")
        self.token = None

    def _request(self, method, path, body=None, headers=None, auth=True):
        h = {"apikey": self.key, "Content-Type": "application/json", "Accept": "application/json"}
        if auth:
            h["Authorization"] = "Bearer " + self.jwt()
        h.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=h)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    raw = r.read().decode()
                    return r.status, (json.loads(raw) if raw.strip() else None), dict(r.headers)
            except urllib.error.HTTPError as e:
                raw = e.read().decode(errors="replace")
                try:
                    payload = json.loads(raw)
                except ValueError:
                    payload = {"message": raw[:500]}
                if e.code in (502, 503, 504) and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                return e.code, payload, dict(e.headers)
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise SystemExit(err(f"cannot reach the CRM: {e}"))

    def jwt(self):
        if self.token:
            return self.token
        try:
            with open(self.token_file) as f:
                cached = json.load(f)
            if cached.get("url") == self.url and cached.get("email") == self.email and cached.get("exp", 0) - time.time() > 120:
                self.token = cached["token"]
                return self.token
        except (OSError, ValueError):
            pass
        if not self.email or not self.password:
            raise SystemExit(err("no CRM credentials: set SWJ_CRM_EMAIL and SWJ_CRM_PASSWORD in the environment"))
        status, payload, _ = self._request("POST", "/auth/v1/token?grant_type=password",
                                           {"email": self.email, "password": self.password}, auth=False)
        if status != 200 or not payload or "access_token" not in payload:
            raise SystemExit(err("CRM sign in failed", status=status, detail=(payload or {}).get("error_description") or (payload or {}).get("msg") or payload))
        self.token = payload["access_token"]
        try:
            fd = os.open(self.token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"url": self.url, "email": self.email, "token": self.token,
                           "exp": time.time() + int(payload.get("expires_in", 3600))}, f)
        except OSError:
            pass
        return self.token

    def rest(self, method, table, params=None, body=None, prefer=None):
        qs = ("?" + urllib.parse.urlencode(params, doseq=True)) if params else ""
        headers = {}
        if prefer:
            headers["Prefer"] = prefer
        status, payload, hdrs = self._request(method, f"/rest/v1/{table}{qs}", body, headers)
        return status, payload, hdrs


# ---------------------------------------------------------------------------------------------
# Query building
# ---------------------------------------------------------------------------------------------
OPS = {"eq": "eq", "==": "eq", "ne": "neq", "!=": "neq", "neq": "neq", "lt": "lt", "<": "lt", "lte": "lte", "<=": "lte",
       "gt": "gt", ">": "gt", "gte": "gte", ">=": "gte", "like": "like", "ilike": "ilike", "in": "in", "not-in": "not.in",
       "contains": "cs", "array-contains": "cs", "is-null": "is", "not-null": "not.is"}


def pg_literal(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    s = str(v)
    if re.search(r"[,()\"\s]", s) or s == "":
        return '"' + s.replace('"', '\\"') + '"'
    return s


def where_params(table, where):
    params = []
    for clause in where or []:
        if not (isinstance(clause, list) and len(clause) in (2, 3)):
            raise SystemExit(err(f"bad where clause {clause!r}; use [field, op, value]"))
        field, op = clause[0], clause[1]
        val = clause[2] if len(clause) == 3 else None
        pg = OPS.get(op)
        if not pg:
            raise SystemExit(err(f"unknown operator {op!r}"))
        col = column_for(table, field)
        if table == "runs":
            col = col or f"data->>{field}"
        key = col if col else f"extra->>{field}"
        if pg in ("in", "not.in"):
            if not isinstance(val, list):
                raise SystemExit(err(f"{field} {op} needs a list"))
            params.append((key, f"{pg}.({','.join(pg_literal(x) for x in val)})"))
        elif pg == "cs":
            vals = val if isinstance(val, list) else [val]
            params.append((key, "cs.{" + ",".join(pg_literal(x) for x in vals) + "}"))
        elif pg in ("is", "not.is"):
            params.append((key, f"{pg}.null"))
        else:
            if col and TABLES[table].get(col) == "d" and isinstance(val, str) and TS_RE.match(val) and not DATE_RE.match(val):
                val = val[:10]
            if isinstance(val, bool):
                val = "true" if val else "false"
            elif val is None:
                val = "null"
            params.append((key, f"{pg}.{val}"))
    return params


def order_param(table, order):
    if not order:
        return None
    desc = order.startswith("-")
    field = order.lstrip("-")
    col = column_for(table, field) or f"extra->>{field}"
    return f"{col}.{'desc' if desc else 'asc'}.nullslast"


def select_param(table, select):
    if not select:
        return "*"
    cols = []
    for f in select.split(","):
        f = f.strip()
        col = column_for(table, f)
        cols.append(col or f"extra->>{f}")
    if "id" not in cols:
        cols.insert(0, "id")
    if "updated_at" in TABLES[table] and "updated_at" not in cols:
        cols.append("updated_at")
    return ",".join(cols)


def fetch(client, table, params, limit=None, select="*"):
    out, offset, page = [], 0, min(PAGE, limit or PAGE)
    while True:
        p = list(params) + [("select", select), ("limit", str(page)), ("offset", str(offset))]
        status, payload, _ = client.rest("GET", table, p)
        if status != 200:
            raise SystemExit(err(f"read {table} failed", status=status, detail=payload))
        out.extend(payload)
        if len(payload) < page or (limit and len(out) >= limit):
            break
        offset += page
    return out[:limit] if limit else out


def emit(obj, out_file=None):
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    if out_file:
        with open(out_file, "w") as f:
            f.write(text)
        print(json.dumps({"written": out_file, "count": len(obj) if isinstance(obj, list) else 1}))
    else:
        print(text)


# ---------------------------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------------------------
def read_one(client, table, doc_id):
    status, payload, _ = client.rest("GET", table, [("id", f"eq.{doc_id}"), ("select", "*")])
    if status != 200:
        raise SystemExit(err(f"read {table}/{doc_id} failed", status=status, detail=payload))
    return payload[0] if payload else None


def write_one(client, op, collection, doc_id, data, if_version=None):
    collection, doc_id = split_ref(collection, doc_id)
    table = table_for(collection)
    data = data or {}
    if table == "runs" and op in ("set", "upsert", "update") and doc_id is None:
        return {"ok": False, "error": "runs needs an id"}
    if op == "set":
        row = to_row(table, data, for_insert=True)
        if doc_id:
            row["id"] = doc_id
        status, payload, _ = client.rest("POST", table, None, row, prefer="return=representation")
        if status == 409:
            return {"ok": False, "op": op, "collection": collection, "id": doc_id, "error": "exists", "detail": (payload or {}).get("message")}
        if status not in (200, 201):
            return {"ok": False, "op": op, "collection": collection, "id": doc_id, "error": "insert failed", "status": status, "detail": payload}
        return {"ok": True, "op": op, "collection": collection, "id": payload[0]["id"], "version": payload[0].get("updated_at")}
    if op == "upsert":
        current = read_one(client, table, doc_id)
        if current is None:
            return write_one(client, "set", collection, doc_id, data)
        return write_one(client, "update", collection, doc_id, data, None)
    if op == "update":
        current = read_one(client, table, doc_id)
        if current is None:
            return {"ok": False, "op": op, "collection": collection, "id": doc_id, "error": "not found"}
        if if_version and current.get("updated_at") and str(current["updated_at"]) != str(if_version):
            return {"ok": False, "op": op, "collection": collection, "id": doc_id, "error": "version conflict",
                    "current": from_row(table, current)}
        if table == "runs":
            merged = dict(current.get("data") or {})
            merged.update({k: v for k, v in data.items() if k not in ("id", "_version")})
            row = {"data": merged}
        else:
            row = to_row(table, data, existing_extra=current.get("extra") if "extra" in TABLES[table] else None)
            row.pop("id", None)
        if not row:
            return {"ok": True, "op": op, "collection": collection, "id": doc_id, "version": current.get("updated_at"), "unchanged": True}
        params = [("id", f"eq.{doc_id}")]
        if "updated_at" in TABLES[table] and current.get("updated_at"):
            params.append(("updated_at", f"eq.{current['updated_at']}"))
        status, payload, _ = client.rest("PATCH", table, params, row, prefer="return=representation")
        if status not in (200, 204):
            return {"ok": False, "op": op, "collection": collection, "id": doc_id, "error": "update failed", "status": status, "detail": payload}
        if not payload:
            latest = read_one(client, table, doc_id)
            return {"ok": False, "op": op, "collection": collection, "id": doc_id, "error": "version conflict",
                    "current": from_row(table, latest) if latest else None}
        return {"ok": True, "op": op, "collection": collection, "id": doc_id, "version": payload[0].get("updated_at")}
    if op == "delete":
        params = [("id", f"eq.{doc_id}")]
        if if_version and "updated_at" in TABLES[table]:
            params.append(("updated_at", f"eq.{if_version}"))
        status, payload, _ = client.rest("DELETE", table, params, None, prefer="return=representation")
        if status not in (200, 204):
            return {"ok": False, "op": op, "collection": collection, "id": doc_id, "error": "delete failed", "status": status, "detail": payload}
        if not payload:
            exists = read_one(client, table, doc_id)
            if exists is None:
                return {"ok": True, "op": op, "collection": collection, "id": doc_id, "already_gone": True}
            return {"ok": False, "op": op, "collection": collection, "id": doc_id, "error": "version conflict or no delete right",
                    "current": from_row(table, exists)}
        return {"ok": True, "op": op, "collection": collection, "id": doc_id}
    return {"ok": False, "error": f"unknown op {op!r}"}


# ---------------------------------------------------------------------------------------------
# Working days
# ---------------------------------------------------------------------------------------------
def is_workday(d):
    s = d.isoformat()
    if d.weekday() >= 5 or s in BANK_HOLIDAYS:
        return False
    if (d.month == 12 and d.day >= 24) or (d.month == 1 and d.day <= 2):
        return False
    return True


def add_workdays(d, n):
    step = 1 if n >= 0 else -1
    left = abs(n)
    while left:
        d += dt.timedelta(days=step)
        if is_workday(d):
            left -= 1
    return d


# ---------------------------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------------------------
def main(argv):
    ap = argparse.ArgumentParser(prog="swj_crm.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("whoami")
    s = sub.add_parser("schema"); s.add_argument("collection", nargs="?")
    s = sub.add_parser("get"); s.add_argument("collection"); s.add_argument("ids", nargs="*")
    for name in ("query", "list"):
        s = sub.add_parser(name); s.add_argument("collection"); s.add_argument("--where", default=None); s.add_argument("--order", default=None)
        s.add_argument("--limit", type=int, default=None); s.add_argument("--select", default=None); s.add_argument("--out", default=None)
    for name in ("set", "update", "upsert", "delete"):
        s = sub.add_parser(name); s.add_argument("collection"); s.add_argument("id", nargs="?"); s.add_argument("--data", default="{}")
        s.add_argument("--if-version", dest="if_version", default=None)
    s = sub.add_parser("batch"); s.add_argument("file")
    sub.add_parser("today")
    s = sub.add_parser("workday"); s.add_argument("what", choices=["is", "add"]); s.add_argument("date"); s.add_argument("n", nargs="?", type=int, default=0)
    a = ap.parse_args(argv)

    if a.cmd == "today":
        now = dt.datetime.now(LONDON)
        print(json.dumps({"date": now.date().isoformat(), "time": now.strftime("%H:%M"), "weekday": now.strftime("%A"),
                          "workday": is_workday(now.date()), "now": now.isoformat(timespec="seconds")}))
        return 0
    if a.cmd == "workday":
        d = dt.date.fromisoformat(a.date)
        if a.what == "is":
            print(json.dumps({"date": a.date, "workday": is_workday(d)}))
        else:
            print(json.dumps({"from": a.date, "add": a.n, "date": add_workdays(d, a.n).isoformat()}))
        return 0
    if a.cmd == "schema":
        if a.collection:
            table = table_for(a.collection)
            rev = {v: k for k, v in RENAME.get(table, {}).items()}
            fields = [rev.get(c, camel(c)) for c in TABLES[table] if c not in ("extra", "updated_at")]
            print(json.dumps({"collection": a.collection, "table": table, "fields": fields, "idRule": ID_RULES.get(a.collection, ID_RULES["general"]),
                              "enums": {k.split(".", 1)[1]: v for k, v in ENUMS.items() if k.startswith(a.collection + ".")},
                              "note": "any other field is kept in extra and comes back flat on reads"}))
        else:
            print(json.dumps({"collections": sorted(COLLECTIONS), "idRules": ID_RULES, "enums": ENUMS,
                              "versioning": "every read carries _version; pass it as ifVersion on update and delete",
                              "dates": "YYYY-MM-DD for dates, ISO 8601 for timestamps, blank string clears a value"}))
        return 0

    client = Client()
    if a.cmd == "whoami":
        status, payload, _ = client.rest("GET", "team_members", [("select", "name,email,role,active")])
        if status != 200:
            print(err("signed in but cannot read team_members (is the skills user in team_members and active?)", status=status, detail=payload))
            return 1
        print(json.dumps({"signedInAs": client.email, "team": payload}))
        return 0
    if a.cmd == "get":
        collection, first = split_ref(a.collection, a.ids[0] if a.ids else None)
        ids = [first] + a.ids[1:] if first else a.ids
        table = table_for(collection)
        if not ids:
            print(err("get needs at least one id")); return 1
        rows = fetch(client, table, [("id", "in.(" + ",".join(pg_literal(i) for i in ids) + ")")])
        by_id = {r["id"]: from_row(table, r) for r in rows}
        docs = [by_id.get(i, {"id": i, "found": False}) for i in ids]
        print(json.dumps(docs[0] if len(ids) == 1 else docs, ensure_ascii=False, separators=(",", ":")))
        return 0
    if a.cmd in ("query", "list"):
        table = table_for(a.collection)
        where = json.loads(a.where) if a.where else []
        params = where_params(table, where)
        order = order_param(table, a.order)
        if order:
            params.append(("order", order))
        elif table != "runs":
            params.append(("order", "id.asc"))
        rows = fetch(client, table, params, a.limit, select_param(table, a.select))
        docs = [from_row(table, r) for r in rows]
        if a.select:
            keep = set(f.strip() for f in a.select.split(",")) | {"id", "_version"}
            docs = [{k: v for k, v in d.items() if k in keep} for d in docs]
        emit(docs, a.out)
        return 0
    if a.cmd in ("set", "update", "upsert", "delete"):
        data = json.loads(a.data) if a.data else {}
        res = write_one(client, a.cmd, a.collection, a.id, data, a.if_version)
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res.get("ok") else 2
    if a.cmd == "batch":
        with open(a.file) as f:
            writes = json.load(f)
        if not isinstance(writes, list):
            print(err("batch file must hold a list of writes")); return 1
        results = []
        for w in writes:
            try:
                results.append(write_one(client, w.get("op"), w.get("collection"), w.get("id"), w.get("data"), w.get("ifVersion")))
            except SystemExit as e:  # a bad value or unknown collection in one write must not abort the rest
                detail = e.code if isinstance(e.code, str) else str(e.code)
                try:
                    detail = json.loads(detail).get("error", detail)
                except (ValueError, AttributeError):
                    pass
                results.append({"ok": False, "op": w.get("op"), "collection": w.get("collection"), "id": w.get("id"), "error": detail})
        ok = sum(1 for r in results if r.get("ok"))
        print(json.dumps({"ok": ok, "failed": len(results) - ok, "results": results}, ensure_ascii=False))
        return 0 if ok == len(results) else 2
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code)
            sys.exit(1)
        raise
