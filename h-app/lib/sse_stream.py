"""Redis Stream polling shared by every HTTP door that offers cursor
catch-up plus a live Server-Sent Events tail.

Extracted from modules/api/server.py once modules/webui/routes.py needed
the exact same poll/keepalive machinery for a second Redis Stream (a
webui agent's own "inbox", written by modules/webui/port.py) -- two real
callers, not a hypothetical third. modules/api/server.py's own six
existing call sites (messages, activity, board's per-agent reads, alerts)
are unchanged in behavior; only the functions' address moved.

The keepalive/poll intervals are deliberately NOT constants owned here:
each caller keeps its own module-level ``SSE_KEEPALIVE_INTERVAL_S``/
``SSE_POLL_INTERVAL_S`` (api and webui may reasonably tune these
differently) and a test that patches a caller's own module attribute
(e.g. ``modules.api.server.SSE_KEEPALIVE_INTERVAL_S``) keeps working --
patching a name in the caller's namespace could not otherwise reach a
constant this module read from its own globals instead.
"""

from typing import Any

import redis
from fastapi import HTTPException, Request, status
from fastapi.responses import StreamingResponse
import asyncio
import json
import time


def read_stream_entries(
    client: Any,
    key: str,
    after: str | None,
    limit: int,
    preferred_field: str = "envelope",
) -> list[dict[str, Any]]:
    min_id = f"({after}" if after else "-"
    try:
        raw_entries = client.xrange(key, min=min_id, max="+", count=limit)
    except Exception as exc:
        if isinstance(exc, redis.exceptions.RedisError):
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
        return []

    entries = []
    b_pref = preferred_field.encode()
    s_pref = preferred_field

    for entry_id, fields in raw_entries:
        cid = entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)
        raw_val = None
        if b_pref in fields:
            raw_val = fields[b_pref]
        elif s_pref in fields:
            raw_val = fields[s_pref]
        elif fields:
            raw_val = next(iter(fields.values()))
        if not raw_val:
            continue
        val_str = raw_val.decode() if isinstance(raw_val, bytes) else str(raw_val)
        try:
            val_dict = json.loads(val_str)
        except (json.JSONDecodeError, TypeError):
            continue
        val_dict["cursor"] = cid
        entries.append(val_dict)
    return entries


def stream_response(
    request: Request,
    client: Any,
    key: str,
    event_name: str,
    after: str | None,
    preferred_field: str,
    *,
    keepalive_interval_s: float,
    poll_interval_s: float,
) -> StreamingResponse:
    header_last_id = request.headers.get("last-event-id")
    cursor = after or header_last_id

    async def event_generator():
        nonlocal cursor
        last_sent = time.monotonic()
        while True:
            if await request.is_disconnected():
                break
            try:
                entries = await asyncio.to_thread(
                    read_stream_entries,
                    client, key, cursor, 100, preferred_field
                )
            except Exception as exc:
                err_detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
                err_payload = json.dumps({"error": err_detail})
                yield f"event: error\ndata: {err_payload}\n\n"
                break
            if entries:
                for entry in entries:
                    cid = entry["cursor"]
                    cursor = cid
                    data_json = json.dumps(entry)
                    yield f"id: {cid}\nevent: {event_name}\ndata: {data_json}\n\n"
                last_sent = time.monotonic()
            else:
                now = time.monotonic()
                if now - last_sent >= keepalive_interval_s:
                    # A bare comment line: valid SSE, ignored by EventSource
                    # and by clients/telegram/bot.py's parser (any line
                    # starting with ':' is skipped), but it is a byte on the
                    # wire -- which is the whole point. An idle stream that
                    # never sends a byte looks identical, to anything
                    # watching the connection from outside this generator,
                    # to a stream nobody is reading anymore.
                    yield ": keepalive\n\n"
                    last_sent = now
                await asyncio.sleep(poll_interval_s)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
