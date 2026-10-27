"""Persistência local em SQLite: histórico da conversa, ações aguardando confirmação e controle de rotinas."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path


class Armazenamento:
    def __init__(self, arquivo: Path):
        arquivo.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(arquivo, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS historico (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                papel TEXT NOT NULL, texto TEXT NOT NULL, criado REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS acoes (
                id TEXT PRIMARY KEY, tipo TEXT NOT NULL, dados TEXT NOT NULL,
                resumo TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pendente', criado REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS avisos (
                chave TEXT PRIMARY KEY, criado REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS uso (
                id INTEGER PRIMARY KEY AUTOINCREMENT, criado REAL NOT NULL, dia TEXT NOT NULL,
                provedor TEXT NOT NULL, modelo TEXT NOT NULL, origem TEXT NOT NULL,
                entrada INTEGER DEFAULT 0, saida INTEGER DEFAULT 0,
                cache_escrita INTEGER DEFAULT 0, cache_leitura INTEGER DEFAULT 0,
                segundos REAL DEFAULT 0, custo REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS uso_dia ON uso (dia);
            CREATE TABLE IF NOT EXISTS integracoes (
                nome TEXT PRIMARY KEY, ultimo_ok REAL, ultimo_erro REAL, erro TEXT);
            CREATE TABLE IF NOT EXISTS ajustes (
                chave TEXT PRIMARY KEY, valor TEXT NOT NULL);
        """)
        self.db.commit()

    # ---------------- histórico (apenas texto; o uso de ferramentas fica dentro de cada turno)
    def adicionar_historico(self, papel: str, texto: str) -> None:
        self.db.execute("INSERT INTO historico (papel, texto, criado) VALUES (?,?,?)",
                        (papel, texto, time.time()))
        self.db.commit()

    def historico(self, limite: int = 20, horas: int = 12) -> list[dict]:
        """Últimas mensagens recentes, garantindo que começa com 'user' e alterna papéis."""
        corte = time.time() - horas * 3600
        linhas = self.db.execute(
            "SELECT papel, texto FROM historico WHERE criado >= ? ORDER BY id DESC LIMIT ?",
            (corte, limite)).fetchall()[::-1]
        msgs: list[dict] = []
        for papel, texto in linhas:
            if msgs and msgs[-1]["role"] == papel:
                msgs[-1]["content"] += "\n\n" + texto
            else:
                msgs.append({"role": papel, "content": texto})
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        return msgs

    def limpar_historico(self) -> None:
        self.db.execute("DELETE FROM historico")
        self.db.commit()

    # ---------------- ações que exigem confirmação
    def nova_acao(self, tipo: str, dados: dict, resumo: str) -> str:
        id_acao = uuid.uuid4().hex[:10]
        self.db.execute("INSERT INTO acoes (id, tipo, dados, resumo, criado) VALUES (?,?,?,?,?)",
                        (id_acao, tipo, json.dumps(dados, ensure_ascii=False), resumo, time.time()))
        self.db.commit()
        return id_acao

    def pegar_acao(self, id_acao: str) -> dict | None:
        r = self.db.execute("SELECT id, tipo, dados, resumo, status FROM acoes WHERE id=?",
                            (id_acao,)).fetchone()
        if not r:
            return None
        return {"id": r[0], "tipo": r[1], "dados": json.loads(r[2]), "resumo": r[3], "status": r[4]}

    def marcar_acao(self, id_acao: str, status: str) -> bool:
        """Muda o status só se ainda estiver pendente (evita duplo clique executar duas vezes)."""
        cur = self.db.execute("UPDATE acoes SET status=? WHERE id=? AND status='pendente'",
                              (status, id_acao))
        self.db.commit()
        return cur.rowcount == 1

    def marcar_acao_final(self, id_acao: str, status: str) -> None:
        self.db.execute("UPDATE acoes SET status=? WHERE id=?", (status, id_acao))
        self.db.commit()

    # ---------------- avisos já enviados (ex.: preparo de uma reunião)
    def ja_avisou(self, chave: str) -> bool:
        return self.db.execute("SELECT 1 FROM avisos WHERE chave=?", (chave,)).fetchone() is not None

    def registrar_aviso(self, chave: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO avisos (chave, criado) VALUES (?,?)", (chave, time.time()))
        self.db.execute("DELETE FROM avisos WHERE criado < ?", (time.time() - 30 * 86400,))
        self.db.commit()

    # ---------------- uso e custos de API
    def registrar_uso(self, dia: str, provedor: str, modelo: str, origem: str, custo: float, *,
                      entrada: int = 0, saida: int = 0, cache_escrita: int = 0, cache_leitura: int = 0,
                      segundos: float = 0) -> None:
        self.db.execute(
            "INSERT INTO uso (criado, dia, provedor, modelo, origem, entrada, saida, cache_escrita, "
            "cache_leitura, segundos, custo) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), dia, provedor, modelo, origem, entrada, saida, cache_escrita, cache_leitura,
             segundos, custo))
        self.db.commit()

    def custo_periodo(self, dia_ini: str, dia_fim: str) -> float:
        r = self.db.execute("SELECT COALESCE(SUM(custo),0) FROM uso WHERE dia BETWEEN ? AND ?",
                            (dia_ini, dia_fim)).fetchone()
        return float(r[0])

    def custo_por_dia(self, dia_ini: str, dia_fim: str) -> dict[str, float]:
        return {d: float(c) for d, c in self.db.execute(
            "SELECT dia, SUM(custo) FROM uso WHERE dia BETWEEN ? AND ? GROUP BY dia", (dia_ini, dia_fim))}

    def uso_agrupado(self, dia_ini: str, dia_fim: str, campo: str) -> list[dict]:
        assert campo in ("origem", "provedor", "modelo")
        linhas = self.db.execute(
            f"SELECT {campo}, COUNT(*), SUM(custo), SUM(entrada+cache_escrita+cache_leitura), SUM(saida), "
            f"SUM(segundos) FROM uso WHERE dia BETWEEN ? AND ? GROUP BY {campo} ORDER BY SUM(custo) DESC",
            (dia_ini, dia_fim)).fetchall()
        return [{"nome": n, "chamadas": q, "custo": float(c or 0), "entrada": int(e or 0),
                 "saida": int(s or 0), "segundos": float(sg or 0)} for n, q, c, e, s, sg in linhas]

    # ---------------- status das integrações
    def registrar_status(self, nome: str, ok: bool, erro: str | None = None) -> None:
        agora = time.time()
        if ok:
            self.db.execute("INSERT INTO integracoes (nome, ultimo_ok) VALUES (?,?) "
                            "ON CONFLICT(nome) DO UPDATE SET ultimo_ok=excluded.ultimo_ok", (nome, agora))
        else:
            self.db.execute("INSERT INTO integracoes (nome, ultimo_erro, erro) VALUES (?,?,?) "
                            "ON CONFLICT(nome) DO UPDATE SET ultimo_erro=excluded.ultimo_erro, erro=excluded.erro",
                            (nome, agora, (erro or "")[:500]))
        self.db.commit()

    def status_integracoes(self) -> dict[str, dict]:
        return {n: {"ultimo_ok": ok, "ultimo_erro": er, "erro": msg} for n, ok, er, msg in
                self.db.execute("SELECT nome, ultimo_ok, ultimo_erro, erro FROM integracoes")}

    # ---------------- ajustes editáveis pelo painel (sobrepõem as variáveis de ambiente)
    def ajuste(self, chave: str, padrao: str | None = None) -> str | None:
        r = self.db.execute("SELECT valor FROM ajustes WHERE chave=?", (chave,)).fetchone()
        return r[0] if r else padrao

    def salvar_ajuste(self, chave: str, valor: str) -> None:
        self.db.execute("INSERT INTO ajustes (chave, valor) VALUES (?,?) "
                        "ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor", (chave, valor))
        self.db.commit()
