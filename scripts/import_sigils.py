#!/usr/bin/env python3
"""Connect NI indices to and import OpenSearch-compatible Sigils dashboards."""

import argparse
import base64
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid


DEFAULT_DASHBOARDS = (
    "overview",
    "git",
    "github_issues",
    "github_pull_requests",
)
SIGILS_BASE_URL = (
    "https://raw.githubusercontent.com/chaoss/grimoirelab-sigils/"
    "{ref}/panels/json/opensearch_dashboards/{dashboard}.ndjson"
)
ALIASES = (
    ("git_ni_enriched", "git", True),
    ("github_ni_enriched", "github_issues", True),
    ("git-aoc_ni_enriched", "git_areas_of_code", False),
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Create Sigils aliases and import dashboard saved objects."
    )
    parser.add_argument(
        "dashboards",
        nargs="*",
        default=DEFAULT_DASHBOARDS,
        help="Sigils dashboard names without the .ndjson suffix",
    )
    parser.add_argument(
        "--opensearch-url",
        default="https://localhost:9200",
        help="OpenSearch base URL (default: %(default)s)",
    )
    parser.add_argument(
        "--dashboards-url",
        default="http://localhost:5601",
        help="OpenSearch Dashboards base URL (default: %(default)s)",
    )
    parser.add_argument("--username", default=os.environ.get("OPENSEARCH_USERNAME", "admin"))
    parser.add_argument(
        "--password",
        default=os.environ.get("OPENSEARCH_PASSWORD", "GrimoireLab.1"),
        help="Defaults to OPENSEARCH_PASSWORD or the local Compose password",
    )
    parser.add_argument(
        "--sigils-ref",
        default="main",
        help="Sigils Git ref to download (default: %(default)s)",
    )
    parser.add_argument(
        "--default-index",
        default="git",
        help="Default OpenSearch Dashboards data-view ID (default: %(default)s)",
    )
    parser.add_argument(
        "--time-from",
        default="now-5y",
        help="Default dashboard time-range start (default: %(default)s)",
    )
    parser.add_argument(
        "--time-to",
        default="now",
        help="Default dashboard time-range end (default: %(default)s)",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification for local self-signed certificates",
    )
    return parser.parse_args()


def request(url, username, password, *, data=None, headers=None, method=None, insecure=False):
    request_headers = dict(headers or {})
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    request_headers["Authorization"] = f"Basic {credentials}"
    req = urllib.request.Request(
        url, data=data, headers=request_headers, method=method
    )
    context = ssl._create_unverified_context() if insecure else None
    return urllib.request.urlopen(req, context=context)


def index_exists(args, index):
    url = f"{args.opensearch_url.rstrip('/')}/{urllib.parse.quote(index, safe='')}"
    try:
        with request(
            url,
            args.username,
            args.password,
            method="HEAD",
            insecure=args.insecure,
        ):
            return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise


def alias_indices(args, alias):
    url = (
        f"{args.opensearch_url.rstrip('/')}/_alias/"
        f"{urllib.parse.quote(alias, safe='')}"
    )
    try:
        with request(
            url,
            args.username,
            args.password,
            insecure=args.insecure,
        ) as response:
            return set(json.load(response))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return set()
        raise


def connect_aliases(args):
    actions = []
    for index, alias, required in ALIASES:
        if index_exists(args, index):
            for existing_index in alias_indices(args, alias) - {index}:
                actions.append(
                    {"remove": {"index": existing_index, "alias": alias}}
                )
            actions.append({"add": {"index": index, "alias": alias}})
        elif required:
            raise RuntimeError(f"required index does not exist: {index}")
        else:
            print(f"[sigils] optional index not found; skipping alias {alias} -> {index}")

    payload = json.dumps({"actions": actions}).encode()
    url = f"{args.opensearch_url.rstrip('/')}/_aliases"
    with request(
        url,
        args.username,
        args.password,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
        insecure=args.insecure,
    ) as response:
        result = json.load(response)
    if not result.get("acknowledged"):
        raise RuntimeError(f"OpenSearch did not acknowledge alias changes: {result}")

    for action in actions:
        if "remove" in action:
            remove = action["remove"]
            print(
                f"[sigils] disconnected {remove['alias']} "
                f"from stale index {remove['index']}"
            )
        else:
            add = action["add"]
            print(f"[sigils] connected {add['alias']} -> {add['index']}")


def download_dashboard(dashboard, ref):
    if not dashboard.replace("_", "").replace("-", "").isalnum():
        raise ValueError(f"invalid dashboard name: {dashboard}")
    url = SIGILS_BASE_URL.format(
        ref=urllib.parse.quote(ref, safe="/"),
        dashboard=urllib.parse.quote(dashboard, safe=""),
    )
    with urllib.request.urlopen(url) as response:
        return response.read()


def multipart_file(filename, content):
    boundary = f"----grimoirelab-{uuid.uuid4().hex}"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/ndjson\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def import_dashboard(args, dashboard):
    content = download_dashboard(dashboard, args.sigils_ref)
    filename = f"{dashboard}.ndjson"
    body, content_type = multipart_file(filename, content)
    url = f"{args.dashboards_url.rstrip('/')}/api/saved_objects/_import?overwrite=true"
    with request(
        url,
        args.username,
        args.password,
        data=body,
        headers={"Content-Type": content_type, "osd-xsrf": "true"},
        method="POST",
        insecure=args.insecure,
    ) as response:
        result = json.load(response)
    if not result.get("success"):
        errors = json.dumps(result.get("errors", result), indent=2)
        raise RuntimeError(f"failed to import {filename}:\n{errors}")
    print(
        f"[sigils] imported {filename}: "
        f"{result.get('successCount', 0)} saved objects"
    )


def configure_dashboard_defaults(args):
    time_defaults = json.dumps(
        {"from": args.time_from, "to": args.time_to},
        separators=(",", ":"),
    )
    payload = json.dumps(
        {
            "changes": {
                "defaultIndex": args.default_index,
                "timepicker:timeDefaults": time_defaults,
            }
        }
    ).encode()
    url = f"{args.dashboards_url.rstrip('/')}/api/opensearch-dashboards/settings"
    with request(
        url,
        args.username,
        args.password,
        data=payload,
        headers={"Content-Type": "application/json", "osd-xsrf": "true"},
        method="POST",
        insecure=args.insecure,
    ) as response:
        result = json.load(response)

    settings = result.get("settings", {})
    if settings.get("defaultIndex", {}).get("userValue") != args.default_index:
        raise RuntimeError("OpenSearch Dashboards did not save the default data view")
    if (
        settings.get("timepicker:timeDefaults", {}).get("userValue")
        != time_defaults
    ):
        raise RuntimeError("OpenSearch Dashboards did not save the default time range")
    print(
        f"[sigils] configured default data view {args.default_index} "
        f"and time range {args.time_from} to {args.time_to}"
    )


def main():
    args = parse_args()
    try:
        connect_aliases(args)
        for dashboard in args.dashboards:
            import_dashboard(args, dashboard)
        configure_dashboard_defaults(args)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()
