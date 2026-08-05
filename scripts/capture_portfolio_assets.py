#!/usr/bin/env python3
"""Capture the README's screenshots by driving the real application.

Run it with ``make capture``. Every image in ``docs/assets/`` is produced by this script from
the running system — nothing is composed by hand, so an image that stops matching the code is
a build failure rather than a stale file somebody forgot.

Determinism is the point:

* a fixed 1440x900 viewport at 2x, so images are consistent and legible;
* the demo API's synthetic state is reseeded before the run;
* the deterministic model provider, so the runtime picks the same tool every time;
* animations disabled, so a frame is never captured mid-transition.

Everything it captures is synthetic. The tickets, teams, and people are invented, and the
only credentials involved are the shipped development placeholders.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
ASSETS = REPO_ROOT / "docs" / "assets"
SPEC = REPO_ROOT / "examples" / "support-api.openapi.yaml"

VIEWPORT = {"width": 1440, "height": 900}
SCALE = 2

#: Disable animation so a screenshot is never taken mid-transition, and stabilise the caret
#: so a focused textarea does not blink into one capture and out of the next.
STABILISE_CSS = """
  *, *::before, *::after {
    animation-duration: 0s !important;
    animation-delay: 0s !important;
    transition-duration: 0s !important;
    transition-delay: 0s !important;
    caret-color: transparent !important;
  }
  ::-webkit-scrollbar { width: 8px; height: 8px; }
