"""Tests for the shipped plugins.

The locomotion tests check arithmetic against hand-computed numbers rather
than against the implementation, because "it returns what it returns" is not a
test. If the wheel maths is wrong, a robot drives into furniture, and that is
worth a few explicit expected values.
"""

from __future__ import annotations

import asyncio

import pytest

from emet_sdk.types import Action, Twist

from emet_hal.differential import DifferentialDrive
from emet_hal.mock import MockActuator, MockSensor, MockWake
from emet_hal.pocketsphinx_wake import PocketSphinxWake
from emet_hal.tracked import TrackedDrive

# r = 0.05 m, W = 0.20 m, chosen so the sums come out in round numbers.
DRIVE_BLOCK = {
    "id": "base",
    "type": "drive",
    "kinematics": "differential",
    "driver": {"plugin": "emet_hal.mock", "params": {}},
    "geometry": {"wheel_radius_m": 0.05, "track_width_m": 0.20},
    "limits": {"max_linear_mps": 1.0, "max_angular_rps": 2.0},
}

HEAD_BLOCK = {
    "id": "head",
    "type": "joint_group",
    "role": "head",
    "driver": {"plugin": "emet_hal.mock", "params": {}},
    "joints": [
        {"id": "pan", "axis": "yaw", "range_deg": [-90, 90], "home_deg": 0},
        {"id": "tilt", "axis": "pitch", "range_deg": [-30, 45], "home_deg": 0},
    ],
}


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------ differential


def test_straight_ahead_spins_both_wheels_equally():
    drive = DifferentialDrive(DRIVE_BLOCK)
    run(drive.start())
    # 0.5 m/s / 0.05 m radius = 10 rad/s on each wheel.
    speeds = drive.solve(Twist(linear_mps=0.5))
    assert speeds.left == pytest.approx(10.0)
    assert speeds.right == pytest.approx(10.0)


def test_turning_in_place_spins_the_wheels_opposite():
    drive = DifferentialDrive(DRIVE_BLOCK)
    run(drive.start())
    # w * W/2 / r = 1.0 * 0.10 / 0.05 = 2 rad/s, opposed.
    speeds = drive.solve(Twist(angular_rps=1.0))
    assert speeds.left == pytest.approx(-2.0)
    assert speeds.right == pytest.approx(2.0)
    assert speeds.left == pytest.approx(-speeds.right)


def test_declared_limits_are_respected():
    drive = DifferentialDrive(DRIVE_BLOCK)
    run(drive.start())
    fast = drive.solve(Twist(linear_mps=99.0))
    capped = drive.solve(Twist(linear_mps=1.0))          # the declared maximum
    assert fast.left == pytest.approx(capped.left)


def test_missing_geometry_fails_loudly_rather_than_silently_doing_nothing():
    """Without a wheel radius every command computes to zero.

    That looks exactly like a dead motor, and sends a builder hunting through
    wiring when the problem is three lines of YAML.
    """
    from emet_sdk.plugin import PluginError

    block = {**DRIVE_BLOCK, "geometry": {}}
    with pytest.raises(PluginError, match="wheel_radius_m"):
        run(DifferentialDrive(block).start())


def test_stop_means_stop():
    drive = DifferentialDrive(DRIVE_BLOCK)
    run(drive.start())
    run(drive.command(Twist(linear_mps=0.5)))
    run(drive.stop())
    assert drive.last.left == 0.0
    assert drive.last.right == 0.0


# ----------------------------------------------------------------- tracked


def test_tracked_shares_the_straight_line_maths():
    """Treads and wheels differ on turns, not on going forward."""
    wheels = DifferentialDrive(DRIVE_BLOCK)
    treads = TrackedDrive({**DRIVE_BLOCK, "kinematics": "tracked"})
    run(wheels.start())
    run(treads.start())
    assert treads.solve(Twist(linear_mps=0.4)).left == pytest.approx(
        wheels.solve(Twist(linear_mps=0.4)).left
    )


