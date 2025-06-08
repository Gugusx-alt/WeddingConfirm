from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time

# Lista de números
numeros = ["+5546999068666", "+5546999352280"]
mensagem = "Olá! Aqui é a confirmação da sua presença no casamento. Você poderá comparecer?"

# Inicia o navegador e abre o WhatsApp Web
driver = webdriver.Chrome()
driver.get("https://web.whatsapp.com")

print("Aguardando o WhatsApp Web carregar...")
WebDriverWait(driver, 60).until(
    EC.presence_of_element_located((By.XPATH, '//div[@role="grid"]'))
)
print("WhatsApp Web carregado.")

# Acessa o grupo "Apenas eu"
try:
    grupo = WebDriverWait(driver, 30).until(
        EC.presence_of_element_located((By.XPATH, '//*[contains(@title, "Apenas eu")]'))
    )
    grupo.click()
    time.sleep(3)
    print("Grupo 'Apenas eu' localizado e clicado.")
except Exception as e:
    print(f"Erro ao localizar o grupo: {e}")
    driver.quit()

# Envia os links wa.me no grupo
try:
    caixa_msg = WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.XPATH, '//footer//div[@contenteditable="true"]'))
    )
    for numero in numeros:
        link = f"wa.me/{numero[1:]}"
        caixa_msg.send_keys(link)
        caixa_msg.send_keys(Keys.SHIFT, Keys.ENTER)
    time.sleep(1)
    caixa_msg.send_keys(Keys.ENTER)
    time.sleep(3)
    print("Links wa.me enviados no grupo.")
except Exception as e:
    print(f"Erro ao enviar os links: {e}")
    driver.quit()

# Clica no link e envia mensagem
for numero in numeros:
    try:
        link = f"wa.me/{numero[1:]}"
        try:
            elemento_link = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, f'//a[contains(@href, "{link}")]'))
            )
            elemento_link.click()
            print(f"Cliquei no link do número {numero}")
            time.sleep(2)
        except Exception as e:
            print(f"Erro ao clicar no link do número {numero}: {e}")
            continue

        # Troca para nova aba
        driver.switch_to.window(driver.window_handles[-1])

        # Aguarda e envia mensagem
        try:
            campo_mensagem = WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((By.XPATH, '//footer//div[@contenteditable="true"]'))
            )
            campo_mensagem.click()
            campo_mensagem.send_keys(mensagem)
            campo_mensagem.send_keys(Keys.ENTER)
            print(f"Mensagem enviada para {numero}")
            time.sleep(2)
        except Exception as e:
            print(f"Erro ao enviar mensagem para {numero}: {e}")
            continue

        # Fecha a aba e volta
        if len(driver.window_handles) > 1:
            driver.close()
            driver.switch_to.window(driver.window_handles[0])
            time.sleep(1)

    except Exception as e:
        print(f"Erro ao processar {numero}: {e}")
        continue

print("Tudo pronto!")
driver.quit()
