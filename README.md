# gh-governance-collect

Org-wide GitHub governance collector. **One file, Python 3 standard library, nothing to install.**

For every repository in an organization it records:

- **effective branch rules** on the default branch — repository-, organization- *and*
  enterprise-level rulesets, folded in by GitHub rather than re-matched here
- **the ruleset behind each rule**, with its enforcement mode and bypass-actor count
- **deployment environments** — required reviewers, and whether self-review is prevented
- optionally: custom repository property values, and approver-team membership

Output is SQLite (one row per repo per run), a CSV, and a printed rollup that separates
**configured** from **enforced**.

Read-only by construction: every request is a `GET` except the GitHub App token exchange.
It writes nothing to GitHub and changes no settings.

---

## 1. Get it onto the machine that will run it

Pick whichever your network allows. All three give you the same file.

**Clone** (preferred — `.gitattributes` forces LF line endings, so the checksum below holds
even on Windows, where Git otherwise rewrites them):

```
git clone https://github.com/simulacra-ghas-lab/gh-governance-collect.git
cd gh-governance-collect
```

**Download just the script:**

```
curl -O https://raw.githubusercontent.com/simulacra-ghas-lab/gh-governance-collect/main/gh-governance-collect.py
```

PowerShell:

```
Invoke-WebRequest -Uri https://raw.githubusercontent.com/simulacra-ghas-lab/gh-governance-collect/main/gh-governance-collect.py -OutFile gh-governance-collect.py
```

**Copy from the browser** if neither works: open the raw URL above, select all, paste into
an editor, and save as `gh-governance-collect.py`.

### Verify the file

```
shasum -a 256 gh-governance-collect.py          # macOS / Linux
Get-FileHash -Algorithm SHA256 .\gh-governance-collect.py   # Windows PowerShell
```

```
78cde8be1460f532ad46ff231aa28f6111b7f89e75e6f34ae391dc35bf6a2066
```

If it doesn't match, the likely cause is **CRLF line endings**, not a bad copy — the file still
runs fine, only the hash comparison breaks. In VS Code, click `CRLF` in the status bar and switch
to `LF`. In Notepad++, Edit → EOL Conversion → Unix (LF). Notepad won't preserve LF; don't use it.

Cheaper checks that survive line-ending changes: the file is **1026 lines**, and the last line is
`sys.exit(130)`.

### Windows notes

- Use `py -3` or `python`. `python3` usually isn't on the PATH.
- Corporate proxy: nothing to configure. The tool reads `HTTPS_PROXY` and the system proxy
  settings automatically.
- If the first call fails with `CERTIFICATE_VERIFY_FAILED`, that's TLS inspection, not a bug.
  Point `SSL_CERT_FILE` at your corporate root CA bundle.
- `openssl` is needed only for GitHub App auth. On Windows it ships with Git, at
  `C:\Program Files\Git\usr\bin\openssl.exe` — add that to PATH if it isn't already.

---

## 2. Register a GitHub App

You can run this with a personal access token (`export GITHUB_TOKEN=...`, or just be signed in
to `gh`), and that's fine for a first look. For anything whose number gets reported, use an App:
roughly three times the rate limit, it outlives any individual's account, and the permission set
is entirely read-only.

### Permissions to request

| Scope | Permission | Access | Needed for |
|---|---|---|---|
| Repository | `Metadata` | Read | Repository inventory (mandatory for any App) |
| Repository | `Administration` | Read | Effective branch rules, repo-level rulesets |
| Repository | `Environments` | Read | Environments, required reviewers, `prevent_self_review` |
| Organization | `Administration` | Read | Org ruleset detail — `enforcement` and bypass actors |
| Organization | `Members` | Read | Expanding approver teams (`--expand-teams`) |
| Organization | `Custom properties` | Read | Repository property values (`--properties`) |

**No write permission of any kind, and no webhook.** If a review asks what the App can do to the
estate, the answer is nothing.

### Steps

1. Target org → **Settings** → **Developer settings** → **GitHub Apps** → **New GitHub App**
2. Name it something durable, e.g. `governance-collector`. Homepage URL can point at the repo
   that will hold the script.
3. Under **Webhook**, **uncheck Active**. This is a poller — it needs no callback and no
   inbound endpoint.
4. Set the permissions in the table above.
5. **Where can this GitHub App be installed?** → **Only on this account**.
6. **Create GitHub App**. Note the **App ID** at the top of the settings page.
7. **Generate a private key**. The browser downloads a `.pem` once; GitHub never shows it again.
8. **Install App** → your org → **All repositories**. Coverage measured against a partial
   installation is a misleading denominator.

**Put the `.pem` straight into the secret store the job will run from, and delete the download.**
It should not live on a laptop.

---

## 3. First run

Start small, to prove credentials and scopes before spending an hour of quota:

```
python3 gh-governance-collect.py --org YOUR_ORG --limit 20 --summary
```

Then the real thing:

