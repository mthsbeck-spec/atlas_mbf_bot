"""Painel web (aiohttp) para acompanhar custos, integrações e ajustar rotinas/limites.

Roda no mesmo processo do bot. Só sobe se PAINEL_SENHA estiver definida.
Acesso: https://<domínio-do-railway>/painel
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import time
from datetime import date, datetime, timedelta
from html import escape
from typing import TYPE_CHECKING
from urllib.parse import quote

from aiohttp import web

from .gestao import INTEGRACOES, ORIGENS, ROTINAS

if TYPE_CHECKING:
    from .bot import Bot

log = logging.getLogger(__name__)
COOKIE = "atlas_sessao"
VALIDADE_SESSAO = 7 * 86400
MAX_FALHAS, JANELA_BLOQUEIO = 5, 15 * 60


# ---------------------------------------------------------------- formatação
def _num(v: float, casas: int = 2) -> str:
    s = f"{v:,.{casas}f}"
    return s.replace(",", "§").replace(".", ",").replace("§", ".")


def usd(v: float) -> str:
    return f"US$ {_num(v)}"


def brl(v: float) -> str:
    return f"R$ {_num(v)}"


def _tokens(n: int) -> str:
    return f"{n / 1_000_000:.2f} mi".replace(".", ",") if n >= 1_000_000 else f"{n / 1000:.0f} mil"


class Painel:
    def __init__(self, bot: "Bot"):
        self.bot = bot
        self.cfg, self.gestor, self.banco = bot.cfg, bot.gestor, bot.banco
        self.falhas: dict[str, list[float]] = {}
        segredo = self.banco.ajuste("painel_segredo")
        if not segredo:
            segredo = secrets.token_hex(32)
            self.banco.salvar_ajuste("painel_segredo", segredo)
        # trocar a senha invalida todas as sessões
        self.chave = hashlib.sha256((segredo + self.cfg.painel_senha).encode()).digest()
        self.runner: web.AppRunner | None = None

    # ------------------------------------------------------------ servidor
    def criar_app(self) -> web.Application:
        app = web.Application(middlewares=[self._seguranca])
        app.add_routes([
            web.get("/saude", self.saude),
            web.get("/", self.raiz),
            web.get("/painel", self.pagina),
            web.post("/painel/entrar", self.entrar),
            web.post("/painel/sair", self.sair),
            web.post("/painel/rotinas", self.salvar_rotinas),
            web.post("/painel/financeiro", self.salvar_financeiro),
            web.post("/painel/testar", self.testar),
            web.post("/painel/login-m365", self.login_m365),
            web.post("/painel/rodar/{rotina}", self.rodar_rotina),
        ])
        return app

    async def iniciar(self) -> None:
        self.runner = web.AppRunner(self.criar_app(), access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "0.0.0.0", self.cfg.porta).start()
        log.info("Painel no ar na porta %s (/painel)", self.cfg.porta)

    async def parar(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    # ------------------------------------------------------------ segurança
    @web.middleware
    async def _seguranca(self, request: web.Request, handler):
        if request.method == "POST":
            origem = request.headers.get("Origin")
            if origem and origem.split("://", 1)[-1] != request.headers.get("Host", request.host):
                raise web.HTTPForbidden(text="Origem inválida")
        resp = await handler(request)
        resp.headers.update({
            "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                                       "img-src data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        })
        return resp

    def _assinar(self, exp: int) -> str:
        return hmac.new(self.chave, str(exp).encode(), hashlib.sha256).hexdigest()

    def _autenticado(self, request: web.Request) -> bool:
        token = request.cookies.get(COOKIE, "")
        try:
            exp_s, assinatura = token.split(".", 1)
            exp = int(exp_s)
        except ValueError:
            return False
        return exp > time.time() and hmac.compare_digest(assinatura, self._assinar(exp))

    @staticmethod
    def _ip(request: web.Request) -> str:
        return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote or "?")

    def _bloqueado(self, ip: str) -> bool:
        agora = time.time()
        self.falhas[ip] = [t for t in self.falhas.get(ip, []) if agora - t < JANELA_BLOQUEIO]
        return len(self.falhas[ip]) >= MAX_FALHAS

    def _exigir_login(self, request: web.Request) -> None:
        if not self._autenticado(request):
            raise web.HTTPFound("/painel")

    @staticmethod
    def _voltar(msg: str, ancora: str = "") -> web.HTTPFound:
        return web.HTTPFound(f"/painel?msg={quote(msg)}{'#' + ancora if ancora else ''}")

    # ------------------------------------------------------------ rotas
    async def raiz(self, request: web.Request) -> web.Response:
        raise web.HTTPFound("/painel")

    async def saude(self, request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def entrar(self, request: web.Request) -> web.Response:
        ip = self._ip(request)
        if self._bloqueado(ip):
            return self._html(self._tela_login("Muitas tentativas. Aguarde 15 minutos."), status=429)
        dados = await request.post()
        senha = str(dados.get("senha", ""))
        if not hmac.compare_digest(senha.encode(), self.cfg.painel_senha.encode()):
            self.falhas.setdefault(ip, []).append(time.time())
            log.warning("Senha incorreta no painel (ip %s)", ip)
            await asyncio.sleep(1)
            return self._html(self._tela_login("Senha incorreta."), status=401)
        self.falhas.pop(ip, None)
        exp = int(time.time()) + VALIDADE_SESSAO
        resp = web.HTTPFound("/painel")
        https = request.headers.get("X-Forwarded-Proto", request.scheme) == "https"
        resp.set_cookie(COOKIE, f"{exp}.{self._assinar(exp)}", max_age=VALIDADE_SESSAO, httponly=True,
                        secure=https, samesite="Strict", path="/")
        raise resp

    async def sair(self, request: web.Request) -> web.Response:
        resp = web.HTTPFound("/painel")
        resp.del_cookie(COOKIE, path="/")
        raise resp

    async def pagina(self, request: web.Request) -> web.Response:
        if not self._autenticado(request):
            return self._html(self._tela_login())
        return self._html(self._tela_painel(request.query.get("msg", "")))

    async def salvar_rotinas(self, request: web.Request) -> web.Response:
        self._exigir_login(request)
        d = await request.post()
        try:
            horas = {}
            for chave, (rotulo, _) in ROTINAS.items():  # valida tudo antes de salvar
                hh, mm = str(d.get(f"{chave}_hora", "")).split(":")
                if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                    raise ValueError(f"Horário inválido em {rotulo}")
                horas[chave] = f"{int(hh):02d}:{int(mm):02d}"
            minutos = int(d.get("reuniao_minutos", "15"))
            self.gestor.salvar_reunioes(d.get("reuniao_ativa") == "on", minutos)
            for chave, hora in horas.items():
                self.gestor.salvar_rotina(chave, d.get(f"{chave}_ativa") == "on", hora)
        except (ValueError, TypeError) as e:
            raise self._voltar(f"⚠️ {e}", "rotinas")
        self.bot.agendar_rotinas()
        raise self._voltar("✅ Rotinas atualizadas.", "rotinas")

    async def salvar_financeiro(self, request: web.Request) -> web.Response:
        self._exigir_login(request)
        d = await request.post()

        def n(campo: str) -> float:
            return float(str(d.get(campo, "0")).replace(",", ".") or 0)
        try:
            self.gestor.salvar_financeiro(limite=n("limite"), cotacao=n("cotacao"), custo_fixo=n("custo_fixo"),
                                          preco_entrada=n("preco_entrada"), preco_saida=n("preco_saida"),
                                          preco_audio=n("preco_audio"))
        except ValueError as e:
            raise self._voltar(f"⚠️ {e}", "financeiro")
        raise self._voltar("✅ Limite e preços salvos.", "financeiro")

    async def testar(self, request: web.Request) -> web.Response:
        self._exigir_login(request)
        res = await self.bot.testar_integracoes()
        falhas = [INTEGRACOES[n] for n, (ok, _) in res.items() if not ok]
        raise self._voltar("✅ Todas as integrações responderam." if not falhas
                           else "⚠️ Com problema: " + ", ".join(falhas), "integracoes")

    async def login_m365(self, request: web.Request) -> web.Response:
        self._exigir_login(request)
        try:
            await self.bot.iniciar_login_m365()
        except Exception as e:
            raise self._voltar(f"⚠️ Não consegui iniciar o login: {e}", "integracoes")
        raise self._voltar("Código de login gerado — siga as instruções abaixo.", "integracoes")

    async def rodar_rotina(self, request: web.Request) -> web.Response:
        self._exigir_login(request)
        r = self.bot.rotinas
        funcoes = {"briefing": ("briefing", r.briefing), "pendencias": ("pendências", r.pendencias),
                   "fim_dia": ("fim do dia", r.fim_do_dia)}
        chave = request.match_info["rotina"]
        if chave not in funcoes:
            raise web.HTTPNotFound()
        nome, fn = funcoes[chave]
        self.bot.app.create_task(self.bot._rodar(self.bot.app, nome, fn))
        raise self._voltar(f"▶️ {ROTINAS[chave][0]} em execução — chega no Telegram em instantes.", "rotinas")

    # ------------------------------------------------------------ HTML
    @staticmethod
    def _html(corpo: str, status: int = 200) -> web.Response:
        return web.Response(text=corpo, status=status, content_type="text/html", charset="utf-8")

    def _tela_login(self, erro: str = "") -> str:
        aviso = f'<p class="erro" role="alert">{escape(erro)}</p>' if erro else ""
        return _pagina("Atlas · Entrar", f"""
