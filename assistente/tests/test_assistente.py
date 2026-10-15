"""Testes com Graph e Claude simulados (não acessam a internet). Rode: python -m pytest -q"""
import asyncio
from datetime import datetime
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest

from assistente.agente import Agente, calcular_janelas, executar_acao
from assistente.armazenamento import Armazenamento
from assistente.bot import _partes
from assistente.config import Config
from assistente.graph import _houve_resposta
from assistente.rotinas import Rotinas

FUSO = ZoneInfo("America/Sao_Paulo")


class GraphFalso:
    def __init__(self):
        self.enviados, self.rascunhos = [], []

    async def meu_email(self):
        return "matheus@larcacapital.com.br"

    async def buscar_emails(self, texto=None, pasta="inbox", dias=7, apenas_nao_lidos=False, limite=15):
        return [{"id": "m1", "assunto": "Aporte FIDC", "de": "Leandro <leandro@larcacapital.com.br>",
                 "recebido": "2026-10-14T11:00:00Z", "lido": False, "conversa": "c1", "previa": "..."},
                {"id": "m2", "assunto": "Relatório diário", "de": "BI <bi@larcacapital.com.br>",
                 "recebido": "2026-10-14T10:00:00Z", "lido": True, "conversa": "c2", "previa": "..."}]

    async def ler_email(self, id_msg):
        return {"id": id_msg, "assunto": "Aporte FIDC", "de": "Leandro <leandro@larcacapital.com.br>"}

    async def criar_rascunho_resposta(self, id_msg, corpo, todos=False):
        self.rascunhos.append(corpo)
        return {"id": "rasc1"}

    async def enviar_rascunho(self, id_r):
        self.enviados.append(id_r)

    async def listar_eventos(self, ini, fim):
        return []

    async def enviados_sem_resposta(self, *a, **k):
        return []


class ClaudeFalso:
    """Primeiro pede para buscar e-mails, depois prepara uma resposta, depois responde em texto."""

    def __init__(self):
        self.chamadas = []
        roteiro = [
            [NS(type="tool_use", id="t1", name="buscar_emails", input={"texto": "FIDC"})],
            [NS(type="tool_use", id="t2", name="preparar_resposta", input={"id": "m1", "corpo": "Ok, de acordo."})],
            [NS(type="text", text="Preparei a resposta ao Leandro; confirme no botão.")],
        ]
        self.messages = NS(create=self._create)
        self._roteiro = iter(roteiro)

    async def _create(self, **kw):
        self.chamadas.append(kw)
        conteudo = next(self._roteiro)
        stop = "tool_use" if conteudo[0].type == "tool_use" else "end_turn"
        return NS(content=conteudo, stop_reason=stop)


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    cfg = Config()
    banco = Armazenamento(cfg.arquivo_db)
    g = GraphFalso()
    claude = ClaudeFalso()
    return cfg, banco, g, claude, Agente(cfg, g, banco, cliente=claude)


def test_laco_do_agente_gera_acao_sem_enviar(ambiente):
    cfg, banco, g, claude, ag = ambiente
    r = asyncio.run(ag.conversar("responde o Leandro sobre o FIDC"))
    assert "confirme" in r.texto
    assert len(r.acoes) == 1 and "Responder" in r.acoes[0]["resumo"]
    assert g.rascunhos == ["Ok, de acordo."] and g.enviados == []  # nada enviado sem confirmação
    # resultados de ferramenta voltaram ao Claude no formato certo
    ult = claude.chamadas[-1]["messages"][-2]["content"][0]  # [-1] é a resposta final
    assert ult["type"] == "tool_result" and ult["tool_use_id"] == "t2"
    # histórico guardou só texto
    assert [m["role"] for m in banco.historico()] == ["user", "assistant"]


