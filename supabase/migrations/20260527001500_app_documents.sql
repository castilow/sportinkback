create table if not exists public.app_documents (
    row_id bigserial primary key,
    collection text not null,
    doc jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists idx_app_documents_collection
    on public.app_documents (collection);

create index if not exists idx_app_documents_collection_doc_id
    on public.app_documents (collection, (doc->>'id'));

create unique index if not exists uq_app_documents_collection_doc_id
    on public.app_documents (collection, (doc->>'id'))
    where doc ? 'id';

create unique index if not exists uq_app_documents_users_email
    on public.app_documents ((lower(doc->>'email')))
    where collection = 'users' and doc ? 'email';

create unique index if not exists uq_app_documents_login_identifier
    on public.app_documents ((doc->>'identifier'))
    where collection = 'login_attempts' and doc ? 'identifier';

create unique index if not exists uq_app_documents_poll_votes_pair
    on public.app_documents ((doc->>'poll_id'), (doc->>'voter_name'))
    where collection = 'poll_votes' and doc ? 'poll_id' and doc ? 'voter_name';
