# Deploy Backend - Render

Esta pasta contem o backend Python do ReservaBot pronto para rodar separado do front.

## Estrutura

- `main.py`: entrypoint de producao.
- `scripts/config_server.py`: servidor HTTP/API.
- `services/`: modulos de negocio, Supabase, Groq, WhatsApp Cloud API, PDF, perfis e reservas.
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

## Variaveis obrigatorias

Configure no painel do Render:

```bash
PORT=10000
CONFIG_SERVER_HOST=0.0.0.0
CORS_ALLOW_ORIGIN=https://seu-site.netlify.app

GROQ_API_KEY=
GROQ_MODEL=llama-3.3-70b-versatile

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
