#!/usr/bin/env python3
# Copyright 2024 The github_dashboard_collection authors.
#
# Licensed under the Apache License, Version 2.0 (the "License"): you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
#
# Modular input that collects GitHub Copilot AI-credit billing / usage data
# from the GitHub REST API and indexes it in Splunk.
#
# It feeds four reports:
#   1. Overall AI credit pool (allocation + consumed)  -> budgets endpoint
#   2. AI credit pool consumption over time            -> ai_credit/usage endpoint
#   3. Consumption by user                             -> ai_credit/usage?user=<login>
#   4. Usage grouped by model                          -> one event per usageItems entry
#
# Only the Python standard library and the bundled Splunk Python SDK
# (vendored under bin/lib/splunklib) are used. No third-party HTTP libraries.

import json
import os
import sys
import time as _time
from datetime import date, datetime, timedelta, timezone

# Make the vendored Splunk SDK importable regardless of the cwd Splunk uses.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

import urllib.error
import urllib.parse
import urllib.request

from splunklib.modularinput import Argument, Event, Scheme, Script

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

APP_NAME = "github_dashboard_collection"
KIND = "github_copilot_billing"

SOURCETYPE_BUDGET = "github:copilot:billing:budget"
SOURCETYPE_AI_CREDIT = "github:copilot:billing:ai_credit"
SOURCETYPE_AI_CREDIT_BY_USER = "github:copilot:billing:ai_credit_by_user"

DEFAULT_API_BASE_URL = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
ACCEPT_HEADER = "application/vnd.github+json"
USER_AGENT = "github_dashboard_collection-copilot-billing/1.0"

# GitHub only retains the last 24 months of billing data.
MAX_LOOKBACK_DAYS = 730
DEFAULT_LOOKBACK_DAYS = 30
# Bound the number of days collected in a single run so a large backfill does
# not run forever; the checkpoint advances so the next run continues.
MAX_DAYS_PER_RUN = 62

