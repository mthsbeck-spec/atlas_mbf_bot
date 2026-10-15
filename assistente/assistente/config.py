"""Configuração lida de variáveis de ambiente (.env)."""
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo


def _lista(nome: str, padrao: str = "") -> list[str]:
    return [x.strip() for x in os.getenv(nome, padrao).split(",") if x.strip()]


def _hora(nome: str, padrao: str) -> tuple[int, int]:
    h, m = os.getenv(nome, padrao).split(":")
    return int(h), int(m)


@dataclass
class Config:
    # Telegram
    telegram_token: str = field(default_factory=lambda: os.environ.get("TELEGRAM_TOKEN", ""))
    # Só este chat pode falar com o bot (seu ID pessoal no Telegram)
    chat_id_autorizado: int = field(default_factory=lambda: int(os.environ.get("TELEGRAM_CHAT_ID", "0")))

    # Claude
    anthropic_api_key: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", ""))
    modelo: str = field(default_factory=lambda: os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5"))

    # Microsoft 365 (registro de app no Entra ID / Azure AD)
    ms_client_id: str = field(default_factory=lambda: os.environ.get("MS_CLIENT_ID", ""))
    ms_tenant_id: str = field(default_factory=lambda: os.environ.get("MS_TENANT_ID", "organizations"))

    # Persistência
    pasta_dados: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", "./data")))

    # Fuso e preferências
    fuso: ZoneInfo = field(default_factory=lambda: ZoneInfo(os.getenv("TZ_NAME", "America/Sao_Paulo")))
    contatos_prioritarios: list[str] = field(
        default_factory=lambda: _lista("CONTATOS_PRIORITARIOS", "Leandro,Marcelo,Dilvania,Bruno"))
    temas_prioritarios: list[str] = field(
        default_factory=lambda: _lista("TEMAS_PRIORITARIOS",
                                       "assinatura,assinar,FIDC,aporte,cessão,credenciamento"))
    remetentes_ignorados: list[str] = field(
        default_factory=lambda: _lista("REMETENTES_IGNORADOS", "bi@larcacapital.com.br,noreply,no-reply"))

    # Rotinas (seg–sex)
    hora_briefing: tuple[int, int] = field(default_factory=lambda: _hora("HORA_BRIEFING", "07:30"))
    hora_pendencias: tuple[int, int] = field(default_factory=lambda: _hora("HORA_PENDENCIAS", "14:00"))
    hora_fim_dia: tuple[int, int] = field(default_factory=lambda: _hora("HORA_FIM_DIA", "18:30"))
    minutos_antes_reuniao: int = field(default_factory=lambda: int(os.getenv("MINUTOS_ANTES_REUNIAO", "15")))
    dias_sem_resposta: int = field(default_factory=lambda: int(os.getenv("DIAS_SEM_RESPOSTA", "3")))

    @property
    def arquivo_token(self) -> Path:
        return self.pasta_dados / "ms_token_cache.json"

    @property
    def arquivo_db(self) -> Path:
        return self.pasta_dados / "assistente.db"
