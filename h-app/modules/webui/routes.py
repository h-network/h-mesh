"""Browser-facing routes for the webui port_type, mounted onto the
already-running api service (see modules/webui/port.py's own docstring for
why this is not a second daemon).

Every route here requires the exact same Bearer token the rest of the api
service already requires (``create_app``'s app-level ``authorize``
dependency covers routes registered here too, since they are added to the
same ``FastAPI`` instance) -- deliberately not loosened for these routes.
Native browser ``EventSource`` cannot set an ``Authorization`` header, so
the served page below does not use it; its own JS opens the live stream with
``fetch()`` (which can set the header) and parses the ``text/event-stream``
framing by hand instead. This keeps the existing token-only auth boundary
completely untouched -- no new ``?token=`` query-param fallback was added to
``authorize()`` to make plain ``EventSource`` work, which would have widened
every existing api route's auth surface, not just these new ones.
"""

from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from core.keys import prefix
from core.registry import port_type
from lib.sse_stream import read_stream_entries, stream_response

# Own constants, not shared with modules/api/server.py's -- see
# lib/sse_stream.py's module docstring for why keeping these local (rather
# than importing api's) is what keeps a test able to patch one service's
# interval without silently also changing the other's.
SSE_KEEPALIVE_INTERVAL_S = 3.0
SSE_POLL_INTERVAL_S = 0.1

_PAGE_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>h-mesh live: __AGENT__</title>
<style>
  body { font-family: monospace; background: #111; color: #ddd; margin: 2rem; }
  #log { white-space: pre-wrap; }
  .event { border-bottom: 1px solid #333; padding: 0.4em 0; }
  .kind { color: #6cf; }
  .stale { color: #888; }
  input { font-family: monospace; }
</style>
</head>
<body>
<h3>live: __AGENT__</h3>
<p>
  API token (leave blank if this server has none configured):
  <input id="token" type="password" size="40">
  <button id="connect">connect</button>
  <span id="status" class="stale">not connected</span>
</p>
<div id="log"></div>
<script>
(function () {
  var agent = "__AGENT__";
  var logEl = document.getElementById("log");
  var statusEl = document.getElementById("status");

  function append(kind, data) {
    var row = document.createElement("div");
    row.className = "event";
    var kindEl = document.createElement("span");
    kindEl.className = "kind";
    kindEl.textContent = "[" + kind + "] ";
    row.appendChild(kindEl);
    var textEl = document.createElement("span");
    textEl.textContent = JSON.stringify(data);
    row.appendChild(textEl);
    logEl.appendChild(row);
    row.scrollIntoView();
  }

  // Native EventSource cannot set an Authorization header, so this parses
  // the text/event-stream framing by hand over a fetch() response instead.
  async function connect(token) {
    var headers = {};
    if (token) headers["Authorization"] = "Bearer " + token;
    statusEl.textContent = "connecting...";
    statusEl.className = "";
    var resp = await fetch("/agents/" + agent + "/live/stream", { headers: headers });
    if (!resp.ok) {
      statusEl.textContent = "failed: HTTP " + resp.status;
      statusEl.className = "stale";
      return;
    }
    statusEl.textContent = "connected";
    var reader = resp.body.getReader();
    var decoder = new TextDecoder();
    var buffer = "";
    while (true) {
      var chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      var blocks = buffer.split("\\n\\n");
      buffer = blocks.pop();
      for (var i = 0; i < blocks.length; i++) {
        var block = blocks[i];
        if (!block || block.charAt(0) === ":") continue;
        var kind = "message";
        var dataLine = null;
        var lines = block.split("\\n");
        for (var j = 0; j < lines.length; j++) {
          var line = lines[j];
          if (line.indexOf("event:") === 0) kind = line.slice(6).trim();
          if (line.indexOf("data:") === 0) dataLine = line.slice(5).trim();
        }
        if (dataLine === null) continue;
        try {
          append(kind, JSON.parse(dataLine));
        } catch (e) {
          append(kind, dataLine);
        }
      }
    }
    statusEl.textContent = "disconnected";
    statusEl.className = "stale";
  }

  document.getElementById("connect").addEventListener("click", function () {
    connect(document.getElementById("token").value);
  });
})();
</script>
</body>
</html>"""


def _require_webui_agent(client: Any, *, pod: str, tenant: str, agent: str) -> str:
    try:
        inbox_key = prefix(pod, tenant, agent, "inbox")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="invalid agent") from exc
    if port_type(client, pod=pod, tenant=tenant, agent=agent) != "webui":
        raise HTTPException(status_code=404, detail="invalid agent")
    return inbox_key


def register_webui_routes(app: FastAPI, *, settings: Any, client: Any) -> None:
    """Mount the webui-facing routes onto an already-constructed api app."""

    @app.get("/agents/{agent}/live", include_in_schema=False)
    def live_page(agent: str) -> HTMLResponse:
        _require_webui_agent(client, pod=settings.pod, tenant=settings.tenant, agent=agent)
        return HTMLResponse(content=_PAGE_TEMPLATE.replace("__AGENT__", agent))

    @app.get("/agents/{agent}/live/events")
    def live_events(
        agent: str,
        after: str | None = None,
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> dict[str, Any]:
        inbox_key = _require_webui_agent(client, pod=settings.pod, tenant=settings.tenant, agent=agent)
        events = read_stream_entries(client, inbox_key, after=after, limit=limit, preferred_field="envelope")
        next_cursor = events[-1]["cursor"] if events else after
        return {
            "agent": agent,
            "events": events,
            "next_cursor": next_cursor,
        }

    @app.get("/agents/{agent}/live/stream", include_in_schema=False)
    async def live_stream(
        agent: str,
        request: Request,
        after: str | None = None,
    ) -> StreamingResponse:
        inbox_key = _require_webui_agent(client, pod=settings.pod, tenant=settings.tenant, agent=agent)
        return stream_response(
            request, client, inbox_key, "live", after, "envelope",
            keepalive_interval_s=SSE_KEEPALIVE_INTERVAL_S, poll_interval_s=SSE_POLL_INTERVAL_S,
        )
