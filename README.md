# CIS Docker Benchmark – Compliance-as-Code Platform

Hệ thống tự động hoá audit, thu thập bằng chứng (evidence), giám sát và remediation theo **CIS Docker Benchmark**, được triển khai bằng Ansible. Stack tuân thủ kiến trúc *Compliance-as-Code* và 3 nguyên tắc kiểm toán (Chain of Custody / Integrity & Hashing / Traceability).

> Audit playbooks (`audit/`) và remediation playbooks (`Remediation/`) là source-of-truth gốc – không thay đổi khi nâng cấp pipeline. Toàn bộ phần mở rộng (orchestration, evidence pipeline, reporting, monitoring, security) nằm trong `cismonitoringv2/`.

---

## 1. Kiến trúc tổng thể

```
┌────────┐    ┌──────────────────────────┐    ┌────────────────────────┐    ┌───────────────┐
│ User / │───▶│ Orchestration & Scanning │───▶│ Evidence & Data        │───▶│ Reporting     │
│ Cron / │    │ (Ansible playbooks)      │    │ Pipeline               │    │ HTML / PDF    │
│  CI    │    │  audit_docker.yml        │    │  Collector → Normalizer│    └───────────────┘
└────────┘    │  collect_results.yml     │    │  → Evidence Store      │
              │  remediate_docker.yml    │    │  + Custody Journal     │
              │  verify_evidence.yml     │    └──────────┬─────────────┘
              └────────────┬─────────────┘               │
                           │ SSH                          ▼
                           ▼                    ┌────────────────────┐
                  ┌──────────────────┐          │ Metrics Gateway    │
                  │  Docker Host     │          │ Prometheus         │
                  │  (Docker Engine) │          │ Pushgateway :9091  │
                  └──────────────────┘          └──────────┬─────────┘
                           ▲                               │ scrape
                           │                               ▼
                  ┌────────┴────────┐          ┌─────────────────────┐
                  │ Remediation     │          │ Monitoring & Dashb. │
                  │ (Automated)     │          │ Prometheus :9090    │
                  │ remediate_*.yml │          │ Grafana    :3000    │
                  └─────────────────┘          └─────────────────────┘
                           ▲
                           │
                  ┌────────┴────────┐
                  │ Security & Secrets│
                  │ Ansible Vault /   │
                  │ .env              │
                  └───────────────────┘
```

### 9 khối ↔ thư mục

| # | Khối                             | Implementation                                                          |
|---|----------------------------------|-------------------------------------------------------------------------|
| 1 | User / Trigger                   | `ansible-playbook …`, cron, CI                                          |
| 2 | Docker Host                      | hosts khai báo trong `inventory/hosts`                                  |
| 3 | Orchestration & Scanning         | `cismonitoringv2/orchestration/*.yml` import lại `audit/*` (source cũ)  |
| 4 | Evidence & Data Pipeline         | `cismonitoringv2/evidence_pipeline/` (collector + normalizer + store)   |
| 5 | Metrics Gateway                  | `cismonitoringv2/monitoring/` (Pushgateway port 9091)                   |
| 6 | Reporting                        | `cismonitoringv2/reporting/generate_report.py` (HTML + tuỳ chọn PDF)    |
| 7 | Monitoring & Dashboard           | `cismonitoringv2/monitoring/` (Prometheus 9090 + Grafana 3000)          |
| 8 | Remediation (Automated)          | `cismonitoringv2/orchestration/remediate_docker.yml` + `Remediation/*`  |
| 9 | Security & Secrets               | `cismonitoringv2/security/` (Ansible Vault template + `.env.example`)   |

---

## 2. Tuân thủ 3 Audit Principles

| Nguyên tắc | Thực hiện |
|---|---|
| **Chain of Custody** (Chuỗi hành trình bằng chứng) | Mỗi `run` được gán UUIDv4 + metadata `ingested_at` (UTC ISO-8601) + `ingested_by` (`user@host`, override qua `CIS_AUDIT_ACTOR`). Mọi sự kiện thu thập / verify / supersede ghi đồng thời vào bảng `custody_log` (SQLite) và file append-only `store/journal/custody-YYYYMMDD.jsonl`. **Không bao giờ ghi đè** – phiên bản cũ giữ nguyên, gán `superseded_by` trỏ tới run mới. |
| **Integrity & Hashing** (Tính toàn vẹn) | Tại thời điểm ingest, tính cả `source_sha256` (hash file JSON gốc) và `record_sha256` (hash canonical-JSON của record đã normalize). Script `verify_evidence.py` re-hash định kỳ, ghi `integrity_checks` + custody event `VERIFIED` / `INTEGRITY_FAIL`, exit code ≠ 0 khi phát hiện tamper / mất file. |
| **Traceability** (Khả năng truy vết) | Mỗi `run` và mỗi `check` đều có UUID riêng. Run liên kết về file gốc qua `source` + `source_sha256` → truy ngược được tới bytes nguyên bản. Báo cáo HTML hiển thị UUID, hash chip, ingestion metadata, integrity badge và custody log 200 sự kiện gần nhất. |