"""


@dataclass(frozen=True, slots=True)
class Endpoints:
    console: str
    control_plane: str
    runtime: str
    demo_api: str


class CaptureError(RuntimeError):
    """A capture step did not produce what the README needs."""


# --------------------------------------------------------------------------------------
# Control-plane and runtime driving
# --------------------------------------------------------------------------------------


def reset_control_plane(endpoints: Endpoints, admin_token: str) -> dict[str, Any]:
    """Put the Control Plane into the exact state the screenshots describe.

    Driven through the public HTTP API rather than the database, so the captured state is
    reachable the same way a real administrator would reach it.
    """
    headers = {"x-toollayer-admin-token": admin_token}
    with httpx.Client(timeout=30.0, follow_redirects=False, trust_env=False) as client:

        def call(method: str, path: str, expected: tuple[int, ...], **kwargs: Any) -> Any:
            response = client.request(
                method, f"{endpoints.control_plane}{path}", headers=headers, **kwargs
            )
            if response.status_code not in expected:
                raise CaptureError(
                    f"{method} {path} returned {response.status_code}: {response.text}"
                )
            return response.json() if response.content else None

        draft = call(
            "POST",
            "/admin/v1/connectors",
            (201,),
            json={
                "connector_key": "support-api",
                "document": SPEC.read_text(encoding="utf-8"),
                "document_filename": SPEC.name,
                "base_url": endpoints.demo_api,
            },
        )

        # The same review decisions the demo and the tests make: one operation dropped, one
        # state-changing operation restricted to a role and gated behind confirmation. This is
        # what makes the later screenshots show governance rather than a pass-through.
        reviewed = call(
            "PATCH",
            "/admin/v1/connectors/support-api/draft",
            (200,),
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

        published = call(
            "POST",
            "/admin/v1/connectors/support-api/publish",
            (201,),
            json={"expected_revision": reviewed["revision"], "version": "0.1.0"},
        )
        call(
            "POST",
            "/admin/v1/deployments",
            (201, 409),
            json={"deployment_key": "demo-workspace", "display_name": "Demo Workspace"},
        )
        snapshot = call(
            "POST",
            "/admin/v1/deployments/demo-workspace/snapshots",
            (201,),
            json={"selections": [{"connector_key": "support-api", "version": "0.1.0"}]},
        )

        client.post(f"{endpoints.runtime}/v1/snapshot/refresh", timeout=30.0)
        return {"published": published, "snapshot": snapshot}


def runtime_transcript(endpoints: Endpoints) -> dict[str, Any]:
    """Collect one accepted and one refused runtime turn, as JSON."""
    agent = {"x-toollayer-caller": "avery@example.org", "x-toollayer-roles": "support-agent"}
    with httpx.Client(timeout=30.0, follow_redirects=False, trust_env=False) as client:
        accepted = client.post(
            f"{endpoints.runtime}/v1/chat",
            headers=agent,
            json={"utterance": "show me the open high priority tickets for the billing team"},
        )
        if accepted.status_code != 200:
            raise CaptureError(f"the runtime refused the demo question: {accepted.text}")

        refused = client.post(
            f"{endpoints.runtime}/v1/tools/change_support_ticket_status/execute",
            headers=agent,
            json={
                "arguments": {"ticket_id": "TKT-1001", "body": {"status": "closed"}},
                "confirmed": True,
            },
        )
        if refused.status_code != 403:
            raise CaptureError(
                f"the restricted tool was not refused as expected: {refused.status_code}"
            )

        tools = client.get(f"{endpoints.runtime}/v1/tools", headers=agent).json()

    return {"accepted": accepted.json(), "refused": refused.json(), "tools": tools}


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------


def render_terminal(title: str, lines: list[tuple[str, str]]) -> str:
    """Build a terminal-styled HTML page for the runtime transcripts.

    The runtime has no UI of its own — it is a service, and giving it a fake one would
    misrepresent the project. Rendering its real JSON responses as terminal output shows what
    a client actually receives, which is the honest picture.
    """
    palette = {
        "dim": "#7d8590",
        "text": "#e6edf3",
        "ok": "#3fb950",
        "warn": "#d29922",
        "bad": "#f85149",
        "accent": "#79c0ff",
        "key": "#a5d6ff",
    }
    body = "\n".join(f'<div class="line line--{kind}">{content}</div>' for kind, content in lines)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 28px; background: #0d1117;
    font: 14px/1.65 ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    color: {palette["text"]};
  }}
  .window {{
    border: 1px solid #30363d; border-radius: 10px; overflow: hidden;
    background: #0d1117; box-shadow: 0 12px 40px rgba(0,0,0,0.5);
  }}
  .titlebar {{
    display: flex; align-items: center; gap: 8px;
    padding: 11px 14px; background: #161b22; border-bottom: 1px solid #30363d;
  }}
  .dot {{ width: 11px; height: 11px; border-radius: 50%; }}
  .dot--r {{ background: #ff5f57; }}
  .dot--y {{ background: #febc2e; }}
  .dot--g {{ background: #28c840; }}
  .title {{ margin-left: 8px; color: {palette["dim"]}; font-size: 12.5px; }}
  .content {{ padding: 18px 20px 22px; }}
  .line {{ white-space: pre-wrap; word-break: break-word; }}
  .line--cmd {{ color: {palette["text"]}; }}
  .line--dim {{ color: {palette["dim"]}; }}
  .line--ok {{ color: {palette["ok"]}; }}
  .line--warn {{ color: {palette["warn"]}; }}
  .line--bad {{ color: {palette["bad"]}; }}
  .line--accent {{ color: {palette["accent"]}; }}
  .line--key {{ color: {palette["key"]}; }}
  .line--gap {{ height: 10px; }}
  b {{ color: #fff; font-weight: 600; }}
</style></head><body>
  <div class="window">
    <div class="titlebar">
      <span class="dot dot--r"></span><span class="dot dot--y"></span>
      <span class="dot dot--g"></span>
      <span class="title">{title}</span>
    </div>
    <div class="content">{body}</div>
  </div>
</body></html>"""


