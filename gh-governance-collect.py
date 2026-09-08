#!/usr/bin/env python3
"""
gh-governance-collect.py -- org-wide GitHub governance collector.

One file. Python 3.8+. Standard library only -- nothing to pip install.
`openssl` is required only for GitHub App authentication, not for token auth.

For every repository in an organization this records:

  * effective branch rules on the default branch. The /rules/branches endpoint
    already folds in repository-, organization- AND enterprise-level rulesets,
    so there is no condition-matching logic to reimplement here.
  * the ruleset each rule came from, with its enforcement mode and bypass-actor
    count -- the difference between "configured" and "enforced".
  * deployment environments, their required reviewers, and prevent_self_review.
  * optionally: custom repository property values, and expanded team membership
    for environment approver teams.

Results land in SQLite (one row per repo) plus an optional CSV. A dashboard
reads the database; it never calls the API.

    python3 gh-governance-collect.py --org ACME --db governance.db --summary

Authentication, in order of preference:
    --app-id 123 --private-key app.pem     GitHub App: ~3x the rate limit,
                                           not tied to a human account
    GITHUB_TOKEN=...                       PAT or installation token
    (falls back to `gh auth token` if the gh CLI is signed in)

Base URL override for proxies or GHES:
    GITHUB_API=https://github.example.com/api/v3

Read-only: every request this makes is a GET, except the App token exchange.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

VERSION = "1.0.0"
USER_AGENT = "gh-governance-collect/" + VERSION
DEFAULT_API = os.environ.get("GITHUB_API", "https://api.github.com").rstrip("/")

# Environment names treated as production-like. Deliberately does NOT match
# "non-prod", "preprod" or "staging". Tune with --prod-pattern, and read the
# "environment names NOT classified prod-like" list the summary prints -- that
# list is how you prove the heuristic caught everything it should have.
DEFAULT_PROD_PATTERN = r"(?i)^(prod|prd|production|live|dr)([-_./].*)?$"

LINK_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def b64u(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def log(msg: str) -> None:
    sys.stderr.write("[{}] {}\n".format(time.strftime("%H:%M:%S"), msg))
    sys.stderr.flush()


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------

class TokenAuth:
    """A static PAT or installation token."""

    def __init__(self, token: str):
        self._token = token

    def token(self) -> str:
        return self._token

    def describe(self) -> str:
        return "token (static)"


class AppAuth:
    """GitHub App installation auth.

    Signs the RS256 JWT by shelling out to `openssl`, so no PyJWT/cryptography
    dependency is needed. Installation tokens last an hour and are refreshed
    automatically five minutes before expiry.
    """

    def __init__(self, app_id: str, key_path: str, api: str, org: str,
                 installation_id: str = None):
        self.app_id = str(app_id)
        self.key_path = key_path
        self.api = api
        self.org = org
        self.installation_id = installation_id
        self._token = None
        self._expires = 0.0
        self._lock = threading.Lock()
        if not os.path.exists(key_path):
            raise SystemExit("private key not found: {}".format(key_path))

    def _jwt(self) -> str:
        now = int(time.time())
        header = {"alg": "RS256", "typ": "JWT"}
        payload = {"iat": now - 60, "exp": now + 540, "iss": self.app_id}
        parts = [b64u(json.dumps(x, separators=(",", ":")).encode()) for x in (header, payload)]
        signing_input = b".".join(parts)
        try:
            proc = subprocess.run(
                ["openssl", "dgst", "-sha256", "-sign", self.key_path],
                input=signing_input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            raise SystemExit("openssl not found on PATH; needed for --app-id auth")
        if proc.returncode != 0:
            raise SystemExit("openssl signing failed: {}".format(proc.stderr.decode().strip()))
        return (signing_input + b"." + b64u(proc.stdout)).decode()

    def _raw(self, method: str, path: str, jwt: str):
        req = urllib.request.Request(self.api + path, method=method)
        req.add_header("Authorization", "Bearer " + jwt)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", USER_AGENT)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:400]
            raise SystemExit("App auth failed on {} {}: {} {}".format(method, path, exc.code, body))

    def token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires - 300:
                return self._token
            jwt = self._jwt()
            iid = self.installation_id
            if not iid:
                inst = self._raw("GET", "/orgs/{}/installation".format(self.org), jwt)
                iid = inst["id"]
                self.installation_id = iid
            data = self._raw("POST", "/app/installations/{}/access_tokens".format(iid), jwt)
            self._token = data["token"]
            exp = data.get("expires_at")
            self._expires = time.time() + 3600
            if exp:
                try:
                    self._expires = datetime.strptime(
                        exp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
                except ValueError:
                    pass
            return self._token

    def describe(self) -> str:
        return "GitHub App {} (installation {})".format(self.app_id, self.installation_id or "auto")


def resolve_auth(args) -> object:
    if args.app_id:
        if not args.private_key:
            raise SystemExit("--app-id requires --private-key")
        return AppAuth(args.app_id, args.private_key, args.api, args.org, args.installation_id)
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not tok:
        try:
            proc = subprocess.run(["gh", "auth", "token"],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            if proc.returncode == 0:
                tok = proc.stdout.decode().strip()
        except FileNotFoundError:
            pass
    if not tok:
        raise SystemExit(
            "no credentials. Set GITHUB_TOKEN, or sign in with `gh auth login`, "
            "or pass --app-id/--private-key.")
    return TokenAuth(tok)


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------

class Pacer:
    """Keeps the sustained request rate under GitHub's secondary limit.

    The documented ceiling is roughly 900 points per minute; the default here
    leaves headroom for anything else using the same credential.
    """

    def __init__(self, per_minute: int):
        self.interval = 60.0 / max(1, per_minute)
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if self._next < now:
                self._next = now
            delay = self._next - now
            self._next += self.interval
        if delay > 0:
            time.sleep(delay)


class Limiter:
    """Tracks primary rate-limit headers and parks the run before exhaustion."""

    def __init__(self, floor: int = 100):
        self._lock = threading.Lock()
        self.floor = floor
        self.remaining = None
        self.limit = None
        self.reset_at = None
        self.first_remaining = None

    def observe(self, headers) -> None:
        with self._lock:
            rem = headers.get("x-ratelimit-remaining")
            lim = headers.get("x-ratelimit-limit")
            rst = headers.get("x-ratelimit-reset")
            if rem is not None:
                self.remaining = int(rem)
                if self.first_remaining is None:
                    self.first_remaining = self.remaining
            if lim is not None:
                self.limit = int(lim)
            if rst is not None:
                self.reset_at = int(rst)

    def maybe_park(self) -> None:
        with self._lock:
            rem, rst = self.remaining, self.reset_at
        if rem is None or rem > self.floor:
            return
        nap = max(0.0, (rst or 0) - time.time()) + 3
        log("primary rate limit nearly exhausted ({} left); sleeping {:.0f}s".format(rem, nap))
        time.sleep(nap)


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  org TEXT, started_at TEXT, finished_at TEXT,
  repos_total INTEGER, repos_in_scope INTEGER, repos_errored INTEGER,
  api_calls INTEGER, api_not_modified INTEGER,
  rate_limit INTEGER, rate_used INTEGER,
  tool_version TEXT, params TEXT
);
CREATE TABLE IF NOT EXISTS repos (
  run_id INTEGER, org TEXT, repo TEXT, default_branch TEXT, visibility TEXT,
  archived INTEGER, fork INTEGER, is_empty INTEGER, pushed_at TEXT, in_scope INTEGER,
  pr_required INTEGER, pr_required_enforced INTEGER,
  self_review_blocked INTEGER, self_review_blocked_enforced INTEGER,
  required_approvals INTEGER, code_owner_review INTEGER,
  force_push_blocked INTEGER, force_push_blocked_enforced INTEGER,
  deletion_blocked INTEGER, deletion_blocked_enforced INTEGER,
  ruleset_count INTEGER, evaluate_only INTEGER, max_bypass_actors INTEGER,
  enforcement_unknown INTEGER,
  env_count INTEGER, prod_env_count INTEGER,
  prod_env_with_reviewers INTEGER, prod_env_self_review_blocked INTEGER,
  min_prod_approver_count INTEGER, error TEXT,
  PRIMARY KEY (run_id, org, repo)
);
CREATE TABLE IF NOT EXISTS repo_rules (
  run_id INTEGER, org TEXT, repo TEXT, rule_type TEXT, ruleset_id INTEGER,
  ruleset_source TEXT, ruleset_source_type TEXT, enforcement TEXT,
  bypass_actors INTEGER, parameters TEXT
);
CREATE TABLE IF NOT EXISTS environments (
  run_id INTEGER, org TEXT, repo TEXT, environment TEXT, is_prod_like INTEGER,
  has_required_reviewers INTEGER, prevent_self_review INTEGER,
  reviewer_users TEXT, reviewer_teams TEXT, approver_count INTEGER,
  wait_timer INTEGER, branch_policy TEXT
);
CREATE TABLE IF NOT EXISTS rulesets (
  source_type TEXT, source TEXT, ruleset_id INTEGER, name TEXT, target TEXT,
  enforcement TEXT, bypass_actors INTEGER, bypass_detail TEXT,
  conditions TEXT, fetched_at TEXT,
  PRIMARY KEY (source_type, source, ruleset_id)
);
CREATE TABLE IF NOT EXISTS teams (
  org TEXT, slug TEXT, members TEXT, member_count INTEGER, fetched_at TEXT,
  PRIMARY KEY (org, slug)
);
CREATE TABLE IF NOT EXISTS repo_properties (
  run_id INTEGER, org TEXT, repo TEXT, name TEXT, value TEXT
);
CREATE TABLE IF NOT EXISTS http_cache (
  url TEXT PRIMARY KEY, etag TEXT, body TEXT, fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_repos_run ON repos(run_id);
CREATE INDEX IF NOT EXISTS idx_rules_run ON repo_rules(run_id, repo);
CREATE INDEX IF NOT EXISTS idx_envs_run ON environments(run_id, repo);
"""