def test_confirmacao_executa_uma_vez(ambiente):
    cfg, banco, g, claude, ag = ambiente
    r = asyncio.run(ag.conversar("responde"))
    acao = banco.pegar_acao(r.acoes[0]["id"])
    assert banco.marcar_acao(acao["id"], "processando") is True
    assert banco.marcar_acao(acao["id"], "processando") is False  # duplo clique
    assert asyncio.run(executar_acao(g, acao)).startswith("✅")
    assert g.enviados == ["rasc1"]


def test_houve_resposta():
    eu = "matheus@larcacapital.com.br"
    conversa = [
        {"from": {"emailAddress": {"address": eu}}, "sentDateTime": "2026-10-10T12:00:00Z"},
        {"from": {"emailAddress": {"address": "x@vert.com.br"}}, "receivedDateTime": "2026-10-09T12:00:00Z"},
    ]
    assert _houve_resposta(conversa, eu, "2026-10-10T12:00:00Z") is False
    conversa.append({"from": {"emailAddress": {"address": "x@vert.com.br"}},
                     "receivedDateTime": "2026-10-11T09:00:00Z"})
    assert _houve_resposta(conversa, eu, "2026-10-10T12:00:00Z") is True


def test_janelas_livres():
    agora = datetime(2026, 10, 14, 8, 0, tzinfo=FUSO)  # quarta
    ocupados = [(datetime(2026, 10, 14, 9, 0, tzinfo=FUSO), datetime(2026, 10, 14, 11, 0, tzinfo=FUSO)),
                (datetime(2026, 10, 14, 11, 30, tzinfo=FUSO), datetime(2026, 10, 14, 17, 30, tzinfo=FUSO))]
    j = calcular_janelas(ocupados, agora, agora, 0, 30, FUSO)
    assert j == ["qua 14/10 11:00–11:30", "qua 14/10 17:30–18:00"]
    assert calcular_janelas(ocupados, agora, agora, 0, 60, FUSO) == []
    sabado = datetime(2026, 10, 17, 8, 0, tzinfo=FUSO)
    assert calcular_janelas([], sabado, sabado, 1, 30, FUSO) == []  # fim de semana pulado


def test_partes_telegram():
    texto = ("linha\n" * 2000)
    partes = _partes(texto)
    assert all(len(p) <= 4096 for p in partes) and "".join(partes).strip() == texto.strip()


def test_briefing_filtra_relatorios(ambiente):
    cfg, banco, g, claude, ag = ambiente
    capturado = {}

    async def conversar(prompt, rotulo_historico=None, usar_historico=True):
        capturado["prompt"] = prompt
        return NS(texto="ok", acoes=[])
    ag.conversar = conversar
    asyncio.run(Rotinas(ag).briefing())
    assert "Aporte FIDC" in capturado["prompt"] and "Relatório diário" not in capturado["prompt"]


def test_login_pelo_telegram(tmp_path, monkeypatch):
    import assistente.bot as b
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")

    class AuthFalso:
        def iniciar_login(self):
            return {"verification_uri": "https://microsoft.com/devicelogin", "user_code": "ABC123"}

        def concluir_login(self, fluxo):
            return {"access_token": "x"}

    monkeypatch.setattr(b, "Autenticador", lambda *a: AuthFalso())
    bot = b.Bot(Config())

    async def eu():
        return {"displayName": "Matheus Beck", "mail": "matheus@larcacapital.com.br"}
    bot.graph.eu = eu
    respostas, enviados, tarefas = [], [], []

    async def reply_text(t):
        respostas.append(t)

    async def send_message(cid, t):
        enviados.append((cid, t))
    update = NS(message=NS(reply_text=reply_text))
    ctx = NS(bot=NS(send_message=send_message), application=NS(create_task=lambda c: tarefas.append(c)))

    async def rodar():
        await bot.c_login(update, ctx)
        await tarefas[0]
    asyncio.run(rodar())
    assert "ABC123" in respostas[0] and enviados == [(42, enviados[0][1])] and "✅" in enviados[0][1]
