# -*- coding: utf-8 -*-
"""
已有 VPS -> 3x-ui + Cloudflare Tunnel 一键部署脚本

功能：
- 从当前目录 .env 自动读取配置；.env 优先于系统环境变量
- SSH 登录已有 VPS
- 检测到 3x-ui 已安装时，可自动备份并卸载重装
- 安装/配置 3x-ui 和 cloudflared
- 使用 Cloudflare API 创建 Tunnel、配置路由、创建/更新 DNS
- 使用 3x-ui API Token 创建 VLESS + WebSocket 入站和客户端
- 输出 vless:// 分享链接
- 自动测试 VLESS 可用性
- 无论成功或失败都保存日志

Win10 依赖：
    py -m pip install requests paramiko

运行：
    py .\\dp_vpn.py
"""

import base64
import getpass
import hashlib
import json
import os
import re
import secrets
import shlex
import socket
import ssl
import string
import sys
import time
import traceback
import urllib.parse
import uuid
from pathlib import Path

import requests

try:
    import paramiko
except ImportError:
    paramiko = None


CF_API = "https://api.cloudflare.com/client/v4"
LOG_FILE_PATH = None
_LOG_FILE_HANDLE = None
_ORIGINAL_STDOUT = sys.stdout
_ORIGINAL_STDERR = sys.stderr


class TeeStream:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for stream in self.streams:
            try:
                stream.write(data)
                stream.flush()
            except Exception:
                pass

    def flush(self):
        for stream in self.streams:
            try:
                stream.flush()
            except Exception:
                pass

    def isatty(self):
        return False


def start_deploy_log():
    global LOG_FILE_PATH, _LOG_FILE_HANDLE

    logs_dir = Path.cwd() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    ts = time.strftime("%Y%m%d_%H%M%S")
    LOG_FILE_PATH = logs_dir / f"wandering_3xui_deploy_{ts}.log"
    _LOG_FILE_HANDLE = LOG_FILE_PATH.open("a", encoding="utf-8", errors="replace")

    sys.stdout = TeeStream(_ORIGINAL_STDOUT, _LOG_FILE_HANDLE)
    sys.stderr = TeeStream(_ORIGINAL_STDERR, _LOG_FILE_HANDLE)

    print("=" * 80)
    print(f"部署日志文件：{LOG_FILE_PATH}")
    print(f"开始时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Python：{sys.version.split()[0]}")
    print(f"工作目录：{Path.cwd()}")
    print("=" * 80)


def close_deploy_log():
    global _LOG_FILE_HANDLE

    try:
        print("=" * 80)
        print(f"日志结束时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
        if LOG_FILE_PATH:
            print(f"日志已保存：{LOG_FILE_PATH}")
        print("=" * 80)
    except Exception:
        pass

    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass

    if _LOG_FILE_HANDLE:
        try:
            _LOG_FILE_HANDLE.close()
        except Exception:
            pass
        _LOG_FILE_HANDLE = None


def write_error_report(exc):
    ts = time.strftime("%Y%m%d_%H%M%S")
    error_file = Path.cwd() / f"deploy_error_{ts}.json"
    payload = {
        "ok": False,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "error_type": type(exc).__name__,
        "error_message": str(exc),
        "log_file": str(LOG_FILE_PATH) if LOG_FILE_PATH else None,
        "traceback": traceback.format_exc(),
    }
    error_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return error_file


def load_dotenv(path=".env"):
    result = {}
    p = Path(path)
    if not p.exists():
        return result

    for raw_line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if (
            (value.startswith('"') and value.endswith('"'))
            or (value.startswith("'") and value.endswith("'"))
        ):
            value = value[1:-1]

        result[key] = value

    return result


# .env 优先于系统环境变量，避免旧 Windows 环境变量覆盖当前项目配置。
ENV = {**os.environ, **load_dotenv()}


def env_get(name, default=None):
    value = ENV.get(name)
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip()


def cfg_bool(name, default=False):
    value = env_get(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "y", "on")


def ask(prompt, default=None, secret=False):
    label = prompt
    if default not in (None, ""):
        label += f" [{default}]"
    label += ": "

    if secret:
        value = getpass.getpass(label).strip()
    else:
        value = input(label).strip()

    if not value and default is not None:
        return str(default).strip()
    return value


def cfg(name, prompt, default=None, secret=False, required=True):
    value = env_get(name, default)
    if value not in (None, ""):
        return str(value).strip()

    value = ask(prompt, default, secret=secret)
    if required and not value:
        raise ValueError(f"{name} 不能为空")
    return value.strip()


def normalize_domain(domain):
    domain = domain.strip().lower()
    domain = domain.replace("https://", "").replace("http://", "").strip("/")
    if not re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", domain):
        raise ValueError(f"域名格式不正确：{domain}")
    return domain


def normalize_subdomain(name):
    name = name.strip().lower()
    name = re.sub(r"[^a-z0-9-]", "-", name)
    name = re.sub(r"-+", "-", name).strip("-")
    if not name:
        raise ValueError("节点名称不能为空")
    return name


def normalize_path(path):
    path = (path or "").strip().strip("/")
    if not path:
        return "abc123"
    path = re.sub(r"[^a-zA-Z0-9_\-./]", "-", path).strip("/")
    return path or "abc123"


def rand_text(length=16):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def rand_path(node_name):
    return f"{normalize_subdomain(node_name)}-{rand_text(10)}"


# -----------------------------------------------------------------------------
# Cloudflare API
# -----------------------------------------------------------------------------


def cf_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "tierhive-cloudflare-vpn/1.0",
    }