class Store:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self.lock = threading.Lock()

    def cache_get(self, url: str):
        with self.lock:
            row = self.conn.execute(
                "SELECT etag, body FROM http_cache WHERE url=?", (url,)).fetchone()
        return row

    def cache_put(self, url: str, etag: str, body: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO http_cache(url, etag, body, fetched_at) VALUES(?,?,?,?) "
                "ON CONFLICT(url) DO UPDATE SET etag=excluded.etag, body=excluded.body, "
                "fetched_at=excluded.fetched_at",
                (url, etag, body, now_iso()))
            self.conn.commit()

    def execute(self, sql: str, params=()):
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def executemany(self, sql: str, rows) -> None:
        rows = list(rows)
        if not rows:
            return
        with self.lock:
            self.conn.executemany(sql, rows)
            self.conn.commit()

    def query(self, sql: str, params=()):
        with self.lock:
            return self.conn.execute(sql, params).fetchall()


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------

class Client:
    def __init__(self, auth, store: Store, api: str, pacer: Pacer, limiter: Limiter,
                 timeout: int = 30, retries: int = 4):
        self.auth = auth
        self.store = store
        self.api = api
        self.pacer = pacer
        self.limiter = limiter
        self.timeout = timeout
        self.retries = retries
        self.calls = 0
        self.not_modified = 0
        self._counter_lock = threading.Lock()

    def _bump(self, not_modified: bool = False) -> None:
        with self._counter_lock:
            self.calls += 1
            if not_modified:
                self.not_modified += 1

    def _request(self, url: str, etag: str = None):
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", "Bearer " + self.auth.token())
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", USER_AGENT)
        if etag:
            req.add_header("If-None-Match", etag)
        self.pacer.wait()
        self.limiter.maybe_park()
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")

    def get(self, path: str, cache: bool = False, allow_404: bool = True):
        """Returns (parsed_json_or_None, next_url_or_None)."""
        url = path if path.startswith("http") else self.api + path
        cached = self.store.cache_get(url) if cache else None
        etag = cached[0] if cached else None

        for attempt in range(self.retries + 1):
            try:
                status, headers, body = self._request(url, etag)
                self.limiter.observe(headers)
                self._bump()
                nxt = LINK_NEXT.search(headers.get("Link", "") or "")
                return json.loads(body) if body.strip() else None, (nxt.group(1) if nxt else None)
            except urllib.error.HTTPError as exc:
                headers = dict(exc.headers or {})
                self.limiter.observe(headers)
                if exc.code == 304 and cached:
                    self._bump(not_modified=True)
                    return json.loads(cached[1]), None
                if exc.code == 404 and allow_404:
                    self._bump()
                    return None, None
                if exc.code in (403, 429):
                    self._bump()
                    retry_after = headers.get("retry-after")
                    remaining = headers.get("x-ratelimit-remaining")
                    if retry_after:
                        nap = float(retry_after)
                    elif remaining == "0":
                        nap = max(0.0, int(headers.get("x-ratelimit-reset", 0)) - time.time()) + 3
                    else:
                        # Not a rate limit: most often a permissions problem.
                        raise PermissionError("403 on {}: {}".format(
                            url, exc.read().decode(errors="replace")[:200]))
                    if attempt >= self.retries:
                        raise
                    log("throttled on {}; sleeping {:.0f}s".format(url.rsplit("/", 2)[-1], nap))
                    time.sleep(min(nap, 3600))
                    continue
                if 500 <= exc.code < 600 and attempt < self.retries:
                    self._bump()
                    time.sleep(2 ** attempt)
                    continue
                self._bump()
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt >= self.retries:
                    raise
                time.sleep(2 ** attempt)

        raise RuntimeError("unreachable")

    def get_json(self, path: str, cache: bool = False):
        data, _ = self.get(path, cache=cache)
        if cache and data is not None:
            # Re-request bodies are cached only when a fresh 200 came back; the
            # ETag is written by get_cached() below.
            pass
        return data

    def get_cached(self, path: str):
        """GET with an ETag round-trip. 304s do not count against the primary limit."""
        url = path if path.startswith("http") else self.api + path
        cached = self.store.cache_get(url)
        etag = cached[0] if cached else None
        for attempt in range(self.retries + 1):
            try:
                status, headers, body = self._request(url, etag)
                self.limiter.observe(headers)
                self._bump()
                new_etag = headers.get("ETag")
                if new_etag:
                    self.store.cache_put(url, new_etag, body)
                return json.loads(body) if body.strip() else None
            except urllib.error.HTTPError as exc:
                headers = dict(exc.headers or {})
                self.limiter.observe(headers)
                if exc.code == 304 and cached:
                    self._bump(not_modified=True)
                    return json.loads(cached[1])
                if exc.code == 404:
                    self._bump()
                    return None
                if exc.code in (403, 429):
                    self._bump()
                    retry_after = headers.get("retry-after")
                    remaining = headers.get("x-ratelimit-remaining")
                    if retry_after:
                        nap = float(retry_after)
                    elif remaining == "0":
                        nap = max(0.0, int(headers.get("x-ratelimit-reset", 0)) - time.time()) + 3
                    else:
                        raise PermissionError("403 on {}: {}".format(
                            url, exc.read().decode(errors="replace")[:200]))
                    if attempt >= self.retries:
                        raise
                    time.sleep(min(nap, 3600))
                    continue
                if 500 <= exc.code < 600 and attempt < self.retries:
                    self._bump()
                    time.sleep(2 ** attempt)
                    continue
                self._bump()
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt >= self.retries:
                    raise
                time.sleep(2 ** attempt)
        raise RuntimeError("unreachable")

    def paginate(self, path: str, key: str = None):
        """Walk a paginated list. Deliberately NOT ETag-cached: a new repo can
        shift items between pages, so a 304 on page 1 would not prove the
        inventory is unchanged."""
        out = []
        url = path if path.startswith("http") else self.api + path
        while url:
            data, nxt = self.get(url)
            if data is None:
                break
            out.extend(data if key is None else data.get(key, []))
            url = nxt
        return out


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------

