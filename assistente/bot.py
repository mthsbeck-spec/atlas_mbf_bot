"""Bot do Telegram: interface de conversa, botões de confirmação e agendamento das rotinas."""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import time, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import InvalidToken
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
                          filters)

from .agente import Agente, Resposta, cancelar_acao, executar_acao
from .armazenamento import Armazenamento
from .config import Config
from .graph import Autenticador, ErroGraph, Graph
from .rotinas import Rotinas
from .transcricao import ErroTranscricao, Transcritor

log = logging.getLogger(__name__)
LIMITE_TG = 4000
DIAS_UTEIS = (1, 2, 3, 4, 5)  # PTB ≥ 20: 0 = domingo


def _partes(texto: str) -> list[str]:
    """Quebra mensagens longas respeitando parágrafos (limite do Telegram: 4096)."""
    partes, atual = [], ""
    for par in texto.split("\n"):
        while len(par) > LIMITE_TG:
            partes.append(par[:LIMITE_TG])
            par = par[LIMITE_TG:]
        if len(atual) + len(par) + 1 > LIMITE_TG:
            partes.append(atual)
            atual = ""
        atual += par + "\n"
    if atual.strip():
        partes.append(atual)
    return partes or ["(vazio)"]


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.banco = Armazenamento(cfg.arquivo_db)
        self.graph = Graph(Autenticador(cfg.ms_client_id, cfg.ms_tenant_id, cfg.arquivo_token), cfg.fuso.key)
        self.agente = Agente(cfg, self.graph, self.banco)
        self.rotinas = Rotinas(self.agente)
        self.transcritor = Transcritor(cfg.openai_api_key, cfg.modelo_transcricao, cfg.contatos_prioritarios)
        self.trava = asyncio.Lock()  # uma conversa por vez, para manter o histórico coerente

    # ------------------------------------------------------------ envio
    async def enviar(self, app: Application, resp: Resposta) -> None:
        cid = self.cfg.chat_id_autorizado
        for p in _partes(resp.texto):
            await app.bot.send_message(cid, p, disable_web_page_preview=True)
        for a in resp.acoes:
            teclado = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Confirmar", callback_data=f"ok:{a['id']}"),
                InlineKeyboardButton("❌ Cancelar", callback_data=f"no:{a['id']}")]])
            await app.bot.send_message(cid, _partes(a["resumo"])[0], reply_markup=teclado)

    async def _rodar(self, app: Application, nome: str, coro_fn) -> None:
        async with self.trava:
            try:
                await app.bot.send_chat_action(self.cfg.chat_id_autorizado, ChatAction.TYPING)
                await self.enviar(app, await coro_fn())
            except ErroGraph as e:
                await app.bot.send_message(self.cfg.chat_id_autorizado, f"⚠️ Outlook: {e}")
            except Exception as e:
                log.exception("Falha em %s", nome)
                await app.bot.send_message(self.cfg.chat_id_autorizado, f"⚠️ Falha em {nome}: {e}")

    # ------------------------------------------------------------ handlers
    async def h_texto(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        texto = update.message.text
        await self._rodar(ctx.application, "conversa", lambda: self.agente.conversar(texto))

    async def h_audio(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Mensagem de voz/áudio: baixa do Telegram, transcreve e trata como texto."""
        msg = update.message
        if not self.transcritor.ativo:
            await msg.reply_text("Para entender áudio, configure OPENAI_API_KEY nas variáveis do Railway.")
            return
        midia = msg.voice or msg.audio
        if midia.file_size and midia.file_size > 20 * 1024 * 1024:
            await msg.reply_text("Áudio grande demais para o Telegram me entregar (máx. 20 MB).")
            return
        await ctx.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
        try:
            arquivo = await midia.get_file()
            audio = bytes(await arquivo.download_as_bytearray())
            nome = getattr(midia, "file_name", None) or "voz.ogg"
            if nome.lower().endswith(".oga"):
                nome = nome[:-4] + ".ogg"
            texto = await self.transcritor.transcrever(audio, nome)
        except ErroTranscricao as e:
            await msg.reply_text(f"⚠️ {e}")
            return
        except Exception as e:
            log.exception("Falha ao transcrever")
            await msg.reply_text(f"⚠️ Falha ao transcrever o áudio: {e}")
            return
        await msg.reply_text(f"🎙️ Entendi: “{texto}”")
        await self._rodar(ctx.application, "conversa", lambda: self.agente.conversar(texto))

    async def h_botao(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q.message.chat.id != self.cfg.chat_id_autorizado:
            await q.answer("Não autorizado")
            return
        await q.answer()
        decisao, id_acao = q.data.split(":", 1)
        acao = self.banco.pegar_acao(id_acao)
        if not acao or not self.banco.marcar_acao(id_acao, "processando"):
            await q.edit_message_reply_markup(None)
            return
        if decisao == "ok":
            resultado = await executar_acao(self.graph, acao)
            self.banco.marcar_acao_final(id_acao, "executada" if resultado.startswith("✅") else "erro")
        else:
            await cancelar_acao(self.graph, acao)
            self.banco.marcar_acao_final(id_acao, "cancelada")
            resultado = "❌ Cancelado."
        self.banco.adicionar_historico("user", f"[{'Confirmei' if decisao == 'ok' else 'Cancelei'}] "
                                               f"{acao['resumo'].splitlines()[0]}")
        self.banco.adicionar_historico("assistant", resultado)
        await q.edit_message_text(f"{acao['resumo']}\n\n{resultado}"[:LIMITE_TG])

    async def c_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "Oi, Matheus! Pode falar comigo normalmente. Exemplos:\n"
            "• o que tenho amanhã?\n• tem algo do Leandro sobre o FIDC?\n"
            "• responde o último e-mail da Vert dizendo que vejo amanhã\n"
            "• marca 30 min com o Marcelo quinta à tarde\n\n"
            "Comandos: /briefing /pendencias /fimdodia /agenda /limpar /login")

    async def c_login(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Login no Microsoft 365 pelo próprio Telegram (device code) — não precisa de terminal no servidor."""
        auth = self.graph.auth
        try:
            fluxo = await asyncio.to_thread(auth.iniciar_login)
        except ErroGraph as e:
            await update.message.reply_text(f"⚠️ {e}")
            return
        await update.message.reply_text(
            "🔐 Login no Microsoft 365\n"
            f"1. Abra: {fluxo['verification_uri']}\n"
            f"2. Digite o código: {fluxo['user_code']}\n"
            "3. Entre com sua conta @larcacapital.com.br e aceite as permissões.\n\n"
            "O código vale 15 minutos. Eu aviso aqui quando concluir.")

        async def aguardar() -> None:
            try:
                await asyncio.to_thread(auth.concluir_login, fluxo)
                self.graph._eu = None
                eu = await self.graph.eu()
                msg = f"✅ Conectado como {eu.get('displayName')} ({eu.get('mail')}). Pode começar!"
            except Exception as e:
                msg = f"⚠️ Login não concluído: {e}"
            await ctx.bot.send_message(self.cfg.chat_id_autorizado, msg)
        ctx.application.create_task(aguardar())

    async def c_meuid(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(f"Seu chat id: {update.effective_chat.id}")

    async def c_briefing(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._rodar(ctx.application, "briefing", self.rotinas.briefing)

    async def c_pendencias(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._rodar(ctx.application, "pendências", self.rotinas.pendencias)

    async def c_fimdodia(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._rodar(ctx.application, "fim do dia", self.rotinas.fim_do_dia)

    async def c_agenda(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._rodar(ctx.application, "agenda",
                          lambda: self.agente.conversar("Qual é a minha agenda de hoje e de amanhã?"))

    async def c_limpar(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        self.banco.limpar_historico()
        await update.message.reply_text("Histórico da conversa apagado. 🧹")

    # ------------------------------------------------------------ jobs agendados
    async def j_briefing(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._rodar(ctx.application, "briefing", self.rotinas.briefing)

    async def j_pendencias(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._rodar(ctx.application, "pendências", self.rotinas.pendencias)

    async def j_fim_dia(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._rodar(ctx.application, "fim do dia", self.rotinas.fim_do_dia)

    async def j_reunioes(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            proximas = await self.rotinas.reunioes_proximas()
        except Exception:
            log.exception("Falha ao verificar reuniões")
            return
        for ev in proximas:
            await self._rodar(ctx.application, "preparo de reunião",
                              lambda ev=ev: self.rotinas.preparar_reuniao(ev))

    # ------------------------------------------------------------ montagem
    def construir(self) -> Application:
        app = Application.builder().token(self.cfg.telegram_token).build()
        dono = filters.Chat(chat_id=self.cfg.chat_id_autorizado)

        app.add_handler(CommandHandler("meuid", self.c_meuid))  # livre: ajuda a descobrir o chat id
        app.add_handler(CommandHandler("start", self.c_start, filters=dono))
        app.add_handler(CommandHandler("login", self.c_login, filters=dono))
        app.add_handler(CommandHandler("ajuda", self.c_start, filters=dono))
        app.add_handler(CommandHandler("briefing", self.c_briefing, filters=dono))
        app.add_handler(CommandHandler("pendencias", self.c_pendencias, filters=dono))
        app.add_handler(CommandHandler("fimdodia", self.c_fimdodia, filters=dono))
        app.add_handler(CommandHandler("agenda", self.c_agenda, filters=dono))
        app.add_handler(CommandHandler("limpar", self.c_limpar, filters=dono))
        app.add_handler(MessageHandler(dono & filters.TEXT & ~filters.COMMAND, self.h_texto))
        app.add_handler(MessageHandler(dono & (filters.VOICE | filters.AUDIO), self.h_audio))
        app.add_handler(CallbackQueryHandler(self.h_botao, pattern=r"^(ok|no):"))
        # qualquer outra pessoa é ignorada em silêncio

        jq, fz = app.job_queue, self.cfg.fuso
        h = lambda hm: time(hm[0], hm[1], tzinfo=fz)  # noqa: E731
        jq.run_daily(self.j_briefing, h(self.cfg.hora_briefing), days=DIAS_UTEIS, name="briefing")
        jq.run_daily(self.j_pendencias, h(self.cfg.hora_pendencias), days=DIAS_UTEIS, name="pendencias")
        jq.run_daily(self.j_fim_dia, h(self.cfg.hora_fim_dia), days=DIAS_UTEIS, name="fim_dia")
        jq.run_repeating(self.j_reunioes, interval=timedelta(minutes=5), first=20, name="reunioes")

        async def ao_iniciar(_app: Application) -> None:
            eu = await _app.bot.get_me()
            log.info("Bot @%s no ar. Chat autorizado: %s", eu.username, self.cfg.chat_id_autorizado)
            msg = ("Estou no ar. ✅ Outlook conectado." if self.graph.auth.conectado()
                   else "Estou no ar, mas ainda sem acesso ao seu Outlook. Mande /login.")
            try:
                await _app.bot.send_message(self.cfg.chat_id_autorizado, msg)
            except Exception as e:  # não derruba o bot: só avisa no log
                log.warning("Não consegui mandar mensagem para o chat %s (%s). Abra o bot @%s no Telegram, "
                            "toque em Iniciar/Start e confira se TELEGRAM_CHAT_ID é o seu Id do @userinfobot.",
                            self.cfg.chat_id_autorizado, e, eu.username)
        app.post_init = ao_iniciar

        async def ao_parar(_app: Application) -> None:
            await self.graph.fechar()
        app.post_shutdown = ao_parar
        return app


def iniciar() -> None:
    """Ponto de entrada do container (não depende do __main__.py)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.info("Iniciando assistente…")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # não logar URLs com token do bot
    cfg = Config()
    faltando = [n for n, v in [("TELEGRAM_TOKEN", cfg.telegram_token), ("ANTHROPIC_API_KEY", cfg.anthropic_api_key),
                               ("MS_CLIENT_ID", cfg.ms_client_id), ("TELEGRAM_CHAT_ID", cfg.chat_id_autorizado)] if not v]
    if faltando:
        sys.exit(f"Configure no .env: {', '.join(faltando)}")
    try:
        bot = Bot(cfg)
    except Exception as e:  # MSAL valida tenant/client na inicialização
        sys.exit(f"Erro ao preparar o acesso ao Microsoft 365. Confira MS_TENANT_ID e MS_CLIENT_ID. Detalhe: {e}")
    try:
        bot.construir().run_polling(allowed_updates=["message", "callback_query"])
    except InvalidToken:
        sys.exit("TELEGRAM_TOKEN inválido. Copie de novo o token do @BotFather (formato 123456:ABC...).")