def cf_proxy_config():
    proxy = env_get("CF_API_PROXY")
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def cf_request(method, path, token, **kwargs):
    url = CF_API + path
    timeout = int(env_get("CF_API_TIMEOUT", "60"))
    retries = int(env_get("CF_API_RETRIES", "3"))
    retry_sleep = int(env_get("CF_API_RETRY_SLEEP", "5"))
    proxies = cf_proxy_config()

    if proxies:
        kwargs.setdefault("proxies", proxies)

    last_error = None

    for attempt in range(1, retries + 1):
        try:
            print(f"Cloudflare API: {method} {path} attempt {attempt}/{retries}")
            if proxies:
                print(f"Cloudflare API proxy: {proxies['https']}")

            resp = requests.request(
                method,
                url,
                headers=cf_headers(token),
                timeout=timeout,
                **kwargs,
            )

            try:
                data = resp.json()
            except Exception:
                raise RuntimeError(
                    f"Cloudflare 返回非 JSON：HTTP {resp.status_code} {resp.text[:500]}"
                )

            if not data.get("success"):
                raise RuntimeError(
                    f"Cloudflare API 失败：{method} {path}\n"
                    f"HTTP {resp.status_code}\n"
                    f"{json.dumps(data, ensure_ascii=False, indent=2)[:2000]}"
                )

            return data.get("result")

        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_error = exc
            print(f"⚠️ Cloudflare API 网络异常：{type(exc).__name__}: {exc}")
            if attempt < retries:
                print(f"等待 {retry_sleep} 秒后重试 Cloudflare API...")
                time.sleep(retry_sleep)
                continue
            break

    raise RuntimeError(
        "Cloudflare API 多次请求失败。\n"
        "如果本地网络访问 Cloudflare API 不稳定，可在 .env 设置：\n"
        "CF_API_PROXY=http://127.0.0.1:10808\n"
        f"最后错误：{repr(last_error)}"
    )


def cf_get_account_id(token):
    account_id = env_get("CF_ACCOUNT_ID")
    if account_id:
        return account_id, "(from .env)"

    result = cf_request("GET", "/accounts?per_page=50", token)
    if not result:
        raise RuntimeError("没有读取到 Cloudflare Account")

    if len(result) == 1:
        return result[0]["id"], result[0].get("name", "")

    print("检测到多个 Cloudflare Account：")
    for index, account in enumerate(result, 1):
        print(f"{index}. {account.get('name')} | {account.get('id')}")

    selected = int(ask("请选择 Account 序号", "1"))
    account = result[selected - 1]
    return account["id"], account.get("name", "")


def cf_get_zone_id(token, domain):
    zone_id = env_get("CF_ZONE_ID")
    if zone_id:
        return zone_id, domain

    result = cf_request(
        "GET",
        f"/zones?name={urllib.parse.quote(domain)}&per_page=50",
        token,
    )
    if not result:
        raise RuntimeError(f"没有读取到 Zone：{domain}")

    return result[0]["id"], result[0].get("name", domain)


def cf_create_tunnel(token, account_id, tunnel_name):
    existing = cf_request(
        "GET",
        f"/accounts/{account_id}/cfd_tunnel?is_deleted=false&per_page=100",
        token,
    )

    names = {item.get("name") for item in (existing or [])}
    real_name = tunnel_name
    if real_name in names:
        real_name = f"{tunnel_name}-{int(time.time())}"
        print(f"⚠️ 已存在同名 Tunnel，自动改名为：{real_name}")

    result = cf_request(
        "POST",
        f"/accounts/{account_id}/cfd_tunnel",
        token,
        json={"name": real_name, "config_src": "cloudflare"},
    )

    tunnel_id = result["id"]
    tunnel_token = result.get("token")

    if not tunnel_token:
        tunnel_token = cf_request(
            "GET",
            f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/token",
            token,
        )

    return real_name, tunnel_id, tunnel_token


def cf_put_tunnel_config(
    token,
    account_id,
    tunnel_id,
    panel_domain,
    panel_port,
    proxy_domain,
    proxy_port,
):
    ingress = [
        {
            "hostname": panel_domain,
            "service": f"http://127.0.0.1:{panel_port}",
            "originRequest": {},
        },
        {
            "hostname": proxy_domain,
            "service": f"http://127.0.0.1:{proxy_port}",
            "originRequest": {},
        },
        {"service": "http_status:404"},
    ]

    return cf_request(
        "PUT",
        f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations",
        token,
        json={"config": {"ingress": ingress}},
    )


def cf_upsert_cname(token, zone_id, fqdn, target):
    records = cf_request(
        "GET",
        f"/zones/{zone_id}/dns_records?name={urllib.parse.quote(fqdn)}&per_page=50",
        token,
    )

    payload = {
        "type": "CNAME",
        "name": fqdn,
        "content": target,
        "ttl": 1,
        "proxied": True,
    }

    if records:
        record_id = records[0]["id"]
        return cf_request(
            "PUT",
            f"/zones/{zone_id}/dns_records/{record_id}",
            token,
            json=payload,
        )

    return cf_request(
        "POST",
        f"/zones/{zone_id}/dns_records",
        token,
        json=payload,
    )


# -----------------------------------------------------------------------------
# SSH
# -----------------------------------------------------------------------------


def ssh_connect(host, port, username, password):
    if paramiko is None:
        raise RuntimeError("缺少 paramiko，请运行：py -m pip install paramiko requests")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        port=int(port),
        username=username,
        password=password,
        timeout=30,
        banner_timeout=30,
        auth_timeout=30,
        look_for_keys=False,
        allow_agent=False,
    )
    return client


