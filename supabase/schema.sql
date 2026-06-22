-- ReservaBot Supabase schema.
-- Run this file in the Supabase SQL editor before enabling the production flow.

create extension if not exists pgcrypto;

create table if not exists public.perfis_clientes (
    id uuid primary key default gen_random_uuid(),
    nome text not null unique,
    descricao text,
    ativo boolean not null default true,
    criterios jsonb not null default '{}'::jsonb,
    mensagem_disparo text,
    prompt_ia text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.clientes (
    id uuid primary key default gen_random_uuid(),
    nome text not null,
    telefone text not null unique,
    telefone_raw text,
    telefones jsonb not null default '[]'::jsonb,
    data_nascimento date,
    data_nascimento_raw text,
    aniversario_ddmm text,
    idade integer check (idade is null or idade between 0 and 130),
    info_topo_pdf text,
    periodo_aniversario text,
    tipo text,
    regiao text,
    numero integer,
    origem text not null default 'pdf',
    pagina integer,
    linha integer,
    perfil_id uuid references public.perfis_clientes(id) on delete set null,
    perfil_nome text,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.conversas (
    id uuid primary key default gen_random_uuid(),
    cliente_id uuid references public.clientes(id) on delete set null,
    cliente_telefone text not null,
    status text not null default 'aguardando_cliente'
        check (status in ('aberta', 'aguardando_cliente', 'em_atendimento', 'finalizada', 'erro')),
    data_inicio timestamptz not null default now(),
    data_fim timestamptz,
    origem text not null default 'aniversario'
        check (origem in ('aniversario', 'pdf', 'whatsapp', 'manual', 'webhook')),
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.mensagens (
    id uuid primary key default gen_random_uuid(),
    conversa_id uuid not null references public.conversas(id) on delete cascade,
    remetente text not null check (remetente in ('cliente', 'bot', 'agente', 'sistema')),
    conteudo text not null,
    timestamp timestamptz not null default now(),
    provider_message_id text,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create table if not exists public.reservas (
    id uuid primary key default gen_random_uuid(),
    cliente_id uuid references public.clientes(id) on delete set null,
    cliente_telefone text not null,
    conversa_id uuid references public.conversas(id) on delete set null,
    data_reserva date not null,
    horario time,
    pessoas integer check (pessoas is null or pessoas > 0),
    observacoes text,
    status text not null default 'confirmada'
        check (status in ('pendente', 'identificada', 'confirmada', 'cancelada', 'erro')),
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.disparos_mensagens (
    id uuid primary key default gen_random_uuid(),
    cliente_id uuid references public.clientes(id) on delete set null,
    telefone text not null,
    tipo_disparo text not null default 'aniversario',
    data_referencia date not null default current_date,
    mensagem text,
    status text not null default 'pendente'
        check (status in ('pendente', 'enviado', 'entregue', 'lido', 'falha', 'pulado')),
    provider text,
    provider_message_id text,
    erro text,
    modo_teste boolean not null default false,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists idx_clientes_telefone on public.clientes (telefone);
create index if not exists idx_clientes_aniversario on public.clientes (aniversario_ddmm);
create index if not exists idx_clientes_perfil on public.clientes (perfil_id);
create index if not exists idx_conversas_cliente on public.conversas (cliente_id);
create index if not exists idx_conversas_telefone_status on public.conversas (cliente_telefone, status);
create index if not exists idx_mensagens_conversa_timestamp on public.mensagens (conversa_id, timestamp);
create unique index if not exists idx_mensagens_provider_message_id_unico
    on public.mensagens (provider_message_id)
    where provider_message_id is not null and provider_message_id <> '';
create index if not exists idx_reservas_cliente on public.reservas (cliente_id);
create index if not exists idx_reservas_data on public.reservas (data_reserva);
create index if not exists idx_perfis_clientes_ativo on public.perfis_clientes (ativo);
create index if not exists idx_disparos_mensagens_telefone_data
    on public.disparos_mensagens (telefone, tipo_disparo, data_referencia);
create unique index if not exists ux_disparos_mensagens_dia
    on public.disparos_mensagens (telefone, tipo_disparo, data_referencia)
    where modo_teste = false;

alter table public.clientes
    add column if not exists perfil_id uuid references public.perfis_clientes(id) on delete set null,
    add column if not exists perfil_nome text;

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists trg_perfis_clientes_updated_at on public.perfis_clientes;
create trigger trg_perfis_clientes_updated_at
before update on public.perfis_clientes
for each row execute function public.set_updated_at();

drop trigger if exists trg_clientes_updated_at on public.clientes;
create trigger trg_clientes_updated_at
before update on public.clientes
for each row execute function public.set_updated_at();

drop trigger if exists trg_conversas_updated_at on public.conversas;
create trigger trg_conversas_updated_at
before update on public.conversas
for each row execute function public.set_updated_at();

drop trigger if exists trg_reservas_updated_at on public.reservas;
create trigger trg_reservas_updated_at
before update on public.reservas
for each row execute function public.set_updated_at();

drop trigger if exists trg_disparos_mensagens_updated_at on public.disparos_mensagens;
create trigger trg_disparos_mensagens_updated_at
before update on public.disparos_mensagens
for each row execute function public.set_updated_at();

alter table public.clientes enable row level security;
alter table public.perfis_clientes enable row level security;
alter table public.conversas enable row level security;
alter table public.mensagens enable row level security;
alter table public.reservas enable row level security;
alter table public.disparos_mensagens enable row level security;

-- No public policies are created here. The backend flow should use
-- SUPABASE_SERVICE_ROLE_KEY; anon access remains blocked by RLS by default.
