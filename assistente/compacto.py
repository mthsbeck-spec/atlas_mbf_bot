"""Formato compacto para mandar e-mails e eventos ao Claude gastando poucos tokens.

- IDs do Outlook (~150 caracteres) viram referências curtas e estáveis, como "m3f9a1c2" (e-mail) e
  "e7b20d41" (evento). O mapeamento fica no SQLite e as ferramentas resolvem a referência de volta.
- Cada item vira uma linha de texto, sem chaves JSON repetidas.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .armazenamento import Armazenamento

_REF = re.compile(r"^[me][0-9a-f]{7}$")


class Referencias:
    def __init__(self, banco: Armazenamento):
        self.banco = banco
        banco.db.execute("CREATE TABLE IF NOT EXISTS refs (ref TEXT PRIMARY KEY, id TEXT NOT NULL)")
        banco.db.commit()

    def ref(self, id_graph: str, tipo: str = "m") -> str:
        r = tipo + hashlib.sha1(id_graph.encode()).hexdigest()[:7]
        self.banco.db.execute("INSERT OR IGNORE INTO refs (ref, id) VALUES (?,?)", (r, id_graph))
        self.banco.db.commit()
        return r

    def resolver(self, valor: str) -> str:
        """Aceita referência curta ou ID completo (compatibilidade)."""
        v = (valor or "").strip().strip("[]")
        if _REF.match(v):
            linha = self.banco.db.execute("SELECT id FROM refs WHERE ref=?", (v,)).fetchone()
            if linha:
                return linha[0]
        return valor


def _quando(iso_utc: str | None, fuso: ZoneInfo) -> str:
    if not iso_utc:
        return "?"
    try:
        return datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(fuso).strftime("%d/%m %H:%M")
    except ValueError:
        return iso_utc[:16]


def _curto(txt: str | None, n: int) -> str:
    t = re.sub(r"\s+", " ", txt or "").strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def emails(lista: list[dict], refs: Referencias, fuso: ZoneInfo, enviados: bool = False, previa: int = 140) -> str:
    """Uma linha por e-mail: [ref] data · remetente · assunto · marcadores — prévia."""
    if not lista:
        return "(nenhum)"
    linhas = []
    for e in lista:
        quem = ("para " + ", ".join(e.get("para", [])[:3])) if enviados else e.get("de", "")
        marcas = [m for m, ativo in [("📎", e.get("anexos")), ("NÃO LIDO", e.get("lido") is False and not enviados),
                                     ("⚑", e.get("sinalizado")), ("alta prioridade", e.get("importancia") == "high")]
                  if ativo]
        linha = f"[{refs.ref(e['id'])}] {_quando(e.get('recebido'), fuso)} · {quem} · {_curto(e.get('assunto'), 120)}"
        if marcas:
            linha += " · " + " ".join(marcas)
        if previa and e.get("previa"):
            linha += " — " + _curto(e["previa"], previa)
        linhas.append(linha)
    return "\n".join(linhas)


def sem_resposta(lista: list[dict], refs: Referencias) -> str:
    if not lista:
        return "(nenhum)"
    return "\n".join(f"[{refs.ref(e['id'])}] enviado {e['enviado'][:10]} para {', '.join(e['para'][:3])} · "
                     f"{_curto(e.get('assunto'), 120)}" for e in lista)


def email_completo(e: dict, refs: Referencias, fuso: ZoneInfo, limite_corpo: int = 4000) -> str:
    cab = [f"[{refs.ref(e['id'])}] {_curto(e.get('assunto'), 200)}",
           f"De: {e.get('de')} · {_quando(e.get('recebido'), fuso)}",
           f"Para: {', '.join(e.get('para', []))}"]
    if e.get("cc"):
        cab.append(f"Cc: {', '.join(e['cc'])}")
    if e.get("anexos"):
        cab.append("Tem anexos")
    corpo = (e.get("corpo") or "").strip()
    # corta o histórico citado de respostas anteriores, que costuma repetir o que já foi lido
    corte = re.search(r"\n(De:|From:|-----Original|Em .{5,80} escreveu:|On .{5,80} wrote:)", corpo)
    if corte and corte.start() > 200:
        corpo = corpo[: corte.start()].rstrip() + "\n[histórico anterior omitido]"
    return "\n".join(cab) + "\n\n" + _curto_bloco(corpo, limite_corpo)


def _curto_bloco(txt: str, n: int) -> str:
    return txt if len(txt) <= n else txt[:n] + "…[cortado]"


def eventos(lista: list[dict], refs: Referencias, eu: str = "", detalhado: bool = False) -> str:
    """Uma linha por evento: [ref] dia hora–hora · título · local · com quem · resposta."""
    if not lista:
        return "(nenhum)"
    nomes_resp = {"accepted": "aceito", "tentativelyAccepted": "talvez", "declined": "recusado",
                  "notResponded": "SEM RESPOSTA", "organizer": "organizador", "none": ""}
    linhas = []
    for e in lista:
        quando = "dia inteiro" if e.get("dia_inteiro") else f"{e['inicio'][8:10]}/{e['inicio'][5:7]} " \
                                                           f"{e['inicio'][11:16]}–{e['fim'][11:16]}"
        outros = [p for p in e.get("participantes", []) if p and p.lower() != eu]
        partes = [f"[{refs.ref(e['id'], 'e')}] {quando}", _curto(e.get("titulo"), 100)]
        local = "Teams" if e.get("online") else (e.get("local") or "")
        if local:
            partes.append(_curto(local, 40))
        if outros:
            partes.append("com " + ", ".join(outros[:6]) + (f" +{len(outros) - 6}" if len(outros) > 6 else ""))
        resp = nomes_resp.get(e.get("minha_resposta") or "none", "")
        if resp:
            partes.append(resp)
        linha = " · ".join(partes)
        if detalhado:
            if e.get("previa"):
                linha += "\n  Pauta: " + _curto(e["previa"], 300)
            if e.get("link"):
                linha += "\n  Link: " + e["link"]
        linhas.append(linha)
    return "\n".join(linhas)
