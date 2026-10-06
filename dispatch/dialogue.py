"""The state machine owns slot order, validation, consent and write decisions."""

import re
from dataclasses import dataclass, field
from .domain import CATEGORIES, EMERGENCY, Intake, category_for, normalize_phone

QUESTIONS = {
    "description": "Опишите, пожалуйста, что случилось.",
    "category": "К какой категории относится проблема: вода, отопление, электричество, лифт или общее имущество?",
    "address": "Назовите улицу и номер дома.",
    "apartment": "Назовите номер квартиры или скажите «общая зона».",
    "name": "Как к вам обращаться?",
    "phone": "Назовите контактный номер телефона.",
    "consent": "Разрешаете позвонить на этот номер, чтобы проверить выполнение работ?",
}
YES = re.compile(r"^(да|верно|подтверждаю|соглас\w*|разрешаю|выполнен\w*)(?:\s|[.,!]|$)", re.I)
NO = re.compile(r"^(нет|неверно|не |отказываюсь|запрещаю)", re.I)
HELLO = "Здравствуйте. Вы позвонили в автоматизированную диспетчерскую управляющей компании. Разговор записывается. "


@dataclass
class Reply:
    text: str
    action: str = "continue"


@dataclass
class Dialogue:
    greeting: str = HELLO
    callback: int | None = None
    slots: dict = field(default_factory=dict)
    state: str = "description"

    def start(self):
        if self.callback:
            self.state = "callback"
            return Reply(
                f"Здравствуйте. Это автоматизированная диспетчерская. Разговор записывается. По заявке номер {self.callback} работы выполнены?"
            )
        return Reply(self.greeting + " " + QUESTIONS[self.state])

    def accept(self, text, extracted=None):
        text = text.strip()[:1600]
        if EMERGENCY.search(text):
            self.slots["description"] = text
            self.slots["category"] = category_for(text)
            return Reply("Передаю аварийное обращение дежурному диспетчеру.", "emergency")
        if re.search(r"оператор|соедините с диспетчером|позовите диспетчера", text, re.I):
            return Reply("Соединяю с диспетчером.", "transfer")
        if self.state == "callback":
            if NO.match(text):
                return Reply("Зафиксировала замечание. Заявка возвращена в работу.", "reopen")
            if YES.match(text):
                return Reply("Спасибо за подтверждение. Заявка закрыта.", "close")
            return Reply("Скажите «да», если проблема устранена, или «нет», если нужна доработка.")
        if self.state == "confirm":
            if YES.match(text) and not NO.match(text):
                Intake.model_validate(self.slots)
                return Reply("Заявка зарегистрирована.", "create")
            if NO.match(text):
                self.slots.clear()
                self.state = "description"
                return Reply("Запишу исправленные данные. " + QUESTIONS[self.state])
            return Reply("Всё записано верно? Ответьте «да» или «нет».")
        if not text:
            return Reply(QUESTIONS[self.state])
        value = text
        if self.state == "phone":
            try:
                value = normalize_phone(text)
            except ValueError:
                try:
                    value = normalize_phone((extracted or {}).get("phone") or "")
                except ValueError:
                    return Reply("Повторите, пожалуйста, номер с кодом города или оператора.")
        elif self.state == "consent":
            if not (YES.match(text) or NO.match(text)):
                return Reply("Разрешаете контрольный звонок? Ответьте «да» или «нет».")
            value = bool(YES.match(text)) and not NO.match(text)
        elif self.state == "category":
            value = category_for(text)
        else:
            limits = {"description": (3, 1600), "address": (3, 240), "apartment": (1, 32), "name": (2, 100)}
            lo, hi = limits[self.state]
            if not lo <= len(value) <= hi:
                return Reply(QUESTIONS[self.state])
        self.slots[self.state] = value
        if self.state == "description":
            category = category_for(text)
            if category == "other":
                category = (extracted or {}).get("category") or "other"
            if category in CATEGORIES and category != "other":
                self.slots["category"] = category
        self.state = next((k for k in QUESTIONS if k not in self.slots), "confirm")
        if self.state != "confirm":
            return Reply(QUESTIONS[self.state])
        d = Intake.model_validate(self.slots)
        permission = "разрешён" if d.consent else "не разрешён"
        return Reply(
            f"Проверим заявку. {d.address}, квартира {d.apartment}. Имя {d.name}. "
            f"{CATEGORIES[d.category]}. {d.description}. Телефон {' '.join(d.phone[1:])}. "
            f"Контрольный звонок {permission}. Всё верно?"
        )
