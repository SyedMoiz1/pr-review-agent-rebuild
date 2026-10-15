CREATE TABLE webhook_events (
    id BIGSERIAL PRIMARY KEY,
    delivery_id TEXT UNIQUE NOT NULL,
    event_type TEXT,
    action TEXT,
    repo TEXT,
    pr_number INTEGER,
    payload JSONB NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now() 
);
