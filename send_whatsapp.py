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


@dataclass(frozen=True)
class Contact:
    phone_e164_digits: str
    name: str | None = None


def _only_digits(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def normalize_phone(raw: str, default_ddi: str) -> str:
    """
    Telefone em E.164 sem +, ex: '5511999999999'.
    """
    digits = _only_digits(raw)
    if not digits:
        raise ValueError("telefone vazio")

    if digits.startswith("00") and len(digits) > 2:
        digits = digits[2:]

    if default_ddi and not digits.startswith(default_ddi):
        if default_ddi == "55" and len(digits) in (10, 11):
            digits = f"{default_ddi}{digits}"
        elif len(digits) in (8, 9, 10, 11):
            digits = f"{default_ddi}{digits}"

    if len(digits) < 10:
        raise ValueError(f"telefone curto demais: {digits!r}")

    return digits


def read_contacts(path: str, default_ddi: str) -> list[Contact]:
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    contacts: list[Contact] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            phone_part = line
            name_part = None
            if "," in line:
                phone_part, name_part = [p.strip() for p in line.split(",", 1)]
            try:
                phone = normalize_phone(phone_part, default_ddi=default_ddi)
            except ValueError:
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
    driver.get(WHATSAPP_WEB_URL)
    wait = WebDriverWait(driver, timeout_s)
    wait.until(
        EC.any_of(
            EC.presence_of_element_located((By.XPATH, '//div[@id="side"]')),
            EC.presence_of_element_located((By.XPATH, '//div[@role="grid"]')),
            EC.presence_of_element_located((By.XPATH, '//*[@data-testid="chat-list"]')),
        )
    )


def is_invalid_number_dialog_present(driver: webdriver.Chrome) -> bool:
    """
    Detecta se há um diálogo indicando que o número é inválido ou não existe no WhatsApp.
    Retorna True se encontrar, e tenta fechar o diálogo automaticamente.
    """
    # Procura por diálogos de erro
    dialogs = driver.find_elements(By.XPATH, '//div[@role="dialog"]')
    
    # Também procura por mensagens de erro em outros elementos
    error_elements = driver.find_elements(By.XPATH, 
        '//div[contains(@class, "error") or contains(@class, "invalid")] | '
        '//span[contains(@class, "error") or contains(@class, "invalid")] | '
        '//div[@role="alert"]'
    )

    # Lista expandida de textos que indicam número inválido/não existe
    invalid_snippets = [
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
        # Outras variações
        "número incorreto",
        "número não encontrado",
    ]

    all_elements = list(dialogs) + list(error_elements)
    
    for element in all_elements:
        try:
            # Tenta obter o texto de várias formas
            text = ""
            try:
                text = (element.text or "").strip().lower()
            except Exception:
                pass
            
            if not text:
                try:
                    text = (element.get_attribute("innerText") or "").strip().lower()
                except Exception:
                    pass
            
            if not text:
                try:
                    text = (element.get_attribute("textContent") or "").strip().lower()
                except Exception:
                    continue
            
            # Verifica se contém algum dos snippets de erro
            if any(snippet.lower() in text for snippet in invalid_snippets):
                print(f"    Debug: Detectado diálogo de número inválido: {text[:100]}")
                # Tenta fechar o diálogo clicando em OK/Fechar
                try:
                    # Procura botão OK em vários formatos
                    ok_selectors = [
                        './/div[@role="button" and (normalize-space()="OK" or normalize-space()="Ok" or normalize-space()="ok")]',
                        './/button[normalize-space()="OK" or normalize-space()="Ok" or normalize-space()="ok"]',
                        './/span[normalize-space()="OK" or normalize-space()="Ok" or normalize-space()="ok"]',
                        './/div[contains(@class, "button") and (normalize-space()="OK" or normalize-space()="Ok")]',
                    ]
                    for selector in ok_selectors:
                        try:
                            ok_btn = element.find_element(By.XPATH, selector)
                            if ok_btn.is_displayed():
                                ok_btn.click()
                                time.sleep(0.5)
                                break
                        except Exception:
                            continue
                except Exception:
                    pass
                return True
        except Exception:
            continue

    return False


def try_click_send(driver: webdriver.Chrome, timeout_s: int) -> None:
    """
    Tenta enviar a mensagem que já está pré-preenchida na URL.
    Aguarda o campo de texto aparecer, garante que tem conteúdo, e tenta enviar.
    """
    wait = WebDriverWait(driver, timeout_s)
    
    # Aguarda o campo de texto aparecer
    textbox = wait.until(
        EC.presence_of_element_located((By.XPATH, '//footer//div[@contenteditable="true"]'))
    )
    
    # Aguarda mais tempo para garantir que a mensagem foi pré-preenchida
    time.sleep(3)
    
    # Verifica se o campo tem conteúdo
    try:
        text_content = textbox.text or textbox.get_attribute("innerText") or ""
        if not text_content.strip():
            # Tenta obter o texto de outra forma
            text_content = driver.execute_script("return arguments[0].innerText || arguments[0].textContent;", textbox)
        print(f"    Debug: Conteúdo do campo de texto: {text_content[:50]}...")
    except Exception as e:
        print(f"    Debug: Erro ao ler conteúdo do campo: {e}")
    
    # Foca no campo de texto
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", textbox)
        time.sleep(0.5)
        textbox.click()
        time.sleep(0.5)
        # Tenta focar via JavaScript também
        driver.execute_script("arguments[0].focus();", textbox)
        time.sleep(0.5)
    except Exception as e:
        print(f"    Debug: Erro ao focar no campo: {e}")
    
    # Tenta encontrar o botão de enviar - seletores específicos que evitam o botão de anexos
    # O botão de enviar geralmente tem data-icon="send" e fica à direita do campo de texto
    send_button_selectors = [
        # Mais específico: botão de enviar no footer, excluindo botões de anexos
        ('xpath', '//footer//span[@data-icon="send"]'),
        ('xpath', '//footer//button[@data-testid="send"]'),
        ('xpath', '//footer//div[@role="button" and @data-testid="send"]'),
        ('xpath', '//span[@data-icon="send" and not(ancestor::*[@data-icon="attach" or @data-icon="plus" or @data-icon="clip"])]'),
        ('xpath', '//*[@data-testid="send" and not(ancestor::*[@data-icon="attach" or @data-icon="plus" or @data-icon="clip"])]'),
        # Seletores mais genéricos, mas verificando que não é botão de anexos
        ('xpath', '//span[@data-icon="send"]'),
        ('xpath', '//*[@data-testid="send"]'),
    ]
    
    button_found = False
    for selector_type, selector in send_button_selectors:
        try:
            if selector_type == 'xpath':
                buttons = driver.find_elements(By.XPATH, selector)
            else:
                buttons = [driver.find_element(By.CSS_SELECTOR, selector)]
            
            # Filtra botões que não são de anexos
            for btn in buttons:
                if not btn.is_displayed():
                    continue
                
                # Verifica se não é botão de anexos verificando atributos e posição
                try:
                    # Pega o ícone do botão
                    icon = btn.get_attribute("data-icon") or ""
                    # Pega o aria-label
                    aria_label = btn.get_attribute("aria-label") or ""
                    # Verifica se é botão de anexos
                    if icon in ["attach", "plus", "clip", "document", "poll"]:
                        print(f"    Debug: Pulando botão de anexos (icon={icon})")
                        continue
                    if "attach" in aria_label.lower() or "anex" in aria_label.lower() or "document" in aria_label.lower():
                        print(f"    Debug: Pulando botão de anexos (label={aria_label})")
                        continue
                    
                    # Verifica se está próximo ao campo de texto (à direita)
                    try:
                        textbox_rect = textbox.rect
                        btn_rect = btn.rect
                        # O botão de enviar deve estar à direita do campo de texto
                        if btn_rect['x'] < textbox_rect['x']:
                            print(f"    Debug: Botão está à esquerda do campo, pode ser anexos")
                            continue
                    except Exception:
                        pass
                    
                    print(f"    Debug: Botão de enviar encontrado (icon={icon}, label={aria_label})")
                    
                    # Tenta múltiplas formas de clicar
                    try:
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                        time.sleep(0.3)
                        btn.click()
                        print("    Debug: Clique direto funcionou")
                        button_found = True
                        break
                    except Exception:
                        try:
                            ActionChains(driver).move_to_element(btn).click().perform()
                            print("    Debug: Clique via ActionChains funcionou")
                            button_found = True
                            break
                        except Exception:
                            try:
                                driver.execute_script("arguments[0].click();", btn)
                                print("    Debug: Clique via JavaScript funcionou")
                                button_found = True
                                break
                            except Exception as e:
                                print(f"    Debug: Erro ao clicar no botão: {e}")
                                continue
                except Exception as e:
                    print(f"    Debug: Erro ao verificar botão: {e}")
                    continue
            
            if button_found:
                break
                
        except Exception as e:
            print(f"    Debug: Erro ao buscar botão com {selector}: {e}")
            continue
    
    if button_found:
        time.sleep(2)  # Aguarda confirmação do envio
        return
    
    # Se não encontrou o botão, tenta enviar com Enter (mais confiável)
    if not button_found:
        print("    Debug: Botão não encontrado, tentando Enter no campo de texto...")
        try:
            # Garante que o campo está focado
            textbox.click()
            time.sleep(0.5)
            driver.execute_script("arguments[0].focus();", textbox)
            time.sleep(0.5)
            
            # Envia Enter - esta é a forma mais confiável
            textbox.send_keys(Keys.ENTER)
            time.sleep(1)
            
            # Verifica se a mensagem foi enviada (o campo deve ficar vazio)
            time.sleep(1)
            print("    Debug: Enter enviado no campo de texto")
        except Exception as e:
            raise Exception(f"Não consegui enviar a mensagem. Erro: {e}")


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
    p = argparse.ArgumentParser(description="Enviar mensagem para lista de números via WhatsApp Web (Selenium).")
    p.add_argument("--numbers-file", required=True, help="Arquivo .txt (1 número por linha).")
    p.add_argument("--message-file", required=True, help="Arquivo de texto com a mensagem.")
    p.add_argument("--default-ddi", default="55", help="DDI padrão para números sem DDI. Ex: 55 (Brasil).")
    p.add_argument("--profile-dir", default="./.wa_profile", help="Diretório do perfil do Chrome para manter login.")
    p.add_argument("--login-timeout", type=int, default=120, help="Tempo (s) para escanear o QR.")
    p.add_argument("--per-chat-timeout", type=int, default=30, help="Timeout (s) por chat.")
    p.add_argument("--delay-min", type=float, default=2.0, help="Delay mínimo entre envios (s).")
    p.add_argument("--delay-max", type=float, default=5.0, help="Delay máximo entre envios (s).")
    p.add_argument("--headless", action="store_true", help="Tentar rodar sem janela (pode falhar no WhatsApp Web).")
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
                continue

            # Verifica primeiro se é número inválido (antes de esperar o campo de texto)
            time.sleep(2)  # Aguarda um pouco para o WhatsApp carregar
            if is_invalid_number_dialog_present(driver):
                print(f"  ⚠ Número inválido ou sem WhatsApp: {c.phone_e164_digits}")
                failed += 1
                # Aguarda um pouco e tenta fechar qualquer diálogo restante
                time.sleep(1)
                try:
                    # Tenta pressionar ESC para fechar diálogos
                    driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
                    time.sleep(0.5)
                except Exception:
                    pass
                continue

            # Aguarda o campo de texto aparecer (indica que o chat está pronto)
            try:
                wait = WebDriverWait(driver, int(args.per_chat_timeout))
                wait.until(
                    EC.presence_of_element_located((By.XPATH, '//footer//div[@contenteditable="true"]'))
                )
                # Aguarda mais um pouco para garantir que a mensagem foi pré-preenchida
                time.sleep(2)
            except TimeoutException:
                # Se deu timeout, verifica se é porque o número é inválido
                if is_invalid_number_dialog_present(driver):
                    print(f"  ⚠ Número inválido ou sem WhatsApp: {c.phone_e164_digits}")
                else:
                    print(f"  ✗ Timeout ao abrir chat: {c.phone_e164_digits}")
                failed += 1
                continue

            # Verifica novamente se apareceu algum diálogo de erro após carregar
            if is_invalid_number_dialog_present(driver):
                print(f"  ⚠ Número inválido ou sem WhatsApp: {c.phone_e164_digits}")
                failed += 1
                continue

            # Tenta enviar a mensagem
            try:
                try_click_send(driver, timeout_s=int(args.per_chat_timeout))
                
                # Verifica se após enviar apareceu algum erro (número inválido, etc)
                time.sleep(1)
                if is_invalid_number_dialog_present(driver):
                    print(f"  ⚠ Número inválido ou sem WhatsApp: {c.phone_e164_digits}")
                    failed += 1
                    continue
                
                sent += 1
                print(f"  ✓ Mensagem enviada para {c.phone_e164_digits}")
            except Exception as e:
                # Verifica se o erro foi por número inválido
                if is_invalid_number_dialog_present(driver):
                    print(f"  ⚠ Número inválido ou sem WhatsApp: {c.phone_e164_digits}")
                else:
                    print(f"  ✗ Falhou ao enviar para {c.phone_e164_digits}: {e}")
                failed += 1
                continue

            delay = random.uniform(float(args.delay_min), float(args.delay_max))
            time.sleep(delay)

        print(f"Finalizado. Enviados: {sent} | Falhas: {failed}")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
