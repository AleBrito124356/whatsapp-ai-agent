"""The conversation state machine: legal transitions."""

from __future__ import annotations

from app.state import (
    ALL_STATES,
    BOOKING_STEPS,
    State,
    TRANSITIONS,
    can_transition,
)


def test_booking_steps_are_ordered_and_valid():
    assert BOOKING_STEPS[0] == State.BOOKING_SERVICE
    assert BOOKING_STEPS[-1] == State.BOOKING_CONFIRM
    assert set(BOOKING_STEPS) <= ALL_STATES


def test_happy_path_transitions_are_legal():
    assert can_transition(State.IDLE, State.BOOKING_SERVICE)
    assert can_transition(State.BOOKING_SERVICE, State.BOOKING_DATE)
    assert can_transition(State.BOOKING_DATE, State.BOOKING_TIME)
    assert can_transition(State.BOOKING_TIME, State.BOOKING_NAME)
    assert can_transition(State.BOOKING_NAME, State.BOOKING_CONFIRM)


def test_cancel_and_escalate_are_always_allowed():
    for state in ALL_STATES:
        assert can_transition(state, State.IDLE)
        assert can_transition(state, State.HUMAN)


def test_illegal_shortcuts_are_rejected():
    # Cannot jump straight from picking a service to entering a name.
    assert not can_transition(State.BOOKING_SERVICE, State.BOOKING_NAME)
    # Cannot skip from idle into the middle of the flow.
    assert not can_transition(State.IDLE, State.BOOKING_TIME)


def test_unknown_target_is_rejected():
    assert not can_transition(State.IDLE, "not_a_state")


def test_transitions_table_targets_are_all_valid_states():
    for source, targets in TRANSITIONS.items():
        assert source in ALL_STATES
        assert targets <= ALL_STATES
