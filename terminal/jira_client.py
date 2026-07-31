"""
Jira client — thin httpx wrapper, no SDK. Frozen reference, not a background mirror:
fetches on demand (popover open, tab focus). See Docs/WorkItems/connections.md.

Auth: API token + basic auth (email:token), token generated manually and stored in
secrets.json (see secrets_store). No creds -> available() is False, Jira features off.

Endpoints (Jira Cloud REST v3):
- POST /rest/api/3/search/jql  (the classic GET /search was deprecated 2025-05; this
  is the enhanced replacement, token-paginated)
- GET  /rest/api/3/issue/{key}?fields=...
- GET/POST /rest/api/3/issue/{key}/transitions
"""

import re

import httpx

from terminal import secrets_store

CLOUD_ID = "5574e58b-e70b-4fe9-a7e2-b5be1dc6e2a7"  # already resolved
SITE = "https://mautomacao.atlassian.net"          # human-facing (browse links)
# Scoped API tokens (read:jira-work / write:jira-work) are NOT accepted on the site URL —
# they only authenticate against the api.atlassian.com gateway, addressed by cloudId. Basic
# auth (email:token) still applies. Classic unscoped tokens work on either, so the gateway
# is the safe base for both.
BASE = f"https://api.atlassian.com/ex/jira/{CLOUD_ID}"
_TIMEOUT = 15.0

# Only the fields a work item stores — avoids the ~131KB payload that *all fields returns.
_FIELDS = ["summary", "status", "duedate", "issuetype", "priority"]

# The only project this app cares about. Scopes BOTH the list and the search, so issues of
# other boards never reach the link popover.
PROJECT = "DS"

# JQL for the link popover: my open issues, most-recently-touched first.
_MY_OPEN_JQL = (f"project = {PROJECT} AND assignee = currentUser() "
                "AND statusCategory != Done ORDER BY updated DESC")

# A query only goes through the `key =` clause when it LOOKS like an issue key; free text goes
# to `summary ~ / text ~`. (Jira tolerates `key = "cnpj"` — it just never matches — but keeping
# the two paths apart makes a key search exact and a text search wide.)
_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")


def available() -> bool:
    return secrets_store.jira_creds() is not None


def _client() -> httpx.Client:
    creds = secrets_store.jira_creds()
    if creds is None:
        raise RuntimeError("Jira credentials missing (secrets.json)")
    email, token = creds
    return httpx.Client(
        base_url=BASE,
        auth=httpx.BasicAuth(email, token),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=_TIMEOUT,
    )


def browse_url(key: str) -> str:
    return f"{SITE}/browse/{key}"


def _normalize(issue: dict) -> dict:
    """Map a raw Jira issue to the work-item shape the UI/store consume.
    Jira duedate is date-only, so duedate_has_time is always False here."""
    f = issue.get("fields") or {}
    key = issue.get("key")
    status = ((f.get("status") or {}).get("name"))
    issuetype = ((f.get("issuetype") or {}).get("name"))
    priority = ((f.get("priority") or {}).get("name"))
    return {
        "external_key": key,
        "title": f.get("summary") or "",
        "status": status,
        "duedate": f.get("duedate"),  # "YYYY-MM-DD" or None
        "duedate_has_time": False,
        "external_url": browse_url(key) if key else None,
        "issuetype": issuetype,
        "priority": priority,
    }


def list_my_open_issues(max_results: int = 50) -> list[dict]:
    """My open issues, normalized. [] if Jira disabled."""
    if not available():
        return []
    body = {"jql": _MY_OPEN_JQL, "fields": _FIELDS, "maxResults": max_results}
    with _client() as c:
        r = c.post("/rest/api/3/search/jql", json=body)
        r.raise_for_status()
        data = r.json()
    return [_normalize(it) for it in data.get("issues", [])]


def search_issues(text: str, max_results: int = 25) -> list[dict]:
    """Search open PROJECT issues by key or text (link popover 'pesquiso por key OU nome').
    Unlike list_my_open_issues this is NOT assignee-scoped — you can link a teammate's issue."""
    if not available() or not text.strip():
        return []
    raw = text.strip()
    # Escape backslash first, then quote — a trailing "\" would otherwise yield invalid JQL (400).
    t = raw.replace('\\', '\\\\').replace('"', '\\"')
    # `~` already does word/prefix matching; a trailing "*" inside a quoted multi-word phrase is
    # not a wildcard there, so it only narrowed results. `text ~` widens to description/comments.
    match = f'key = "{t}"' if _KEY_RE.match(raw) else f'(summary ~ "{t}" OR text ~ "{t}")'
    jql = (f'project = {PROJECT} AND {match} AND statusCategory != Done '
           f'ORDER BY updated DESC')
    body = {"jql": jql, "fields": _FIELDS, "maxResults": max_results}
    with _client() as c:
        r = c.post("/rest/api/3/search/jql", json=body)
        r.raise_for_status()
        data = r.json()
    return [_normalize(it) for it in data.get("issues", [])]


def fetch_issue(key: str) -> dict | None:
    """Refetch one issue's status+duedate (tab-focus refresh). None if disabled/not found."""
    if not available() or not key:
        return None
    with _client() as c:
        r = c.get(f"/rest/api/3/issue/{key}", params={"fields": "status,duedate,summary"})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return _normalize(r.json())


def get_transitions(key: str) -> list[dict]:
    """Real transition list (varies per workflow). [{id, name, to_status}]. Empty if disabled."""
    if not available() or not key:
        return []
    with _client() as c:
        r = c.get(f"/rest/api/3/issue/{key}/transitions")
        r.raise_for_status()
        data = r.json()
    out = []
    for t in data.get("transitions", []):
        out.append({
            "id": t.get("id"),
            "name": t.get("name"),
            "to_status": ((t.get("to") or {}).get("name")),
        })
    return out


def transition_issue(key: str, transition_id: str) -> bool:
    """Move an issue (close-tab -> transition). True on success."""
    if not available() or not key or not transition_id:
        return False
    with _client() as c:
        r = c.post(f"/rest/api/3/issue/{key}/transitions",
                   json={"transition": {"id": transition_id}})
        r.raise_for_status()
    return True  # 204 No Content on success
