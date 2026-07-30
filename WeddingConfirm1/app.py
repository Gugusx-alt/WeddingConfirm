"""
WeddingConfirm — servidor web.

    pip install -r requirements.txt
    python app.py

Abre em http://localhost:8000

Duas caras no mesmo servidor:
  /            painel dos noivos (escrever mensagens, importar lista, disparar)
  /c/{token}   página do convidado, um link por pessoa, para confirmar presença
"""

from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)

from core.contatos import formatar_para_exibir, importar
from core.db import Banco
from core.executor import Executor, ritmo_de_dict
from core.transportes import montar_transporte
from core.variacoes import ContextoCampanha, SeletorDeVariacoes

AQUI = Path(__file__).parent
WEB = AQUI / "web"

app = FastAPI(title="WeddingConfirm", docs_url=None, redoc_url=None)
banco = Banco(os.environ.get("WC_DB", AQUI / "weddingconfirm.db"))
executor = Executor(banco)


# --------------------------------------------------------------------------- #
# Páginas
# --------------------------------------------------------------------------- #

@app.get("/", response_class=HTMLResponse)
def painel() -> FileResponse:
    return FileResponse(WEB / "painel.html")


@app.get("/c/{token}", response_class=HTMLResponse)
def pagina_convidado(token: str) -> FileResponse:
    if not banco.por_token(token):
        raise HTTPException(404, "Link não encontrado")
    return FileResponse(WEB / "confirmar.html")


# --------------------------------------------------------------------------- #
# API — campanhas
# --------------------------------------------------------------------------- #

@app.get("/api/campanhas")
def listar_campanhas():
    saida = []
    for c in banco.campanhas():
        saida.append({**c, "resumo": banco.resumo(c["id"])})
    return saida


@app.post("/api/campanhas")
def criar_campanha(dados: dict = Body(...)):
    nome = (dados.get("nome") or "").strip()
    if not nome:
        raise HTTPException(400, "Dê um nome para a campanha.")
    cid = banco.criar_campanha(
        nome,
        noivos=dados.get("noivos", ""),
        data_evento=dados.get("data_evento", ""),
        local=dados.get("local", ""),
        base_url=dados.get("base_url", ""),
        ritmo=_ritmo_serializado(dados.get("ritmo")),
    )
    return {"id": cid}


@app.get("/api/campanhas/{cid}")
def ver_campanha(cid: int):
    c = banco.campanha(cid)
    if not c:
        raise HTTPException(404, "Campanha não encontrada")
    c["ritmo"] = json.loads(c["ritmo"] or "{}")
    return {
        **c,
        "templates": banco.templates(cid),
        "resumo": banco.resumo(cid),
        "estimativa": ritmo_de_dict(c["ritmo"]).estimar(banco.resumo(cid)["pendentes"]),
    }


@app.put("/api/campanhas/{cid}")
def salvar_campanha(cid: int, dados: dict = Body(...)):
    if not banco.campanha(cid):
        raise HTTPException(404, "Campanha não encontrada")
    campos = {k: v for k, v in dados.items()
              if k in {"nome", "noivos", "data_evento", "local", "base_url", "ddi"}}
    if "ritmo" in dados:
        campos["ritmo"] = json.dumps(_ritmo_serializado(dados["ritmo"]))
    banco.atualizar_campanha(cid, **campos)
    return {"ok": True}


def _ritmo_serializado(bruto) -> dict:
    """Aceita dict parcial do formulário e devolve dict completo e válido."""
    ritmo = ritmo_de_dict(bruto or {})
    return {
        "intervalo_mediano_s": ritmo.intervalo_mediano_s,
        "dispersao": ritmo.dispersao,
        "intervalo_min_s": ritmo.intervalo_min_s,
        "intervalo_max_s": ritmo.intervalo_max_s,
        "tamanho_lote": ritmo.tamanho_lote,
        "pausa_lote_min_s": ritmo.pausa_lote_min_s,
        "pausa_lote_max_s": ritmo.pausa_lote_max_s,
        "hora_inicio": ritmo.hora_inicio.strftime("%H:%M"),
        "hora_fim": ritmo.hora_fim.strftime("%H:%M"),
        "teto_diario": ritmo.teto_diario,
        "dia_de_aquecimento": ritmo.dia_de_aquecimento,
        "embaralhar": ritmo.embaralhar,
    }


