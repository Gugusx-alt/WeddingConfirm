# WeddingConfirm

Confirmação de presença em casamento por WhatsApp, com painel visual, mensagens
que não se repetem e link pessoal para cada convidado responder.

---

## Antes de tudo: dois problemas no repositório atual

**1. A pasta `.wa_profile` está versionada.** É onde o Chrome guarda o perfil com
a sessão do WhatsApp Web logada. Quem clonar o repositório pode conseguir a
sessão. Resolva assim, e nesta ordem:

```bash
# 1. abra o WhatsApp no celular > Aparelhos conectados > desconecte todos
# 2. tire do repositório
git rm -r --cached .wa_profile __pycache__
git commit -m "Remove sessão do WhatsApp e cache do controle de versão"
git push
```

Como já entrou no histórico, remover agora não apaga o passado — por isso o
passo 1 (desconectar os aparelhos) é o que de fato encerra a sessão exposta. Se
quiser limpar o histórico também, `git filter-repo --path .wa_profile --invert-paths`.
O `.gitignore` deste pacote já bloqueia a pasta, o banco e as listas reais.

**2. `__pycache__` versionado.** Mesmo comando acima resolve.

---

## O que mudou

| Problema que você levantou | O que foi feito |
|---|---|
| A cada ~10 mensagens tem que trocar o texto, senão o WhatsApp bloqueia | Motor de variação com spintax. Você escreve 4 versões, o sistema gera centenas de textos distintos e garante que nada se repita numa janela de 25 envios |
| Sem interface visual | Painel web em `/`: escrever mensagens com prévia ao vivo, importar lista, ajustar ritmo, disparar, acompanhar em tempo real |
| Confirmação não era automatizada | Cada convidado recebe um link só dele. Ele responde numa página, e o painel soma confirmados, recusas e total de pessoas na festa. Ninguém precisa ler resposta no WhatsApp |
| Progresso só na memória | Tudo em PostgreSQL. Se o Chrome cair no convidado 90 de 200, você retoma do 91 sem reenviar para ninguém |
| Só variar o texto não evita bloqueio | Controle de ritmo: intervalo irregular, pausa entre lotes, janela de horário, teto diário e curva de aquecimento |
| DDI/nono dígito | Normalização corrigida; erro aparece na importação, não no meio do disparo |

---

## Instalar e rodar

Requer um PostgreSQL rodando localmente (`pacman -S postgresql` no Arch, depois
`initdb`, `systemctl enable --now postgresql` e criar um banco/usuário para o
projeto — veja abaixo).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Por padrão conecta em `postgresql://weddingconfirm:weddingconfirm_dev@localhost/weddingconfirm`.
Para usar outro banco, exporte `DATABASE_URL` antes de rodar:

```bash
export DATABASE_URL="postgresql://usuario:senha@host/banco"
```

Criando o banco local pela primeira vez:

```sql
CREATE ROLE weddingconfirm WITH LOGIN PASSWORD 'weddingconfirm_dev';
CREATE DATABASE weddingconfirm OWNER weddingconfirm;
```

Abre em <http://localhost:8000>.

Para o transporte WhatsApp Web você também precisa do Chrome e do chromedriver
compatível (`pacman -S chromium chromedriver` no Arch, `brew install chromedriver`
no macOS).

---

## Como usar

1. **Crie a campanha** com noivos, data, local e o endereço público do sistema.
   Esse endereço é a base do link de cada convidado; em teste local, `http://localhost:8000`.
2. **Escreva as mensagens.** Comece em "Usar exemplo" e adapte. A prévia à
   direita mostra cinco mensagens como sairiam de verdade, com a impressão
   digital de cada uma, e o medidor diz quantos textos diferentes seus templates
   conseguem gerar.
3. **Importe a lista.** Cole `nome,telefone` por linha ou escolha um CSV. A
   importação corrige DDI, insere o nono dígito, remove duplicados e mostra o
   que ficou de fora e por quê.
4. **Ajuste o ritmo.** O padrão é conservador: 150 por dia, das 9h às 20h30,
   um envio a cada ~32s, pausa longa a cada 18 envios.
5. **Ensaie primeiro.** Deixe o transporte em **Simulação** e rode a campanha
   inteira. Nada é enviado; você confere textos, ritmo e painel. Depois
   `DELETE FROM convidados WHERE campanha_id = ?` ou crie outra campanha para
   valer.
6. **Dispare.** Em WhatsApp Web, o Chrome abre e você lê o QR uma vez.
   Pode pausar, fechar a aba e retomar depois.