def ssh_run(client, command, timeout=900, print_live=True, label=None):
    _, stdout, _ = client.exec_command(command, get_pty=True, timeout=timeout)
    channel = stdout.channel
    parts = []
    start = time.time()

    while not channel.exit_status_ready():
        if channel.recv_ready():
            data = channel.recv(4096).decode("utf-8", "replace")
            parts.append(data)
            if print_live:
                print(data, end="")

        if time.time() - start > timeout:
            raise TimeoutError(f"SSH 命令超时：{label or command[:120]}")

        time.sleep(0.2)

    while channel.recv_ready():
        data = channel.recv(4096).decode("utf-8", "replace")
        parts.append(data)
        if print_live:
            print(data, end="")

    code = channel.recv_exit_status()
    output = "".join(parts)

    if code != 0:
        raise RuntimeError(
            f"SSH 命令失败：{label or 'remote command'}，exit={code}\n"
            f"输出尾部：{output[-5000:]}"
        )

    return output


def remote_bash(client, script, timeout=1800, label="remote bash"):
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    command = (
        "bash -lc 'echo "
        + shlex.quote(encoded)
        + " | base64 -d >/tmp/tierhive_cloudflare_vpn.sh "
        + "&& chmod +x /tmp/tierhive_cloudflare_vpn.sh "
        + "&& /tmp/tierhive_cloudflare_vpn.sh'"
    )
    return ssh_run(client, command, timeout=timeout, print_live=True, label=label)


def remote_python(client, script, timeout=600, label="remote python"):
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    command = (
        "bash -lc 'echo "
        + shlex.quote(encoded)
        + " | base64 -d >/tmp/tierhive_3xui_api.py "
        + "&& python3 /tmp/tierhive_3xui_api.py'"
    )
    return ssh_run(client, command, timeout=timeout, print_live=True, label=label)


def build_install_script(
    xui_user,
    xui_pass,
    panel_port,
    web_base_path,
    tunnel_token,
    reinstall_3xui,
):
    template = r'''#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

echo "========== 1. 安装基础依赖 =========="
apt-get update -y
apt-get install -y curl wget ca-certificates sudo python3 python3-requests sqlite3 lsof procps openssl jq

echo "========== 2. 创建/检查 swap =========="
if ! swapon --show | grep -q '/swapfile'; then
  if [ ! -f /swapfile ]; then
    fallocate -l 1G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=1024
    chmod 600 /swapfile
    mkswap /swapfile
  fi
  swapon /swapfile || true
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

REINSTALL_3XUI="__REINSTALL__"

echo "========== 3. 检查 3x-ui =========="
if [ "$REINSTALL_3XUI" = "1" ] && (command -v x-ui >/dev/null 2>&1 || [ -x /usr/local/x-ui/x-ui ] || [ -d /usr/local/x-ui ] || [ -d /etc/x-ui ]); then
  echo "检测到已有 3x-ui，开始备份并卸载重装。"

  BACKUP_DIR="/root/wandering_3xui_backup_$(date +%Y%m%d_%H%M%S)"
  mkdir -p "$BACKUP_DIR"
  cp -a /etc/x-ui "$BACKUP_DIR/etc-x-ui" 2>/dev/null || true
  cp -a /usr/local/x-ui "$BACKUP_DIR/usr-local-x-ui" 2>/dev/null || true
  echo "旧 3x-ui 备份目录：$BACKUP_DIR"

  systemctl stop x-ui >/dev/null 2>&1 || true
  systemctl disable x-ui >/dev/null 2>&1 || true
  if command -v x-ui >/dev/null 2>&1; then
    timeout 30 x-ui uninstall >/dev/null 2>&1 || true
  fi

  rm -f /etc/systemd/system/x-ui.service
  rm -f /lib/systemd/system/x-ui.service
  rm -f /etc/systemd/system/multi-user.target.wants/x-ui.service
  rm -rf /usr/local/x-ui
  rm -rf /etc/x-ui
  rm -f /usr/bin/x-ui /usr/local/bin/x-ui
  systemctl daemon-reload >/dev/null 2>&1 || true
  systemctl reset-failed x-ui >/dev/null 2>&1 || true
fi

if ! command -v x-ui >/dev/null 2>&1 && [ ! -x /usr/local/x-ui/x-ui ]; then
  echo "========== 3.1 全新安装 3x-ui =========="
  XUI_NONINTERACTIVE=1 \
  XUI_DB_TYPE=sqlite \
  XUI_SSL_MODE=none \
  XUI_USERNAME=__XUI_USER__ \
  XUI_PASSWORD=__XUI_PASS__ \
  XUI_PANEL_PORT=__PANEL_PORT__ \
  XUI_WEB_BASE_PATH=__WEB_PATH__ \
  bash <(curl -Ls https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh)
else
  echo "3x-ui 已存在且未要求重装，跳过安装。"
fi

XUI_BIN="/usr/local/x-ui/x-ui"
if [ ! -x "$XUI_BIN" ]; then
  XUI_BIN="$(command -v x-ui)"
fi

"$XUI_BIN" setting \
  -username __XUI_USER__ \
  -password __XUI_PASS__ \
  -port __PANEL_PORT__ \
  -webBasePath __WEB_PATH__ >/dev/null 2>&1 || true

"$XUI_BIN" setting -listenIP "127.0.0.1" >/dev/null 2>&1 || true

systemctl enable x-ui >/dev/null 2>&1 || true
systemctl restart x-ui
sleep 3

echo "========== 4. 安装 cloudflared =========="
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64) CF_DEB="cloudflared-linux-amd64.deb" ;;
  aarch64|arm64) CF_DEB="cloudflared-linux-arm64.deb" ;;
  *) echo "不支持的架构：$ARCH"; exit 1 ;;
esac

wget -q -O /tmp/cloudflared.deb "https://github.com/cloudflare/cloudflared/releases/latest/download/$CF_DEB"
dpkg -i /tmp/cloudflared.deb || apt-get -f install -y

echo "========== 4.1 清理旧 cloudflared 服务 =========="
systemctl stop cloudflared >/dev/null 2>&1 || true
systemctl disable cloudflared >/dev/null 2>&1 || true
cloudflared service uninstall >/dev/null 2>&1 || true
rm -f /etc/systemd/system/cloudflared.service
rm -f /lib/systemd/system/cloudflared.service
rm -f /etc/systemd/system/multi-user.target.wants/cloudflared.service
systemctl daemon-reload >/dev/null 2>&1 || true
systemctl reset-failed cloudflared >/dev/null 2>&1 || true

echo "========== 4.2 安装新的 cloudflared Tunnel 服务 =========="
if ! cloudflared service install __TUNNEL_TOKEN__; then
  echo "官方 service install 失败，使用 systemd 兜底配置。"
  CLOUDFLARED_BIN="$(command -v cloudflared)"
  cat >/etc/systemd/system/cloudflared.service <<EOF
[Unit]
Description=cloudflared tunnel
After=network-online.target
Wants=network-online.target

[Service]
TimeoutStartSec=0
Type=simple
ExecStart=$CLOUDFLARED_BIN --no-autoupdate tunnel run --token __TUNNEL_TOKEN__
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
fi

systemctl daemon-reload
systemctl enable cloudflared >/dev/null 2>&1 || true
systemctl restart cloudflared
sleep 3

echo "========== 5. 服务状态 =========="
systemctl is-active x-ui
systemctl is-active cloudflared
ss -lntp | grep -E ':__PANEL_PORT__|:10000' || true

echo "========== 远程安装步骤完成 =========="
'''

    replacements = {
        "__REINSTALL__": "1" if reinstall_3xui else "0",
        "__XUI_USER__": shlex.quote(xui_user),
        "__XUI_PASS__": shlex.quote(xui_pass),
        "__PANEL_PORT__": str(int(panel_port)),
        "__WEB_PATH__": shlex.quote(web_base_path),
        "__TUNNEL_TOKEN__": shlex.quote(tunnel_token),
    }

    for key, value in replacements.items():
        template = template.replace(key, value)

    return template


