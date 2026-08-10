# Model Plane — the only model access architecture

Hermes has six model kinds: `chat`, `code`, `image`, `video`, `voice`,
`vector`. All six converge on **one** architecture — the model plane in
`hermes_cli/model_plane/` — with a single catalog, a single registration
store, a single activation mechanism, and two credential paths. This document
is the legislation for how models may be added or changed. Ad-hoc extension
outside these rules is rejected in review.

## Ownership rule

- **Chat models belong to providers.** A provider (`ProviderProfile` or a
  custom provider entry) owns chat model access: identity, endpoints, wire
  protocol, and the model catalog.
- **Code, image, video, voice, and vector models belong to capability plugins.**
  Code is a non-media capability with the dedicated `code_agent` selection;
  it does not use Chat switching or media relay. A capability plugin implements the narrow
  `hermes_cli.model_plane.capability.CapabilityProvider` protocol
  (`kind`, `name`, `env_vars`/setup schema, `models`, `is_available`,
  execution) and registers it with the capability registry. Voice models
  additionally carry a sub-capability tag (`tts` or `asr`).

Model access is decoupled from provider/plugin implementations: the model
plane consumes both ownership surfaces through the protocol and never imports
an implementation module.

## One pipeline for every kind

```
catalog  →  registration  →  activation  →  execution
```

1. **Catalog** — `hermes_cli.model_plane.catalog` returns the selectable
   `(kind, provider, model)` rows. Chat rows come from
   `hermes_cli/inventory.py`; Code and media rows come from the capability registry.
   Rows are credential-safe (availability booleans and setup metadata only).
2. **Registration** — `hermes_cli/model_registrations.py` stores user picks
   (and server-managed admin registrations) uniformly for all six kinds.
   Every kind is catalog-backed; `source="manual"` survives only as a legacy
   escape hatch for voice/vector records created before the catalog covered
   them.
3. **Activation** — Code activates into `config["code_agent"]`; media kinds
   activate into their `{kind}_gen` config section via
   `tools_config.select_media_model`; Chat activation stays the `model.default`
   selection. `use_gateway` exists only for generation media (image/video).
4. **Execution** — runtime consumers resolve the active selection and call
   the owning provider (Chat) or capability plugin (Code/media). Code applies
   the coding profile and `coding` toolset before its first request.

## Two credential paths — and only two

1. **User-supplied key** — the key lives in the per-user profile environment;
   the owner worker executes the capability plugin locally.
2. **Deployment-managed relay** — the worker asks the Control Plane over the
   socketpair relay (lease-fenced, framed protocol); the Control Plane holds
   the credentials, executes, and returns the result. Chat uses the HTTP
   adapter (`inference_relay.py`); media kinds use RPC-frame operations
   (the generalized media relay).

No third path. No broker, side channel, or kind-specific credential store.

## Extension rules (binding)

1. New model kinds, providers, or media capabilities register in
   `hermes_cli/model_plane/` — never through a parallel registry, broker,
   catalog, or activation path.
2. Per-kind differences live exclusively in `model_plane/kinds.py`. Do not
   special-case a kind in registrations, tools, or frontends.
3. Credentials reach runtime code only through the two paths above.
4. When a new implementation supersedes an old one, delete the old code,
   callers, compatibility branches, and test fixtures in the same change
   (repository cleanup policy).

## Migration status

- **PR1 (merged #185)** — model plane skeleton: kinds, capability protocol,
  unified catalog with adapters bridging the legacy media registries
  (`agent/image_gen_registry.py`, `agent/video_gen_registry.py`,
  `agent/tts_registry.py`, `agent/transcription_registry.py`) and profile
  embedding declarations; registrations generalized to all five kinds.
- **PR2 (merged #192)** — image/video migration done: every generation
  provider registers through
  `capability.register_media_generation_provider(kind, provider)`
  (`MediaGenerationAdapter` normalizes the catalog surface and delegates
  `generate()` to the plugin); `deployment_media.py` replaces
  `deployment_image.py` with declarative `(kind, provider, models, key_env,
  executor)` routes (no hardcoded provider; the existing APIYI deployment
  auto-activates as the default image route when `APIYI_API_KEY` is present);
  the image broker is generalized into
  `owner_worker/media_relay.py` (`image_generate`/`video_generate`
  operations routed by `(kind, provider, model)`). Tool dispatch checks the
  deployment route first, then falls back to the local plugin with the user
  key. Deleted: `deployment_image.py`, `owner_worker/image_relay.py`,
  `owner_worker/image_dispatch.py`, `agent/image_gen_registry.py`,
  `agent/video_gen_registry.py`, and their tests.
- **PR3 (this change)** — voice/vector completion. Registration converged
  into the capability registry (`register_voice_provider`,
  `resolve_embedding_capability`; the legacy tts/transcription registries
  and `agent/profile_embedding_client.py` are deleted). Consumers read the
  unified `voice_gen`/`vector_gen` selection: `tools/tts_tool.py`,
  `tools/transcription_tools.py`, voice mode, and the volcengine embedding
  skill script. The media relay carries five operations —
  `image_generate`, `video_generate`, `tts_synthesize`, `transcribe`,
  `embed` — and deployment routes now cover all four relay kinds
  (`RELAY_KINDS` in `model_plane/kinds.py`).

## Deployment media routes

`HERMES_DEPLOYMENT_MEDIA_ROUTES` declares one route per `(kind, provider)`:

- **image/video** routes declare an `executor` (`module:attribute`) plus
  optional `base_urls`/`executor_params`; the Control Plane runs the
  executor with the route's credential.
- **voice/vector** routes declare no executor — `executor`, `base_urls`,
  and `executor_params` are rejected. The Control Plane executes through
  the registered capability delegate for the route's provider
  (`get_voice_delegate(provider, "tts"|"asr")` /
  `resolve_embedding_capability(provider)`), so deployment execution stays
  decoupled from plugin wiring. A voice route's `models` is the union of
  its TTS and ASR model ids; the operation (`tts_synthesize` vs
  `transcribe`) plus the selected model choose the capability.

Relay operation contract (all inside the unchanged 96MB frame fence):

| Operation | Kind | Prompt | References | Params | Result |
| --- | --- | --- | --- | --- | --- |
| `image_generate` | image | required | image references per route limits | route executor params | `image` bytes + `mime_type` |
| `video_generate` | video | required | per route limits | route executor params | `video` bytes or `video_url` |
| `tts_synthesize` | voice | required (text) | none | `voice`, `speed`, `format` (`mp3`/`ogg`/`opus`) | `audio` bytes + `mime_type` |
| `transcribe` | voice | may be empty | exactly one audio sample | `language` | `text` transcript |
| `embed` | vector | required (text) | none | `dimensions`, `instructions` | `embedding` + `dimensions` |

Worker-side routing is selection-driven: `tools/tts_tool.py`,
`tools/transcription_tools.py`, and the embedding skill script use the
deployment route only when the active unified selection matches a declared
`(kind, provider, model)`; unmatched selections fall through to the local
capability plugin with the user's own key. The worker reaches the relay
through the process-level `worker_media_relay()` handle registered by the
owner-worker entrypoint.
