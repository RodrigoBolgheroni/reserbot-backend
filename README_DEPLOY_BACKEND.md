# Deploy Backend - Render

Esta pasta contem o backend Python do ReservaBot pronto para rodar separado do front.

## Estrutura

- `main.py`: entrypoint de producao.
- `scripts/config_server.py`: servidor HTTP/API.
- `services/`: modulos de negocio, providers Gemini/Groq, Supabase, WhatsApp Cloud API, PDF, perfis e reservas.
- `supabase/schema.sql`: schema que deve ser rodado no Supabase antes do uso real.
- `data/enviados.json` e `data/reservas.json`: fallback local minimo para compatibilidade.
- `requirements.txt`: dependencias Python.
- `.env.example`: modelo de variaveis de ambiente.

## Render

### Build command

```bash
pip install -r requirements.txt
```

### Start command

```bash
python main.py
```

O servidor usa `PORT` da plataforma e escuta em `0.0.0.0`.

## Disparo unico agendado de 06/08/2026

O backend nao possui scheduler em memoria ativo. Para este disparo especifico,
use um Render Cron Job executando `scripts/disparo_agendado.py`. O comando usa
somente os tres clientes informados explicitamente por `DISPARO_CLIENTE_IDS` ou
`DISPARO_TELEFONES`; ele nao consulta a lista automatica de aniversariantes.

O job faz uma validacao completa dos tres clientes e dos templates antes do
primeiro envio. Em producao, cria primeiro uma linha `pendente` com
`modo_teste=false` em `disparos_mensagens`. O indice unico de producao ja
existente nessa tabela impede um segundo claim para o mesmo telefone/data. A
linha e atualizada para `enviado` ou `falha`, e a conversa e criada com
`status=bot_ativo`. Nenhum schema novo e necessario.

### Variaveis temporarias do job

Configure exatamente uma das listas abaixo no Cron Job. Nao coloque telefones
ou chaves no repositorio:

```bash
DISPARO_CLIENTE_IDS=id-1,id-2,id-3
# ou, alternativamente:
# DISPARO_TELEFONES=5511999990001,5511999990002,5511999990003

DRY_RUN=true
DISPARO_DATA_REFERENCIA=2026-08-06
DISPARO_HORARIO_LOCAL=19:00
DISPARO_TIMEZONE=America/Sao_Paulo
DISPARO_CHAVE_IDEMPOTENCIA=disparo-2026-08-06-19h-3-clientes
```

Para validar sem enviar:

```bash
DRY_RUN=true python scripts/disparo_agendado.py
```

Depois de confirmar no log que `total_destinatarios=3`, os tres templates
estao aprovados e aparece `confirmacao: nenhum envio ocorreu`, altere somente
`DRY_RUN=false`. O comando real e:

```bash
DRY_RUN=false python scripts/disparo_agendado.py
```

O script recusa qualquer quantidade diferente de tres, telefones invalidos,
IDs repetidos, cliente ausente, perfil inelegivel, template nao aprovado,
execucao antes das 19:00 ou execucao em outra data. A chave, a data e o fuso
tambem sao fixos neste job para evitar reutilizacao acidental.

### Configuracao exata no Render

Crie um servico do tipo **Cron Job** no mesmo repositorio/branch do backend:

- Root Directory: `reserva-backend`
- Build Command: `pip install -r requirements.txt`
- Schedule: `0 22 6 8 *` (22:00 UTC = 19:00 em `America/Sao_Paulo` em 06/08/2026)
- Command: `python scripts/disparo_agendado.py`
- Runtime: Python 3, com as mesmas variaveis de Supabase e WhatsApp Cloud do web service
- `WHATSAPP_PROVIDER=cloud`