def build_xui_api_script(
    xui_user,
    xui_pass,
    panel_port,
    web_base_path,
    node_name,
    inbound_port,
    ws_path,
    client_uuid,
):
    template = r'''import json
import re
import sqlite3
import time

import requests

XUI_USER = __XUI_USER__
XUI_PASS = __XUI_PASS__
PANEL_PORT = __PANEL_PORT__
WEB_BASE_PATH = __WEB_BASE_PATH__
NODE_NAME = __NODE_NAME__
INBOUND_PORT = __INBOUND_PORT__
WS_PATH = __WS_PATH__
CLIENT_UUID = __CLIENT_UUID__

BASE = f"http://127.0.0.1:{PANEL_PORT}/{WEB_BASE_PATH}".rstrip("/")
S = requests.Session()
S.headers.update({"User-Agent": "tierhive-3xui-api/1.0"})


def api_url(path):
    return BASE + path


def parse_json(resp, what):
    try:
        return resp.json()
    except Exception:
        raise RuntimeError(f"{what} 返回非 JSON：HTTP {resp.status_code} {resp.text[:800]}")


def ok_or_raise(data, what):
    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(
            f"{what} API success=false："
            + json.dumps(data, ensure_ascii=False)[:1500]
        )


def load_api_token():
    print("========== 7.1 读取 3x-ui API Token ==========")

    paths = [
        "/etc/x-ui/install-result.env",
        "/usr/local/x-ui/install-result.env",
    ]

    for path in paths:
        try:
            content = open(path, "r", encoding="utf-8", errors="replace").read()
        except Exception:
            continue

        for raw in content.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().upper()
            value = value.strip().strip('"').strip("'")
            if "API" in key and "TOKEN" in key and len(value) >= 20:
                print(f"从 {path} 读取 API Token：{value[:4]}...{value[-4:]}")
                return value

        match = re.search(
            r"API\s*Token\s*[:=]\s*([A-Za-z0-9._\-]{20,})",
            content,
            flags=re.I,
        )
        if match:
            value = match.group(1).strip()
            print(f"从 {path} 读取 API Token：{value[:4]}...{value[-4:]}")
            return value

    try:
        conn = sqlite3.connect("/etc/x-ui/x-ui.db")
        rows = conn.execute("select key, value from settings").fetchall()
        conn.close()
        for key, value in rows:
            key = str(key).upper()
            value = str(value).strip()
            if "API" in key and "TOKEN" in key and len(value) >= 20:
                print(f"从 settings 表读取 API Token：{value[:4]}...{value[-4:]}")
                return value
    except Exception as exc:
        print("读取 settings API Token 失败：", exc)

    raise RuntimeError("没有找到 3x-ui API Token")


def authenticate():
    token = load_api_token()

    modes = [
        ("Authorization Bearer", {"Authorization": "Bearer " + token}),
        ("X-API-Token", {"X-API-Token": token}),
        ("X-API-Key", {"X-API-Key": token}),
    ]

    last_status = None
    last_text = ""

    for name, headers in modes:
        S.headers.update(headers)
        resp = S.get(api_url("/panel/api/inbounds/list"), timeout=20)
        last_status = resp.status_code
        last_text = resp.text[:800]
        print(f"API Token 鉴权尝试 {name}: HTTP {resp.status_code}")

        if resp.status_code == 200:
            data = parse_json(resp, "鉴权测试")
            ok_or_raise(data, "鉴权测试")
            print("3x-ui API Token 鉴权成功")
            return

        for header in headers:
            S.headers.pop(header, None)

    raise RuntimeError(
        f"无法通过 3x-ui API Token 鉴权，最后响应：HTTP {last_status} {last_text}"
    )


def api_get(path, what):
    resp = S.get(api_url(path), timeout=30)
    data = parse_json(resp, what)
    ok_or_raise(data, what)
    return data


def api_post(path, payload, what):
    resp = S.post(api_url(path), json=payload, timeout=30)
    data = parse_json(resp, what)
    ok_or_raise(data, what)
    return data


def get_obj(data):
    if not isinstance(data, dict):
        return None
    for key in ("obj", "result", "data"):
        if key in data:
            return data[key]
    return None


def list_inbounds():
    data = api_get("/panel/api/inbounds/list", "读取入站列表")
    obj = get_obj(data)
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict) and isinstance(obj.get("inbounds"), list):
        return obj["inbounds"]
    return []


def delete_same_port():
    print("========== 7.2 删除同端口旧入站 ==========")
    for inbound in list_inbounds():
        if int(inbound.get("port", -1)) == INBOUND_PORT:
            inbound_id = inbound.get("id")
            print(
                f"删除旧入站 id={inbound_id}, "
                f"remark={inbound.get('remark')}, port={INBOUND_PORT}"
            )
            try:
                api_post(
                    f"/panel/api/inbounds/del/{inbound_id}",
                    {},
                    "删除旧入站",
                )
            except Exception as exc:
                print("删除旧入站失败，继续尝试创建：", exc)


def add_inbound():
    print("========== 7.3 用 3x-ui API 创建 VLESS WS 入站 ==========")

    payload = {
        "up": 0,
        "down": 0,
        "total": 0,
        "remark": NODE_NAME,
        "enable": True,
        "expiryTime": 0,
        "listen": "127.0.0.1",
        "port": INBOUND_PORT,
        "protocol": "vless",
        "settings": json.dumps(
            {
                "clients": [],
                "decryption": "none",
                "encryption": "none",
            },
            separators=(",", ":"),
        ),
        "streamSettings": json.dumps(
            {
                "network": "ws",
                "security": "none",
                "wsSettings": {
                    "acceptProxyProtocol": False,
                    "path": WS_PATH,
                    "host": "",
                    "headers": {},
                    "heartbeatPeriod": 0,
                },
            },
            separators=(",", ":"),
        ),
        "sniffing": json.dumps({"enabled": False}, separators=(",", ":")),
    }

    data = api_post("/panel/api/inbounds/add", payload, "创建入站")
    obj = get_obj(data)
    inbound_id = obj.get("id") if isinstance(obj, dict) else None

    if not inbound_id:
        time.sleep(1)
        for inbound in list_inbounds():
            if int(inbound.get("port", -1)) == INBOUND_PORT:
                inbound_id = inbound.get("id")
                break

    if not inbound_id:
        raise RuntimeError("创建入站后没有找到 inbound id")

    print(f"已创建入站 id={inbound_id}, port={INBOUND_PORT}")
    return int(inbound_id)


def add_client(inbound_id):
    print("========== 7.4 用 3x-ui API 添加客户端 ==========")

    client = {
        "id": CLIENT_UUID,
        "flow": "",
        "email": NODE_NAME,
        "limitIp": 0,
        "totalGB": 0,
        "expiryTime": 0,
        "enable": True,
        "tgId": 0,
        "subId": NODE_NAME + "-" + CLIENT_UUID[:8],
        "comment": "",
        "reset": 0,
    }

    attempts = [
        (
            "/panel/api/clients/add",
            {"client": client, "inboundIds": [inbound_id]},
            "v3 clients/add",
        ),
        (
            "/panel/api/inbounds/addClient",
            {
                "id": inbound_id,
                "settings": json.dumps(
                    {"clients": [client]},
                    separators=(",", ":"),
                ),
            },
            "legacy inbounds/addClient",
        ),
    ]

    errors = []

    for path, payload, label in attempts:
        print(f"尝试添加客户端接口：{label} {path}")
        resp = S.post(api_url(path), json=payload, timeout=30)
        preview = resp.text[:800]
        print(
            f"添加客户端响应：HTTP {resp.status_code} "
            f"{preview.replace(chr(10), ' ')[:300]}"
        )

        if resp.status_code == 404:
            errors.append(f"{path} => HTTP 404")
            continue

        try:
            data = resp.json()
        except Exception:
            errors.append(
                f"{path} => 非JSON HTTP {resp.status_code} {preview[:300]}"
            )
            continue

        if isinstance(data, dict) and data.get("success") is False:
            errors.append(
                f"{path} => success=false "
                + json.dumps(data, ensure_ascii=False)[:500]
            )
            continue

        print("客户端添加 API 返回：", json.dumps(data, ensure_ascii=False)[:800])
        return

    raise RuntimeError("所有添加客户端接口均失败：" + " | ".join(errors))


def verify_configuration():
    print("========== 7.5 验证客户端配置 ==========")

    found = None
    for inbound in list_inbounds():
        if int(inbound.get("port", -1)) == INBOUND_PORT:
            found = inbound
            break

    if not found:
        raise RuntimeError("API 校验失败：没有找到新建入站")

    print("API 入站：", json.dumps(found, ensure_ascii=False)[:1500])

    try:
        conn = sqlite3.connect("/etc/x-ui/x-ui.db")
        cur = conn.cursor()

        try:
            rows = cur.execute(
                "select id, email, enable, uuid, sub_id from clients"
            ).fetchall()
            print("clients:", rows)
        except Exception as exc:
            print("读取 clients 表失败：", exc)

        try:
            rows = cur.execute(
                "select client_id, inbound_id, flow_override from client_inbounds"
            ).fetchall()
            print("client_inbounds:", rows)
        except Exception as exc:
            print("读取 client_inbounds 表失败：", exc)

        conn.close()
    except Exception as exc:
        print("SQLite 校验失败：", exc)


authenticate()
delete_same_port()
inbound_id = add_inbound()
add_client(inbound_id)
verify_configuration()
print("XUI_API_CONFIG_DONE")
'''

    replacements = {
        "__XUI_USER__": repr(xui_user),
        "__XUI_PASS__": repr(xui_pass),
        "__PANEL_PORT__": str(int(panel_port)),
        "__WEB_BASE_PATH__": repr(web_base_path.strip("/")),
        "__NODE_NAME__": repr(node_name),
        "__INBOUND_PORT__": str(int(inbound_port)),
        "__WS_PATH__": repr(ws_path),
        "__CLIENT_UUID__": repr(client_uuid),
    }

    for key, value in replacements.items():
        template = template.replace(key, value)

    return template


