"""Tests for Chat GUI model-switch persistence."""

from types import SimpleNamespace


def test_code_runtime_overrides_are_forced_on_resume():
    from tui_gateway import server

    overrides = server._stored_session_runtime_overrides({
        "model": "gpt-5.3-codex",
        "billing_provider": "openrouter",
        "model_config": {
            "model_kind": "code",
            "runtime_profile": "chat",
            "runtime_toolset": "web",
            "provider": "openai-codex",
        },
    })

    assert overrides["model_kind"] == "code"
    assert overrides["runtime_profile"] == "coding"
    assert overrides["runtime_toolset"] == "coding"
    assert overrides["model_override"] == {
        "model": "gpt-5.3-codex",
        "provider": "openai-codex",
        "base_url": None,
        "api_mode": None,
    }


def test_runtime_model_config_keeps_code_identity_and_drops_it_for_chat():
    from tui_gateway import server

    code_agent = SimpleNamespace(
        model="gpt-5.3-codex",
        provider="openai-codex",
        base_url="https://api.example/v1",
        api_mode="codex_responses",
        reasoning_config=None,
        service_tier=None,
        model_kind="code",
        runtime_profile="coding",
        runtime_toolset="coding",
    )
    assert server._runtime_model_config(code_agent) == {
        "model": "gpt-5.3-codex",
        "provider": "openai-codex",
        "base_url": "https://api.example/v1",
        "api_mode": "codex_responses",
        "model_kind": "code",
        "runtime_profile": "coding",
        "runtime_toolset": "coding",
    }

    chat_agent = SimpleNamespace(
        model="claude-test",
        provider="anthropic",
        base_url="",
        api_mode="anthropic_messages",
        reasoning_config=None,
        service_tier=None,
        model_kind="chat",
        runtime_profile=None,
        runtime_toolset=None,
    )
    assert server._runtime_model_config(
        chat_agent,
        {
            "model_kind": "code",
            "runtime_profile": "coding",
            "runtime_toolset": "coding",
        },
    ) == {
        "model": "claude-test",
        "provider": "anthropic",
        "api_mode": "anthropic_messages",
    }


def test_deferred_code_session_record_preserves_runtime_identity(monkeypatch):
    from tui_gateway import server

    monkeypatch.setattr(server, "_required_gateway_transport", lambda: object())
    record = server._deferred_session_record(
        "session-code",
        cols=80,
        cwd="/tmp",
        history=[],
        lease=None,
        resume_runtime_overrides={
            "model_kind": "code",
            "runtime_profile": "coding",
            "runtime_toolset": "coding",
        },
    )

    assert record["model_kind"] == "code"
    assert record["runtime_profile"] == "coding"
    assert record["runtime_toolset"] == "coding"


def test_session_info_reports_selectable_reasoning_levels(monkeypatch):
    from tui_gateway import server

    monkeypatch.setattr(server, "_display_session_cwd", lambda _session: "")
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_get_usage", lambda _agent: {})
    monkeypatch.setattr(server, "_current_profile_name", lambda: "")
    monkeypatch.setattr(server, "_git_branch_for_cwd", lambda _cwd: "")
    monkeypatch.setattr(server, "_session_live_title", lambda *_args: "")
    monkeypatch.setattr(
        "agent.models_dev.get_selectable_reasoning_levels",
        lambda provider, model: ("high", "xhigh", "max")
        if (provider, model) == ("anthropic", "claude-test")
        else (),
    )

    info = server._session_info(SimpleNamespace(
        model="claude-test",
        provider="anthropic",
        reasoning_config={"enabled": True, "effort": "max"},
        service_tier=None,
    ), {})

    assert info["reasoning_effort"] == "max"
    assert info["supported_reasoning_levels"] == ["high", "xhigh", "max"]


