#!/usr/bin/env bash
# GLM Studio 本地启动脚本
# 特性：自动绕开本地代理访问 GLM 域名——即使系统/终端配了坏掉的代理，服务也不受影响。
set -euo pipefail
cd "$(dirname "$0")"

# 读取本地配置（ADMIN_KEY / PORT 等，此文件不入库）
if [ -f .env ]; then
  set -a; source .env; set +a
fi

# GLM 相关域名一律直连（国内站点不需要走代理）
export NO_PROXY="localhost,127.0.0.1,::1,chatglm.cn,.chatglm.cn,open.bigmodel.cn,.bigmodel.cn${NO_PROXY:+,$NO_PROXY}"
export no_proxy="$NO_PROXY"

# 如果本地代理端口（7890/7892/7897）上没有服务，剥掉全部代理变量，防止残留配置劫持请求
proxy_port_alive=0
for port in 7890 7892 7897 6152; do
  nc -z 127.0.0.1 "$port" 2>/dev/null && proxy_port_alive=1 && break
done
if [ "$proxy_port_alive" -eq 0 ]; then
  unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy 2>/dev/null || true
fi

export PORT="${PORT:-8300}"

echo "GLM Studio 启动中 → http://127.0.0.1:${PORT}/admin"
exec .venv/bin/python server.py