def test_tracked_turns_less_than_commanded_because_treads_scrub():
    wheels = DifferentialDrive(DRIVE_BLOCK)
    treads = TrackedDrive({**DRIVE_BLOCK, "kinematics": "tracked"})
    run(wheels.start())
    run(treads.start())
    assert abs(treads.solve(Twist(angular_rps=1.0)).right) < abs(
        wheels.solve(Twist(angular_rps=1.0)).right
    )


def test_slip_is_tunable_per_body():
    block = {**DRIVE_BLOCK, "driver": {"plugin": "x", "params": {"slip_factor": 0.5}}}
    treads = TrackedDrive(block)
    run(treads.start())
    assert treads.slip_factor == 0.5
    # w * slip * W/2 / r = 1.0 * 0.5 * 0.10 / 0.05 = 1.0
    assert treads.solve(Twist(angular_rps=1.0)).right == pytest.approx(1.0)


def test_tracked_descriptor_does_not_promise_the_full_turn_rate():
    """The self-model must not claim a rate the treads cannot deliver."""
    treads = TrackedDrive({**DRIVE_BLOCK, "kinematics": "tracked"})
    run(treads.start())
    assert treads.describe().max_angular_rps < DRIVE_BLOCK["limits"]["max_angular_rps"]
    assert treads.describe().can_turn_in_place


# -------------------------------------------------------------------- mock


def test_mock_describes_itself_from_the_manifest():
    """So that chains bind against it exactly as against the real thing."""
    mock = MockActuator(HEAD_BLOCK)
    run(mock.start())
    d = mock.describe()
    assert d.role == "head"
    assert d.axes == frozenset({"yaw", "pitch"})
    assert d.joints == ("pan", "tilt")
    assert d.healthy


def test_mock_records_what_it_was_asked_to_do():
    mock = MockActuator(HEAD_BLOCK)
    run(mock.start())
    run(mock.apply(Action(capability_id="head", name="tilt", params={"angle_deg": 12})))
    assert len(mock.applied) == 1
    assert mock.applied[0].name == "tilt"


def test_a_failed_start_reports_unhealthy_rather_than_crashing():
    """A dead servo must not take down a robot that can still talk.

    This is the path that makes chains fall through, so it needs to be a
    narrowed descriptor rather than an exception.
    """
    block = {**HEAD_BLOCK, "driver": {"plugin": "emet_hal.mock", "params": {"fail_on_start": True}}}
    mock = MockActuator(block)
    run(mock.start())
    assert not mock.describe().healthy
    assert not mock.health().ok


def test_mock_sensor_polls_and_can_admit_staleness():
    block = {
        "id": "imu",
        "type": "sensor",
        "sensor_kind": "imu",
        "driver": {"plugin": "emet_hal.mock_sensor", "params": {"values": {"pitch_deg": 3.0}}},
    }
    sensor = MockSensor(block)
    reading = run(sensor.poll())
    assert reading.kind == "imu"
    assert reading.values["pitch_deg"] == 3.0
    assert not reading.stale

    stale_block = {**block, "driver": {"plugin": "x", "params": {"values": {}, "stale": True}}}
    assert run(MockSensor(stale_block).poll()).stale


def test_mock_wake_fires_on_the_phrase_it_was_given():
    wake = MockWake({"engine": "mock", "params": {}}, "hey emet")
    run(wake.start())
    assert run(wake.process(b"nothing here")) is None
    event = run(wake.process(b"...hey emet..."))
    assert event is not None and event.phrase == "hey emet"
    assert wake.frames == 2


def test_a_wake_engine_that_failed_to_start_hears_nothing():
    """The path with no fallback beneath it. An actuator that fails to start
    gets bound past; a wake engine that fails just leaves the robot deaf, so
    it has to be visible in the descriptor rather than only at runtime."""
    wake = MockWake({"engine": "mock", "params": {"fail_on_start": True}}, "hey emet")
    run(wake.start())
    assert not wake.describe().healthy
    assert not wake.describe().can_detect("hey emet")
    assert run(wake.process(b"...hey emet...")) is None
    assert not wake.health().ok


def test_a_wake_engine_can_start_and_still_not_know_the_name():
    """Distinct from a failed start, and the likelier failure in practice:
    a healthy engine carrying models for other words."""
    wake = MockWake({"engine": "mock", "params": {"phrases": ["hey jarvis"]}}, "hey emet")
    run(wake.start())
    descriptor = wake.describe()
    assert descriptor.healthy
    assert not descriptor.can_detect("hey emet")


