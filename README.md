# TierHive / VPS + 3x-ui + Cloudflare Tunnel 一键部署

用于把一台已有 VPS 自动配置成 `VLESS + WebSocket + Cloudflare Tunnel` 节点。

脚本会自动完成：

```text
读取 .env
→ SSH 登录 VPS
→ 检测 3x-ui
→ 已安装则备份并卸载重装
→ 安装 3x-ui
→ 安装 cloudflared
→ Cloudflare API 创建 Tunnel
→ 自动创建/更新 DNS
→ 通过 3x-ui API 创建 VLESS + WebSocket 入站
→ 自动添加客户端 UUID
→ 输出 VLESS 分享链接
→ 自动测试节点是否可用
→ 成功或失败都生成日志
```

## 1. 环境要求

本地推荐：

```text
Windows 10 / Windows 11
Python 3.10+
```

安装依赖：

```powershell
py -m pip install requests paramiko
```

VPS 推荐：

```text
Debian 12
Ubuntu 22.04 / 24.04
```

建议使用 `root` 用户。

## 2. 下载项目

```powershell
git clone https://github.com/b8vipvip/tierhive-cloudflare-vpn.git
cd tierhive-cloudflare-vpn
```

复制配置模板：

```powershell
Copy-Item .env.example .env
notepad .env
```

> `.env` 中会包含 VPS 密码和 Cloudflare API Token，请不要提交到 GitHub。仓库已经通过 `.gitignore` 忽略真实 `.env`。

## 3. Cloudflare API Token 权限

至少需要：

### Account

```text
Cloudflare Tunnel Read
Cloudflare Tunnel Write
Account Settings Read
```

### Zone

```text
Zone Read
DNS Read
DNS Write
```

如果 Token 设置了 IP 白名单，需要允许实际调用 Cloudflare API 的出口公网 IP。

直连查看公网 IP：

```powershell
curl.exe https://api.ipify.org
```

通过 v2rayN 本地 HTTP/Mixed 代理查看：

```powershell
curl.exe -x http://127.0.0.1:10808 https://api.ipify.org
```

返回的 IP 加入 Cloudflare Token 白名单，例如：

```text
51.81.245.144/32
```

## 4. 配置 `.env`

核心示例：

```env
VPS_IP=139.99.123.120
SSH_PORT=2658
SSH_USER=root
SSH_PASSWORD=你的root密码

CF_API_TOKEN=你的Cloudflare完整API_Token
CF_ZONE_NAME=cn2.io你的ZONE_NAME可以填你的域名(不要中文)  
CF_ACCOUNT_ID=你的Cloudflare_Account_ID
CF_ZONE_ID=你的Cloudflare_Zone_ID

# 本地直连 Cloudflare API 正常时留空
# CF_API_PROXY=
# 需要通过 v2rayN 时，例如：
CF_API_PROXY=http://127.0.0.1:10808

CF_API_TIMEOUT=60
CF_API_RETRIES=3
CF_API_RETRY_SLEEP=5

NODE_NAME=us01自定义(不要中文)
PROXY_SUBDOMAIN=us01自定义(不要中文)
PANEL_SUBDOMAIN=us01自定义(不要中文)

PANEL_PORT=8888
INBOUND_PORT=10000
WS_PATH=abc123

XUI_USERNAME=admin
XUI_PASSWORD=
XUI_WEB_BASE_PATH=

REINSTALL_3XUI=true
VLESS_UUID=
VLESS_TEST_ATTEMPTS=2
AUTO_YES=false
```

说明：

- `XUI_PASSWORD=` 留空：自动生成。
- `XUI_WEB_BASE_PATH=` 留空：自动生成隐藏路径。
- `VLESS_UUID=` 留空：自动生成 UUID。
- `REINSTALL_3XUI=true`：检测到已有 3x-ui 时，先备份再卸载重装。
- `AUTO_YES=true`：不再询问确认，直接部署。
- `.env` 优先于 Windows 系统环境变量，避免旧环境变量覆盖当前配置。

## 5. 运行

```powershell
py .\dp_vpn.py
```

部署成功后会输出：

```text
3x-ui 面板地址
VLESS UUID
WebSocket Path
vless:// 分享链接
VLESS 自动测试结果
```

将完整 `vless://` 链接复制到 v2rayN 导入即可。

## 6. 默认节点结构

假设：

```text
NODE_NAME=sgp01
CF_ZONE_NAME=cn2.io
```

默认域名类似：

```text
代理节点：sgp01.cn2.io
管理面板：sgp01p.cn2.io
```

客户端连接参数：

```text
协议：VLESS
端口：443
传输：WebSocket
TLS：开启
SNI：代理域名
Host：代理域名
Path：.env 中的 WS_PATH
```

实际链路：

```text
v2rayN
→ Cloudflare :443
→ Cloudflare Tunnel
→ VPS 127.0.0.1:10000
→ Xray
```

## 7. 日志

所有部署都会生成日志：

```text
logs/wandering_3xui_deploy_日期时间.log
```

失败时还会生成：

```text
deploy_error_日期时间.json
```

成功结果：

```text
deploy_result_节点名_时间戳.json
```

## 8. 常见问题

### Cloudflare API 返回 401

先检查实际出口 IP：

```powershell
curl.exe -x http://127.0.0.1:10808 https://api.ipify.org
```

再确认该 IP 已加入 Token 白名单。

测试 Tunnel API：

```powershell
$token = (Get-Content .env | Where-Object { $_ -match '^CF_API_TOKEN=' } | Select-Object -First 1) -replace '^CF_API_TOKEN=',''
$token = $token.Trim().Trim('"').Trim("'")

curl.exe -x http://127.0.0.1:10808 `
  -H "Authorization: Bearer $token" `
  "https://api.cloudflare.com/client/v4/accounts/你的AccountID/cfd_tunnel?is_deleted=false&per_page=5"
```

看到：

```text
"success":true
```

说明 Token、权限、代理和 IP 白名单基本正常。

### VLESS 延迟显示 -1

进入 3x-ui 检查：

```text
客户端数量必须至少为 1
客户端 UUID 必须与分享链接一致
WebSocket Path 必须一致
```

## 9. 安全说明

不要把真实 `.env`、日志或部署结果提交到公开仓库，因为其中可能包含：

```text
VPS root 密码
Cloudflare API Token
3x-ui 面板密码
VLESS UUID
```

仓库中的 `.env.example` 只放占位配置，不包含真实密钥。