---

## 3. Cấu trúc thư mục

```
.
├── ansible.cfg                              # config Ansible (giữ nguyên)
├── inventory/hosts                          # danh sách Docker host target
├── audit/                                   # PLAYBOOK AUDIT GỐC – không sửa
│   ├── auditSection1.yaml … auditSection6.yaml
│   └── docker_benchmark_reports/            # JSON / XLSX kết quả audit fetched về controller
├── Remediation/                             # PLAYBOOK REMEDIATION GỐC – không sửa
│   ├── Remediation_Section1.yaml
│   ├── Remediation_Section2.yml … Section5.yml
│   └── fix_warn_2_12_2_13.yml
└── cismonitoringv2/                         # COMPLIANCE-AS-CODE STACK
    ├── README.md                            # tài liệu chi tiết kỹ thuật
    ├── orchestration/                       # [3] master playbooks
    │   ├── audit_docker.yml                 #   one-shot: audit → push → store → verify → report
    │   ├── collect_results.yml              #   chỉ rebuild evidence store + report (không re-audit)
    │   ├── verify_evidence.yml              #   verify integrity định kỳ
    │   └── remediate_docker.yml             #   chạy mọi Remediation_Section*.yml
    ├── audit/
    │   └── push_metrics.yml                 #   task-file đẩy metric (import từ audit playbook)
    ├── evidence_pipeline/                   # [4] Evidence & Data Pipeline
    │   ├── normalizer.py                    #   chuẩn hoá schema JSON sang canonical record
    │   ├── audit_principles.py              #   UUID / SHA-256 / journal helpers
    │   ├── evidence_collector.py            #   gom → normalize → store + custody log
    │   ├── verify_evidence.py               #   re-hash, log integrity_checks
    │   └── store/
    │       ├── evidence.sqlite              #   Evidence Store (versioned, UUID, SHA-256)
    │       └── journal/custody-YYYYMMDD.jsonl   #   append-only audit trail
    ├── monitoring/                          # [5][7] Pushgateway + Prometheus + Grafana
    │   ├── docker-compose.yml
    │   ├── prometheus/prometheus.yml
    │   ├── grafana/
    │   │   ├── provisioning/                #   datasource + dashboard auto-load
    │   │   └── dashboards/cis-docker.json
    │   ├── scripts/push_metrics.py          #   JSON → Prometheus exposition → PUT
    │   ├── push_all_reports.yml             #   back-fill toàn bộ JSON sẵn có
    │   └── README.md
    ├── reporting/                           # [6] Reporting
    │   ├── generate_report.py               #   HTML (+ PDF tuỳ chọn)
    │   └── output/cis_report_latest.html
    └── security/                            # [9] Security & Secrets
        ├── .env.example
        ├── vault.example.yml
        └── README.md
```

---

## 4. Yêu cầu môi trường

| Thành phần       | Phiên bản              | Ghi chú                                            |
|------------------|------------------------|----------------------------------------------------|
| Ubuntu / Debian  | 20.04 / 22.04 / 24.04  | Cả controller và target host                       |
| Docker Engine    | 24+                    | Trên controller (để chạy monitoring stack)         |
| Docker Compose   | v2 (`docker compose`)  | Tích hợp sẵn trong Docker mới                      |
| Python           | 3.8+                   | Có sẵn trên Ubuntu, không cần `pip install`        |
| Ansible          | 2.12+                  | Trên controller, dùng SSH tới Docker host          |
| SSH access       | có sudo                | Tới mọi host khai báo trong `inventory/hosts`      |

### Cài Docker (nếu chưa có)

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker
```

### (Tuỳ chọn) Cài thư viện sinh PDF

```bash
pip3 install --user weasyprint    # hoặc: sudo apt-get install -y wkhtmltopdf
```

---

## 5. Quick start – 3 lệnh end-to-end

```bash
# 1) Bật stack monitoring (chạy 1 lần)
cd cismonitoringv2/monitoring && docker compose up -d && cd ../..

# 2) Khai báo host trong inventory/hosts (nếu chưa)
echo '[docker]
192.168.1.8 ansible_ssh_common_args="-o StrictHostKeyChecking=no"
' > inventory/hosts