<main class="login">
  <h1>Atlas</h1>
  <p class="sub">Painel do assistente pessoal</p>
  {aviso}
  <form method="post" action="/painel/entrar">
    <label for="senha">Senha</label>
    <input id="senha" name="senha" type="password" autocomplete="current-password" required autofocus>
    <button type="submit">Entrar</button>
  </form>
</main>""")

    def _tela_painel(self, msg: str) -> str:
        g = self.gestor
        hoje = g.hoje()
        ini_mes, fim_mes = g.mes_atual()
        situacao = g.situacao_limite()
        cot = g.cotacao()
        gasto_hoje = self.banco.custo_periodo(hoje.isoformat(), hoje.isoformat())
        dias_corridos = hoje.day
        dias_mes = int(fim_mes[-2:])
        projecao = situacao["gasto"] / dias_corridos * dias_mes if dias_corridos else 0
        ini30 = hoje - timedelta(days=29)
        por_dia = self.banco.custo_por_dia(ini30.isoformat(), hoje.isoformat())
        serie = [((ini30 + timedelta(days=i)), por_dia.get((ini30 + timedelta(days=i)).isoformat(), 0.0))
                 for i in range(30)]
        origens = self.banco.uso_agrupado(ini_mes, fim_mes, "origem")
        provedores = self.banco.uso_agrupado(ini_mes, fim_mes, "provedor")

        flash = f'<div class="flash" role="status">{escape(msg)}</div>' if msg else ""
        return _pagina("Atlas · Painel", f"""