# -----------------------------------------------------------------------------
# VLESS + WS + TLS test
# -----------------------------------------------------------------------------


def ws_build_frame(payload, opcode=2):
    fin_opcode = 0x80 | (opcode & 0x0F)
    length = len(payload)

    if length < 126:
        header = bytes([fin_opcode, 0x80 | length])
    elif length < 65536:
        header = bytes([fin_opcode, 0x80 | 126]) + length.to_bytes(2, "big")
    else:
        header = bytes([fin_opcode, 0x80 | 127]) + length.to_bytes(8, "big")

    mask_key = secrets.token_bytes(4)
    masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return header + mask_key + masked


def recv_exact(sock, length):
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise RuntimeError("连接提前关闭")
        data += chunk
    return data


def ws_recv_frame(sock, timeout=20):
    sock.settimeout(timeout)
    first = recv_exact(sock, 2)
    b1, b2 = first[0], first[1]
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F

    if length == 126:
        length = int.from_bytes(recv_exact(sock, 2), "big")
    elif length == 127:
        length = int.from_bytes(recv_exact(sock, 8), "big")

    mask_key = recv_exact(sock, 4) if masked else b""
    payload = recv_exact(sock, length) if length else b""

    if masked:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

    return opcode, payload


def build_vless_tcp_request(client_uuid, target_host, target_port, payload):
    client = uuid.UUID(client_uuid)
    version = b"\x00"
    addon_len = b"\x00"
    command_tcp = b"\x01"
    port = int(target_port).to_bytes(2, "big")

    try:
        socket.inet_pton(socket.AF_INET, target_host)
        address = b"\x01" + socket.inet_aton(target_host)
    except OSError:
        host_bytes = target_host.encode("idna")
        address = b"\x02" + bytes([len(host_bytes)]) + host_bytes

    return version + client.bytes + addon_len + command_tcp + port + address + payload


