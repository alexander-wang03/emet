"""The body: every declared part started, described, and bound, once, at boot.

Runs on the mock HAL through real entry-point discovery, the way the robot
does: `emet_hal.mock` for every actuator, `tracked` for the treads. No
hardware is touched.
"""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import pytest

from emet_sdk.discovery import PluginRegistry
from emet_sdk.resolve import load_chains
from emet_sdk.types import Action, Health
from emet_sdk.validate import MissingPluginError, load_yaml

from emet_engine.body import Body, format_bindings
from emet_engine.state import BodyState

EXAMPLES = Path(__file__).resolve().parents[2] / "emet-sdk" / "examples"


def run(coro):
    return asyncio.run(coro)


def example(name: str) -> dict:
    return load_yaml(EXAMPLES / name)


def started(manifest: dict, state: BodyState | None = None) -> Body:
    body = Body(manifest, PluginRegistry.discover(), chains=load_chains(), state=state)
    run(body.start())
    return body


def mock_part(cap_id: str, cap_type: str, role: str | None = None, **params) -> dict:
    block: dict = {"id": cap_id, "type": cap_type, "driver": {"plugin": "emet_hal.mock", "params": params}}
    if role:
        block["role"] = role
    return block


def head(**params) -> dict:
    block = mock_part("head", "joint_group", "head", **params)
    block["joints"] = [
        {"id": "pan", "axis": "yaw", "range_deg": [-90, 90], "home_deg": 0, "trim_deg": 0.5},
        {"id": "tilt", "axis": "pitch", "range_deg": [-30, 45], "home_deg": 0},
    ]
    return block


def manifest_with(*capabilities: dict) -> dict:
    return {
        "body": {"id": "test_body", "scale": "desk", "power": "plugged_in"},
        "audio": {"input": {"device": "x"}, "output": {"device": "y"}},
        "capabilities": list(capabilities),
    }


# ------------------------------------------------------------------- bodiless


def test_a_bodiless_body_binds_every_intent_to_the_voice():
    body = started(example("bodiless.yaml"))
    assert body.capabilities == () and body.locomotion is None
    assert len(body.table) == 31
    assert len(body.table.voice_bound) == 31 and not body.table.hardware_bound
    assert body.faults == {}


def test_the_boot_log_groups_the_table_by_what_performs_it():
    """Thirty-one bindings as six groups, every intent exactly once, and no
    line wider than the header's indent leaves room for on 80 columns."""
    lines = format_bindings(started(example("bodiless.yaml")).table)
    groups = [line for line in lines if not line.startswith(" ")]
    assert [g.split()[1] for g in groups] == ["utter", "inflect", "tone", "explain", "backchannel", "silence"]
    assert all(len(line) <= 64 for line in lines)
    assert lines[0].startswith("voice  utter") and "speak" in lines[0]
    named = " ".join(lines).split()
    for intent in load_chains():
        assert named.count(intent) == 1, intent


# ------------------------------------------------------------------ the scout


def test_the_mock_scout_starts_every_part_and_its_treads_describe_themselves():
    body = started(example("mock-scout.yaml"))
    ids = [c.capability_id for c in body.capabilities]
    assert ids == ["head", "base", "eyes", "ring", "imu"]
    assert all(c.healthy for c in body.capabilities)
    assert body.locomotion is not None and body.locomotion.kinematics == "tracked"
    assert body.locomotion.max_angular_rps == pytest.approx(2.0 * 0.85)
    assert body.table["express.curiosity"].capability_id == "head"
    assert body.table["move.approach"].capability_id == "base"
    assert body.table["signal.listening"].capability_id == "ring"
    run(body.shutdown())


def test_hardware_rungs_come_first_in_the_boot_log():
    lines = format_bindings(started(example("mock-scout.yaml")).table)
    first_voice = next(i for i, line in enumerate(lines) if line.startswith("voice"))
    assert all(not line.startswith("voice") for line in lines[:first_voice])
    assert any(line.startswith("base") for line in lines[:first_voice])


def test_an_uninstalled_driver_stops_the_boot_by_name():
    """scout-01 names chips nobody has written drivers for. A typo and a
    missing package look the same to discovery, and neither may be mistaken
    for missing hardware, so boot stops rather than degrading."""
    body = Body(example("scout-01.yaml"), PluginRegistry.discover(), chains=load_chains())
    with pytest.raises(MissingPluginError) as exc:
        run(body.start())
    assert "emet_hal.pca9685" in str(exc.value)


