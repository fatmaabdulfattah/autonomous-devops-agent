# Autonomous DevOps Agent — CLI image
FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system --add safe.directory '*'

# docker CLI only (talks to the host daemon if you mount /var/run/docker.sock)
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker

# INSTALL_FROM=local  -> install from this source tree (day 1-2 testing)
# INSTALL_FROM=pypi   -> install the published package (day 3, after release)
ARG INSTALL_FROM=local
ARG PACKAGE_VERSION=0.1.0
COPY . /opt/agent
RUN if [ "$INSTALL_FROM" = "pypi" ]; then \
        pip install --no-cache-dir "autonomous-devops-agent==${PACKAGE_VERSION}"; \
    else \
        pip install --no-cache-dir /opt/agent; \
    fi \
 && rm -rf /opt/agent

ENV DEVOPS_AGENT_HOME=/root/.devops_agent \
    PYTHONUNBUFFERED=1

WORKDIR /workspace
ENTRYPOINT ["devops"]