def test_config_set_reasoning_max_updates_live_session(monkeypatch):
    from tui_gateway import server

    agent = SimpleNamespace(reasoning_config=None)
    session = {"agent": agent}
    emitted = []
    runtime = server.OwnerWorkerGatewayRuntime("owner", 1, "worker", 1, 0)
    runtime.mutable_state.sessions["runtime-a"] = session
    monkeypatch.setattr(server, "_persist_live_session_runtime", lambda _session: None)
    monkeypatch.setattr(server, "_session_info", lambda live_agent, _session: {
        "reasoning_effort": live_agent.reasoning_config["effort"],
    })
    monkeypatch.setattr(server, "_emit", lambda *args: emitted.append(args))

    with server.owner_worker_gateway_runtime(runtime):
        response = server._methods["config.set"]("request", {
            "key": "reasoning",
            "session_id": "runtime-a",
            "value": "max",
        })

    assert response["result"] == {"key": "reasoning", "value": "max"}
    assert session["create_reasoning_override"] == {
        "enabled": True,
        "effort": "max",
    }
    assert agent.reasoning_config == {"enabled": True, "effort": "max"}
    assert emitted == [("session.info", "runtime-a", {"reasoning_effort": "max"})]


def test_registered_config_switch_uses_runtime_only_fast_path(monkeypatch):
    from hermes_cli import model_cost_guard, model_registrations, model_switch
    from tui_gateway import server

    class Agent:
        model = "old-model"
        provider = "old-provider"
        base_url = "https://old.example/v1"
        api_key = "old-key"
        context_compressor = SimpleNamespace(context_length=65_536)
        _cached_system_prompt = "existing prompt"

        def switch_model(self, **kwargs):
            self.switch_kwargs = kwargs
            self.model = kwargs["new_model"]
            self.provider = kwargs["new_provider"]

    agent = Agent()
    session = {
        "agent": agent,
        "history": [{"role": "user", "content": "hello"}],
        "history_version": 1,
        "session_key": "stored-a",
    }
    runtime = server.OwnerWorkerGatewayRuntime("owner", 1, "worker", 1, 0)
    runtime.mutable_state.sessions["runtime-a"] = session
    emitted = []
    switch_calls = []
    cost_calls = []

    monkeypatch.setattr(
        model_registrations,
        "resolve_chat_model_registration",
        lambda registration_id: {
            "registration_id": registration_id,
            "provider": "new-provider",
            "model": "new-model",
            "source": "catalog",
        },
    )
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    scheduled = []
    monkeypatch.setattr(
        server,
        "_schedule_live_session_runtime_persist",
        lambda live_session, route: scheduled.append((live_session, route)),
    )
    monkeypatch.setattr(server, "_emit", lambda *args: emitted.append(args))
    monkeypatch.setattr(
        server,
        "_session_info",
        lambda *_args: (_ for _ in ()).throw(AssertionError("full session info called")),
    )

    def fake_switch_model(**kwargs):
        switch_calls.append(kwargs)
        return SimpleNamespace(
            success=True,
            new_model="new-model",
            target_provider="new-provider",
            api_key="new-key",
            base_url="https://new.example/v1",
            api_mode="chat_completions",
            relay_provider="",
            deployment_managed=False,
            model_info=None,
            warning_message="",
        )

    monkeypatch.setattr(model_switch, "switch_model", fake_switch_model)

    def fake_expensive_model_warning(*args, **kwargs):
        cost_calls.append((args, kwargs))
        return None

    monkeypatch.setattr(
        model_cost_guard,
        "expensive_model_warning",
        fake_expensive_model_warning,
    )

    with server.owner_worker_gateway_runtime(runtime):
        response = server._methods["config.set"]("request", {
            "key": "model",
            "session_id": "runtime-a",
            "registration_id": "registration-a",
            "value": "new-model --provider new-provider --session",
        })

    assert response["result"]["value"] == "new-model"
    assert switch_calls[0]["trusted_selection"] is True
    assert cost_calls[0][1]["allow_network"] is False
    assert agent.switch_kwargs["route_only"] is True
    assert agent._cached_system_prompt == "existing prompt"
    assert session["history"] == [{"role": "user", "content": "hello"}]
    assert session["history_version"] == 1
    assert session["model_override"]["model"] == "new-model"
    assert scheduled == [(session, {
        "model": "new-model",
        "provider": "new-provider",
        "base_url": "https://new.example/v1",
        "api_mode": "chat_completions",
    })]
    assert emitted == [
        ("session.info", "runtime-a", {"model": "new-model", "provider": "new-provider"})
    ]


