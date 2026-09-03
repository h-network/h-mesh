# One image, one way to run h-mesh: this is the only supported deployment
# path for this repo. It builds the same setup.sh a bare-VM install runs --
# nothing here reimplements the wizard, the roster, or hire's bookkeeping;
# the container's entrypoint (below) calls setup.sh non-interactively, the
# same way a scripted bare-host install already can.
#
# Base is a plain, unmodified python:3.12-slim -- h-mesh does not depend on
# any pre-baked base image. tmux and h-agent are not present on a bare
# Debian/Ubuntu image by default, so this Dockerfile is what setup.sh's own
# "system dependencies" step (redis-server, python3-venv, h-agent) would do
# on a fresh VM, done once at build time instead of on every container
# start: python:3.12-slim's stdlib venv module needs no separate
# python3-venv package (that gap is Debian/Ubuntu's system Python, not
# python.org's own image), h-agent's installer is self-contained (own
# verified fact, not re-checked here), and tmux/redis-server/tini come from
# apt like setup.sh would install them itself.
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# ⚠ jq is load-bearing, not a convenience: h-agent's own installer's
# seed_claude_state()/seed_claude_settings() (install.sh) merge their
# defaults into an already-existing ~/.claude.json/settings.json with jq --
# and one already exists by the time they run, because install_claude()
# itself invokes `claude --version` moments earlier to verify the install,
# which is enough for the CLI to write a bare .claude.json with no
# `hasCompletedOnboarding` key. Without jq, seed_claude_state() takes its
# "preserve existing, warn, do nothing" branch instead of merging --
# silently, printed only as a build-log warning easy to miss (measured:
# this is exactly what happened here) -- and hasCompletedOnboarding never
# gets set, so claude's full first-run tour (theme picker, then a demo
# screen, then account selection) shows on every agent's very first
# launch instead of being skipped.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tmux redis-server tini curl ca-certificates git procps jq \
    && rm -rf /var/lib/apt/lists/*

# Agent CLIs (h-agent, claude, codex, agy) run unattended, as a real,
# unprivileged user -- not root. /workdir is where get_workdir_root()
# (h-app/lib/paths.py) already looks first for a container's agent
# workdirs; creating it here is what makes that branch the one taken
# instead of falling back to ~/h-mesh.
# /home/ubuntu/.h-mesh is where compose.yaml's named volume mounts (Redis's
# AOF, persisted tenant config, daemon logs/pidfiles) -- pre-created here,
# owned by ubuntu, so a fresh named volume's first-mount copy (Docker seeds
# a new volume from whatever already exists at the mountpoint, ownership
# included) lands writable by the user that actually runs the container,
# not root.
RUN useradd --create-home --shell /bin/bash ubuntu \
    && mkdir -p /app /opt/h-mesh /workdir /home/ubuntu/.h-mesh \
    && chown -R ubuntu:ubuntu /app /opt/h-mesh /workdir /home/ubuntu

COPY container/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

WORKDIR /app
USER ubuntu
ENV HOME=/home/ubuntu \
    VIRTUAL_ENV=/opt/h-mesh \
    PATH="/opt/h-mesh/bin:/home/ubuntu/.local/bin:${PATH}"

COPY --chown=ubuntu:ubuntu pyproject.toml README.md LICENSE setup.sh tmux.conf ./
COPY --chown=ubuntu:ubuntu assets/ ./assets/
COPY --chown=ubuntu:ubuntu h-app/ ./h-app/

RUN python3 -m venv "$VIRTUAL_ENV" \
    && "$VIRTUAL_ENV/bin/pip" install --no-cache-dir --upgrade pip \
    && "$VIRTUAL_ENV/bin/pip" install --no-cache-dir -e .

# h-agent's own installer (setup.sh's step 0 runs the identical command on a
# bare host that's missing it -- see H_AGENT_INSTALL_URL there). Baked in at
# build time so a container never needs outbound network just to boot, and
# so `--skip-deps` at runtime (the entrypoint always passes it) is honest --
# every dependency setup.sh would otherwise check for is already here.
#
# ⚠ --ipv4, via .curlrc rather than a flag on this line alone: the installer
# makes its own further curl calls internally (fetching seedProfile,
# setupConfigDir, and the claude/codex/agy installers themselves), each a
# fresh curl invocation this Dockerfile doesn't control directly. Measured
# on a builder whose network resolves raw.githubusercontent.com (and other
# hosts) to AAAA-only with no working IPv6 route: every such request failed
# fast with curl's own exit 22, indistinguishable from a real 404 without
# `-v`. `.curlrc` is the one place a default reaches every curl invocation
# a downstream script makes, not just this file's own. A builder with
# working IPv6 is unaffected -- this only removes a route that would have
# failed anyway.
RUN printf -- '--ipv4\n' > "$HOME/.curlrc"

# Downloaded to a file and executed as a second step, not `curl | bash` --
# a transient failure mid-stream then reads as this installer's own exit
# status instead of a truncated pipe, and the download itself is retried
# independently of execution, same as setup.sh's own h-agent install step
# (see its comment there) and for the same reason.
#
# ⚠ NOT named H_AGENT_INSTALL_URL as a build ARG here, deliberately --
# install.sh itself reads an environment variable of exactly that name as
# an override for where it fetches the h-agent BINARY from (default
# .../h-app/h-agent), completely unrelated to where THIS Dockerfile fetches
# the installer SCRIPT from. A Dockerfile ARG is injected into every RUN
# instruction's shell environment, so reusing that name here silently fed
# this file's own installer-script URL into the script's binary-download
# path instead, breaking every download after the first (each derived by
# string-stripping the intended h-agent URL -- see install.sh's own
# `base_download_url=${url%/h-agent}`) with a same-shaped but wrong path,
# 404ing downstream. Measured directly: renaming this one variable, with no
# other change, is what fixed it -- distinguish that from a slow/flaky
# network, which this was never actually about.
ARG H_AGENT_INSTALLER_URL=https://raw.githubusercontent.com/h-network/h-agent/main/install.sh
RUN curl -fsSL "$H_AGENT_INSTALLER_URL" -o /tmp/h-agent-install.sh \
    || curl -fsSL "$H_AGENT_INSTALLER_URL" -o /tmp/h-agent-install.sh
RUN bash /tmp/h-agent-install.sh && rm -f /tmp/h-agent-install.sh && command -v h-agent

# ⚠ modules/tmux/ops.py's _seed_profile_dirs says outright: "Unprofiled
# agents need nothing -- they read ~/.claude directly, which the image
# already ships populated." True of the base image this codebase otherwise
# assumes, false of h-agent's installer alone: its own seed_claude_settings
# covers permission/telemetry/notification defaults but never sets a
# `theme`, and claude stops on the interactive colour-scheme picker on its
# very first launch without one -- measured live, an agent otherwise fully
# credentialed and trusted sat there unreachable. seedProfile (used for
# profiled accounts) copies settings.json from this same live dir at hire
# time, so seeding it once here covers both the default and every profiled
# account. `dark` matches the picker's own highlighted default.
RUN python3 -c "\
import json; \
path = '$HOME/.claude/settings.json'; \
data = json.load(open(path)); \
data.setdefault('theme', 'dark'); \
json.dump(data, open(path, 'w'), indent=2)"

# The doors bind every interface *inside* the container; a compose/`docker
# run -p` mapping is the deliberate act that decides what actually leaves
# the machine, the same way a host's firewall would. 127.0.0.1 (the
# process-level default -- modules/api/server.py, modules/session/app.py)
# is unreachable from outside this network namespace regardless of any
# port mapping, so a container that never overrides it can publish nothing
# no matter what compose.yaml says. Both doors already refuse to start
# non-loopback without a token (same files) -- that check is what makes
# API_TOKEN mandatory once the api/session daemons are actually enabled,
# not this line.
ENV API_BIND=0.0.0.0 \
    SESSION_BIND=0.0.0.0

EXPOSE 8080 8081

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
