"""Inicia o bot: python -m assistente"""
import logging
import sys

from .bot import Bot
from .config import Config


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # não logar URLs com token do bot
    cfg = Config()
    faltando = [n for n, v in [("TELEGRAM_TOKEN", cfg.telegram_token), ("ANTHROPIC_API_KEY", cfg.anthropic_api_key),
                               ("MS_CLIENT_ID", cfg.ms_client_id), ("TELEGRAM_CHAT_ID", cfg.chat_id_autorizado)] if not v]
    if faltando:
        sys.exit(f"Configure no .env: {', '.join(faltando)}")
    Bot(cfg).construir().run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
