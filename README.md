# Assistente pessoal (Telegram + Claude + Microsoft 365)

Bot do Telegram que conversa com você usando o Claude e tem acesso ao seu Outlook (e-mails e agenda).

**O que faz**

- Conversa livre: "o que tenho amanhã?", "tem algo do Leandro sobre o FIDC?", "responde a Vert dizendo que vejo amanhã", "marca 30 min com o Marcelo quinta à tarde".
- Lê e-mails e agenda à vontade. **Toda alteração** (enviar ou responder e-mail, criar evento, responder convite, mover ou categorizar) aparece com os botões ✅ Confirmar / ❌ Cancelar. E-mails ficam como rascunho no Outlook até você confirmar; se cancelar, o rascunho é apagado.
- Rotinas automáticas (seg–sex, horário de Brasília):

| Rotina | Quando | Conteúdo |
|---|---|---|
| Briefing matinal | 07:30 | Agenda, precisa de você, aguardando terceiros, informativo |
| Cobrança de pendências | 14:00 | O que você enviou e ninguém respondeu, e o que chegou e você ainda não respondeu |
| Resumo do fim do dia | 18:30 | O que aconteceu hoje, o que ficou pendente e a agenda de amanhã |
| Preparo de reunião | 15 min antes | Quem participa, contexto dos últimos e-mails e pontos em aberto |

- **Mensagens de voz:** grave um áudio no Telegram e o bot transcreve (OpenAI), mostra o que entendeu e responde como se fosse texto.
- **Painel web** (`/painel`): custos de IA por dia e por tipo de uso, limite mensal com alerta, status das integrações (com "Testar agora" e "Refazer login do Microsoft 365") e liga/desliga e horário das rotinas.
- **Economia de tokens:** e-mails e eventos vão ao Claude em formato compacto (com referências curtas no lugar dos IDs do Outlook), o contexto fica em cache entre as etapas de cada pedido e as rotinas usam o Claude Haiku (configurável no painel).
- Comandos: `/briefing` `/pendencias` `/fimdodia` `/agenda` `/limpar` `/login` `/ajuda`
- Só responde ao seu chat id. Qualquer outra pessoa é ignorada.

---

## Configuração (uma vez, cerca de 30 min)

### 1. Bot do Telegram
1. No Telegram, abra **@BotFather** → `/newbot` → escolha nome e usuário → copie o **token**.
2. Abra **@userinfobot** → `/start` → copie o seu **Id** (número).

### 2. App no Microsoft Entra ID (Azure AD) da LARCA
Precisa de alguém com perfil de administrador do Microsoft 365 da LARCA.

