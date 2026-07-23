-- Incremental: MVP structured establishment knowledge for ReservaBot.
-- Safe to run in production; it does not recreate or delete existing tables.
-- RLS remains enabled and no public policies are created. The backend must use
-- SUPABASE_SERVICE_ROLE_KEY. Never expose the service role key in the frontend.

create extension if not exists pgcrypto;

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

create table if not exists public.estabelecimentos (
    id uuid primary key default gen_random_uuid(),
    nome text not null,
    slug text,
    telefone text,
    whatsapp text,
    endereco text,
    ponto_referencia text,
    timezone text not null default 'America/Sao_Paulo',
    ativo boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.horarios_funcionamento (
    id uuid primary key default gen_random_uuid(),
    estabelecimento_id uuid not null references public.estabelecimentos(id) on delete cascade,
    dia_semana smallint not null check (dia_semana between 0 and 6),
    fechado boolean not null default false,
    horario_abertura time,
    horario_fechamento time,
    observacao text,
    ativo boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.configuracoes_reserva (
    id uuid primary key default gen_random_uuid(),
    estabelecimento_id uuid not null references public.estabelecimentos(id) on delete cascade,
    quantidade_minima integer,
    quantidade_maxima_automatica integer,
    horarios_permitidos jsonb not null default '[]'::jsonb,
    taxa_valor numeric(10,2),
    taxa_convertida_consumacao boolean not null default false,
    prazo_cancelamento_horas integer,
    pix_chave text,
    pix_titular text,
    exige_comprovante boolean not null default false,
    tolerancia_atraso_minutos integer,
    politica_cancelamento text,
    instrucoes_reserva text,
    ativo boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.espacos (
    id uuid primary key default gen_random_uuid(),
    estabelecimento_id uuid not null references public.estabelecimentos(id) on delete cascade,
    nome text not null,
    descricao text,
    capacidade_maxima integer,
    permite_preferencia boolean not null default true,
    regras text,
    ativo boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.faq_conteudos (
    id uuid primary key default gen_random_uuid(),
    estabelecimento_id uuid not null references public.estabelecimentos(id) on delete cascade,
    categoria text not null,
    titulo text not null,
    conteudo text not null,
    tags text[] not null default '{}',
    ativo boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

alter table public.estabelecimentos
    add column if not exists slug text;

drop index if exists public.ux_estabelecimentos_nome;

update public.estabelecimentos
set slug = 'praia-da-radial'
where id = (
    select id
    from public.estabelecimentos
    where slug is null
      and regexp_replace(lower(trim(nome)), '\s+', ' ', 'g') = 'praia da radial'
    order by created_at asc
    limit 1
);

create unique index if not exists ux_estabelecimentos_slug
    on public.estabelecimentos (slug);

create unique index if not exists ux_horarios_funcionamento_estabelecimento_dia
    on public.horarios_funcionamento (estabelecimento_id, dia_semana);

create unique index if not exists ux_configuracoes_reserva_estabelecimento
    on public.configuracoes_reserva (estabelecimento_id);

create unique index if not exists ux_espacos_estabelecimento_nome
    on public.espacos (estabelecimento_id, nome);

create unique index if not exists ux_faq_conteudos_estabelecimento_categoria_titulo
    on public.faq_conteudos (estabelecimento_id, categoria, titulo);

create index if not exists idx_horarios_funcionamento_estabelecimento
    on public.horarios_funcionamento (estabelecimento_id, dia_semana);

create index if not exists idx_configuracoes_reserva_estabelecimento
    on public.configuracoes_reserva (estabelecimento_id);

create index if not exists idx_espacos_estabelecimento
    on public.espacos (estabelecimento_id);

create index if not exists idx_faq_conteudos_estabelecimento
    on public.faq_conteudos (estabelecimento_id, categoria);

drop trigger if exists trg_estabelecimentos_updated_at on public.estabelecimentos;
create trigger trg_estabelecimentos_updated_at
before update on public.estabelecimentos
for each row execute function public.set_updated_at();

drop trigger if exists trg_horarios_funcionamento_updated_at on public.horarios_funcionamento;
create trigger trg_horarios_funcionamento_updated_at
before update on public.horarios_funcionamento
for each row execute function public.set_updated_at();

drop trigger if exists trg_configuracoes_reserva_updated_at on public.configuracoes_reserva;
create trigger trg_configuracoes_reserva_updated_at
before update on public.configuracoes_reserva
for each row execute function public.set_updated_at();

drop trigger if exists trg_espacos_updated_at on public.espacos;
create trigger trg_espacos_updated_at
before update on public.espacos
for each row execute function public.set_updated_at();

drop trigger if exists trg_faq_conteudos_updated_at on public.faq_conteudos;
create trigger trg_faq_conteudos_updated_at
before update on public.faq_conteudos
for each row execute function public.set_updated_at();

alter table public.estabelecimentos enable row level security;
alter table public.horarios_funcionamento enable row level security;
alter table public.configuracoes_reserva enable row level security;
alter table public.espacos enable row level security;
alter table public.faq_conteudos enable row level security;

comment on table public.estabelecimentos is
    'Structured establishment data for ReservaBot. Access is backend-only through Supabase service role.';
comment on table public.horarios_funcionamento is
    'Structured opening hours. If closing time is less than or equal to opening time, backend treats closing as next day.';
comment on table public.configuracoes_reserva is
    'Structured reservation rules used by ReservaBot. Critical values should live here, not in FAQ text.';
comment on table public.espacos is
    'Reservable or reference spaces for operational rules and customer preferences.';
comment on table public.faq_conteudos is
    'Descriptive knowledge blocks for future RAG. Do not duplicate critical structured rules here.';

do $$
declare
    praia_id uuid;
begin
    insert into public.estabelecimentos (
        nome,
        slug,
        telefone,
        whatsapp,
        endereco,
        ponto_referencia,
        timezone,
        ativo
    )
    values (
        'Praia da Radial',
        'praia-da-radial',
        null,
        null,
        'Rua Guapeperuvu, 56 - Vila Aricanduva, São Paulo - SP',
        'Próximo à Estação Penha do Metrô',
        'America/Sao_Paulo',
        true
    )
    on conflict (slug) do update
    set
        nome = coalesce(nullif(public.estabelecimentos.nome, ''), excluded.nome),
        telefone = coalesce(nullif(public.estabelecimentos.telefone, ''), excluded.telefone),
        whatsapp = coalesce(nullif(public.estabelecimentos.whatsapp, ''), excluded.whatsapp),
        endereco = coalesce(nullif(public.estabelecimentos.endereco, ''), excluded.endereco),
        ponto_referencia = coalesce(nullif(public.estabelecimentos.ponto_referencia, ''), excluded.ponto_referencia),
        timezone = coalesce(nullif(public.estabelecimentos.timezone, ''), excluded.timezone),
        ativo = public.estabelecimentos.ativo
    returning id into praia_id;

    insert into public.horarios_funcionamento (
        estabelecimento_id,
        dia_semana,
        fechado,
        horario_abertura,
        horario_fechamento,
        observacao,
        ativo
    )
    values
        (praia_id, 1, true,  null,    null,    'Fechado', true),
        (praia_id, 2, true,  null,    null,    'Fechado', true),
        (praia_id, 3, false, '17:00', '22:00', null, true),
        (praia_id, 4, false, '12:00', '23:00', null, true),
        (praia_id, 5, false, '12:00', '00:00', 'Fecha à meia-noite', true),
        (praia_id, 6, false, '12:00', '01:00', 'Fecha no dia seguinte', true),
        (praia_id, 0, false, '12:00', '22:00', null, true)
    on conflict (estabelecimento_id, dia_semana) do update
    set
        fechado = public.horarios_funcionamento.fechado,
        horario_abertura = coalesce(public.horarios_funcionamento.horario_abertura, excluded.horario_abertura),
        horario_fechamento = coalesce(public.horarios_funcionamento.horario_fechamento, excluded.horario_fechamento),
        observacao = coalesce(nullif(public.horarios_funcionamento.observacao, ''), excluded.observacao),
        ativo = public.horarios_funcionamento.ativo;

    insert into public.configuracoes_reserva (
        estabelecimento_id,
        quantidade_minima,
        quantidade_maxima_automatica,
        horarios_permitidos,
        taxa_valor,
        taxa_convertida_consumacao,
        prazo_cancelamento_horas,
        pix_chave,
        pix_titular,
        exige_comprovante,
        tolerancia_atraso_minutos,
        politica_cancelamento,
        instrucoes_reserva,
        ativo
    )
    values (
        praia_id,
        11,
        30,
        '["12:00","13:00","14:00","18:00","19:00"]'::jsonb,
        50.00,
        true,
        24,
        null,
        null,
        true,
        15,
        'Cancelamento com estorno até 24 horas antes da reserva.',
        'Reservas são feitas para grupos acima de 10 pessoas. A taxa é convertida em consumação e o comprovante Pix é obrigatório.',
        true
    )
    on conflict (estabelecimento_id) do update
    set
        quantidade_minima = coalesce(public.configuracoes_reserva.quantidade_minima, excluded.quantidade_minima),
        quantidade_maxima_automatica = coalesce(public.configuracoes_reserva.quantidade_maxima_automatica, excluded.quantidade_maxima_automatica),
        horarios_permitidos = case
            when public.configuracoes_reserva.horarios_permitidos is null
              or public.configuracoes_reserva.horarios_permitidos = '[]'::jsonb
            then excluded.horarios_permitidos
            else public.configuracoes_reserva.horarios_permitidos
        end,
        taxa_valor = coalesce(public.configuracoes_reserva.taxa_valor, excluded.taxa_valor),
        taxa_convertida_consumacao = public.configuracoes_reserva.taxa_convertida_consumacao,
        prazo_cancelamento_horas = coalesce(public.configuracoes_reserva.prazo_cancelamento_horas, excluded.prazo_cancelamento_horas),
        pix_chave = coalesce(nullif(public.configuracoes_reserva.pix_chave, ''), excluded.pix_chave),
        pix_titular = coalesce(nullif(public.configuracoes_reserva.pix_titular, ''), excluded.pix_titular),
        exige_comprovante = public.configuracoes_reserva.exige_comprovante,
        tolerancia_atraso_minutos = coalesce(public.configuracoes_reserva.tolerancia_atraso_minutos, excluded.tolerancia_atraso_minutos),
        politica_cancelamento = coalesce(nullif(public.configuracoes_reserva.politica_cancelamento, ''), excluded.politica_cancelamento),
        instrucoes_reserva = coalesce(nullif(public.configuracoes_reserva.instrucoes_reserva, ''), excluded.instrucoes_reserva),
        ativo = public.configuracoes_reserva.ativo;

    insert into public.espacos (
        estabelecimento_id,
        nome,
        descricao,
        capacidade_maxima,
        permite_preferencia,
        regras,
        ativo
    )
    values
        (
            praia_id,
            'Salão',
            'Área interna do estabelecimento.',
            25,
            true,
            'Limite operacional informado para o salão: 25 pessoas. Sábado e domingo, reservas acima de 25 pessoas devem ser direcionadas para a Areia. Reservas às 18h ou 19h não têm preferência de local garantida.',
            true
        ),
        (
            praia_id,
            'Areia',
            'Área externa na areia.',
            null,
            true,
            'Sem limite definido no MVP. Sábado e domingo, reservas acima de 25 pessoas devem ser direcionadas para a Areia. Reservas às 18h ou 19h não têm preferência de local garantida.',
            true
        )
    on conflict (estabelecimento_id, nome) do update
    set
        descricao = coalesce(nullif(public.espacos.descricao, ''), excluded.descricao),
        capacidade_maxima = coalesce(public.espacos.capacidade_maxima, excluded.capacidade_maxima),
        permite_preferencia = public.espacos.permite_preferencia,
        regras = coalesce(nullif(public.espacos.regras, ''), excluded.regras),
        ativo = public.espacos.ativo;

    insert into public.faq_conteudos (
        estabelecimento_id,
        categoria,
        titulo,
        conteudo,
        tags,
        ativo
    )
    values
        (
            praia_id,
            'aniversario',
            'Bolo e decoração',
            'Informações sobre bolo, decoração e detalhes de comemoração devem ser confirmadas com a equipe.',
            array['bolo', 'decoracao', 'aniversario'],
            true
        ),
        (
            praia_id,
            'estacionamento',
            'Estacionamento',
            'Informação de estacionamento ainda precisa ser confirmada com a equipe.',
            array['estacionamento', 'carro', 'localizacao'],
            true
        ),
        (
            praia_id,
            'criancas',
            'Espaço kids',
            'Informações sobre espaço kids ainda precisam ser confirmadas com a equipe.',
            array['criancas', 'kids', 'familia'],
            true
        ),
        (
            praia_id,
            'esportes',
            'Esportes e Day Use',
            'Informações sobre esportes, Day Use e quadras ainda precisam ser confirmadas com a equipe.',
            array['esportes', 'day use', 'quadras'],
            true
        ),
        (
            praia_id,
            'entrada',
            'Entrada de crianças',
            'Regras de entrada de crianças ainda precisam ser confirmadas com a equipe.',
            array['entrada', 'criancas', 'idade'],
            true
        ),
        (
            praia_id,
            'espacos',
            'Localização dos espaços',
            'O estabelecimento possui áreas de salão e areia. A preferência de local pode ser informada, mas a confirmação depende das regras operacionais da reserva.',
            array['salao', 'areia', 'espacos'],
            true
        )
    on conflict (estabelecimento_id, categoria, titulo) do nothing;
end $$;
