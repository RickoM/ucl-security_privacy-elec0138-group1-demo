# UCL ELEC0138 — 5G Security Lab
**Security and Privacy · MSc Telecommunications with Business · UCL 2025/26**

A fully working 5G Security demonstration lab built on **Open5GS** and **UERANSIM** running on **AWS EC2**. The demo shows real 5G registration, live attack simulations against the 5G core, and WAF mitigation — all controlled from a single HTML file in the browser.

---

## 📋 Overview

| Component | Technology | Purpose |
|---|---|---|
| Frontend | `5g_demo.html` (HTML + JS) | 7-stage interactive demo UI |
| Proxy | `ue_proxy.py` (FastAPI + Python) | REST API bridging browser to 5G core |
| Radio Simulator | UERANSIM v3.2.6 (C++) | Simulates UE + gNB |
| 5G Core | Open5GS v2.7.6 | Real AMF, AUSF, UDM, UPF, NRF |
| Infrastructure | AWS EC2 t3.medium | Ubuntu 22.04, eu-west-2 |
| Database | MongoDB | Subscriber profiles |

---

## 🏗️ System Architecture

```
Browser (5g_demo.html)
        │
        │ HTTP REST (port 9999)
        ▼
FastAPI Proxy (ue_proxy.py) ── AWS EC2 13.43.42.62
        │
        ├── subprocess spawn ──► UERANSIM (nr-gnb + nr-ue)
        │                              │
        │                              │ NGAP/NAS (N2)
        │                              ▼
        └── journalctl / curl ──► Open5GS 5G Core
                                       │
                                  AMF  │  AUSF  │  UDM
                                  127.0.0.5  │  127.0.0.11  │  127.0.0.12
                                       │
                                      UPF (127.0.0.7)
                                       │
                                    Internet
```

---

## 🚀 EC2 Setup — Step by Step

### 1. Launch EC2 Instance

- **AMI:** Ubuntu 22.04 LTS
- **Instance type:** t3.medium (2 vCPU, 4GB RAM)
- **Region:** eu-west-2 (London)
- **Storage:** 20GB gp2
- **Security group inbound rules:**
  - Port 22 (SSH) — your IP
  - Port 9999 (TCP) — 0.0.0.0/0 (proxy)
  - Port 38412 (SCTP) — 127.0.0.0/8 (NGAP)

### 2. Connect to EC2

```bash
ssh -i ~/open5gs-key.pem ubuntu@<YOUR_EC2_IP>
```

Or use **EC2 Instance Connect** from the AWS Console.

### 3. Update System

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y curl wget git cmake build-essential \
    libsctp-dev lksctp-tools iproute2 \
    python3-pip python3-dev
```

---

## 📡 Open5GS Installation

### 1. Install Open5GS

```bash
sudo add-apt-repository ppa:open5gs/latest
sudo apt update
sudo apt install -y open5gs
```

### 2. Install MongoDB

```bash
curl -fsSL https://pgp.mongodb.com/server-6.0.asc | sudo gpg -o /usr/share/keyrings/mongodb-server-6.0.gpg --dearmor
echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-6.0.gpg ] https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/6.0 multiverse" | sudo tee /etc/apt/sources.list.d/mongodb-org-6.0.list
sudo apt update
sudo apt install -y mongodb-org
sudo systemctl start mongod
sudo systemctl enable mongod
```

### 3. Configure Open5GS PLMN

Edit `/etc/open5gs/amf.yaml` — set MCC/MNC:

```yaml
amf:
  plmn_support:
    - plmn_id:
        mcc: 001
        mnc: 01
```

Edit `/etc/open5gs/nrf.yaml` — same PLMN:

```yaml
nrf:
  serving:
    - plmn_id:
        mcc: 001
        mnc: 01
```

### 4. Start Open5GS Services

```bash
sudo systemctl enable open5gs-nrfd open5gs-scpd open5gs-amfd \
    open5gs-ausfd open5gs-udmd open5gs-udrd open5gs-pcfd \
    open5gs-nssfd open5gs-bsfd open5gs-upfd open5gs-smfd
sudo systemctl start open5gs-nrfd open5gs-scpd open5gs-amfd \
    open5gs-ausfd open5gs-udmd open5gs-udrd open5gs-pcfd \
    open5gs-nssfd open5gs-bsfd open5gs-upfd open5gs-smfd
```

### 5. Add Subscribers to MongoDB

```bash
mongosh open5gs --eval "
db.subscribers.insertOne({
  imsi: '001010000000001',
  security: {
    k: '465B5CE8B199B49FAA5F0A2EE238A6BC',
    op: null,
    opc: 'E8ED289DEBA952E4283B54E88E6183CA',
    amf: '8000',
    sqn: Long(0)
  },
  ambr: { downlink: { value: 1, unit: 3 }, uplink: { value: 1, unit: 3 } },
  slice: [{ sst: 1, default_indicator: true, session: [{
    name: 'internet', type: 3,
    qos: { index: 9, arp: { priority_level: 8, pre_emption_capability: 1, pre_emption_vulnerability: 1 }},
    ambr: { downlink: { value: 1, unit: 3 }, uplink: { value: 1, unit: 3 }},
    ue: { addr: '' }, pcc_rule: []
  }]}],
  access_restriction_data: 32, subscriber_status: 0,
  network_access_mode: 0, subscribed_rau_tau_timer: 12, __v: 0
});
"
```

Repeat for subscriber 2 with `imsi: '001010000000002'`.

---

## 📻 UERANSIM Installation

### 1. Install Dependencies

```bash
sudo apt install -y make gcc g++ libsctp-dev lksctp-tools iproute2
sudo snap install cmake --classic
```

### 2. Build UERANSIM

```bash
cd ~
git clone https://github.com/aligungr/UERANSIM
cd UERANSIM
make
```

### 3. Configure gNB

Save as `~/UERANSIM/config/my-gnb.yaml`:

```yaml
mcc: '001'
mnc: '01'
nci: '0x000000010'
idLength: 32
tac: 1
linkIp: 127.0.0.1
ngapIp: 127.0.0.5
gtpIp: 127.0.0.5
amfConfigs:
  - address: 127.0.0.5
    port: 38412
