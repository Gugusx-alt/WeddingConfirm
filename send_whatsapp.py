import argparse
import os
import random
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


WHATSAPP_WEB_URL = "https://web.whatsapp.com/"

# Textos que indicam número inválido/não registrado no WhatsApp
INVALID_NUMBER_SNIPPETS = [
    # Inglês
    "phone number shared via url is invalid",
    "phone number is invalid",
    "invalid phone number",
    "this phone number is not registered",
    "phone number not registered",
    "number is not on whatsapp",
    "this number is not on whatsapp",
    # Português
    "número de telefone compartilhado via link é inválido",
    "número de telefone compartilhado via url é inválido",
    "número de telefone é inválido",
    "número inválido",
    "número de telefone não é válido",
    "este número de telefone não está registrado",
    "número não está registrado",
    "este número não está no whatsapp",
    "número não está no whatsapp",
    "não está no whatsapp",
    "não possui whatsapp",
    "número incorreto",
    "número não encontrado",
]


@dataclass(frozen=True)
class Contact:
    phone_e164_digits: str
    name: str | None = None


def _only_digits(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def normalize_phone(raw: str, default_ddi: str) -> str:
    """
    Retorna o telefone em formato E.164 sem '+', ex: '5511999999999'.

    CORREÇÃO: A lógica anterior adicionava DDI em números que já tinham DDI
    mas cujo comprimento coincidia com os intervalos (8-11 dígitos). Agora a
    verificação de prefixo é feita antes de qualquer tentativa de adição.
    """
    digits = _only_digits(raw)
    if not digits:
        raise ValueError("telefone vazio")

    # Remove prefixo internacional '00' (ex: 005511...)
    if digits.startswith("00") and len(digits) > 2:
        digits = digits[2:]

    # Só adiciona DDI se o número claramente não o possui
    if default_ddi and not digits.startswith(default_ddi):
        # Comprimentos locais típicos (sem DDI): 8-11 dígitos
        # Se já tem comprimento de número completo (>=12), não adiciona DDI
        if len(digits) <= 11:
            digits = f"{default_ddi}{digits}"

    if len(digits) < 10:
        raise ValueError(f"telefone curto demais: {digits!r}")

    return digits


def read_contacts(path: str, default_ddi: str) -> list[Contact]:
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    contacts: list[Contact] = []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            phone_part = line
            name_part = None
            if "," in line:
                phone_part, name_part = [p.strip() for p in line.split(",", 1)]
            try:
                phone = normalize_phone(phone_part, default_ddi=default_ddi)
            except ValueError as e:
                print(f"  Aviso: linha {lineno} ignorada ({e}): {line!r}")
                continue
            contacts.append(Contact(phone_e164_digits=phone, name=name_part or None))

    seen: set[str] = set()
    unique: list[Contact] = []
    for c in contacts:
        if c.phone_e164_digits in seen:
            continue
        seen.add(c.phone_e164_digits)
        unique.append(c)
    return unique


def render_message(template: str, contact: Contact) -> str:
    name = contact.name or ""
    return template.replace("{nome}", name).replace("{name}", name)


def build_send_url(phone_e164_digits: str, message: str) -> str:
    encoded_text = urllib.parse.quote(message)
    return (
        "https://web.whatsapp.com/send"
        f"?phone={phone_e164_digits}&text={encoded_text}&type=phone_number&app_absent=0"
    )


def wait_for_login(driver: webdriver.Chrome, timeout_s: int) -> None:
    """
    CORREÇÃO: Agora captura TimeoutException e relança com mensagem amigável,
    evitando traceback confuso para o usuário.
    """
    driver.get(WHATSAPP_WEB_URL)
    try:
        WebDriverWait(driver, timeout_s).until(
            EC.any_of(
                EC.presence_of_element_located((By.XPATH, '//div[@id="side"]')),
                EC.presence_of_element_located((By.XPATH, '//div[@role="grid"]')),
                EC.presence_of_element_located((By.XPATH, '//*[@data-testid="chat-list"]')),
            )
        )
    except TimeoutException:
        raise SystemExit(
            f"Timeout de {timeout_s}s atingido aguardando login. "
            "Escaneie o QR Code dentro do tempo limite ou aumente --login-timeout."
        )


def _get_element_text(element) -> str:
    """Tenta obter o texto de um elemento de múltiplas formas."""
    for method in (
        lambda el: el.text,
        lambda el: el.get_attribute("innerText"),
        lambda el: el.get_attribute("textContent"),
    ):
        try:
            text = (method(element) or "").strip().lower()
            if text:
                return text
        except Exception:
            continue
    return ""


def _try_close_dialog(dialog_element) -> None:
    """Tenta fechar um diálogo clicando em botão OK/Fechar."""
    ok_selectors = [
        './/div[@role="button" and (normalize-space()="OK" or normalize-space()="Ok" or normalize-space()="ok")]',
        './/button[normalize-space()="OK" or normalize-space()="Ok" or normalize-space()="ok"]',
        './/span[normalize-space()="OK" or normalize-space()="Ok" or normalize-space()="ok"]',
        './/div[contains(@class, "button") and (normalize-space()="OK" or normalize-space()="Ok")]',
    ]
    for selector in ok_selectors:
        try:
            btn = dialog_element.find_element(By.XPATH, selector)
            if btn.is_displayed():
                btn.click()
                time.sleep(0.5)
                return
        except Exception:
            continue


def is_invalid_number_dialog_present(driver: webdriver.Chrome) -> bool:
    """
    Detecta diálogo de número inválido/não registrado no WhatsApp.

    CORREÇÃO: Os seletores de `error_elements` foram separados em chamadas
    individuais de `find_elements`, pois o operador `|` no XPath dentro de
    um único `find_elements` pode falhar silenciosamente em alguns ChromeDrivers.
    """
    # Coleta elementos candidatos com seletores separados (mais confiável)
    candidate_selectors = [
        (By.XPATH, '//div[@role="dialog"]'),
        (By.XPATH, '//div[@role="alert"]'),
        (By.XPATH, '//div[contains(@class, "error")]'),
        (By.XPATH, '//div[contains(@class, "invalid")]'),
        (By.XPATH, '//span[contains(@class, "error")]'),
        (By.XPATH, '//span[contains(@class, "invalid")]'),
    ]

    seen_ids: set[str] = set()
    candidates = []
    for by, selector in candidate_selectors:
        try:
            elements = driver.find_elements(by, selector)
            for el in elements:
                try:
                    el_id = el.id  # ID interno do WebDriver, único por elemento
                    if el_id not in seen_ids:
                        seen_ids.add(el_id)
                        candidates.append(el)
                except Exception:
                    candidates.append(el)
        except Exception:
            continue

    for element in candidates:
        text = _get_element_text(element)
        if not text:
            continue
        if any(snippet in text for snippet in INVALID_NUMBER_SNIPPETS):
            print(f"    Debug: Diálogo de número inválido detectado: {text[:100]!r}")
            _try_close_dialog(element)
            return True

    return False


def _find_send_button(driver: webdriver.Chrome, textbox):
    """
    Procura o botão de enviar no footer, excluindo botões de anexos.
    Retorna o elemento se encontrado, ou None.
    """
    send_selectors = [
        '//footer//span[@data-icon="send"]',
        '//footer//button[@data-testid="send"]',
        '//footer//div[@role="button" and @data-testid="send"]',
        '//span[@data-icon="send"]',
        '//*[@data-testid="send"]',
    ]
    attach_icons = {"attach", "plus", "clip", "document", "poll"}

    try:
        textbox_x = textbox.rect.get("x", 0)
    except Exception:
        textbox_x = 0

    for selector in send_selectors:
        try:
            buttons = driver.find_elements(By.XPATH, selector)
            for btn in buttons:
                try:
                    if not btn.is_displayed():
                        continue
                    icon = btn.get_attribute("data-icon") or ""
                    aria_label = (btn.get_attribute("aria-label") or "").lower()
                    if icon in attach_icons:
                        continue
                    if any(w in aria_label for w in ("attach", "anex", "document")):
                        continue
                    # Botão de enviar deve estar à direita do campo de texto
                    try:
                        if btn.rect.get("x", 0) < textbox_x:
                            continue
                    except Exception:
                        pass
                    return btn
                except Exception:
                    continue
        except Exception:
            continue
    return None


def try_click_send(driver: webdriver.Chrome, timeout_s: int) -> None:
    """
    Envia a mensagem pré-preenchida na URL.

    CORREÇÕES:
    - Verifica se o campo tem conteúdo antes de enviar, alertando se estiver vazio.
    - Respeita timeout_s nos sleeps (sleeps fixos não ultrapassam o timeout).
    - Lança exceção descritiva se não conseguir enviar.
    """
    wait = WebDriverWait(driver, timeout_s)

    textbox = wait.until(
        EC.presence_of_element_located((By.XPATH, '//footer//div[@contenteditable="true"]'))
    )

    # Aguarda preenchimento da mensagem (máx. 30% do timeout ou 5s)
    prefill_wait = min(5.0, timeout_s * 0.3)
    time.sleep(prefill_wait)

    # CORREÇÃO: Verifica conteúdo antes de enviar para não enviar mensagem vazia
    try:
        text_content = (
            textbox.text
            or driver.execute_script(
                "return arguments[0].innerText || arguments[0].textContent;", textbox
            )
            or ""
        )
        if not text_content.strip():
            raise Exception(
                "Campo de mensagem está vazio após pré-preenchimento da URL. "
                "A mensagem NÃO foi enviada para evitar envio em branco."
            )
        print(f"    Debug: Mensagem pré-preenchida ({len(text_content)} chars)")
    except Exception as e:
        if "vazio" in str(e):
            raise
        print(f"    Debug: Não foi possível verificar conteúdo do campo: {e}")

    # Foca no campo
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", textbox)
        time.sleep(0.3)
        textbox.click()
        time.sleep(0.3)
        driver.execute_script("arguments[0].focus();", textbox)
        time.sleep(0.3)
    except Exception as e:
        print(f"    Debug: Erro ao focar no campo: {e}")

    # Tenta clicar no botão de enviar
    btn = _find_send_button(driver, textbox)
    if btn:
        for click_fn in (
            lambda: btn.click(),
            lambda: ActionChains(driver).move_to_element(btn).click().perform(),
            lambda: driver.execute_script("arguments[0].click();", btn),
        ):
            try:
                click_fn()
                print("    Debug: Mensagem enviada via botão de enviar")
                time.sleep(2)
                return
            except Exception:
                continue

    # Fallback: Enter no campo de texto
    print("    Debug: Botão não encontrado, enviando com Enter...")
    try:
        textbox.click()
        time.sleep(0.3)
        driver.execute_script("arguments[0].focus();", textbox)
        time.sleep(0.3)
        textbox.send_keys(Keys.ENTER)
        time.sleep(1)
        print("    Debug: Mensagem enviada via Enter")
    except Exception as e:
        raise Exception(f"Não foi possível enviar a mensagem: {e}")


def create_driver(profile_dir: str | None, headless: bool) -> webdriver.Chrome:
    options = webdriver.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-notifications")
    options.add_argument("--start-maximized")

    if profile_dir:
        os.makedirs(profile_dir, exist_ok=True)
        options.add_argument(f"--user-data-dir={profile_dir}")

    return webdriver.Chrome(options=options)


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Enviar mensagem para lista de números via WhatsApp Web (Selenium)."
    )
    p.add_argument("--numbers-file", required=True, help="Arquivo .txt (1 número por linha).")
    p.add_argument("--message-file", required=True, help="Arquivo de texto com a mensagem.")
    p.add_argument("--default-ddi", default="55", help="DDI padrão para números sem DDI. Ex: 55 (Brasil).")
    p.add_argument("--profile-dir", default="./.wa_profile", help="Diretório do perfil do Chrome para manter login.")
    p.add_argument("--login-timeout", type=int, default=120, help="Tempo (s) para escanear o QR.")
    p.add_argument("--per-chat-timeout", type=int, default=30, help="Timeout (s) por chat.")
    p.add_argument("--delay-min", type=float, default=2.0, help="Delay mínimo entre envios (s).")
    p.add_argument("--delay-max", type=float, default=5.0, help="Delay máximo entre envios (s).")
    p.add_argument("--headless", action="store_true", help="Rodar sem janela (pode falhar no WhatsApp Web).")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv or sys.argv[1:])

    with open(args.message_file, "r", encoding="utf-8") as f:
        template = f.read().strip("\n")

    contacts = read_contacts(args.numbers_file, default_ddi=str(args.default_ddi))
    if not contacts:
        raise SystemExit("Nenhum contato válido encontrado no arquivo.")

    try:
        driver = create_driver(profile_dir=args.profile_dir, headless=bool(args.headless))
    except WebDriverException as e:
        raise SystemExit(
            "Não consegui iniciar o Chrome/Chromium via Selenium.\n"
            "No Arch, normalmente resolve com: pacman -S chromium chromedriver\n"
            f"Erro: {e}"
        )

    sent = 0
    failed = 0

    try:
        print("Abrindo WhatsApp Web (se for a primeira vez, escaneie o QR Code)...")
        wait_for_login(driver, timeout_s=int(args.login_timeout))
        print("Login OK. Começando envios...")

        for idx, c in enumerate(contacts, start=1):
            msg = render_message(template, c)
            url = build_send_url(c.phone_e164_digits, msg)
            print(f"[{idx}/{len(contacts)}] Processando {c.phone_e164_digits}...")

            try:
                driver.get(url)
            except Exception as e:
                print(f"  ✗ Erro ao abrir URL: {e}")
                failed += 1
                # CORREÇÃO: aplica delay mesmo em falha para evitar flood
                time.sleep(random.uniform(float(args.delay_min), float(args.delay_max)))
                continue

            # Verificação inicial de número inválido
            time.sleep(2)
            if is_invalid_number_dialog_present(driver):
                print(f"  ⚠ Número inválido ou sem WhatsApp: {c.phone_e164_digits}")
                failed += 1
                try:
                    driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
                    time.sleep(0.5)
                except Exception:
                    pass
                # CORREÇÃO: aplica delay mesmo em falha
                time.sleep(random.uniform(float(args.delay_min), float(args.delay_max)))
                continue

            # Aguarda o campo de texto (chat pronto)
            try:
                WebDriverWait(driver, int(args.per_chat_timeout)).until(
                    EC.presence_of_element_located(
                        (By.XPATH, '//footer//div[@contenteditable="true"]')
                    )
                )
                time.sleep(2)
            except TimeoutException:
                if is_invalid_number_dialog_present(driver):
                    print(f"  ⚠ Número inválido ou sem WhatsApp: {c.phone_e164_digits}")
                else:
                    print(f"  ✗ Timeout ao abrir chat: {c.phone_e164_digits}")
                failed += 1
                time.sleep(random.uniform(float(args.delay_min), float(args.delay_max)))
                continue

            # Segunda verificação após carregar
            if is_invalid_number_dialog_present(driver):
                print(f"  ⚠ Número inválido ou sem WhatsApp: {c.phone_e164_digits}")
                failed += 1
                time.sleep(random.uniform(float(args.delay_min), float(args.delay_max)))
                continue

            # Envia a mensagem
            try:
                try_click_send(driver, timeout_s=int(args.per_chat_timeout))

                # Verificação pós-envio
                time.sleep(1)
                if is_invalid_number_dialog_present(driver):
                    print(f"  ⚠ Número inválido ou sem WhatsApp (pós-envio): {c.phone_e164_digits}")
                    failed += 1
                else:
                    sent += 1
                    print(f"  ✓ Mensagem enviada para {c.phone_e164_digits}")
            except Exception as e:
                if is_invalid_number_dialog_present(driver):
                    print(f"  ⚠ Número inválido ou sem WhatsApp: {c.phone_e164_digits}")
                else:
                    print(f"  ✗ Falhou ao enviar para {c.phone_e164_digits}: {e}")
                failed += 1

            time.sleep(random.uniform(float(args.delay_min), float(args.delay_max)))

        print(f"\nFinalizado. Enviados: {sent} | Falhas: {failed} | Total: {len(contacts)}")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()