-- v3_phases_config.sql
-- Add phases_config column to agent_profiles (JSON blob)
-- This column stores per-profile phase pipeline configuration
ALTER TABLE agent_profiles ADD COLUMN phases_config TEXT;
