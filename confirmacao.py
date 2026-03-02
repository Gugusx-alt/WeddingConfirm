"""
Wrapper simples para chamar o script de envio via WhatsApp Web.

Uso:
  python confirmacao.py --numbers-file examples/numeros.txt --message-file examples/mensagem.txt
"""

from send_whatsapp import main


if __name__ == "__main__":
    main()