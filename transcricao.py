"""Transcrição de mensagens de voz via API da OpenAI (a API do Claude não recebe áudio)."""
from __future__ import annotations

import httpx

URL = "https://api.openai.com/v1/audio/transcriptions"
LIMITE_BYTES = 25 * 1024 * 1024  # limite da API da OpenAI

# Vocabulário que ajuda o modelo a acertar nomes e jargão
VOCABULARIO_BASE = ("LARCA Capital, FIDC, cessão, aporte, consignado, CCB, INSS, FGTS, Saque-Aniversário, "
                    "eSocial, Crédito do Trabalhador, credenciamento, convênio, Vert, Daycoval, Exército, "
                    "Outlook, Teams")


class ErroTranscricao(Exception):
    pass


class Transcritor:
    def __init__(self, api_key: str, modelo: str = "gpt-4o-transcribe", nomes_extras: list[str] | None = None):
        self.api_key = api_key
        self.modelo = modelo
        self.vocabulario = VOCABULARIO_BASE + (", " + ", ".join(nomes_extras) if nomes_extras else "")

    @property
    def ativo(self) -> bool:
        return bool(self.api_key)

    async def transcrever(self, audio: bytes, nome_arquivo: str = "voz.ogg") -> str:
        if not self.ativo:
            raise ErroTranscricao("Transcrição não configurada: falta OPENAI_API_KEY.")
        if len(audio) > LIMITE_BYTES:
            raise ErroTranscricao("Áudio grande demais (máx. 25 MB). Grave um áudio mais curto.")
        dados = {
            "model": self.modelo,
            "language": "pt",
            "response_format": "json",
            "prompt": f"Conversa de trabalho em português do Brasil. Termos: {self.vocabulario}.",
        }
        async with httpx.AsyncClient(timeout=120) as http:
            r = await http.post(URL, headers={"Authorization": f"Bearer {self.api_key}"},
                                data=dados, files={"file": (nome_arquivo, audio)})
        if r.status_code == 401:
            raise ErroTranscricao("OPENAI_API_KEY inválida. Gere outra em platform.openai.com.")
        if r.status_code == 429:
            raise ErroTranscricao("OpenAI recusou por limite/saldo. Confira os créditos em platform.openai.com.")
        if r.status_code >= 400:
            raise ErroTranscricao(f"OpenAI {r.status_code}: {r.text[:200]}")
        texto = (r.json().get("text") or "").strip()
        if not texto:
            raise ErroTranscricao("Não consegui entender o áudio. Pode repetir?")
        return texto
