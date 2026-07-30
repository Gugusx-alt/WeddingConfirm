"""
Ritmo de envio.

Variar o texto resolve metade do problema. A outra metade é o ritmo: 150 envios
em 12 minutos, todos com intervalo entre 2 e 5 segundos, é um padrão que
nenhuma pessoa produz. O que este módulo controla:

- Intervalo irregular (log-normal), não uniforme: a maioria dos intervalos
  fica perto da mediana e alguns são bem longos, como digitação humana.
- Lotes: a cada N envios, uma pausa de vários minutos.
- Janela de horário: nada de convite às 3h da manhã. Denúncia de usuário é o
  que mais pesa no bloqueio, e mensagem de madrugada é denúncia garantida.
- Teto diário, com aquecimento para número novo.
- Ordem embaralhada, para o envio não seguir a ordem da planilha (que costuma
  agrupar familiares com o mesmo DDD em sequência).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Sequence

# Aquecimento: quantos envios liberar por dia de uso do número.
# Número que nunca disparou em massa e manda 300 no primeiro dia é o caso
# clássico de bloqueio.
CURVA_AQUECIMENTO = [20, 40, 80, 150, 250, 400]


@dataclass
class Ritmo:
    intervalo_mediano_s: float = 32.0     # mediana entre envios
    dispersao: float = 0.55               # sigma do log-normal; maior = mais irregular
    intervalo_min_s: float = 8.0
    intervalo_max_s: float = 240.0

    tamanho_lote: int = 18                # envios antes da pausa longa
    pausa_lote_min_s: float = 240.0
    pausa_lote_max_s: float = 900.0

    hora_inicio: time = time(9, 0)
    hora_fim: time = time(20, 30)

    teto_diario: int = 150
    dia_de_aquecimento: int | None = None  # 1 = primeiro dia do número

    embaralhar: bool = True

    def limite_do_dia(self) -> int:
        if self.dia_de_aquecimento is None:
            return self.teto_diario
        i = max(1, self.dia_de_aquecimento) - 1
        aquecido = CURVA_AQUECIMENTO[min(i, len(CURVA_AQUECIMENTO) - 1)]
        return min(self.teto_diario, aquecido)

    # -- intervalos -------------------------------------------------------- #

    def proximo_intervalo(self, rng: random.Random, indice: int) -> float:
        """Segundos de espera antes do próximo envio."""
        if indice > 0 and indice % self.tamanho_lote == 0:
            return rng.uniform(self.pausa_lote_min_s, self.pausa_lote_max_s)
        mu = _log(self.intervalo_mediano_s)
        valor = _exp(rng.gauss(mu, self.dispersao))
        return max(self.intervalo_min_s, min(self.intervalo_max_s, valor))

    # -- janela de horário ------------------------------------------------- #

    def dentro_da_janela(self, agora: datetime | None = None) -> bool:
        agora = agora or datetime.now()
        return self.hora_inicio <= agora.time() <= self.hora_fim

    def espera_ate_abrir(self, agora: datetime | None = None) -> float:
        """Segundos até a janela abrir. 0 se já está aberta."""
        agora = agora or datetime.now()
        if self.dentro_da_janela(agora):
            return 0.0
        hoje_inicio = datetime.combine(agora.date(), self.hora_inicio)
        alvo = hoje_inicio if agora < hoje_inicio else hoje_inicio + timedelta(days=1)
        return (alvo - agora).total_seconds()

    # -- estimativa para a interface --------------------------------------- #

    def estimar(self, quantidade: int) -> dict:
        """Previsão de duração e de dias necessários, para mostrar antes de começar."""
        por_dia = self.limite_do_dia()
        dias = max(1, -(-quantidade // por_dia))       # divisão para cima
        no_dia = min(quantidade, por_dia)
        lotes = max(0, (no_dia - 1) // self.tamanho_lote)
        segundos = no_dia * self.intervalo_mediano_s + lotes * (
            (self.pausa_lote_min_s + self.pausa_lote_max_s) / 2
        )
        janela_s = (
            datetime.combine(date.today(), self.hora_fim)
            - datetime.combine(date.today(), self.hora_inicio)
        ).total_seconds()
        return {
            "por_dia": por_dia,
            "dias": dias,
            "duracao_hoje_s": int(segundos),
            "duracao_hoje_texto": _duracao_texto(segundos),
            "cabe_na_janela": segundos <= janela_s,
        }


def ordenar_para_envio(convidados: Sequence, ritmo: Ritmo, rng: random.Random) -> list:
    """Embaralha, mas mantém contatos do mesmo DDD longe um do outro."""
    lista = list(convidados)
    if not ritmo.embaralhar:
        return lista
    rng.shuffle(lista)
    # Espalha DDDs repetidos: ordena por (posição dentro do grupo do DDD).
    contagem: dict[str, int] = {}
    chaveado = []
    for c in lista:
        ddd = getattr(c, "telefone", "")[2:4]
        contagem[ddd] = contagem.get(ddd, 0) + 1
        chaveado.append((contagem[ddd], rng.random(), c))
    chaveado.sort(key=lambda t: (t[0], t[1]))
    return [c for _, _, c in chaveado]


@dataclass
class ContadorDiario:
    """Conta envios por dia para respeitar o teto, sobrevivendo a reinício."""
    dia: date
    enviados: int = 0

    def registrar(self, quando: datetime | None = None) -> None:
        quando = quando or datetime.now()
        if quando.date() != self.dia:
            self.dia, self.enviados = quando.date(), 0
        self.enviados += 1

    def atingiu(self, ritmo: Ritmo, quando: datetime | None = None) -> bool:
        quando = quando or datetime.now()
        if quando.date() != self.dia:
            return False
        return self.enviados >= ritmo.limite_do_dia()


def _log(x: float) -> float:
    import math
    return math.log(x)


def _exp(x: float) -> float:
    import math
    return math.exp(x)


def _duracao_texto(segundos: float) -> str:
    m = int(segundos // 60)
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}"
