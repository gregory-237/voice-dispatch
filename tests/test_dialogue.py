import pytest
from dispatch.dialogue import Dialogue
from dispatch.domain import normalize_phone


def test_confirm_only_after_complete_intake():
    flow = Dialogue()
    for text in ["Течёт кран", "ул. Мира 12", "34", "Ирина", "8 999 000 12 34", "нет"]:
        assert flow.accept(text).action == "continue"
    assert flow.state == "confirm"
    assert flow.slots["consent"] is False
    assert flow.accept("да").action == "create"


@pytest.mark.parametrize("state", ["name", "confirm", "callback"])
def test_emergency_preempts_questions(state):
    flow = Dialogue(state=state)
    assert flow.accept("В квартире запах газа").action == "emergency"


def test_phone_validation():
    assert normalize_phone("8 (900) 000-10-10") == "+79000001010"
    with pytest.raises(ValueError):
        normalize_phone("нет телефона")


def test_callback_dispute():
    flow = Dialogue(callback=17)
    flow.start()
    assert flow.accept("Нет, всё ещё течёт").action == "reopen"
