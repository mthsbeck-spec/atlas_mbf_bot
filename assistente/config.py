"""Configuração lida de variáveis de ambiente (.env)."""
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo


def _env(nome: str, padrao: str = "") -> str:
    """Variável vazia conta como não definida (evita erro ao colar o .env.example com campos em branco)."""
    return (os.getenv(nome) or "").strip() or padrao


def _int(nome: str, padrao: int = 0) -> int:
    v = _env(nome, str(padrao))
    try:
        return int(v)
    except ValueError:
        raise SystemExit(f"Variável {nome} deve ser um número, veio: {v!r}")


def _lista(nome: str, padrao: str = "") -> list[str]:
    return [x.strip() for x in _env(nome, padrao).split(",") if x.strip()]


def _hora(nome: str, padrao: str) -> tuple[int, int]:
    h, m = _env(nome, padrao).split(":")
    return int(h), int(m)


@dataclass
class Config:
    # Telegram
    telegram_token: str = field(default_factory=lambda: _env("TELEGRAM_TOKEN", ""))
    # Só este chat pode falar com o bot (seu ID pessoal no Telegram)
    chat_id_autorizado: int = field(default_factory=lambda: _int("TELEGRAM_CHAT_ID"))

    # Claude
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY", ""))
    modelo: str = field(default_factory=lambda: _env("CLAUDE_MODEL", "claude-sonnet-4-5"))
    # Modelo mais barato para as rotinas automáticas (briefing, pendências, fim do dia, reuniões)
    modelo_rotinas: str = field(default_factory=lambda: _env("CLAUDE_MODEL_ROTINAS", "claude-haiku-4-5"))

    # Transcrição de voz (OpenAI)
    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY", ""))
    modelo_transcricao: str = field(default_factory=lambda: _env("TRANSCRICAO_MODELO", "gpt-4o-transcribe"))

    # Microsoft 365 (registro de app no Entra ID / Azure AD)
    ms_client_id: str = field(default_factory=lambda: _env("MS_CLIENT_ID", ""))
    ms_tenant_id: str = field(default_factory=lambda: _env("MS_TENANT_ID", "organizations"))

    # Painel web (opcional: só sobe se PAINEL_SENHA estiver definida)
    painel_senha: str = field(default_factory=lambda: _env("PAINEL_SENHA", ""))
    porta: int = field(default_factory=lambda: _int("PORT", 8080))

    # Persistência
    pasta_dados: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "./data")))

    # Fuso e preferências
    fuso: ZoneInfo = field(default_factory=lambda: ZoneInfo(_env("TZ_NAME", "America/Sao_Paulo")))
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
    minutos_antes_reuniao: int = field(default_factory=lambda: _int("MINUTOS_ANTES_REUNIAO", 15))
    dias_sem_resposta: int = field(default_factory=lambda: _int("DIAS_SEM_RESPOSTA", 3))

    @property
    def arquivo_token(self) -> Path:
        return self.pasta_dados / "ms_token_cache.json"

    @property
    def arquivo_db(self) -> Path:
        return self.pasta_dados / "assistente.db"
