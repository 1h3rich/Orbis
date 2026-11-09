"""El fallo parcial de arranque drena todos los workers antes de soltar leases."""

import asyncio
import importlib

import pytest


def test_failed_worker_start_requests_stop_before_releasing_leases(monkeypatch, tmp_path):
    module = importlib.import_module("app.main")
    events = []

    class Worker:
        def __init__(self, name, fail=False):
            self.name = name
            self.fail = fail

        def start(self):
            events.append(("start", self.name))
            if self.fail:
                raise RuntimeError("startup failed")

        def request_stop(self):
            events.append(("request_stop", self.name))

        def stop(self):
            events.append(("stop", self.name))

    names = ("_paper_dca_worker", "_spot_strategy_worker", "_funding_worker",
             "_signal_risk_worker", "_signal_worker", "_observed_paper")
    for index, name in enumerate(names):
        monkeypatch.setattr(module, name, Worker(name, fail=index == 2))
    for name in ("_lab_path", "_automation_path", "_paper_perp_path", "_spot_strategy_path"):
        monkeypatch.setattr(module, name, tmp_path / (name + ".db"))

    async def startup():
        async with module.lifespan(module.app):
            pytest.fail("Startup must not succeed")

    with pytest.raises(RuntimeError, match="startup failed"):
        asyncio.run(startup())
    assert events[:3] == [("start", name) for name in names[:3]]
    assert [event[0] for event in events[3:]] == ["request_stop"] * 6 + ["stop"] * 6
