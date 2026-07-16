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

# 3. Cloudflare 完整配置流程

> Cloudflare 控制台的菜单名称以后可能会略有变化，但配置原则不变。

## 3.1 准备域名

先确保准备给节点使用的域名已经托管在 Cloudflare，例如：

```text
cn2.io
```

脚本不要求你提前手动创建节点子域名。

例如 `.env` 配置：

```env
NODE_NAME=sgp01
PROXY_SUBDOMAIN=sgp01
PANEL_SUBDOMAIN=sgp01p
CF_ZONE_NAME=cn2.io
```

脚本会自动创建或更新：

```text
代理节点：sgp01.cn2.io
3x-ui 面板：sgp01p.cn2.io
```

DNS 会自动指向脚本创建的 Cloudflare Tunnel。

---

## 3.2 获取 Cloudflare Account ID 和 Zone ID

进入 Cloudflare 控制台。

需要找到并复制：

```text
Account ID
Zone ID
```

其中：

```text
Account ID = Cloudflare 账户 ID
Zone ID    = 当前域名的 Zone ID
```

填写到 `.env`：

```env
CF_ACCOUNT_ID=你的Cloudflare_Account_ID
CF_ZONE_ID=你的Cloudflare_Zone_ID
```

建议直接填写这两个 ID，可以减少脚本自动查询 Cloudflare API 的请求次数。

---

## 3.3 创建 Cloudflare API Token

进入 Cloudflare API Token 管理页面，新建一个自定义 Token。

建议单独创建一个专门给本项目使用的 Token，例如命名：

```text
wandering-3x-ui
```

不要使用主账户密码，也不要把 Global API Key 提交到项目中。

### Account 权限

添加：

```text
Cloudflare Tunnel Read
Cloudflare Tunnel Write
Account Settings Read
```

Account 资源范围选择：

```text
自己的 Cloudflare Account
```

例如在只有一个 Cloudflare 账户的情况下，可以选择该整个账户。

### Zone 权限

添加：

```text
Zone Read
DNS Read
DNS Write
```

Zone 资源范围建议只授权实际使用的域名，例如：

```text
cn2.io
```

不建议为了方便给 Token 不必要的全账户 DNS 权限。

最终权限结构可以理解为：

```text
Account：
  Cloudflare Tunnel Read
  Cloudflare Tunnel Write
  Account Settings Read

指定域名 cn2.io：
  Zone Read
  DNS Read
  DNS Write
```

---

## 3.4 配置 Token IP 白名单

如果 API Token 开启了 IP 地址限制，必须授权：

```text
真正访问 Cloudflare API 时的公网出口 IP
```

### 情况 A：脚本直接使用本地网络访问 Cloudflare API

PowerShell 执行：

```powershell
curl.exe https://api.ipify.org
```

假设返回：

```text
117.182.66.169
```

Cloudflare Token IP 白名单填写：

```text
117.182.66.169/32
```

### 情况 B：脚本通过 v2rayN 代理访问 Cloudflare API

本项目常用的 v2rayN 本地 HTTP/Mixed 代理端口为：

```text
127.0.0.1:10808
```

执行：

```powershell
curl.exe -x http://127.0.0.1:10808 https://api.ipify.org
```

假设返回：

```text
51.81.245.144
```

那么 Cloudflare Token IP 白名单应该加入：

```text
51.81.245.144/32
```

注意：

```text
CF_API_PROXY=http://127.0.0.1:10808
```

表示脚本调用 Cloudflare API 时会通过 v2rayN 出口访问。

因此 Cloudflare 看到的是代理节点出口 IP，而不是 Windows 本机宽带 IP。

不要把以下地址加入公网 IP 白名单：

```text
127.0.0.1
192.168.x.x
10.x.x.x
```

这些属于本地或内网地址。

---

## 3.5 复制 API Token

创建 Token 成功后，立即复制完整 Token，并保存到 `.env`：

```env
CF_API_TOKEN=你的完整Cloudflare_API_Token
```

正确示例格式：

