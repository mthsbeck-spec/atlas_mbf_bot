"""Rotinas proativas: briefing matinal, preparo de reuniões, cobrança de pendências e resumo do fim do dia.

Cada rotina coleta os dados de forma determinística (Graph) e pede ao Claude para redigir a mensagem.
O Claude ainda pode usar as ferramentas para aprofundar (ex.: ler um e-mail inteiro).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .agente import Agente, Resposta


def _json(x) -> str:
    return json.dumps(x, ensure_ascii=False, default=str)


def _filtrar_ruido(emails: list[dict], ignorar: list[str]) -> list[dict]:
    ign = [i.lower() for i in ignorar]
    return [e for e in emails if not any(i in e["de"].lower() for i in ign)]


class Rotinas:
    def __init__(self, agente: Agente):
        self.ag = agente
        self.cfg = agente.cfg
        self.g = agente.graph

    def _hoje(self) -> tuple[datetime, datetime]:
        agora = datetime.now(self.cfg.fuso)
        ini = agora.replace(hour=0, minute=0, second=0, microsecond=0)
        return ini, ini + timedelta(days=1)

    # ------------------------------------------------------------ briefing matinal
    async def briefing(self) -> Resposta:
        ini, fim = self._hoje()
        janela = 3 if ini.weekday() == 0 else 1  # segunda-feira pega o fim de semana
        agenda = await self.g.listar_eventos(ini, fim)
        emails = _filtrar_ruido(await self.g.buscar_emails(dias=janela, limite=50), self.cfg.remetentes_ignorados)
        sem_resposta = await self.g.enviados_sem_resposta(self.cfg.dias_sem_resposta,
                                                         ignorar=self.cfg.remetentes_ignorados)
        prompt = f"""Monte o BRIEFING MATINAL de hoje.

Formato (texto simples, curto, para ler no celular):
☀️ Bom dia, Matheus — <dia da semana e data>

📅 AGENDA
• HH:MM Título (com quem) — destaque conflitos e convites ainda sem resposta

🔴 PRECISA DE VOCÊ
• o que exige ação dele hoje (assinaturas, decisões, respostas), mais urgente primeiro, com remetente

⏳ AGUARDANDO TERCEIROS
• e-mails que ele enviou e estão sem resposta (há quantos dias)

ℹ️ INFORMATIVO
• no máximo 5 itens relevantes; agrupe o resto em uma linha ("+ N e-mails de rotina")

Termine perguntando se ele quer que você responda ou agende algo. Se uma seção estiver vazia, omita.

AGENDA DE HOJE: {_json(agenda)}
E-MAILS RECEBIDOS (últimos {janela} dia(s), sem relatórios/newsletters): {_json(emails)}
ENVIADOS SEM RESPOSTA: {_json(sem_resposta)}"""
        return await self.ag.conversar(prompt, rotulo_historico="[Rotina] Gere meu briefing matinal.",
                                     origem="briefing")

    # ------------------------------------------------------------ cobrança de pendências
    async def pendencias(self) -> Resposta:
        sem_resposta = await self.g.enviados_sem_resposta(self.cfg.dias_sem_resposta,
                                                         ignorar=self.cfg.remetentes_ignorados)
        nao_respondidos = await self.g.recebidos_sem_resposta(dias=7, ignorar=self.cfg.remetentes_ignorados)
        prompt = f"""Faça a COBRANÇA DE PENDÊNCIAS do dia.

1) ⏳ Você está esperando: e-mails que o Matheus enviou há {self.cfg.dias_sem_resposta}+ dias sem resposta. \
Para os que parecem importantes, ofereça redigir um follow-up curto.
2) 📥 Esperam por você: e-mails recebidos que ele ainda não respondeu e que de fato pedem resposta \
(ignore avisos automáticos, cópias informativas e agradecimentos). Priorize contatos e temas prioritários.

Seja seletivo: no máximo 8 itens no total. Se não houver nada relevante, diga isso em uma linha.

