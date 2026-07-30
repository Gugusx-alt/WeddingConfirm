"""
Transportes de envio.

O executor não sabe como a mensagem sai. Isso permite três coisas:

- TransporteSimulado: roda a campanha inteira sem enviar nada. Serve para
  conferir textos, ritmo e interface antes de mexer com números reais.
- TransporteWhatsAppWeb: Selenium, como no projeto original, com correções.
- TransporteCloudAPI: API oficial do WhatsApp Business. É o caminho certo se
  isso vai virar serviço para outras pessoas. Ver README.

Correções em relação ao send_whatsapp.py original:

- A mensagem não vai mais pré-preenchida na URL: é digitada em blocos, com
  pausas. O destinatário vê "digitando...", que é o que acontece quando uma
  pessoa escreve. Colar 400 caracteres de uma vez, não.
- Quebra de linha usa Shift+Enter. Antes, um template com duas linhas era
  enviado como duas mensagens separadas ao apertar Enter.
- O envio é considerado bem-sucedido se o clique em enviar funcionou e não há
  sinal explícito de problema (número inválido, sessão caída). Tentamos antes
  confirmar visualmente o balão de saída no chat, mas a estrutura da página
  muda entre versões do WhatsApp Web e isso gerava falso negativo com
  mensagens que tinham chegado de verdade — pior que não confirmar.
- Detecta sessão encerrada e conta banida, e para a campanha em vez de
  acumular 200 falhas em silêncio.
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Protocol

# --------------------------------------------------------------------------- #

@dataclass
class Resultado:
    status: str            # enviado | invalido | falhou
    detalhe: str = ""


class SessaoPerdida(RuntimeError):
    """QR desconectado, conta banida ou navegador fechado. Interrompe tudo."""


class EnvioCancelado(RuntimeError):
    """Você pediu para parar enquanto o transporte estava esperando algo."""


class Transporte(Protocol):
    def preparar(self, deve_parar: "Callable[[], bool] | None" = None) -> None: ...
    def enviar(self, telefone: str, texto: str,
               deve_parar: "Callable[[], bool] | None" = None) -> Resultado: ...
    def encerrar(self) -> None: ...


# --------------------------------------------------------------------------- #
# Simulado
# --------------------------------------------------------------------------- #

class TransporteSimulado:
    """Não envia nada. Finge latência e uma taxa de números inválidos."""

    nome = "Simulação"

    def __init__(self, taxa_invalidos: float = 0.06, latencia: float = 0.4) -> None:
        self.taxa_invalidos = taxa_invalidos
        self.latencia = latencia
        self.rng = random.Random()

    def preparar(self, deve_parar=None) -> None:
        pass

    def enviar(self, telefone: str, texto: str, deve_parar=None) -> Resultado:
        time.sleep(self.latencia)
        if self.rng.random() < self.taxa_invalidos:
            return Resultado("invalido", "simulado: número sem WhatsApp")
        return Resultado("enviado", "simulado")

    def encerrar(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# WhatsApp Web (Selenium)
# --------------------------------------------------------------------------- #

TEXTOS_NUMERO_INVALIDO = [
    "número de telefone compartilhado via url é inválido",
    "número de telefone compartilhado via link é inválido",
    "número de telefone é inválido",
    "número inválido",
    "este número não está no whatsapp",
    "não está no whatsapp",
    "não possui whatsapp",
    "phone number shared via url is invalid",
    "phone number is invalid",
    "invalid phone number",
    "is not on whatsapp",
]

TEXTOS_SESSAO_PERDIDA = [
    "escaneie o código qr",
    "scan the qr code",
    "conecte um aparelho",
    "sua conta foi banida",
    "your account was banned",
    "this account is not allowed to use whatsapp",
    "esta conta não pode usar o whatsapp",
]


def _interrompivel(condicao, deve_parar):
    """Envolve uma condição do WebDriverWait para checar 'deve_parar' a cada tentativa.

    Sem isso, pedir para parar não tinha efeito enquanto o Selenium estivesse
    esperando o Chrome carregar ou a conversa abrir — só depois do timeout.
    """
    def _passo(driver):
        if deve_parar and deve_parar():
            raise EnvioCancelado()
        return condicao(driver)
    return _passo


class TransporteWhatsAppWeb:
    nome = "WhatsApp Web"

    def __init__(
        self,
        perfil: str = "./.wa_profile",
        timeout_login: int = 180,
        timeout_chat: int = 40,
        digitacao_cps: float = 22.0,     # caracteres por segundo
    ) -> None:
        self.perfil = perfil
        self.timeout_login = timeout_login
        self.timeout_chat = timeout_chat
        self.digitacao_cps = digitacao_cps
        self.driver = None
        self.rng = random.Random()

    # -- ciclo de vida ----------------------------------------------------- #

    def preparar(self, deve_parar=None) -> None:
        from selenium import webdriver
        from selenium.common.exceptions import WebDriverException
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        opcoes = webdriver.ChromeOptions()
        opcoes.add_argument("--disable-notifications")
        opcoes.add_argument("--start-maximized")
        opcoes.add_argument("--disable-blink-features=AutomationControlled")
        opcoes.add_experimental_option("excludeSwitches", ["enable-automation"])
        os.makedirs(self.perfil, exist_ok=True)
        opcoes.add_argument(f"--user-data-dir={os.path.abspath(self.perfil)}")

        try:
            self.driver = webdriver.Chrome(options=opcoes)
        except WebDriverException as e:
            raise SessaoPerdida(
                "Não foi possível abrir o Chrome. Instale o chromedriver "
                f"compatível com a sua versão do Chrome. Detalhe: {e}"
            ) from e

        self.driver.get("https://web.whatsapp.com/")
        try:
            WebDriverWait(self.driver, self.timeout_login, poll_frequency=0.5).until(
                _interrompivel(
                    EC.any_of(
                        EC.presence_of_element_located((By.XPATH, '//div[@id="side"]')),
                        EC.presence_of_element_located((By.XPATH, '//div[@role="grid"]')),
                    ),
                    deve_parar,
                )
            )
        except EnvioCancelado:
            raise
        except Exception:
            raise SessaoPerdida(
                f"Passaram {self.timeout_login}s e o WhatsApp Web não conectou. "
                "Leia o QR Code na janela do Chrome e comece de novo."
            )

    def encerrar(self) -> None:
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

    # -- envio ------------------------------------------------------------- #

    def enviar(self, telefone: str, texto: str, deve_parar=None) -> Resultado:
        from selenium.common.exceptions import InvalidSessionIdException, NoSuchWindowException

        try:
            return self._enviar_interno(telefone, texto, deve_parar)
        except (InvalidSessionIdException, NoSuchWindowException) as e:
            raise SessaoPerdida(
                "A janela do Chrome fechou ou a sessão caiu no meio do envio. "
                "Abra o painel e comece de novo — quem já recebeu não será reenviado."
            ) from e

    def _enviar_interno(self, telefone: str, texto: str, deve_parar=None) -> Resultado:
        from selenium.common.exceptions import TimeoutException
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        d = self.driver
        if d is None:
            raise SessaoPerdida("Transporte não foi preparado.")

        # Abre a conversa SEM texto pré-preenchido; o texto é digitado depois.
        d.get(f"https://web.whatsapp.com/send?phone={telefone}&type=phone_number&app_absent=0")

        try:
            campo = WebDriverWait(d, self.timeout_chat, poll_frequency=0.5).until(
                _interrompivel(
                    EC.presence_of_element_located(
                        (By.XPATH, '//footer//div[@contenteditable="true"]')
                    ),
                    deve_parar,
                )
            )
        except EnvioCancelado:
            raise
        except TimeoutException:
            if self._sessao_perdida():
                raise SessaoPerdida("Sessão do WhatsApp Web caiu ou a conta foi bloqueada.")
            if self._numero_invalido():
                return Resultado("invalido", "número não está no WhatsApp")
            return Resultado("falhou", "a conversa não abriu no tempo esperado")

        if self._numero_invalido():
            return Resultado("invalido", "número não está no WhatsApp")

        try:
            campo.click()
            self._digitar(campo, texto, deve_parar)
        except EnvioCancelado:
            raise
        except Exception as e:
            if self._sessao_perdida():
                raise SessaoPerdida("Sessão do WhatsApp Web caiu durante a digitação.")
            return Resultado("falhou", f"erro ao digitar: {e}")

        if not self._clicar_enviar(campo):
            return Resultado("falhou", "não achei o botão de enviar")

        # Confirmar visualmente que a bolha "saiu" é frágil: a Meta muda a
        # estrutura da página com frequência, e essa checagem já deu falso
        # negativo com mensagens que tinham chegado de verdade. Em vez disso,
        # confiamos que o clique funcionou e só marcamos falha diante de um
        # sinal explícito (número inválido ou sessão caída).
        time.sleep(1.5)
        if deve_parar and deve_parar():
            raise EnvioCancelado()
        if self._numero_invalido():
            return Resultado("invalido", "número não está no WhatsApp")
        if self._sessao_perdida():
            raise SessaoPerdida("Sessão do WhatsApp Web caiu ou a conta foi bloqueada.")
        return Resultado("enviado")

    # -- auxiliares -------------------------------------------------------- #

    def _digitar(self, campo, texto: str, deve_parar=None) -> None:
        """Digita em blocos curtos. Shift+Enter para quebra de linha."""
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.keys import Keys

        for i, linha in enumerate(texto.split("\n")):
            if deve_parar and deve_parar():
                raise EnvioCancelado()
            if i:
                ActionChains(self.driver).key_down(Keys.SHIFT).send_keys(
                    Keys.ENTER
                ).key_up(Keys.SHIFT).perform()
            pos = 0
            while pos < len(linha):
                if deve_parar and deve_parar():
                    raise EnvioCancelado()
                tamanho = self.rng.randint(6, 18)
                bloco = linha[pos:pos + tamanho]
                campo.send_keys(bloco)
                pos += tamanho
                time.sleep(len(bloco) / self.digitacao_cps * self.rng.uniform(0.7, 1.5))
        time.sleep(self.rng.uniform(0.4, 1.2))

    def _clicar_enviar(self, campo) -> bool:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys

        for seletor in (
            '//footer//button[@aria-label="Enviar" or @aria-label="Send"]',
            '//footer//span[@data-icon="send"]',
            '//footer//span[@data-icon="wds-ic-send-filled"]',
            '//footer//*[@data-testid="send"]',
        ):
            try:
                for btn in self.driver.find_elements(By.XPATH, seletor):
                    if btn.is_displayed():
                        self.driver.execute_script("arguments[0].click();", btn)
                        time.sleep(1.0)
                        return True
            except Exception:
                continue
        try:
            campo.send_keys(Keys.ENTER)
            time.sleep(1.0)
            return True
        except Exception:
            return False

    def _texto_da_pagina(self) -> str:
        from selenium.webdriver.common.by import By
        pedacos = []
        for seletor in ('//div[@role="dialog"]', '//div[@role="alert"]', "//body"):
            try:
                for el in self.driver.find_elements(By.XPATH, seletor)[:3]:
                    pedacos.append((el.text or "").lower())
            except Exception:
                continue
        return " ".join(pedacos)

    def _numero_invalido(self) -> bool:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys

        texto = self._texto_da_pagina()
        if not any(s in texto for s in TEXTOS_NUMERO_INVALIDO):
            return False
        # Fecha o diálogo para não travar o próximo contato.
        for seletor in (
            '//div[@role="dialog"]//div[@role="button"]',
            '//div[@role="dialog"]//button',
        ):
            try:
                for btn in self.driver.find_elements(By.XPATH, seletor):
                    if btn.is_displayed():
                        btn.click()
                        time.sleep(0.4)
                        return True
            except Exception:
                continue
        try:
            self.driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        except Exception:
            pass
        return True

    def _sessao_perdida(self) -> bool:
        texto = self._texto_da_pagina()
        return any(s in texto for s in TEXTOS_SESSAO_PERDIDA)


# --------------------------------------------------------------------------- #
# Cloud API oficial
# --------------------------------------------------------------------------- #

class TransporteCloudAPI:
    """
    API oficial do WhatsApp Business. Exige template aprovado pela Meta para
    iniciar conversa. `template` é o nome do template e o texto da mensagem é
    passado como parâmetro do corpo.

    Sem risco de bloqueio por automação, porque é o canal previsto para isso.
    Em troca: precisa de conta Business verificada e cada conversa iniciada por
    template tem custo.
    """

    nome = "Cloud API"
    BASE = "https://graph.facebook.com/v21.0"

    def __init__(self, phone_number_id: str, token: str, template: str,
                 idioma: str = "pt_BR") -> None:
        self.phone_number_id = phone_number_id
        self.token = token
        self.template = template
        self.idioma = idioma

    def preparar(self, deve_parar=None) -> None:
        if not (self.phone_number_id and self.token and self.template):
            raise SessaoPerdida(
                "Faltam phone_number_id, token de acesso ou nome do template."
            )

    def encerrar(self) -> None:
        pass

    def enviar(self, telefone: str, texto: str, deve_parar=None) -> Resultado:
        corpo = {
            "messaging_product": "whatsapp",
            "to": telefone,
            "type": "template",
            "template": {
                "name": self.template,
                "language": {"code": self.idioma},
                "components": [
                    {"type": "body", "parameters": [{"type": "text", "text": texto}]}
                ],
            },
        }
        req = urllib.request.Request(
            f"{self.BASE}/{self.phone_number_id}/messages",
            data=json.dumps(corpo).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                json.loads(r.read())
            return Resultado("enviado")
        except urllib.error.HTTPError as e:
            try:
                detalhe = json.loads(e.read()).get("error", {})
            except Exception:
                detalhe = {}
            codigo = detalhe.get("code")
            mensagem = detalhe.get("message", str(e))
            if codigo in (131026, 131051):          # destinatário não recebe
                return Resultado("invalido", mensagem)
            if codigo in (190, 131031):             # token/conta
                raise SessaoPerdida(f"Credencial ou conta com problema: {mensagem}")
            return Resultado("falhou", mensagem)
        except Exception as e:
            return Resultado("falhou", str(e))


def montar_transporte(config: dict) -> Transporte:
    tipo = (config or {}).get("tipo", "simulado")
    if tipo == "whatsapp_web":
        return TransporteWhatsAppWeb(
            perfil=config.get("perfil", "./.wa_profile"),
            timeout_login=int(config.get("timeout_login", 180)),
        )
    if tipo == "cloud_api":
        return TransporteCloudAPI(
            phone_number_id=config.get("phone_number_id", ""),
            token=config.get("token", ""),
            template=config.get("template", ""),
        )
    return TransporteSimulado()
