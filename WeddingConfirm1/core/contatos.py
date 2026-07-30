"""
Leitura e normalização da lista de convidados.

Corrige três defeitos da versão original do normalize_phone:

1. DDD 55 confundido com DDI 55. "55 9988-7766" (Santa Maria/RS, 10 dígitos)
   começa com "55" e a versão antiga concluía que já tinha DDI, gerando um
   número sem DDI. Agora o comprimento é o sinal principal, não o prefixo.
2. Nono dígito ausente. Listas antigas guardam celular com 8 dígitos.
   O WhatsApp não acha esse número. Agora o 9 é inserido quando o padrão
   indica celular.
3. DDD inexistente passava batido. Agora é validado contra a lista da Anatel,
   então erro de digitação aparece na importação em vez de virar "número
   inválido" 40 minutos depois no meio do envio.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass

from .variacoes import Convidado

DDDS_VALIDOS = {
    11, 12, 13, 14, 15, 16, 17, 18, 19,
    21, 22, 24, 27, 28,
    31, 32, 33, 34, 35, 37, 38,
    41, 42, 43, 44, 45, 46, 47, 48, 49,
    51, 53, 54, 55,
    61, 62, 63, 64, 65, 66, 67, 68, 69,
    71, 73, 74, 75, 77, 79,
    81, 82, 83, 84, 85, 86, 87, 88, 89,
    91, 92, 93, 94, 95, 96, 97, 98, 99,
}

_NAO_DIGITO = re.compile(r"\D+")


class TelefoneInvalido(ValueError):
    """Não há como enviar para este número."""


class TelefoneSuspeito(TelefoneInvalido):
    """Formato válido, mas provavelmente errado. Entra na lista com aviso."""

    def __init__(self, mensagem: str, e164: str) -> None:
        super().__init__(mensagem)
        self.e164 = e164


def so_digitos(valor: str) -> str:
    return _NAO_DIGITO.sub("", valor or "")


def normalizar_telefone(bruto: str, ddi_padrao: str = "55") -> str:
    """
    Devolve E.164 sem '+', ex: '5546999887766'. Levanta TelefoneInvalido com
    mensagem em português explicando o que está errado.
    """
    d = so_digitos(bruto)
    if not d:
        raise TelefoneInvalido("telefone vazio")

    if d.startswith("00") and len(d) > 4:      # 005546... -> 5546...
        d = d[2:]
    elif d.startswith("0") and len(d) in (11, 12):
        d = d[1:]                              # tronco: 046999887766 -> 46999887766

    if ddi_padrao == "55":
        return _normalizar_br(d)

    # Outros países: heurística simples por comprimento.
    if not d.startswith(ddi_padrao) and len(d) <= 11:
        d = ddi_padrao + d
    if len(d) < 10:
        raise TelefoneInvalido(f"curto demais: {d}")
    return d


def _normalizar_br(d: str) -> str:
    # Decide se o DDI já está presente pelo COMPRIMENTO, não pelo prefixo.
    if len(d) in (10, 11):                     # DDD + 8 ou 9 dígitos
        nacional = d
    elif len(d) in (12, 13) and d.startswith("55"):
        nacional = d[2:]
    elif len(d) in (12, 13):
        raise TelefoneInvalido(
            f"{d} tem {len(d)} dígitos e não começa com 55. "
            "Se for de outro país, informe o DDI na importação."
        )
    else:
        raise TelefoneInvalido(f"{d} tem {len(d)} dígitos; esperado 10, 11, 12 ou 13")

    ddd = int(nacional[:2])
    if ddd not in DDDS_VALIDOS:
        raise TelefoneInvalido(f"DDD {ddd:02d} não existe")

    assinante = nacional[2:]

    if len(assinante) == 8 and assinante[0] in "6789":
        # Celular antigo sem o nono dígito.
        assinante = "9" + assinante
    elif len(assinante) == 9 and assinante[0] != "9":
        raise TelefoneInvalido(
            f"celular com 9 dígitos deveria começar com 9: {nacional}"
        )
    elif len(assinante) == 8 and assinante[0] in "2345":
        # WhatsApp Business funciona em fixo, então não descarto — só aviso.
        raise TelefoneSuspeito(
            "parece telefone fixo; confira se é o celular",
            "55" + f"{ddd:02d}" + assinante,
        )

    return "55" + f"{ddd:02d}" + assinante


def formatar_para_exibir(e164: str) -> str:
    """5546999887766 -> +55 (46) 99988-7766"""
    if e164.startswith("55") and len(e164) == 13:
        return f"+55 ({e164[2:4]}) {e164[4:9]}-{e164[9:]}"
    if e164.startswith("55") and len(e164) == 12:
        return f"+55 ({e164[2:4]}) {e164[4:8]}-{e164[8:]}"
    return "+" + e164


# --------------------------------------------------------------------------- #
# Importação
# --------------------------------------------------------------------------- #

@dataclass
class ResultadoImportacao:
    convidados: list[Convidado]
    rejeitados: list[dict]      # {"linha": int, "conteudo": str, "motivo": str}
    duplicados: int
    avisos: list[dict] = None   # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.avisos is None:
            self.avisos = []


_CABECALHOS_TEL = {"telefone", "celular", "whatsapp", "fone", "numero", "número", "phone"}
_CABECALHOS_NOME = {"nome", "convidado", "name", "contato"}


def _detectar_colunas(primeira_linha: list[str]) -> tuple[int, int] | None:
    """Se a primeira linha for cabeçalho, devolve (col_telefone, col_nome)."""
    baixo = [c.strip().lower() for c in primeira_linha]
    tel = nome = None
    for i, c in enumerate(baixo):
        if c in _CABECALHOS_TEL and tel is None:
            tel = i
        if c in _CABECALHOS_NOME and nome is None:
            nome = i
    if tel is None:
        return None
    return tel, (nome if nome is not None else -1)


def importar(texto: str, ddi_padrao: str = "55") -> ResultadoImportacao:
    """
    Aceita CSV com cabeçalho, "telefone,nome", "nome,telefone" ou uma coluna só.
    Linhas em branco e começando com # são ignoradas. Ordem das colunas é
    detectada: o campo que tiver 10+ dígitos é o telefone.
    """
    linhas = list(csv.reader(io.StringIO(texto.strip()), skipinitialspace=True))
    linhas = [l for l in linhas if l and any(c.strip() for c in l)]
    if not linhas:
        return ResultadoImportacao([], [], 0)

    col_tel = col_nome = None
    mapa = _detectar_colunas(linhas[0])
    if mapa:
        col_tel, col_nome = mapa
        linhas = linhas[1:]

    convidados: list[Convidado] = []
    rejeitados: list[dict] = []
    avisos: list[dict] = []
    vistos: set[str] = set()
    duplicados = 0

    for n, linha in enumerate(linhas, start=2 if mapa else 1):
        conteudo = ",".join(linha)
        if conteudo.strip().startswith("#"):
            continue

        if col_tel is not None:
            parte_tel = linha[col_tel] if col_tel < len(linha) else ""
            parte_nome = linha[col_nome] if 0 <= col_nome < len(linha) else ""
        else:
            # Sem cabeçalho: o campo com mais dígitos é o telefone.
            campos = [c.strip() for c in linha if c.strip()]
            if not campos:
                continue
            parte_tel = max(campos, key=lambda c: len(so_digitos(c)))
            outros = [c for c in campos if c is not parte_tel]
            parte_nome = outros[0] if outros else ""

        try:
            tel = normalizar_telefone(parte_tel, ddi_padrao)
        except TelefoneSuspeito as e:
            tel = e.e164
            avisos.append({"linha": n, "conteudo": conteudo, "motivo": str(e)})
        except TelefoneInvalido as e:
            rejeitados.append({"linha": n, "conteudo": conteudo, "motivo": str(e)})
            continue

        if tel in vistos:
            duplicados += 1
            continue
        vistos.add(tel)
        nome = parte_nome.strip() or None
        convidados.append(Convidado(telefone=tel, nome=nome))

    return ResultadoImportacao(convidados, rejeitados, duplicados, avisos)
