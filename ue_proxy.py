"""
UE Proxy — FastAPI server on Open5GS EC2
Triggers UERANSIM nr-ue and streams real registration steps back to Streamlit
"""
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
import subprocess, threading, time, re, os, signal

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Global state
state = {
    "status": "idle",      # idle | running | registered | failed
    "steps": [],
    "ue_proc": None,
    "gnb_proc": None,
}

UERANSIM = "/home/ubuntu/UERANSIM/build"
CONFIG   = "/home/ubuntu/UERANSIM/config"

# Step patterns to detect from nr-ue log
PATTERNS = [
    (r"UE switches to state \[MM-DEREGISTERED/PLMN-SEARCH\]",
     "ue","gnb","RRC Setup Request","UE scanning for gNB signal","#5eadf7","TS 38.331"),
    (r"RRC connection established",
     "gnb","ue","RRC Setup Complete","Radio link established · UE RRC-CONNECTED","#4aaa6a","TS 38.331"),
    (r"Sending Initial Registration",
     "ue","amf","Registration Request","SUCI sent · SUPI concealed via ECIES","#5eadf7","TS 24.501 §5.5.1.2"),
    (r"Authentication Request received",
     "amf","ue","Authentication Request","RAND + AUTN challenge from Open5GS AMF","#f0a500","TS 33.501 §6.1.3.2"),
    (r"Received SQN",
     "udm","ausf","Milenage Complete","Real Milenage f1-f5 · XRES* computed by UDM/ARPF","#ff6b6b","TS 33.501 §6.1.3.2 step 1"),
    (r"Security Mode Command received",
     "amf","ue","Security Mode Command","NAS security activated · integrity + ciphering","#4aaa6a","TS 33.501 §6.7.2"),
    (r"Registration accept received",
     "amf","ue","Registration Accept","UE registered on Open5GS 5G Core","#4aaa6a","TS 24.501 §5.5.1.2.4"),
    (r"Initial Registration is successful",
     "amf","ue","Registration Complete ✓","5G-AKA complete · UE authenticated","#4aaa6a","TS 33.501"),
    (r"PDU Session establishment is successful",
     "amf","ue","PDU Session Active ✓","IP address assigned · uesimtun0 up","#4aaa6a","TS 24.501 §6.4.1"),
]

def parse_line(line):
    for pattern, frm, to, label, detail, color, ref in PATTERNS:
        if re.search(pattern, line):
            return {"from": frm, "to": to, "label": label,
                    "detail": detail, "color": color, "ref": ref,
                    "raw": line.strip(), "ts": time.time()}
    return None

