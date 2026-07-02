# forge config file

forge reads all configuration from environment variables. To avoid re-exporting
secrets in every shell, you can put any of them in an optional file:

    ~/.forge/config.env

Override the location with the `FORGE_CONFIG` environment variable.

## Format

`.env`-style `KEY=value` lines, where each key is the **environment variable
name** forge already uses:

    # ~/.forge/config.env
    SLACK_BOT_TOKEN=xoxb-1234-…
    SLACK_APP_TOKEN=xapp-1-…
    SLACK_ALLOWED_USER=U012ABC
    GH_TOKEN=ghp_…
    CLAUDE_CODE_OAUTH_TOKEN=sk-ant-…
    FORGE_PROVIDER=claude
    FORGE_MAX_SESSIONS=4

- Blank lines and `#` comments are ignored.
- A `#` only starts a comment at the **beginning of a line**; an inline `#` is
  not special and the value is taken verbatim to the end of the line. For
  example `GH_TOKEN=ghp_x # note` sets the value to `ghp_x # note`, trailing
  comment and all. Wrap the value in quotes if you need literal leading or
  trailing spaces.
- A leading `export ` is allowed, so the same file can be `source`d in a shell.
- Surrounding whitespace and one layer of matching quotes are stripped from
  values — a pasted token with a stray trailing space or newline is normalized
  rather than silently reaching the API as an invalid token.

## Precedence

An environment variable set in the shell **always wins** over the file, so you
can still override any value per-invocation with an `export`. This includes a
variable that is set but **empty** (e.g. `export SLACK_BOT_TOKEN=`) — an empty
value still counts as "set" and beats the file. `unset` the variable (don't
just set it empty) if you want the file's value to apply.

## Security

The file holds secrets. It lives outside any git repo, so it cannot be
committed. Keep it private:

    chmod 600 ~/.forge/config.env

forge warns on startup if the file is group- or world-readable.
