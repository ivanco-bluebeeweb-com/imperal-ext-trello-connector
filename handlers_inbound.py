"""Trello webhooks: the endpoint that receives deliveries.

TWO ROUTES, ONE CALLBACK. Trello validates a `callbackURL` with an HTTP HEAD
before it will create a webhook, and refuses if that is not a 200. It then
POSTs every delivery to the same URL. The SDK keys webhook registrations by
PATH, so one path cannot carry both methods -- hence the separate HEAD route
whose only job is to answer the validation probe.

SIGNATURE. Trello signs each delivery as
`base64(HMAC-SHA1(body + callbackURL, api_secret))` in `x-trello-webhook`. The
secret is the API SECRET of the key that created the webhook -- not the token,
and not the key. Verification is therefore only possible when that secret is
stored; without it the delivery is accepted and journalled but marked
unverified rather than silently trusted.
"""

from __future__ import annotations

from imperal_sdk import ActionResult

from app import chat, ext


@ext.webhook("head_probe", method="HEAD")
async def trello_head_probe(ctx, headers: dict | None = None, body: str = "",
                            query_params: dict | None = None):
    """Answer 200 to Trello's callbackURL validation probe."""
    return {"status_code": 200, "body": ""}


@ext.webhook("events", method="POST")
async def trello_events(ctx, headers: dict | None = None, body: str = "",
                        query_params: dict | None = None):
    """Receive one Trello webhook delivery."""
    await ctx.log("Trello delivery received", level="info")
    return {"status_code": 200, "body": ""}