ENVIADOS SEM RESPOSTA: {_json(sem_resposta)}
RECEBIDOS NÃO RESPONDIDOS (7 dias): {_json(nao_respondidos)}"""
        return await self.ag.conversar(prompt, rotulo_historico="[Rotina] Liste minhas pendências de e-mail.",
                                     origem="pendencias")

    # ------------------------------------------------------------ resumo do fim do dia
    async def fim_do_dia(self) -> Resposta:
        ini, fim = self._hoje()
        hoje = await self.g.listar_eventos(ini, fim)
        amanha = await self.g.listar_eventos(fim, fim + timedelta(days=3 if fim.weekday() == 5 else 1))
        recebidos = _filtrar_ruido(await self.g.buscar_emails(dias=1, limite=50), self.cfg.remetentes_ignorados)
        ini_utc = ini.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        recebidos = [e for e in recebidos if (e["recebido"] or "") >= ini_utc]
        enviados = await self.g.buscar_emails(pasta="sentitems", dias=1, limite=50)
        nao_respondidos = await self.g.recebidos_sem_resposta(dias=2, ignorar=self.cfg.remetentes_ignorados)
        prompt = f"""Monte o RESUMO DE FIM DE DIA.

🌙 Fechamento — <data>
✅ HOJE: reuniões que aconteceram e o que ele despachou por e-mail (agrupe por assunto, sem listar tudo)
📌 FICOU PARA AMANHÃ: o que chegou hoje e ainda espera resposta dele
📅 AMANHÃ: agenda do próximo dia útil, com alertas (reunião cedo, conflitos, algo para preparar)

Curto e objetivo.

REUNIÕES HOJE: {_json(hoje)}
E-MAILS ENVIADOS HOJE: {_json(enviados)}
RECEBIDOS HOJE: {_json(recebidos)}
AINDA SEM RESPOSTA DELE: {_json(nao_respondidos)}
AGENDA DO PRÓXIMO DIA ÚTIL: {_json(amanha)}"""
        return await self.ag.conversar(prompt, rotulo_historico="[Rotina] Gere meu resumo de fim de dia.",
                                     origem="fim_dia")

    # ------------------------------------------------------------ preparo de reuniões
    async def reunioes_proximas(self) -> list[dict]:
        """Reuniões que começam dentro da janela de aviso e ainda não foram avisadas."""
        agora = datetime.now(self.cfg.fuso)
        limite = agora + timedelta(minutes=self.ag.gestor.reunioes()[1] + 5)
        eventos = await self.g.listar_eventos(agora, limite)
        eu = await self.g.meu_email()
        saida = []
        for e in eventos:
            inicio = datetime.fromisoformat(e["inicio"]).replace(tzinfo=self.cfg.fuso)
            outros = [p for p in e["participantes"] if p and p.lower() != eu]
            if (e["dia_inteiro"] or e["minha_resposta"] == "declined" or inicio < agora or not outros
                    or self.ag.banco.ja_avisou(f"reuniao:{e['id']}:{e['inicio']}")):
                continue
            saida.append(e)
        return saida

    async def preparar_reuniao(self, evento: dict) -> Resposta:
        self.ag.banco.registrar_aviso(f"reuniao:{evento['id']}:{evento['inicio']}")
        prompt = f"""Daqui a pouco o Matheus tem esta reunião. Prepare-o em até ~12 linhas.

Antes de escrever, use buscar_emails (pasta '' = todas, dias 30) com o assunto da reunião e com from:<participante> \
dos principais participantes, para achar o contexto recente. Leia o e-mail mais relevante se precisar.

Formato:
🗓 <HH:MM> <título> (<online/local>)
👥 Quem: nomes/empresas
🧭 Contexto: o que está em jogo, últimas trocas de e-mail
❓ Pontos em aberto / o que ele precisa decidir ou levar
🔗 link da reunião, se houver

REUNIÃO: {_json(evento)}"""
        return await self.ag.conversar(prompt, rotulo_historico=f"[Rotina] Prepare-me para a reunião "
                                                                f"'{evento['titulo']}' das {evento['inicio'][11:16]}.",
                                     origem="reuniao")
