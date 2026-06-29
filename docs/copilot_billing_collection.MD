# GitHub Copilot Billing Collection

This modular input collects **GitHub Copilot AI-credit billing and usage** data
from the GitHub REST API and indexes it in Splunk. It ships inside the
`github_dashboard_collection` Splunk app (`bin/github_copilot_billing.py`) and
powers four reports:

1. **Overall AI credit pool** — allocation and consumed amount for the
   enterprise budget.
2. **AI credit pool consumption over time.**
3. **Consumption by user.**
4. **Usage grouped by model.**

## Prerequisites

- Splunk 9.x (uses the bundled Python 3 interpreter). The Splunk Python SDK is
  vendored under `bin/lib/splunklib`, so no extra install is required.
- A GitHub **personal access token (PAT)** belonging to an enterprise admin or
  billing manager.

### Required token scopes

> **Use a _classic_ personal access token.** The GitHub billing usage
> endpoints do **not** support fine-grained PATs. A classic PAT created by an
> enterprise owner or billing manager is required.

The classic PAT must be created by an **enterprise owner / billing manager**
and include:

- `manage_billing:copilot` — read Copilot billing data
- `read:enterprise` — read enterprise profile data

Only the **past 24 months** of billing data is available from the API.

## Endpoints used

All requests send these headers:

```
Authorization: Bearer <PAT>
Accept: application/vnd.github+json
X-GitHub-Api-Version: 2022-11-28
```

| Report | Endpoint |
| --- | --- |
| Overall pool | `GET /enterprises/{ent}/settings/billing/budgets?scope=enterprise` |
| Consumption over time | `GET /enterprises/{ent}/settings/billing/ai_credit/usage` |
| Consumption by user | `GET /enterprises/{ent}/copilot/billing/seats` then `.../ai_credit/usage?user={login}` |
| Consumption by cost center | `GET /enterprises/{ent}/settings/billing/cost-centers` then `.../ai_credit/usage?cost_center_id={id}` |
| Consumption by organization | `.../ai_credit/usage?organization={org}` |
| Usage by model | one Splunk event per `usageItems[]` entry (carries `model`) |

The base URL defaults to `https://api.github.com` and is configurable via
`api_base_url` for GitHub Enterprise Server (`https://<host>/api/v3`).

> **Why per-user usage is collected in a loop.** The unfiltered enterprise
> `ai_credit/usage` response does **not** carry a per-item `user` field — the
> top-level `user` is only populated when you filter with `?user={login}`.
> There is no single bulk per-user call, so `collect_by_user` enumerates the
> Copilot seats and issues one `ai_credit/usage?user={login}` request per user.
> The **model** breakdown comes from the single unfiltered enterprise call,
> where `model` is present on every `usageItems[]` entry.

> **Cost center and organization attribution are cheaper than per-user.** The
> `ai_credit/usage` endpoint also accepts `cost_center_id` and `organization`
> filters, so `collect_by_cost_center` and `collect_by_org` make **one** call
> per group per day instead of one per user. There is still no multi-value form
> (`?user=a,b` / `?organization=x,y` are not supported), so each group is its
> own request. `collect_by_cost_center` auto-enumerates active cost centers from
> the cost-centers endpoint unless you pin a `cost_center_ids` list;
> `collect_by_org` always uses the explicit `organizations` list you provide.

## Providing the PAT

The token is **never** stored in plaintext in a `.conf` file. The add-on keeps
it in Splunk's encrypted credential store (`storage/passwords`). There are two
ways to get it there.

### Option A, enter it in the input (recommended)

Type the PAT into the input's **token** field, either in the example stanza you
copy into `local/inputs.conf` or in Splunk Web under **Settings > Data inputs >
GitHub Copilot Billing**. On the first run the modular input moves that value
into `storage/passwords` (realm = the stanza `<name>`) and rewrites the `token`
field to `<encrypted>`, so the secret never lingers in clear text. Leave the
field blank (or masked) on later edits to keep using the stored token.

### Option B, store it directly via REST (advanced)

If you would rather not put the token in the stanza at all, create the
credential out of band with the **realm set to the input name** (the `<name>`
part of the stanza), or to the app name `github_dashboard_collection`:

```
curl -k -u admin:<splunk-password> \
  https://localhost:8089/servicesNS/nobody/github_dashboard_collection/storage/passwords \
  -d name=github_pat \
  -d password=<YOUR_GITHUB_PAT> \
  -d realm=customer-success-architects-ea-sandbox
```

The `name` (username) can be anything; the input reads the first credential
found under the matching realm. Either way the token stays encrypted at rest and
never appears in `default/`.

## Enabling the input

1. Edit (or copy) the example stanza in `default/inputs.conf` into
   `local/inputs.conf`, set your `enterprise` slug, and remove `disabled = true`.
2. Provide the PAT (Option A or B above). For Option A, set `token = <your PAT>`
   in the stanza; it is encrypted and masked on the first run.
