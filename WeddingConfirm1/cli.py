#!/usr/bin/env python3
"""
Linha de comando, para quem não quer subir o servidor.

Envio de verdade:
    python cli.py --numeros numeros.txt --mensagens mensagens.txt --transporte whatsapp_web

Ensaio, sem enviar nada (recomendado antes do primeiro disparo real):
    python cli.py --numeros numeros.txt --mensagens mensagens.txt --transporte simulado

Só ver como as mensagens vão sair:
    python cli.py --mensagens mensagens.txt --previa 8

O arquivo de mensagens aceita várias versões separadas por uma linha com ---.
O progresso vai para o mesmo banco do painel web, então dá para começar aqui e
acompanhar no navegador.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from core.contatos import importar
from core.db import Banco
from core.executor import Executor
from core.limites import Ritmo
from core.transportes import montar_transporte
from core.variacoes import ContextoCampanha, SeletorDeVariacoes, separar_templates


def argumentos(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="WeddingConfirm pela linha de comando.")
    p.add_argument("--numeros", help="arquivo com um convidado por linha: nome,telefone")
    p.add_argument("--mensagens", required=True, help="arquivo com as versões da mensagem")
    p.add_argument("--previa", type=int, metavar="N", help="só mostra N exemplos e sai")
    p.add_argument("--transporte", default="simulado",
                   choices=["simulado", "whatsapp_web", "cloud_api"])
    p.add_argument("--campanha", default="Campanha pelo terminal")
    p.add_argument("--noivos", default="")
    p.add_argument("--data", default="")
    p.add_argument("--local", default="")
    p.add_argument("--base-url", default="", help="base dos links de confirmação")
    p.add_argument("--ddi", default="55")
    p.add_argument("--teto-diario", type=int, default=150)
    p.add_argument("--intervalo", type=float, default=32.0, help="segundos entre envios (mediana)")
    p.add_argument("--lote", type=int, default=18, help="envios antes da pausa longa")
    p.add_argument("--inicio", default="09:00")
    p.add_argument("--fim", default="20:30")
    p.add_argument("--perfil", default="./.wa_profile")
    p.add_argument("--banco", default="weddingconfirm.db")
    p.add_argument("--api-id", default="")
    p.add_argument("--api-token", default="")
    p.add_argument("--api-template", default="")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    a = argumentos(argv if argv is not None else sys.argv[1:])
    templates = separar_templates(Path(a.mensagens).read_text(encoding="utf-8"))
    if not templates:
        print("O arquivo de mensagens está vazio.")
        return 1

    ctx = ContextoCampanha(noivos=a.noivos, data=a.data, local=a.local,
                           base_url=a.base_url or "https://seu-link")
    seletor = SeletorDeVariacoes(templates)

    if a.previa:
        diag = seletor.diagnostico(a.previa)
        print(f"{len(templates)} versões · {diag['capacidade']} textos diferentes possíveis")
        print(diag["recado"], "\n")
        for i, exemplo in enumerate(seletor.amostra(a.previa, ctx), 1):
            print(f"--- {i}. para {exemplo['nome']}  [{exemplo['fp']}]")
            print(exemplo["texto"], "\n")
        return 0

    if not a.numeros:
        print("Informe --numeros, ou use --previa para só ver os exemplos.")
        return 1

    resultado = importar(Path(a.numeros).read_text(encoding="utf-8"), ddi_padrao=a.ddi)
    for r in resultado.rejeitados:
        print(f"  fora: linha {r['linha']} — {r['motivo']}")
    for w in resultado.avisos:
        print(f"  confira: linha {w['linha']} — {w['motivo']}")
    if not resultado.convidados:
        print("Nenhum telefone válido no arquivo.")
        return 1

    banco = Banco(a.banco)
    cid = banco.criar_campanha(
        a.campanha, noivos=a.noivos, data_evento=a.data, local=a.local,
        base_url=a.base_url, ddi=a.ddi,
        ritmo={"teto_diario": a.teto_diario, "intervalo_mediano_s": a.intervalo,
               "tamanho_lote": a.lote, "hora_inicio": a.inicio, "hora_fim": a.fim},
    )
    banco.salvar_templates(cid, templates)
    novos = banco.adicionar_convidados(cid, resultado.convidados)

    diag = seletor.diagnostico(novos)
    print(f"\nCampanha {cid}: {novos} convidados · {diag['capacidade']} textos possíveis")
    print(Ritmo(teto_diario=a.teto_diario, intervalo_mediano_s=a.intervalo,
                tamanho_lote=a.lote).estimar(novos))
    if diag["nivel"] != "ok":
        print(f"AVISO: {diag['recado']}")
    if a.transporte != "simulado":
        if input(f"\nEnviar de verdade para {novos} pessoas? [digite SIM] ").strip() != "SIM":
            print("Cancelado. A campanha ficou salva; retome pelo painel web.")
            return 0

    config = {"tipo": a.transporte, "perfil": a.perfil, "phone_number_id": a.api_id,
              "token": a.api_token, "template": a.api_template}
    executor = Executor(banco)
    executor.iniciar(cid, montar_transporte(config))

    try:
        while executor.rodando() or not executor.fila.empty():
            for e in executor.eventos_pendentes(timeout=2.0):
                print(f"  [{e['quando'][11:16]}] {e['texto']}")
    except KeyboardInterrupt:
        print("\nParando com cuidado...")
        executor.parar()
        while executor.rodando():
            time.sleep(0.3)

    r = banco.resumo(cid)
    print(f"\nEnviados {r['enviados']} · inválidos {r['invalidos']} · "
          f"falhas {r['falhas']} · pendentes {r['pendentes']}")
    print(f"Retome ou acompanhe as respostas em: python app.py (campanha {cid})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