# --------------------------------------------------------------------------- #
# API — mensagens
# --------------------------------------------------------------------------- #

@app.post("/api/campanhas/{cid}/mensagens")
def salvar_mensagens(cid: int, dados: dict = Body(...)):
    corpos = [c for c in (dados.get("corpos") or []) if c.strip()]
    if not corpos:
        raise HTTPException(400, "Escreva ao menos uma mensagem.")
    banco.salvar_templates(cid, corpos)
    return {"ok": True, "quantidade": len(corpos)}


@app.post("/api/campanhas/{cid}/previa")
def previa(cid: int, dados: dict = Body(...)):
    """Roleta de variações: mostra como as mensagens vão sair de verdade."""
    campanha = banco.campanha(cid) or {}
    corpos = [c for c in (dados.get("corpos") or []) if c.strip()]
    if not corpos:
        return {"amostra": [], "diagnostico": None, "erro": "Nenhuma mensagem escrita ainda."}
    ctx = ContextoCampanha(
        noivos=campanha.get("noivos", ""),
        data=campanha.get("data_evento", ""),
        local=campanha.get("local", ""),
        base_url=campanha.get("base_url") or "https://seu-link",
    )
    try:
        seletor = SeletorDeVariacoes(corpos)
        quantidade = max(1, min(12, int(dados.get("quantidade", 5))))
        pendentes = banco.resumo(cid)["pendentes"] or 100
        return {
            "amostra": seletor.amostra(quantidade, ctx),
            "diagnostico": seletor.diagnostico(pendentes),
        }
    except ValueError as e:
        return {"amostra": [], "diagnostico": None, "erro": str(e)}


# --------------------------------------------------------------------------- #
# API — convidados
# --------------------------------------------------------------------------- #

@app.post("/api/campanhas/{cid}/convidados")
def importar_convidados(cid: int, dados: dict = Body(...)):
    if not banco.campanha(cid):
        raise HTTPException(404, "Campanha não encontrada")
    texto = dados.get("texto") or ""
    if not texto.strip():
        raise HTTPException(400, "Cole a lista de convidados ou envie um CSV.")
    r = importar(texto, ddi_padrao=str(dados.get("ddi", "55")))
    novos = banco.adicionar_convidados(cid, r.convidados)
    return {
        "lidos": len(r.convidados),
        "novos": novos,
        "ja_existiam": len(r.convidados) - novos,
        "duplicados_no_arquivo": r.duplicados,
        "rejeitados": r.rejeitados,
        "avisos": r.avisos,
        "resumo": banco.resumo(cid),
    }


@app.get("/api/campanhas/{cid}/convidados")
def listar_convidados(cid: int):
    saida = []
    for c in banco.convidados(cid):
        saida.append({
            "id": c["id"],
            "nome": c["nome"],
            "telefone": formatar_para_exibir(c["telefone"]),
            "status": c["status"],
            "erro": c["erro"],
            "enviado_em": c["enviado_em"],
            "mensagem": c["mensagem"],
            "fp": c["fp"],
            "resposta": c["resposta"],
            "acompanhantes": c["acompanhantes"],
            "observacao": c["observacao"],
            "link": c["token"],
        })
    return saida


