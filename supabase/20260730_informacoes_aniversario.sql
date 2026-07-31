do $$
declare
    praia_id uuid;
begin
    select id
      into praia_id
      from public.estabelecimentos
     where slug = 'praia-da-radial'
     limit 1;

    if praia_id is null then
        raise notice 'Estabelecimento praia-da-radial não encontrado; FAQs de aniversário não alteradas.';
        return;
    end if;

    update public.faq_conteudos
       set ativo = false
     where estabelecimento_id = praia_id
       and categoria = 'aniversario'
       and (
            lower(titulo) in ('bolo e decoração', 'bolo e decoracao')
            or lower(conteudo) like '%confirmad%com a equipe%'
       );

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
            'Lista de aniversário',
            'A Praia da Radial não trabalha com lista de aniversário.',
            array['lista', 'aniversario', 'convidados'],
            true
        ),
        (
            praia_id,
            'aniversario',
            'Bolo de aniversário',
            'O cliente pode levar bolo. A equipe pode guardá-lo na geladeira até a hora do parabéns. Recomenda-se que o cliente leve pratos e garfos para servir.',
            array['bolo', 'aniversario', 'geladeira', 'pratos', 'garfos', 'utensilios'],
            true
        )
    on conflict (estabelecimento_id, categoria, titulo) do update
    set
        conteudo = excluded.conteudo,
        tags = excluded.tags,
        ativo = true;
end $$;