class RulesetCache:
    """Ruleset details are shared across repos: fetch each one once per run."""

    def __init__(self, client: Client, store: Store, org: str, enterprise: str = None):
        self.client = client
        self.store = store
        self.org = org
        self.enterprise = enterprise
        self._cache = {}
        self._lock = threading.Lock()

    def get(self, source_type: str, source: str, ruleset_id):
        if ruleset_id is None:
            return None
        key = (source_type or "", source or "", ruleset_id)
        with self._lock:
            if key in self._cache:
                return self._cache[key]

        st = (source_type or "").lower()
        if st == "organization":
            path = "/orgs/{}/rulesets/{}".format(source or self.org, ruleset_id)
        elif st == "repository":
            path = "/repos/{}/rulesets/{}".format(source, ruleset_id)
        elif st == "enterprise":
            if not self.enterprise:
                detail = {"enforcement": "unknown",
                          "note": "enterprise ruleset; pass --enterprise to resolve"}
                with self._lock:
                    self._cache[key] = detail
                return detail
            path = "/enterprises/{}/rulesets/{}".format(self.enterprise, ruleset_id)
        else:
            return None

        try:
            detail = self.client.get_cached(path)
        except PermissionError as exc:
            detail = {"enforcement": "unknown", "note": str(exc)[:200]}
        except urllib.error.HTTPError as exc:
            detail = {"enforcement": "unknown", "note": "HTTP {}".format(exc.code)}

        if detail is None:
            detail = {"enforcement": "unknown", "note": "not found"}

        with self._lock:
            self._cache[key] = detail

        if detail.get("enforcement") != "unknown":
            self.store.execute(
                "INSERT INTO rulesets(source_type, source, ruleset_id, name, target, "
                "enforcement, bypass_actors, bypass_detail, conditions, fetched_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_type, source, ruleset_id) "
                "DO UPDATE SET name=excluded.name, enforcement=excluded.enforcement, "
                "bypass_actors=excluded.bypass_actors, bypass_detail=excluded.bypass_detail, "
                "conditions=excluded.conditions, fetched_at=excluded.fetched_at",
                (source_type, source, ruleset_id, detail.get("name"), detail.get("target"),
                 detail.get("enforcement"), len(detail.get("bypass_actors") or []),
                 json.dumps(detail.get("bypass_actors") or []),
                 json.dumps(detail.get("conditions") or {}), now_iso()))
        return detail


