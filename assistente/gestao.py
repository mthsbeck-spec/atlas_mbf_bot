"""Gestão: ajustes editáveis (rotinas, limite, preços), registro de custos e status das integrações."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from . import custos
from .armazenamento import Armazenamento
from .config import Config

ROTINAS = {  # chave: (rótulo, atributo de horário padrão na Config)
    "briefing": ("Briefing matinal", "hora_briefing"),
    "pendencias": ("Cobrança de pendências", "hora_pendencias"),
    "fim_dia": ("Resumo do fim do dia", "hora_fim_dia"),
}
INTEGRACOES = {
    "telegram": "Telegram",
    "microsoft365": "Microsoft 365 (Outlook e agenda)",
    "claude": "Claude (Anthropic)",
    "openai": "Transcrição de voz (OpenAI)",
}
ORIGENS_ROTINA = {"briefing", "pendencias", "fim_dia", "reuniao"}
MODELOS_ROTINAS = {"claude-haiku-4-5": "Claude Haiku 4.5 (mais barato)", "claude-sonnet-4-5": "Claude Sonnet 4.5"}
ORIGENS = {
    "conversa": "Conversas por texto", "voz": "Conversas por voz", "transcricao": "Transcrição de áudio",
    "briefing": "Briefing matinal", "pendencias": "Cobrança de pendências", "fim_dia": "Resumo do fim do dia",
    "reuniao": "Preparo de reuniões", "agenda": "Comando /agenda",
}


def _bool(v: str | None) -> bool:
    return str(v).lower() in ("1", "true", "sim", "on")


class Gestor:
    def __init__(self, cfg: Config, banco: Armazenamento):
        self.cfg, self.banco = cfg, banco

    # ------------------------------------------------------------ datas
    def hoje(self) -> date:
        return datetime.now(self.cfg.fuso).date()

    def mes_atual(self) -> tuple[str, str]:
        h = self.hoje()
        ini = h.replace(day=1)
        fim = (ini + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        return ini.isoformat(), fim.isoformat()

    # ------------------------------------------------------------ rotinas
    def rotina(self, chave: str) -> tuple[bool, tuple[int, int]]:
        ativo = _bool(self.banco.ajuste(f"rotina_{chave}_ativa", "1"))
        padrao = getattr(self.cfg, ROTINAS[chave][1])
        h = self.banco.ajuste(f"rotina_{chave}_hora", f"{padrao[0]:02d}:{padrao[1]:02d}")
        hh, mm = h.split(":")
        return ativo, (int(hh), int(mm))

    def salvar_rotina(self, chave: str, ativa: bool, hora: str) -> None:
        if chave not in ROTINAS:
            raise ValueError("Rotina desconhecida")
        hh, mm = hora.strip().split(":")
        if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
            raise ValueError("Horário inválido")
        self.banco.salvar_ajuste(f"rotina_{chave}_ativa", "1" if ativa else "0")
        self.banco.salvar_ajuste(f"rotina_{chave}_hora", f"{int(hh):02d}:{int(mm):02d}")

    def reunioes(self) -> tuple[bool, int]:
        ativo = _bool(self.banco.ajuste("rotina_reuniao_ativa", "1"))
        minutos = int(self.banco.ajuste("rotina_reuniao_minutos", str(self.cfg.minutos_antes_reuniao)))
        return ativo, minutos

    def salvar_reunioes(self, ativa: bool, minutos: int) -> None:
        if not 5 <= minutos <= 120:
            raise ValueError("Use entre 5 e 120 minutos")
        self.banco.salvar_ajuste("rotina_reuniao_ativa", "1" if ativa else "0")
        self.banco.salvar_ajuste("rotina_reuniao_minutos", str(minutos))

    # ------------------------------------------------------------ preços, limite e cotação
    def _num(self, chave: str, padrao: float) -> float:
        try:
            return float(self.banco.ajuste(chave, str(padrao)))
        except ValueError:
            return padrao

    def modelo_rotinas(self) -> str:
        return self.banco.ajuste("modelo_rotinas", self.cfg.modelo_rotinas)

    def salvar_modelo_rotinas(self, modelo: str) -> None:
        if modelo not in MODELOS_ROTINAS and modelo != self.cfg.modelo:
            raise ValueError("Modelo desconhecido")
        self.banco.salvar_ajuste("modelo_rotinas", modelo)

    def modelo_para(self, origem: str) -> str:
        return self.modelo_rotinas() if origem in ORIGENS_ROTINA else self.cfg.modelo

    def precos(self, modelo: str | None = None) -> dict[str, float]:
        """Preços do modelo de conversa podem ser editados no painel; os demais vêm da tabela."""
        modelo = modelo or self.cfg.modelo
        ent, sai = custos.precos_claude(modelo)
        editavel = modelo == self.cfg.modelo
        return {
            "entrada": self._num("preco_entrada", ent) if editavel else ent,
            "saida": self._num("preco_saida", sai) if editavel else sai,
            "audio": self._num("preco_audio_min",
                               custos.PRECOS_AUDIO.get(self.cfg.modelo_transcricao, custos.PRECO_PADRAO_AUDIO)),
        }

    def limite_mensal(self) -> float:
        return self._num("limite_mensal_usd", 0.0)  # 0 = sem limite

    def cotacao(self) -> float:
        return self._num("cotacao_brl", 5.40)

    def custo_fixo(self) -> float:
        return self._num("custo_fixo_mensal_usd", 5.0)

    def salvar_financeiro(self, *, limite: float, cotacao: float, custo_fixo: float,
                          preco_entrada: float, preco_saida: float, preco_audio: float) -> None:
        for nome, v in [("limite", limite), ("cotação", cotacao), ("custo fixo", custo_fixo),
                        ("preço de entrada", preco_entrada), ("preço de saída", preco_saida),
                        ("preço do áudio", preco_audio)]:
            if v < 0 or v > 100_000:
                raise ValueError(f"Valor inválido para {nome}")
        for chave, v in [("limite_mensal_usd", limite), ("cotacao_brl", cotacao),
                         ("custo_fixo_mensal_usd", custo_fixo), ("preco_entrada", preco_entrada),
                         ("preco_saida", preco_saida), ("preco_audio_min", preco_audio)]:
            self.banco.salvar_ajuste(chave, f"{v:g}")

    # ------------------------------------------------------------ registro de uso
    def registrar_claude(self, modelo: str, uso, origem: str) -> float:
        p = self.precos(modelo)
        ent = getattr(uso, "input_tokens", 0) or 0
        sai = getattr(uso, "output_tokens", 0) or 0
        ce = getattr(uso, "cache_creation_input_tokens", 0) or 0
        cl = getattr(uso, "cache_read_input_tokens", 0) or 0
        valor = custos.custo_claude(ent, sai, ce, cl, p["entrada"], p["saida"])
        self.banco.registrar_uso(self.hoje().isoformat(), "claude", modelo, origem, valor,
                                 entrada=ent, saida=sai, cache_escrita=ce, cache_leitura=cl)
        return valor

    def registrar_audio(self, modelo: str, segundos: float, origem: str = "transcricao") -> float:
        valor = custos.custo_audio(segundos, self.precos()["audio"])
        self.banco.registrar_uso(self.hoje().isoformat(), "openai", modelo, origem, valor, segundos=segundos)
        return valor

    def status(self, nome: str, ok: bool, erro: str | None = None) -> None:
        self.banco.registrar_status(nome, ok, erro)

    # ------------------------------------------------------------ limite
    def situacao_limite(self) -> dict:
        ini, fim = self.mes_atual()
        gasto = self.banco.custo_periodo(ini, fim)
        limite = self.limite_mensal()
        pct = (gasto / limite * 100) if limite else 0.0
        nivel = "sem_limite" if not limite else "estourado" if pct >= 100 else "alerta" if pct >= 80 else "ok"
        return {"gasto": gasto, "limite": limite, "pct": pct, "nivel": nivel}

    def rotinas_bloqueadas(self) -> bool:
        return self.situacao_limite()["nivel"] == "estourado"

    def alertas_de_limite(self) -> list[str]:
        """Mensagens a enviar (uma vez por mês para 80% e para 100%)."""
        s = self.situacao_limite()
        if s["nivel"] in ("ok", "sem_limite"):
            return []
        mes = self.mes_atual()[0][:7]
        msgs = []
        if not self.banco.ja_avisou(f"limite80:{mes}"):
            self.banco.registrar_aviso(f"limite80:{mes}")
            if s["nivel"] == "alerta":
                msgs.append(f"⚠️ Gasto com IA chegou a {s['pct']:.0f}% do limite do mês "
                            f"(US$ {s['gasto']:.2f} de US$ {s['limite']:.2f}).")
        if s["nivel"] == "estourado" and not self.banco.ja_avisou(f"limite100:{mes}"):
            self.banco.registrar_aviso(f"limite100:{mes}")
            msgs.append(f"⛔ Limite mensal de IA atingido (US$ {s['gasto']:.2f} de US$ {s['limite']:.2f}). "
                        "Pausei as rotinas automáticas até o próximo mês; conversas continuam funcionando. "
                        "Você pode aumentar o limite no painel.")
        return msgs