```env
CF_API_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

不要写成：

```env
CF_API_TOKEN=Bearer xxxxxxxxxxxxxxxxx
```

`.env` 里只保存 Token 本身，脚本会自动添加 `Authorization: Bearer` 请求头。

---

## 3.6 配置 Cloudflare API 代理

如果 Windows 本地可以稳定直连 Cloudflare API，可以将代理配置留空或注释：

```env
# CF_API_PROXY=
```

如果本地访问 Cloudflare API 超时，而 v2rayN 本地代理端口是 `10808`，设置：

```env
CF_API_PROXY=http://127.0.0.1:10808
```

同时建议：

```env
CF_API_TIMEOUT=60
CF_API_RETRIES=3
CF_API_RETRY_SLEEP=5
```

测试代理是否正常：

```powershell
curl.exe -x http://127.0.0.1:10808 https://api.ipify.org
```

只要能正常返回公网 IP，说明本地 HTTP 代理可以使用。

---

## 3.7 测试 Cloudflare Tunnel API 权限

进入项目目录：

```powershell
cd D:\AI\tierhive-cloudflare-vpn
```

从 `.env` 读取 Token：

```powershell
$token = (Get-Content .env | Where-Object { $_ -match '^CF_API_TOKEN=' } | Select-Object -First 1) -replace '^CF_API_TOKEN=',''
$token = $token.Trim().Trim('"').Trim("'")
```

如果使用 v2rayN 代理，测试：

```powershell
curl.exe -x http://127.0.0.1:10808 `
  -H "Authorization: Bearer $token" `
  "https://api.cloudflare.com/client/v4/accounts/你的AccountID/cfd_tunnel?is_deleted=false&per_page=5"
```

看到：

```text
"success":true
```

说明下面几项基本正常：

```text
API Token
Cloudflare Tunnel 权限
Account ID
Token IP 白名单
本地代理
```

如果返回：

```text
Authentication error
```

优先检查：

```text
1. Token 是否复制完整
2. .env 是否保存了旧 Token
3. Windows 系统环境变量是否存在旧 CF_API_TOKEN
4. 当前 Cloudflare API 出口 IP 是否在 Token 白名单
5. CF_API_PROXY 端口是否正确
```

---

## 3.8 Cloudflare 配置完成检查清单

运行脚本之前确认：

```text
[ ] 域名已经托管到 Cloudflare
[ ] 已取得 Account ID
[ ] 已取得 Zone ID
[ ] 已创建 API Token
[ ] Cloudflare Tunnel Read / Write 已授权
[ ] Zone Read 已授权
[ ] DNS Read / Write 已授权
[ ] Token IP 白名单包含实际 API 出口 IP
[ ] .env 已填写 CF_API_TOKEN
[ ] .env 已填写 CF_ACCOUNT_ID
[ ] .env 已填写 CF_ZONE_ID
[ ] 需要代理时已配置 CF_API_PROXY=http://127.0.0.1:10808
[ ] Tunnel API 测试返回 success=true
```

完成后即可运行部署脚本。

## 4. 配置 `.env`

核心示例：

```env
VPS_IP=139.99.123.120
SSH_PORT=2658
SSH_USER=root
SSH_PASSWORD=你的root密码

CF_API_TOKEN=你的Cloudflare完整API_Token
CF_ZONE_NAME=cn2.io
CF_ACCOUNT_ID=你的Cloudflare_Account_ID
CF_ZONE_ID=你的Cloudflare_Zone_ID

# 本地直连 Cloudflare API 正常时留空
# CF_API_PROXY=

# 需要通过 v2rayN 时，例如：
CF_API_PROXY=http://127.0.0.1:10808

CF_API_TIMEOUT=60
CF_API_RETRIES=3
CF_API_RETRY_SLEEP=5

NODE_NAME=us01
PROXY_SUBDOMAIN=us01
PANEL_SUBDOMAIN=us01p

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

- `CF_ZONE_NAME` 填自己的域名，例如 `cn2.io`，不要填写中文。
- `NODE_NAME` 自定义节点名称，例如 `us01`、`sgp01`，不要使用中文。
- `PROXY_SUBDOMAIN` 是代理节点子域名前缀。
- `PANEL_SUBDOMAIN` 是 3x-ui 面板子域名前缀。
- `XUI_PASSWORD=` 留空：自动生成。
- `XUI_WEB_BASE_PATH=` 留空：自动生成隐藏路径。
- `VLESS_UUID=` 留空：自动生成 UUID。
- `REINSTALL_3XUI=true`：检测到已有 3x-ui 时，先备份再卸载重装。
- `AUTO_YES=true`：不再询问确认，直接部署。
- 项目会优先读取当前目录的 `.env` 配置。

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
