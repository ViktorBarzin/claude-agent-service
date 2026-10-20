# bd (beads CLI), built from source against musl. The release tarballs are
# glibc builds, and since 1.3.0 they need fcntl64, which gcompat does not
# provide ("Error relocating /usr/local/bin/bd: fcntl64: symbol not found").
# CGO stays on, as upstream's release build has it, so embedded Dolt still
# works; gms_pure_go is upstream's release build tag.
#
# A BD_VERSION bump changes the schema the shared beads database needs. Read
# the release notes and docs/runbooks/beads-bd-upgrade.md in the infra repo
# before changing it: every bd client on the server has to move together.
FROM golang:1.26.9-alpine3.23 AS bd
ARG BD_VERSION=1.3.1
ARG BD_COMMIT=c1c4b642ac1c08d8c828007a1c2f96e47e43ef7c
RUN apk add --no-cache gcc g++ musl-dev git \
    && CGO_ENABLED=1 GOBIN=/out go install -tags gms_pure_go \
       -ldflags "-s -w -X main.Version=${BD_VERSION} -X main.Build=$(echo ${BD_COMMIT} | cut -c1-9) -X main.Commit=${BD_COMMIT}" \
       "github.com/steveyegge/beads/cmd/bd@v${BD_VERSION}" \
    && /out/bd version

FROM alpine:3.23

# The homelab CLI, from its own published image. Without it the agent reaches for
# `kubectl logs` on one pod when `homelab logs query` would search every service
# cluster-wide over 30 days in Loki, and it has no way to record or recall a
# durable learning between runs — each run starts blind. Both are things every
# human session on this box takes for granted.
#
# Both images are alpine-based, so the musl binary runs as-is; the CLI is built
# CGO-free in golang:alpine.
COPY --from=ghcr.io/viktorbarzin/infra-cli:latest /app/infra_cli /usr/local/bin/homelab

ARG TERRAFORM_VERSION=1.16.6
ARG TERRAGRUNT_VERSION=1.1.6
ARG SOPS_VERSION=3.13.3
ARG KUBECTL_VERSION=1.35.9
ARG VAULT_VERSION=2.2.0

# System packages: infra tools + Python + Node.js (for Claude CLI).
# gcompat/libc6-compat were added for bd's glibc release binary. bd is now
# built against musl in the first stage; the shim stays for any other
# glibc-linked tool.
RUN apk add --no-cache \
    bash curl git git-crypt jq openssh-client openssl unzip \
    python3 py3-pip \
    nodejs npm \
    gcompat libc6-compat \
    && rm -rf /var/cache/apk/*

# Terraform
RUN curl -fsSL "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" \
    -o /tmp/terraform.zip \
    && unzip /tmp/terraform.zip terraform -d /usr/local/bin/ \
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
    && unzip /tmp/vault.zip vault -d /usr/local/bin/ \
    && rm /tmp/vault.zip \
    && chmod +x /usr/local/bin/vault

# Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# bd (beads CLI), from the musl build stage at the top of this file.
COPY --from=bd /out/bd /usr/local/bin/bd
# bd 1.3 sends anonymous usage metrics to gastownhall-eventsapi.com unless this
# is set. Every bd run in this image (agent runs, the beads-dispatcher and
# beads-reaper CronJobs, claude-breakglass) inherits it.
ENV BD_DISABLE_METRICS=1

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
# The f1-stream report intake. app/intake.py reads it from here and defines the
# agent on the command line, so the run loads no settings file that could widen
# its tools; it does not need to be copied into ~/.claude/agents.
COPY agents/f1-report-intake.md /usr/share/agent-seed/f1-report-intake.md
# The breakglass deployment (separate stack) seeds this one instead of the
# untrusted-input agents; its init container copies whichever it needs.
COPY agents/breakglass.md /usr/share/agent-seed/breakglass.md

# Breakglass entrypoint. The breakglass Deployment overrides the default CMD
# with this (ssh-agent bootstrap + ssh aliases, then uvicorn for the breakglass
# app). It ships in every image but only that deployment runs it. The built
# frontend lives under app/breakglass/static/ (committed — no in-cluster build,
# per ADR-0002), so the `COPY app/` above carries it in.
COPY docker-entrypoint-breakglass.sh /srv/docker-entrypoint-breakglass.sh
RUN chmod 0755 /srv/docker-entrypoint-breakglass.sh

USER agent
WORKDIR /workspace/infra
EXPOSE 8080

CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--app-dir", "/srv"]