Copie para o Cron Job as variaveis de conexao ja usadas pelo backend
(`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, tabelas `SUPABASE_*`,
`WHATSAPP_API_VERSION`, `WHATSAPP_PHONE_NUMBER_ID` e
`WHATSAPP_ACCESS_TOKEN`) e as variaveis temporarias acima. Mantenha
`DRY_RUN=true` no primeiro deploy. Render interpreta as expressoes de Cron em
UTC; o job tambem valida o fuso e bloqueia execucoes fora de 06/08/2026.

Para cancelar antes das 19:00, apague o Cron Job ou altere `DRY_RUN` para
`true` antes da proxima execucao. Depois de um sucesso, remova o Cron Job (ou
deixe-o inofensivo: a data fixa impede novo disparo em 07/08/2026).

### Conferencia depois das 19:00

No Supabase, confira as tres linhas da chave e os retornos da Meta:

```sql
select cliente_id, status, provider_message_id, modo_teste,
       metadata->>'chave_idempotencia' as chave,
       metadata->>'template_name' as template_name
from public.disparos_mensagens
where tipo_disparo = 'aniversario'
  and data_referencia = '2026-08-06'
  and modo_teste = false
  and metadata->>'chave_idempotencia' = 'disparo-2026-08-06-19h-3-clientes';
```

Deve haver exatamente tres linhas, com `status=enviado`, `entregue` ou
`lido`. `provider_message_id` identifica cada mensagem; os webhooks da Meta
atualizam `entregue`/`lido` e preservam a chave. Confira também `conversas` por
esses `cliente_id`, esperando `origem=aniversario` e `status=bot_ativo`.

## Variaveis obrigatorias

Configure no painel do Render:

```bash
PORT=10000
CONFIG_SERVER_HOST=0.0.0.0
CORS_ALLOW_ORIGIN=https://seu-site.netlify.app

AI_PRIMARY_PROVIDER=gemini
AI_FALLBACK_PROVIDER=groq
AI_TIMEOUT_SECONDS=30

GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash-lite
GEMINI_PRIMARY_MODEL=
GEMINI_FALLBACK_MODEL=

GROQ_API_KEY=
GROQ_MODEL=llama-3.3-70b-versatile
GROQ_PRIMARY_MODEL=
GROQ_FALLBACK_MODEL=

SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=
SUPABASE_CLIENTES_TABLE=clientes
SUPABASE_PERFIS_TABLE=perfis_clientes
SUPABASE_CONVERSAS_TABLE=conversas
SUPABASE_MENSAGENS_TABLE=mensagens
SUPABASE_RESERVAS_TABLE=reservas

WHATSAPP_PROVIDER=cloud
WHATSAPP_API_VERSION=v20.0
WHATSAPP_PHONE_NUMBER_ID=
WHATSAPP_ACCESS_TOKEN=
WHATSAPP_VERIFY_TOKEN=
WHATSAPP_TIMEOUT_SEGUNDOS=20

PDF_UPLOAD_MAX_MB=15
NOME_RESTAURANTE=ReservaBot
AGENTE_PERSONALIDADE=educado, objetivo e acolhedor
MENSAGEM_ANIVERSARIO=Ola, {nome}! Feliz aniversario! Temos uma condicao especial para voce comemorar aqui no restaurante. Quer reservar uma mesa?
HORARIO_DISPARO=09:00
TIMEZONE=America/Sao_Paulo
EXECUTAR_DISPARO_AO_INICIAR=false
```

`SUPABASE_SERVICE_ROLE_KEY` fica somente no Render. Nunca coloque essa chave no Netlify ou no HTML.

## Providers de IA

O backend usa o contrato interno estruturado do agente. O Gemini pode ser ativado como
provider principal com `AI_PRIMARY_PROVIDER=gemini` e o Groq permanece como fallback
com `AI_FALLBACK_PROVIDER=groq`. As chaves `GEMINI_API_KEY` e `GROQ_API_KEY` ficam
somente no Render e nunca devem ser colocadas no frontend.

Para voltar temporariamente ao Groq, configure `AI_PRIMARY_PROVIDER=groq` e mantenha
`GROQ_API_KEY` configurada. O fluxo de reservas, comprovantes e aprovacao humana nao
depende do provider escolhido.

Os logs registram provider, modelo, resultado e categoria do erro, sem registrar
chaves, prompts completos ou o historico integral da conversa.

As variaveis `AI_FALLBACK_API_KEY`, `AI_FALLBACK_MODEL` e `AI_FALLBACK_BASE_URL`
continuam disponiveis para o fallback OpenAI-compatible legado. Para usa-lo,
configure `AI_FALLBACK_PROVIDER` como `openai` ou `openai_compatible`.

## CORS

`CORS_ALLOW_ORIGIN` deve conter o dominio publico do Netlify, por exemplo:

```bash
CORS_ALLOW_ORIGIN=https://reservabot.netlify.app
```

Tambem aceita lista separada por virgula se precisar liberar preview:

```bash
CORS_ALLOW_ORIGIN=https://reservabot.netlify.app,https://deploy-preview-1--reservabot.netlify.app
```

## Webhook da Meta

Depois de publicar no Render, cadastre na Meta:

```text
https://seu-backend.onrender.com/api/whatsapp/webhook
```

Use o mesmo valor de `WHATSAPP_VERIFY_TOKEN` configurado no Render.

O backend suporta:

- `GET /api/whatsapp/webhook` para verificacao da Meta.
- `POST /api/whatsapp/webhook` para receber mensagens.

## Endpoints usados pelo front

- `GET /api/health`
- `GET /api/config`
- `POST /api/config`
- `GET /api/clientes`
- `GET /api/reservas`
- `GET /api/perfis`
- `POST /api/perfis`
- `POST /api/perfis/ativar`
- `POST /api/perfis/excluir`
- `POST /api/clientes/pdf/preview`
- `POST /api/clientes/pdf/confirmar`
- `POST /api/disparos/aniversarios`
- `GET /api/whatsapp/webhook`
- `POST /api/whatsapp/webhook`

## Supabase

Antes do primeiro uso real, rode `supabase/schema.sql` no SQL Editor do Supabase.

O backend usa `SUPABASE_SERVICE_ROLE_KEY` para gravar clientes, perfis, conversas, mensagens e reservas.

## Observacao importante

O provider principal em producao deve ser:

```bash
WHATSAPP_PROVIDER=cloud
```

O suporte Selenium continua no codigo por compatibilidade, mas nao deve ser usado no Render.