# --------------------------------------------------------------- dead parts


def test_a_part_that_will_not_start_is_bound_past():
    manifest = manifest_with(head(fail_on_start=True, fault_detail="the servo board did not answer"),
                             mock_part("eyes", "display", "eyes"))
    body = started(manifest)
    by_id = {c.capability_id: c for c in body.capabilities}
    assert not by_id["head"].healthy and by_id["eyes"].healthy
    assert body.faults == {"head": "the servo board did not answer"}
    assert body.table["express.curiosity"].capability_id == "eyes", "curiosity fell through to the eyes"
    assert body.health["eyes"].ok


def test_treads_whose_arithmetic_cannot_start_leave_the_drive_unhealthy():
    """No geometry: the tracked plugin raises PluginError. A motor driver with
    no arithmetic above it moves nothing, so the whole drive is dead, and
    `move.approach` falls through to the voice rung that explains."""
    drive = mock_part("base", "drive")
    drive["kinematics"] = "tracked"
    body = started(manifest_with(drive))
    (base,) = body.capabilities
    assert not base.healthy
    assert body.locomotion is None
    assert "geometry" in body.faults["base"]
    assert body.table["move.approach"].is_voice and body.table["move.approach"].action == "explain"


def test_a_plugin_that_describes_itself_healthy_and_reports_unhealthy_is_believed_unhealthy(monkeypatch):
    from emet_hal import mock

    monkeypatch.setattr(mock.MockActuator, "health", lambda self: Health(ok=False, detail="overheating"))
    body = started(manifest_with(head()))
    assert not body.capabilities[0].healthy
    assert body.faults == {"head": "overheating"}


# ---------------------------------------------------------- body-local state


def test_a_trim_in_the_state_file_wins_over_the_manifests_first_guess(tmp_path):
    state = BodyState.load("test_body", tmp_path / "s.json")
    state.set_trim("head", "pan", 2.5)
    manifest = manifest_with(head())
    before = copy.deepcopy(manifest)
    body = started(manifest, state)
    joints = {j["id"]: j for j in body.plugin("head").capability["joints"]}
    assert joints["pan"]["trim_deg"] == 2.5
    assert joints["tilt"].get("trim_deg") is None, "no trim on file, none invented"
    assert manifest == before, "the manifest itself is never rewritten"


def test_what_a_part_carried_over_reaches_its_params_and_is_asked_for_again(tmp_path, monkeypatch):
    from emet_hal import mock

    state = BodyState.load("test_body", tmp_path / "s.json")
    state.carry("capability.head", {"learned": 7})
    monkeypatch.setattr(mock.MockActuator, "carry_over", lambda self: {"learned": 8})
    body = started(manifest_with(head(fixed=1)), state)
    assert dict(body.plugin("head").params) == {"fixed": 1, "learned": 7}
    assert body.carry_over() == {"capability.head": {"learned": 8}}


def test_reserved_types_are_listed_and_nothing_is_built():
    body = started(manifest_with({"id": "claw", "type": "manipulator"}))
    assert body.reserved == ["claw"] and body.capabilities == ()


# --------------------------------------------------------------- lifecycle


def test_apply_reaches_the_part_the_binding_names():
    body = started(example("mock-scout.yaml"))
    binding = body.table["express.curiosity"]
    run(body.apply(binding, Action(capability_id="head", name=binding.action, params=dict(binding.params))))
    (applied,) = body.plugin("head").applied
    assert applied.name == "tilt" and applied.params["angle_deg"] == 12


def test_shutdown_stops_every_part_and_survives_one_that_raises(monkeypatch):
    from emet_hal import mock

    body = started(example("mock-scout.yaml"))
    calls: list[str] = []

    async def broken(self):
        calls.append(self.capability_id)
        raise RuntimeError("stuck relay")

    monkeypatch.setattr(mock.MockActuator, "shutdown", broken)
    run(body.shutdown())
    assert set(calls) == {"head", "base", "eyes", "ring"}
    run(body.shutdown())  # twice is safe


def test_a_cancel_during_shutdown_still_stops_the_parts_after_it(monkeypatch):
    """A second Ctrl-C while one part stops. The rest are still told to stop,
    and the cancellation is raised once they have been."""
    from emet_hal import mock

    body = started(example("mock-scout.yaml"))
    stopped: list[str] = []

    async def shutdown(self):
        stopped.append(self.capability_id)
        if self.capability_id == "ring":
            raise asyncio.CancelledError()

    monkeypatch.setattr(mock.MockActuator, "shutdown", shutdown)
    with pytest.raises(asyncio.CancelledError):
        run(body.shutdown())
    assert set(stopped) == {"head", "base", "eyes", "ring"}