def collect_repo(client: Client, rulesets: RulesetCache, org: str, repo: dict,
                 prod_re, bypass_threshold: int) -> dict:
    """All API work for one repository: two calls in the common case."""
    name = repo["name"]
    row = {
        "repo": name,
        "default_branch": repo.get("default_branch"),
        "visibility": repo.get("visibility"),
        "archived": int(bool(repo.get("archived"))),
        "fork": int(bool(repo.get("fork"))),
        "is_empty": int((repo.get("size") or 0) == 0),
        "pushed_at": repo.get("pushed_at"),
        "error": None,
        "rules": [],
        "envs": [],
    }

    try:
        branch = repo.get("default_branch")
        if branch:
            path = "/repos/{}/{}/rules/branches/{}".format(
                org, name, urllib.parse.quote(branch, safe=""))
            effective = client.get_cached(path) or []
        else:
            effective = []

        for item in effective:
            st = item.get("ruleset_source_type")
            src = item.get("ruleset_source")
            rid = item.get("ruleset_id")
            detail = rulesets.get(st, src, rid) or {}
            row["rules"].append({
                "type": item.get("type"),
                "ruleset_id": rid,
                "ruleset_source": src,
                "ruleset_source_type": st,
                "enforcement": detail.get("enforcement", "unknown"),
                "bypass_actors": len(detail.get("bypass_actors") or []),
                "parameters": item.get("parameters") or {},
            })

        envs = client.get_cached("/repos/{}/{}/environments?per_page=100".format(org, name))
        for env in (envs or {}).get("environments", []) or []:
            reviewers_users, reviewers_teams = [], []
            prevent_self = None
            wait_timer = None
            has_reviewers = 0
            for pr in env.get("protection_rules", []) or []:
                if pr.get("type") == "required_reviewers":
                    has_reviewers = 1
                    prevent_self = int(bool(pr.get("prevent_self_review")))
                    for rv in pr.get("reviewers", []) or []:
                        target = rv.get("reviewer") or {}
                        if rv.get("type") == "Team":
                            reviewers_teams.append(target.get("slug") or target.get("name"))
                        else:
                            reviewers_users.append(target.get("login"))
                elif pr.get("type") == "wait_timer":
                    wait_timer = pr.get("wait_timer")
            row["envs"].append({
                "name": env.get("name"),
                "is_prod_like": int(bool(prod_re.match(env.get("name") or ""))),
                "has_required_reviewers": has_reviewers,
                "prevent_self_review": prevent_self,
                "reviewer_users": reviewers_users,
                "reviewer_teams": reviewers_teams,
                "wait_timer": wait_timer,
                "branch_policy": json.dumps(env.get("deployment_branch_policy") or {}),
            })

    except PermissionError as exc:
        row["error"] = "permission: {}".format(exc)[:300]
    except urllib.error.HTTPError as exc:
        row["error"] = "http {}".format(exc.code)
    except Exception as exc:  # noqa: BLE001 - one bad repo must not kill the run
        row["error"] = "{}: {}".format(type(exc).__name__, exc)[:300]

    return summarise_repo(row, bypass_threshold)


