# Security & Secrets

This directory holds the secret-management surface for the
Compliance-as-Code stack. Nothing in here should ever be committed in
plain text – the files shipped in the repo are **templates** only.

## Files

| File                  | Purpose                                                        |
| --------------------- | -------------------------------------------------------------- |
| `.env.example`        | Template for shell / Docker Compose environment variables.     |
| `vault.example.yml`   | Template for an Ansible Vault file (SSH/sudo creds, etc.).     |

## How to use

### 1. Environment variables (Docker Compose)

```bash
cp cismonitoringv2/security/.env.example cismonitoringv2/security/.env
# edit values, then load them into the monitoring stack
set -a; source cismonitoringv2/security/.env; set +a
cd cismonitoringv2/monitoring && docker compose up -d
```

### 2. Ansible Vault (playbook runs)

```bash
cp cismonitoringv2/security/vault.example.yml cismonitoringv2/security/vault.yml
ansible-vault encrypt cismonitoringv2/security/vault.yml

# Use during a run:
ansible-playbook -i inventory/hosts \
  cismonitoringv2/orchestration/audit_docker.yml \
  -e @cismonitoringv2/security/vault.yml --ask-vault-pass
```

## What is git-ignored

The repo-level `.gitignore` already excludes:

- `cismonitoringv2/security/.env`
- `cismonitoringv2/security/vault.yml`

so accidental commits of real secrets are blocked.

## Rotation guidance

- Rotate Grafana admin password after the first login.
- Use SSH keys (not passwords) for `ansible_user` on target Docker hosts;
  store the key path in `.env` (`ANSIBLE_PRIVATE_KEY_FILE`).
- Re-encrypt Vault files after any credential change:
  `ansible-vault rekey cismonitoringv2/security/vault.yml`.