Prefere terminal? `python cli.py --mensagens mensagens.exemplo.txt --previa 8`
mostra exemplos, e `--numeros lista.txt --transporte whatsapp_web` dispara. As
duas vias usam o mesmo banco.

---

## Como escrever mensagens que variam

```
{Oi|Olá|Oiê}, {primeiro_nome}! {Tudo bem|Tudo certo}?
{Estamos|Ficamos} muito felizes de {te convidar|convidar você} para o nosso casamento, dia {data}.
{Confirma|Responde} {aqui|neste link}: {link}
```

Cada `{a|b|c}` sorteia uma opção por envio. Esse trecho sozinho gera
3 × 2 × 2 × 2 × 2 × 2 = 192 textos. Com quatro versões dessas, mais de 500.
Aninhar funciona: `{Oi{!| —}|Olá}`.

Campos disponíveis: `{primeiro_nome}`, `{nome}`, `{saudacao}` (bom dia / boa
tarde / boa noite conforme a hora), `{link}`, `{noivos}`, `{data}`, `{local}`.

**Cuidado com concordância.** `{Estamos|A gente está} {felizes|animados}` pode
produzir "A gente está animados". Leia a prévia; ela existe para isso.

Como a garantia funciona: cada texto gerado recebe uma impressão digital
calculada sem acentos, sem pontuação, sem link e sem o nome próprio — então
"Oi, Ana!" e "Oi, Bruno!" contam como o **mesmo** texto. O seletor mantém as
últimas 25 impressões digitais e recusa repetir qualquer uma. Se seus templates
não derem variação suficiente, o painel avisa antes de você disparar, e o resumo
mostra quantos textos acabaram repetidos.

**O que este projeto deliberadamente não faz:** inserir caracteres invisíveis,
trocar letras por homóglifos ou embaralhar pontuação. São truques de spam, não
melhoram a entrega e estragam a mensagem que os seus convidados vão ler.

---

## Sobre bloqueio, e sobre virar um serviço

Vale separar duas coisas.

**Para o seu casamento.** Automatizar o WhatsApp Web com Selenium contraria os
Termos de Serviço do WhatsApp, e o risco de restrição existe mesmo com tudo
bem ajustado. O que mais pesa não é o volume: é **denúncia de destinatário**.
Convidado que reconhece o remetente e esperava a mensagem não denuncia. Por isso:

- Avise antes por outro canal ("vou te mandar o link do casamento no zap").
- Comece pelos contatos que já têm você salvo.
- Use um número que já conversa normalmente, com foto e nome preenchidos.
- Se for número novo, use a curva de aquecimento (20 no primeiro dia, 40 no
  segundo, e assim por diante).
- Deixe o teto diário baixo e o horário comercial. Convite às 3h da manhã é
  denúncia garantida.
- Respeite quem pede para sair. O link tem opção de descadastro, e o painel
  marca a pessoa como `optout`.

**Para outras pessoas usarem.** Aqui o Selenium não serve. Se você oferecer
isso como serviço, seus clientes vão perder os números deles, e a
responsabilidade cai em você. O caminho é a
[WhatsApp Cloud API](https://developers.facebook.com/docs/whatsapp/cloud-api):

- Conta WhatsApp Business verificada pela Meta.
- Templates de mensagem aprovados antes do envio (o texto passa por revisão).
- Custo por conversa iniciada, tabelado por país.
- Em troca: sem risco de bloqueio por automação, entrega confirmada por webhook,
  e resposta do convidado chegando na sua API.

O transporte `cloud_api` já está implementado em `core/transportes.py`. Ponto de
atenção: na Cloud API a variação de texto muda de papel, porque o corpo da
mensagem vem do template aprovado e você só preenche os parâmetros. O motor de
variação continua útil para o parâmetro livre, e todo o resto do sistema —
painel, lista, links de confirmação, relatório — funciona igual.

Se o serviço for hospedado, ainda faltam: login por usuário (hoje qualquer um
que abrir a porta 8000 vê tudo), um usuário por campanha no banco, HTTPS, e a
página de confirmação num domínio próprio, porque link `localhost` não abre no
celular do convidado.

---

## Estrutura

```
app.py                 servidor web: painel, API, página de confirmação
cli.py                 mesma coisa pelo terminal
core/
  variacoes.py         spintax, placeholders, garantia de não repetição
  contatos.py          normalização de telefone e importação de lista
  limites.py           ritmo: intervalos, lotes, horário, teto, aquecimento
  transportes.py       WhatsApp Web (Selenium), Cloud API, simulado
  executor.py          roda a campanha em thread, com pausar/retomar
  db.py                PostgreSQL
web/
  painel.html          painel dos noivos
  confirmar.html       página que o convidado abre
```

## Licença

MIT, como antes.