def summarise_repo(row: dict, bypass_threshold: int) -> dict:
    """Collapse rules and environments into the per-repo control flags."""
    rules = row["rules"]

    def enforced(rule) -> bool:
        return (rule["enforcement"] == "active"
                and rule["bypass_actors"] <= bypass_threshold)

    pr_rules = [r for r in rules if r["type"] == "pull_request"]
    row["pr_required"] = int(bool(pr_rules))
    row["pr_required_enforced"] = int(any(enforced(r) for r in pr_rules))

    self_review = [r for r in pr_rules
                   if r["parameters"].get("require_last_push_approval")]
    row["self_review_blocked"] = int(bool(self_review))
    row["self_review_blocked_enforced"] = int(any(enforced(r) for r in self_review))

    approvals = [r["parameters"].get("required_approving_review_count", 0) for r in pr_rules]
    row["required_approvals"] = max(approvals) if approvals else 0
    row["code_owner_review"] = int(any(
        r["parameters"].get("require_code_owner_review") for r in pr_rules))

    ff = [r for r in rules if r["type"] == "non_fast_forward"]
    row["force_push_blocked"] = int(bool(ff))
    row["force_push_blocked_enforced"] = int(any(enforced(r) for r in ff))

    dele = [r for r in rules if r["type"] == "deletion"]
    row["deletion_blocked"] = int(bool(dele))
    row["deletion_blocked_enforced"] = int(any(enforced(r) for r in dele))

    ids = {r["ruleset_id"] for r in rules if r["ruleset_id"] is not None}
    row["ruleset_count"] = len(ids)
    modes = {r["enforcement"] for r in rules}
    row["evaluate_only"] = int(bool(rules) and "active" not in modes)
    row["enforcement_unknown"] = int("unknown" in modes)
    row["max_bypass_actors"] = max([r["bypass_actors"] for r in rules], default=0)

    envs = row["envs"]
    prod = [e for e in envs if e["is_prod_like"]]
    row["env_count"] = len(envs)
    row["prod_env_count"] = len(prod)
    row["prod_env_with_reviewers"] = int(bool(prod) and all(
        e["has_required_reviewers"] for e in prod))
    row["prod_env_self_review_blocked"] = int(bool(prod) and all(
        e["prevent_self_review"] for e in prod))
    counts = [len(e["reviewer_users"]) + len(e["reviewer_teams"])
              for e in prod if e["has_required_reviewers"]]
    row["min_prod_approver_count"] = min(counts) if counts else 0
    return row


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