```
python3 gh-governance-collect.py \
  --org YOUR_ORG \
  --db governance.db \
  --app-id 123456 --private-key app.pem \
  --properties --expand-teams \
  --summary --summary-out summary.txt --csv coverage.csv
```

The App installation is discovered from `--org`; there's no installation ID to look up.
Installation tokens last an hour and are refreshed automatically mid-run.

---

## 4. Schedule it

`examples/governance-collect.yml` is a ready-to-use workflow. It lives in `examples/` here on
purpose — in `.github/workflows/` it would fire on this repo's schedule.

**Where it goes:** copy it to `.github/workflows/governance-collect.yml` in an **internal repo
inside the org you're measuring** — an existing admin/platform repo is ideal. Commit
`gh-governance-collect.py` to that repo's root alongside it, so the script itself is under change
control. That's the same argument you'd make for policy-as-code, and it's what makes the number
reproducible without you present.

**Two secrets** on that repo (or the org):

| Secret | Value |
|---|---|
| `GOVERNANCE_APP_ID` | The App ID from step 2 |
| `GOVERNANCE_APP_PRIVATE_KEY` | The entire `.pem`, `-----BEGIN` and `-----END` lines included |

The workflow prints the rollup to the run summary page, so the nightly number is readable without
downloading anything, and keeps the database and CSV as a 90-day artifact.

### Where the database lives

An Actions runner is destroyed when the job ends, so `governance.db` survives only because two
steps copy it out. They are not interchangeable:

| | Mechanism | Lifetime | What it's for |
|---|---|---|---|
| **Cache** | `actions/cache/restore` at the start, `actions/cache/save` at the end | Evicted after **7 days** untouched; LRU over the repo's 10 GB budget | Carrying the ETag cache and run history into the next run. Best-effort. |
| **Artifact** | `actions/upload-artifact` | **90 days**, per run, downloadable | The retained evidence copy: this database, this CSV, this timestamp. |

Losing the cache costs you **one full-price run**, not data — the next run rebuilds the ETag
cache from scratch and carries on. But it does reset the accumulated run history in that file,
so if the *trend* matters beyond 90 days, don't rely on either of these: have the job push
`coverage.csv` into whatever reporting database the dashboard already reads, or commit it to a
data branch. Cache and artifacts are convenience and evidence respectively; neither is a
system of record.

Restore and save are split (rather than a single `actions/cache`) so the save still runs when a
later step fails. Otherwise one bad run throws away the cache and the next one pays full price
for no reason.

The database is written with SQLite in WAL mode, and the collector checkpoints the WAL back into
the main file before exiting — so the single `governance.db` that gets cached or uploaded is
complete. No `-wal` or `-shm` sidecar needs to be copied alongside it.

If Actions is restricted, the same script runs unchanged on a build agent or a small VM under
cron. It has no dependencies to install and needs nothing inbound. There the database simply
persists on disk and none of the above applies.

---

## 5. Reading the output

```
Denominator
  repositories returned by the API .......... 4,812
  excluded: archived ....................... 1,106
  excluded: forks ..........................   233
  excluded: empty (0 KB, no default branch) .   184
  IN SCOPE ................................. 3,289

Branch controls on the default branch
                                             CONFIGURED  ENFORCED
  Pull request required                        3,104 94.4%  2,376 72.2%
  Self-review prevented                        1,802 54.8%  1,455 44.2%

  Why the two columns differ
    repos whose only rulesets are evaluate/disabled ..   412
    repos with a ruleset over the bypass threshold ...   316
    repos with at least one unresolved ruleset .......     0
```

| Term | Meaning |
|---|---|
| **CONFIGURED** | A ruleset carrying the rule applies to the default branch, per `/repos/{owner}/{repo}/rules/branches/{branch}` — GitHub's own answer, not a re-implementation of its matching logic. |
| **ENFORCED** | That ruleset is `enforcement: active` **and** carries no more than `--bypass-threshold` bypass actors (default: 0). Anything in `evaluate` or `disabled` counts as configured and not enforced. |
| **IN SCOPE** | Non-archived, non-fork, non-empty. Every exclusion is printed with its count, and excluded repos are still written with `in_scope = 0` so the denominator can be recomputed. |

A repo that errors is recorded in the `error` column and counted separately. **A repository you
couldn't read is never counted as compliant.**

### Five ways a coverage number inflates

| | Effect | |
|---|---|---|
| **Evaluate mode** | overstates | A ruleset set to `evaluate` applies and enforces nothing. It appears in the effective-rules response exactly like an active one. |
| **Bypass actors** | overstates | A rule with a broad bypass list is compliant on paper and optional in practice. |
| **Prod-like naming** | understates | Environments are free text. The default pattern matches `prod`, `prd`, `production`, `live`, `dr` and deliberately not `preprod` or `non-prod`. |
| **Approver groups** | overstates | A required-reviewers group of one, or one containing the deployer, with `prevent_self_review` off. |
| **Denominator drift** | overstates | Archived repos, forks and empty repos flatter every percentage. |