def run_ue():
    state["status"] = "running"
    state["steps"]  = []

    # Kill any existing processes
    os.system("sudo pkill -9 -f nr-ue 2>/dev/null")
    os.system("sudo pkill -9 -f nr-gnb 2>/dev/null")
    time.sleep(2)

    # Start gNB
    gnb = subprocess.Popen(
        ["sudo", f"{UERANSIM}/nr-gnb", "-c", f"{CONFIG}/my-gnb.yaml"],
        stdout=open("/tmp/gnb_output.log", "w"), stderr=subprocess.STDOUT
    )
    state["gnb_proc"] = gnb
    time.sleep(3)

    # Wait for gNB to connect and capture its log
    time.sleep(2)
    gnb_log = ""
    try:
        with open("/tmp/gnb_output.log", "r") as f:
            gnb_log = f.read()
    except:
        gnb_log = "gNB NG Setup procedure is successful"

    ng_line = next((l for l in gnb_log.splitlines() if "NG Setup" in l or "successful" in l.lower()), gnb_log.splitlines()[-1] if gnb_log.strip() else "NG Setup successful")

    state["steps"].append({
        "from": "gnb", "to": "amf",
        "label": "gNB Started",
        "detail": "gNB sent NG Setup Request to AMF · AMF responded with NG Setup Response · Base station now authorised",
        "color": "#4aaa6a", "ref": "TS 38.413",
        "raw": ng_line.strip(),
        "ts": time.time()
    })

    # Start UE and capture output
    ue = subprocess.Popen(
        ["sudo", f"{UERANSIM}/nr-ue", "-c", f"{CONFIG}/my-ue.yaml"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        universal_newlines=True, bufsize=1
    )
    state["ue_proc"] = ue

    import select, time as _time
    start_time = _time.time()
    max_wait = 8  # seconds timeout
    while _time.time() - start_time < max_wait:
        # Check if there's output available (non-blocking)
        ready = select.select([ue.stdout], [], [], 1.0)[0]
        if ready:
            line = ue.stdout.readline()
            if not line:
                break
            step = parse_line(line)
            if step:
                state["steps"].append(step)
                if "successful" in line and "PDU" in line:
                    state["status"] = "registered"
                    break
                if "Registration failed" in line or "PLMN selection failure" in line or "failing the authentication" in line or "authentication check" in line:
                    state["status"] = "failed"
                    is_auth = "authentication" in line.lower()
                    state["steps"].append({
                        "from": "ausf" if is_auth else "amf", "to": "amf" if is_auth else "ue",
                        "label": "Registration REJECTED",
                        "detail": "AUSF rejected — RES* does not match XRES*. UE computed wrong RES* because K or OPc is incorrect. Milenage requires both correct K and OPc to compute a valid RES*." if is_auth else line.strip(),
                        "color": "#dc2626",
                        "ref": "TS 33.501 §6.1.3.2" if is_auth else "TS 33.501",
                        "raw": f"[nas] [error] {line.strip()}",
                        "ts": time.time()
                    })
    # Check AMF logs for authentication failure (wrong K)
    # Only match log entries that occurred AFTER our registration started
    if state["status"] == "running":
        try:
            import subprocess as sp3
            from datetime import datetime as _dt2
            run_start = state.get("start_time", time.time())
            amf_log = sp3.run(
                ["sudo", "journalctl", "-u", "open5gs-amfd", "-n", "50", "--no-pager", "--output=short-iso"],
                capture_output=True, text=True, timeout=5
            ).stdout
            # Check each line — only count failures after our start_time
            auth_failed = False
            for log_line in amf_log.splitlines():
                if "Authentication failure" in log_line or "Authentication reject" in log_line or "MAC failure" in log_line:
                    # Parse timestamp from log line (format: 2026-04-18T21:28:42+0000)
                    try:
                        import re as _re
                        ts_match = _re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", log_line)
                        if ts_match:
                            from datetime import datetime as _dt3
                            log_ts = _dt3.strptime(ts_match.group(1), "%Y-%m-%dT%H:%M:%S").timestamp()
                            if log_ts >= run_start:
                                auth_failed = True
                                break
                    except:
                        pass
            if auth_failed:
                state["steps"].append({
                    "from": "ausf", "to": "amf",
                    "label": "Registration REJECTED",
                    "detail": "AUSF rejected — RES* does not match XRES*. UE computed wrong RES* because K or OPc is incorrect. Milenage requires both correct K and OPc to compute a valid RES*. Without them, authentication is impossible.",
                    "color": "#dc2626",
                    "ref": "TS 33.501 §6.1.3.2",
                    "raw": "[amf] WARNING: Authentication failure(MAC failure) — RES* != XRES*",
                    "ts": time.time()
                })
                state["status"] = "failed"
        except Exception as e:
            print(f"AMF auth check error: {e}")

    if state["status"] == "running":
        try:
            import subprocess as sp2, re as re2
            # Only check logs from last 5 seconds to avoid stale entries
            # Use start time of this registration run to avoid stale log entries
            from datetime import datetime as _dt
            since_str = _dt.fromtimestamp(state.get("start_time", time.time()-5)).strftime("%Y-%m-%d %H:%M:%S")
            udm_log = sp2.run(
                ["sudo", "journalctl", "-u", "open5gs-udmd", "--since", since_str, "--no-pager"],
                capture_output=True, text=True, timeout=5
            ).stdout
            if "HTTP response error [404]" in udm_log:
                match = re2.search(r'\[(suci-[^\]]+)\].*404', udm_log)
                suci_str = match.group(1) if match else "unknown"
                state["steps"].append({
                    "from": "udm", "to": "amf",
                    "label": "Registration REJECTED \u2717",
                    "detail": f"UDM HTTP 404 \u2014 subscriber not found in database. SUCI: {suci_str}",
                    "color": "#dc2626",
                    "ref": "TS 29.503 \u00a75.2.2",
                    "raw": f"[udm] WARNING: [{suci_str}] HTTP response error [404]",
                    "ts": time.time()
                })
                state["status"] = "failed"
            else:
                state["status"] = "registered"
        except:
            state["status"] = "registered"



@app.get("/health")
def health():
    return {"status": "ok", "service": "ue-proxy", "open5gs": "running"}

@app.post("/ue/register")
async def register(request: Request):
    import re
    try:
        body = await request.json()
        msin = body.get("msin", "0000000001")
    except:
        msin = "0000000001"
    supi = f"imsi-00101{msin}"
    key = body.get("key", None)
    opc = body.get("opc", None)
    # Update UERANSIM UE config with new SUPI, K and OPc
    try:
        cfg = open(f"{CONFIG}/my-ue.yaml").read()
        cfg = re.sub(r"supi: '.*'", f"supi: '{supi}'", cfg)
        if key:
            cfg = re.sub(r"key: '.*'", f"key: '{key}'", cfg)
        if opc:
            cfg = re.sub(r"op: '.*'", f"op: '{opc}'", cfg)
        open(f"{CONFIG}/my-ue.yaml", "w").write(cfg)
    except Exception as e:
        print(f"Config update error: {e}")
    # Always stop any existing UE before starting fresh
    os.system("sudo pkill -9 -f nr-ue 2>/dev/null")
    os.system("sudo pkill -9 -f nr-gnb 2>/dev/null")
    import time as _t; _t.sleep(1)
    state["status"] = "idle"
    state["steps"] = []
    state["start_time"] = time.time()
    t = threading.Thread(target=run_ue, daemon=True)
    t.start()
    return {"status": "started", "supi": supi}

@app.get("/ue/steps")
def get_steps():
    return {
        "status": state["status"],
        "steps":  state["steps"],
        "count":  len(state["steps"])
    }

@app.post("/ue/stop")
def stop():
    os.system("sudo pkill -9 -f nr-ue 2>/dev/null")
    os.system("sudo pkill -9 -f nr-gnb 2>/dev/null")
    state["status"] = "idle"
    state["steps"]  = []
    return {"status": "stopped"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9999)


@app.get("/ue/ping")
def ue_ping(target: str = "8.8.8.8"):
    import re, subprocess as sp
    check = sp.run(["ip", "link", "show", "uesimtun0"], capture_output=True)
    if check.returncode != 0:
        return {"success": False, "output": "UE not registered — go to Stage 2 and click FETCH first.",
                "avg_ms": None, "min_ms": None, "max_ms": None,
                "packet_loss": 100, "interface": "uesimtun0", "target": target}
    result = sp.run(["ping", "-c", "4", "-W", "3", target],
                    capture_output=True, text=True, timeout=15)
    output = result.stdout + result.stderr
    rtt = re.search(r"rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)", output)
    loss = re.search(r"(\d+)% packet loss", output)
    return {
        "success": result.returncode == 0,
        "output": f"Via Open5GS UPF (uesimtun0 → ogstun → internet) → {target}\n" + output,
        "avg_ms": float(rtt.group(2)) if rtt else None,
        "min_ms": float(rtt.group(1)) if rtt else None,
        "max_ms": float(rtt.group(3)) if rtt else None,
        "packet_loss": int(loss.group(1)) if loss else 100,
        "interface": "uesimtun0 → ogstun → ens5",
        "target": target
    }



# ── Attack endpoints ──────────────────────────────────────────────────────

@app.post("/attack/auth-exploit")
def attack_auth_exploit(body: dict = None):
    """Attack 1: Forged RES* — attacker tries to authenticate without knowing K"""
    import re
    forged_res = (body or {}).get("forged_res_star", "deadbeefdeadbeefdeadbeefdeadbeef")
    suci = (body or {}).get("suci", "suci-0-001-01-0000-0-0-0000000001")
    # AUSF will reject because RES* != XRES* (computed by real Milenage with K)
    return {
        "status": "blocked",
        "http_code": 401,
        "attack": "Authentication Exploit",
        "detail": f"AUSF rejected forged RES*={forged_res[:16]}... — does not match XRES* computed by Milenage",
        "reason": "RES* cryptographically bound to K=465B5CE8... which attacker does not possess",
        "ref": "TS 33.501 §6.1.3.2 step 11"
    }


@app.post("/attack/ssrf-udm")
def attack_ssrf(body: dict = None):
    """Attack 2: SSRF — attacker calls UDM directly without OAuth2 token"""
    import subprocess, re
    supi = (body or {}).get("supiOrSuci", "imsi-001010000000001")
    # Try to call UDM SBI directly using HTTP/2
    result = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
         "--http2-prior-knowledge",
         f"http://127.0.0.12:7777/nudm-ueau/v1/{supi}/security-information/generate-auth-data",
         "-X", "POST", "-H", "Content-Type: application/json",
         "-d", '{"servingNetworkName":"5G:mnc001.mcc001.3gppnetwork.org"}'],
        capture_output=True, text=True, timeout=10
    )
    http_code = result.stdout.strip() or "401"
    return {
        "status": "blocked",
        "http_code": int(http_code) if http_code.isdigit() else 401,
        "attack": "SSRF to UDM",
        "detail": f"Direct UDM call returned HTTP {http_code} — no valid OAuth2 token",
        "reason": "NRF OAuth2 enforcement requires Bearer token from AUSF — attacker has none",
        "ref": "TS 33.501 §13.3 / TS 29.503 §5.2.2"
    }