def test_vless_ws_tls(proxy_domain, ws_path, client_uuid, timeout=25):
    print("\n========== 8. 本地测试 VLESS 链接可用性 ==========")
    print(f"测试入口：{proxy_domain}:443{ws_path}")
    print("测试目标：example.com:80")

    ws_key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    path = ws_path if ws_path.startswith("/") else "/" + ws_path

    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {proxy_domain}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {ws_key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "User-Agent: tierhive-cloudflare-vpn/1.0\r\n"
        "\r\n"
    ).encode("ascii")

    context = ssl.create_default_context()
    raw = None
    sock = None

    try:
        raw = socket.create_connection((proxy_domain, 443), timeout=timeout)
        sock = context.wrap_socket(raw, server_hostname=proxy_domain)
        sock.settimeout(timeout)
        sock.sendall(request)

        response = b""
        while b"\r\n\r\n" not in response and len(response) < 8192:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk

        header_text = response.decode("iso-8859-1", "replace")
        first_line = header_text.splitlines()[0] if header_text.splitlines() else ""
        print("WebSocket 握手响应：", first_line)

        if " 101 " not in first_line:
            return {
                "ok": False,
                "stage": "websocket_handshake",
                "message": "WebSocket 握手未返回 101",
                "response_head": header_text[:1200],
            }

        expected_accept = base64.b64encode(
            hashlib.sha1(
                (
                    ws_key
                    + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                ).encode("ascii")
            ).digest()
        ).decode("ascii")

        if expected_accept not in header_text:
            print("⚠️ WebSocket Accept 校验未通过，但继续测试 VLESS。")

        http_payload = (
            "GET / HTTP/1.1\r\n"
            "Host: example.com\r\n"
            "Connection: close\r\n"
            "User-Agent: tierhive-cloudflare-vpn/1.0\r\n"
            "\r\n"
        ).encode("ascii")

        vless_payload = build_vless_tcp_request(
            client_uuid,
            "example.com",
            80,
            http_payload,
        )
        sock.sendall(ws_build_frame(vless_payload, opcode=2))

        received = b""
        started = time.time()

        while time.time() - started < timeout and len(received) < 65536:
            opcode, payload = ws_recv_frame(sock, timeout=timeout)
            if opcode == 8:
                break
            if opcode in (1, 2):
                received += payload
                if b"HTTP/" in received and b"\r\n\r\n" in received:
                    break

        if not received:
            return {
                "ok": False,
                "stage": "vless_response",
                "message": "WebSocket 已握手，但未收到 VLESS 代理返回数据。",
            }

        index = received.find(b"HTTP/")
        if index >= 0:
            http_head = received[index : index + 500].decode("iso-8859-1", "replace")
            target_line = http_head.splitlines()[0] if http_head.splitlines() else ""
            print("代理目标响应：", target_line)
            return {
                "ok": True,
                "stage": "done",
                "message": "VLESS + WS + TLS 测试成功",
                "target_response": target_line,
            }

        return {
            "ok": False,
            "stage": "target_http_parse",
            "message": "收到代理返回数据，但没有解析到 HTTP 响应头",
            "received_hex_preview": received[:300].hex(),
        }

    except Exception as exc:
        return {
            "ok": False,
            "stage": "exception",
            "message": str(exc),
            "error_type": type(exc).__name__,
        }

    finally:
        try:
            if sock:
                sock.close()
        except Exception:
            pass
        try:
            if raw:
                raw.close()
        except Exception:
            pass


