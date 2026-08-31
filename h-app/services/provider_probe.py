"""Probe a local model provider endpoint before accepting it in the wizard.

Ported from the reference project's setup.sh, same shape and same
reasoning -- see that script for the measured findings this mirrors:

⚠ ASK, THEN VERIFY -- and verify with a REAL served model id. claude talks
to /v1/messages; a provider that doesn't answer there reports "issue with
the selected model" from claude's side, which reads as a model problem and
isn't one. A 404 alone doesn't mean the route is missing either -- vLLM
answers an unknown model with 404 too, so probing with a made-up id would
condemn a working provider. Discover a real id via /v1/models (falling back
to ollama's /api/tags) and probe with that.

⚠ A GENEROUS timeout on the probe itself, not a short one. A local model
that has to load answers in tens of seconds the first time and under a
second once warm -- a short timeout turns a cold start into a false verdict
about the provider.
"""

import argparse
import json
import urllib.error
import urllib.request
from collections.abc import Sequence

DEFAULT_MODELS_TIMEOUT = 5
DEFAULT_MESSAGE_TIMEOUT = 90


def _get(url: str, timeout: float) -> tuple[int | None, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")
    except Exception:
        return None, ""


def _post_json(url: str, payload: dict, timeout: float) -> tuple[int | None, str]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")
    except Exception:
        return None, ""


def list_served_models(
    base_url: str, kind: str, *, timeout: float = DEFAULT_MODELS_TIMEOUT
) -> list[str]:
    """Model ids this endpoint actually serves -- offer these rather than
    asking someone to type one: ollama ids carry a tag (gpt-oss:20b) that's
    easy to mistype as gpt-oss-20b."""
    status, body = _get(f"{base_url}/v1/models", timeout)
    if status == 200:
        try:
            data = json.loads(body)
            ids = [m["id"] for m in data.get("data", [])]
            if ids:
                return ids
        except (ValueError, KeyError, TypeError):
            pass
    if kind == "ollama":
        status, body = _get(f"{base_url}/api/tags", timeout)
        if status == 200:
            try:
                data = json.loads(body)
                return [m["name"] for m in data.get("models", [])]
            except (ValueError, KeyError, TypeError):
                pass
    return []


class ProbeResult:
    def __init__(self, ok: bool, message: str):
        self.ok = ok
        self.message = message


def probe_messages_endpoint(
    base_url: str, model: str, *, timeout: float = DEFAULT_MESSAGE_TIMEOUT
) -> ProbeResult:
    """POST /v1/messages with a real served model id -- claude's own wire shape."""
    payload = {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}
    status, body = _post_json(f"{base_url}/v1/messages", payload, timeout)
    if status is None:
        return ProbeResult(
            False,
            f"{base_url}/v1/messages did not answer within {timeout:.0f}s. That's 'no answer', "
            "not 'not served' -- a model still loading looks the same from here. Try again "
            "once it's warm.",
        )
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if isinstance(data, dict) and data.get("type") == "message":
        return ProbeResult(True, "/v1/messages answered -- claude can use this provider")
    return ProbeResult(False, f"/v1/messages answered, but not with a message: {body[:160]}")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="h-mesh provider-probe")
    sub = parser.add_subparsers(dest="command", required=True)

    p_models = sub.add_parser("models", help="list model ids served at this endpoint")
    p_models.add_argument("url")
    p_models.add_argument("kind", choices=("vllm", "ollama"))

    p_probe = sub.add_parser("probe", help="verify /v1/messages answers for this model id")
    p_probe.add_argument("url")
    p_probe.add_argument("model")

    args = parser.parse_args(argv)
    if args.command == "models":
        for model_id in list_served_models(args.url, args.kind):
            print(model_id)
    elif args.command == "probe":
        result = probe_messages_endpoint(args.url, args.model)
        print(result.message)
        raise SystemExit(0 if result.ok else 1)


if __name__ == "__main__":
    main()
