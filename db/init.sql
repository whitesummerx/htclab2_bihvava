CREATE TABLE IF NOT EXISTS appointments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_code VARCHAR(64) NOT NULL,
    doctor VARCHAR(128) NOT NULL,
    room VARCHAR(32) NOT NULL,
    start_time TIMESTAMPTZ NOT NULL,
    duration_minutes INTEGER NOT NULL,
    status VARCHAR(32) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Використовуємо явне приведення часу до UTC для сумісності з індексом
CREATE INDEX IF NOT EXISTS idx_appointments_start_time_date
ON appointments (((start_time AT TIME ZONE 'UTC')::date));