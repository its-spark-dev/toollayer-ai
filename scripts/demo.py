#!/usr/bin/env python3
"""Drive the whole ToolLayer AI flow against running services.

Run it with ``make demo``. It performs, in order:

1. register the synthetic Support API with the Control Plane;
2. review the proposal — exclude one operation, restrict one write tool;
3. publish an immutable version;
4. create a deployment and an immutable snapshot;
5. make the Runtime load the snapshot;
6. ask a question in natural language and show the governed execution;
7. show three rejections, each with its own error code.

Everything runs offline against the deterministic provider, so the output is the same on
every run — which is what makes it usable as a demo and as a smoke test.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC = REPO_ROOT / "examples" / "support-api.openapi.yaml"

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"


class DemoError(RuntimeError):
    """A step did not produce the expected result."""


def step(number: int, title: str) -> None:
    print(f"\n{BOLD}[{number}] {title}{RESET}")


def detail(text: str) -> None:
    print(f"    {DIM}{text}{RESET}")


def good(text: str) -> None:
    print(f"    {GREEN}✓{RESET} {text}")


def rejected(code: str, text: str) -> None:
    print(f"    {YELLOW}✗{RESET} {text} {DIM}({code}){RESET}")


class Client:
    def __init__(self, base_url: str, headers: dict[str, str]) -> None:
        self._base = base_url.rstrip("/")
        self._headers = headers
        self._client = httpx.Client(timeout=30.0, follow_redirects=False, trust_env=False)

    def request(self, method: str, path: str, **kwargs: Any) -> tuple[int, Any]:
        response = self._client.request(
            method, f"{self._base}{path}", headers=self._headers, **kwargs
        )
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, None

    def expect(self, method: str, path: str, expected: int, **kwargs: Any) -> Any:
        status, body = self.request(method, path, **kwargs)
        if status != expected:
            raise DemoError(f"{method} {path} returned {status}, expected {expected}: {body}")
        return body

    def close(self) -> None:
        self._client.close()


def wait_for(client: Client, path: str, *, label: str, attempts: int = 40) -> None:
    for _ in range(attempts):
        try:
            status, _ = client.request("GET", path)
            if status < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise DemoError(f"{label} did not become available")


def run(control_plane_url: str, runtime_url: str, admin_token: str) -> int:
    control = Client(control_plane_url, {"x-toollayer-admin-token": admin_token})
    agent = Client(
        runtime_url,
        {"x-toollayer-caller": "avery@example.org", "x-toollayer-roles": "support-agent"},
    )
    lead = Client(
        runtime_url,
        {"x-toollayer-caller": "bao@example.org", "x-toollayer-roles": "support-lead"},
    )

    try:
        print(f"{BOLD}ToolLayer AI — end-to-end demonstration{RESET}")
        detail(f"control plane: {control_plane_url}")
        detail(f"runtime:       {runtime_url}")

        wait_for(control, "/healthz", label="the control plane")

        step(1, "Register the synthetic Support API")
        draft = control.expect(
            "POST",
            "/admin/v1/connectors",
            201,
            json={
                "connector_key": "support-api",
                "document": SPEC.read_text(encoding="utf-8"),
                "document_filename": SPEC.name,
            },
        )
        detail(
            f"source digest {draft['source']['digest'][:23]}…  "
            f"({draft['source']['byte_length']} bytes)"
        )
        good(f"analyzed {len(draft['analysis']['operations'])} operations into tool definitions")
        for entry in draft["analysis"]["operations"]:
            tool = entry["tool"]
            print(
                f"      {entry['key']:44} →  {tool['tool_name']:32} "
                f"{DIM}{tool['policy']['effect_class']}{RESET}"
            )

        step(2, "Review the proposal")
        reviewed = control.expect(
            "PATCH",
            "/admin/v1/connectors/support-api/draft",
            200,
            json={
                "expected_revision": draft["revision"],
                "operations": [
                    {"operation_key": "get /v1/teams", "selection": "excluded"},
                    {
                        "operation_key": "post /v1/tickets/{ticket_id}/status",
                        "access_mode": "restricted",
                        "allowed_roles": ["support-lead"],
                        "requires_confirmation": True,
                    },
                ],
            },
        )
        good("excluded list_support_teams from publication")
        good("restricted change_support_ticket_status to the support-lead role")
        good(f"draft is ready to publish (revision {reviewed['revision']})")

        step(3, "Publish an immutable version")
        published = control.expect(
            "POST",
            "/admin/v1/connectors/support-api/publish",
            201,
            json={"expected_revision": reviewed["revision"], "version": "0.1.0"},
        )
        good(f"published 0.1.0 with {published['tool_count']} tools")
        detail(f"document digest {published['document_digest'][:23]}…")

        for provider in ("openai", "anthropic"):
            projection = control.expect(
                "GET",
                f"/admin/v1/connectors/support-api/versions/0.1.0/adapters/{provider}",
                200,
            )
            good(
                f"{provider:9} adapter projected {len(projection['tools'])} tools "
                f"({'complete' if projection['complete'] else 'partial'})"
            )

        step(4, "Create a deployment and an immutable snapshot")
        status, _ = control.request(
            "POST",
            "/admin/v1/deployments",
            json={"deployment_key": "demo-workspace", "display_name": "Demo Workspace"},
        )
        if status not in (201, 409):
            raise DemoError(f"creating the deployment returned {status}")
        snapshot = control.expect(
            "POST",
            "/admin/v1/deployments/demo-workspace/snapshots",
            201,
            json={"selections": [{"connector_key": "support-api", "version": "0.1.0"}]},
        )
        good(
            f"snapshot revision {snapshot['revision']} pins "
            f"{snapshot['connector_count']} connector, {snapshot['tool_count']} tools"
        )
        detail(f"snapshot id {snapshot['snapshot_id']}")
        detail(f"snapshot digest {snapshot['snapshot_digest'][:23]}…")

        step(5, "Load the snapshot in the Runtime")
        wait_for(agent, "/healthz", label="the runtime")
        loaded = agent.expect("POST", "/v1/snapshot/refresh", 200)
        good(
            f"runtime is serving snapshot revision {loaded['snapshot_revision']} "
            f"with {loaded['tool_count']} tools"
        )

        step(6, "Tool discovery is role-aware")
        agent_tools = agent.expect("GET", "/v1/tools", 200)["tools"]
        lead_tools = lead.expect("GET", "/v1/tools", 200)["tools"]
        good(f"support-agent sees {len(agent_tools)} tools")
        good(f"support-lead  sees {len(lead_tools)} tools")
        detail(
            "difference: "
            + ", ".join(
                sorted(
                    {tool["tool_name"] for tool in lead_tools}
                    - {tool["tool_name"] for tool in agent_tools}
                )
            )
        )

        step(7, "Ask a question in natural language")
        question = "show me the open high priority tickets for the billing team"
        print(f'    {BOLD}"{question}"{RESET}')
        outcome = agent.expect("POST", "/v1/chat", 200, json={"utterance": question})
        good(f"selected tool  {outcome['selected_tool']}")
        good(f"arguments      {json.dumps(outcome['arguments'], sort_keys=True)}")
        good(
            f"upstream       HTTP {outcome['result']['http_status']} "
            f"in {outcome['result']['duration_ms']} ms (content marked untrusted)"
        )
        print()
        for line in outcome["message"].splitlines():
            print(f"      {line}")

        step(8, "An authorized caller changes state, with confirmation")
        write = lead.expect(
            "POST",
            "/v1/chat",
            200,
            json={"utterance": "mark ticket TKT-1003 as in progress", "confirmed": True},
        )
        good(f"selected tool  {write['selected_tool']}")
        good(f"arguments      {json.dumps(write['arguments'], sort_keys=True)}")

        step(9, "Rejections")
        checks = [
            (
                agent,
                "POST",
                "/v1/tools/change_support_ticket_status/execute",
                {
                    "arguments": {"ticket_id": "TKT-1001", "body": {"status": "closed"}},
                    "confirmed": True,
                },
                "an unauthorized role calls the restricted write tool",
            ),
            (
                lead,
                "POST",
                "/v1/tools/delete_every_ticket/execute",
                {"arguments": {}},
                "a fabricated tool name that is not in the snapshot",
            ),
            (
                agent,
                "POST",
                "/v1/tools/list_support_tickets/execute",
                {"arguments": {"status": "open", "callback_url": "https://attacker.test/collect"}},
                "an argument the published schema does not declare",
            ),
            (
                lead,
                "POST",
                "/v1/chat",
                {"utterance": "mark ticket TKT-1005 as closed"},
                "a state change without explicit confirmation",
            ),
        ]
        failures = 0
        for client, method, path, payload, description in checks:
            status, body = client.request(method, path, json=payload)
            if status < 400:
                print(f"    {RED}!{RESET} NOT REJECTED: {description}")
                failures += 1
                continue
            rejected(body["error"]["code"], description)

        print()
        if failures:
            print(f"{RED}{failures} control(s) did not reject as expected{RESET}")
            return 1
        print(f"{GREEN}{BOLD}Demo complete.{RESET} Every control behaved as documented.")
        detail("see tests/security/ for the same rejections as assertions")
        return 0

    finally:
        control.close()
        agent.close()
        lead.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the ToolLayer AI demonstration flow.")
    parser.add_argument("--control-plane-url", default="http://localhost:8080")
    parser.add_argument("--runtime-url", default="http://localhost:8082")
    parser.add_argument("--admin-token", default="dev-admin-token-change-me")
    args = parser.parse_args(argv)

    try:
        return run(args.control_plane_url, args.runtime_url, args.admin_token)
    except DemoError as error:
        print(f"\n{RED}demo failed:{RESET} {error}", file=sys.stderr)
        return 1
    except httpx.HTTPError as error:
        print(f"\n{RED}could not reach a service:{RESET} {error}", file=sys.stderr)
        print("start the stack first with: docker compose up -d", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
