# Adding @forge to a Slack channel

forge works in channels in addition to DMs. It only takes instructions from
`SLACK_ALLOWED_USER`; anyone else who `@`-mentions it gets a visible "I only
answer @<you>" notice (posted once per person per channel).

## One-time Slack app config (Socket Mode — no request URL needed)

In <https://api.slack.com/apps> → your forge app:

1. **OAuth & Permissions → Bot Token Scopes** — add:
   - `app_mentions:read`
   - `channels:history`   (public channels)
   - `groups:history`     (private channels)

   Keep the existing `chat:write`, `files:read`, `files:write`, `im:history`,
   `im:read`, `im:write`. (`files:read` is required for image attachments —
   without it the bot replies "couldn't fetch — add the files:read bot scope
   and reinstall the app".)

2. **Event Subscriptions → Subscribe to bot events** — add:
   - `app_mention`
   - `message.channels`
   - `message.groups`

   Keep `message.im`.

3. **Reinstall the app** (OAuth & Permissions → Reinstall to Workspace) to apply
   the new scopes.

## Using it

1. Invite forge to a channel: `/invite @forge`.
2. Start work with a mention: `@forge add a logout button to dhis2-app`.
3. forge opens a thread; reply in that thread (no `@forge` needed) to keep going.
4. `@forge introduce yourself` / `@forge help` shows what it can do.

## Who can drive it

- Only `SLACK_ALLOWED_USER` is obeyed.
- Anyone else who `@forge`s (or replies inside a forge thread) sees a one-time
  visible notice that forge only answers the allowed user.
- Ambient channel chatter that doesn't address forge is ignored.
