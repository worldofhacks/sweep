"""Test-only ASGI entry point; refuses startup without explicit test-mode settings."""

from adapters.sim.runtime import create_m14_sim_app

app = create_m14_sim_app()
