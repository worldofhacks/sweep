"""ASGI entry point for the production-path two-aircraft simulator gate."""

from adapters.sim.runtime import create_m14_sim_app

app = create_m14_sim_app()
