\set ON_ERROR_STOP on

SELECT 'CREATE DATABASE kolabi_market'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'kolabi_market')\gexec

SELECT 'CREATE DATABASE kolabi_account'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'kolabi_account')\gexec

SELECT 'CREATE DATABASE kolabi_critical'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'kolabi_critical')\gexec

SELECT 'CREATE DATABASE kolabi_audit'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'kolabi_audit')\gexec

SELECT 'CREATE DATABASE kolabi_telemetry'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'kolabi_telemetry')\gexec

-- Adversarial bot/account lanes, used with --account-scope advers.
SELECT 'CREATE DATABASE kolabi_account_advers'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'kolabi_account_advers')\gexec

SELECT 'CREATE DATABASE kolabi_critical_advers'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'kolabi_critical_advers')\gexec

SELECT 'CREATE DATABASE kolabi_audit_advers'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'kolabi_audit_advers')\gexec

SELECT 'CREATE DATABASE kolabi_telemetry_advers'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'kolabi_telemetry_advers')\gexec
