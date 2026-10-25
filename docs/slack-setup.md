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
5. Your member ID (starts with `U`): click your own name on any message you sent, then **⋮** and
   **Copy member ID**. If Slack shows a Pro upgrade prompt instead, skip this: run the bot without
   `SLACK_ALLOWED_USER_ID` (setup mode, step 3) and DM it; it replies with your member ID.

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

## Long-run check-in

On days your accepted plan has a long run, the bot DMs you at 10am (your timezone) asking how it
went. If the bot was off at 10, it sends the check-in when it starts, until 8pm. It's sent at
most once a day. Answer however you like; the coach asks at most one follow-up, reads the entry
back and asks "Save it?". Reply **yes** to save it to your fuel log or **no** to drop it.
Anything else is treated as a correction. The bot has to be running for the check-in to arrive.

## Sunday weekly check-in

Sundays at 6pm (your timezone; late until 10pm if the bot was off) the bot sends one message:
a recap of the week that just ended (plan vs. imported runs and your fuel log), a preview of the
coming week (each run with effort, strength days, and the week's focus from approved knowledge
sections), and one question about your week. Reply to adjust anything. It only runs while the
plan covers that week or the next, and costs about 15 to 20 cents each.

## Notes

- Each answer costs about 10 to 15 cents in API usage, the same as `coach ask`.
- The bot remembers the last 10 exchanges in a DM and forgets after 6 hours of quiet, or when it
  restarts.
- Other people in the workspace who message it get "Sorry, this coach is private for now."
- Revoke a leaked token from the app's settings page and put the new one in `.env`.
