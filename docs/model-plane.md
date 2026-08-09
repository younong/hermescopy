# Model Plane — the only model access architecture

Hermes has exactly five model kinds: `chat`, `image`, `video`, `voice`,
`vector`. All five converge on **one** architecture — the model plane in
`hermes_cli/model_plane/` — with a single catalog, a single registration
store, a single activation mechanism, and two credential paths. This document
is the legislation for how models may be added or changed. Ad-hoc extension
outside these rules is rejected in review.

## Ownership rule

- **Chat models belong to providers.** A provider (`ProviderProfile` or a
  custom provider entry) owns chat model access: identity, endpoints, wire
  protocol, and the model catalog.
- **Image, video, voice, and vector models belong to capability plugins.**
  A capability plugin implements the narrow
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
   `hermes_cli/inventory.py`; media rows come from the capability registry.
   Rows are credential-safe (availability booleans and setup metadata only).
2. **Registration** — `hermes_cli/model_registrations.py` stores user picks
   (and server-managed admin registrations) uniformly for all five kinds.
   Every kind is catalog-backed; `source="manual"` survives only as a legacy
   escape hatch for voice/vector records created before the catalog covered
   them.
3. **Activation** — media kinds activate into their `{kind}_gen` config
   section via `tools_config.select_media_model`; chat activation stays the
   `model.default` selection. `use_gateway` exists only for generation media
   (image/video).
4. **Execution** — runtime consumers resolve the active selection and call
   the owning provider (chat) or capability plugin (media).

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

- **PR1 (this change)** — model plane skeleton: kinds, capability protocol,
  unified catalog with adapters bridging the legacy media registries
  (`agent/image_gen_registry.py`, `agent/video_gen_registry.py`,
  `agent/tts_registry.py`, `agent/transcription_registry.py`) and profile
  embedding declarations; registrations generalized to all five kinds.
- **PR2** — image/video migration: all generation providers become capability
  plugins, the image broker generalizes into the media relay, and the legacy
  registries plus `deployment_image.py` are deleted.
- **PR3** — voice/vector completion: TTS/ASR/embedding capability plugins,
  relay operations, and real consumers wired to the unified selection.

Until the migration PRs land, the adapters in
`hermes_cli/model_plane/capability.py` are the only sanctioned bridge to the
legacy registries. Do not add new code against the legacy registries directly.
