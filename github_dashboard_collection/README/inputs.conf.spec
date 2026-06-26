[github_copilot_billing://<name>]
* Collects GitHub Copilot AI-credit billing and usage data for a GitHub
* enterprise and indexes it in Splunk. Feeds the budget, ai_credit and
* ai_credit_by_user sourcetypes.
* Enter the personal access token (PAT) in the "token" field below. On the
* first run it is moved into Splunk's encrypted credential store
* (storage/passwords, realm = this input's <name>) and the field is masked, so
* the secret is never left in clear text. Use a CLASSIC PAT; fine-grained PATs
* are not supported by the billing endpoints.
* See docs/copilot_billing_collection.MD for setup instructions.

enterprise = <string>
* Required. The slug version of the GitHub enterprise name (for example
* customer-success-architects-ea-sandbox).

token = <string>
* Optional. A CLASSIC GitHub PAT with manage_billing:copilot and enterprise
* admin / billing-manager access. Encrypted into storage/passwords on the
* first run, then masked in this file. Leave blank to reuse the stored token,
* or to supply the credential out of band via storage/passwords.

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
