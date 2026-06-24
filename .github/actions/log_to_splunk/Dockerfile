# Container image that runs your code
FROM python:3.12-slim

# Copies your code file from your action repository to the filesystem path `/app` of the container
ADD . /app
WORKDIR /app

RUN pip install --no-cache-dir requests

# Code file to execute when the docker container starts up
CMD ["python", "/app/main.py"]
