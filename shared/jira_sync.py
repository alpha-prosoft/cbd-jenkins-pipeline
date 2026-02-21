#!/usr/bin/env python3
"""
JIRA Sync - Keeps commit messages and JIRA tickets in sync.

Subcommands:
  check  - Validates JIRA ticket status and syncs commit message with JIRA summary/description.
  close  - Transitions a JIRA ticket to "Done".

Usage:
  python3 jira_sync.py check --jira-url URL --jira-user USER --jira-password PW \\
                              --jira-issue ISSUE --gerrit-message MESSAGE
  python3 jira_sync.py close --jira-url URL --jira-user USER --jira-password PW \\
                              --jira-issue ISSUE
"""

import argparse
import json
import logging
import sys
import urllib.request
import urllib.error
import ssl
import base64

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(format=LOG_FORMAT, level=logging.INFO, stream=sys.stderr)
log = logging.getLogger("jira_sync")


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only)
# ---------------------------------------------------------------------------

def _make_auth_header(user, password):
    """Build a Basic-Auth header value."""
    token = base64.b64encode("{}:{}".format(user, password).encode()).decode()
    return "Basic {}".format(token)


def _ssl_context():
    """Return a permissive SSL context (mirrors the -k / --insecure flag
    used in the original shell scripts)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _jira_request(url, user, password, method="GET", data=None):
    """
    Perform an HTTP request against the JIRA REST API.

    Returns parsed JSON on success, raises on HTTP errors.
    """
    headers = {
        "Authorization": _make_auth_header(user, password),
        "Content-Type": "application/json",
    }
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, context=_ssl_context()) as resp:
            raw = resp.read().decode()
            if not raw:
                return {}
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode() if exc.fp else ""
        log.error("JIRA API %s %s failed (HTTP %s): %s", method, url, exc.code, error_body)
        raise
    except urllib.error.URLError as exc:
        log.error("JIRA API %s %s connection error: %s", method, url, exc.reason)
        raise


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _parse_gerrit_message(raw_message, jira_issue):
    """
    Split a Gerrit commit message into a summary and description.

    The first line is the summary (with the ``[JIRA-123] `` prefix stripped).
    Everything after the first line is the description.

    Returns (summary, description).
    """
    lines = raw_message.strip().splitlines()
    if not lines:
        return ("", "")

    # Strip the [JIRA-123] prefix from the first line
    first_line = lines[0]
    prefix = "[{}] ".format(jira_issue)
    if first_line.startswith(prefix):
        first_line = first_line[len(prefix):]

    summary = first_line.strip()
    description = "\n".join(lines[1:]).strip()
    return summary, description


def cmd_check(args):
    """
    Validate JIRA ticket state and sync commit message -> JIRA fields.

    Exit codes:
      0  - success (ticket is valid, fields updated if needed)
      1  - error (ticket not in correct state, API failure, etc.)
    """
    jira_url = args.jira_url.rstrip("/")
    issue_url = "{}/rest/api/2/issue/{}".format(jira_url, args.jira_issue)

    # -- Fetch JIRA issue ------------------------------------------------
    log.info("Fetching JIRA issue %s from %s", args.jira_issue, issue_url)
    try:
        issue = _jira_request(issue_url, args.jira_user, args.jira_password)
    except Exception:
        log.error("Failed to fetch JIRA issue %s", args.jira_issue)
        return 1

    # Handle JIRA error responses
    if "errorMessages" in issue:
        for msg in issue["errorMessages"]:
            log.error("JIRA error for %s: %s", args.jira_issue, msg)
        return 1

    fields = issue.get("fields", {})
    status_name = fields.get("status", {}).get("name", "Unknown")
    issue_type = fields.get("issuetype", {}).get("name", "Unknown")
    issue_summary = fields.get("summary", "")
    issue_description = fields.get("description", "") or ""

    log.info("JIRA %s  status=%s  type=%s", args.jira_issue, status_name, issue_type)

    # -- Validate status -------------------------------------------------
    if status_name != "In Progress":
        log.error(
            "JIRA issue %s must be 'In Progress' but is '%s'. URL: %s",
            args.jira_issue, status_name, issue_url,
        )
        return 1

    # -- Skip BUG / STORY ------------------------------------------------
    if issue_type.upper() in ("BUG", "STORY"):
        log.info("Issue type is '%s' - skipping commit-message sync.", issue_type)
        return 0

    # -- Parse commit message --------------------------------------------
    gerrit_summary, gerrit_description = _parse_gerrit_message(
        args.gerrit_message, args.jira_issue,
    )
    log.info("Commit summary:     %s", gerrit_summary)
    if gerrit_description:
        preview = gerrit_description[:120]
        if len(gerrit_description) > 120:
            preview += "..."
        log.info("Commit description: %s", preview)
    else:
        log.info("Commit description: (empty)")

    # -- Determine what needs updating -----------------------------------
    update_fields = {}

    if issue_summary != gerrit_summary:
        update_fields["summary"] = gerrit_summary
        log.info("Summary differs - will update JIRA summary.")

    if issue_description != gerrit_description:
        update_fields["description"] = gerrit_description
        log.info("Description differs - will update JIRA description.")

    if not update_fields:
        log.info("JIRA issue and commit message are already in sync. Nothing to update.")
        return 0

    # -- Update JIRA issue -----------------------------------------------
    log.info("Updating JIRA issue %s with fields: %s", args.jira_issue, list(update_fields.keys()))
    payload = {"fields": update_fields}

    try:
        _jira_request(issue_url, args.jira_user, args.jira_password, method="PUT", data=payload)
    except Exception:
        log.error("Failed to update JIRA issue %s", args.jira_issue)
        return 1

    log.info("JIRA issue %s updated successfully.", args.jira_issue)
    return 0


def cmd_close(args):
    """
    Transition a JIRA ticket to "Done".

    Exit codes:
      0  - success
      1  - error (transition not found, API failure, etc.)
    """
    jira_url = args.jira_url.rstrip("/")
    transitions_url = "{}/rest/api/2/issue/{}/transitions".format(jira_url, args.jira_issue)

    # -- Fetch available transitions -------------------------------------
    log.info("Fetching transitions for %s", args.jira_issue)
    try:
        data = _jira_request(transitions_url, args.jira_user, args.jira_password)
    except Exception:
        log.error("Failed to fetch transitions for %s", args.jira_issue)
        return 1

    transitions = data.get("transitions", [])
    done_transition = next((t for t in transitions if t.get("name") == "Done"), None)

    if done_transition is None:
        available = [t.get("name") for t in transitions]
        log.error(
            "No 'Done' transition found for %s. Available transitions: %s",
            args.jira_issue, available,
        )
        return 1

    done_id = done_transition["id"]
    log.info("Found 'Done' transition (id=%s) for %s", done_id, args.jira_issue)

    # -- Execute transition -----------------------------------------------
    payload = {"transition": {"id": done_id}}
    try:
        _jira_request(transitions_url, args.jira_user, args.jira_password, method="POST", data=payload)
    except Exception:
        log.error("Failed to transition %s to Done", args.jira_issue)
        return 1

    log.info("JIRA issue %s transitioned to Done.", args.jira_issue)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _add_common_args(parser):
    """Add arguments shared by all subcommands."""
    parser.add_argument("--jira-url", required=True,
                        help="Base JIRA URL (e.g. https://jira.example.com)")
    parser.add_argument("--jira-user", required=True,
                        help="JIRA username")
    parser.add_argument("--jira-password", required=True,
                        help="JIRA password")
    parser.add_argument("--jira-issue", required=True,
                        help="JIRA issue key (e.g. AP-123)")


def main():
    parser = argparse.ArgumentParser(
        description="JIRA Sync - keep commit messages and JIRA tickets in sync.",
    )
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # -- check -----------------------------------------------------------
    check_parser = subparsers.add_parser(
        "check", help="Validate ticket and sync commit message to JIRA")
    _add_common_args(check_parser)
    check_parser.add_argument("--gerrit-message", required=True,
                              help="Full Gerrit commit message")

    # -- close -----------------------------------------------------------
    close_parser = subparsers.add_parser(
        "close", help="Transition JIRA ticket to Done")
    _add_common_args(close_parser)

    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    if args.command == "check":
        return cmd_check(args)
    elif args.command == "close":
        return cmd_close(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
