from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time

numeros = ["5546999068666, 5546999207515, 5546999170360"]

mensagem = "Olá! Aqui é a confirmação da sua presença no casamento. Você poderá comparecer?"


driver = webdriver.Chrome()
driver.get("https://web.whatsapp.com")

print("Aguardando o WhatsApp Web carregar...")
WebDriverWait(driver, 60).until(
    EC.presence_of_element_located((By.XPATH, '//div[@role="grid"]'))
)
print("WhatsApp Web carregado.")


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

        # Verifica se aparece o botão "OK" (número inexistente)
        try:
            botao_ok = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.XPATH, '//div[@role="button" and .="OK"]'))
            )
            botao_ok.click()
            print(f"Número {numero} não existe no WhatsApp. Informando no grupo e pulando para o próximo.")

            # Fecha a aba e volta para o grupo
            driver.close()
            driver.switch_to.window(driver.window_handles[0])
            time.sleep(1)

            # Volta para o grupo "Apenas eu"
            grupo = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, '//*[contains(@title, "Apenas eu")]'))
            )
            grupo.click()
            time.sleep(2)

            # Envia mensagem de erro no grupo
            caixa_msg = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.XPATH, '//footer//div[@contenteditable="true"]'))
            )
            caixa_msg.click()
            caixa_msg.send_keys(f"O número {numero} não existe no WhatsApp.")
            caixa_msg.send_keys(Keys.ENTER)
            time.sleep(2)
            continue  # Pula para o próximo número
        except Exception as e:
            pass  # Não encontrou o botão OK, segue normalmente

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

        # Volta para o grupo "Apenas eu" antes de ir para o próximo número
        try:
            grupo = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, '//*[contains(@title, "Apenas eu")]'))
            )
            grupo.click()
            time.sleep(2)
            print("Voltou para o grupo 'Apenas eu'.")
        except Exception as e:
            print(f"Erro ao voltar para o grupo: {e}")
            continue

    except Exception as e:
        print(f"Erro ao processar {numero}: {e}")
        continue

print("Tudo pronto!")
driver.quit()