BRANCH_CONTROLS = [
    ("pr_required", "Pull request required"),
    ("self_review_blocked", "Self-review prevented (require_last_push_approval)"),
    ("force_push_blocked", "Force push blocked"),
    ("deletion_blocked", "Branch deletion blocked"),
]


def pct(n: int, d: int) -> str:
    return "  n/a" if not d else "{:5.1f}%".format(100.0 * n / d)


def print_summary(store: Store, run_id: int, org: str, args, env_names_seen) -> None:
    rows = store.query(
        "SELECT * FROM repos WHERE run_id=?", (run_id,))
    cols = [d[1] for d in store.query("PRAGMA table_info(repos)")]
    recs = [dict(zip(cols, r)) for r in rows]
    scoped = [r for r in recs if r["in_scope"]]
    n = len(scoped)

    out = []
    out.append("")
    out.append("=" * 78)
    out.append("GOVERNANCE COVERAGE -- {}   run {}   {}".format(org, run_id, now_iso()))
    out.append("=" * 78)
    out.append("")
    out.append("Denominator")
    out.append("  repositories returned by the API .......... {}".format(len(recs)))
    out.append("  excluded: archived ....................... {}".format(
        sum(1 for r in recs if r["archived"])))
    out.append("  excluded: forks .......................... {}".format(
        sum(1 for r in recs if r["fork"])))
    out.append("  excluded: empty (0 KB, no default branch) . {}".format(
        sum(1 for r in recs if r["is_empty"])))
    out.append("  IN SCOPE ................................. {}".format(n))
    errored = [r for r in scoped if r["error"]]
    if errored:
        out.append("  of which errored (counted as unknown) .... {}".format(len(errored)))
    out.append("")
    out.append("Branch controls on the default branch")
    out.append("  {:<52} {:>9} {:>9}".format("", "CONFIGURED", "ENFORCED"))
    for key, label in BRANCH_CONTROLS:
        c = sum(1 for r in scoped if r[key])
        e = sum(1 for r in scoped if r[key + "_enforced"])
        out.append("  {:<52} {:>4} {} {:>4} {}".format(
            label[:52], c, pct(c, n), e, pct(e, n)))
    out.append("")
    out.append("  CONFIGURED = a ruleset carrying the rule applies to the repo.")
    out.append("  ENFORCED   = that ruleset is enforcement=active AND has at most")
    out.append("               {} bypass actor(s).".format(args.bypass_threshold))
    ev = sum(1 for r in scoped if r["evaluate_only"])
    unk = sum(1 for r in scoped if r["enforcement_unknown"])
    byp = sum(1 for r in scoped if r["max_bypass_actors"] > args.bypass_threshold)
    out.append("")
    out.append("  Why the two columns differ")
    out.append("    repos whose only rulesets are evaluate/disabled .. {}".format(ev))
    out.append("    repos with a ruleset over the bypass threshold ... {}".format(byp))
    out.append("    repos with at least one unresolved ruleset ....... {}".format(unk))
    if unk:
        out.append("      (enterprise rulesets need --enterprise to resolve)")

    out.append("")
    out.append("Deployment environments")
    with_prod = [r for r in scoped if r["prod_env_count"] > 0]
    d = len(with_prod)
    out.append("  repos with any environment ............... {:>4} {}".format(
        sum(1 for r in scoped if r["env_count"] > 0),
        pct(sum(1 for r in scoped if r["env_count"] > 0), n)))
    out.append("  repos with a prod-like environment ....... {:>4} {}".format(d, pct(d, n)))
    if d:
        rv = sum(1 for r in with_prod if r["prod_env_with_reviewers"])
        sr = sum(1 for r in with_prod if r["prod_env_self_review_blocked"])
        solo = sum(1 for r in with_prod if 0 < r["min_prod_approver_count"] < 2)
        out.append("    ...all prod envs have required reviewers  {:>4} {}   (of {} with prod)".format(
            rv, pct(rv, d), d))
        out.append("    ...all prod envs prevent self-review      {:>4} {}   (of {} with prod)".format(
            sr, pct(sr, d), d))
        out.append("    ...prod approver group of exactly 1       {:>4} {}   <- segregation risk".format(
            solo, pct(solo, d)))
    out.append("")
    out.append("  Prod-like pattern: {}".format(args.prod_pattern))
    unmatched = sorted({e for e in env_names_seen if not re.match(args.prod_pattern, e)})
    if unmatched:
        shown = unmatched[:25]
        out.append("  Environment names NOT classified prod-like ({} distinct):".format(
            len(unmatched)))
        out.append("    " + ", ".join(shown) + (" ..." if len(unmatched) > 25 else ""))
        out.append("  ^ read this list before publishing any number above.")
    out.append("")
    out.append("=" * 78)
    text = "\n".join(out)
    print(text)
    if args.summary_out:
        with open(args.summary_out, "w") as fh:
            fh.write(text + "\n")
        log("summary written to {}".format(args.summary_out))


