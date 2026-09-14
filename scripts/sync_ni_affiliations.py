#!/usr/bin/env python3
"""Enroll contributing NI GitHub organization members in SortingHat.

The GitHub member roster is kept in memory and is never written to disk. The
script previews changes by default; --apply and an explicit --from-date are
required to mutate SortingHat.
"""

import argparse
import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


GITHUB_API = "https://api.github.com"
DEFAULT_SORTINGHAT_URL = "http://localhost:8000/identities/api/"
PAGE_SIZE = 100

INDIVIDUALS_QUERY = """
query Individuals($page: Int!, $pageSize: Int!) {
  individuals(page: $page, pageSize: $pageSize, filters: {source: "github"}) {
    entities {
      mk
      identities {
        source
        username
      }
      enrollments {
        group {
          name
        }
        start
        end
      }
    }
    pageInfo {
      hasNext
      page
    }
  }
}
"""

ORGANIZATION_QUERY = """
query Organization($name: String!) {
  organizations(page: 1, pageSize: 10, filters: {name: $name}) {
    entities {
      name
    }
  }
}
"""

ADD_ORGANIZATION_MUTATION = """
mutation AddOrganization($name: String!) {
  addOrganization(name: $name) {
    organization {
      name
    }
  }
}
"""

ENROLL_MUTATION = """
mutation Enroll(
  $uuid: String!,
  $group: String!,
  $fromDate: DateTime!,
  $toDate: DateTime!
) {
  enroll(
    uuid: $uuid,
    group: $group,
    fromDate: $fromDate,
    toDate: $toDate
  ) {
    uuid
  }
}
"""

TOKEN_MUTATION = """
mutation Token($username: String!, $password: String!) {
  tokenAuth(username: $username, password: $password) {
    token
  }
}
"""


class SyncError(Exception):
    """Raised when the affiliation sync cannot continue safely."""


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Preview or apply SortingHat enrollments for contributors who are "
            "members of the authenticated GitHub organization."
        )
    )
    parser.add_argument("--github-org", default="ni")
    parser.add_argument("--organization", default="NI")
    parser.add_argument(
        "--sortinghat-url",
        default=DEFAULT_SORTINGHAT_URL,
        help="SortingHat GraphQL endpoint (default: %(default)s)",
    )
    parser.add_argument(
        "--sortinghat-username",
        default=os.environ.get("SORTINGHAT_USERNAME", "admin"),
    )
    parser.add_argument(
        "--minimum-members",
        type=int,
        default=1000,
        help="Abort below this count to detect incomplete org visibility",
    )
    parser.add_argument(
        "--from-date",
        help="Enrollment start date in YYYY-MM-DD format; required with --apply",
    )
    parser.add_argument(
        "--to-date",
        default="2100-01-01",
        help="Enrollment end date in YYYY-MM-DD format (default: %(default)s)",
    )
    parser.add_argument(
        "--include-already-enrolled",
        action="store_true",
        help="Allow candidates that already have a non-NI enrollment",
    )
    parser.add_argument(
        "--show-logins",
        action="store_true",
        help="Print matched GitHub logins; hidden by default",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create the organization if needed and apply proposed enrollments",
    )
    return parser.parse_args()


def parse_date(value, option):
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise SyncError(f"{option} must use YYYY-MM-DD format") from exc


def require_environment(name, default=None):
    value = os.environ.get(name, default)
    if not value:
        raise SyncError(f"set {name} before running this command")
    return value


def request_json(url, *, data=None, headers=None):
    request = urllib.request.Request(
        url,
        data=json.dumps(data).encode() if data is not None else None,
        headers=headers or {},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request) as response:
            return json.load(response), response.headers
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise SyncError(f"{url} returned HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise SyncError(f"unable to connect to {url}: {exc.reason}") from exc


def github_members(org, token):
    members = set()
    page = 1
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "grimoirelab-ni-affiliation-sync",
    }

    while True:
        query = urllib.parse.urlencode({"per_page": PAGE_SIZE, "page": page})
        url = f"{GITHUB_API}/orgs/{urllib.parse.quote(org)}/members?{query}"
        result, _ = request_json(url, headers=headers)
        if not isinstance(result, list):
            raise SyncError("GitHub members response was not a list")
        members.update(member["login"].casefold() for member in result)
        if len(result) < PAGE_SIZE:
            break
        page += 1
    return members


