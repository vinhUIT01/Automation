# CIS Docker Benchmark – Monitoring Stack

Hệ thống lưu trữ và giám sát kết quả audit CIS Docker bằng **Prometheus + Pushgateway + Grafana**, tích hợp trực tiếp với các playbook Ansible trong `audit/`.

---

## 1. Kiến trúc

```
┌──────────────────┐   push (HTTP PUT)   ┌──────────────┐  scrape  ┌────────────┐  query  ┌─────────┐
│  Ansible audit   │ ──────────────────▶ │  Pushgateway │ ───────▶│ Prometheus │ ──────▶│ Grafana │
│  (batch job)     │                     │   :9091      │         │   :9090    │        │  :3000  │
└──────────────────┘                     └──────────────┘         └────────────┘        └─────────┘
        │
        ▼
JSON / XLSX reports
audit/docker_benchmark_reports/
```

- **Ansible** chạy audit → sinh JSON → đẩy metric lên Pushgateway.
- **Pushgateway** giữ tạm metric của batch job.
- **Prometheus** scrape Pushgateway mỗi 15 giây, lưu vào TSDB (retention 180 ngày).
- **Grafana** truy vấn Prometheus, hiển thị dashboard.
- **JSON/XLSX gốc** vẫn được giữ trong `audit/docker_benchmark_reports/` làm bản ghi chính thức của mỗi lần audit.

---

## 2. Cấu trúc thư mục

```
.
├── ansible.cfg
├── inventory/hosts
├── audit/                                  # playbook audit + JSON kết quả (giữ nguyên)
│   ├── auditSection1.yaml ... 6.yaml
│   └── docker_benchmark_reports/           # JSON / XLSX fetched về controller
├── Remediation/                            # playbook remediation (giữ nguyên)
└── cismonitoringv2/                        # = Compliance-as-Code stack
    ├── orchestration/                      # [3] master playbooks
    │   ├── audit_docker.yml                #   chạy toàn bộ audit + pipeline
    │   ├── collect_results.yml             #   re-build evidence + report
    │   ├── verify_evidence.yml             #   verify integrity (định kỳ)
    │   └── remediate_docker.yml            #   chạy toàn bộ remediation
    ├── audit/
    │   └── push_metrics.yml                #   task file đẩy metric (cũ)
    ├── evidence_pipeline/                  # [4] Evidence & Data Pipeline
    │   ├── normalizer.py                   #   chuẩn hoá schema JSON
    │   ├── audit_principles.py             #   UUID + SHA-256 + journal helpers
    │   ├── evidence_collector.py           #   gom → normalize → store + custody
    │   ├── verify_evidence.py              #   re-hash + log integrity_checks
    │   └── store/
    │       ├── evidence.sqlite             #   Evidence Store (versioned, UUID, hash)
    │       └── journal/custody-YYYYMMDD.jsonl  #   append-only custody journal
    ├── reporting/                          # [6] Reporting
    │   ├── generate_report.py              #   HTML (+ PDF tuỳ chọn)
    │   └── output/cis_report_latest.html
    ├── monitoring/                         # [5][7] Pushgateway + Prometheus + Grafana
    │   ├── docker-compose.yml
    │   ├── prometheus/prometheus.yml
    │   ├── grafana/...
    │   ├── scripts/push_metrics.py
    │   └── push_all_reports.yml
    └── security/                           # [9] Security & Secrets
        ├── .env.example
        ├── vault.example.yml
        └── README.md
```

### Sơ đồ ↔ thư mục

| Khối trong sơ đồ                       | Implementation                                      |
| -------------------------------------- | --------------------------------------------------- |
| [1] User / Trigger                     | `ansible-playbook …/audit_docker.yml`               |
| [2] Docker Host                        | hosts trong `inventory/hosts`                       |
| [3] Orchestration & Scanning (Ansible) | `cismonitoringv2/orchestration/*.yml` + `audit/*`   |
| [4] Evidence & Data Pipeline           | `cismonitoringv2/evidence_pipeline/`                |
| [5] Metrics Gateway (Pushgateway)      | `monitoring/docker-compose.yml` (port 9091)         |
| [6] Reporting (HTML / PDF)             | `cismonitoringv2/reporting/generate_report.py`      |
| [7] Monitoring & Dashboard             | `monitoring/` (Prometheus 9090, Grafana 3000)       |
| [8] Remediation (Automated)            | `orchestration/remediate_docker.yml` + `Remediation/` |
| [9] Security & Secrets                 | `cismonitoringv2/security/`                         |

### Tuân thủ 3 Audit Principles

