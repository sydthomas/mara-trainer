# Slack setup

The coach runs as a Slack app in Socket Mode: the bot connects out to Slack, so there's no public
URL or hosting. It answers direct messages from one allowed user. It runs while
`uv run coach slack run` is running on your computer.

## 1. Create the app (about 5 minutes)

1. Go to https://api.slack.com/apps, click **Create New App**, then **From a manifest**, and pick
   your workspace.
2. Choose YAML, paste this manifest, then **Next** and **Create**:

```yaml
display_information:
  name: Mara Coach
  description: Marathon coach
features:
  app_home:
    home_tab_enabled: false
    messages_tab_enabled: true
    messages_tab_read_only_enabled: false
  bot_user:
    display_name: Mara Coach
    always_online: false
oauth_config:
  scopes:
    bot:
      - chat:write
      - im:history
      - im:read
      - im:write
settings:
  event_subscriptions:
    bot_events:
      - message.im
  interactivity:
    is_enabled: false
  org_deploy_enabled: false
  socket_mode_enabled: true
  token_rotation_enabled: false
```

3. **Basic Information**, then **App-Level Tokens**, then **Generate Token and Scopes**: name it
   `socket`, add the scope `connections:write`, and generate. It starts with `xapp-`.
4. **Install App**, then **Install to Workspace**, then **Allow**. Copy the **Bot User OAuth
   Token**. It starts with `xoxb-`.
5. Your member ID: in Slack, click your profile picture, then **Profile**, the **⋮** menu, and
   **Copy member ID**. It starts with `U`.

## 2. Add the settings to `.env`

Open `.env` yourself and add these lines (never paste tokens into chat, commits or issues):

```
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_ALLOWED_USER_ID=U...
```

`ANTHROPIC_API_KEY` must already be there.

## 3. Run it

```
uv run coach slack run
```

In Slack, open **Mara Coach** under Apps and send it a message. It posts "On it..." and then
edits that message with the answer (usually under a minute). Ctrl-C stops the bot.

## Notes

- Each answer costs about 10 to 15 cents in API usage, the same as `coach ask`.
- The bot remembers the last 10 exchanges in a DM and forgets after 6 hours of quiet, or when it
  restarts.
- Other people in the workspace who message it get "Sorry, this coach is private for now."
- Revoke a leaked token from the app's settings page and put the new one in `.env`.
