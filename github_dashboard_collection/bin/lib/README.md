# Vendored third-party libraries

## splunklib (Splunk Enterprise SDK for Python)

The `splunklib/` directory is a vendored copy of the **Splunk Enterprise SDK
for Python** (`splunk-sdk` on PyPI, version 2.1.1). It is bundled here so the
`github_copilot_billing` modular input can run against the Python interpreter
shipped with Splunk 9.x without requiring a separate `pip install`.

- Upstream: https://github.com/splunk/splunk-sdk-python
- License: Apache License, Version 2.0
  (see the license header at the top of each `splunklib/*.py` file, and
  https://www.apache.org/licenses/LICENSE-2.0)

To update, download the desired `splunk-sdk` release and replace the
`splunklib/` directory:

```
pip download splunk-sdk --no-deps -d /tmp/sdk
tar -C /tmp/sdk -xzf /tmp/sdk/splunk-sdk-*.tar.gz
cp -R /tmp/sdk/splunk-sdk-*/splunklib bin/lib/splunklib
```
