"""Agente: Claude + ferramentas do Outlook. Leitura é livre; escrita gera uma ação pendente de confirmação."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from anthropic import AsyncAnthropic

from .armazenamento import Armazenamento
from .config import Config
from .gestao import Gestor
from .graph import ErroGraph, Graph

log = logging.getLogger(__name__)
MAX_PASSOS = 12

# ---------------------------------------------------------------- ferramentas expostas ao Claude
FERRAMENTAS: list[dict] = [
    {
        "name": "buscar_emails",
        "description": "Lista/busca e-mails. Sem 'texto' traz os mais recentes. 'texto' aceita palavras e "
                       "sintaxe KQL do Outlook (ex.: from:leandro, subject:FIDC, hasattachments:true).",
        "input_schema": {"type": "object", "properties": {
            "texto": {"type": "string"},
            "pasta": {"type": "string", "description": "inbox (padrão), sentitems, archive, drafts ou nome de "
                                                      "pasta. Use '' para todas as pastas."},
            "dias": {"type": "integer", "description": "Janela em dias (padrão 7)"},
            "apenas_nao_lidos": {"type": "boolean"},
            "limite": {"type": "integer", "description": "Máx. 50 (padrão 15)"}}},
    },
    {
        "name": "ler_email",
        "description": "Lê o conteúdo completo de um e-mail pelo id.",
        "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    },
    {
        "name": "enviados_sem_resposta",
        "description": "E-mails que o Matheus enviou e ninguém respondeu ainda.",
        "input_schema": {"type": "object", "properties": {
            "dias_min": {"type": "integer", "description": "Enviados há pelo menos N dias (padrão 3)"}}},
    },
    {
        "name": "listar_eventos",
        "description": "Eventos da agenda entre duas datas (YYYY-MM-DD ou YYYY-MM-DDTHH:MM, horário de Brasília).",
        "input_schema": {"type": "object", "properties": {
            "inicio": {"type": "string"}, "fim": {"type": "string"}}, "required": ["inicio", "fim"]},
    },
    {
        "name": "horarios_livres",
        "description": "Encontra janelas livres na agenda em dias úteis, entre 9h e 18h.",
        "input_schema": {"type": "object", "properties": {
            "duracao_min": {"type": "integer"}, "a_partir_de": {"type": "string", "description": "YYYY-MM-DD"},
            "dias": {"type": "integer", "description": "Quantos dias olhar (padrão 5)"}},
            "required": ["duracao_min"]},
    },
    # ---- ações (todas passam por confirmação do Matheus no Telegram)
    {
        "name": "preparar_email",
        "description": "Cria um RASCUNHO de e-mail novo no Outlook e pede confirmação para enviar.",
        "input_schema": {"type": "object", "properties": {
            "para": {"type": "array", "items": {"type": "string"}},
            "cc": {"type": "array", "items": {"type": "string"}},
            "assunto": {"type": "string"}, "corpo": {"type": "string"}},
            "required": ["para", "assunto", "corpo"]},
    },
    {
        "name": "preparar_resposta",
        "description": "Cria um RASCUNHO de resposta a um e-mail e pede confirmação para enviar.",
        "input_schema": {"type": "object", "properties": {
            "id": {"type": "string"}, "corpo": {"type": "string"},
            "responder_todos": {"type": "boolean"}}, "required": ["id", "corpo"]},
    },
    {
        "name": "preparar_evento",
        "description": "Propõe criar um evento/reunião na agenda (pede confirmação).",
        "input_schema": {"type": "object", "properties": {
            "titulo": {"type": "string"},
            "inicio": {"type": "string", "description": "YYYY-MM-DDTHH:MM (Brasília)"},
            "fim": {"type": "string", "description": "YYYY-MM-DDTHH:MM (Brasília)"},
            "participantes": {"type": "array", "items": {"type": "string"}},
            "local": {"type": "string"}, "descricao": {"type": "string"},
            "online": {"type": "boolean", "description": "Gerar link do Teams"}},
            "required": ["titulo", "inicio", "fim"]},
    },
    {
        "name": "preparar_resposta_convite",
        "description": "Propõe aceitar, recusar ou responder 'talvez' a um convite (pede confirmação).",
        "input_schema": {"type": "object", "properties": {
            "id_evento": {"type": "string"},
            "resposta": {"type": "string", "enum": ["aceitar", "recusar", "talvez"]},
            "comentario": {"type": "string"}}, "required": ["id_evento", "resposta"]},
    },
    {
        "name": "preparar_organizacao_email",
        "description": "Propõe mover um e-mail de pasta e/ou aplicar categorias (Ação, Aguardando, Assinar).",
        "input_schema": {"type": "object", "properties": {
            "id": {"type": "string"}, "assunto": {"type": "string", "description": "Para exibir ao Matheus"},
            "mover_para": {"type": "string"},
            "categorias": {"type": "array", "items": {"type": "string"}}}, "required": ["id", "assunto"]},
    },
]


@dataclass
class Resposta:
    texto: str
    acoes: list[dict] = field(default_factory=list)  # [{"id":..., "resumo":...}]


def _dt(s: str, fuso) -> datetime:
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=fuso)


class Agente:
    def __init__(self, cfg: Config, graph: Graph, banco: Armazenamento, cliente: AsyncAnthropic | None = None,
                 gestor: Gestor | None = None):
        self.cfg, self.graph, self.banco = cfg, graph, banco
        self.gestor = gestor or Gestor(cfg, banco)
        self.claude = cliente or AsyncAnthropic(api_key=cfg.anthropic_api_key)

    # ---------------- prompt de sistema
    def _sistema(self) -> list[dict]:
        agora = datetime.now(self.cfg.fuso)
        dias = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
        fixo = f"""Você é o assistente pessoal do Matheus Beck, sócio da LARCA Capital (SCD em Porto Alegre que opera \
consignado militar, INSS, FGTS Saque-Aniversário e Crédito do Trabalhador). Os outros sócios são Leandro e Marcelo.
Você conversa com ele pelo Telegram e tem acesso ao Outlook (e-mails e agenda) dele.

