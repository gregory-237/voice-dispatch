"""Create deployment secrets once. Run from the repository root."""

import secrets
from pathlib import Path

if __name__ == "__main__":
    config = Path(".env.example").read_text(encoding="utf-8")
    for key in [
        "POSTGRES_PASSWORD",
        "SESSION_SECRET",
        "ADMIN_PASSWORD",
        "API_TOKEN",
        "WEBHOOK_SECRET",
        "AMI_PASSWORD",
    ]:
        config = config.replace(key + "=\n", key + "=" + secrets.token_urlsafe(32) + "\n")
    with Path(".env").open("x", encoding="utf-8") as f:
        f.write(config)
    print("Создан .env. Пароль первого входа хранится в ADMIN_PASSWORD.")