def test_registered_route_preflight_is_read_only(monkeypatch):
    from hermes_cli import model_cost_guard, model_registrations, model_switch
    from tui_gateway import server

    agent = SimpleNamespace(model="old-model", provider="old-provider")
    session = {
        "agent": agent,
        "history": [{"role": "user", "content": "hello"}],
        "history_version": 1,
    }
    runtime = server.OwnerWorkerGatewayRuntime("owner", 1, "worker", 1, 0)
    runtime.mutable_state.sessions["runtime-a"] = session
    switch_calls = []
    emitted = []

    monkeypatch.setattr(
        model_registrations,
        "resolve_chat_model_registration",
        lambda registration_id: {
            "registration_id": registration_id,
            "provider": "new-provider",
            "model": "new-model",
        },
    )
    monkeypatch.setattr(
        model_switch,
        "switch_model",
        lambda **kwargs: (
            switch_calls.append(kwargs)
            or SimpleNamespace(
                success=True,
                new_model="new-model",
                target_provider="new-provider",
                api_key="new-key",
                base_url="https://new.example/v1",
                api_mode="chat_completions",
                relay_provider="",
                deployment_managed=False,
                model_info=None,
                warning_message="",
            )
        ),
    )
    monkeypatch.setattr(model_cost_guard, "expensive_model_warning", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_emit", lambda *args: emitted.append(args))
    monkeypatch.setattr(
        server,
        "_schedule_live_session_runtime_persist",
        lambda *_args: (_ for _ in ()).throw(AssertionError("preflight persisted route")),
    )

    with server.owner_worker_gateway_runtime(runtime):
        response = server._methods["prompt.model_preflight"]("request", {
            "session_id": "runtime-a",
            "registration_id": "registration-a",
            "model": "new-model",
            "provider": "new-provider",
        })

    assert response["result"] == {
        "value": "new-model",
        "warning": "",
        "confirm_required": False,
        "confirm_message": "",
    }
    assert switch_calls[0]["trusted_selection"] is True
    assert agent.model == "old-model"
    assert agent.provider == "old-provider"
    assert session["history"] == [{"role": "user", "content": "hello"}]
    assert session["history_version"] == 1
    assert "model_override" not in session
    assert emitted == []


def test_submitted_route_rejects_mismatch_and_employee_pin(monkeypatch):
    from hermes_cli import model_registrations
    from tui_gateway import server

    monkeypatch.setattr(
        model_registrations,
        "resolve_chat_model_registration",
        lambda registration_id: {
            "registration_id": registration_id,
            "provider": "registered-provider",
            "model": "registered-model",
        },
    )
    runtime = server.OwnerWorkerGatewayRuntime("owner", 1, "worker", 1, 0)
    runtime.mutable_state.sessions["runtime-a"] = {
        "agent": SimpleNamespace(),
        "employee_policy": {},
    }

    with server.owner_worker_gateway_runtime(runtime):
        mismatch = server._methods["prompt.submit"]("request", {
            "session_id": "runtime-a",
            "text": "hello",
            "registration_id": "registration-a",
            "model": "different-model",
            "provider": "registered-provider",
        })
        pinned = server._methods["prompt.submit"]("request", {
            "session_id": "runtime-a",
            "text": "hello",
            "registration_id": "registration-a",
            "model": "registered-model",
            "provider": "registered-provider",
        })

    assert mismatch["error"]["code"] == 4002
    assert mismatch["error"]["message"] == "model registration does not match requested route"
    assert pinned["error"]["code"] == 4032
    assert runtime.mutable_state.sessions["runtime-a"].get("running") is not True


def test_busy_prompt_merge_keeps_latest_submitted_route(monkeypatch):
    from tui_gateway import server

    session = {
        "agent": SimpleNamespace(interrupt=lambda: None, steer=lambda _text: True),
        "_active_turn_generation": 1,
    }
    first_route = {
        "registration_id": "registration-b",
        "model": "model-b",
        "provider": "provider-b",
    }
    latest_route = {
        "registration_id": "registration-c",
        "model": "model-c",
        "provider": "provider-c",
    }
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "steer")
    monkeypatch.setattr(server, "_clear_pending", lambda *_args, **_kwargs: None)

    first = server._handle_busy_submit(
        "request", "runtime-a", session, "message b", object(), first_route
    )
    second = server._handle_busy_submit(
        "request", "runtime-a", session, "message c", object(), latest_route
    )

    assert first["result"] == {"status": "queued"}
    assert second["result"] == {"status": "queued"}
    assert session["queued_prompt"]["text"] == "message b\n\nmessage c"
    assert session["queued_prompt"]["submitted_route"] == latest_route