DEFAULT_PER_PAGE = 100


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _to_bool(value, default=False):
    """Coerce a Splunk-supplied value (string/None) to a bool."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "t", "y")


def _parse_date(value):
    """Parse a YYYY-MM-DD string into a datetime.date, or return None."""
    if not value:
        return None
    return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()


def _day_epoch(day):
    """Epoch seconds at 00:00:00 UTC for a datetime.date."""
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()


def _parse_link_header(link_header):
    """Return a dict of rel -> url parsed from an RFC 5988 Link header."""
    links = {}
    if not link_header:
        return links
    for part in link_header.split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        url = section[0].strip().lstrip("<").rstrip(">")
        for param in section[1:]:
            if "=" in param:
                k, v = param.strip().split("=", 1)
                if k.strip() == "rel":
                    links[v.strip().strip('"')] = url
    return links


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class GitHubRateLimitError(Exception):
    """Raised when the GitHub API keeps rate limiting us past our retries."""


class GitHubBillingClient:
    """Thin GitHub REST client built on urllib.

    The url opener is injectable so the client can be exercised with mocked
    HTTP responses (see ``--selftest``) without contacting GitHub.
    """

    def __init__(self, token, api_base_url=DEFAULT_API_BASE_URL, opener=None,
                 logger=None, max_retries=5):
        self.token = token
        self.base_url = (api_base_url or DEFAULT_API_BASE_URL).rstrip("/")
        self._opener = opener or urllib.request.build_opener()
        self._log = logger or (lambda level, msg: None)
        self.max_retries = max_retries

    def _headers(self):
        return {
            "Authorization": "Bearer %s" % self.token,
            "Accept": ACCEPT_HEADER,
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": USER_AGENT,
        }

    def _full_url(self, path_or_url, params=None):
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            url = path_or_url
        else:
            url = self.base_url + path_or_url
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        return url

    def request(self, path_or_url, params=None):
        """Perform a GET and return (status, body_dict_or_None, link_header).

        Returns status 404 with a ``None`` body when the resource is missing or
        the feature is not enabled, so callers can skip gracefully.
        """
        url = self._full_url(path_or_url, params)
        attempt = 0
        while True:
            attempt += 1
            req = urllib.request.Request(url, headers=self._headers(), method="GET")
            try:
                resp = self._opener.open(req, timeout=60)
                body = resp.read()
                link = resp.headers.get("Link") if hasattr(resp, "headers") else None
                data = json.loads(body.decode("utf-8")) if body else None
                return 200, data, link
            except urllib.error.HTTPError as err:
                status = err.code
                retry_after = self._retry_after_seconds(err)
                if status in (403, 429) and retry_after is not None \
                        and attempt <= self.max_retries:
                    self._log("WARN", "Rate limited (HTTP %s) on %s; sleeping %ss "
                              "(attempt %s/%s)" % (status, url, retry_after,
                                                   attempt, self.max_retries))
                    _time.sleep(retry_after)
                    continue
                if status == 404:
                    self._log("INFO", "HTTP 404 for %s (feature off or no data); "
                              "skipping" % url)
                    return 404, None, None
                if status in (403, 429):
                    detail = self._safe_read(err)
                    raise GitHubRateLimitError(
                        "HTTP %s for %s: %s" % (status, url, detail))
                detail = self._safe_read(err)
                raise RuntimeError("HTTP %s for %s: %s" % (status, url, detail))
            except urllib.error.URLError as err:
                if attempt <= self.max_retries:
                    backoff = min(60, 2 ** attempt)
                    self._log("WARN", "Network error on %s: %s; retrying in %ss"
                              % (url, err, backoff))
                    _time.sleep(backoff)
                    continue
                raise

    @staticmethod
    def _safe_read(err):
        try:
            return err.read().decode("utf-8", "replace")[:500]
        except Exception:  # noqa: BLE001 - best-effort error detail only
            return "<no body>"

    @staticmethod
    def _retry_after_seconds(err):
        """Determine how long to wait from Retry-After / X-RateLimit headers."""
        headers = getattr(err, "headers", None)
        if headers is None:
            return None
        retry_after = headers.get("Retry-After")
        if retry_after:
            try:
                return max(1, int(retry_after))
            except (TypeError, ValueError):
                return None
        remaining = headers.get("X-RateLimit-Remaining")
        reset = headers.get("X-RateLimit-Reset")
        if remaining is not None and str(remaining) == "0" and reset:
            try:
                wait = int(reset) - int(_time.time())
                return max(1, min(wait, 3600))
            except (TypeError, ValueError):
                return None
        # Secondary rate limits: back off a fixed amount when we can't tell.
        if err.code in (403, 429):
            return 60
        return None

    def paginate(self, path, params=None):
        """Yield each page body following ``rel="next"`` Link headers."""
        next_url = self._full_url(path, params)
        while next_url:
            status, body, link = self.request(next_url)
            if status == 404 or body is None:
                return
            yield body
            links = _parse_link_header(link)
            next_url = links.get("next")


# ---------------------------------------------------------------------------
# Checkpointing
# ---------------------------------------------------------------------------

class Checkpoint:
    """Persists the last-collected day per input under the checkpoint dir."""

    def __init__(self, checkpoint_dir, input_name):
        safe = "".join(c if c.isalnum() or c in ("-", "_", ".") else "_"
                       for c in input_name)
        self.path = None
        if checkpoint_dir:
            self.path = os.path.join(checkpoint_dir, "%s.json" % safe)

    def read_last_day(self):
        if not self.path or not os.path.exists(self.path):
            return None
        try:
            with open(self.path, "r") as fh:
                data = json.load(fh)
            return _parse_date(data.get("last_collected_day"))
        except (ValueError, OSError):
            return None

    def write_last_day(self, day):
        if not self.path:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"last_collected_day": day.isoformat()}, fh)
        os.replace(tmp, self.path)


# ---------------------------------------------------------------------------
# Credential lookup (Splunk storage/passwords)
# ---------------------------------------------------------------------------

def get_token_from_storage(session_key, server_uri, realm, app=APP_NAME):
    """Read the PAT from Splunk's encrypted credential store.

    Looks for a storage/passwords entry whose realm matches the input name
    (preferred) or the app name. Returns the clear-text password, or None.
    """
    import splunklib.client as client

    parts = urllib.parse.urlsplit(server_uri or "https://127.0.0.1:8089")
    service = client.Service(
        scheme=parts.scheme or "https",
        host=parts.hostname or "127.0.0.1",
        port=parts.port or 8089,
        token=("Splunk %s" % session_key) if session_key else None,
        owner="nobody",
        app=app,
    )
    return _select_password(service.storage_passwords, realm, app)


def _select_password(storage_passwords, realm, app):
    """Pick the best-matching credential from a storage/passwords collection."""
    by_realm = []
    by_app = []
    for sp in storage_passwords:
        sp_realm = getattr(sp, "realm", "") or ""
        if sp_realm == realm:
            by_realm.append(sp)
        elif sp_realm == app:
            by_app.append(sp)
    for candidate in by_realm + by_app:
        try:
            return candidate.clear_password
        except Exception:  # noqa: BLE001 - skip entries we cannot decrypt
            continue
    return None


# ---------------------------------------------------------------------------
# Collection logic
# ---------------------------------------------------------------------------

class BillingCollector:
    """Builds Splunk events from the GitHub billing endpoints."""

    def __init__(self, client, enterprise, input_name, emit, log):
        self.client = client
        self.enterprise = enterprise
        self.input_name = input_name
        self.emit = emit          # emit(sourcetype, time_epoch, payload_dict)
        self.log = log

    # -- budgets (report 1: overall AI credit pool) -------------------------

    def collect_budgets(self):
        count = 0
        now = _time.time()
        for page in self.client.paginate(
                "/enterprises/%s/settings/billing/budgets" % self.enterprise,
                params={"scope": "enterprise", "per_page": DEFAULT_PER_PAGE}):
            effective = page.get("effective_budget") or {}
            budgets = page.get("budgets") or []
            for budget in budgets:
                payload = dict(budget)
                payload["enterprise"] = self.enterprise
                payload["scope"] = "enterprise"
                # Surface the response-level effective budget (allocation +
                # consumed) on every budget event for easy reporting.
                if effective:
                    payload["effective_budget"] = effective
                    payload["effective_budget_amount"] = effective.get("budget_amount")
                    payload["effective_consumed_amount"] = \
                        effective.get("consumed_amount")
                self.emit(SOURCETYPE_BUDGET, now, payload)
                count += 1
            # If the enterprise has no explicit budgets but an effective pool
            # exists, still emit the pool so the allocation/consumed is captured.
            if not budgets and effective:
                payload = {
                    "enterprise": self.enterprise,
                    "scope": "enterprise",
                    "effective_budget": effective,
                    "effective_budget_amount": effective.get("budget_amount"),
                    "effective_consumed_amount": effective.get("consumed_amount"),
                }
                self.emit(SOURCETYPE_BUDGET, now, payload)
                count += 1
        self.log("INFO", "Collected %s budget event(s)" % count)
        return count

    # -- ai credit usage (reports 2 & 4: over time, by model) ---------------

    def _emit_usage_items(self, body, day, sourcetype, extra=None):
        """Emit one event per usageItems entry (so model grouping works)."""
        if not body:
            return 0
        time_period = body.get("timePeriod") or body.get("time_period")
        usage_items = body.get("usageItems") or body.get("usage_items") or []
        epoch = _day_epoch(day)
        count = 0
        for item in usage_items:
            payload = dict(item)
            payload["enterprise"] = body.get("enterprise", self.enterprise)
            payload["scope"] = "enterprise"
            payload["date"] = day.isoformat()
            payload["time_period"] = {
                "year": day.year, "month": day.month, "day": day.day,
            }
            if time_period is not None:
                payload["api_time_period"] = time_period
            if extra:
                payload.update(extra)
            self.emit(sourcetype, epoch, payload)
            count += 1
        return count

    def collect_ai_credit_for_day(self, day):
        params = {"year": day.year, "month": day.month, "day": day.day}
        status, body, _ = self.client.request(
            "/enterprises/%s/settings/billing/ai_credit/usage" % self.enterprise,
            params=params)
        if status == 404 or body is None:
            return 0
        return self._emit_usage_items(body, day, SOURCETYPE_AI_CREDIT)

    # -- per-user usage (report 3: consumption by user) ---------------------

    def list_seat_logins(self):
        logins = []
        for page in self.client.paginate(
                "/enterprises/%s/copilot/billing/seats" % self.enterprise,
                params={"per_page": DEFAULT_PER_PAGE}):
            for seat in (page.get("seats") or []):
                assignee = seat.get("assignee") or {}
                login = assignee.get("login")
                if login:
                    logins.append(login)
        # De-duplicate while preserving order.
        seen = set()
        unique = []
        for login in logins:
            if login not in seen:
                seen.add(login)
                unique.append(login)
        self.log("INFO", "Found %s Copilot seat login(s)" % len(unique))
        return unique

    def collect_by_user_for_day(self, day, logins):
        total = 0
        for login in logins:
            params = {"year": day.year, "month": day.month, "day": day.day,
                      "user": login}
            status, body, _ = self.client.request(
                "/enterprises/%s/settings/billing/ai_credit/usage"
                % self.enterprise, params=params)
            if status == 404 or body is None:
                continue
            total += self._emit_usage_items(
                body, day, SOURCETYPE_AI_CREDIT_BY_USER, extra={"user": login})
        return total


# ---------------------------------------------------------------------------
# Date range planning
# ---------------------------------------------------------------------------

def plan_day_range(last_collected, start_date, lookback_days, today=None):
    """Return the inclusive list of days to collect for this run.

    First run backfills from ``start_date`` (or ``lookback_days`` ago); later
    runs resume the day after the last collected day. The range is clamped to
    the 24-month retention window and bounded by ``MAX_DAYS_PER_RUN``.
    """
    today = today or date.today()
    end_day = today - timedelta(days=1)  # data lags; collect through yesterday
    earliest_allowed = today - timedelta(days=MAX_LOOKBACK_DAYS)

    if last_collected is not None:
        start = last_collected + timedelta(days=1)
    elif start_date is not None:
        start = start_date
    else:
        start = today - timedelta(days=lookback_days)

    if start < earliest_allowed:
        start = earliest_allowed
    if start > end_day:
        return []

    days = []
    cursor = start
    while cursor <= end_day and len(days) < MAX_DAYS_PER_RUN:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


# ---------------------------------------------------------------------------
# Modular input
# ---------------------------------------------------------------------------

class GithubCopilotBilling(Script):

    def get_scheme(self):
        scheme = Scheme("GitHub Copilot Billing")
        scheme.description = ("Collects GitHub Copilot AI-credit billing and "
                              "usage data for an enterprise.")
        scheme.use_external_validation = True
        scheme.use_single_instance = False

        enterprise = Argument("enterprise")
        enterprise.title = "Enterprise slug"
        enterprise.description = "The slug of the GitHub enterprise (e.g. my-ent)."
        enterprise.data_type = Argument.data_type_string
        enterprise.required_on_create = True
        scheme.add_argument(enterprise)

        api_base = Argument("api_base_url")
        api_base.title = "API base URL"
        api_base.description = ("Base URL of the GitHub REST API. Defaults to "
                                "https://api.github.com. For GHES use "
                                "https://HOST/api/v3.")
        api_base.data_type = Argument.data_type_string
        api_base.required_on_create = False
        scheme.add_argument(api_base)

        for name, title in (
                ("collect_budgets", "Collect budgets"),
                ("collect_ai_credit", "Collect AI credit usage"),
                ("collect_by_user", "Collect AI credit usage by user")):
            arg = Argument(name)
            arg.title = title
            arg.data_type = Argument.data_type_boolean
            arg.required_on_create = False
            scheme.add_argument(arg)

        start_date = Argument("start_date")
        start_date.title = "Start date (YYYY-MM-DD)"
        start_date.description = ("First day to backfill on the initial run. "
                                  "Limited to the past 24 months.")
        start_date.data_type = Argument.data_type_string
        start_date.required_on_create = False
        scheme.add_argument(start_date)

        lookback = Argument("lookback_days")
        lookback.title = "Lookback days"
        lookback.description = ("Days to backfill on the first run when no "
                                "start_date is set (default 30).")
        lookback.data_type = Argument.data_type_number
        lookback.required_on_create = False
        scheme.add_argument(lookback)

        return scheme

    def validate_input(self, validation_definition):
        params = validation_definition.parameters
        enterprise = (params.get("enterprise") or "").strip()
        if not enterprise:
            raise ValueError("'enterprise' must not be empty")

        api_base = (params.get("api_base_url") or "").strip()
        if api_base:
            parsed = urllib.parse.urlsplit(api_base)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError("'api_base_url' must be an http(s) URL")

        start_date = (params.get("start_date") or "").strip()
        if start_date:
            try:
                _parse_date(start_date)
            except ValueError:
                raise ValueError("'start_date' must be in YYYY-MM-DD format")

        lookback = (params.get("lookback_days") or "").strip()
        if lookback:
            try:
                if int(lookback) < 1:
                    raise ValueError
            except ValueError:
                raise ValueError("'lookback_days' must be a positive integer")

    def stream_events(self, inputs, ew):
        for stanza_name, stanza in inputs.inputs.items():
            input_name = stanza_name.split("://", 1)[-1]

            def log(level, msg, _name=input_name):
                ew.log(level, "[%s] %s" % (_name, msg))

            try:
                self._run_input(inputs, ew, input_name, stanza, log)
            except Exception as err:  # noqa: BLE001 - report and keep other inputs alive
                ew.log(ew.ERROR if hasattr(ew, "ERROR") else "ERROR",
                       "[%s] collection failed: %s" % (input_name, err))

    def _run_input(self, inputs, ew, input_name, stanza, log):
        enterprise = (stanza.get("enterprise") or "").strip()
        if not enterprise:
            log("ERROR", "missing 'enterprise'; skipping")
            return

        api_base_url = (stanza.get("api_base_url") or DEFAULT_API_BASE_URL).strip()
        collect_budgets = _to_bool(stanza.get("collect_budgets"), True)
        collect_ai_credit = _to_bool(stanza.get("collect_ai_credit"), True)
        collect_by_user = _to_bool(stanza.get("collect_by_user"), False)
        start_date = _parse_date(stanza.get("start_date"))
        try:
            lookback_days = int(stanza.get("lookback_days") or DEFAULT_LOOKBACK_DAYS)
        except (TypeError, ValueError):
            lookback_days = DEFAULT_LOOKBACK_DAYS
        lookback_days = max(1, min(lookback_days, MAX_LOOKBACK_DAYS))

        session_key = inputs.metadata.get("session_key")
        server_uri = inputs.metadata.get("server_uri")
        token = get_token_from_storage(session_key, server_uri, input_name)
        if not token:
            log("ERROR", "no PAT found in storage/passwords for realm '%s' (or "
                "app '%s'); store the token then re-run" % (input_name, APP_NAME))
            return

        client = GitHubBillingClient(token, api_base_url, logger=log)

        def emit(sourcetype, time_epoch, payload):
            ew.write_event(Event(
                data=json.dumps(payload, separators=(",", ":")),
                stanza=None,
                time="%.3f" % time_epoch,
                sourcetype=sourcetype,
            ))

        collector = BillingCollector(client, enterprise, input_name, emit, log)

        if collect_budgets:
            collector.collect_budgets()

        if not (collect_ai_credit or collect_by_user):
            return

        checkpoint = Checkpoint(inputs.metadata.get("checkpoint_dir"), input_name)
        last_collected = checkpoint.read_last_day()
        days = plan_day_range(last_collected, start_date, lookback_days)
        if not days:
            log("INFO", "no new days to collect")
            return
        log("INFO", "collecting %s day(s): %s..%s"
            % (len(days), days[0].isoformat(), days[-1].isoformat()))

        logins = collector.list_seat_logins() if collect_by_user else []

        for day in days:
            if collect_ai_credit:
                collector.collect_ai_credit_for_day(day)
            if collect_by_user and logins:
                collector.collect_by_user_for_day(day, logins)
            checkpoint.write_last_day(day)


# ---------------------------------------------------------------------------
# Self-test (offline, no Splunk required)
# ---------------------------------------------------------------------------

def _run_selftest():
    """Exercise parsing / pagination / event building with mocked HTTP."""

    class FakeHeaders(dict):
        def get(self, key, default=None):
            for k, v in self.items():
                if k.lower() == key.lower():
                    return v
            return default

    class FakeResponse:
        def __init__(self, body, link=None):
            self._body = json.dumps(body).encode("utf-8")
            self.headers = FakeHeaders({"Link": link} if link else {})

        def read(self):
            return self._body

    class FakeOpener:
        def __init__(self, routes):
            self.routes = routes

        def open(self, req, timeout=None):
            url = req.full_url
            path = url.split("https://api.github.com")[-1]
            for matcher, response in self.routes:
                if matcher in path:
                    return response
            raise urllib.error.HTTPError(url, 404, "not found", {}, None)

    seats_page = {"seats": [{"assignee": {"login": "octocat"}}]}
    budgets_page = {
        "budgets": [{"id": "b1", "budget_amount": 500, "budget_scope": "enterprise",
                     "budget_product_sku": "ai_credits"}],
        "effective_budget": {"budget_amount": 500, "consumed_amount": 123.45},
    }
    usage_page = {
        "enterprise": "acme",
        "timePeriod": {"year": 2024, "month": 1, "day": 2},
        "usageItems": [
            {"sku": "ai_credits", "model": "gpt-4o", "unitType": "credit",
             "grossQuantity": 10, "grossAmount": 1.0, "netQuantity": 9,
             "netAmount": 0.9, "pricePerUnit": 0.1},
            {"sku": "ai_credits", "model": "claude", "unitType": "credit",
             "grossQuantity": 5, "grossAmount": 0.5, "netQuantity": 5,
             "netAmount": 0.5, "pricePerUnit": 0.1},
        ],
    }
    opener = FakeOpener([
        ("/settings/billing/budgets", FakeResponse(budgets_page)),
        ("/copilot/billing/seats", FakeResponse(seats_page)),
        ("/settings/billing/ai_credit/usage", FakeResponse(usage_page)),
    ])

    client = GitHubBillingClient("dummy-token", opener=opener)
    events = []

    def emit(sourcetype, time_epoch, payload):
        events.append((sourcetype, time_epoch, payload))

    def log(level, msg):
        pass

    collector = BillingCollector(client, "acme", "selftest", emit, log)

    n_budget = collector.collect_budgets()
    assert n_budget == 1, "expected 1 budget event, got %s" % n_budget
    assert events[0][2]["effective_consumed_amount"] == 123.45

    day = date(2024, 1, 2)
    n_credit = collector.collect_ai_credit_for_day(day)
    assert n_credit == 2, "expected 2 ai_credit events, got %s" % n_credit
    models = {e[2].get("model") for e in events if e[0] == SOURCETYPE_AI_CREDIT}
    assert models == {"gpt-4o", "claude"}, "model grouping broken: %s" % models

    logins = collector.list_seat_logins()
    assert logins == ["octocat"], "seat logins wrong: %s" % logins
    n_user = collector.collect_by_user_for_day(day, logins)
    assert n_user == 2, "expected 2 by-user events, got %s" % n_user
    user_events = [e for e in events if e[0] == SOURCETYPE_AI_CREDIT_BY_USER]
    assert all(e[2]["user"] == "octocat" for e in user_events)

    # Date-range planning.
    assert plan_day_range(date(2024, 1, 1), None, 30, today=date(2024, 1, 5)) == \
        [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    assert plan_day_range(None, None, 2, today=date(2024, 1, 5)) == \
        [date(2024, 1, 3), date(2024, 1, 4)]
    assert plan_day_range(date(2024, 1, 4), None, 30, today=date(2024, 1, 5)) == []

    # Link-header parsing.
    links = _parse_link_header('<https://api.github.com/x?page=2>; rel="next", '
                               '<https://api.github.com/x?page=5>; rel="last"')
    assert links.get("next", "").endswith("page=2"), links

    print("SELFTEST OK: %s events emitted across 3 sourcetypes" % len(events))
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_run_selftest())
    sys.exit(GithubCopilotBilling().run(sys.argv))