# 3) Chạy toàn bộ pipeline (audit → push → evidence → verify → report)
ansible-playbook -i inventory/hosts cismonitoringv2/orchestration/audit_docker.yml
```

Sau khi chạy:
- **Grafana**  : http://localhost:3000  (`admin / admin`) → dashboard `CIS Docker Benchmark`
- **Prometheus**: http://localhost:9090
- **Pushgateway**: http://localhost:9091
- **HTML report**: `cismonitoringv2/reporting/output/cis_report_latest.html`
- **Evidence Store**: `cismonitoringv2/evidence_pipeline/store/evidence.sqlite`
- **Custody journal**: `cismonitoringv2/evidence_pipeline/store/journal/custody-YYYYMMDD.jsonl`

---

## 6. Các cách sử dụng

### 6.1. Chạy audit + tự động ingest + verify + report

```bash
ansible-playbook -i inventory/hosts \
  cismonitoringv2/orchestration/audit_docker.yml
```

Extra-vars tuỳ chọn:

| Biến                | Mặc định                  | Ý nghĩa                                 |
|---------------------|---------------------------|-----------------------------------------|
| `push_metrics_skip` | `false`                   | Bỏ qua bước push Pushgateway            |
| `collect_skip`      | `false`                   | Bỏ qua evidence collector               |
| `verify_skip`       | `false`                   | Bỏ qua bước verify integrity            |
| `report_skip`       | `false`                   | Bỏ qua sinh HTML report                 |
| `report_pdf`        | `false`                   | Kèm xuất PDF (yêu cầu weasyprint/wkhtmltopdf) |
| `pushgateway_url`   | `http://localhost:9091`   | Đổi đích push                           |

Ví dụ:

```bash
ansible-playbook -i inventory/hosts \
  cismonitoringv2/orchestration/audit_docker.yml \
  -e report_pdf=true \
  -e pushgateway_url=http://10.0.0.5:9091
```

### 6.2. Chỉ re-build evidence store + report (không re-audit)

Hữu ích khi đã có JSON trong `audit/docker_benchmark_reports/`, chỉ muốn cập nhật Evidence Store hoặc đổi format report.

```bash
ansible-playbook cismonitoringv2/orchestration/collect_results.yml \
  -e report_pdf=true        # tuỳ chọn
```

### 6.3. Verify integrity định kỳ (cron / CI)

```bash
ansible-playbook cismonitoringv2/orchestration/verify_evidence.yml
# exit code 0 = mọi run pass; exit 1 = phát hiện tamper hoặc mất file
```

Ví dụ crontab mỗi giờ:

```cron
0 * * * * cd /opt/cis-docker && \
  ansible-playbook cismonitoringv2/orchestration/verify_evidence.yml \
  >> /var/log/cis-verify.log 2>&1
```

### 6.4. Auto-remediation (cẩn thận – thay đổi cấu hình host)

```bash
ansible-playbook -i inventory/hosts \
  cismonitoringv2/orchestration/remediate_docker.yml

# Khuyến nghị chạy audit lại ngay sau đó để kiểm chứng:
ansible-playbook -i inventory/hosts \
  cismonitoringv2/orchestration/audit_docker.yml
```

### 6.5. Sử dụng từng script Python độc lập

```bash
# Evidence Collector
python3 cismonitoringv2/evidence_pipeline/evidence_collector.py \
  --reports-dir audit/docker_benchmark_reports \
  --store       cismonitoringv2/evidence_pipeline/store/evidence.sqlite

# Integrity Verifier
python3 cismonitoringv2/evidence_pipeline/verify_evidence.py \
  --store cismonitoringv2/evidence_pipeline/store/evidence.sqlite

# Report Generator
python3 cismonitoringv2/reporting/generate_report.py \
  --store      cismonitoringv2/evidence_pipeline/store/evidence.sqlite \
  --output-dir cismonitoringv2/reporting/output \
  --pdf                       # tuỳ chọn
  --include-superseded        # tuỳ chọn: hiện cả phiên bản cũ

# Push metric thủ công (debug)
python3 cismonitoringv2/monitoring/scripts/push_metrics.py \
  --json        audit/docker_benchmark_reports/192.168.1.8_section1.json \
  --host        192.168.1.8 \
  --section     section1 \
  --pushgateway http://localhost:9091 \
  --dry-run                   # in metric ra stdout, không PUT
```

### 6.6. Cách cũ – chạy từng section thủ công (giữ tương thích)

