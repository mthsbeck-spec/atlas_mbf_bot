"""Cliente do Microsoft Graph (Outlook: e-mail e agenda) com autenticação delegada via MSAL.

A autenticação é feita uma única vez com `python -m assistente.login` (device code).
Depois disso o refresh token fica em cache no disco e é renovado automaticamente.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import msal

GRAPH = "https://graph.microsoft.com/v1.0"
ESCOPOS = ["User.Read", "Mail.ReadWrite", "Mail.Send", "Calendars.ReadWrite"]


class ErroGraph(Exception):
    pass


# ---------------------------------------------------------------- autenticação
class Autenticador:
    def __init__(self, client_id: str, tenant_id: str, arquivo_cache: Path):
        self.arquivo_cache = arquivo_cache
        self.cache = msal.SerializableTokenCache()
        if arquivo_cache.exists():
            self.cache.deserialize(arquivo_cache.read_text())
        self.app = msal.PublicClientApplication(
            client_id,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
            token_cache=self.cache,
        )

    def _salvar(self) -> None:
        if self.cache.has_state_changed:
            self.arquivo_cache.parent.mkdir(parents=True, exist_ok=True)
            self.arquivo_cache.write_text(self.cache.serialize())
            self.arquivo_cache.chmod(0o600)

    def token(self) -> str:
        contas = self.app.get_accounts()
        if not contas:
            raise ErroGraph("Sem login no Microsoft 365. Mande /login aqui no Telegram.")
        res = self.app.acquire_token_silent(ESCOPOS, account=contas[0])
        self._salvar()
        if not res or "access_token" not in res:
            raise ErroGraph("Login do Microsoft 365 expirou. Mande /login aqui no Telegram.")
        return res["access_token"]

    def conectado(self) -> bool:
        return bool(self.app.get_accounts())

    def iniciar_login(self) -> dict:
        """Gera o código de login (device code). Mostre fluxo['verification_uri'] e fluxo['user_code']."""
        fluxo = self.app.initiate_device_flow(scopes=ESCOPOS)
        if "user_code" not in fluxo:
            raise ErroGraph(f"Falha ao iniciar login: {json.dumps(fluxo, indent=2)}")
        return fluxo

    def concluir_login(self, fluxo: dict) -> dict:
        """Bloqueia até você concluir o login no navegador (ou o código expirar, ~15 min)."""
        res = self.app.acquire_token_by_device_flow(fluxo)
        self._salvar()
        if "access_token" not in res:
            raise ErroGraph(f"Login falhou: {res.get('error_description')}")
        return res

    def login_interativo(self) -> dict:
        fluxo = self.iniciar_login()
        print(fluxo["message"])
        res = self.app.acquire_token_by_device_flow(fluxo)
        self._salvar()
        if "access_token" not in res:
            raise ErroGraph(f"Login falhou: {res.get('error_description')}")
        return res


# ---------------------------------------------------------------- utilidades
def _limpar_texto(txt: str | None, limite: int = 4000) -> str:
    if not txt:
        return ""
    txt = re.sub(r"\r", "", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt[:limite] + ("…[cortado]" if len(txt) > limite else "")


def _resumo_email(m: dict) -> dict:
    remet = (m.get("from") or {}).get("emailAddress", {})
    return {
        "id": m["id"],
        "assunto": m.get("subject"),
        "de": f'{remet.get("name", "")} <{remet.get("address", "")}>',
        "para": [r["emailAddress"]["address"] for r in m.get("toRecipients", [])],
        "recebido": m.get("receivedDateTime"),
        "lido": m.get("isRead"),
        "importancia": m.get("importance"),
        "sinalizado": (m.get("flag") or {}).get("flagStatus") == "flagged",
        "categorias": m.get("categories", []),
        "anexos": m.get("hasAttachments"),
        "previa": _limpar_texto(m.get("bodyPreview"), 300),
        "conversa": m.get("conversationId"),
    }


def _resumo_evento(e: dict) -> dict:
    return {
        "id": e["id"],
        "titulo": e.get("subject"),
        "inicio": e["start"]["dateTime"][:16],
        "fim": e["end"]["dateTime"][:16],
        "local": (e.get("location") or {}).get("displayName"),
        "online": bool(e.get("isOnlineMeeting")),
        "link": (e.get("onlineMeeting") or {}).get("joinUrl"),
        "organizador": (e.get("organizer") or {}).get("emailAddress", {}).get("address"),
        "participantes": [a["emailAddress"].get("address") for a in e.get("attendees", [])],
        "minha_resposta": (e.get("responseStatus") or {}).get("response"),
        "cancelado": e.get("isCancelled"),
        "dia_inteiro": e.get("isAllDay"),
        "previa": _limpar_texto(e.get("bodyPreview"), 400),
    }


CAMPOS_EMAIL = ("id,subject,from,toRecipients,receivedDateTime,isRead,importance,flag,"
                "categories,hasAttachments,bodyPreview,conversationId")


# ---------------------------------------------------------------- cliente
class Graph:
    def __init__(self, autenticador: Autenticador, fuso: str = "America/Sao_Paulo"):
        self.auth = autenticador
        self.fuso = fuso
        self._eu: dict | None = None
        self.http = httpx.AsyncClient(timeout=30)

    async def _req(self, metodo: str, caminho: str, *, params: dict | None = None,
                   json_body: Any = None, headers: dict | None = None) -> dict:
        token = await asyncio.to_thread(self.auth.token)
        h = {"Authorization": f"Bearer {token}",
             "Prefer": f'outlook.timezone="{self.fuso}", outlook.body-content-type="text"'}
        if headers:
            h.update(headers)
        url = caminho if caminho.startswith("http") else GRAPH + caminho
        for tentativa in range(3):
            r = await self.http.request(metodo, url, params=params, json=json_body, headers=h)
            if r.status_code == 429 or r.status_code >= 500:
                await asyncio.sleep(int(r.headers.get("Retry-After", 2 * (tentativa + 1))))
                continue
            break
        if r.status_code >= 400:
            raise ErroGraph(f"Graph {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    # ---------------- perfil
    async def eu(self) -> dict:
        if self._eu is None:
            self._eu = await self._req("GET", "/me", params={"$select": "displayName,mail,userPrincipalName"})
        return self._eu

    async def meu_email(self) -> str:
        e = await self.eu()
        return (e.get("mail") or e.get("userPrincipalName") or "").lower()

    # ---------------- e-mail: leitura
    async def buscar_emails(self, texto: str | None = None, pasta: str = "inbox", dias: int = 7,
                            apenas_nao_lidos: bool = False, limite: int = 15) -> list[dict]:
        desde = (datetime.now(timezone.utc) - timedelta(days=dias))
        caminho = f"/me/mailFolders/{pasta}/messages" if pasta else "/me/messages"
        params: dict[str, Any] = {"$select": CAMPOS_EMAIL, "$top": min(limite, 50)}
        if texto:
            # $search não combina com $filter/$orderby → data entra na própria consulta KQL
            t = texto.replace('"', "")
            params["$search"] = f'"{t} received>={desde.date().isoformat()}"'
        else:
            f = f"receivedDateTime ge {desde.strftime('%Y-%m-%dT%H:%M:%SZ')}"
            if apenas_nao_lidos:
                f += " and isRead eq false"
            params["$filter"] = f
            params["$orderby"] = "receivedDateTime desc"
        dados = await self._req("GET", caminho, params=params)
        msgs = [_resumo_email(m) for m in dados.get("value", [])]
        if texto and apenas_nao_lidos:
            msgs = [m for m in msgs if not m["lido"]]
        return msgs[:limite]

    async def ler_email(self, id_msg: str) -> dict:
        m = await self._req("GET", f"/me/messages/{id_msg}",
                            params={"$select": CAMPOS_EMAIL + ",body,ccRecipients"})
        r = _resumo_email(m)
        r["cc"] = [x["emailAddress"]["address"] for x in m.get("ccRecipients", [])]
        r["corpo"] = _limpar_texto((m.get("body") or {}).get("content"), 6000)
        r.pop("previa", None)
        return r

    async def mensagens_da_conversa(self, conversa_id: str) -> list[dict]:
        cid = conversa_id.replace("'", "''")
        dados = await self._req("GET", "/me/messages", params={
            "$filter": f"conversationId eq '{cid}'",
            "$select": "id,from,receivedDateTime,sentDateTime,isDraft", "$top": 50})
        return dados.get("value", [])

    async def enviados_sem_resposta(self, dias_min: int, dias_max: int = 21,
                                    ignorar: list[str] | None = None) -> list[dict]:
        """E-mails que EU enviei há mais de `dias_min` dias e que ninguém respondeu."""
        agora = datetime.now(timezone.utc)
        enviados = await self._req("GET", "/me/mailFolders/sentitems/messages", params={
            "$filter": f"sentDateTime ge {(agora - timedelta(days=dias_max)).strftime('%Y-%m-%dT%H:%M:%SZ')} "
                       f"and sentDateTime le {(agora - timedelta(days=dias_min)).strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "$orderby": "sentDateTime desc", "$top": 50,
            "$select": "id,subject,toRecipients,sentDateTime,conversationId"})
        eu = await self.meu_email()
        ignorar = [i.lower() for i in (ignorar or [])]
        vistos, pendentes = set(), []
        for m in enviados.get("value", []):
            conv = m.get("conversationId")
            if not conv or conv in vistos:
                continue  # só o último envio de cada conversa
            vistos.add(conv)
            dest = [r["emailAddress"]["address"].lower() for r in m.get("toRecipients", [])]
            if not dest or all(d == eu for d in dest) or any(i in d for d in dest for i in ignorar):
                continue
            conversa = await self.mensagens_da_conversa(conv)
            if not _houve_resposta(conversa, eu, m["sentDateTime"]):
                pendentes.append({"id": m["id"], "assunto": m.get("subject"), "para": dest,
                                  "enviado": m["sentDateTime"][:16]})
        return pendentes

    async def recebidos_sem_resposta(self, dias: int = 7, ignorar: list[str] | None = None,
                                     limite: int = 40) -> list[dict]:
        """E-mails recebidos na caixa de entrada que EU ainda não respondi."""
        msgs = await self.buscar_emails(pasta="inbox", dias=dias, limite=limite)
        eu = await self.meu_email()
        ignorar = [i.lower() for i in (ignorar or [])]
        vistos, pendentes = set(), []
        for m in msgs:  # já vem do mais recente para o mais antigo
            conv = m.get("conversa")
            if not conv or conv in vistos:
                continue
            vistos.add(conv)
            de = m["de"].lower()
            if eu in de or any(i in de for i in ignorar):
                continue
            conversa = await self.mensagens_da_conversa(conv)
            respondi = any(
                ((c.get("from") or {}).get("emailAddress") or {}).get("address", "").lower() == eu
                and not c.get("isDraft") and (c.get("sentDateTime") or "") > (m["recebido"] or "")
                for c in conversa)
            if not respondi:
                pendentes.append(m)
        return pendentes

    # ---------------- e-mail: escrita (sempre via confirmação no bot)
    async def criar_rascunho(self, para: list[str], assunto: str, corpo: str,
                             cc: list[str] | None = None) -> dict:
        return await self._req("POST", "/me/messages", json_body={
            "subject": assunto,
            "body": {"contentType": "Text", "content": corpo},
            "toRecipients": [{"emailAddress": {"address": a}} for a in para],
            "ccRecipients": [{"emailAddress": {"address": a}} for a in (cc or [])],
        })

    async def criar_rascunho_resposta(self, id_msg: str, corpo: str, todos: bool = False) -> dict:
        acao = "createReplyAll" if todos else "createReply"
        return await self._req("POST", f"/me/messages/{id_msg}/{acao}", json_body={"comment": corpo})

    async def enviar_rascunho(self, id_rascunho: str) -> None:
        await self._req("POST", f"/me/messages/{id_rascunho}/send")

    async def excluir_rascunho(self, id_rascunho: str) -> None:
        await self._req("DELETE", f"/me/messages/{id_rascunho}")

    async def _id_pasta(self, nome: str) -> str:
        conhecidas = {"inbox", "archive", "deleteditems", "junkemail", "drafts", "sentitems"}
        if nome.lower() in conhecidas:
            return nome.lower()
        for caminho in ("/me/mailFolders", "/me/mailFolders/inbox/childFolders"):
            dados = await self._req("GET", caminho, params={"$top": 100, "$select": "id,displayName"})
            for p in dados.get("value", []):
                if p["displayName"].lower() == nome.lower():
                    return p["id"]
        raise ErroGraph(f"Pasta '{nome}' não encontrada")

    async def mover_email(self, id_msg: str, pasta: str) -> None:
        await self._req("POST", f"/me/messages/{id_msg}/move",
                        json_body={"destinationId": await self._id_pasta(pasta)})

    async def categorizar_email(self, id_msg: str, categorias: list[str]) -> None:
        await self._req("PATCH", f"/me/messages/{id_msg}", json_body={"categories": categorias})

    # ---------------- agenda
    async def listar_eventos(self, inicio: datetime, fim: datetime) -> list[dict]:
        dados = await self._req("GET", "/me/calendarView", params={
            "startDateTime": inicio.isoformat(), "endDateTime": fim.isoformat(),
            "$orderby": "start/dateTime", "$top": 100,
            "$select": "id,subject,start,end,location,isOnlineMeeting,onlineMeeting,organizer,"
                       "attendees,responseStatus,isCancelled,isAllDay,bodyPreview"})
        return [_resumo_evento(e) for e in dados.get("value", []) if not e.get("isCancelled")]

    async def criar_evento(self, titulo: str, inicio: str, fim: str, participantes: list[str] | None = None,
                           local: str | None = None, descricao: str | None = None, online: bool = False) -> dict:
        corpo: dict[str, Any] = {
            "subject": titulo,
            "start": {"dateTime": inicio, "timeZone": self.fuso},
            "end": {"dateTime": fim, "timeZone": self.fuso},
            "attendees": [{"emailAddress": {"address": a}, "type": "required"} for a in (participantes or [])],
        }
        if local:
            corpo["location"] = {"displayName": local}
        if descricao:
            corpo["body"] = {"contentType": "Text", "content": descricao}
        if online:
            corpo["isOnlineMeeting"] = True
            corpo["onlineMeetingProvider"] = "teamsForBusiness"
        return _resumo_evento(await self._req("POST", "/me/events", json_body=corpo))

    async def responder_convite(self, id_evento: str, resposta: str, comentario: str = "") -> None:
        acao = {"aceitar": "accept", "recusar": "decline", "talvez": "tentativelyAccept"}[resposta]
        await self._req("POST", f"/me/events/{id_evento}/{acao}",
                        json_body={"comment": comentario, "sendResponse": True})

    async def fechar(self) -> None:
        await self.http.aclose()


def _houve_resposta(conversa: list[dict], eu: str, enviado_em: str) -> bool:
    """True se alguém além de mim mandou mensagem na conversa depois do meu envio."""
    for c in conversa:
        if c.get("isDraft"):
            continue
        remet = ((c.get("from") or {}).get("emailAddress") or {}).get("address", "").lower()
        quando = c.get("receivedDateTime") or c.get("sentDateTime") or ""
        if remet and remet != eu and quando > enviado_em:
            return True
    return False
