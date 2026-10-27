"""Bot do Telegram: interface de conversa, botões de confirmação e agendamento das rotinas."""
from __future__ import annotations

import asyncio
import logging
import sys
import time as _time
from datetime import time, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import InvalidToken
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
                          filters)

from .agente import Agente, Resposta, cancelar_acao, executar_acao
from .armazenamento import Armazenamento
from .config import Config
from .gestao import ROTINAS, Gestor
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
        self.gestor = Gestor(cfg, self.banco)
        self.graph = Graph(Autenticador(cfg.ms_client_id, cfg.ms_tenant_id, cfg.arquivo_token), cfg.fuso.key)
        self.graph.ao_status = lambda ok, erro=None: self.gestor.status("microsoft365", ok, erro)
        self.agente = Agente(cfg, self.graph, self.banco, gestor=self.gestor)
        self.rotinas = Rotinas(self.agente)
        self.transcritor = Transcritor(cfg.openai_api_key, cfg.modelo_transcricao, cfg.contatos_prioritarios)
        self.trava = asyncio.Lock()  # uma conversa por vez, para manter o histórico coerente
        self.app: Application | None = None
        self.login_m365: dict | None = None  # login em andamento (mostrado no painel)
        self.painel = None

    # ------------------------------------------------------------ envio
    async def avisar(self, texto: str) -> None:
        """Mensagem simples ao Matheus, registrando o status do Telegram."""
        if not self.app:
            return
        try:
            await self.app.bot.send_message(self.cfg.chat_id_autorizado, texto, disable_web_page_preview=True)
            self.gestor.status("telegram", True)
        except Exception as e:
            self.gestor.status("telegram", False, str(e))
            log.warning("Falha ao enviar mensagem no Telegram: %s", e)

    async def enviar(self, app: Application, resp: Resposta) -> None:
        cid = self.cfg.chat_id_autorizado
        for p in _partes(resp.texto):
            await app.bot.send_message(cid, p, disable_web_page_preview=True)
        for a in resp.acoes:
            teclado = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Confirmar", callback_data=f"ok:{a['id']}"),
                InlineKeyboardButton("❌ Cancelar", callback_data=f"no:{a['id']}")]])
            await app.bot.send_message(cid, _partes(a["resumo"])[0], reply_markup=teclado)

    async def _rodar(self, app: Application, nome: str, coro_fn, automatica: bool = False) -> None:
        if automatica and self.gestor.rotinas_bloqueadas():
            log.info("Rotina '%s' pulada: limite mensal de gasto atingido", nome)
            return
        async with self.trava:
            try:
                await app.bot.send_chat_action(self.cfg.chat_id_autorizado, ChatAction.TYPING)
                await self.enviar(app, await coro_fn())
                self.gestor.status("telegram", True)
            except ErroGraph as e:
                await self.avisar(f"⚠️ Outlook: {e}")
            except Exception as e:
                log.exception("Falha em %s", nome)
                await self.avisar(f"⚠️ Falha em {nome}: {e}")
        for alerta in self.gestor.alertas_de_limite():
            await self.avisar(alerta)

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
            if "configurada" not in str(e) and "grande" not in str(e) and "entender" not in str(e):
                self.gestor.status("openai", False, str(e))
            await msg.reply_text(f"⚠️ {e}")
            return
        except Exception as e:
            self.gestor.status("openai", False, str(e))
            log.exception("Falha ao transcrever")
            await msg.reply_text(f"⚠️ Falha ao transcrever o áudio: {e}")
            return
        self.gestor.status("openai", True)
        self.gestor.registrar_audio(self.cfg.modelo_transcricao, float(getattr(midia, "duration", 0) or 0))
        await msg.reply_text(f"🎙️ Entendi: “{texto}”")
        await self._rodar(ctx.application, "conversa", lambda: self.agente.conversar(texto, origem="voz"))

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

    async def iniciar_login_m365(self) -> dict:
        """Gera o código de login do Microsoft 365 e conclui em segundo plano (usado pelo /login e pelo painel)."""
        auth = self.graph.auth
        fluxo = await asyncio.to_thread(auth.iniciar_login)
        self.login_m365 = {"url": fluxo["verification_uri"], "codigo": fluxo["user_code"],
                           "expira": _time.time() + int(fluxo.get("expires_in", 900)), "status": "aguardando"}
        estado = self.login_m365

        async def aguardar() -> None:
            try:
                await asyncio.to_thread(auth.concluir_login, fluxo)
                self.graph._eu = None
                eu = await self.graph.eu()
                estado["status"] = "concluido"
                await self.avisar(f"✅ Microsoft 365 conectado como {eu.get('displayName')} ({eu.get('mail')}).")
            except Exception as e:
                estado["status"] = f"falhou: {e}"
                self.gestor.status("microsoft365", False, f"Login não concluído: {e}")
                await self.avisar(f"⚠️ Login do Microsoft 365 não concluído: {e}")
        if self.app:
            self.app.create_task(aguardar())
        else:
            asyncio.get_running_loop().create_task(aguardar())
        return self.login_m365

    async def c_login(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Login no Microsoft 365 pelo próprio Telegram (device code) — não precisa de terminal no servidor."""
        try:
            lg = await self.iniciar_login_m365()
        except ErroGraph as e:
            await update.message.reply_text(f"⚠️ {e}")
            return
        await update.message.reply_text(
            "🔐 Login no Microsoft 365\n"
            f"1. Abra: {lg['url']}\n"
            f"2. Digite o código: {lg['codigo']}\n"
            "3. Entre com sua conta @larcacapital.com.br e aceite as permissões.\n\n"
            "O código vale 15 minutos. Eu aviso aqui quando concluir.")

    async def testar_integracoes(self) -> dict[str, tuple[bool, str]]:
        """Teste ativo de cada integração (botão 'Testar agora' do painel)."""
        res: dict[str, tuple[bool, str]] = {}
        try:
            eu = await self.app.bot.get_me()
            res["telegram"] = (True, f"@{eu.username}")
        except Exception as e:
            res["telegram"] = (False, str(e))
        try:
            self.graph._eu = None
            eu = await self.graph.eu()
            res["microsoft365"] = (True, eu.get("mail") or eu.get("userPrincipalName") or "")
        except Exception as e:
            res["microsoft365"] = (False, str(e))
        try:
            await self.agente.claude.models.list(limit=1)
            res["claude"] = (True, self.cfg.modelo)
        except Exception as e:
            res["claude"] = (False, str(e))
        if self.transcritor.ativo:
            ok, det = await self.transcritor.testar()
            res["openai"] = (ok, det)
        else:
            res["openai"] = (False, "OPENAI_API_KEY não configurada")
        for nome, (ok, det) in res.items():
            self.gestor.status(nome, ok, None if ok else det)
        return res

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
                          lambda: self.agente.conversar("Qual é a minha agenda de hoje e de amanhã?",
                                                       origem="agenda"))

    async def c_limpar(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        self.banco.limpar_historico()
        await update.message.reply_text("Histórico da conversa apagado. 🧹")

    # ------------------------------------------------------------ jobs agendados
    async def j_briefing(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._rodar(ctx.application, "briefing", self.rotinas.briefing, automatica=True)

    async def j_pendencias(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._rodar(ctx.application, "pendências", self.rotinas.pendencias, automatica=True)

    async def j_fim_dia(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._rodar(ctx.application, "fim do dia", self.rotinas.fim_do_dia, automatica=True)

    async def j_reunioes(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.gestor.reunioes()[0] or self.gestor.rotinas_bloqueadas() or not self.graph.auth.conectado():
            return
        try:
            proximas = await self.rotinas.reunioes_proximas()
        except Exception:
            log.exception("Falha ao verificar reuniões")
            return
        for ev in proximas:
            await self._rodar(ctx.application, "preparo de reunião",
                              lambda ev=ev: self.rotinas.preparar_reuniao(ev), automatica=True)

    def agendar_rotinas(self) -> None:
        """(Re)agenda as rotinas diárias conforme os ajustes do painel. Pode ser chamado a qualquer momento."""
        jq = self.app.job_queue
        funcoes = {"briefing": self.j_briefing, "pendencias": self.j_pendencias, "fim_dia": self.j_fim_dia}
        for chave in ROTINAS:
            for job in jq.get_jobs_by_name(chave):
                job.schedule_removal()
            ativa, (hh, mm) = self.gestor.rotina(chave)
            if ativa:
                jq.run_daily(funcoes[chave], time(hh, mm, tzinfo=self.cfg.fuso), days=DIAS_UTEIS, name=chave)

    # ------------------------------------------------------------ montagem
    def construir(self) -> Application:
        app = Application.builder().token(self.cfg.telegram_token).build()
        self.app = app
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

        self.agendar_rotinas()
        app.job_queue.run_repeating(self.j_reunioes, interval=timedelta(minutes=5), first=20, name="reunioes")

        async def ao_iniciar(_app: Application) -> None:
            eu = await _app.bot.get_me()
            log.info("Bot @%s no ar. Chat autorizado: %s", eu.username, self.cfg.chat_id_autorizado)
            msg = ("Estou no ar. ✅ Outlook conectado." if self.graph.auth.conectado()
                   else "Estou no ar, mas ainda sem acesso ao seu Outlook. Mande /login.")
            if self.cfg.painel_senha:
                from .painel import Painel
                self.painel = Painel(self)
                await self.painel.iniciar()
            try:
                await _app.bot.send_message(self.cfg.chat_id_autorizado, msg)
                self.gestor.status("telegram", True)
            except Exception as e:  # não derruba o bot: só avisa no log
                self.gestor.status("telegram", False, str(e))
                log.warning("Não consegui mandar mensagem para o chat %s (%s). Abra o bot @%s no Telegram, "
                            "toque em Iniciar/Start e confira se TELEGRAM_CHAT_ID é o seu Id do @userinfobot.",
                            self.cfg.chat_id_autorizado, e, eu.username)
        app.post_init = ao_iniciar

        async def ao_parar(_app: Application) -> None:
            if self.painel:
                await self.painel.parar()
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
