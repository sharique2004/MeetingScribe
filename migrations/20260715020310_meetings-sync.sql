-- Meetings synced from the desktop app for phone viewing.
-- One row per (user, desktop meeting id): transcript + summary TEXT only —
-- never audio. Rows exist only for meetings the user explicitly toggled
-- "View on phone"; un-toggling deletes the row.

CREATE TABLE public.meetings (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  meeting_id text NOT NULL,                 -- desktop id, YYYYMMDD-HHMMSS
  title      text NOT NULL DEFAULT '',
  created    timestamptz,
  duration   real,
  mode       text,
  speakers   jsonb NOT NULL DEFAULT '{}'::jsonb,  -- label -> display name
  turns      jsonb NOT NULL DEFAULT '[]'::jsonb,  -- [{speaker,start,end,text}]
  summary    jsonb,
  stats      jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, meeting_id)
);

CREATE INDEX meetings_user_created_idx ON public.meetings (user_id, created DESC);

-- Owner-only access. The mobile web app reads with the signed-in user's JWT;
-- the desktop app writes with the same identity. Nothing is public.
ALTER TABLE public.meetings ENABLE ROW LEVEL SECURITY;

CREATE POLICY meetings_owner_select ON public.meetings FOR SELECT TO authenticated
  USING (user_id = (SELECT auth.uid()));
CREATE POLICY meetings_owner_insert ON public.meetings FOR INSERT TO authenticated
  WITH CHECK (user_id = (SELECT auth.uid()));
CREATE POLICY meetings_owner_update ON public.meetings FOR UPDATE TO authenticated
  USING (user_id = (SELECT auth.uid()))
  WITH CHECK (user_id = (SELECT auth.uid()));
CREATE POLICY meetings_owner_delete ON public.meetings FOR DELETE TO authenticated
  USING (user_id = (SELECT auth.uid()));

REVOKE ALL ON public.meetings FROM anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.meetings TO authenticated;

-- Keep updated_at honest on every write.
CREATE OR REPLACE FUNCTION public.meetings_touch_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

CREATE TRIGGER meetings_touch_updated_at
  BEFORE UPDATE ON public.meetings
  FOR EACH ROW EXECUTE FUNCTION public.meetings_touch_updated_at();
