"""A small command-line client for the Runtime.

This is the "reference client" the architecture calls for. It exists to show what a client
of this runtime has to do — send text, read a structured outcome, render it — and to make the
demo runnable without a browser. It is deliberately minimal: anything more would start
turning the runtime into the chatbot product it is explicitly not.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import httpx

__all__ = ["main"]


def _post(base_url: str, path: str, payload: dict[str, Any], headers: dict[str, str]) -> Any:
    with httpx.Client(timeout=30.0, follow_redirects=False, trust_env=False) as client:
        response = client.post(f"{base_url.rstrip('/')}{path}", json=payload, headers=headers)
        return response.status_code, response.json()


def _get(base_url: str, path: str, headers: dict[str, str]) -> Any:
    with httpx.Client(timeout=30.0, follow_redirects=False, trust_env=False) as client:
        response = client.get(f"{base_url.rstrip('/')}{path}", headers=headers)
        return response.status_code, response.json()


def _headers(args: argparse.Namespace) -> dict[str, str]:
    headers: dict[str, str] = {}
    if args.caller:
        headers["x-toollayer-caller"] = args.caller
    if args.roles:
        headers["x-toollayer-roles"] = args.roles
    return headers


def _render(status: int, body: Any, *, verbose: bool) -> int:
    if status >= 400:
        error = body.get("error", {}) if isinstance(body, dict) else {}
        print(f"REJECTED [{status}] {error.get('code', 'unknown')}: {error.get('message', '')}")
        for detail in error.get("details", [])[:5]:
            print(f"  - {detail.get('code')}: {detail.get('message')}")
        return 1

    if isinstance(body, dict) and "tools" in body:
        print(f"{len(body['tools'])} tool(s) available to {body.get('caller') or 'anonymous'}:")
        for tool in body["tools"]:
            marker = "!" if tool["effect_class"] != "read" else " "
            print(
                f" {marker} {tool['tool_name']:34} {tool['effect_class']:12} {tool['display_name']}"
            )
        return 0

    print(f"tool      : {body.get('selected_tool')}")
    print(f"connector : {body.get('connector_key')} {body.get('connector_version')}")
    print(f"arguments : {json.dumps(body.get('arguments', {}), sort_keys=True)}")
    result = body.get("result")
    if result:
        print(
            f"upstream  : HTTP {result['http_status']} in "
            f"{result['duration_ms']} ms (untrusted content)"
        )
    print()
    print(body.get("message", ""))
    if verbose:
        print()
        print("trace:")
        for step in body.get("trace", []):
            print(f"  - {json.dumps(step, sort_keys=True)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="toollayer-runtime",
        description="A minimal reference client for the ToolLayer AI runtime.",
    )
    parser.add_argument("--url", default="http://localhost:8082", help="runtime base URL")
    parser.add_argument("--caller", default=None, help="caller subject to assert")
    parser.add_argument("--roles", default=None, help="comma-separated roles to assert")
    parser.add_argument("-v", "--verbose", action="store_true", help="print the decision trace")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("tools", help="list the tools this caller may use")

    ask = subparsers.add_parser("ask", help="send one natural-language request")
    ask.add_argument("utterance", help="the request text")
    ask.add_argument("--confirm", action="store_true", help="supply confirmation up front")
    ask.add_argument("--dry-run", action="store_true", help="stop before calling the API")

    run = subparsers.add_parser("run", help="execute one named tool directly")
    run.add_argument("tool_name")
    run.add_argument("--arguments", default="{}", help="arguments as a JSON object")
    run.add_argument("--confirm", action="store_true")

    args = parser.parse_args(argv)
    headers = _headers(args)

    try:
        if args.command == "tools":
            status, body = _get(args.url, "/v1/tools", headers)
        elif args.command == "ask":
            status, body = _post(
                args.url,
                "/v1/chat",
                {"utterance": args.utterance, "confirmed": args.confirm, "dry_run": args.dry_run},
                headers,
            )
        else:
            try:
                arguments = json.loads(args.arguments)
            except ValueError:
                print("--arguments must be a JSON object", file=sys.stderr)
                return 2
            status, body = _post(
                args.url,
                f"/v1/tools/{args.tool_name}/execute",
                {"arguments": arguments, "confirmed": args.confirm},
                headers,
            )
    except httpx.HTTPError:
        print(f"could not reach the runtime at {args.url}", file=sys.stderr)
        return 2

    return _render(status, body, verbose=args.verbose)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