def test_a_driver_that_raises_something_else_is_bound_past_and_still_put_to_rest(monkeypatch):
    """The contract asks for `PluginError`; a bus that does not answer raises
    `OSError` on a real board. Either way the robot keeps its voice, and the
    half-started part is shut down."""
    from emet_hal import mock

    stopped: list[str] = []
    real_start, real_shutdown = mock.MockActuator.start, mock.MockActuator.shutdown

    async def start(self):
        if self.capability_id == "head":
            raise OSError(121, "Remote I/O error")
        await real_start(self)

    async def shutdown(self):
        stopped.append(self.capability_id)
        await real_shutdown(self)

    monkeypatch.setattr(mock.MockActuator, "start", start)
    monkeypatch.setattr(mock.MockActuator, "shutdown", shutdown)
    body = started(example("mock-scout.yaml"))
    assert "OSError" in body.faults["head"] and "Remote I/O error" in body.faults["head"]
    assert body.table["express.curiosity"].capability_id == "eyes"
    run(body.shutdown())
    assert "head" in stopped


def test_a_boot_cancelled_while_a_part_starts_still_puts_it_to_rest(monkeypatch):
    from emet_hal import mock

    stopped: list[str] = []
    real_shutdown = mock.MockActuator.shutdown

    async def slow_start(self):
        await asyncio.sleep(5)

    async def shutdown(self):
        stopped.append(self.capability_id)
        await real_shutdown(self)

    monkeypatch.setattr(mock.MockActuator, "start", slow_start)
    monkeypatch.setattr(mock.MockActuator, "shutdown", shutdown)
    body = Body(example("mock-scout.yaml"), PluginRegistry.discover(), chains=load_chains())

    async def scenario():
        task = asyncio.ensure_future(body.start())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await body.shutdown()

    run(scenario())
    assert stopped == ["head"]


def test_a_drives_locomotion_plugin_carries_over_under_its_own_key(tmp_path, monkeypatch):
    """The motor driver and the arithmetic above it are built from the same
    block; what each learned is kept apart, so neither is handed the other's."""
    from emet_hal import tracked

    state = BodyState.load("mock_scout", tmp_path / "s.json")
    state.carry("locomotion.base", {"slip_factor": 0.9})
    monkeypatch.setattr(tracked.TrackedDrive, "carry_over", lambda self: {"slip_factor": 0.8})
    body = started(example("mock-scout.yaml"), state)
    assert body._locomotion_plugins["base"].slip_factor == 0.9
    assert body.carry_over()["locomotion.base"] == {"slip_factor": 0.8}
    assert "slip_factor" not in dict(body.plugin("base").params)


def test_the_locomotion_plugin_is_not_handed_what_the_motor_driver_carried(tmp_path):
    """The other direction: a name the two plugins share stays each one's."""
    from emet_hal import tracked

    state = BodyState.load("mock_scout", tmp_path / "s.json")
    state.carry("capability.base", {"slip_factor": 0.1, "motor_only": "x"})
    body = started(example("mock-scout.yaml"), state)
    assert dict(body.plugin("base").params)["slip_factor"] == 0.1
    assert body._locomotion_plugins["base"].slip_factor == tracked.TrackedDrive.DEFAULT_SLIP
    assert "motor_only" not in dict(body._locomotion_plugins["base"].params)


def test_a_body_built_not_to_carry_gets_its_trims_and_nothing_its_plugins_carried(tmp_path):
    """A replay: the recording is another room, so what the plugins learned
    live stays on file and out of the run. A trim is the body's own and
    still applies."""
    state = BodyState.load("test_body", tmp_path / "s.json")
    state.set_trim("head", "pan", 2.5)
    state.carry("capability.head", {"learned": 7})
    body = Body(manifest_with(head(fixed=1)), PluginRegistry.discover(), chains=load_chains(), state=state, carry=False)
    run(body.start())
    assert dict(body.plugin("head").params) == {"fixed": 1}
    joints = {j["id"]: j for j in body.plugin("head").capability["joints"]}
    assert joints["pan"]["trim_deg"] == 2.5
