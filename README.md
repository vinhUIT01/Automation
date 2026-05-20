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
├── audit/                                  # playbook audit + JSON kết quả
│   ├── auditSection1.yaml ... 6.yaml
│   ├── docker_benchmark_reports/           # JSON / XLSX fetched về controller
│   └── push_metrics.yml                    # task file đẩy metric lên Pushgateway
├── Remediation/                            # playbook remediation
└── monitoring/
    ├── docker-compose.yml                  # Prometheus + Pushgateway + Grafana
    ├── prometheus/prometheus.yml           # scrape config
    ├── grafana/
    │   ├── provisioning/                   # tự nối datasource + load dashboard
    │   └── dashboards/cis-docker.json      # dashboard mặc định
    ├── scripts/push_metrics.py             # JSON → Prometheus exposition → PUT
    ├── push_all_reports.yml                # back-fill toàn bộ JSON sẵn có
    └── README.md
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

```bash
# Lần 1: khởi động hệ thống
cd monitoring && docker compose up -d && cd ..

# Lần 1: back-fill toàn bộ kết quả cũ
ansible-playbook monitoring/push_all_reports.yml

# Mở Grafana: http://localhost:3000  (admin/admin)
# → Dashboard "CIS Docker Benchmark" có dữ liệu

# Lần sau: chạy audit như cũ
ansible-playbook -i inventory/hosts audit/auditSection1.yaml
# Nếu đã tích hợp 5.2 thì metric tự lên
# Nếu chưa: chạy lại 5.1 để back-fill
```

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