def accepted_transcript_lines(outcome: dict[str, Any]) -> list[tuple[str, str]]:
    result = outcome["result"]
    steps = {step["step"]: step for step in outcome["trace"]}
    content = result["content"]
    items = content.get("items", []) if isinstance(content, dict) else []
    discovered = steps.get("tools_discovered", {})

    lines: list[tuple[str, str]] = [
        ("cmd", "$ toollayer-runtime --roles support-agent ask \\"),
        ("cmd", "    &quot;show me the open high priority tickets for the billing team&quot;"),
        ("gap", ""),
        (
            "dim",
            f"snapshot   revision {outcome['snapshot']['revision']}"
            f" · {outcome['snapshot']['snapshot_id'][:24]}…",
        ),
        ("gap", ""),
        ("ok", f"✓ tool selected        <b>{outcome['selected_tool']}</b>"),
        (
            "dim",
            f"  from {len(discovered.get('visible', []))} tools this caller may use"
            f" · {discovered.get('hidden', 0)} hidden by policy",
        ),
        (
            "ok",
            "✓ arguments generated  " + json.dumps(outcome["arguments"], sort_keys=True),
        ),
        ("ok", "✓ arguments validated  against the published JSON Schema"),
        (
            "ok",
            "✓ policy evaluated     effect="
            f"{steps.get('policy_evaluated', {}).get('effect', 'read')}"
            " · caller authorised",
        ),
        (
            "ok",
            f"✓ executed             HTTP {result['http_status']} in {result['duration_ms']} ms",
        ),
        ("gap", ""),
        ("warn", "  upstream content is marked untrusted and is never read as an instruction"),
        ("gap", ""),
    ]
    lines.append(("accent", f"{len(items)} matching ticket(s):"))
    for item in items[:4]:
        lines.append(
            (
                "key",
                f"  {item.get('ticket_id')}  {item.get('status'):<12} {item.get('priority'):<8} "
                f"{str(item.get('subject'))[:52]}",
            )
        )
    return lines


def refused_transcript_lines(
    refused: dict[str, Any], tools: dict[str, Any]
) -> list[tuple[str, str]]:
    error = refused["error"]
    visible = sorted(tool["tool_name"] for tool in tools["tools"])
    return [
        ("dim", "# the caller holds the support-agent role"),
        ("cmd", "$ toollayer-runtime --roles support-agent run change_support_ticket_status \\"),
        (
            "cmd",
            "    --arguments '{&quot;ticket_id&quot;:&quot;TKT-1001&quot;,"
            "&quot;body&quot;:{&quot;status&quot;:&quot;closed&quot;}}' --confirm",
        ),
        ("gap", ""),
        ("bad", f"REJECTED [403] <b>{error['code']}</b>"),
        ("dim", f"  {error['message']}"),
        ("gap", ""),
        ("dim", "  The tool exists and the arguments are valid. Authorization is a separate"),
        ("dim", "  step, and it does not care that discovery was bypassed."),
        ("gap", ""),
        ("accent", "tools this caller may use:"),
        *[("key", f"  {name}") for name in visible],
        ("gap", ""),
        ("dim", "  change_support_ticket_status is restricted to support-lead and is not listed."),
    ]


# --------------------------------------------------------------------------------------
# Capture
# --------------------------------------------------------------------------------------