Regras:
- Responda sempre em português do Brasil, direto e curto: é uma conversa de celular.
- Texto simples, sem Markdown (sem **, #, tabelas). Pode usar quebras de linha, "•" e poucos emojis funcionais.
- Busque os dados com as ferramentas antes de afirmar algo sobre e-mails ou agenda. Nunca invente.
- Para QUALQUER alteração (enviar/responder e-mail, criar evento, responder convite, mover/categorizar) use as \
ferramentas preparar_*. Elas NÃO executam: geram um botão de confirmação para o Matheus. Nunca diga que algo foi \
enviado ou criado; diga que está aguardando a confirmação dele.
- Ao redigir e-mails em nome dele: tom profissional e cordial, sem floreios, assinatura "Matheus Beck | LARCA Capital".
- Prioridades dele: assinaturas pendentes e temas {", ".join(self.cfg.temas_prioritarios)}; \
mensagens de {", ".join(self.cfg.contatos_prioritarios)}.
- Ignore relatórios automáticos e newsletters, a menos que ele pergunte.
- Datas e horas sempre no horário de Brasília."""
        return [
            {"type": "text", "text": fixo, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": f"Agora: {dias[agora.weekday()]}, {agora:%d/%m/%Y %H:%M}."},
        ]

    # ---------------- laço principal
    async def conversar(self, texto: str, rotulo_historico: str | None = None,
                        usar_historico: bool = True, origem: str = "conversa") -> Resposta:
        """`rotulo_historico`: texto curto salvo no histórico no lugar de `texto` (usado pelas rotinas,
        para não guardar o pacote de dados inteiro, mas permitir perguntas de acompanhamento)."""
        msgs = (self.banco.historico() if usar_historico else []) + [{"role": "user", "content": texto}]
        acoes: list[dict] = []
        final = ""
        for _ in range(MAX_PASSOS):
            try:
                r = await self.claude.messages.create(
                    model=self.cfg.modelo, max_tokens=2000, system=self._sistema(),
                    tools=FERRAMENTAS, messages=msgs)
            except Exception as e:
                self.gestor.status("claude", False, str(e))
                raise
            self.gestor.status("claude", True)
            if getattr(r, "usage", None) is not None:
                self.gestor.registrar_claude(self.cfg.modelo, r.usage, origem)
            msgs.append({"role": "assistant", "content": r.content})
            if r.stop_reason != "tool_use":
                final = "".join(b.text for b in r.content if b.type == "text").strip()
                break
            resultados = []
            for bloco in r.content:
                if bloco.type != "tool_use":
                    continue
                try:
                    saida = await self._executar(bloco.name, bloco.input, acoes)
                    resultados.append({"type": "tool_result", "tool_use_id": bloco.id,
                                       "content": json.dumps(saida, ensure_ascii=False, default=str)})
                except Exception as e:  # erro volta ao Claude para ele explicar/contornar
                    log.exception("Erro na ferramenta %s", bloco.name)
                    resultados.append({"type": "tool_result", "tool_use_id": bloco.id,
                                       "content": f"Erro: {e}", "is_error": True})
            msgs.append({"role": "user", "content": resultados})
        else:
            final = "Não consegui concluir em poucos passos. Pode reformular de forma mais específica?"

        final = final or ("Preparei a ação abaixo para você confirmar." if acoes else "Feito.")
        self.banco.adicionar_historico("user", rotulo_historico or texto)
        self.banco.adicionar_historico("assistant", final)
        return Resposta(final, acoes)

    # ---------------- despacho das ferramentas
    async def _executar(self, nome: str, a: dict[str, Any], acoes: list[dict]) -> Any:
        g, fuso = self.graph, self.cfg.fuso
        if nome == "buscar_emails":
            return await g.buscar_emails(a.get("texto"), a.get("pasta", "inbox"), a.get("dias", 7),
                                         a.get("apenas_nao_lidos", False), a.get("limite", 15))
        if nome == "ler_email":
            return await g.ler_email(a["id"])
        if nome == "enviados_sem_resposta":
            return await g.enviados_sem_resposta(a.get("dias_min", self.cfg.dias_sem_resposta),
                                                 ignorar=self.cfg.remetentes_ignorados)
        if nome == "listar_eventos":
            ini, fim = _dt(a["inicio"], fuso), _dt(a["fim"], fuso)
            if len(a["fim"]) == 10:
                fim += timedelta(days=1)  # data sem hora → inclui o dia inteiro
            return await g.listar_eventos(ini, fim)
        if nome == "horarios_livres":
            return await self._horarios_livres(a["duracao_min"], a.get("a_partir_de"), a.get("dias", 5))

        # ---- ações com confirmação
        if nome == "preparar_email":
            rasc = await g.criar_rascunho(a["para"], a["assunto"], a["corpo"], a.get("cc"))
            resumo = (f"✉️ Enviar e-mail\nPara: {', '.join(a['para'])}"
                      + (f"\nCc: {', '.join(a['cc'])}" if a.get("cc") else "")
                      + f"\nAssunto: {a['assunto']}\n\n{a['corpo']}")
            return self._registrar("enviar_rascunho", {"id": rasc["id"]}, resumo, acoes)
        if nome == "preparar_resposta":
            orig = await g.ler_email(a["id"])
            rasc = await g.criar_rascunho_resposta(a["id"], a["corpo"], a.get("responder_todos", False))
            resumo = (f"↩️ Responder{' a todos' if a.get('responder_todos') else ''}\n"
                      f"Para: {orig['de']}\nAssunto: {orig['assunto']}\n\n{a['corpo']}")
            return self._registrar("enviar_rascunho", {"id": rasc["id"]}, resumo, acoes)
        if nome == "preparar_evento":
            ini, fim = _dt(a["inicio"], fuso), _dt(a["fim"], fuso)
            resumo = (f"📅 Criar evento: {a['titulo']}\n{ini:%d/%m %H:%M}–{fim:%H:%M}"
                      + (f"\nLocal: {a['local']}" if a.get("local") else "")
                      + (" (Teams)" if a.get("online") else "")
                      + (f"\nConvidados: {', '.join(a['participantes'])}" if a.get("participantes") else ""))
            dados = {**a, "inicio": ini.strftime("%Y-%m-%dT%H:%M:00"), "fim": fim.strftime("%Y-%m-%dT%H:%M:00")}
            return self._registrar("criar_evento", dados, resumo, acoes)
        if nome == "preparar_resposta_convite":
            resumo = f"📨 {a['resposta'].capitalize()} convite" + (
                f"\nComentário: {a['comentario']}" if a.get("comentario") else "")
            return self._registrar("responder_convite", a, resumo, acoes)
        if nome == "preparar_organizacao_email":
            partes = []
            if a.get("mover_para"):
                partes.append(f"mover para '{a['mover_para']}'")
            if a.get("categorias"):
                partes.append(f"categorias: {', '.join(a['categorias'])}")
            resumo = f"🗂 {a['assunto']}\n" + " e ".join(partes)
            return self._registrar("organizar_email", a, resumo, acoes)
        raise ValueError(f"Ferramenta desconhecida: {nome}")

    def _registrar(self, tipo: str, dados: dict, resumo: str, acoes: list[dict]) -> dict:
        id_acao = self.banco.nova_acao(tipo, dados, resumo)
        acoes.append({"id": id_acao, "resumo": resumo})
        return {"status": "aguardando_confirmacao", "id_acao": id_acao,
                "obs": "O Matheus verá um botão para confirmar. Não diga que foi executado."}

    async def _horarios_livres(self, duracao: int, a_partir_de: str | None, dias: int) -> list[str]:
        fuso = self.cfg.fuso
        agora = datetime.now(fuso)
        inicio = _dt(a_partir_de, fuso) if a_partir_de else agora
        eventos = await self.graph.listar_eventos(inicio.replace(hour=0, minute=0), inicio + timedelta(days=dias + 1))
        ocupados = [(_dt(e["inicio"], fuso), _dt(e["fim"], fuso)) for e in eventos
                    if not e["dia_inteiro"] and e["minha_resposta"] != "declined"]
        return calcular_janelas(ocupados, inicio, agora, dias, duracao, fuso)


SEMANA = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


def calcular_janelas(ocupados: list[tuple[datetime, datetime]], inicio: datetime, agora: datetime,
                     dias: int, duracao: int, fuso, h_ini: int = 9, h_fim: int = 18) -> list[str]:
    livres: list[str] = []
    dur = timedelta(minutes=duracao)
    for d in range(dias + 1):
        dia = (inicio + timedelta(days=d)).date()
        if dia.weekday() >= 5:
            continue
        ini_dia = datetime(dia.year, dia.month, dia.day, h_ini, tzinfo=fuso)
        fim_dia = datetime(dia.year, dia.month, dia.day, h_fim, tzinfo=fuso)
        cursor = max(ini_dia, agora)
        if cursor.minute % 30 or cursor.second or cursor.microsecond:  # próximo múltiplo de 30 min
            cursor = cursor.replace(second=0, microsecond=0) + timedelta(minutes=30 - cursor.minute % 30)
        blocos = sorted((max(s, ini_dia), min(e, fim_dia)) for s, e in ocupados if s < fim_dia and e > ini_dia)
        for s, e in blocos + [(fim_dia, fim_dia)]:
            if s - cursor >= dur:
                livres.append(f"{SEMANA[dia.weekday()]} {s:%d/%m} {cursor:%H:%M}–{s:%H:%M}")
            cursor = max(cursor, e)
        if len(livres) >= 12:
            break
    return livres


# ---------------------------------------------------------------- execução após o "Confirmar"
async def executar_acao(graph: Graph, acao: dict) -> str:
    d = acao["dados"]
    t = acao["tipo"]
    try:
        if t == "enviar_rascunho":
            await graph.enviar_rascunho(d["id"])
            return "✅ E-mail enviado."
        if t == "criar_evento":
            ev = await graph.criar_evento(d["titulo"], d["inicio"], d["fim"], d.get("participantes"),
                                          d.get("local"), d.get("descricao"), d.get("online", False))
            return f"✅ Evento criado: {ev['titulo']} ({ev['inicio'].replace('T', ' ')})"
        if t == "responder_convite":
            await graph.responder_convite(d["id_evento"], d["resposta"], d.get("comentario", ""))
            return "✅ Resposta ao convite enviada."
        if t == "organizar_email":
            if d.get("categorias"):
                await graph.categorizar_email(d["id"], d["categorias"])
            if d.get("mover_para"):
                await graph.mover_email(d["id"], d["mover_para"])
            return "✅ E-mail organizado."
    except ErroGraph as e:
        return f"⚠️ Não consegui executar: {e}"
    return "⚠️ Tipo de ação desconhecido."


async def cancelar_acao(graph: Graph, acao: dict) -> None:
    """Ao cancelar um e-mail, apaga o rascunho criado no Outlook."""
    if acao["tipo"] == "enviar_rascunho":
        try:
            await graph.excluir_rascunho(acao["dados"]["id"])
        except ErroGraph:
            pass
