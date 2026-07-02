# Container image that runs the Splunkbase publish script.
# Single stage on a modern Python so the interpreter that runs publish.py
# matches the one the dependencies (requests/urllib3) were installed for.
# The previous multi-stage build installed deps with python:3-slim (3.14) but
# ran them on distroless python3-debian10 (3.7); urllib3 2.x uses PEP 585
# generics (tuple[...]) and crashed with "'type' object is not subscriptable".
FROM python:3.12-slim

# Copies your code file from your action repository to the filesystem path `/app`
ADD . /app
WORKDIR /app

RUN pip install --no-cache-dir requests

# publish.py reads its inputs from INPUT_* environment variables.
ENTRYPOINT ["python", "/app/publish.py"]