def capture(endpoints: Endpoints, admin_token: str, *, animate: bool) -> list[Path]:
    from playwright.sync_api import sync_playwright

    ASSETS.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    state = reset_control_plane(endpoints, admin_token)
    transcript = runtime_transcript(endpoints)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=SCALE,
            color_scheme="light",
            reduced_motion="reduce",
        )
        page = context.new_page()
        page.add_init_script("window.localStorage.clear()")

        # A failed capture is otherwise a bare Playwright timeout with no clue why. Recording
        # what the page logged turns "the tab never appeared" into "the request was blocked".
        console_log: list[str] = []
        page.on("console", lambda message: console_log.append(f"{message.type}: {message.text}"))
        page.on("pageerror", lambda error: console_log.append(f"pageerror: {error}"))
        page.on(
            "requestfailed",
            lambda request: console_log.append(
                f"requestfailed: {request.method} {request.url} — {request.failure}"
            ),
        )

        def shot(name: str, *, full: bool = False) -> None:
            page.add_style_tag(content=STABILISE_CSS)
            page.wait_for_timeout(350)
            target = ASSETS / name
            page.screenshot(path=str(target), full_page=full, animations="disabled")
            written.append(target)
            print(f"  captured {target.relative_to(REPO_ROOT)}")

        # ---- Console: register --------------------------------------------------------
        page.goto(endpoints.console, wait_until="networkidle")
        page.wait_for_timeout(500)
        shot("01-register.png")

        # ---- Console: review, with the side-by-side transformation ---------------------
        page.get_by_role("button", name="Analyze document").click()
        try:
            page.get_by_role("tab", name="Side by side").wait_for(timeout=20_000)
        except Exception as error:
            failure = ASSETS / "_capture-failure.png"
            page.screenshot(path=str(failure))
            detail = "\n    ".join(console_log[-12:]) or "(the page logged nothing)"
            raise CaptureError(
                f"the console never reached the review stage.\n"
                f"  screenshot: {failure}\n"
                f"  page log:\n    {detail}\n"
                f"  underlying: {error}"
            ) from None
        page.wait_for_timeout(900)
        shot("02-review-transformation.png")

        # The hero crops to the transformation panel alone: at README width the full console
        # shrinks to the point where the JSON is unreadable, and the JSON is the whole point.
        panel = page.locator(".panel__right")
        panel.screenshot(path=str(ASSETS / "00-hero.png"), animations="disabled")
        written.append(ASSETS / "00-hero.png")
        print("  captured docs/assets/00-hero.png")

        # ---- Console: provider projections --------------------------------------------
        page.get_by_role("tab", name="OpenAI").click()
        page.wait_for_timeout(500)
        shot("03-provider-projection.png")

        # ---- Console: publish ----------------------------------------------------------
        page.get_by_role("button", name="1 · Register").click()
        page.wait_for_timeout(300)
        page.get_by_role("button", name="3 · Publish").click()
        page.wait_for_timeout(1200)
        shot("04-published-versions.png")

        # ---- Console: deployment snapshot ----------------------------------------------
        page.get_by_role("button", name="4 · Deploy").click()
        page.wait_for_timeout(400)
        page.get_by_role("button", name="3 · Publish").click()
        page.get_by_role("button", name="Create a deployment snapshot").click()
        page.wait_for_timeout(1600)
        shot("05-deployment-snapshot.png", full=True)

        # ---- Runtime transcripts --------------------------------------------------------
        page.set_content(
            render_terminal(
                "LLM Orchestration Runtime — governed execution",
                accepted_transcript_lines(transcript["accepted"]),
            )
        )
        page.wait_for_timeout(300)
        page.locator(".window").screenshot(path=str(ASSETS / "06-runtime-execution.png"))
        written.append(ASSETS / "06-runtime-execution.png")
        print("  captured docs/assets/06-runtime-execution.png")

        page.set_content(
            render_terminal(
                "LLM Orchestration Runtime — an unauthorized call",
                refused_transcript_lines(transcript["refused"], transcript["tools"]),
            )
        )
        page.wait_for_timeout(300)
        page.locator(".window").screenshot(path=str(ASSETS / "07-runtime-rejection.png"))
        written.append(ASSETS / "07-runtime-rejection.png")
        print("  captured docs/assets/07-runtime-rejection.png")

        context.close()

        if animate:
            record_walkthrough(browser, endpoints, admin_token)

        browser.close()

    print(
        f"\nsnapshot {state['snapshot']['snapshot_id']}"
        f" · digest {state['snapshot']['snapshot_digest'][:23]}…"
    )
    return written


def record_walkthrough(browser: Any, endpoints: Endpoints, admin_token: str) -> None:
    """Record a paced walkthrough of the console alone.

    Recorded in its own context so the video contains the four pipeline stages and nothing
    else — the still captures reuse the same page for the runtime transcripts, and those
    would otherwise appear in the middle of the walkthrough.

    The pacing is deliberate: long enough that each stage is readable without pausing, short
    enough that the whole thing stays under half a minute.
    """
    print("  recording the console walkthrough…")

    context = browser.new_context(
        viewport=VIEWPORT,
        device_scale_factor=1,
        color_scheme="light",
        reduced_motion="reduce",
        record_video_dir=str(ASSETS / "_video"),
        record_video_size=VIEWPORT,
    )
    page = context.new_page()
    page.goto(endpoints.console, wait_until="networkidle")
    page.add_style_tag(content=STABILISE_CSS)
    page.wait_for_timeout(2000)

    # Driven entirely through the console, with no API setup: the walkthrough registers a new
    # draft, and because a version was not named the Control Plane proposes the next one after
    # what the still captures already published. So the video shows a real second publication
    # rather than a replayed first one.
    page.get_by_role("button", name="Analyze document").click()
    page.get_by_role("tab", name="Side by side").wait_for(timeout=20_000)
    page.wait_for_timeout(3000)

    # The governance decision is the part worth watching: a reviewer restricting a
    # state-changing tool to one role, and the badge appearing on the operation.
    status_tool = page.get_by_role("button").filter(has_text="change_support_ticket_status").first
    if status_tool.count():
        status_tool.click()
        page.wait_for_timeout(1800)
        restrict = page.get_by_text("Only these roles").first
        if restrict.count():
            restrict.click()
            page.wait_for_timeout(2400)
        confirm = page.get_by_text("Require explicit confirmation").first
        if confirm.count():
            confirm.click()
            page.wait_for_timeout(2200)

    for tab in ("OpenAI", "Side by side"):
        page.get_by_role("tab", name=tab).click()
        page.wait_for_timeout(1800)

    publish = page.get_by_role("button", name="Publish").first
    if publish.count():
        publish.click()
        page.wait_for_timeout(3000)

    snapshot = page.get_by_role("button", name="Create a deployment snapshot")
    if snapshot.count():
        snapshot.click()
        page.wait_for_timeout(3200)
    page.mouse.wheel(0, 480)
    page.wait_for_timeout(2600)

    context.close()