@app.get("/api/campanhas/{cid}/planilha.csv")
def exportar(cid: int):
    buffer = io.StringIO()
    escritor = csv.writer(buffer)
    escritor.writerow(["nome", "telefone", "status_envio", "resposta",
                       "acompanhantes", "observacao", "enviado_em", "erro"])
    for c in banco.convidados(cid):
        escritor.writerow([
            c["nome"] or "", formatar_para_exibir(c["telefone"]), c["status"],
            c["resposta"] or "", c["acompanhantes"] or 0, c["observacao"] or "",
            c["enviado_em"] or "", c["erro"] or "",
        ])
    return StreamingResponse(
        io.BytesIO(buffer.getvalue().encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="convidados-{cid}.csv"'},
    )


# --------------------------------------------------------------------------- #
# API — disparo
# --------------------------------------------------------------------------- #

@app.post("/api/campanhas/{cid}/iniciar")
def iniciar(cid: int, dados: dict = Body(default={})):
    if not banco.campanha(cid):
        raise HTTPException(404, "Campanha não encontrada")
    if not banco.templates(cid):
        raise HTTPException(400, "Escreva as mensagens antes de disparar.")
    if banco.resumo(cid)["pendentes"] == 0:
        raise HTTPException(400, "Não há convidados pendentes nesta campanha.")
    if executor.rodando():
        raise HTTPException(409, "Já existe um envio em andamento.")
    transporte = montar_transporte(dados.get("transporte") or {"tipo": "simulado"})
    executor.iniciar(cid, transporte)
    return {"ok": True, "transporte": getattr(transporte, "nome", "?")}


@app.post("/api/campanhas/{cid}/pausar")
def pausar(cid: int):
    executor.pausar()
    return {"ok": True}


@app.post("/api/campanhas/{cid}/retomar")
def retomar(cid: int):
    executor.retomar()
    return {"ok": True}


@app.post("/api/campanhas/{cid}/parar")
def parar(cid: int):
    executor.parar()
    return {"ok": True}


@app.get("/api/campanhas/{cid}/situacao")
def situacao(cid: int):
    dados = executor.situacao()
    if dados.get("campanha_id") != cid:
        dados = {"estado": "parada", "campanha_id": cid, "atual": None,
                 "resumo": banco.resumo(cid)}
    dados["eventos"] = banco.eventos(cid, 60)
    return dados


@app.get("/api/campanhas/{cid}/stream")
def stream(cid: int, request: Request):
    """Server-Sent Events: cada envio aparece no log ao vivo."""

    def gerar():
        yield ": conectado\n\n"
        while True:
            for evento in executor.eventos_pendentes(timeout=20.0):
                yield f"data: {json.dumps(evento, ensure_ascii=False)}\n\n"
            yield ": ping\n\n"

    return StreamingResponse(
        gerar(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------- #
# Confirmação do convidado
# --------------------------------------------------------------------------- #

@app.get("/api/c/{token}")
def dados_convidado(token: str):
    c = banco.por_token(token)
    if not c:
        raise HTTPException(404, "Link não encontrado")
    return {
        "nome": c["nome"],
        "primeiro_nome": (c["nome"] or "").split()[0] if c["nome"] else "",
        "noivos": c["noivos"],
        "data_evento": c["data_evento"],
        "local": c["local"],
        "resposta": c["resposta"],
        "acompanhantes": c["acompanhantes"],
        "observacao": c["observacao"],
    }


@app.post("/api/c/{token}")
def responder(token: str, dados: dict = Body(...)):
    c = banco.por_token(token)
    if not c:
        raise HTTPException(404, "Link não encontrado")
    resposta = dados.get("resposta")
    if resposta not in ("sim", "nao"):
        raise HTTPException(400, "Escolha se você vai ou não.")
    banco.registrar_resposta(
        c["id"], resposta,
        acompanhantes=dados.get("acompanhantes", 0),
        observacao=dados.get("observacao", ""),
    )
    banco.registrar_evento(
        c["campanha_id"], "ok",
        f"{c['nome'] or c['telefone']} respondeu: "
        f"{'vai' if resposta == 'sim' else 'não vai'}",
    )
    return {"ok": True}


@app.get("/c/{token}/sair")
def sair_da_lista(token: str):
    c = banco.por_token(token)
    if not c:
        raise HTTPException(404, "Link não encontrado")
    banco.marcar_optout(c["id"])
    banco.registrar_evento(c["campanha_id"], "aviso",
                           f"{c['nome'] or c['telefone']} pediu para não receber mais mensagens")
    return RedirectResponse(f"/c/{token}?saiu=1")


@app.exception_handler(HTTPException)
def erro_json(request: Request, exc: HTTPException):
    return JSONResponse({"erro": exc.detail}, status_code=exc.status_code)


if __name__ == "__main__":
    import uvicorn
    porta = int(os.environ.get("PORT", 8000))
    print(f"\n  WeddingConfirm em http://localhost:{porta}\n")
    uvicorn.run(app, host="127.0.0.1", port=porta, log_level="warning")