3. Choose what to collect with `collect_budgets`, `collect_ai_credit`,
   `collect_by_user`, `collect_by_cost_center`, and `collect_by_org`, and set the
   `interval` (a daily `86400` is recommended). For `collect_by_cost_center`,
   optionally pin `cost_center_ids`; for `collect_by_org`, list the orgs in
   `organizations`.
4. On the first run the input backfills from `start_date` (or `lookback_days`,
   default 30) and writes a per-input checkpoint so subsequent runs only fetch
   new days. The 24-month retention window is enforced automatically.

Example `local/inputs.conf`:

```
[github_copilot_billing://customer-success-architects-ea-sandbox]
enterprise = customer-success-architects-ea-sandbox
# token = ghp_...   (encrypted into storage/passwords and masked on first run)
api_base_url = https://api.github.com
collect_budgets = true
collect_ai_credit = true
collect_by_user = true
# Optional group-level attribution (one API call per group per day):
collect_by_cost_center = false
# cost_center_ids = abc123,def456   (blank = auto-enumerate active cost centers)
collect_by_org = false
# organizations = my-org,another-org
lookback_days = 30
interval = 86400
index = github
disabled = false
```

## Sourcetypes

| Sourcetype | Contents |
| --- | --- |
| `github:copilot:billing:budget` | One event per enterprise budget, enriched with `effective_budget_amount` (allocation) and `effective_consumed_amount`. |
| `github:copilot:billing:ai_credit` | One event per `usageItems[]` entry for the enterprise. |
| `github:copilot:billing:ai_credit_by_user` | One event per `usageItems[]` entry, tagged with `user`. |
| `github:copilot:billing:ai_credit_by_cost_center` | One event per `usageItems[]` entry, tagged with `cost_center_id` and `cost_center_name`. |
| `github:copilot:billing:ai_credit_by_org` | One event per `usageItems[]` entry, tagged with `organization`. |

Every event is JSON (`KV_MODE = json`) and is enriched with `enterprise`,
`scope`, a `date` (YYYY-MM-DD), and a `time_period` object (`year`/`month`/`day`,
also flattened to top-level `year`/`month`/`day` fields). By-user events also
carry `user`; by-cost-center events carry `cost_center_id` and
`cost_center_name` (scope `cost_center`); by-org events carry `organization`
(scope `organization`).

## Example SPL

Replace `index=github` with your index (or use the app's `github_source`
macro).

### 1. Overall AI credit pool (allocation vs. consumed)

```
index=github sourcetype=github:copilot:billing:budget
| stats latest(effective_budget_amount) AS allocation
        latest(effective_consumed_amount) AS consumed by enterprise
| eval remaining = allocation - consumed
| eval pct_consumed = round(consumed / allocation * 100, 1)
```

### 2. AI credit pool consumption over time

```
index=github sourcetype=github:copilot:billing:ai_credit
| timechart span=1d sum(grossAmount) AS gross_spend sum(netAmount) AS net_spend
```

### 3. Consumption by user

```
index=github sourcetype=github:copilot:billing:ai_credit_by_user
| stats sum(grossQuantity) AS credits sum(netAmount) AS net_spend by user
| sort - net_spend
```

### 4. Usage grouped by model

```
index=github sourcetype=github:copilot:billing:ai_credit
| stats sum(grossQuantity) AS credits sum(grossAmount) AS gross_spend by model
| sort - credits
```

### 5. Consumption by cost center

```
index=github sourcetype=github:copilot:billing:ai_credit_by_cost_center
| stats sum(grossQuantity) AS credits sum(grossAmount) AS gross_spend
        by cost_center_name
| sort - gross_spend
```

### 6. Consumption by organization

```
index=github sourcetype=github:copilot:billing:ai_credit_by_org
| stats sum(grossQuantity) AS credits sum(grossAmount) AS gross_spend
        by organization
| sort - gross_spend
```

## Troubleshooting

- **No PAT available** — set `token` in the input (it is encrypted on the next
  run), or confirm a `storage/passwords` entry exists whose realm matches the
  input `<name>` (or the app name). Check `splunkd.log` for `[<name>] no PAT
  available ...`.
- **HTTP 403 / 404** — the token lacks `manage_billing:copilot`, is not an
  enterprise admin/billing manager, **is a fine-grained PAT (use a classic
  PAT)**, or the AI-credit feature is not enabled for the enterprise. 404s are
  skipped gracefully.
- **HTTP 429 / rate limits** — handled automatically with `Retry-After` /
  `X-RateLimit-Reset` backoff.
- **Validate offline** — run a self-test that exercises parsing, pagination, and
  event building with mocked HTTP (no Splunk required):

  ```
  $SPLUNK_HOME/bin/splunk cmd python \
    $SPLUNK_HOME/etc/apps/github_dashboard_collection/bin/github_copilot_billing.py --selftest
  ```
