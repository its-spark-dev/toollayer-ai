# System context

A C4-style context view: who and what interacts with ToolLayer AI, and across which
boundaries.

## Context diagram

```mermaid
C4Context
    title ToolLayer AI — system context

    Person(admin, "Connector administrator", "Registers API descriptions, reviews generated tools, publishes versions, and creates deployment snapshots.")
    Person(user, "End user", "Asks a question in natural language through an AI application. Never talks to ToolLayer AI directly.")

    System_Boundary(toollayer, "ToolLayer AI") {
        System(cp, "Tool Control Plane", "Turns OpenAPI descriptions into governed, versioned, provider-neutral tool definitions. Owns configuration time.")
        System(rt, "LLM Orchestration Runtime", "Consumes an immutable deployment snapshot and executes validated, authorized tool calls. Owns request time. A reference implementation, not a chatbot product.")
    }

    System_Ext(client, "AI application", "The reference client, playground, or CLI. Authenticates the end user and asserts their roles to the runtime.")
    System_Ext(api, "Upstream HTTP API", "The service the tools call. In this repository, the synthetic Sample Support API.")
    System_Ext(model, "Model provider", "Selects a tool and proposes arguments. A deterministic offline provider by default; a real provider is optional.")

    Rel(admin, cp, "Registers, reviews, publishes, deploys", "HTTPS + admin token")
    Rel(user, client, "Asks a question")
    Rel(client, rt, "Sends the request and the caller's roles", "HTTPS")
    Rel(rt, cp, "Reads the active deployment snapshot", "HTTPS + service token, conditional GET")
    Rel(rt, model, "Requests a tool selection and arguments")
    Rel(rt, api, "Executes one allowlisted, validated operation", "HTTPS, no redirects, bounded")

    UpdateRelStyle(rt, cp, $offsetY="-20")
    UpdateRelStyle(rt, api, $offsetY="10")
```

## Actors

| Actor | Trust | What they can do |
|---|---|---|
| Connector administrator | Trusted | Everything at configuration time. Holds the admin token. |
| End user | Untrusted | Supplies natural-language text. Their input reaches tool selection and argument generation, and is validated at every step. |
| AI application | Partially trusted | Asserts the caller's identity and roles. ToolLayer AI does not authenticate users; it enforces what the client asserts. |
| Model provider | **Untrusted** | Proposes a tool and arguments. Every proposal is re-checked against the snapshot and the published schema. |
| Upstream API | **Untrusted** | Returns content that is treated as data and never as instructions. |

The two entries marked untrusted are the ones people usually get wrong. A model provider is a
component the system *asks for advice*, not one it obeys, and an upstream API is a source of
attacker-influenceable text.

## Boundaries

```mermaid
flowchart TB
    subgraph Internet["Untrusted"]
        EU["End user input"]
        UP["Upstream API responses"]
    end

    subgraph Semi["Partially trusted"]
        CL["AI application<br/>(asserts identity and roles)"]
        MP["Model provider<br/>(proposes tool and arguments)"]
    end

    subgraph Trusted["Trusted, operator-controlled"]
        CPB["Control Plane"]
        RTB["Runtime"]
        DB[("Control-plane database")]
    end

    EU --> CL --> RTB
    RTB <--> MP
    RTB --> UP
    RTB -->|"service token, read-only"| CPB
    CPB <--> DB

    classDef untrusted fill:#fdeceb,stroke:#b3251e,color:#5c1310
    classDef semi fill:#fff6e5,stroke:#9a6300,color:#4a3000
    classDef trusted fill:#eaf6ef,stroke:#147a4a,color:#0b3d26
    class EU,UP untrusted
    class CL,MP semi
    class CPB,RTB,DB trusted
```

Each arrow crossing into the trusted zone has a named control:

| Crossing | Control |
|---|---|
| End-user text → runtime | Length bounds; the text only ever reaches the provider, never the request builder |
| Model proposal → execution | Tool name resolved against the snapshot; arguments validated against the published schema |
| Client role assertion → authorization | Roles are enforced as asserted; the runtime does not authenticate |
| Runtime → control plane | Service token, read-only endpoint, versioned path |
| Runtime → upstream API | Origin allowlist, method allowlist, post-resolution address check, no redirects, bounds |
| Upstream response → runtime | Size cap, defensive decoding, marked `untrusted`, never re-enters selection |

## What is out of scope

- User authentication and session management. The AI application does that.
- Credential storage and OAuth flows. Connectors carry no secrets; see `docs/feature-parity.md`.
- Rate limiting, quotas, and billing.
- Multi-tenancy. One organization scope.
- Model hosting. The provider is an interface, not a component this project runs.