1. Acesse [entra.microsoft.com](https://entra.microsoft.com) → **Aplicativos → Registros de aplicativo → Novo registro**.
   - Nome: `Assistente Matheus`. Tipos de conta: **somente este diretório**. URI de redirecionamento: deixe em branco.
2. Na tela do app, copie o **ID do aplicativo (cliente)** e o **ID do diretório (locatário)**.
3. **Autenticação → Configurações avançadas → Permitir fluxos de clientes públicos = Sim** → Salvar.
4. **Permissões de API → Adicionar → Microsoft Graph → Permissões delegadas**:
   `User.Read`, `Mail.ReadWrite`, `Mail.Send`, `Calendars.ReadWrite`, `offline_access`.
   Depois clique em **Conceder consentimento do administrador**.

São permissões *delegadas*: o bot só enxerga a **sua** caixa, nunca a dos outros usuários.

### 3. Chave do Claude
[console.anthropic.com](https://console.anthropic.com) → **API Keys** → criar. Configure também um limite de gasto mensal em **Billing → Limits**.

### 3b. Chave da OpenAI (só para mensagens de voz)
[platform.openai.com](https://platform.openai.com) → **API keys** → *Create new secret key*. Adicione créditos em **Billing** e defina um limite mensal. No Railway, crie a variável `OPENAI_API_KEY`. Opcional: `TRANSCRICAO_MODELO=gpt-4o-mini-transcribe`, que é mais barato.

### 4. Hospedagem na sua conta Railway (sem servidor da LARCA)
Plano Hobby, a partir de cerca de US$ 5/mês, pago no seu cartão. Confira os valores em [docs.railway.com/pricing](https://docs.railway.com/pricing/plans).

1. Crie um repositório **privado** no GitHub e suba esta pasta (o `.gitignore` já impede o envio do `.env` e da pasta `data/`).
2. Em [railway.com](https://railway.com): **New Project → Deploy from GitHub repo** → escolha o repositório. O build usa o `Dockerfile` e o `railway.toml`.
3. No serviço criado:
   - **Variables → Raw Editor**: cole o conteúdo do `.env.example` preenchido (tokens, IDs e preferências).
   - **Settings → Volumes → New Volume**, com *mount path* `/data`. É ali que ficam o login do Microsoft 365 e o histórico. Sem volume, o login se perde a cada deploy.
4. Faça o deploy. Em 1–2 minutos o bot manda no Telegram: "Estou no ar, mas ainda sem acesso ao seu Outlook".
5. Mande **/login** no Telegram. O bot responde com um link e um código: abra o link, digite o código e entre com sua conta LARCA. Ele avisa quando conectar.

Pronto: mande `/briefing` para testar.

- **Atualizar o código:** faça `git push`, e o Railway refaz o deploy sozinho.
- **Mudar horários ou contatos:** edite em *Variables*. O serviço reinicia sozinho.
- **"Login expirou"** (por exemplo, depois de trocar a senha): mande `/login` de novo.
- **Logs:** aba *Deployments → View logs*.

<details><summary>Alternativa: rodar com Docker em qualquer máquina</summary>

```bash
cp .env.example .env && nano .env
docker compose up -d --build     # depois mande /login no Telegram
```
</details>

---

### 5. Painel web
1. No Railway, em **Variables**, crie `PAINEL_SENHA` com uma senha forte (12+ caracteres).
2. No card do bot, vá em **Settings → Networking → Generate Domain**. Se ele pedir a porta, informe `8080`.
3. Abra `https://<domínio-gerado>/painel` e entre com a senha.

O painel bloqueia por 15 minutos depois de 5 senhas erradas, e a sessão dura 7 dias. Trocar a `PAINEL_SENHA` derruba todas as sessões abertas.

## Segurança e LGPD (leia antes de ligar)
- **O volume `/data` guarda o login do seu e-mail.** Proteja a conta do Railway e a do GitHub com autenticação em dois fatores (2FA) e não convide ninguém para o projeto. O bot não abre nenhuma porta pública, porque usa *polling*.
- Para revogar o acesso a qualquer momento: em [myaccount.microsoft.com](https://myaccount.microsoft.com) → *Aplicativos*, ou peça ao administrador para desativar o app no Entra ID.
- **Telegram:** conversas com bots **não** têm criptografia ponta a ponta; os resumos passam pelos servidores do Telegram. Vale validar com o Jurídico/Compliance se isso é aceitável para e-mails com dados de clientes. Se não for, troque a interface por Teams ou WhatsApp, mantendo o resto do código.
- **Claude API:** o conteúdo dos e-mails consultados é enviado à API da Anthropic para processamento. Pelos termos comerciais da API, esses dados não são usados para treinar modelos. Confirme se isso está coberto na política de privacidade e no registro de operações da LARCA.
- O bot nunca envia nada sem o seu clique em ✅.

## Estrutura
```
assistente/
  config.py        variáveis do .env
  graph.py         Microsoft Graph: e-mail, agenda e login (MSAL)
  agente.py        Claude + ferramentas; ações viram pedidos de confirmação
  rotinas.py       briefing, pendências, fim do dia, preparo de reunião
  bot.py           Telegram: mensagens, botões e agendamento
  armazenamento.py SQLite: histórico, ações pendentes e avisos
tests/             testes com Graph e Claude simulados (python -m pytest -q)
```

## Próximos passos possíveis
- Teams (ler e enviar chats): basta adicionar as permissões `Chat.Read`/`ChatMessage.Send` e novas ferramentas.
- Consultar o vault LARCA_BRAIN para dar contexto nas reuniões.
- WhatsApp como segundo canal, depois da aprovação da conta Meta Business.