| Nguyên tắc | Implementation |
|---|---|
| **1. Chain of Custody** (Chuỗi hành trình bằng chứng) | Mỗi run được gán UUIDv4 + metadata `ingested_at` (ISO-8601 UTC) + `ingested_by` (user@host, override qua `CIS_AUDIT_ACTOR`) + đường dẫn file gốc. Mọi sự kiện thu thập / verify / supersede ghi vào bảng `custody_log` và file append-only `store/journal/custody-YYYYMMDD.jsonl`. Không bao giờ ghi đè – phiên bản cũ giữ nguyên, gán `superseded_by` trỏ tới run mới. |
| **2. Integrity & Hashing** (Tính toàn vẹn) | Tại thời điểm ingest tính cả `source_sha256` (hash file JSON gốc) và `record_sha256` (hash canonical-JSON của record đã normalize). `verify_evidence.py` re-hash định kỳ, ghi `integrity_checks` + custody event `VERIFIED` / `INTEGRITY_FAIL`. Phát hiện ngay khi file gốc bị sửa hoặc DB bị tamper. |
| **3. Traceability** (Khả năng truy vết) | Mỗi run **và** mỗi check đều mang UUID riêng. Run trỏ về file gốc qua cột `source` + `source_sha256` → có thể truy ngược tới bytes nguyên bản. Báo cáo HTML hiển thị UUID + 16 ký tự đầu của hash + ingestion metadata + custody log 200 sự kiện gần nhất. |

Bật / tắt từng bước qua extra-vars:

```bash
# Skip integrity verify trong audit_docker.yml
ansible-playbook .../audit_docker.yml -e verify_skip=true

# Chạy verify độc lập (cron / CI)
ansible-playbook cismonitoringv2/orchestration/verify_evidence.yml

# Pin actor cho CI / scheduler
CIS_AUDIT_ACTOR=ci@github-runner ansible-playbook .../audit_docker.yml
```

---

## 3. Yêu cầu trên Ubuntu

| Phần | Phiên bản | Ghi chú |
|---|---|---|
| Ubuntu | 20.04 / 22.04 / 24.04 | |
| Docker Engine | 24+ | Cần để chạy stack monitoring |
| Docker Compose | v2 (tích hợp trong `docker`) | `docker compose ...` |
| Python | 3.6+ | Đã sẵn trên Ubuntu, không cần `pip install` |
| Ansible | bất kỳ phiên bản đang dùng cho audit | Chỉ cần khi tích hợp vào playbook |