The summary prints every environment name it did **not** classify as prod-like. Read that list
before publishing any environment number — it's what turns the heuristic from an assertion into
something someone else can check.

---

## 6. Querying the database

```sql
-- headline rollup, latest run
SELECT COUNT(*)                          AS in_scope,
       SUM(pr_required)                  AS pr_configured,
       SUM(pr_required_enforced)         AS pr_enforced,
       SUM(self_review_blocked)          AS selfrev_configured,
       SUM(self_review_blocked_enforced) AS selfrev_enforced
FROM repos
WHERE in_scope = 1
  AND run_id = (SELECT MAX(run_id) FROM runs);

-- the gap list: covered on paper, not in practice
SELECT repo, ruleset_count, evaluate_only, max_bypass_actors
FROM repos
WHERE in_scope = 1
  AND run_id = (SELECT MAX(run_id) FROM runs)
  AND pr_required = 1 AND pr_required_enforced = 0
ORDER BY max_bypass_actors DESC;

-- prod environments where one person can approve their own deploy
SELECT repo, environment, reviewer_users, reviewer_teams
FROM environments
WHERE is_prod_like = 1
  AND run_id = (SELECT MAX(run_id) FROM runs)
  AND (prevent_self_review = 0 OR approver_count < 2);

-- where policy actually comes from (org vs enterprise vs repo)
SELECT ruleset_source_type, COUNT(*)
FROM repo_rules
WHERE run_id = (SELECT MAX(run_id) FROM runs)
GROUP BY 1;
```

Tables: `runs`, `repos`, `repo_rules`, `environments`, `rulesets`, `teams`, `repo_properties`,
`http_cache`.

---

## 7. What a run costs

Two calls per repository in the common case. Everything shared — ruleset detail, team membership —
is fetched once per run, not once per repository.

| Data | Endpoint | Cost |
|---|---|---|
| Repo inventory | `/orgs/{org}/repos` | repos ÷ 100 |
| Effective branch rules | `/repos/../rules/branches/{b}` | 1 / repo |
| Environments, approvers, self-review | `/repos/../environments` | 1 / repo |
| Ruleset detail | `/orgs/{org}/rulesets/{id}` | 1 / ruleset |
| Approver team membership | `/orgs/{org}/teams/{slug}/members` | 1 / team |
| Custom properties | `/orgs/{org}/properties/values` | repos ÷ 100 |

| Repos | First full scan | Against a 15k/hr App budget |
|---|---|---|
| 1,000 | ~2,050 | 14% of one hour |
| 5,000 | ~10,100 | fits one window |
| 20,000 | ~40,100 | 3 windows, ~45 min wall clock |

**Re-runs are close to free.** Per-repo responses are stored with their `ETag` and re-requested
with `If-None-Match`; a `304` doesn't count against the primary rate limit. Measured on a real
org, second run with nothing changed: 15 requests issued, 12 served as `304`, **0 quota consumed**.

The repo inventory is deliberately *not* ETag-cached: a new repository shifts items between pages,
so a `304` on page one wouldn't prove the inventory is unchanged.

Secondary limits bind before primary ones. `--rpm` defaults to 700, under the ~900/min ceiling,
and `--workers` to 8. `Retry-After` and `x-ratelimit-reset` are both honoured.

---

## 8. Flags

| Flag | Default | |
|---|---|---|
| `--org` | *required* | Organization login |
| `--db` | `governance.db` | SQLite path |
| `--api` | `https://api.github.com` | Base URL. Also `GITHUB_API`. |
| `--app-id` / `--private-key` | — | GitHub App auth |
| `--installation-id` | auto | Discovered from `--org` |
| `--enterprise` | — | Enterprise slug, to resolve enterprise-tier rulesets |
| `--workers` | `8` | Concurrent requests |
| `--rpm` | `700` | Request ceiling per minute |
| `--limit` | — | Stop after N repos (smoke test) |
| `--include-archived` / `--include-forks` | off | Widen the denominator |
| `--prod-pattern` | see below | Regex for production-like environment names |
| `--bypass-threshold` | `0` | Bypass actors tolerated before a rule stops counting as enforced |
| `--properties` | off | Collect custom repository property values |
| `--expand-teams` | off | Resolve approver teams to member lists |
| `--no-cache` | off | Ignore stored ETags and re-fetch everything |
| `--csv` / `--summary` / `--summary-out` | — | Output |

Default prod pattern: `(?i)^(prod|prd|production|live|dr)([-_./].*)?$`

### A note on enterprise rulesets

An org installation token generally can't read `/enterprises/{slug}/rulesets`. Where that happens
the tool records `enforcement: unknown` rather than assuming active, and the summary line
*"repos with at least one unresolved ruleset"* tells you how many repos are affected. If that
number is zero, both columns are complete. If it isn't, the last query in section 6 shows where
the policy is coming from.