def test_queued_route_is_revalidated_before_application(monkeypatch):
    from hermes_cli import model_registrations
    from tui_gateway import server

    session = {"agent": SimpleNamespace()}
    route = {
        "registration_id": "registration-a",
        "model": "captured-model",
        "provider": "captured-provider",
    }
    monkeypatch.setattr(
        model_registrations,
        "resolve_chat_model_registration",
        lambda _registration_id: {
            "model": "edited-model",
            "provider": "captured-provider",
        },
    )

    try:
        server._apply_submitted_chat_route("runtime-a", session, route)
    except ValueError as exc:
        assert str(exc) == "model registration does not match requested route"
    else:
        raise AssertionError("edited queued registration was accepted")


def test_stale_route_persistence_is_discarded_before_db_access():
    from tui_gateway import server

    db = SimpleNamespace(get_session=lambda _sid: (_ for _ in ()).throw(
        AssertionError("stale route touched the database")
    ))
    agent = SimpleNamespace(_session_db=db)
    session = {
        "agent": agent,
        "route_generation": 2,
        "session_key": "stored-a",
    }

    server._persist_live_session_runtime(
        session,
        route={"model": "old-model", "provider": "old-provider"},
        generation=1,
    )


def test_registered_config_switch_rejects_mismatched_route(monkeypatch):
    from hermes_cli import model_registrations
    from tui_gateway import server

    session = {"agent": SimpleNamespace()}
    runtime = server.OwnerWorkerGatewayRuntime("owner", 1, "worker", 1, 0)
    runtime.mutable_state.sessions["runtime-a"] = session
    monkeypatch.setattr(
        model_registrations,
        "resolve_chat_model_registration",
        lambda registration_id: {
            "registration_id": registration_id,
            "provider": "registered-provider",
            "model": "registered-model",
        },
    )

    with server.owner_worker_gateway_runtime(runtime):
        response = server._methods["config.set"]("request", {
            "key": "model",
            "session_id": "runtime-a",
            "registration_id": "registration-a",
            "value": "different-model --provider registered-provider --session",
        })

    assert response["error"]["code"] == 4002
    assert response["error"]["message"] == "model registration does not match requested route"


def test_persist_model_switch_uses_config_set_value_for_all_model_keys(monkeypatch):
    from hermes_cli import config
    from tui_gateway import server

    calls = []
    monkeypatch.setattr(
        config,
        "set_config_value",
        lambda key, value: calls.append((key, value)),
    )

    server._persist_model_switch(
        SimpleNamespace(
            new_model="claude-sonnet-4-6",
            target_provider="anthropic",
            base_url="",
            deployment_managed=False,
        )
    )

    assert calls == [
        ("model.default", "claude-sonnet-4-6"),
        ("model.provider", "anthropic"),
        ("model.base_url", ""),
    ]