def export_csv(store: Store, run_id: int, path: str) -> None:
    cols = [d[1] for d in store.query("PRAGMA table_info(repos)")]
    rows = store.query("SELECT * FROM repos WHERE run_id=? ORDER BY repo", (run_id,))
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        w.writerows(rows)
    log("csv written to {} ({} rows)".format(path, len(rows)))


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="gh-governance-collect.py",
        description="Collect org-wide GitHub governance posture into SQLite.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Authentication")[0].strip())
    p.add_argument("--org", required=True, help="organization login")
    p.add_argument("--db", default="governance.db", help="SQLite path (default: governance.db)")
    p.add_argument("--api", default=DEFAULT_API, help="API base URL (env: GITHUB_API)")
    p.add_argument("--app-id", help="GitHub App ID")
    p.add_argument("--private-key", help="path to the App's PEM private key")
    p.add_argument("--installation-id", help="App installation id (auto-discovered if omitted)")
    p.add_argument("--enterprise", help="enterprise slug, to resolve enterprise rulesets")
    p.add_argument("--workers", type=int, default=8, help="concurrent requests (default: 8)")
    p.add_argument("--rpm", type=int, default=700,
                   help="request ceiling per minute, under the ~900 secondary limit")
    p.add_argument("--limit", type=int, help="stop after N repos (smoke test)")
    p.add_argument("--include-archived", action="store_true")
    p.add_argument("--include-forks", action="store_true")
    p.add_argument("--prod-pattern", default=DEFAULT_PROD_PATTERN,
                   help="regex for production-like environment names")
    p.add_argument("--bypass-threshold", type=int, default=0,
                   help="bypass actors tolerated before a rule stops counting as enforced")
    p.add_argument("--properties", action="store_true",
                   help="also collect custom repository property values")
    p.add_argument("--expand-teams", action="store_true",
                   help="resolve environment approver teams to member counts")
    p.add_argument("--no-cache", action="store_true", help="ignore stored ETags")
    p.add_argument("--csv", help="also write the per-repo table to this CSV")
    p.add_argument("--summary", action="store_true", help="print the coverage rollup")
    p.add_argument("--summary-out", help="also write the rollup to this file")
    p.add_argument("--version", action="version", version=VERSION)
    args = p.parse_args(argv)

    args.api = args.api.rstrip("/")
    prod_re = re.compile(args.prod_pattern)

    auth = resolve_auth(args)
    store = Store(args.db)
    if args.no_cache:
        store.execute("DELETE FROM http_cache")
    pacer = Pacer(args.rpm)
    limiter = Limiter()
    client = Client(auth, store, args.api, pacer, limiter)

    log("auth: {}".format(auth.describe()))
    rl = client.get_json("/rate_limit") or {}
    core = (rl.get("resources") or {}).get("core") or {}
    if core:
        log("rate limit: {} of {} remaining".format(core.get("remaining"), core.get("limit")))

    started = now_iso()
    cur = store.execute(
        "INSERT INTO runs(org, started_at, tool_version, params) VALUES(?,?,?,?)",
        (args.org, started, VERSION, json.dumps(vars(args), default=str)))
    run_id = cur.lastrowid

    log("listing repositories in {} ...".format(args.org))
    repos = client.paginate("/orgs/{}/repos?per_page=100&type=all".format(args.org))
    log("{} repositories returned".format(len(repos)))

    def in_scope(r) -> bool:
        if r.get("archived") and not args.include_archived:
            return False
        if r.get("fork") and not args.include_forks:
            return False
        return not ((r.get("size") or 0) == 0 or not r.get("default_branch"))

    scoped = [r for r in repos if in_scope(r)]
    if args.limit:
        scoped = scoped[:args.limit]
    log("{} repositories in scope".format(len(scoped)))

    rulesets = RulesetCache(client, store, args.org, args.enterprise)
    results = []
    env_names_seen = set()
    done = 0

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(collect_repo, client, rulesets, args.org, r,
                               prod_re, args.bypass_threshold): r for r in scoped}
        for fut in as_completed(futures):
            row = fut.result()
            results.append(row)
            env_names_seen.update(e["name"] for e in row["envs"] if e["name"])
            done += 1
            if done % 100 == 0 or done == len(scoped):
                log("{}/{} repos   {} calls ({} not-modified)   {} quota left".format(
                    done, len(scoped), client.calls, client.not_modified,
                    limiter.remaining))

    out_of_scope = [r for r in repos if not in_scope(r)]
    repo_rows, rule_rows, env_rows = [], [], []

    for r in out_of_scope:
        repo_rows.append((run_id, args.org, r["name"], r.get("default_branch"),
                          r.get("visibility"), int(bool(r.get("archived"))),
                          int(bool(r.get("fork"))), int((r.get("size") or 0) == 0),
                          r.get("pushed_at"), 0) + (None,) * 18)

    for row in results:
        repo_rows.append((
            run_id, args.org, row["repo"], row["default_branch"], row["visibility"],
            row["archived"], row["fork"], row["is_empty"], row["pushed_at"], 1,
            row["pr_required"], row["pr_required_enforced"],
            row["self_review_blocked"], row["self_review_blocked_enforced"],
            row["required_approvals"], row["code_owner_review"],
            row["force_push_blocked"], row["force_push_blocked_enforced"],
            row["deletion_blocked"], row["deletion_blocked_enforced"],
            row["ruleset_count"], row["evaluate_only"], row["max_bypass_actors"],
            row["enforcement_unknown"], row["env_count"], row["prod_env_count"],
            row["prod_env_with_reviewers"], row["prod_env_self_review_blocked"],
            row["min_prod_approver_count"], row["error"]))
        for rule in row["rules"]:
            rule_rows.append((run_id, args.org, row["repo"], rule["type"], rule["ruleset_id"],
                              rule["ruleset_source"], rule["ruleset_source_type"],
                              rule["enforcement"], rule["bypass_actors"],
                              json.dumps(rule["parameters"])))
        for env in row["envs"]:
            env_rows.append((run_id, args.org, row["repo"], env["name"], env["is_prod_like"],
                             env["has_required_reviewers"], env["prevent_self_review"],
                             ",".join(x for x in env["reviewer_users"] if x),
                             ",".join(x for x in env["reviewer_teams"] if x),
                             len(env["reviewer_users"]) + len(env["reviewer_teams"]),
                             env["wait_timer"], env["branch_policy"]))

    store.executemany(
        "INSERT OR REPLACE INTO repos VALUES(" + ",".join("?" * 30) + ")", repo_rows)
    store.executemany(
        "INSERT INTO repo_rules VALUES(?,?,?,?,?,?,?,?,?,?)", rule_rows)
    store.executemany(
        "INSERT INTO environments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", env_rows)

    if args.expand_teams:
        slugs = sorted({s for row in results for e in row["envs"]
                        for s in e["reviewer_teams"] if s})
        log("expanding {} approver teams ...".format(len(slugs)))
        for slug in slugs:
            members = client.paginate(
                "/orgs/{}/teams/{}/members?per_page=100".format(args.org, slug))
            logins = [m.get("login") for m in members]
            store.execute(
                "INSERT INTO teams(org, slug, members, member_count, fetched_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(org, slug) DO UPDATE SET "
                "members=excluded.members, member_count=excluded.member_count, "
                "fetched_at=excluded.fetched_at",
                (args.org, slug, json.dumps(logins), len(logins), now_iso()))

    if args.properties:
        log("collecting custom repository properties ...")
        try:
            values = client.paginate(
                "/orgs/{}/properties/values?per_page=100".format(args.org))
            prop_rows = []
            for entry in values:
                for prop in entry.get("properties", []) or []:
                    prop_rows.append((run_id, args.org, entry.get("repository_name"),
                                      prop.get("property_name"),
                                      json.dumps(prop.get("value"))))
            store.executemany("INSERT INTO repo_properties VALUES(?,?,?,?,?)", prop_rows)
            log("{} property values recorded".format(len(prop_rows)))
        except Exception as exc:  # noqa: BLE001
            log("properties unavailable: {}".format(exc))

    used = None
    if limiter.first_remaining is not None and limiter.remaining is not None:
        used = limiter.first_remaining - limiter.remaining
    store.execute(
        "UPDATE runs SET finished_at=?, repos_total=?, repos_in_scope=?, repos_errored=?, "
        "api_calls=?, api_not_modified=?, rate_limit=?, rate_used=? WHERE run_id=?",
        (now_iso(), len(repos), len(scoped),
         sum(1 for r in results if r["error"]), client.calls, client.not_modified,
         limiter.limit, used, run_id))

    log("done: {} API calls, {} not-modified (free), {} quota consumed".format(
        client.calls, client.not_modified, used if used is not None else "?"))
    log("database: {}   run_id: {}".format(args.db, run_id))

    if args.csv:
        export_csv(store, run_id, args.csv)
    if args.summary:
        print_summary(store, run_id, args.org, args, env_names_seen)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
