CREATE TABLE IF NOT EXISTS public.ai_provider_status (
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  indisponivel_ate TIMESTAMPTZ,
  motivo TEXT,
  metadata JSONB DEFAULT '{}'::jsonb,
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (provider, model)
);

ALTER TABLE public.ai_provider_status ENABLE ROW LEVEL SECURITY;
