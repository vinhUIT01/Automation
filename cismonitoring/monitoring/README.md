# CIS Docker Audit – Monitoring stack

Persists audit results in Prometheus and visualises them in Grafana.
Ansible playbooks push metrics to a Pushgateway after each audit run.

```
┌──────────────────┐ push (HTTP PUT) ┌──────────────┐ scrape ┌────────────┐ query ┌─────────┐
│  Ansible audit   │ ──────────────▶ │  Pushgateway │ ─────▶│ Prometheus │ ────▶ │ Grafana │
│  (batch job)     │                 │   :9091      │       │   :9090    │       │  :3000  │
└──────────────────┘                 └──────────────┘       └────────────┘       └─────────┘
```

The fetched JSON reports under `audit/docker_benchmark_reports/` remain
the authoritative per-run artefact; Prometheus stores the time series for
historical trending (180-day retention by default).

## Bring the stack up

```bash
cd monitoring
docker compose up -d
```

| Service     | URL                       | Credentials   |
| ----------- | ------------------------- | ------------- |
| Grafana     | http://localhost:3000     | admin / admin |
| Prometheus  | http://localhost:9090     | –             |
| Pushgateway | http://localhost:9091     | –             |

The Grafana dashboard "CIS Docker Benchmark" is provisioned automatically.

## Push metrics from an audit run

### Option A – integrate into each section playbook

After the `Report | Fetch JSON to controller` task in any
`audit/auditSection*.y*ml`, include the shared task file:

```yaml
- name: Push CIS metrics to Pushgateway
  ansible.builtin.import_tasks: push_metrics.yml
  vars:
    section_name: section1
    json_local: "./docker_benchmark_reports/{{ inventory_hostname }}_section1.json"
```

Override the Pushgateway URL with `-e pushgateway_url=http://host:9091`
or disable pushing with `-e push_metrics_skip=true`.

### Option B – back-fill from existing JSON reports

```bash
ansible-playbook monitoring/push_all_reports.yml \
  -e pushgateway_url=http://localhost:9091
```

This scans `audit/docker_benchmark_reports/` and pushes every
`<host>_section<N>.json` it finds.

### Option C – push a single JSON manually

```bash
python3 monitoring/scripts/push_metrics.py \
  --json audit/docker_benchmark_reports/192.168.1.8_section1.json \
  --host 192.168.1.8 \
  --section section1 \
  --pushgateway http://localhost:9091
```

Add `--dry-run` to print the exposition format without pushing.

## Metrics exposed

| Metric                                       | Labels                                  | Meaning                              |
| -------------------------------------------- | --------------------------------------- | ------------------------------------ |
| `cis_docker_check`                           | `id`, `title`, `sub_section`, `status`  | One check: 1=PASS, 0=FAIL, 2=INFO, 3=WARN, 4=SKIP, 5=ERROR |
| `cis_docker_summary{outcome=…}`              | `outcome=total\|pass\|fail\|info\|warn\|skip` | Aggregate counts per audit run |
| `cis_docker_compliance_percent`              | –                                       | `pass / (pass + fail) * 100`         |
| `cis_docker_last_audit_timestamp_seconds`    | –                                       | Unix timestamp of the most recent push |

Every series is tagged with `instance=<host>` and `section=<section>` by
the Pushgateway grouping path.

## Useful PromQL

```promql
# Per-host compliance right now
cis_docker_compliance_percent

# Total failing checks across the fleet
sum(cis_docker_summary{outcome="fail"})

# Failing checks per host, by sub-section
sum by (instance, sub_section) (cis_docker_check{status="FAIL"})

# Audit freshness: hosts that haven't reported in > 24h
time() - cis_docker_last_audit_timestamp_seconds > 86400
```

## Operational notes

- **Pushgateway persistence** is enabled (`/data/pushgateway.data`,
  flushed every minute) so a container restart does not lose the last
  pushed snapshot.
- **Grouping** is `job=cis_docker_audit / instance=<host> / section=<section>`;
  pushing one section does not overwrite another.
- **Stale series**: a check that disappears from a future report still
  shows in Prometheus until the same grouping is re-pushed without it.
  To clear a grouping manually:
  ```bash
  curl -X DELETE http://localhost:9091/metrics/job/cis_docker_audit/instance/<host>/section/<section>
  ```
- **Retention**: configured to 180 days in `docker-compose.yml`. Adjust
  `--storage.tsdb.retention.time` to suit.
