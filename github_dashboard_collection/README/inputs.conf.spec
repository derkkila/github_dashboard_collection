[github_copilot_billing://<name>]
* Collects GitHub Copilot AI-credit billing and usage data for a GitHub
* enterprise and indexes it in Splunk. Feeds the budget, ai_credit and
* ai_credit_by_user sourcetypes.
* The personal access token (PAT) is NOT configured here. Store it in Splunk's
* encrypted credential store (storage/passwords) with the realm set to this
* input's <name> (or to the app name "github_dashboard_collection"). See
* docs/copilot_billing_collection.MD for setup instructions.

enterprise = <string>
* Required. The slug version of the GitHub enterprise name (for example
* customer-success-architects-ea-sandbox).

api_base_url = <string>
* Optional. Base URL of the GitHub REST API.
* Defaults to https://api.github.com.
* For GitHub Enterprise Server use https://<host>/api/v3.

collect_budgets = <boolean>
* Optional. Collect the enterprise AI credit budget pool (allocation +
* consumed) from the budgets endpoint. Defaults to true.

collect_ai_credit = <boolean>
* Optional. Collect AI credit usage over time (one event per usageItems
* entry so usage can be grouped by model). Defaults to true.

collect_by_user = <boolean>
* Optional. Loop the Copilot seats and collect AI credit usage per user,
* tagging each event with the user login. Defaults to false.

start_date = <string>
* Optional. First day to backfill on the initial run, in YYYY-MM-DD format.
* Only the past 24 months of data is available. If unset, lookback_days is
* used instead.

lookback_days = <integer>
* Optional. Number of days to backfill on the first run when start_date is
* not set. Defaults to 30. Clamped to the 24-month (730 day) retention window.

interval = <integer or cron schedule>
* Optional. Standard Splunk modular input scheduling interval, in seconds or
* as a cron expression. A daily schedule is recommended, for example 86400.
