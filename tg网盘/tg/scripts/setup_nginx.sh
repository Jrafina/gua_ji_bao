#!/bin/bash
# 为 TG 存储池配置对外 HTTPS 入口（默认 8443，独立端口直连以绕过 Cloudflare 100MB 限制）
# 自带兜底：nginx 没装就装，证书没有就生成自签名证书，可直接在全新机器上跑。
#   bash setup_nginx.sh
#   TG_NGINX_PORT=9443 bash setup_nginx.sh   # 换端口
set -e

PORT="${TG_NGINX_PORT:-8443}"
SSL_DIR=/etc/nginx/ssl

echo "=== 1. 确保 nginx 已安装 ==="
if ! command -v nginx >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get install -y nginx >/tmp/nginx_apt.log 2>&1 || { tail -20 /tmp/nginx_apt.log; exit 1; }
fi
nginx -v 2>&1
systemctl enable nginx >/dev/null 2>&1 || true
systemctl start nginx 2>/dev/null || true

echo "=== 2. 选择证书（优先沿用已有的，否则生成自签名）==="
mkdir -p "$SSL_DIR"
CERT=""
KEY=""
for base in /etc/nginx/ssl/o.jrafina.top /etc/nginx/ssl/server /etc/nginx/ssl/tgpool; do
  if [ -f "${base}.crt" ] && [ -f "${base}.key" ]; then
    CERT="${base}.crt"
    KEY="${base}.key"
    break
  fi
done

if [ -n "$CERT" ]; then
  echo "  沿用已有证书: $CERT"
else
  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [ -z "$IP" ] && IP="127.0.0.1"
  # 用配置文件而非 -addext，兼容老版 openssl
  cat > /tmp/tgpool-openssl.cnf <<EOF
[req]
distinguished_name = dn
x509_extensions    = v3
prompt             = no
[dn]
C = CN
O = tgpool
CN = ${IP}
[v3]
subjectAltName = IP:${IP}
EOF
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -config /tmp/tgpool-openssl.cnf \
    -keyout "$SSL_DIR/tgpool.key" -out "$SSL_DIR/tgpool.crt" 2>/dev/null
  rm -f /tmp/tgpool-openssl.cnf
  CERT="$SSL_DIR/tgpool.crt"
  KEY="$SSL_DIR/tgpool.key"
  echo "  已生成自签名证书（CN/SAN = $IP），有效期 10 年"
  echo "  浏览器首次访问会提示不受信任，点「继续访问」即可"
fi
chmod 600 "$KEY"
openssl x509 -in "$CERT" -noout -subject -dates 2>/dev/null | sed 's/^/  /'

echo "=== 3. 写入 server block（端口 ${PORT}）==="
cat > /etc/nginx/sites-available/tgpool <<EOF
# TG Storage Pool - 独立端口直连, 绕过 Cloudflare 100MB 上传限制
server {
    listen ${PORT} ssl;
    listen [::]:${PORT} ssl;
    server_name _;

    ssl_certificate     ${CERT};
    ssl_certificate_key ${KEY};
    ssl_protocols TLSv1.2 TLSv1.3;

    client_max_body_size 2000m;
    proxy_read_timeout   3600s;
    proxy_send_timeout   3600s;
    send_timeout         3600s;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_http_version 1.1;
        # 关闭缓冲，保证大文件流式传输（不落 nginx 磁盘）
        proxy_request_buffering off;
        proxy_buffering         off;
    }
}
EOF

ln -sf /etc/nginx/sites-available/tgpool /etc/nginx/sites-enabled/tgpool

echo "=== 4. 配置检查 ==="
nginx -t

echo "=== 5. 重载 ==="
systemctl reload nginx

echo "=== 6. 监听检查 ==="
ss -tlnp | grep -E ":${PORT}|:80 |:443 " || true

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "完成。访问地址: https://${IP}:${PORT}/"
echo "若从外网访问不通，检查云服务商安全组/防火墙是否放行 ${PORT} 端口。"
