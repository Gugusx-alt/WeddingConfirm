#!/usr/bin/env python3
"""
Compatibilidade com a versão antiga.

Os argumentos --numbers-file / --message-file continuam funcionando e são
traduzidos para o cli.py novo. Prefira `python cli.py` ou `python app.py`.
"""
import sys

MAPA = {
    "--numbers-file": "--numeros",
    "--message-file": "--mensagens",
    "--default-ddi": "--ddi",
    "--profile-dir": "--perfil",
    "--delay-min": None,      # substituído por --intervalo
    "--delay-max": None,
    "--login-timeout": None,
    "--per-chat-timeout": None,
    "--headless": None,
}


def traduzir(argv):
    saida, pular = [], False
    for i, arg in enumerate(argv):
        if pular:
            pular = False
            continue
        chave = arg.split("=")[0]
        if chave in MAPA:
            novo = MAPA[chave]
            if novo is None:
                print(f"  aviso: {chave} não existe mais; veja README.md")
                pular = "=" not in arg and chave != "--headless"
                continue
            saida.append(arg.replace(chave, novo))
        else:
            saida.append(arg)
    return saida


if __name__ == "__main__":
    from cli import main
    argv = traduzir(sys.argv[1:])
    if "--transporte" not in " ".join(argv):
        argv += ["--transporte", "whatsapp_web"]
    print("Este script agora é um atalho para cli.py.\n")
    raise SystemExit(main(argv))
