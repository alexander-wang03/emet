"""The closed set of kinds a health report may name.

A plugin says why it is not healthy in `Health.faults`, and the engine acts
on the kind. A language model the vendor no longer serves still boots and
says one line at each wake, an account out of credit gets a line that asks
nothing of the person in the room, and any other fault stops the boot. So
the kinds are a vocabulary, closed as `STOP_REASONS` is, with one prefix
left open for a plugin written outside Emet. Before this set existed six
plugins chose a kind by matching the words of their own refusal.
"""

from __future__ import annotations

import pytest

import emet_sdk
from emet_sdk.types import FAULT_KINDS, FAULT_PREFIX, Health

#: The kinds the code set before the set was closed, each found by a search
#: of the four packages for `faults=`.
BEFORE = {"no_key", "start_failed", "no_model", "no_library", "unpronounceable_phrase", "locomotion", "health", "unhealthy"}


def test_the_set_is_the_eight_in_use_and_the_two_for_a_model_and_a_bill():
    assert FAULT_KINDS == BEFORE | {"model_unavailable", "out_of_credit"}
    assert FAULT_PREFIX == "x_"


def test_a_plugin_author_finds_the_set_at_the_top_of_the_sdk():
    assert emet_sdk.FAULT_KINDS is FAULT_KINDS
    assert emet_sdk.FAULT_PREFIX == FAULT_PREFIX


@pytest.mark.parametrize("kind", sorted(FAULT_KINDS))
def test_every_kind_in_the_set_is_taken(kind):
    assert Health(ok=False, detail="why", faults=(kind,)).faults == (kind,)


def test_a_report_may_name_several_kinds():
    assert Health(ok=False, faults=("no_key", "x_cold")).faults == ("no_key", "x_cold")


def test_a_healthy_report_names_no_kind_and_may_carry_a_notice():
    """A healthy report's detail is a notice printed once at boot, such as
    the date a vendor has set for retiring the model."""
    notice = Health(detail="the model 'claude-opus-5' retires on 2027-07-24 at Anthropic.")
    assert notice.ok and notice.faults == ()


@pytest.mark.parametrize("kind", ["x_overcurrent", "x_timeout", "x_2", "x_motor_stall", "x_a_"])
def test_a_plugins_own_kind_carries_the_prefix(kind):
    assert Health(ok=False, faults=(kind,)).faults == (kind,)


@pytest.mark.parametrize(
    "kind",
    ["overcurrent", "No_Key", "no key", "x_", "x__a", "x_Overcurrent", "x_motor-stall", "", "model unavailable", "X_cold"],
)
def test_a_kind_outside_the_set_is_refused_and_the_set_is_named(kind):
    with pytest.raises(ValueError) as caught:
        Health(ok=False, faults=(kind,))
    message = str(caught.value)
    assert message.startswith(f"unknown fault kind {kind!r}.")
    assert all(name in message for name in FAULT_KINDS)
    assert "written x_, then a lower-case letter or a digit, then lower-case letters" in message


def test_the_refusal_says_what_to_write_instead():
    with pytest.raises(ValueError) as caught:
        Health(ok=False, faults=("overcurrent",))
    assert str(caught.value) == (
        "unknown fault kind 'overcurrent'. Health.faults holds kinds from emet_sdk.types.FAULT_KINDS "
        "(health, locomotion, model_unavailable, no_key, no_library, no_model, out_of_credit, start_failed, "
        "unhealthy, unpronounceable_phrase), or a plugin's own kind, written x_, then a lower-case letter or a "
        "digit, then lower-case letters, digits or _, such as 'x_overcurrent'."
    )


@pytest.mark.parametrize(
    ("kind", "suggested"),
    [
        ("motor-stall", "'x_motor_stall'"),
        ("x_Motor", "'x_motor'"),
        ("", "'x_name'"),
        ("Over Current!", "'x_over_current'"),
        ("x_", "'x_name'"),
        ("x__", "'x_name'"),
        ("_x_cold", "'x_cold'"),
        ("model-unavailable", "'model_unavailable'"),
        ("No_Key", "'no_key'"),
        ("NO_MODEL", "'no_model'"),
        ("Out Of Credit", "'out_of_credit'"),
        ("x_model-unavailable", "'x_model_unavailable'"),
    ],
)
def test_the_suggested_kind_is_one_the_set_would_take(kind, suggested):
    with pytest.raises(ValueError, match="such as " + suggested):
        Health(ok=False, faults=(kind,))
    Health(ok=False, faults=(suggested.strip("'"),))


def test_one_kind_given_as_a_string_is_refused_before_its_letters_are_read():
    """`faults="no_key"` would otherwise be read as seven one-letter kinds."""
    with pytest.raises(TypeError) as caught:
        Health(ok=False, faults="no_key")
    assert str(caught.value) == (
        "Health.faults is a tuple of kinds, and it was given the string 'no_key'. Write faults=('no_key',)."
    )


def test_a_kind_that_is_not_a_string_is_refused():
    with pytest.raises(TypeError, match=r"Health\.faults holds strings, and it was given 3 \(int\)\."):
        Health(ok=False, faults=(3,))


def test_kinds_given_as_a_generator_are_read_once_and_kept():
    """A generator is used up by the check, so the report keeps what it read.
    The engine asks `"model_unavailable" in health.faults`, and a generator
    kept as given would answer no, refusing a boot that should go on."""
    health = Health(ok=False, faults=(kind for kind in ("model_unavailable", "x_cold")))
    assert health.faults == ("model_unavailable", "x_cold")
    assert "model_unavailable" in health.faults and "model_unavailable" in health.faults


def test_kinds_given_as_a_list_are_kept_as_a_tuple_so_the_report_hashes():
    health = Health(ok=False, faults=["no_key"])
    assert health.faults == ("no_key",)
    assert hash(health) == hash(Health(ok=False, faults=("no_key",)))


@pytest.mark.parametrize(("given", "shown"), [(None, "None (NoneType)"), (3, "3 (int)")])
def test_faults_that_hold_no_kinds_at_all_are_refused_with_what_to_write(given, shown):
    with pytest.raises(TypeError) as caught:
        Health(ok=False, faults=given)
    assert str(caught.value) == (
        f"Health.faults is a tuple of kinds, and it was given {shown}. Write faults=() when there is none."
    )