Cài Docker nếu chưa có:

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker
```

---

## 4. Khởi động stack monitoring

```bash
cd monitoring
docker compose up -d
docker compose ps          # kiểm tra 3 container đang Up
```

Truy cập:

| Service     | URL                       | Credential    |
| ----------- | ------------------------- | ------------- |
| Grafana     | http://localhost:3000     | admin / admin |
| Prometheus  | http://localhost:9090     | –             |
| Pushgateway | http://localhost:9091     | –             |

Dashboard **"CIS Docker Benchmark"** được provision tự động trong folder `CIS Docker` của Grafana.

Tắt stack: `docker compose down`. Dữ liệu được giữ trong Docker volume (`prometheus-data`, `pushgateway-data`, `grafana-data`); muốn xoá hết: `docker compose down -v`.

---

## 5. Đẩy metric lên Pushgateway

Có 3 cách, dùng cách nào cũng được:

### 5.1. Back-fill từ JSON đã có (đơn giản nhất, dùng cho lần đầu)

```bash
ansible-playbook monitoring/push_all_reports.yml
```

Playbook tự quét `audit/docker_benchmark_reports/*_section*.json` và đẩy từng file lên Pushgateway. Đổi địa chỉ Pushgateway:

```bash
ansible-playbook monitoring/push_all_reports.yml \
  -e pushgateway_url=http://10.0.0.5:9091
```

### 5.2. Tích hợp vào audit playbook (tự động đẩy sau mỗi lần audit)

Mở `audit/auditSection1.yaml` (hoặc bất kỳ section nào), thêm khối sau **sau task `Report | Fetch JSON to controller`**:

```yaml
- name: Push CIS metrics to Pushgateway
  ansible.builtin.import_tasks: push_metrics.yml
  vars:
    section_name: section1
    json_local: "./docker_benchmark_reports/{{ inventory_hostname }}_section1.json"
```

Đổi `section_name` và đường dẫn `json_local` cho mỗi file section tương ứng. Từ lần audit kế tiếp, metric sẽ tự lên dashboard.

Tham số override khi chạy:
- `-e pushgateway_url=http://host:9091` – đổi đích push
- `-e push_metrics_skip=true` – tạm tắt push

### 5.3. Push một file JSON thủ công (debug)

```bash
python3 monitoring/scripts/push_metrics.py \
  --json audit/docker_benchmark_reports/192.168.1.8_section1.json \
  --host 192.168.1.8 \
  --section section1 \
  --pushgateway http://localhost:9091
```

Thêm `--dry-run` để chỉ in metric ra stdout, không push.

---

## 6. Quy trình điển hình

### 6.1. Cách cũ (chạy từng section)

```bash
cd cismonitoringv2/monitoring && docker compose up -d && cd ../..
ansible-playbook cismonitoringv2/monitoring/push_all_reports.yml
ansible-playbook -i inventory/hosts audit/auditSection1.yaml
```

### 6.2. Cách mới (one-shot end-to-end theo sơ đồ)

```bash
# Bật monitoring stack 1 lần
cd cismonitoringv2/monitoring && docker compose up -d && cd ../..

# Chạy TOÀN BỘ pipeline: audit → push metrics → evidence store → HTML report
ansible-playbook -i inventory/hosts \
  cismonitoringv2/orchestration/audit_docker.yml

# Nếu chỉ muốn dựng lại Evidence Store + report từ JSON đã có
ansible-playbook cismonitoringv2/orchestration/collect_results.yml \
  -e report_pdf=true            # tuỳ chọn: kèm PDF

# Auto-remediate (cẩn thận – thay đổi cấu hình target)
ansible-playbook -i inventory/hosts \
  cismonitoringv2/orchestration/remediate_docker.yml
```

Kết quả sau khi chạy:
- Pushgateway → Prometheus → Grafana có dữ liệu mới (xem mục 7 dashboard).
- `cismonitoringv2/evidence_pipeline/store/evidence.sqlite` được cập nhật.
- `cismonitoringv2/reporting/output/cis_report_latest.html` là báo cáo HTML mới nhất.

---

## 7. Metric exposed

| Metric | Labels | Ý nghĩa |
|---|---|---|
| `cis_docker_check` | `id`, `title`, `sub_section`, `status` | 1=PASS, 0=FAIL, 2=INFO, 3=WARN, 4=SKIP, 5=ERROR |
| `cis_docker_summary{outcome=...}` | `outcome=total\|pass\|fail\|info\|warn\|skip` | Tổng hợp số check theo trạng thái |
| `cis_docker_compliance_percent` | – | `pass / (pass + fail) × 100` |
| `cis_docker_last_audit_timestamp_seconds` | – | Unix time lần push gần nhất |

Mọi series đều có thêm `instance=<host>` và `section=<sectionN>` do Pushgateway grouping.

### PromQL mẫu

```promql
# Compliance % hiện tại của từng host
cis_docker_compliance_percent

# Tổng số check FAIL toàn fleet
sum(cis_docker_summary{outcome="fail"})

# Số check FAIL theo host và sub-section
sum by (instance, sub_section) (cis_docker_check{status="FAIL"})

# Host nào hơn 24h chưa báo cáo
time() - cis_docker_last_audit_timestamp_seconds > 86400
```

---

## 8. Vận hành

- **Pushgateway persistence**: bật sẵn (`/data/pushgateway.data`, flush mỗi 1 phút) → restart container không mất snapshot gần nhất.
- **Grouping**: `job=cis_docker_audit / instance=<host> / section=<section>` → push section khác nhau không ghi đè nhau.
- **Xoá series cũ thủ công**:
  ```bash
  curl -X DELETE http://localhost:9091/metrics/job/cis_docker_audit/instance/<host>/section/<section>
  ```
- **Retention**: 180 ngày, đổi trong `monitoring/docker-compose.yml` (`--storage.tsdb.retention.time`).
- **Đổi mật khẩu Grafana**: sửa biến `GF_SECURITY_ADMIN_PASSWORD` trong `monitoring/docker-compose.yml` rồi `docker compose up -d`.

---

## 9. Troubleshooting

| Triệu chứng | Nguyên nhân | Cách xử lý |
|---|---|---|
| `docker compose up` lỗi `permission denied` trên socket | User chưa thuộc group `docker` | `sudo usermod -aG docker $USER && newgrp docker` |
| Pushgateway log `405 Method Not Allowed` | Script dùng GET/POST sai | Đảm bảo dùng `push_metrics.py` v1 trở lên (PUT) |
| Grafana không thấy metric | Pushgateway chưa nhận push | `curl http://localhost:9091/metrics` xem có series không |
| Prometheus target `pushgateway` DOWN | Chạy stack từ ngoài thư mục `monitoring/` | `cd monitoring && docker compose up -d` |
| Dashboard trống dù có metric | Variable `host` chưa load | Refresh trang Grafana sau 30s (provisioning interval) |

---

## 10. Tham khảo thêm

- Chi tiết kỹ thuật và PromQL nâng cao: `monitoring/README.md`
- Pushgateway docs: https://github.com/prometheus/pushgateway
- Grafana provisioning: https://grafana.com/docs/grafana/latest/administration/provisioning/