slices:
  - sst: 1
ignoreStreamIds: true
```

### 4. Configure UE

Save as `~/UERANSIM/config/my-ue.yaml`:

```yaml
supi: 'imsi-001010000000001'
mcc: '001'
mnc: '01'
key: '465B5CE8B199B49FAA5F0A2EE238A6BC'
op: 'E8ED289DEBA952E4283B54E88E6183CA'
opType: 'OPC'
amf: '8000'
gnbSearchList:
  - 127.0.0.1
sessions:
  - type: 'IPv4'
    apn: 'internet'
    slice:
      sst: 1
configured-nssai:
  - sst: 1
default-nssai:
  - sst: 1
    sd: 1
integrity:
  IA1: true
  IA2: true
  IA3: true
ciphering:
  EA1: true
  EA2: true
  EA3: true
integrityMaxRate:
  uplink: 'full'
  downlink: 'full'
```

---

## ⚡ FastAPI Proxy Setup

### 1. Install Dependencies

```bash
pip3 install fastapi uvicorn --break-system-packages
```

### 2. Copy Proxy File

Copy `ue_proxy.py` to `~/ue_proxy.py` on EC2.

### 3. Create Systemd Service

```bash
sudo tee /etc/systemd/system/ue-proxy.service << EOF
[Unit]
Description=UE Proxy FastAPI
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu
ExecStart=/usr/bin/python3 -m uvicorn ue_proxy:app --host 0.0.0.0 --port 9999
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ue-proxy
sudo systemctl start ue-proxy
```

### 4. Verify

```bash
curl http://localhost:9999/health
# Should return: {"status":"ok","service":"ue-proxy","open5gs":"running"}
```

---

## 🌐 Running the Demo

1. Open `5g_demo.html` in any browser
2. Enter your EC2 URL: `http://<EC2_IP>:9999`
3. Navigate through the 7 stages:

| Stage | Description |
|---|---|
| 1 — Architecture | 5G network + demo system overview |
| 2 — Registration | Real UERANSIM registration on Open5GS |
| 3 — Data Proof | Ping internet through Open5GS UPF |
| 4 — Attack 1 | Forged RES* authentication exploit |
| 5 — Attack 2 | SSRF on UDM SBI (bypasses AMF/AUSF) |
| 6 — Attack 3 | SQN Desynchronisation DoS |
| 7 — WAF | Rate limiting mitigation |

---

## 🔐 Test Credentials

| Parameter | Valid | Invalid |
|---|---|---|
| MSIN | `0000000001` or `0000000002` | `0000000099` (not in DB) |
| K | `465B5CE8B199B49FAA5F0A2EE238A6BC` | `FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF` |
| OPc | `E8ED289DEBA952E4283B54E88E6183CA` | `FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF` |

---

## 🛡️ Attack Summary

| Attack | Type | Endpoint | Result |
|---|---|---|---|
| Auth Exploit | Real | `/attack/auth-exploit` | HTTP 401 — AUSF rejects RES* ≠ XRES* |
| SSRF on UDM | Real | `/attack/ssrf-udm` | HTTP 400 — No OAuth2 Bearer token |
| SQN DoS | Simulated | `/attack/sqn-dos` | HTTP 200 — UDM overwhelmed |
| WAF Mitigation | Real | `/waf/enable` | HTTP 429 — blocked after 3 attempts |

---

## 📁 Repository Structure

```
├── 5g_demo.html          # Frontend — 7-stage interactive demo
├── ue_proxy.py           # FastAPI proxy — REST API on EC2
├── configs/
│   ├── my-gnb.yaml       # UERANSIM gNB configuration
│   └── my-ue.yaml        # UERANSIM UE configuration
└── README.md             # This file
```

---

## 📚 3GPP References

| Spec | Section | Topic |
|---|---|---|
| TS 33.501 | §6.1.3.2 | 5G-AKA authentication procedure |
| TS 33.501 | §13.3 | NRF OAuth2 enforcement |
| TS 33.102 | §6.3.5 | SQN resynchronisation (DoS vector) |
| TS 29.503 | §5.2.2 | UDM SBI interface |
| TS 24.501 | §5.5.1.2 | NAS registration procedure |
| TS 38.331 | — | RRC protocol |
| TS 38.413 | — | NGAP protocol |
| TS 33.117 | — | WAF rate limiting |

---

## ⚠️ Security Notice

This project is for **educational purposes only** as part of UCL ELEC0138. All attacks are demonstrated in a controlled isolated environment against infrastructure we own and operate. Do not use these techniques against real networks.

---

## 👥 Authors

**Group 1** — UCL MSc Telecommunications with Business  
ELEC0138 Security and Privacy · Academic Year 2025/26
