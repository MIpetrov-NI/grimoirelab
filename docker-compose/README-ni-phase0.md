# NI OSS Health Dashboard - Local Run Guide

Stand up the GrimoireLab pipeline locally against a **Tier-1 subset** of the NI org
(top 10 public repos by stars). Goal: prove collection -> enrichment -> OpenSearch
end to end before scaling to all ~219 public repos.

This runs inside the GrimoireLab **fork** (the tool). The companion context repo
(`oss-health-dashboard`) only holds knowledge/docs and does not run anything.

## Prerequisites

- Docker + Docker Compose.
- A GitHub token (the repo owner already has one via `gh`). Scopes needed: `repo`
  (public), `read:org`. Classic or fine-grained both work for public read.
- Python 3.10+ (only to (re)generate config; not needed to run the containers).

## What each file is

| File | Committed? | Purpose |
|------|-----------|---------|
| `default-grimoirelab-settings/projects.json` | yes | Repo list GrimoireLab collects (generated). |
| `default-grimoirelab-settings/setup-ni.cfg.template` | yes | SirMordred config with a `__GITHUB_TOKEN__` placeholder. |
| `default-grimoirelab-settings/setup-ni.cfg` | **no (git-ignored)** | Runtime config with the real token. Mounted into mordred. |
| `default-grimoirelab-settings/aliases-ni.json` | yes | Maps NI physical indices to the stable names used by Sigils. |
| `scripts/gen_projects.py` | yes | Regenerates `projects.json` from the live org. |
| `scripts/render_setup.py` | yes | Injects the token into the runtime config. |
| `scripts/import_sigils.py` | yes | Creates dashboard aliases and imports OpenSearch Sigils saved objects. |
| `scripts/sync_ni_affiliations.py` | yes | Previews or enrolls contributing NI GitHub members in SortingHat. |

## Steps

Run from the fork root.

### 1. (Optional) Regenerate the repo list

Already committed, but to refresh from the live org:

```bash
GITHUB_TOKEN=$(gh auth token) \
  python scripts/gen_projects.py --org ni --tier 1 --top 10 \
    --output default-grimoirelab-settings/projects.json
```

### 2. Render the runtime config (injects your token)

```bash
GITHUB_TOKEN=$(gh auth token) \
  python scripts/render_setup.py \
    default-grimoirelab-settings/setup-ni.cfg.template \
    default-grimoirelab-settings/setup-ni.cfg
```

The token is written only into the git-ignored `setup-ni.cfg`. Confirm it will not be
committed:

```bash
git check-ignore default-grimoirelab-settings/setup-ni.cfg   # prints the path => ignored
```

### 3. Bring up the stack

```bash
cd docker-compose
docker compose up -d
```

Services: `mariadb`, `valkey`, `opensearch` (9200), `opensearch-dashboards` (5601),
`sortinghat`, `nginx` (8000), and `mordred` (the orchestrator).

Every service uses Docker's `unless-stopped` restart policy. After a reboot, the
stack starts again when Docker Desktop starts, unless it was explicitly stopped
before the reboot. Enable **Start Docker Desktop when you sign in to your
computer** in Docker Desktop settings so collection resumes without a manual
`docker compose up -d`. An interrupted backend operation may restart, but
persisted OpenSearch and SortingHat data is retained.

### 4. Watch the pipeline

```bash
docker compose logs -f mordred
```

Collection (git is fast; github is rate-limited) then enrichment. First backfill of
10 repos typically takes a few to several minutes. Expect periodic github pauses:
`sleep-for-rate` waits out the GitHub API budget rather than failing.

### 5. Verify data landed in OpenSearch

```bash
curl -sk -u admin:GrimoireLab.1 'https://localhost:9200/_cat/indices?v' | grep _ni_
```

You should see (row counts grow as enrichment runs):

- `git_ni_raw`, `git_ni_enriched`
- `github_ni_raw`, `github_ni_enriched`
- `git-aoc_ni_enriched`, `git-onion_ni_enriched` (study outputs)

### 6. Import the Sigils dashboards

Run this from the fork root after `git_ni_enriched` and `github_ni_enriched` exist:

```bash
python scripts/import_sigils.py --insecure
```

The script:

1. Connects the `git` alias to `git_ni_enriched`.
2. Connects the `github_issues` alias to `github_ni_enriched`.
3. Connects `git_areas_of_code` when `git-aoc_ni_enriched` exists.
4. Downloads the OpenSearch-compatible `overview`, `git`, `github_issues`, and
   `github_pull_requests` NDJSON bundles from Sigils.
5. Imports the saved objects with overwrite enabled, so rerunning is safe.
6. Sets `git` as the default data view and `now-5y` to `now` as the default
   dashboard time range.

