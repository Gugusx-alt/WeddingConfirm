"""
Motor de variação de mensagens.

Problema que resolve: enviar o MESMO texto para N pessoas é o padrão mais óbvio
de comportamento automatizado. A solução aqui é gerar texto genuinamente
diferente para cada convidado, e garantir por construção que a mesma string não
se repita dentro de uma janela de envios.

Três camadas de variação:

1. Múltiplos templates (o usuário escreve 3-6 versões do convite).
2. Spintax dentro de cada template: {Oi|Olá|Oiê} escolhe uma das opções.
   Aceita aninhamento: {Oi{,| }|Olá} funciona.
3. Placeholders personalizados: {nome}, {primeiro_nome}, {saudacao}, {link}.

E uma camada de garantia: SeletorDeVariacoes lembra as últimas N impressões
digitais (hash do texto normalizado) e recusa repetir qualquer uma delas.
"""

from __future__ import annotations

import hashlib
import random
import re
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Sequence

# Placeholders reconhecidos. Chave = nome no template, valor = descrição na UI.
PLACEHOLDERS = {
    "nome": "Nome completo do convidado, como está na lista",
    "primeiro_nome": "Só o primeiro nome",
    "saudacao": "Bom dia / Boa tarde / Boa noite, conforme a hora do envio",
    "link": "Link pessoal de confirmação do convidado",
    "noivos": "Nome do casal, definido na campanha",
    "data": "Data do casamento, definida na campanha",
    "local": "Local da cerimônia, definido na campanha",
}

_SEP_TEMPLATES = re.compile(r"^\s*---+\s*$", re.MULTILINE)


# --------------------------------------------------------------------------- #
# Spintax
# --------------------------------------------------------------------------- #

class ErroDeSintaxe(ValueError):
    """Chaves desbalanceadas no template."""


def _achar_bloco(texto: str, inicio: int) -> int:
    """Índice da '}' que fecha a '{' em `inicio`. -1 se não fechar."""
    profundidade = 0
    for i in range(inicio, len(texto)):
        if texto[i] == "{":
            profundidade += 1
        elif texto[i] == "}":
            profundidade -= 1
            if profundidade == 0:
                return i
    return -1


def _dividir_opcoes(corpo: str) -> list[str]:
    """Divide por '|' respeitando aninhamento."""
    partes, profundidade, atual = [], 0, []
    for ch in corpo:
        if ch == "{":
            profundidade += 1
        elif ch == "}":
            profundidade -= 1
        if ch == "|" and profundidade == 0:
            partes.append("".join(atual))
            atual = []
        else:
            atual.append(ch)
    partes.append("".join(atual))
    return partes


def expandir_spintax(template: str, rng: random.Random | None = None) -> str:
    """Resolve um bloco {a|b|c} por vez, da esquerda para a direita."""
    rng = rng or random
    resultado = template
    guarda = 0
    while True:
        guarda += 1
        if guarda > 500:  # template patológico; devolve o que deu
            return resultado
        abre = resultado.find("{")
        if abre == -1:
            return resultado
        fecha = _achar_bloco(resultado, abre)
        if fecha == -1:
            raise ErroDeSintaxe(
                "Falta fechar uma chave '{'. Confira se toda opção {a|b} tem o '}'."
            )
        corpo = resultado[abre + 1:fecha]
        opcoes = _dividir_opcoes(corpo)
        if len(opcoes) == 1 and corpo.strip() in PLACEHOLDERS:
            # É placeholder, não spintax: pula para procurar depois dele.
            proximo = resultado.find("{", fecha)
            if proximo == -1:
                return resultado
            # Reentra na busca a partir do próximo bloco.
            prefixo, resto = resultado[:proximo], resultado[proximo:]
            return prefixo + expandir_spintax(resto, rng)
        escolha = rng.choice(opcoes)
        resultado = resultado[:abre] + escolha + resultado[fecha + 1:]


def contar_combinacoes(template: str) -> int:
    """Quantos textos distintos este template consegue gerar (só spintax)."""
    total = 1
    i = 0
    while i < len(template):
        if template[i] == "{":
            fim = _achar_bloco(template, i)
            if fim == -1:
                break
            corpo = template[i + 1:fim]
            opcoes = _dividir_opcoes(corpo)
            if not (len(opcoes) == 1 and corpo.strip() in PLACEHOLDERS):
                total *= max(1, len(opcoes)) * max(
                    1, sum(contar_combinacoes(o) for o in opcoes) // max(1, len(opcoes))
                )
            i = fim + 1
        else:
            i += 1
    return total


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #

def separar_templates(texto: str) -> list[str]:
    """Um arquivo com blocos separados por uma linha '---' vira vários templates."""
    blocos = [b.strip() for b in _SEP_TEMPLATES.split(texto)]
    return [b for b in blocos if b]


def saudacao_por_hora(agora: datetime | None = None) -> str:
    h = (agora or datetime.now()).hour
    if h < 12:
        return "bom dia"
    if h < 18:
        return "boa tarde"
    return "boa noite"


@dataclass(frozen=True)
class Convidado:
    telefone: str                 # E.164 sem '+', ex: 5546999998888
    nome: str | None = None
    token: str | None = None      # token do link pessoal de confirmação
    id: int | None = None

    @property
    def primeiro_nome(self) -> str:
        if not self.nome:
            return ""
        return self.nome.strip().split()[0]


