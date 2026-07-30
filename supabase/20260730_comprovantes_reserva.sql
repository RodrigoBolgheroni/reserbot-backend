-- Fluxo incremental de solicitacao, comprovante privado e confirmacao humana.

-- O webhook atualiza conversas antes de reservas. A migration adquire os locks
-- na mesma ordem para evitar deadlock com mensagens recebidas durante o deploy.
set lock_timeout = '30s';
lock table public.conversas in share row exclusive mode;
lock table public.estabelecimentos in share mode;
lock table public.espacos in share mode;
lock table public.reservas in access exclusive mode;

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

alter table public.reservas
    add column if not exists estabelecimento_id uuid references public.estabelecimentos(id) on delete set null,
    add column if not exists espaco_id uuid references public.espacos(id) on delete set null,
    add column if not exists status_pagamento text not null default 'nao_iniciado';

alter table public.reservas drop constraint if exists reservas_status_check;
alter table public.reservas
    add constraint reservas_status_check
    check (status in (
        'pendente', 'identificada', 'aguardando_comprovante', 'aguardando_analise',
        'confirmada', 'cancelada', 'erro'
    ));

alter table public.reservas drop constraint if exists reservas_status_pagamento_check;
alter table public.reservas
    add constraint reservas_status_pagamento_check
    check (status_pagamento in (
        'nao_iniciado', 'aguardando_comprovante', 'aguardando_analise',
        'aprovado', 'rejeitado', 'estornado'
    ));

create table if not exists public.comprovantes_reserva (
    id uuid primary key default gen_random_uuid(),
    reserva_id uuid references public.reservas(id) on delete set null,
    conversa_id uuid not null references public.conversas(id) on delete cascade,
    provider_message_id text not null,
    media_id text not null,
    tipo_midia text not null check (tipo_midia in ('imagem', 'pdf')),
    mime_type text not null check (mime_type in ('image/jpeg', 'image/png', 'image/webp', 'application/pdf')),
    nome_original text,
    tamanho_bytes bigint not null check (tamanho_bytes > 0 and tamanho_bytes <= 15728640),
    sha256 text,
    bucket text not null,
    storage_path text not null,
    recebido_em timestamptz not null default now(),
    status_analise text not null default 'aguardando_analise'
        check (status_analise in ('aguardando_analise', 'aprovado', 'rejeitado')),
    analisado_em timestamptz,
    analisado_por text,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create unique index if not exists ux_comprovantes_provider_message_id
    on public.comprovantes_reserva(provider_message_id);
create unique index if not exists ux_comprovantes_storage_path
    on public.comprovantes_reserva(bucket, storage_path);
create index if not exists idx_comprovantes_reserva_recebido
    on public.comprovantes_reserva(reserva_id, recebido_em desc);
create index if not exists idx_reservas_conversa_status
    on public.reservas(conversa_id, status);

drop trigger if exists trg_comprovantes_reserva_updated_at on public.comprovantes_reserva;
create trigger trg_comprovantes_reserva_updated_at
before update on public.comprovantes_reserva
for each row execute function public.set_updated_at();

alter table public.comprovantes_reserva enable row level security;

create or replace function public.confirmar_reserva_comprovante(
    p_reserva_id uuid,
    p_analisado_por text default 'painel_autenticado'
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    reserva_atualizada public.reservas%rowtype;
begin
    if not exists (
        select 1
        from public.comprovantes_reserva
        where reserva_id = p_reserva_id
          and status_analise = 'aguardando_analise'
    ) then
        raise exception 'reserva_sem_comprovante_pendente';
    end if;

    update public.reservas
    set
        status = 'confirmada',
        status_pagamento = 'aprovado',
        metadata = coalesce(metadata, '{}'::jsonb) || jsonb_build_object(
            'confirmada_por', coalesce(nullif(p_analisado_por, ''), 'painel_autenticado'),
            'confirmada_em', now()
        )
    where id = p_reserva_id
      and status = 'aguardando_analise'
    returning * into reserva_atualizada;

    if reserva_atualizada.id is null then
        raise exception 'reserva_nao_aguarda_analise';
    end if;

    update public.comprovantes_reserva
    set
        status_analise = 'aprovado',
        analisado_em = now(),
        analisado_por = coalesce(nullif(p_analisado_por, ''), 'painel_autenticado')
    where reserva_id = p_reserva_id
      and status_analise = 'aguardando_analise';

    return to_jsonb(reserva_atualizada);
end;
$$;

revoke all on function public.confirmar_reserva_comprovante(uuid, text) from public;
revoke all on function public.confirmar_reserva_comprovante(uuid, text) from anon;
revoke all on function public.confirmar_reserva_comprovante(uuid, text) from authenticated;
grant execute on function public.confirmar_reserva_comprovante(uuid, text) to service_role;

-- Bucket privado. Leituras e escritas ocorrem apenas pelo backend com service role.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
    'reserva-comprovantes',
    'reserva-comprovantes',
    false,
    15728640,
    array['image/jpeg', 'image/png', 'image/webp', 'application/pdf']
)
on conflict (id) do update
set
    public = false,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;

-- Preenche somente campos vazios; nunca substitui configuracao feita no painel.
update public.configuracoes_reserva cr
set
    pix_chave = coalesce(nullif(cr.pix_chave, ''), '42.538.063/0001-46'),
    pix_titular = coalesce(nullif(cr.pix_titular, ''), 'Praia da Radial')
from public.estabelecimentos e
where e.id = cr.estabelecimento_id
  and e.slug = 'praia-da-radial'
  and (nullif(cr.pix_chave, '') is null or nullif(cr.pix_titular, '') is null);