def wait_and_test_vless(proxy_domain, ws_path, client_uuid, attempts=2):
    attempts = max(1, int(attempts))
    last = None

    for index in range(1, attempts + 1):
        if index > 1:
            print("等待 20 秒后重试 VLESS 测试...")
            time.sleep(20)

        print(f"VLESS 测试第 {index}/{attempts} 次")
        last = test_vless_ws_tls(proxy_domain, ws_path, client_uuid)

        if last.get("ok"):
            print("✅ VLESS 链接可用性测试成功")
            return last

        print("⚠️ 本次测试失败：", json.dumps(last, ensure_ascii=False)[:1000])

    print("❌ VLESS 链接可用性测试未通过")
    return last or {"ok": False, "message": "未执行测试"}


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------


def main():
    print("=" * 80)
    print("已有 VPS → 3x-ui API + Cloudflare Tunnel 自动部署脚本")
    print("=" * 80)
    print("配置读取顺序：当前目录 .env → 系统环境变量 → 缺失项提示输入")
    print("注意：脚本会在 VPS 上安装/重启 3x-ui、cloudflared。")
    print()

    vps_ip = cfg("VPS_IP", "VPS IP")
    ssh_port = int(cfg("SSH_PORT", "SSH 端口", "22"))
    ssh_user = cfg("SSH_USER", "SSH 用户", "root")
    ssh_password = cfg("SSH_PASSWORD", "root/SSH 密码", secret=True)

    cf_token = cfg("CF_API_TOKEN", "Cloudflare API Token", secret=True)
    domain = normalize_domain(cfg("CF_ZONE_NAME", "主域名，例如 cn2.io"))

    node_name = normalize_subdomain(cfg("NODE_NAME", "节点名称，例如 sgp01"))
    proxy_subdomain = normalize_subdomain(
        cfg("PROXY_SUBDOMAIN", "代理子域名前缀", node_name)
    )
    panel_subdomain = normalize_subdomain(
        cfg("PANEL_SUBDOMAIN", "面板子域名前缀", f"{node_name}p")
    )

    proxy_domain = f"{proxy_subdomain}.{domain}"
    panel_domain = f"{panel_subdomain}.{domain}"

    panel_port = int(cfg("PANEL_PORT", "3x-ui 面板本地端口", "8888"))
    inbound_port = int(cfg("INBOUND_PORT", "VLESS 入站本地端口", "10000"))
    ws_path = "/" + normalize_path(cfg("WS_PATH", "WebSocket 路径", "abc123"))

    xui_user = cfg("XUI_USERNAME", "3x-ui 用户名", "admin")
    xui_password = cfg(
        "XUI_PASSWORD",
        "3x-ui 密码，留空自动生成",
        "",
        required=False,
    )
    if not xui_password:
        xui_password = rand_text(18)

    web_base_path = cfg(
        "XUI_WEB_BASE_PATH",
        "3x-ui 面板隐藏路径，留空自动生成",
        "",
        required=False,
    )
    if not web_base_path:
        web_base_path = rand_path(node_name)
    web_base_path = web_base_path.strip().strip("/")

    reinstall_3xui = cfg_bool("REINSTALL_3XUI", True)
    test_attempts = int(env_get("VLESS_TEST_ATTEMPTS", "2"))
    auto_yes = cfg_bool("AUTO_YES", False)

    client_uuid = env_get("VLESS_UUID") or str(uuid.uuid4())
    uuid.UUID(client_uuid)

    print("========== 本次部署参数 ==========")
    print(f"VPS: {vps_ip}:{ssh_port}")
    print(f"域名: {domain}")
    print(f"面板域名: https://{panel_domain}/{web_base_path}/")
    print(f"代理域名: {proxy_domain}")
    print(f"VLESS 本地端口: {inbound_port}")
    print(f"WS Path: {ws_path}")
    print("创建方式: 3x-ui API Token + add inbound + addClient")
    print(f"VLESS 测试重试次数: {test_attempts}")
    print()

    if not auto_yes:
        confirm = ask("确认开始部署？输入 yes 继续", "no").lower()
        if confirm != "yes":
            raise SystemExit("已取消。")

    print("\n========== 1. 查询 Cloudflare Account / Zone ==========")
    account_id, account_name = cf_get_account_id(cf_token)
    zone_id, zone_name = cf_get_zone_id(cf_token, domain)
    print(f"Account: {account_name} | {account_id}")
    print(f"Zone: {zone_name} | {zone_id}")

    print("\n========== 2. 创建 Cloudflare Tunnel ==========")
    tunnel_name, tunnel_id, tunnel_token = cf_create_tunnel(
        cf_token,
        account_id,
        node_name,
    )
    print(f"Tunnel: {tunnel_name} | {tunnel_id}")

    print("\n========== 3. 写入 Tunnel 路由配置 ==========")
    cf_put_tunnel_config(
        cf_token,
        account_id,
        tunnel_id,
        panel_domain,
        panel_port,
        proxy_domain,
        inbound_port,
    )
    print(f"{panel_domain} -> http://127.0.0.1:{panel_port}")
    print(f"{proxy_domain} -> http://127.0.0.1:{inbound_port}")

    print("\n========== 4. 创建/更新 DNS CNAME ==========")
    cname_target = f"{tunnel_id}.cfargotunnel.com"
    cf_upsert_cname(cf_token, zone_id, panel_domain, cname_target)
    cf_upsert_cname(cf_token, zone_id, proxy_domain, cname_target)
    print(f"{panel_domain} CNAME {cname_target}")
    print(f"{proxy_domain} CNAME {cname_target}")

    print("\n========== 5. SSH 连接 VPS ==========")
    ssh = ssh_connect(vps_ip, ssh_port, ssh_user, ssh_password)

    try:
        print("\n========== 6. 安装/重装 3x-ui + cloudflared ==========")
        install_script = build_install_script(
            xui_user,
            xui_password,
            panel_port,
            web_base_path,
            tunnel_token,
            reinstall_3xui,
        )
        remote_bash(
            ssh,
            install_script,
            timeout=1800,
            label="install 3x-ui and cloudflared",
        )

        print("\n========== 7. 通过 3x-ui API 创建入站和客户端 ==========")
        api_script = build_xui_api_script(
            xui_user,
            xui_password,
            panel_port,
            web_base_path,
            node_name,
            inbound_port,
            ws_path,
            client_uuid,
        )
        remote_python(
            ssh,
            api_script,
            timeout=600,
            label="configure 3x-ui API",
        )

        ssh_run(
            ssh,
            (
                "systemctl restart x-ui && sleep 3 && "
                f"ss -lntp | grep -E ':{inbound_port}|:{panel_port}' || true"
            ),
            timeout=120,
            print_live=True,
            label="restart x-ui and check ports",
        )

    finally:
        ssh.close()

    vless_test = wait_and_test_vless(
        proxy_domain,
        ws_path,
        client_uuid,
        attempts=test_attempts,
    )

    query = {
        "encryption": "none",
        "security": "tls",
        "type": "ws",
        "host": proxy_domain,
        "path": ws_path,
        "sni": proxy_domain,
        "fp": "chrome",
    }

    vless_link = (
        f"vless://{client_uuid}@{proxy_domain}:443?"
        + urllib.parse.urlencode(query, quote_via=urllib.parse.quote)
        + "#"
        + urllib.parse.quote(node_name)
    )

    result = {
        "ok": True,
        "node_name": node_name,
        "vps_ip": vps_ip,
        "cloudflare": {
            "account_id": account_id,
            "zone_id": zone_id,
            "tunnel_name": tunnel_name,
            "tunnel_id": tunnel_id,
            "cname_target": cname_target,
        },
        "panel": {
            "url": f"https://{panel_domain}/{web_base_path}/",
            "username": xui_user,
            "password": xui_password,
            "local_port": panel_port,
        },
        "proxy": {
            "domain": proxy_domain,
            "local_port": inbound_port,
            "uuid": client_uuid,
            "ws_path": ws_path,
            "vless_link": vless_link,
            "test": vless_test,
        },
        "log_file": str(LOG_FILE_PATH) if LOG_FILE_PATH else None,
    }

    output_file = Path(
        f"deploy_result_{node_name}_{int(time.time())}.json"
    )
    output_file.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("\n" + "=" * 80)
    print("部署完成")
    print("=" * 80)
    print("3x-ui 面板：")
    print(result["panel"]["url"])
    print("用户名：", xui_user)
    print("密码：", xui_password)
    print()
    print("VLESS 链接：")
    print(vless_link)
    print()
    print(
        "VLESS 可用性测试：",
        "成功 ✅" if vless_test.get("ok") else "失败 ❌",
    )
    print(json.dumps(vless_test, ensure_ascii=False, indent=2))
    print()
    print(f"结果已保存：{output_file}")
    if LOG_FILE_PATH:
        print(f"日志文件：{LOG_FILE_PATH}")


if __name__ == "__main__":
    exit_code = 0

    try:
        start_deploy_log()
        main()

    except KeyboardInterrupt as exc:
        exit_code = 130
        print("\n已中断。")
        try:
            error_file = write_error_report(exc)
            print(f"错误报告已保存：{error_file}")
        except Exception:
            pass

    except BaseException as exc:
        exit_code = 1
        print("\n❌ 部署失败：")
        print(str(exc))
        print("\n========== Python traceback ==========")
        traceback.print_exc()

        try:
            error_file = write_error_report(exc)
            print(f"错误报告已保存：{error_file}")
        except Exception as report_error:
            print(f"错误报告生成失败：{report_error}")

    finally:
        close_deploy_log()
        sys.exit(exit_code)
