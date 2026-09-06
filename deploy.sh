#!/usr/bin/env bash
# 一键部署：本地代码 → VPS → 重建容器 → 健康验证
# 用法: ./deploy.sh   （可用环境变量覆盖目标: VPS_HOST / VPS_KEY / VPS_DIR）
set -euo pipefail
cd "$(dirname "$0")"

VPS_HOST="${VPS_HOST:-ubuntu@43.173.121.68}"
VPS_KEY="${VPS_KEY:-$HOME/.ssh/server_access}"
VPS_DIR="${VPS_DIR:-glm2api}"

echo "[1/4] 本地编译检查"
.venv/bin/python -m py_compile glmstudio/*.py server.py

echo "[2/4] 同步代码到 ${VPS_HOST}:${VPS_DIR} ，不动 data/ 数据库"
rsync -az -e "ssh -i ${VPS_KEY}" \
  --exclude '.venv' --exclude 'data' --exclude '_ref' --exclude '.git' \
  --exclude '__pycache__' --exclude '.env' --exclude 'deploy.sh' \
  ./ "${VPS_HOST}:${VPS_DIR}/"

echo "[3/4] 重建容器"
ssh -i "${VPS_KEY}" "${VPS_HOST}" "cd ${VPS_DIR} && docker compose up -d --build 2>&1 | tail -1"

echo "[4/4] 健康验证"
for i in 1 2 3 4 5 6; do
  if ssh -i "${VPS_KEY}" "${VPS_HOST}" \
    "set -a; source ~/${VPS_DIR}/.env 2>/dev/null || true; curl -s --max-time 5 http://127.0.0.1:\${GLM_PORT:-8000}/health" 2>/dev/null; then
    echo && echo "deploy done" && exit 0
  fi
  sleep 4
done
echo "健康检查未通过，请上 VPS 查看: docker logs glm-studio --tail 30"
exit 1
