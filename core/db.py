"""
Persistência em PostgreSQL.

O motivo de existir: a versão original guardava o progresso em variáveis de
memória. Se o Chrome caísse no convidado 90 de 200, não havia como retomar sem
reenviar para os 90 primeiros — que é justamente o pior cenário possível,
porque mensagem duplicada é o gatilho número um de denúncia.

Aqui todo envio é gravado assim que acontece, e a campanha retoma exatamente de
onde parou.
"""

from __future__ import annotations

import json
import secrets
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, time
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from .variacoes import Convidado

ESQUEMA = """
CREATE TABLE IF NOT EXISTS campanhas (
    id            SERIAL PRIMARY KEY,
    nome          TEXT NOT NULL,
    noivos        TEXT DEFAULT '',
    data_evento   TEXT DEFAULT '',
    local         TEXT DEFAULT '',
    base_url      TEXT DEFAULT '',
    ddi           TEXT DEFAULT '55',
    ritmo         TEXT DEFAULT '{}',
    estado        TEXT DEFAULT 'rascunho',   -- rascunho|enviando|pausada|concluida
    criada_em     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS templates (
    id          SERIAL PRIMARY KEY,
    campanha_id INTEGER NOT NULL REFERENCES campanhas(id) ON DELETE CASCADE,
    corpo       TEXT NOT NULL,
    ordem       INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS convidados (
    id           SERIAL PRIMARY KEY,
    campanha_id  INTEGER NOT NULL REFERENCES campanhas(id) ON DELETE CASCADE,
    telefone     TEXT NOT NULL,
    nome         TEXT,
    token        TEXT NOT NULL UNIQUE,
    grupo        TEXT DEFAULT '',
    status       TEXT DEFAULT 'pendente',   -- pendente|enviado|invalido|falhou|optout
    tentativas   INTEGER DEFAULT 0,
    mensagem     TEXT,
    fp           TEXT,
    erro         TEXT,
    enviado_em   TEXT,
    UNIQUE(campanha_id, telefone)
);

CREATE TABLE IF NOT EXISTS respostas (
    convidado_id   INTEGER PRIMARY KEY REFERENCES convidados(id) ON DELETE CASCADE,
    resposta       TEXT NOT NULL,            -- sim|nao
    acompanhantes  INTEGER DEFAULT 0,
    observacao     TEXT DEFAULT '',
    respondido_em  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS eventos (
    id          SERIAL PRIMARY KEY,
    campanha_id INTEGER NOT NULL,
    quando      TEXT NOT NULL,
    nivel       TEXT NOT NULL,               -- info|ok|aviso|erro
    texto       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conv_camp ON convidados(campanha_id, status);
CREATE INDEX IF NOT EXISTS idx_ev_camp ON eventos(campanha_id, id);
"""