# -------------------------------------------------------------- carry-over


def test_mock_wake_carries_over_whatever_its_params_say():
    """So an engine test can watch a value go round a boot: kept in the
    body's state file at the end of one run, back in `params` at the next."""
    wake = MockWake({"engine": "mock", "params": {"carry_over": {"cmninit": "1,2,3"}}}, "hey emet")
    asyncio.run(wake.start())
    carried = wake.carry_over()
    assert carried == {"cmninit": "1,2,3"}
    carried["cmninit"] = "edited"
    assert wake.carry_over() == {"cmninit": "1,2,3"}, "a copy, so a caller cannot edit the params"
    empty = MockWake({"engine": "mock", "params": {}}, "hey emet")
    asyncio.run(empty.start())
    assert empty.carry_over() == {}


def test_mock_wake_carries_nothing_once_shut_down():
    """As pocketsphinx, which has let its decoder go: an engine that asks
    after `shutdown()` has asked too late, and a test built on the mock
    has to see that."""
    wake = MockWake({"engine": "mock", "params": {"carry_over": {"cmninit": "1,2,3"}}}, "hey emet")
    assert wake.carry_over() == {}, "never started"
    asyncio.run(wake.start())
    asyncio.run(wake.shutdown())
    assert wake.carry_over() == {}


class FakeDecoder:
    """Only what `carry_over()` asks of a pocketsphinx decoder, so these
    tests run with the `wake` extra absent."""

    def __init__(self, mean: str = "41.2,4.1,-0.9", *, fails: bool = False) -> None:
        self.mean = mean
        self.fails = fails
        self.asked: list[bool] = []

    def get_cmn(self, update: bool) -> str:
        self.asked.append(update)
        if self.fails:
            raise RuntimeError("this build reports no cepstral mean")
        return self.mean


def listening(decoder: FakeDecoder) -> PocketSphinxWake:
    """The plugin as `start()` leaves it once the phrase is taught, with
    `decoder` in place of the real one."""
    plugin = PocketSphinxWake({"engine": "pocketsphinx", "params": {}}, "hey emet")
    plugin._decoder = decoder
    plugin._listening = True
    return plugin


def test_pocketsphinx_carries_over_the_mean_its_decoder_reports():
    """The hint and the carry-over read the same mean, and neither asks the
    decoder to update it: reading the mean must not change it."""
    decoder = FakeDecoder()
    plugin = listening(decoder)
    assert plugin.carry_over() == {"cmninit": "41.2,4.1,-0.9"}
    assert "41.2,4.1,-0.9" in plugin.warm_start_hint()
    assert decoder.asked == [False, False]


def test_pocketsphinx_carries_nothing_when_it_is_not_listening():
    plugin = listening(FakeDecoder())
    plugin._listening = False
    assert plugin.carry_over() == {}
    assert plugin.warm_start_hint() is None
    never_started = PocketSphinxWake({"engine": "pocketsphinx", "params": {}}, "hey emet")
    assert never_started.carry_over() == {}


def test_a_decoder_that_cannot_report_its_mean_costs_only_the_warm_start():
    """No exception reaches the engine at the end of the run. The next boot
    starts cold, which is all a missing mean costs."""
    plugin = listening(FakeDecoder(fails=True))
    assert plugin.carry_over() == {}
    assert plugin.warm_start_hint() is None


# ----------------------------------------------------------------- version


def test_the_reported_version_matches_the_installed_distribution():
    """One declaration, in `pyproject.toml`, read back at import time.

    This test exists because the two used to be written separately and drifted:
    the installed metadata said 0.1.0 while the source said 0.2.0, and
    nothing noticed because nothing compared them. A version that is quietly
    wrong is worse than no version, because it gets reported in bug reports as
    fact.
    """
    from importlib.metadata import version

    import emet_hal

    assert emet_hal.__version__ == version("emet-hal")
    assert emet_hal.__version__ != "0+unknown", "package is not installed"