<header class="topo">
  <div><h1>Atlas</h1><p class="sub">Painel do assistente · {hoje:%d/%m/%Y}</p></div>
  <form method="post" action="/painel/sair"><button class="sec" type="submit">Sair</button></form>
</header>
{flash}
<nav class="abas">
  <a href="#custos">Custos</a><a href="#integracoes">Integrações</a>
  <a href="#rotinas">Rotinas</a><a href="#financeiro">Limite e preços</a>
</nav>

<section id="custos">
  <h2>Custos de IA</h2>
  <div class="tiles">
    {_tile("Gasto no mês", usd(situacao['gasto']), brl(situacao['gasto'] * cot))}
    {_tile("Hoje", usd(gasto_hoje), brl(gasto_hoje * cot))}
    {_tile("Projeção do mês", usd(projecao), f"no ritmo atual · {dias_corridos} de {dias_mes} dias")}
    {self._tile_limite(situacao)}
  </div>
  <div class="cartao">
    <h3>Gasto por dia · <span class="so-desk">últimos 30 dias</span><span class="so-cel">últimos 14 dias</span></h3>
    {_grafico_barras(serie, classe="so-desk")}
    {_grafico_barras(serie[-14:], L=360, A=200, rotulo_cada=4, classe="so-cel")}
    <p class="nota">Valores em US$ por dia. Toque ou passe o mouse sobre uma barra para ver o valor.</p>
    <details><summary>Ver tabela</summary>{_tabela_dias(serie, cot)}</details>
  </div>
  <div class="duas">
    <div class="cartao">
      <h3>Por tipo de uso · mês atual</h3>
      {_barras_horizontais(origens)}
    </div>
    <div class="cartao">
      <h3>Por serviço · mês atual</h3>
      {self._tabela_provedores(provedores, cot)}
    </div>
  </div>
  <p class="nota">Valores estimados a partir dos tokens e minutos de áudio registrados pelo bot, com os preços
  configurados em <a href="#financeiro">Limite e preços</a>. A fatura oficial está em console.anthropic.com e
  platform.openai.com.</p>