```bash
ansible-playbook -i inventory/hosts audit/auditSection1.yaml
ansible-playbook cismonitoringv2/monitoring/push_all_reports.yml
```

---

## 7. Metric exposed trong Pushgateway / Prometheus

| Metric                                       | Labels                                    | Ý nghĩa                                                |
|---------------------------------------------|--------------------------------------------|---------------------------------------------------------|
| `cis_docker_check`                          | `id`, `title`, `sub_section`, `status`     | 1=PASS, 0=FAIL, 2=INFO, 3=WARN, 4=SKIP, 5=ERROR        |
| `cis_docker_summary{outcome=…}`             | `outcome=total\|pass\|fail\|info\|warn\|skip` | Tổng hợp theo trạng thái                              |
| `cis_docker_compliance_percent`             | –                                          | `pass / (pass + fail) × 100`                            |
| `cis_docker_last_audit_timestamp_seconds`   | –                                          | Unix time của lần push gần nhất                         |

Mọi series đều có thêm `instance=<host>` và `section=<sectionN>` từ Pushgateway grouping.

### PromQL mẫu

```promql
# Compliance % hiện tại theo host
cis_docker_compliance_percent

# Tổng FAIL toàn fleet
sum(cis_docker_summary{outcome="fail"})

# FAIL theo host và sub-section
sum by (instance, sub_section) (cis_docker_check{status="FAIL"})

# Host > 24h chưa báo cáo
time() - cis_docker_last_audit_timestamp_seconds > 86400
```

---

## 8. Evidence Store – schema chính

### Bảng `runs` (1 dòng cho mỗi lần ingest)

| Cột                | Kiểu        | Mô tả                                                   |
|--------------------|-------------|---------------------------------------------------------|
| `id`               | INTEGER PK  | rowid                                                   |
| `uuid`             | TEXT UNIQUE | UUIDv4 truy vết                                         |
| `host`             | TEXT        | hostname target                                         |
| `section`          | TEXT        | `section1`, `section2_3`, …                             |
| `generated`        | TEXT        | timestamp do playbook audit ghi                         |
| `source`           | TEXT        | đường dẫn file JSON gốc                                 |
| `source_sha256`    | TEXT        | SHA-256 bytes của file gốc                              |
| `record_sha256`    | TEXT        | SHA-256 của canonical-JSON record đã normalize          |
| `ingested_at`      | TEXT        | timestamp UTC khi ingest                                |
| `ingested_by`      | TEXT        | `user@host` hoặc `CIS_AUDIT_ACTOR`                      |
| `superseded_by`    | INTEGER     | NULL = HEAD đang active; ngược lại trỏ tới run mới hơn  |
| `total/pass_n/fail_n/info_n/warn_n/skip_n/error_n` | INTEGER | counters |
| `compliance`       | REAL        | `pass / (pass+fail) × 100`                              |

### Bảng `checks`, `custody_log`, `integrity_checks`

- `checks`            – 1 dòng / 1 check trong 1 run, mang UUID riêng
- `custody_log`       – event log (INGESTED / DUPLICATE / SUPERSEDED / VERIFIED / INTEGRITY_FAIL)
- `integrity_checks`  – lịch sử mỗi lần verify (hash hiện tại, OK / FAIL, notes)

Mọi event đồng thời được append vào file JSONL trong `store/journal/`.

---

## 9. Security & Secrets

```bash
# 1) Env-file (Docker Compose, runtime config)
cp cismonitoringv2/security/.env.example cismonitoringv2/security/.env
vi cismonitoringv2/security/.env
set -a; source cismonitoringv2/security/.env; set +a
cd cismonitoringv2/monitoring && docker compose up -d

# 2) Ansible Vault (SSH/sudo password, registry token…)
cp cismonitoringv2/security/vault.example.yml cismonitoringv2/security/vault.yml
ansible-vault encrypt cismonitoringv2/security/vault.yml
ansible-playbook -i inventory/hosts \
  cismonitoringv2/orchestration/audit_docker.yml \
  -e @cismonitoringv2/security/vault.yml --ask-vault-pass
```

`.gitignore` đã loại trừ `cismonitoringv2/security/.env` và `vault.yml`, cùng với `*.sqlite`, file journal, HTML report sinh ra → không lo lộ secret hoặc commit nhầm dữ liệu lớn.

Pin actor cho CI / scheduler:

```bash
CIS_AUDIT_ACTOR=ci@github-runner ansible-playbook \
  cismonitoringv2/orchestration/audit_docker.yml -i inventory/hosts
```

---

## 10. Vận hành

### 10.1. Quản lý monitoring stack

