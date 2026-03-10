-- v4_awaiting_input.sql
-- Add input_type and input_data columns to tickets for AWAITING_INPUT state
ALTER TABLE tickets ADD COLUMN input_type TEXT;
ALTER TABLE tickets ADD COLUMN input_data TEXT;
