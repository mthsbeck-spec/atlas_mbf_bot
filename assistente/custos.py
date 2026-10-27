"""Tabela de preços (estimativa) e cálculo de custo por chamada de API.

Preços em US$. Podem ser sobrescritos no painel (ajustes 'preco_entrada', 'preco_saida', 'preco_audio_min').
Confira os valores vigentes nas páginas de preço da Anthropic e da OpenAI.
"""
from __future__ import annotations

# US$ por milhão de tokens (entrada, saída) — por prefixo do ID do modelo
PRECOS_CLAUDE: list[tuple[str, float, float]] = [
    ("claude-opus-4-5", 5.0, 25.0),
    ("claude-opus", 15.0, 75.0),
    ("claude-sonnet", 3.0, 15.0),
    ("claude-haiku-4", 1.0, 5.0),
    ("claude-haiku", 0.8, 4.0),
]
PRECO_PADRAO_CLAUDE = (3.0, 15.0)
FATOR_CACHE_ESCRITA = 1.25
FATOR_CACHE_LEITURA = 0.10

# US$ por minuto de áudio
PRECOS_AUDIO = {"gpt-4o-transcribe": 0.006, "gpt-4o-mini-transcribe": 0.003, "whisper-1": 0.006}
PRECO_PADRAO_AUDIO = 0.006


def precos_claude(modelo: str) -> tuple[float, float]:
    for prefixo, ent, sai in PRECOS_CLAUDE:
        if modelo.startswith(prefixo):
            return ent, sai
    return PRECO_PADRAO_CLAUDE


def custo_claude(entrada: int, saida: int, cache_escrita: int, cache_leitura: int,
                 preco_entrada: float, preco_saida: float) -> float:
    return (entrada * preco_entrada
            + cache_escrita * preco_entrada * FATOR_CACHE_ESCRITA
            + cache_leitura * preco_entrada * FATOR_CACHE_LEITURA
            + saida * preco_saida) / 1_000_000


def custo_audio(segundos: float, preco_minuto: float) -> float:
    return max(segundos, 1) / 60 * preco_minuto