</section>

<section id="integracoes">
  <h2>Integrações</h2>
  {self._login_pendente()}
  <div class="integ">{self._cards_integracoes()}</div>
  <div class="acoes">
    <form method="post" action="/painel/testar"><button type="submit">Testar agora</button></form>
    <form method="post" action="/painel/login-m365"><button class="sec" type="submit">Refazer login do Microsoft 365</button></form>
  </div>
</section>

<section id="rotinas">
  <h2>Rotinas automáticas</h2>
  {self._form_rotinas()}
</section>

<section id="financeiro">
  <h2>Limite e preços</h2>
  {self._form_financeiro()}
</section>
<script>{_JS}</script>""")

    def _tile_limite(self, s: dict) -> str:
        if s["nivel"] == "sem_limite":
            return _tile("Limite mensal", "Sem limite", '<a href="#financeiro">Definir limite</a>')
        rotulo = {"ok": ("✓", "Dentro do limite", "bom"), "alerta": ("!", "Acima de 80%", "alerta"),
                  "estourado": ("✕", "Limite atingido · rotinas pausadas", "critico")}[s["nivel"]]
        pct = min(s["pct"], 100)
        return f"""<div class="tile">
  <p class="tl">Limite mensal</p>
  <p class="tv">{_num(s['pct'], 0)}%</p>
  <div class="medidor" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{pct:.0f}"
       aria-label="Uso do limite mensal"><span class="{rotulo[2]}" style="width:{pct:.1f}%"></span></div>
  <p class="ts"><span class="status {rotulo[2]}">{rotulo[0]} {rotulo[1]}</span> · {usd(s['limite'])}</p>
</div>"""

    def _tabela_provedores(self, prov: list[dict], cot: float) -> str:
        nomes = {"claude": "Claude (Anthropic)", "openai": "Transcrição (OpenAI)"}
        linhas = "".join(
            f"<tr><td>{escape(nomes.get(p['nome'], p['nome']))}</td><td class='n'>{p['chamadas']}</td>"
            f"<td class='n'>{usd(p['custo'])}</td><td class='n'>{brl(p['custo'] * cot)}</td></tr>" for p in prov)
        fixo = self.gestor.custo_fixo()
        linhas += (f"<tr class='fixo'><td>Railway (custo fixo informado)</td><td class='n'>—</td>"
                   f"<td class='n'>{usd(fixo)}</td><td class='n'>{brl(fixo * cot)}</td></tr>")
        total = sum(p["custo"] for p in prov) + fixo
        return (f"<table><thead><tr><th>Serviço</th><th class='n'>Chamadas</th><th class='n'>US$</th>"
                f"<th class='n'>R$</th></tr></thead><tbody>{linhas}</tbody><tfoot><tr><td>Total do mês</td>"
                f"<td></td><td class='n'>{usd(total)}</td><td class='n'>{brl(total * cot)}</td></tr></tfoot></table>"
                f"<p class='nota'>O limite mensal considera só IA (Claude + OpenAI), não o Railway.</p>")

    def _login_pendente(self) -> str:
        lg = self.bot.login_m365
        if not lg or lg["status"] != "aguardando" or lg["expira"] < time.time():
            return ""
        return f"""<div class="codigo" role="status">
  <p><strong>Login do Microsoft 365 em andamento</strong></p>
  <p>1. Abra <a href="{escape(lg['url'])}" target="_blank" rel="noopener">{escape(lg['url'])}</a></p>
  <p>2. Digite o código <code>{escape(lg['codigo'])}</code></p>
  <p>3. Entre com a conta @larcacapital.com.br. Você recebe a confirmação no Telegram.</p>
