# Strava integration review — 2026-10-12

Status: blocked on provider permission for the intended product use. No OAuth routes, tokens, provider activity storage or synchronization jobs have been enabled.

## Product compatibility

The [API Policy effective June 1, 2026](https://www.strava.com/legal/api_policy) changes the assumptions behind the previous roadmap:

- Section 5.3 prohibits API data and derivatives in AI application development or operation, including context and retrieval. Section 3.5 describes a separate personal-use Strava MCP exception.
- Sections 5.4–5.5 restrict combining, analyzing and persistently indexing provider data. Section 6.2 permits only a seven-day transient cache.
- Sections 2.5 and 7.4 require deletion following withdrawal or a deletion request. Section 6.3 requires reflecting Strava-side deletions within 48 hours.

These constraints conflict with the planned persistent combined activity history and AI coaching. The existing `activities` table and trailing summaries must not become the destination for Strava API records under the current assumptions. Credentials alone do not resolve this blocker. Obtain explicit provider permission for this product use, or agree on a separate permitted product design before implementing OAuth/sync. Do not send provider data to the model gateway or derive coaching context from it.

Manual records and direct watch FIT imports remain the current development path. Hardware-specific compatibility still requires an actual watch export.

## Verified technical requirements for a permitted future slice

The [authentication documentation](https://developers.strava.com/docs/authentication/) requires app registration, a server-held client secret, a registered callback domain (loopback is allowed), authorization-code exchange, and checking granted scopes. Request only activity read scopes needed by the agreed design; `activity:read_all` includes Only You activities. Persist a short-lived, single-use OAuth state bound to the signed-in account. The current SameSite=Strict session cookie will not accompany a cross-site callback, so the callback needs an intentional state/session binding design.

Access tokens expire after six hours. Refresh-token rotation must be serialized and saved atomically. Use the documented `POST https://www.strava.com/oauth/revoke` endpoint for disconnect; the old deauthorize endpoint retires June 1, 2027.

The [rate-limit documentation](https://developers.strava.com/docs/rate-limits/) currently lists default overall quotas of 200 requests per 15 minutes / 2,000 daily and read quotas of 100 / 1,000. Actual response headers and the app dashboard are authoritative for the registered app. New apps start with one athlete; expanded access and subscription requirements need checking in that dashboard.

If provider permission permits a future bounded sync, use a durable job with a fixed date window, page/request budget, persisted cursor, retry backoff and application-wide quota accounting. Preserve provider identity, handle revoke/delete events and enforce the approved retention policy. Test interruption and reprocessing before enabling real requests. Webhooks require externally reachable infrastructure and should remain disabled until that access is available.

The [changelog](https://developers.strava.com/docs/changelog/) announces `https://api-v3.strava.com` starting January 4, 2027; it is not the current base URL. Recheck documentation before implementation.