The canonical `git` alias applies repository-specific analysis boundaries from
`aliases-ni.json`. For `ni/linux`, commits before the repository was created in
the NI organization (`2014-06-20T14:25:04Z`) remain in the physical indices for
provenance but are excluded from dashboards. Add a reviewed alias-filter clause
for any other repository with inherited upstream history; do not rely on the
interactive dashboard time picker to establish portfolio scope.

To pin or test another Sigils revision, pass `--sigils-ref <tag-or-commit>`.
Credentials can be overridden with `OPENSEARCH_USERNAME` and
`OPENSEARCH_PASSWORD`. Override the dashboard defaults with `--default-index`,
`--time-from`, and `--time-to`.

### 7. Verify the dashboard connections

Confirm the canonical aliases resolve to NI indices:

```bash
curl -sk -u admin:GrimoireLab.1 \
  'https://localhost:9200/_cat/aliases/git,github_issues,git_areas_of_code?v'
```

Confirm the imported data views have the IDs expected by the visualizations:

```bash
curl -s -u admin:GrimoireLab.1 -H 'osd-xsrf: true' \
  'http://localhost:5601/api/saved_objects/_find?type=index-pattern&per_page=100'
```

Open http://localhost:5601, go to **Dashboards**, and open:

- Git
- GitHub Issues
- GitHub Pull Requests
- Overview

The importer sets **Last 5 years** as the default. If an already-open browser tab
retains a shorter range in its URL, select **Last 5 years** once or reopen the
dashboard from the Dashboards list. The imported data views use
`grimoire_creation_date` as their time field.

### 8. Classify NI-affiliated contributors

GitHub organization membership is the authoritative source for whether a contributor
is a current member of the NI GitHub organization. The sync reads the authenticated
membership list into memory, intersects it with GitHub identities already collected
by SortingHat, and proposes `NI` enrollments. It does not write or commit the member
roster.

The token needs `read:org`. Preview first:

```bash
GITHUB_TOKEN=$(gh auth token) python scripts/sync_ni_affiliations.py
```

The command aborts if fewer than 1,000 members are visible, which prevents a token
that can see only public memberships from silently producing an incomplete result.
No SortingHat data changes without `--apply`.

GitHub does not expose organization membership start dates. The initial NI policy
explicitly treats current NI membership as a proxy for historical NI affiliation so
the existing contribution history can be classified. Apply the reviewed snapshot:

```bash
GITHUB_TOKEN=$(gh auth token) \
  python scripts/sync_ni_affiliations.py --apply --from-date 1900-01-01
```

This is a cohort approximation, not verified employment history. Dashboards and
reports must describe it as current NI GitHub membership projected historically.

The sync is additive: it does not withdraw departed members or overwrite contributors
who already have another SortingHat enrollment. Those cases require review because a
missing GitHub membership does not provide a reliable departure date.

After applying enrollments, allow Mordred's identity autorefresh to propagate
affiliations into enriched documents. Keep unmatched contributors as `Unknown`;
do not equate `Unknown` with external.

## Notes / gotchas

- **`panels = false` is intentional.** Mordred's panel phase targets legacy Kibiter
  JSON and APIs. OpenSearch Dashboards 3 uses the NDJSON import script above.
- **Do not point Sigils at `*_ni_enriched` directly.** The stable aliases (`git`,
  `github_issues`, and `git_areas_of_code`) decouple saved objects from physical
  NI index names.
- **Missing area-of-code panels.** If `git-aoc_ni_enriched` does not exist yet, the
  import script skips its optional alias. Rerun the script after that study completes.
- **Identity resolution is provisional.** SortingHat merges identities, but org
  affiliation and bus-factor/diversity metrics are only trustworthy after the Phase 2
  identity pass (see `docs/metrics.md`, principle P4 in the companion repo).
- **SortingHat rejects a large identity request.** The nginx and Django settings
  permit identity GraphQL requests up to 100 MiB. Recreate nginx and SortingHat,
  then restart Mordred after changing this limit:
  `docker compose up -d --no-deps --force-recreate nginx sortinghat`, followed by
  `docker compose restart mordred`.
- **Rate limits.** Tier 1 (10 repos) stays well under 5000 req/hr. Scaling to all 219
  repos needs the tiering in `gen_projects.py` (`--tier 1,2` + git-only Tier 3) and
  incremental runs; that is a later phase.
- **Reset.** `docker compose down -v` drops volumes (OpenSearch + MariaDB data) for a
  clean re-run.
- **Restart after reboot.** Docker restarts the stack when Docker Desktop starts.
  If Docker Desktop does not start automatically, open it and run
  `docker compose up -d`. Use `docker compose stop` before shutting down when you
  intentionally do not want the stack to restart.

## Teardown

```bash
cd docker-compose
docker compose down        # stop, keep data
docker compose down -v     # stop and wipe indices/identities
```
