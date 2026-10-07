ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG DOCKER_CLI_IMAGE=docker:29.2.1-cli
FROM ${DOCKER_CLI_IMAGE} AS docker-cli

FROM ${PYTHON_IMAGE} AS runtime
WORKDIR /opt/runner
COPY bootstrap.py /opt/runner/bootstrap.py
RUN chmod 0555 /opt/runner/bootstrap.py
USER 65532:65532
CMD ["python", "-I", "-B", "/opt/runner/bootstrap.py", "run"]

FROM ${PYTHON_IMAGE} AS broker
WORKDIR /opt/runner
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY requirements.lock /opt/runner/requirements.lock
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements.lock
COPY common.py resolver.py docker_backend.py service.py storage.py /opt/runner/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["python", "/opt/runner/service.py"]
