FROM node:22-bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates curl gnupg jq \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*
# Both agent CLIs ship in the image; FORGE_PROVIDER picks which one runs a turn.
RUN npm install -g @anthropic-ai/claude-code@latest @openai/codex@latest bun
# corepack ships with node 22; enable it so pnpm/yarn shims are on PATH for any user.
RUN corepack enable
# Chromium runtime libraries so agents can launch a headless browser to self-verify
# (Playwright/agent-browser download the browser binary at runtime; these are the
# system .so deps it links against).
RUN apt-get update && apt-get install -y --no-install-recommends \
      libglib2.0-0 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
      libdrm2 libdbus-1-3 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
      libxrandr2 libgbm1 libxshmfence1 libpango-1.0-0 libcairo2 libasound2 \
      libatspi2.0-0 fonts-liberation \
 && rm -rf /var/lib/apt/lists/*
# Bake Playwright + its Chromium into the image so agents launch a browser
# immediately instead of downloading ~170MB (or hunting the filesystem) every
# session. The env var is what makes any playwright — the global CLI or a
# repo-local install — find the shared browsers; a+rX because agents run as
# the unprivileged forge user.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN npm install -g playwright@latest \
 && playwright install chromium \
 && chmod -R a+rX /ms-playwright
RUN useradd -m -u 1001 forge && mkdir -p /work && chown forge:forge /work
USER forge
WORKDIR /work
# Keep the container alive so the orchestrator can exec into it repeatedly.
ENTRYPOINT ["sleep", "infinity"]
