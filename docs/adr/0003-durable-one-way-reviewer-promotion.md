# Promote once behind a durable readiness marker

A Poppy Instance enters reviewer mode only after its GitHub setup state contains a validated App
installation and its ChatGPT setup state represents a verified managed account. Either connection
may arrive first. One in-process runtime module serializes the transition and owns the existing
Review Cycle and scheduler, while GitHub and Codex remain injected adapters at their external
seams.

Promotion constructs and validates the reviewer resources, then an outer ASGI gate stops admitting
new setup requests and lets every admitted response finish before atomically persisting a
non-secret readiness marker. The marker immediately and permanently makes every Setup Session
route unavailable. Poppy then durably removes the server-side bootstrap and session state before
publishing the active reviewer to the webhook handler. Webhooks therefore see either a `503`
Service Unavailable response or a complete reviewer runtime, never a partially initialized one.

The readiness marker is authoritative across restart and is consulted before creating a Setup
Session or logging a bootstrap link. A ready instance reloads the separately persisted GitHub and
Codex credentials, revalidates both integrations, and starts the reviewer during the ASGI
lifespan. Missing, corrupt, revoked, or invalid credentials fail closed without deleting the
marker or reopening setup. Before the marker is written, the live setup page retries a transient
runtime-validation failure. Deleting the private data volume remains the only fresh-start
mechanism.