</div>"""

    def _cards_integracoes(self) -> str:
        st = self.banco.status_integracoes()
        detalhes = {
            "telegram": f"Chat autorizado: {self.cfg.chat_id_autorizado}",
            "microsoft365": self._conta_m365(),
            "claude": f"Modelo: {self.cfg.modelo}",
            "openai": (f"Modelo: {self.cfg.modelo_transcricao}" if self.bot.transcritor.ativo
                       else "OPENAI_API_KEY não configurada"),
        }
        cards = []
        for chave, nome in INTEGRACOES.items():
            s = st.get(chave, {})
            ok, er = s.get("ultimo_ok") or 0, s.get("ultimo_erro") or 0
            if chave == "openai" and not self.bot.transcritor.ativo:
                classe, rotulo = "neutro", "— Não configurado"
            elif chave == "microsoft365" and not self.bot.graph.auth.conectado():
                classe, rotulo = "critico", "✕ Sem login"
            elif er > ok:
                classe, rotulo = "critico", "✕ Com erro"
            elif ok:
                classe, rotulo = "bom", "✓ Funcionando"
            else:
                classe, rotulo = "neutro", "— Sem uso ainda"
            linhas = [f"<p class='ts'>{escape(detalhes[chave])}</p>"]
            if ok:
                linhas.append(f"<p class='ts'>Último sucesso: {self._quando(ok)}</p>")
            if er and er >= ok:
                linhas.append(f"<p class='ts erro-txt'>Último erro ({self._quando(er)}): "
                              f"{escape((s.get('erro') or '')[:240])}</p>")
            cards.append(f"<div class='cartao'><div class='lin'><h3>{escape(nome)}</h3>"
                         f"<span class='status {classe}'>{rotulo}</span></div>{''.join(linhas)}</div>")
        return "".join(cards)

    def _conta_m365(self) -> str:
        try:
            contas = self.bot.graph.auth.app.get_accounts()
        except Exception:
            contas = []
        return f"Conta: {contas[0].get('username')}" if contas else "Nenhuma conta conectada"

    def _quando(self, ts: float) -> str:
        d = datetime.fromtimestamp(ts, self.cfg.fuso)
        seg = time.time() - ts
        rel = ("agora" if seg < 90 else f"há {seg / 60:.0f} min" if seg < 3600
               else f"há {seg / 3600:.0f} h" if seg < 86400 else f"há {seg / 86400:.0f} dias")
        return f"{d:%d/%m %H:%M} ({rel})"

    def _form_rotinas(self) -> str:
        g = self.gestor
        linhas = []
        for chave, (rotulo, _) in ROTINAS.items():
            ativa, (hh, mm) = g.rotina(chave)
            linhas.append(f"""<tr>
  <td><label class="chk"><input type="checkbox" name="{chave}_ativa" {'checked' if ativa else ''}> {rotulo}</label></td>
  <td><input type="time" name="{chave}_hora" value="{hh:02d}:{mm:02d}" aria-label="Horário — {rotulo}" required></td>
  <td class="n"><button class="sec peq" type="submit" form="rodar_{chave}">Rodar agora</button></td>
</tr>""")
        r_ativa, r_min = g.reunioes()
        aviso = ("<p class='status critico'>✕ Limite mensal atingido: as rotinas estão pausadas até o próximo mês "
                 "ou até você aumentar o limite.</p>" if g.rotinas_bloqueadas() else "")
        forms_rodar = "".join(f'<form id="rodar_{c}" method="post" action="/painel/rodar/{c}"></form>' for c in ROTINAS)
        return f"""{aviso}
<form method="post" action="/painel/rotinas" class="cartao">
  <table class="form">
    <thead><tr><th>Rotina (dias úteis)</th><th>Horário</th><th></th></tr></thead>
    <tbody>{''.join(linhas)}
    <tr>
      <td><label class="chk"><input type="checkbox" name="reuniao_ativa" {'checked' if r_ativa else ''}> Preparo de reuniões</label></td>
      <td><input type="number" name="reuniao_minutos" min="5" max="120" value="{r_min}" aria-label="Minutos antes"> min antes</td>
      <td></td>
    </tr></tbody>
  </table>
  <button type="submit">Salvar rotinas</button>