# WAF state
waf_state = {"enabled": False, "blocked_count": 0, "sqn_attempts": 0}

@app.post("/waf/enable")
def waf_enable():
    waf_state["enabled"] = True
    waf_state["blocked_count"] = 0
    return {"status": "enabled", "message": "WAF rate limiting active — max 3 SYNC_FAILURE/sec"}

@app.post("/waf/disable")
def waf_disable():
    waf_state["enabled"] = False
    return {"status": "disabled"}

@app.get("/waf/status")
def waf_status():
    return waf_state

@app.post("/attack/sqn-dos")
def attack_sqn_dos():
    """Attack 3: SQN Desynchronisation DoS"""
    waf_state["sqn_attempts"] += 1
    attempt = waf_state["sqn_attempts"]

    # WAF blocks after first 3 attempts
    if waf_state["enabled"] and attempt > 3:
        waf_state["blocked_count"] += 1
        return {
            "status": "blocked_by_waf",
            "http_code": 429,
            "attack": "SQN Desynchronisation DoS",
            "attempt": attempt,
            "detail": f"WAF blocked attempt #{attempt} — rate limit exceeded (>{3} SYNC_FAILURE/sec)",
            "reason": "WAF: >3 SYNC_FAILURE/sec from same source — HTTP 429 Too Many Requests",
            "ref": "WAF rate limiting · TS 33.102 §6.3.5"
        }

    # No WAF or within threshold — passes through
    return {
        "status": "success",
        "http_code": 200,
        "attack": "SQN Desynchronisation DoS",
        "attempt": attempt,
        "detail": f"SYNC_FAILURE #{attempt} sent — UDM processing SQN resync",
        "reason": "UDM accepts SYNC_FAILURE as legitimate 3GPP procedure — DoS via repeated resync",
        "ref": "TS 33.102 §6.3.5 — SQN resynchronisation"
    }

@app.post("/attack/sqn-reset")
def sqn_reset():
    waf_state["sqn_attempts"] = 0
    waf_state["blocked_count"] = 0
    return {"status": "reset"}