class Banco:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        with self.conexao() as con:
            con.execute(ESQUEMA)

    @contextmanager
    def conexao(self) -> Iterator[psycopg.Connection]:
        con = psycopg.connect(self.dsn, row_factory=dict_row)
        try:
            yield con
            con.commit()
        finally:
            con.close()

    # -- campanhas --------------------------------------------------------- #

    def criar_campanha(self, nome: str, **campos) -> int:
        with self.conexao() as con:
            cur = con.execute(
                """INSERT INTO campanhas (nome, noivos, data_evento, local, base_url,
                                          ddi, ritmo, criada_em)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (
                    nome,
                    campos.get("noivos", ""),
                    campos.get("data_evento", ""),
                    campos.get("local", ""),
                    campos.get("base_url", ""),
                    campos.get("ddi", "55"),
                    json.dumps(campos.get("ritmo", {})),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            return int(cur.fetchone()["id"])

    def atualizar_campanha(self, campanha_id: int, **campos) -> None:
        if not campos:
            return
        permitidos = {"nome", "noivos", "data_evento", "local", "base_url",
                      "ddi", "ritmo", "estado"}
        campos = {k: v for k, v in campos.items() if k in permitidos}
        if "ritmo" in campos and not isinstance(campos["ritmo"], str):
            campos["ritmo"] = json.dumps(_ritmo_json(campos["ritmo"]))
        sets = ", ".join(f"{k} = %s" for k in campos)
        with self.conexao() as con:
            con.execute(
                f"UPDATE campanhas SET {sets} WHERE id = %s",
                (*campos.values(), campanha_id),
            )

    def campanha(self, campanha_id: int) -> dict | None:
        with self.conexao() as con:
            row = con.execute(
                "SELECT * FROM campanhas WHERE id = %s", (campanha_id,)
            ).fetchone()
        return dict(row) if row else None

    def campanhas(self) -> list[dict]:
        with self.conexao() as con:
            rows = con.execute(
                "SELECT * FROM campanhas ORDER BY id DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def apagar_campanha(self, campanha_id: int) -> None:
        with self.conexao() as con:
            # templates/convidados/respostas somem via ON DELETE CASCADE;
            # eventos não tem FK declarada, então precisa apagar à parte.
            con.execute("DELETE FROM eventos WHERE campanha_id = %s", (campanha_id,))
            con.execute("DELETE FROM campanhas WHERE id = %s", (campanha_id,))

    # -- templates --------------------------------------------------------- #

    def salvar_templates(self, campanha_id: int, corpos: list[str]) -> None:
        with self.conexao() as con:
            con.execute("DELETE FROM templates WHERE campanha_id = %s", (campanha_id,))
            con.cursor().executemany(
                "INSERT INTO templates (campanha_id, corpo, ordem) VALUES (%s,%s,%s)",
                [(campanha_id, c.strip(), i) for i, c in enumerate(corpos) if c.strip()],
            )

    def templates(self, campanha_id: int) -> list[str]:
        with self.conexao() as con:
            rows = con.execute(
                "SELECT corpo FROM templates WHERE campanha_id = %s ORDER BY ordem",
                (campanha_id,),
            ).fetchall()
        return [r["corpo"] for r in rows]

    # -- convidados -------------------------------------------------------- #

    def adicionar_convidados(self, campanha_id: int, convidados: list[Convidado]) -> int:
        novos = 0
        with self.conexao() as con:
            for c in convidados:
                row = con.execute(
                    """INSERT INTO convidados (campanha_id, telefone, nome, token)
                       VALUES (%s,%s,%s,%s)
                       ON CONFLICT (campanha_id, telefone) DO UPDATE
                           SET nome = COALESCE(NULLIF(convidados.nome,''), EXCLUDED.nome)
                       RETURNING (xmax = 0) AS inserido""",
                    (campanha_id, c.telefone, c.nome, secrets.token_urlsafe(9)),
                ).fetchone()
                if row["inserido"]:
                    novos += 1
        return novos

    def pendentes(self, campanha_id: int) -> list[Convidado]:
        with self.conexao() as con:
            rows = con.execute(
                """SELECT id, telefone, nome, token FROM convidados
                   WHERE campanha_id = %s AND status IN ('pendente','falhou')
                   ORDER BY id""",
                (campanha_id,),
            ).fetchall()
        return [
            Convidado(telefone=r["telefone"], nome=r["nome"], token=r["token"], id=r["id"])
            for r in rows
        ]

    def marcar_envio(
        self,
        convidado_id: int,
        status: str,
        mensagem: str | None = None,
        fp: str | None = None,
        erro: str | None = None,
    ) -> None:
        with self.conexao() as con:
            con.execute(
                """UPDATE convidados
                      SET status = %s, mensagem = COALESCE(%s, mensagem),
                          fp = COALESCE(%s, fp), erro = %s,
                          tentativas = tentativas + 1,
                          enviado_em = CASE WHEN %s='enviado' THEN %s ELSE enviado_em END
                    WHERE id = %s""",
                (status, mensagem, fp, erro, status,
                 datetime.now().isoformat(timespec="seconds"), convidado_id),
            )

    def convidados(self, campanha_id: int) -> list[dict]:
        with self.conexao() as con:
            rows = con.execute(
                """SELECT c.*, r.resposta, r.acompanhantes, r.observacao, r.respondido_em
                     FROM convidados c
                     LEFT JOIN respostas r ON r.convidado_id = c.id
                    WHERE c.campanha_id = %s
                    ORDER BY c.nome IS NULL, c.nome, c.id""",
                (campanha_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def por_token(self, token: str) -> dict | None:
        with self.conexao() as con:
            row = con.execute(
                """SELECT c.*, ca.noivos, ca.data_evento, ca.local, ca.nome AS campanha_nome,
                          r.resposta, r.acompanhantes, r.observacao
                     FROM convidados c
                     JOIN campanhas ca ON ca.id = c.campanha_id
                     LEFT JOIN respostas r ON r.convidado_id = c.id
                    WHERE c.token = %s""",
                (token,),
            ).fetchone()
        return dict(row) if row else None

    # -- respostas --------------------------------------------------------- #

    def registrar_resposta(
        self, convidado_id: int, resposta: str, acompanhantes: int = 0, observacao: str = ""
    ) -> None:
        with self.conexao() as con:
            con.execute(
                """INSERT INTO respostas (convidado_id, resposta, acompanhantes,
                                          observacao, respondido_em)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT(convidado_id) DO UPDATE SET
                       resposta = EXCLUDED.resposta,
                       acompanhantes = EXCLUDED.acompanhantes,
                       observacao = EXCLUDED.observacao,
                       respondido_em = EXCLUDED.respondido_em""",
                (convidado_id, resposta, max(0, int(acompanhantes)), observacao.strip()[:500],
                 datetime.now().isoformat(timespec="seconds")),
            )

    def marcar_optout(self, convidado_id: int) -> None:
        with self.conexao() as con:
            con.execute(
                "UPDATE convidados SET status = 'optout' WHERE id = %s", (convidado_id,)
            )

    # -- resumo e log ------------------------------------------------------ #

    def resumo(self, campanha_id: int) -> dict:
        with self.conexao() as con:
            envio = dict(
                con.execute(
                    """SELECT
                         COUNT(*)                                          AS total,
                         COUNT(*) FILTER (WHERE status='pendente')         AS pendentes,
                         COUNT(*) FILTER (WHERE status='enviado')          AS enviados,
                         COUNT(*) FILTER (WHERE status='invalido')         AS invalidos,
                         COUNT(*) FILTER (WHERE status='falhou')           AS falhas,
                         COUNT(*) FILTER (WHERE status='optout')           AS optout
                       FROM convidados WHERE campanha_id = %s""",
                    (campanha_id,),
                ).fetchone()
            )
            rsvp = dict(
                con.execute(
                    """SELECT
                         COUNT(*) FILTER (WHERE r.resposta='sim')          AS confirmados,
                         COUNT(*) FILTER (WHERE r.resposta='nao')          AS recusados,
                         COALESCE(SUM(CASE WHEN r.resposta='sim'
                                           THEN 1 + r.acompanhantes END), 0) AS pessoas
                       FROM respostas r
                       JOIN convidados c ON c.id = r.convidado_id
                      WHERE c.campanha_id = %s""",
                    (campanha_id,),
                ).fetchone()
            )
            repetidas = con.execute(
                """SELECT COUNT(*) AS n FROM (
                       SELECT fp FROM convidados
                        WHERE campanha_id = %s AND fp IS NOT NULL
                        GROUP BY fp HAVING COUNT(*) > 1) sub""",
                (campanha_id,),
            ).fetchone()["n"]
        envio = {k: (v or 0) for k, v in envio.items()}
        rsvp = {k: (v or 0) for k, v in rsvp.items()}
        aguardando = envio["enviados"] - rsvp["confirmados"] - rsvp["recusados"]
        return {**envio, **rsvp, "aguardando": max(0, aguardando),
                "textos_repetidos": repetidas}

    def registrar_evento(self, campanha_id: int, nivel: str, texto: str) -> dict:
        registro = {
            "quando": datetime.now().isoformat(timespec="seconds"),
            "nivel": nivel,
            "texto": texto,
        }
        with self.conexao() as con:
            con.execute(
                "INSERT INTO eventos (campanha_id, quando, nivel, texto) VALUES (%s,%s,%s,%s)",
                (campanha_id, registro["quando"], nivel, texto),
            )
        return registro

    def eventos(self, campanha_id: int, limite: int = 200) -> list[dict]:
        with self.conexao() as con:
            rows = con.execute(
                """SELECT quando, nivel, texto FROM eventos
                    WHERE campanha_id = %s ORDER BY id DESC LIMIT %s""",
                (campanha_id, limite),
            ).fetchall()
        return [dict(r) for r in rows][::-1]

    def envios_de_hoje(self, campanha_id: int) -> int:
        hoje = datetime.now().date().isoformat()
        with self.conexao() as con:
            return con.execute(
                """SELECT COUNT(*) AS n FROM convidados
                    WHERE campanha_id = %s AND status = 'enviado'
                      AND substr(enviado_em, 1, 10) = %s""",
                (campanha_id, hoje),
            ).fetchone()["n"]


def _ritmo_json(ritmo) -> dict:
    """Ritmo (dataclass) -> dict serializável; time vira 'HH:MM'."""
    d = asdict(ritmo) if not isinstance(ritmo, dict) else dict(ritmo)
    for k, v in list(d.items()):
        if isinstance(v, time):
            d[k] = v.strftime("%H:%M")
    return d
