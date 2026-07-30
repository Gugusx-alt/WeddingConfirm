"""
Executor da campanha.

Roda em uma thread, para a interface web não travar. Estados: parada, enviando,
pausada, concluida. Pode pausar no meio, fechar a aba, voltar depois e retomar —
o progresso está no banco, não na memória.
"""

from __future__ import annotations

import random
import threading
from datetime import datetime, time as dtime
from queue import Empty, Queue
from typing import Callable

from .db import Banco
from .limites import Ritmo, ordenar_para_envio
from .transportes import Resultado, SessaoPerdida, Transporte
from .variacoes import ContextoCampanha, SeletorDeVariacoes


def ritmo_de_dict(d: dict) -> Ritmo:
    d = dict(d or {})
    for campo in ("hora_inicio", "hora_fim"):
        v = d.get(campo)
        if isinstance(v, str) and ":" in v:
            h, m = v.split(":")[:2]
            d[campo] = dtime(int(h), int(m))
    validos = Ritmo.__dataclass_fields__.keys()
    return Ritmo(**{k: v for k, v in d.items() if k in validos})


class Executor:
    def __init__(self, banco: Banco) -> None:
        self.banco = banco
        self.thread: threading.Thread | None = None
        self.campanha_id: int | None = None
        self.estado = "parada"
        self._pausar = threading.Event()
        self._parar = threading.Event()
        self._acordar = threading.Event()
        self.fila: Queue[dict] = Queue(maxsize=500)
        self.atual: dict | None = None

    # -- controle ---------------------------------------------------------- #

    def rodando(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def iniciar(self, campanha_id: int, transporte: Transporte) -> None:
        if self.rodando():
            raise RuntimeError("Já existe uma campanha em envio. Pare antes de iniciar outra.")
        self._pausar.clear()
        self._parar.clear()
        self.campanha_id = campanha_id
        self.estado = "preparando"
        self.thread = threading.Thread(
            target=self._rodar, args=(campanha_id, transporte), daemon=True
        )
        self.thread.start()

    def pausar(self) -> None:
        self._pausar.set()
        self.estado = "pausada"
        self._emitir("info", "Pausado. Nenhum envio novo até você retomar.")

    def retomar(self) -> None:
        self._pausar.clear()
        self._acordar.set()
        if self.rodando():
            self.estado = "enviando"
            self._emitir("info", "Retomado.")

    def parar(self) -> None:
        self._parar.set()
        self._pausar.clear()
        self._acordar.set()

    def eventos_pendentes(self, timeout: float = 25.0) -> list[dict]:
        """Consome a fila para o stream da interface (SSE)."""
        saida: list[dict] = []
        try:
            saida.append(self.fila.get(timeout=timeout))
        except Empty:
            return saida
        while True:
            try:
                saida.append(self.fila.get_nowait())
            except Empty:
                return saida

    def situacao(self) -> dict:
        base = {"estado": self.estado, "campanha_id": self.campanha_id, "atual": self.atual}
        if self.campanha_id:
            base["resumo"] = self.banco.resumo(self.campanha_id)
        return base

    # -- interno ----------------------------------------------------------- #

    def _emitir(self, nivel: str, texto: str, **extra) -> None:
        if self.campanha_id is None:
            return
        registro = self.banco.registrar_evento(self.campanha_id, nivel, texto)
        registro.update(extra)
        registro["estado"] = self.estado
        registro["resumo"] = self.banco.resumo(self.campanha_id)
        if self.fila.full():
            try:
                self.fila.get_nowait()
            except Empty:
                pass
        self.fila.put(registro)

    def _dormir(self, segundos: float) -> bool:
        """Espera podendo ser interrompida. False = pediram para parar."""
        fim = segundos
        passo = 0.5
        while fim > 0:
            if self._parar.is_set():
                return False
            self._acordar.wait(min(passo, fim))
            self._acordar.clear()
            fim -= passo
        return True

    def _esperar_retomada(self) -> bool:
        while self._pausar.is_set():
            if self._parar.is_set():
                return False
            self._acordar.wait(0.5)
            self._acordar.clear()
        return True

    def _rodar(self, campanha_id: int, transporte: Transporte) -> None:
        campanha = self.banco.campanha(campanha_id)
        if not campanha:
            self.estado = "parada"
            return

        import json as _json
        ritmo = ritmo_de_dict(_json.loads(campanha["ritmo"] or "{}"))
        templates = self.banco.templates(campanha_id)
        if not templates:
            self.estado = "parada"
            self._emitir("erro", "Nenhuma mensagem cadastrada. Escreva ao menos uma.")
            return

        ctx = ContextoCampanha(
            noivos=campanha["noivos"],
            data=campanha["data_evento"],
            local=campanha["local"],
            base_url=campanha["base_url"],
        )
        rng = random.Random()
        seletor = SeletorDeVariacoes(templates, rng=rng)
        diag = seletor.diagnostico(len(self.banco.pendentes(campanha_id)))
        if diag["nivel"] != "ok":
            self._emitir("aviso", diag["recado"])

        pendentes = ordenar_para_envio(self.banco.pendentes(campanha_id), ritmo, rng)
        if not pendentes:
            self.estado = "concluida"
            self._emitir("ok", "Nada pendente: todos os convidados já receberam.")
            return

        try:
            self.estado = "preparando"
            self._emitir("info", f"Conectando via {getattr(transporte, 'nome', 'transporte')}...")
            transporte.preparar()
        except SessaoPerdida as e:
            self.estado = "parada"
            self._emitir("erro", str(e))
            return
        except Exception as e:
            self.estado = "parada"
            self._emitir("erro", f"Falha ao preparar o envio: {e}")
            return

        self.estado = "enviando"
        self.banco.atualizar_campanha(campanha_id, estado="enviando")
        enviados_hoje = self.banco.envios_de_hoje(campanha_id)
        self._emitir(
            "ok",
            f"{len(pendentes)} convidados na fila. Limite de hoje: {ritmo.limite_do_dia()} "
            f"({enviados_hoje} já saíram hoje).",
        )

        try:
            for indice, convidado in enumerate(pendentes):
                if self._parar.is_set():
                    break
                if not self._esperar_retomada():
                    break

                if enviados_hoje >= ritmo.limite_do_dia():
                    self._emitir(
                        "aviso",
                        f"Teto de {ritmo.limite_do_dia()} envios de hoje atingido. "
                        "Retome amanhã; os pendentes continuam salvos.",
                    )
                    break

                espera_janela = ritmo.espera_ate_abrir()
                if espera_janela > 0:
                    self._emitir(
                        "info",
                        f"Fora do horário de envio ({ritmo.hora_inicio:%H:%M}–"
                        f"{ritmo.hora_fim:%H:%M}). Aguardando.",
                    )
                    if not self._dormir(espera_janela):
                        break

                if indice:
                    intervalo = ritmo.proximo_intervalo(rng, indice)
                    if intervalo > 90:
                        self._emitir("info", f"Pausa de {int(intervalo // 60)} min entre lotes.")
                    if not self._dormir(intervalo):
                        break

                texto, fp = seletor.proxima(convidado, ctx)
                self.atual = {
                    "nome": convidado.nome,
                    "telefone": convidado.telefone,
                    "indice": indice + 1,
                    "total": len(pendentes),
                }

                try:
                    resultado: Resultado = transporte.enviar(convidado.telefone, texto)
                except SessaoPerdida as e:
                    self._emitir("erro", f"{e} Campanha interrompida; nada foi perdido.")
                    break
                except Exception as e:
                    resultado = Resultado("falhou", str(e))

                self.banco.marcar_envio(
                    convidado.id,
                    resultado.status,
                    mensagem=texto,
                    fp=fp,
                    erro=resultado.detalhe or None,
                )

                rotulo = convidado.nome or convidado.telefone
                if resultado.status == "enviado":
                    enviados_hoje += 1
                    self._emitir("ok", f"Enviado para {rotulo}", convidado=rotulo, fp=fp)
                elif resultado.status == "invalido":
                    self._emitir(
                        "aviso", f"{rotulo} não tem WhatsApp neste número", convidado=rotulo
                    )
                else:
                    self._emitir(
                        "erro", f"Falhou com {rotulo}: {resultado.detalhe}", convidado=rotulo
                    )
        finally:
            try:
                transporte.encerrar()
            except Exception:
                pass
            self.atual = None
            restantes = self.banco.resumo(campanha_id)["pendentes"]
            if self._parar.is_set():
                self.estado = "parada"
                self.banco.atualizar_campanha(campanha_id, estado="pausada")
                self._emitir("info", f"Parado por você. {restantes} convidados continuam pendentes.")
            elif restantes == 0:
                self.estado = "concluida"
                self.banco.atualizar_campanha(campanha_id, estado="concluida")
                self._emitir("ok", "Campanha concluída.")
            else:
                self.estado = "parada"
                self.banco.atualizar_campanha(campanha_id, estado="pausada")
                self._emitir("info", f"Envio encerrado com {restantes} pendentes.")
