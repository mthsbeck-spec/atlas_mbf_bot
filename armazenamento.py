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
