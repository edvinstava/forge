"""Parser for forge's optional .env-style config file (~/.forge/config.env).

Keys are environment-variable names; the parser normalizes values (whitespace,
one layer of quotes) so a pasted secret with a trailing newline does not reach
an API as an invalid token."""
import re
import sys

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_env_file(text: str) -> dict[str, str]:
    """Parse KEY=value lines into a dict. Pure; no filesystem access.

    - Blank lines and `#` comments are ignored.
    - A leading `export ` is tolerated (so the file is shell-source-able).
    - Split on the first `=`; the value keeps any further `=`.
    - Surrounding whitespace is stripped from key and value; one matching pair
      of single/double quotes is removed from the value.
    - Malformed lines (no `=`) and invalid keys are skipped with a stderr warning.
    - On duplicate keys, the last occurrence wins.
    """
    result: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            print(f"forge config: skipping malformed line {lineno}: {raw!r}",
                  file=sys.stderr)
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not _KEY_RE.match(key):
            print(f"forge config: skipping invalid key on line {lineno}: {key!r}",
                  file=sys.stderr)
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result