</form>{forms_rodar}"""

    def _form_financeiro(self) -> str:
        g = self.gestor
        p = g.precos()

        def campo(nome: str, rotulo: str, valor: float, ajuda: str, passo: str = "0.01") -> str:
            return (f'<label>{rotulo}<input name="{nome}" type="number" min="0" step="{passo}" '
                    f'value="{valor:g}" inputmode="decimal"><span class="ajuda">{ajuda}</span></label>')
        return f"""<form method="post" action="/painel/financeiro" class="cartao gridform">
  {campo("limite", "Limite mensal de IA (US$)", g.limite_mensal(), "0 = sem limite. Aviso em 80%; em 100% pausa as rotinas automáticas.")}
  {campo("cotacao", "Cotação US$ → R$", g.cotacao(), "Usada só para exibir valores em reais. Atualize quando quiser.", "0.0001")}
  {campo("custo_fixo", "Custo fixo do Railway (US$/mês)", g.custo_fixo(), "Veja o valor real em railway.com → Usage.")}
  {campo("preco_entrada", "Claude · entrada (US$ por milhão de tokens)", p['entrada'], f"Modelo {escape(self.cfg.modelo)}. Cache: 1,25× na escrita, 0,1× na leitura.", "0.001")}
  {campo("preco_saida", "Claude · saída (US$ por milhão de tokens)", p['saida'], "Confira em anthropic.com/pricing.", "0.001")}
  {campo("preco_audio", "OpenAI · transcrição (US$ por minuto)", p['audio'], f"Modelo {escape(self.cfg.modelo_transcricao)}.", "0.0001")}
  <div class="span"><button type="submit">Salvar limite e preços</button></div>
</form>"""


# ---------------------------------------------------------------- componentes
def _tile(rotulo: str, valor: str, sub: str) -> str:
    return f'<div class="tile"><p class="tl">{rotulo}</p><p class="tv">{valor}</p><p class="ts">{sub}</p></div>'


def _escala(maximo: float) -> float:
    if maximo <= 0:
        return 0.10
    for passo in (0.01, 0.02, 0.05, 0.1, 0.2, 0.25, 0.5, 1, 2, 2.5, 5, 10, 20, 25, 50, 100):
        if maximo <= passo * 4:
            return passo * 4
    return maximo * 1.1


def _grafico_barras(serie: list[tuple[date, float]], L: int = 720, A: int = 220, rotulo_cada: int = 5,
                    classe: str = "") -> str:
    esq, base, topo = 46, 26, 10
    altura = A - base - topo
    maximo = _escala(max(v for _, v in serie))
    n = len(serie)
    passo = (L - esq) / n
    larg = max(passo - 2, 2)  # 2px de respiro entre barras
    partes = []
    for i in range(5):  # grade + eixo y
        v = maximo * i / 4
        y = topo + altura - altura * i / 4
        partes.append(f'<line class="grade" x1="{esq}" x2="{L}" y1="{y:.1f}" y2="{y:.1f}"/>'
                      f'<text class="eixo" x="{esq - 6}" y="{y + 4:.1f}" text-anchor="end">{_num(v)}</text>')
    for i, (d, v) in enumerate(serie):
        x = esq + i * passo + 1
        h = altura * v / maximo if maximo else 0
        y = topo + altura - h
        if h > 0:
            r = min(4, h, larg / 2)
            caminho = (f"M{x:.1f},{topo + altura:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
                       f"H{x + larg - r:.1f} Q{x + larg:.1f},{y:.1f} {x + larg:.1f},{y + r:.1f} V{topo + altura:.1f} Z")
            partes.append(f'<path class="barra" d="{caminho}"/>')
        dica = f"{d:%d/%m} · {usd(v)}"
        partes.append(f'<rect class="alvo" x="{esq + i * passo:.1f}" y="{topo}" width="{passo:.1f}" height="{altura}" '
                      f'data-dica="{escape(dica)}" tabindex="0" aria-label="{escape(dica)}"/>')
        if i == n - 1:
            partes.append(f'<text class="eixo" x="{x + larg:.1f}" y="{A - 8}" text-anchor="end">{d:%d/%m}</text>')
        elif i % rotulo_cada == 0 and i < n - 3:
            partes.append(f'<text class="eixo" x="{x + larg / 2:.1f}" y="{A - 8}" text-anchor="middle">{d:%d/%m}</text>')
    partes.append(f'<line class="base" x1="{esq}" x2="{L}" y1="{topo + altura}" y2="{topo + altura}"/>')
    return (f'<div class="grafico {classe}"><svg viewBox="0 0 {L} {A}" role="img" aria-label="Gasto diário em US$ '
            f'nos últimos {n} dias">{"".join(partes)}</svg><div class="dica" hidden></div></div>')


def _tabela_dias(serie: list[tuple[date, float]], cot: float) -> str:
    linhas = "".join(f"<tr><td>{d:%d/%m/%Y}</td><td class='n'>{usd(v)}</td><td class='n'>{brl(v * cot)}</td></tr>"
                     for d, v in reversed(serie))
    return f"<table><thead><tr><th>Dia</th><th class='n'>US$</th><th class='n'>R$</th></tr></thead><tbody>{linhas}</tbody></table>"


def _barras_horizontais(itens: list[dict]) -> str:
    if not itens:
        return "<p class='ts'>Nenhum uso registrado neste mês ainda.</p>"
    maximo = max(i["custo"] for i in itens) or 1
    linhas = []
    for it in itens:
        pct = it["custo"] / maximo * 100
        det = (f"{it['chamadas']} chamadas" + (f" · {_tokens(it['entrada'])} tokens de entrada"
                                                 if it["entrada"] else "")
               + (f" · {it['segundos'] / 60:.1f} min de áudio".replace(".", ",") if it["segundos"] else ""))
        linhas.append(f"""<div class="hb">
  <div class="lin"><span>{escape(ORIGENS.get(it['nome'], it['nome']))}</span><strong>{usd(it['custo'])}</strong></div>
  <div class="trilho"><span style="width:{pct:.1f}%"></span></div>
  <p class="ts">{det}</p>
