-- Incremental: persistence for ReservaBot conversational state.
-- Safe to run in production; it does not recreate or delete tables.

alter table public.conversas
    add column if not exists metadata jsonb not null default '{}'::jsonb;

create index if not exists idx_conversas_metadata_gin
    on public.conversas using gin (metadata);

create index if not exists idx_conversas_estado_reserva_ativo
    on public.conversas (cliente_telefone, status)
    where metadata ? 'estado_reserva';
