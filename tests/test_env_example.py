from pathlib import Path

ENV_EXAMPLE = Path(__file__).parents[1] / ".env.example"

ACTIVE_ENVIRONMENT_KEYS = {
    "ANTHROPIC_API_KEY",
    "LANGFUSE_HOST",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "OPENAI_API_KEY",
    "SWEEP_ADAPTER_BACKEND",
    "SWEEP_CAPABILITY_RELEASE",
    "SWEEP_ADAPTER_KEYS_JSON",
    "SWEEP_ALLOW_SHARED_ADAPTER_TOKEN",
    "SWEEP_AUDIT_STATE_INTERVAL_MS",
    "SWEEP_COMMAND_DEADLINE_MS",
    "SWEEP_COMMAND_TTL_MS",
    "SWEEP_CONSOLE_ORIGINS",
    "SWEEP_FUTURE_CLOCK_SKEW_MS",
    "SWEEP_INTENT_MAX_AGE_MS",
    "SWEEP_LOCALIZATION_KEYS_JSON",
    "SWEEP_MEDIA_API_PASSWORD",
    "SWEEP_MEDIA_API_URL",
    "SWEEP_MEDIA_API_USERNAME",
    "SWEEP_MEDIA_DRONE1_PASSWORD",
    "SWEEP_MEDIA_DRONE2_PASSWORD",
    "SWEEP_MEDIA_DRONE3_PASSWORD",
    "SWEEP_MEDIA_DRONE4_PASSWORD",
    "SWEEP_MEDIA_HOST",
    "SWEEP_MEDIA_READ_PASSWORD",
    "SWEEP_MEDIA_READ_USERNAME",
    "SWEEP_MEDIA_WEBRTC_ORIGIN",
    "SWEEP_NODE_WATCHDOG_FAILSAFE_MS",
    "SWEEP_NODE_WATCHDOG_HOLD_MS",
    "SWEEP_PLANNING_JSON",
    "SWEEP_QUALIFIED_VOICE_INTENTS",
    "SWEEP_RELAY_ORIGIN",
    "SWEEP_RELAY_TOKEN",
    "SWEEP_SAFETY_JSON",
    "SWEEP_SESSION_ID",
    "SWEEP_SESSION_LOG_DIR",
    "SWEEP_SIM_CAMERA_JSON",
    "SWEEP_STATE_MEMBERSHIP_HISTORY",
    "SWEEP_TELEMETRY_FRESHNESS_MS",
    "SWEEP_TRANSPORT_EVENT_MAX_AGE_MS",
    "SWEEP_VIRTUAL_STICK_HZ",
}


def test_env_example_lists_the_runtime_environment_contract_once() -> None:
    keys = [
        line.partition("=")[0]
        for line in ENV_EXAMPLE.read_text().splitlines()
        if line and not line.startswith("#")
    ]

    assert len(keys) == len(set(keys))
    assert set(keys) == ACTIVE_ENVIRONMENT_KEYS


def test_env_example_does_not_advertise_unimplemented_provider_keys() -> None:
    text = ENV_EXAMPLE.read_text()

    assert "DEEPGRAM_API_KEY" not in text
    assert "WORLD_API_KEY" not in text