</div>""")
    return "".join(linhas)


_JS = """
document.querySelectorAll('.grafico').forEach(function (g) {
  var dica = g.querySelector('.dica');
  function mostrar(alvo) {
    dica.textContent = alvo.getAttribute('data-dica');
    dica.hidden = false;
    var r = alvo.getBoundingClientRect(), c = g.getBoundingClientRect();
    var x = r.left - c.left + r.width / 2;
    dica.style.left = Math.min(Math.max(x, 60), c.width - 60) + 'px';
    g.querySelectorAll('.alvo').forEach(function (a) { a.classList.toggle('ativo', a === alvo); });
  }
  g.querySelectorAll('.alvo').forEach(function (a) {
    a.addEventListener('mouseenter', function () { mostrar(a); });
    a.addEventListener('focus', function () { mostrar(a); });
    a.addEventListener('touchstart', function () { mostrar(a); }, {passive: true});
  });
  g.addEventListener('mouseleave', function () {
    dica.hidden = true;
    g.querySelectorAll('.alvo').forEach(function (a) { a.classList.remove('ativo'); });
  });
});
"""

_CSS = """
:root{color-scheme:light;--pagina:#f9f9f7;--superficie:#fcfcfb;--tinta:#0b0b0b;--tinta2:#52514e;--muda:#898781;
--grade:#e1e0d9;--base:#c3c2b7;--borda:rgba(11,11,11,.10);--serie:#2a78d6;--serie-hover:#1c5cab;
--bom:#0ca30c;--bom-txt:#006300;--alerta:#fab219;--critico:#d03b3b;--trilho:#f0efec;--acento:#2a78d6;--btn-txt:#fff}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--pagina:#0d0d0d;
--superficie:#1a1a19;--tinta:#f0efec;--tinta2:#c3c2b7;--grade:#2c2c2a;--base:#383835;--borda:rgba(255,255,255,.10);
--serie:#3987e5;--serie-hover:#86b6ef;--bom-txt:#0ca30c;--trilho:#383835;--acento:#3987e5}}
:root[data-theme="dark"]{color-scheme:dark;--pagina:#0d0d0d;--superficie:#1a1a19;--tinta:#f0efec;--tinta2:#c3c2b7;
--grade:#2c2c2a;--base:#383835;--borda:rgba(255,255,255,.10);--serie:#3987e5;--serie-hover:#86b6ef;--bom-txt:#0ca30c;
--trilho:#383835;--acento:#3987e5}
*{box-sizing:border-box}
body{margin:0;background:var(--pagina);color:var(--tinta);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;
padding:16px;max-width:1080px;margin-inline:auto}
h1{font-size:22px;margin:0}h2{font-size:18px;margin:32px 0 12px}h3{font-size:14px;margin:0 0 8px;color:var(--tinta2);font-weight:600}
.sub,.ts,.nota{color:var(--tinta2);margin:2px 0;font-size:13px}.nota{color:var(--muda);font-size:12px;margin-top:8px}
a{color:var(--acento)}
.topo{display:flex;justify-content:space-between;align-items:center;gap:12px}
.abas{display:flex;gap:16px;flex-wrap:wrap;margin:16px 0 0;font-size:14px}
.flash{background:var(--superficie);border:1px solid var(--borda);border-left:4px solid var(--acento);padding:10px 12px;
border-radius:8px;margin-top:16px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.tile,.cartao{background:var(--superficie);border:1px solid var(--borda);border-radius:12px;padding:14px 16px}
.cartao{margin-top:12px}.tl{margin:0;color:var(--tinta2);font-size:13px}.tv{margin:4px 0;font-size:26px;font-weight:600}
.duas{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}
.medidor,.trilho{height:8px;background:var(--trilho);border-radius:4px;overflow:hidden;margin:6px 0}
.medidor span,.trilho span{display:block;height:100%;border-radius:4px}
.trilho span{background:var(--serie)}.medidor .bom{background:var(--bom)}.medidor .alerta{background:var(--alerta)}
.medidor .critico{background:var(--critico)}
.status{font-size:12px;font-weight:600;white-space:nowrap}.status.bom{color:var(--bom-txt)}.status.alerta{color:var(--tinta)}
.status.critico{color:var(--critico)}.status.neutro{color:var(--muda)}
.status.alerta::before{content:"";display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--alerta);margin-right:4px}
.lin{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.hb{margin:10px 0}.erro-txt{color:var(--critico)}
.integ{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}.integ .cartao{margin:0}
.acoes{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.codigo{background:var(--superficie);border:1px solid var(--acento);border-radius:12px;padding:12px 16px;margin-bottom:12px}
.codigo code{font-size:20px;font-weight:700;letter-spacing:2px}
.grafico{position:relative}.grafico svg{width:100%;height:auto;display:block}
.grade{stroke:var(--grade);stroke-width:1}.base{stroke:var(--base);stroke-width:1}
.eixo{fill:var(--muda);font-size:11px;font-variant-numeric:tabular-nums}
.barra{fill:var(--serie)}.alvo{fill:transparent;cursor:default;outline:none}.alvo.ativo,.alvo:focus{fill:var(--grade);fill-opacity:.35}
.dica{position:absolute;top:0;transform:translateX(-50%);background:var(--tinta);color:var(--pagina);font-size:12px;
padding:4px 8px;border-radius:6px;pointer-events:none;white-space:nowrap}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--grade)}
th{color:var(--tinta2);font-weight:600;font-size:12px}.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.so-cel{display:none}
tfoot td{font-weight:600;border-bottom:0}tr.fixo td{color:var(--tinta2)}
details{margin-top:8px}summary{cursor:pointer;color:var(--acento);font-size:13px}
button{background:var(--acento);color:var(--btn-txt);border:0;border-radius:8px;padding:9px 14px;font:inherit;
font-weight:600;cursor:pointer;min-height:40px}
button.sec{background:transparent;color:var(--tinta);border:1px solid var(--base)}button.peq{padding:5px 10px;min-height:32px;font-size:13px}
input{font:inherit;color:var(--tinta);background:var(--pagina);border:1px solid var(--base);border-radius:8px;padding:7px 9px;min-height:38px}
input[type=checkbox]{min-height:auto;width:18px;height:18px;vertical-align:middle;accent-color:var(--acento)}
.chk{display:flex;align-items:center;gap:8px}
table.form td{vertical-align:middle}table.form input[type=number]{width:80px}
form.gridform{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}
form.gridform label{display:flex;flex-direction:column;gap:4px;font-size:13px;font-weight:600}
.ajuda{font-weight:400;color:var(--muda);font-size:12px}.span{grid-column:1/-1}
.login{max-width:340px;margin:12vh auto;background:var(--superficie);border:1px solid var(--borda);border-radius:12px;padding:24px}
.login form{display:flex;flex-direction:column;gap:8px;margin-top:16px}.erro{color:var(--critico);font-weight:600}
@media (max-width:560px){.tv{font-size:22px}.so-desk{display:none}.so-cel{display:block}span.so-cel{display:inline}
.tiles{grid-template-columns:1fr 1fr}.tile{padding:12px}table.form th:nth-child(3),table.form td:nth-child(3){display:none}}
"""


def _pagina(titulo: str, corpo: str) -> str:
    return (f'<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">'
            f'<title>{escape(titulo)}</title><style>{_CSS}</style></head><body>{corpo}</body></html>')