class SortingHatClient:
    def __init__(self, url, username, password):
        self.url = url
        self.username = username
        self.password = password
        self.token = None

    def execute(self, query, variables=None, authenticate=True):
        headers = {"Content-Type": "application/json"}
        if authenticate:
            if not self.token:
                self.authenticate()
            headers["Authorization"] = f"JWT {self.token}"
        result, _ = request_json(
            self.url,
            data={"query": query, "variables": variables or {}},
            headers=headers,
        )
        if result.get("errors"):
            messages = "; ".join(error["message"] for error in result["errors"])
            raise SyncError(f"SortingHat GraphQL error: {messages}")
        return result["data"]

    def authenticate(self):
        data = self.execute(
            TOKEN_MUTATION,
            {"username": self.username, "password": self.password},
            authenticate=False,
        )
        self.token = data["tokenAuth"]["token"]

    def individuals(self):
        entities = []
        page = 1
        while True:
            data = self.execute(
                INDIVIDUALS_QUERY,
                {"page": page, "pageSize": PAGE_SIZE},
            )
            result = data["individuals"]
            entities.extend(result["entities"])
            if not result["pageInfo"]["hasNext"]:
                return entities
            page += 1

    def organization_exists(self, name):
        data = self.execute(ORGANIZATION_QUERY, {"name": name})
        return any(
            entity["name"].casefold() == name.casefold()
            for entity in data["organizations"]["entities"]
        )

    def add_organization(self, name):
        self.execute(ADD_ORGANIZATION_MUTATION, {"name": name})

    def enroll(self, uuid, organization, from_date, to_date):
        self.execute(
            ENROLL_MUTATION,
            {
                "uuid": uuid,
                "group": organization,
                "fromDate": from_date,
                "toDate": to_date,
            },
        )


def build_plan(individuals, members, organization, include_already_enrolled):
    matched = []
    already_enrolled = []
    conflicting = []

    for individual in individuals:
        usernames = {
            identity["username"].casefold()
            for identity in individual["identities"]
            if identity["source"].casefold() == "github" and identity.get("username")
        }
        matching_usernames = usernames & members
        if not matching_usernames:
            continue

        enrollments = individual["enrollments"]
        if any(
            enrollment["group"]["name"].casefold() == organization.casefold()
            for enrollment in enrollments
        ):
            already_enrolled.append((individual, matching_usernames))
        elif enrollments and not include_already_enrolled:
            conflicting.append((individual, matching_usernames))
        else:
            matched.append((individual, matching_usernames))

    return matched, already_enrolled, conflicting


def print_logins(label, entries):
    if entries:
        logins = sorted(
            login
            for _, matching_usernames in entries
            for login in matching_usernames
        )
        print(f"{label}: {', '.join(logins)}")


def main():
    args = parse_args()
    try:
        if args.minimum_members < 1:
            raise SyncError("--minimum-members must be greater than zero")
        if args.apply and not args.from_date:
            raise SyncError("--from-date is required with --apply")
        if args.from_date:
            from_date = parse_date(args.from_date, "--from-date")
            to_date = parse_date(args.to_date, "--to-date")
            if from_date > to_date:
                raise SyncError("--from-date must not be after --to-date")

        github_token = require_environment("GITHUB_TOKEN", os.environ.get("GH_TOKEN"))
        sortinghat_password = require_environment(
            "SORTINGHAT_PASSWORD",
            "admin" if args.sortinghat_url == DEFAULT_SORTINGHAT_URL else None,
        )

        members = github_members(args.github_org, github_token)
        if len(members) < args.minimum_members:
            raise SyncError(
                f"only {len(members)} GitHub members were visible; expected at least "
                f"{args.minimum_members}. Verify token access and read:org scope."
            )

        client = SortingHatClient(
            args.sortinghat_url,
            args.sortinghat_username,
            sortinghat_password,
        )
        individuals = client.individuals()
        proposed, already_enrolled, conflicting = build_plan(
            individuals,
            members,
            args.organization,
            args.include_already_enrolled,
        )

        print(f"GitHub organization members visible: {len(members)}")
        print(f"SortingHat individuals with GitHub identities: {len(individuals)}")
        matched_count = len(proposed) + len(already_enrolled) + len(conflicting)
        print(f"Matched contributing organization members: {matched_count}")
        print(f"Already enrolled in {args.organization}: {len(already_enrolled)}")
        print(f"Proposed new {args.organization} enrollments: {len(proposed)}")
        print(f"Skipped due to existing non-{args.organization} enrollments: {len(conflicting)}")

        if args.show_logins:
            print_logins("Proposed member logins", proposed)
            print_logins("Conflicting member logins", conflicting)

        if not args.apply:
            print("Preview only; no SortingHat data was changed.")
            return

        if not client.organization_exists(args.organization):
            client.add_organization(args.organization)
            print(f"Created SortingHat organization: {args.organization}")

        applied = 0
        for individual, matching_usernames in proposed:
            try:
                client.enroll(
                    individual["mk"],
                    args.organization,
                    args.from_date,
                    args.to_date,
                )
            except SyncError as exc:
                identity = individual["mk"]
                if args.show_logins:
                    identity = ",".join(sorted(matching_usernames))
                raise SyncError(
                    f"enrollment failed after {applied} successful updates "
                    f"for {identity}: {exc}"
                ) from exc
            applied += 1

        print(f"Applied {applied} {args.organization} enrollments.")
    except (KeyError, TypeError, SyncError) as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()