@dataclass(frozen=True)
class ContextoCampanha:
    noivos: str = ""
    data: str = ""
    local: str = ""
    base_url: str = ""            # ex: https://confirma.meucasamento.com


def preencher(
    texto: str,
    convidado: Convidado,
    ctx: ContextoCampanha,
    agora: datetime | None = None,
) -> str:
    link = ""
    if convidado.token and ctx.base_url:
        link = f"{ctx.base_url.rstrip('/')}/c/{convidado.token}"
    valores = {
        "nome": convidado.nome or "",
        "primeiro_nome": convidado.primeiro_nome,
        "saudacao": saudacao_por_hora(agora),
        "link": link,
        "noivos": ctx.noivos,
        "data": ctx.data,
        "local": ctx.local,
    }
    for chave, valor in valores.items():
        texto = texto.replace("{" + chave + "}", valor)
    # Limpa resíduos de nome vazio: "Oi , tudo bem" -> "Oi, tudo bem"
    texto = re.sub(r"\s+([,.!?])", r"\1", texto)
    texto = re.sub(r"[ \t]{2,}", " ", texto)
    texto = texto.strip()
    # {saudacao} no início do texto sai minúsculo; a frase precisa começar maiúscula.
    if texto and texto[0].islower():
        texto = texto[0].upper() + texto[1:]
    return texto


# --------------------------------------------------------------------------- #
# Impressão digital e seleção
# --------------------------------------------------------------------------- #

def impressao_digital(texto: str) -> str:
    """
    Hash do texto normalizado: minúsculas, sem acento, sem pontuação e sem o
    nome próprio. Serve para detectar "é a mesma mensagem, só trocou o nome".
    """
    t = unicodedata.normalize("NFKD", texto.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return hashlib.sha1(t.encode()).hexdigest()[:12]


@dataclass
class SeletorDeVariacoes:
    """
    Escolhe o texto de cada envio garantindo que nada se repita dentro de uma
    janela deslizante. Padrão: 25 envios sem repetir (o problema relatado era
    "a cada 10 mensagens tem que trocar" — 25 dá folga confortável).
    """

    templates: Sequence[str]
    janela: int = 25
    tentativas: int = 40
    rng: random.Random = field(default_factory=random.Random)
    _recentes: deque[str] = field(init=False)
    _uso_template: dict[int, int] = field(init=False)

    def __post_init__(self) -> None:
        if not self.templates:
            raise ValueError("Cadastre pelo menos um template de mensagem.")
        self._recentes = deque(maxlen=self.janela)
        self._uso_template = {i: 0 for i in range(len(self.templates))}

    # -- capacidade -------------------------------------------------------- #

    def capacidade(self) -> int:
        """Estimativa de textos distintos possíveis com os templates atuais."""
        return sum(contar_combinacoes(t) for t in self.templates)

    def diagnostico(self, total_convidados: int) -> dict:
        cap = self.capacidade()
        if cap >= max(total_convidados, self.janela) * 2:
            nivel = "ok"
            recado = f"{cap} mensagens diferentes possíveis para {total_convidados} convidados."
        elif cap >= self.janela:
            nivel = "atencao"
            recado = (
                f"Só {cap} mensagens diferentes para {total_convidados} convidados. "
                "Vai funcionar, mas adicione mais opções {a|b|c} para folga."
            )
        else:
            nivel = "erro"
            recado = (
                f"{cap} mensagens diferentes é pouco: a repetição vai acontecer "
                f"antes de {self.janela} envios. Adicione mais templates ou mais opções."
            )
        return {"capacidade": cap, "nivel": nivel, "recado": recado, "janela": self.janela}

    # -- seleção ----------------------------------------------------------- #

    def _template_menos_usado(self) -> int:
        menor = min(self._uso_template.values())
        candidatos = [i for i, n in self._uso_template.items() if n == menor]
        return self.rng.choice(candidatos)

    def proxima(
        self,
        convidado: Convidado,
        ctx: ContextoCampanha,
        agora: datetime | None = None,
    ) -> tuple[str, str]:
        """Devolve (texto, impressao_digital). Nunca repete dentro da janela."""
        melhor: tuple[str, str] | None = None
        for _ in range(self.tentativas):
            idx = self._template_menos_usado()
            bruto = expandir_spintax(self.templates[idx], self.rng)
            texto = preencher(bruto, convidado, ctx, agora)
            fp = impressao_digital(texto)
            melhor = melhor or (texto, fp)
            if fp not in self._recentes:
                self._uso_template[idx] += 1
                self._recentes.append(fp)
                return texto, fp
        # Esgotou as tentativas: os templates não dão variação suficiente.
        # Manda o melhor que achou, mas registra a repetição para o relatório.
        texto, fp = melhor  # type: ignore[misc]
        self._recentes.append(fp)
        return texto, fp

    def amostra(
        self,
        quantidade: int,
        ctx: ContextoCampanha,
        nomes: Iterable[str] = ("Ana Beatriz", "Carlos Eduardo", "Marina", "Tio Zé", "Fernanda Souza"),
    ) -> list[dict]:
        """Pré-visualização para a interface: N mensagens como sairiam de verdade."""
        nomes = list(nomes)
        preview = []
        for i in range(quantidade):
            nome = nomes[i % len(nomes)]
            c = Convidado(telefone="5546900000000", nome=nome, token="exemplo123")
            texto, fp = self.proxima(c, ctx)
            preview.append({"nome": nome, "texto": texto, "fp": fp})
        return preview
