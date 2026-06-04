FROM alpine:3.20

ARG TERRAFORM_VERSION=1.5.7
ARG TERRAGRUNT_VERSION=0.99.4
ARG SOPS_VERSION=3.9.4
ARG KUBECTL_VERSION=1.34.0
ARG BD_VERSION=1.0.2
ARG VAULT_VERSION=1.20.4

# System packages: infra tools + Python + Node.js (for Claude CLI).
# gcompat/libc6-compat provide the glibc shim the bd binary links against.
RUN apk add --no-cache \
    bash curl git git-crypt jq openssh-client openssl unzip \
    python3 py3-pip \
    nodejs npm \
    gcompat libc6-compat \
    && rm -rf /var/cache/apk/*

# Terraform
RUN curl -fsSL "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" \
    -o /tmp/terraform.zip \
    && unzip /tmp/terraform.zip -d /usr/local/bin/ \
    && rm /tmp/terraform.zip

# Terragrunt
RUN curl -fsSL "https://github.com/gruntwork-io/terragrunt/releases/download/v${TERRAGRUNT_VERSION}/terragrunt_linux_amd64" \
    -o /usr/local/bin/terragrunt \
    && chmod +x /usr/local/bin/terragrunt

# SOPS
RUN curl -fsSL "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.linux.amd64" \
    -o /usr/local/bin/sops \
    && chmod +x /usr/local/bin/sops

# kubectl
RUN curl -fsSL "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    -o /usr/local/bin/kubectl \
    && chmod +x /usr/local/bin/kubectl

# Vault CLI — download from HashiCorp releases. The binary used to be
# committed to the repo (495MB) but that doesn't survive the Forgejo
# extraction (.gitignore excludes it). Pulling at build time is cleaner.
RUN curl -fsSL "https://releases.hashicorp.com/vault/${VAULT_VERSION}/vault_${VAULT_VERSION}_linux_amd64.zip" \
    -o /tmp/vault.zip \
    && unzip /tmp/vault.zip -d /usr/local/bin/ \
    && rm /tmp/vault.zip \
    && chmod +x /usr/local/bin/vault

# Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# bd (beads CLI). Upstream github.com/steveyegge/beads redirects to gastownhall/beads
# and publishes release tarballs — there is no bare `bd_linux_amd64` asset.
# The binary is glibc-linked; gcompat+libc6-compat installed above provide the shim.
RUN curl -fsSL "https://github.com/gastownhall/beads/releases/download/v${BD_VERSION}/beads_${BD_VERSION}_linux_amd64.tar.gz" \
    -o /tmp/beads.tar.gz \
    && tar -xzf /tmp/beads.tar.gz -C /tmp \
    && install -m 0755 /tmp/bd /usr/local/bin/bd \
    && rm -rf /tmp/beads.tar.gz /tmp/bd

# Non-root user (Claude CLI blocks --dangerously-skip-permissions as root)
RUN addgroup -g 1000 agent && adduser -u 1000 -G agent -h /home/agent -s /bin/bash -D agent

# Terraform provider cache
ENV TF_PLUGIN_CACHE_DIR=/tmp/terraform-plugin-cache
ENV TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE=1
RUN mkdir -p /tmp/terraform-plugin-cache && chmod 777 /tmp/terraform-plugin-cache

# Python app
COPY requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir --break-system-packages -r /srv/requirements.txt

COPY app/ /srv/app/

# Set up home directory for agent user
RUN mkdir -p /home/agent/.config/sops/age \
    && chown -R agent:agent /home/agent

# Seed files staged in an image-layer path that is NEVER mounted at runtime.
# /workspace (PVC) and /home/agent/.claude (emptyDir) are both volume-mounted in
# production, so COPYing into them here has no effect. An init container in the
# K8s manifest copies these files into the runtime volumes on each pod start.
COPY beads/metadata.json /usr/share/agent-seed/beads-metadata.json
COPY agents/beads-task-runner.md /usr/share/agent-seed/beads-task-runner.md
COPY agents/recruiter-triage.md /usr/share/agent-seed/recruiter-triage.md
COPY agents/nextcloud-todos-planner.md /usr/share/agent-seed/nextcloud-todos-planner.md
COPY agents/nextcloud-todos-exec.md /usr/share/agent-seed/nextcloud-todos-exec.md

USER agent
WORKDIR /workspace/infra
EXPOSE 8080

CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--app-dir", "/srv"]