```bash
cd cismonitoringv2/monitoring
docker compose ps              # kiểm tra 3 container Up
docker compose logs -f         # xem log
docker compose down            # tắt, giữ data
docker compose down -v         # tắt + xoá hết volume (mất TSDB + dashboard)
```

### 10.2. Pushgateway

- Persistence: bật sẵn (`/data/pushgateway.data`, flush 1 phút).
- Xoá series cũ thủ công:

  ```bash
  curl -X DELETE \
    http://localhost:9091/metrics/job/cis_docker_audit/instance/<host>/section/<section>
  ```

### 10.3. Retention / cấu hình

- Prometheus retention 180 ngày, đổi trong `cismonitoringv2/monitoring/docker-compose.yml` (`--storage.tsdb.retention.time`).
- Đổi mật khẩu Grafana: sửa `GF_SECURITY_ADMIN_PASSWORD` trong `docker-compose.yml` rồi `docker compose up -d`.

### 10.4. Backup / archive

```bash
# Toàn bộ bằng chứng (DB + journal + JSON gốc)
tar -czf cis-evidence-$(date +%Y%m%d).tar.gz \
  audit/docker_benchmark_reports \
  cismonitoringv2/evidence_pipeline/store
```

---

## 11. Troubleshooting

| Triệu chứng                                            | Nguyên nhân                                  | Cách xử lý |
|--------------------------------------------------------|----------------------------------------------|-----------|
| `docker compose up` báo `permission denied` trên socket | User chưa thuộc group `docker`               | `sudo usermod -aG docker $USER && newgrp docker` |
| Pushgateway log `405 Method Not Allowed`               | Script dùng GET/POST sai                     | Đảm bảo dùng `push_metrics.py` v1+ (PUT)         |
| Grafana không thấy metric                              | Pushgateway chưa nhận push                   | `curl http://localhost:9091/metrics` xem có series không |
| Prometheus target `pushgateway` DOWN                   | Chạy stack từ ngoài thư mục `monitoring/`    | `cd cismonitoringv2/monitoring && docker compose up -d` |
| Dashboard trống dù có metric                           | Variable `host` chưa load                    | Refresh trang Grafana sau 30s (provisioning interval) |
| `verify_evidence.py` báo `source file missing`         | Đã xoá file JSON gốc                         | Restore từ backup, hoặc re-audit để sinh file mới |
| `verify_evidence.py` báo `source mismatch`             | File JSON gốc bị sửa sau khi ingest          | Điều tra; ingest lại sẽ supersede HEAD, KHÔNG xoá lịch sử |
| Collector skip JSON với `Expecting value`              | File JSON corrupt                            | Re-audit hoặc fix tay; collector tự skip + log stderr |
| `ansible-playbook: command not found`                  | Chưa cài Ansible                             | `sudo apt-get install -y ansible`                 |
| Báo cáo HTML thiếu chip Integrity OK                   | Chưa từng chạy verify                        | `ansible-playbook .../verify_evidence.yml` rồi gen lại report |

---

## 12. Lifecycle điển hình

```bash
# === Setup 1 lần ===
cd cismonitoringv2/monitoring && docker compose up -d && cd ../..
cp cismonitoringv2/security/.env.example cismonitoringv2/security/.env  # và sửa giá trị

# === Mỗi lần audit (manual / cron / CI) ===
ansible-playbook -i inventory/hosts cismonitoringv2/orchestration/audit_docker.yml

# === Giám sát chủ động ===
# Mỗi giờ verify integrity, fail loud nếu có tamper
ansible-playbook cismonitoringv2/orchestration/verify_evidence.yml

# === Khi phát hiện FAIL ===
ansible-playbook -i inventory/hosts cismonitoringv2/orchestration/remediate_docker.yml
ansible-playbook -i inventory/hosts cismonitoringv2/orchestration/audit_docker.yml   # verify lại

# === Báo cáo định kỳ ===
ansible-playbook cismonitoringv2/orchestration/collect_results.yml -e report_pdf=true
# → cismonitoringv2/reporting/output/cis_report_latest.html (+ .pdf)
```

---

## 13. Tham khảo thêm

- Chi tiết kỹ thuật stack monitoring: `cismonitoringv2/monitoring/README.md`
- Quản lý secret: `cismonitoringv2/security/README.md`
- CIS Docker Benchmark: https://www.cisecurity.org/benchmark/docker
- Pushgateway docs:    https://github.com/prometheus/pushgateway
- Grafana provisioning: https://grafana.com/docs/grafana/latest/administration/provisioning/
- Ansible Vault:       https://docs.ansible.com/ansible/latest/user_guide/vault.html