def strip_metadata(paths: list[Path]) -> None:
    """Remove ancillary PNG chunks so no capture-time metadata ships in the repository.

    Playwright writes only a minimal PNG, but re-encoding through a whitelist of critical
    chunks makes that a property of this script rather than of a dependency's current
    behaviour.
    """
    import struct
    import zlib

    keep = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS"}
    for path in paths:
        if path.suffix != ".png":
            continue
        raw = path.read_bytes()
        if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            continue
        out = bytearray(raw[:8])
        offset = 8
        while offset < len(raw):
            (length,) = struct.unpack(">I", raw[offset : offset + 4])
            kind = raw[offset + 4 : offset + 8]
            chunk = raw[offset : offset + 12 + length]
            if kind in keep:
                out += chunk
            offset += 12 + length
        # Recompute nothing: chunks carry their own CRCs and are copied verbatim.
        assert zlib.crc32(b"") == 0  # cheap guard that zlib is present for the reader
        path.write_bytes(bytes(out))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture the README's portfolio assets.")
    parser.add_argument("--console-url", default="http://localhost:5173")
    parser.add_argument("--control-plane-url", default="http://localhost:8080")
    parser.add_argument("--runtime-url", default="http://localhost:8082")
    parser.add_argument("--demo-api-url", default="http://localhost:8081")
    parser.add_argument("--admin-token", default="dev-admin-token-change-me")
    parser.add_argument(
        "--animate",
        action="store_true",
        help="also record a WebM of the console walkthrough",
    )
    args = parser.parse_args(argv)

    try:
        import playwright  # noqa: F401
    except ImportError:
        print(
            "Playwright is not installed. Run:\n"
            "    pip install -e '.[capture]' && playwright install chromium",
            file=sys.stderr,
        )
        return 2

    endpoints = Endpoints(
        console=args.console_url.rstrip("/"),
        control_plane=args.control_plane_url.rstrip("/"),
        runtime=args.runtime_url.rstrip("/"),
        demo_api=args.demo_api_url.rstrip("/"),
    )

    print("Capturing portfolio assets from the running system…")
    try:
        written = capture(endpoints, args.admin_token, animate=args.animate)
    except CaptureError as error:
        print(f"\ncapture failed: {error}", file=sys.stderr)
        return 1
    except httpx.HTTPError as error:
        print(f"\ncould not reach a service: {error}", file=sys.stderr)
        print("start the stack first, or run 'make capture'.", file=sys.stderr)
        return 2

    strip_metadata(written)
    video_dir = ASSETS / "_video"
    if video_dir.exists():
        videos = sorted(video_dir.glob("*.webm"))
        if videos:
            target = ASSETS / "control-plane-walkthrough.webm"
            shutil.move(str(videos[-1]), target)
            print(f"  captured {target.relative_to(REPO_ROOT)}")
        shutil.rmtree(video_dir, ignore_errors=True)

    total = sum(path.stat().st_size for path in ASSETS.glob("*") if path.is_file())
    print(f"\n{len(list(ASSETS.glob('*.png')))} images · {total / 1024:.0f} KiB total")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
