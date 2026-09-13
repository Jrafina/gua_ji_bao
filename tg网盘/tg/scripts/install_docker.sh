#!/bin/bash
set -e
export DEBIAN_FRONTEND=noninteractive

echo "=== 1. test docker hub reachability ==="
timeout 15 curl -s -o /dev/null -w 'registry-1.docker.io: %{http_code} (%{time_total}s)\n' https://registry-1.docker.io/v2/ || echo "registry-1.docker.io: TIMEOUT/BLOCKED"
timeout 15 curl -s -o /dev/null -w 'hub.docker.com: %{http_code} (%{time_total}s)\n' https://hub.docker.com || echo "hub.docker.com: TIMEOUT/BLOCKED"

echo "=== 2. install docker.io ==="
apt-get install -y docker.io >/tmp/docker_apt.log 2>&1 || { tail -20 /tmp/docker_apt.log; exit 1; }
tail -3 /tmp/docker_apt.log

echo "=== 3. configure registry mirrors ==="
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'EOF'
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://dockerproxy.com",
    "https://mirror.baidubce.com",
    "https://hub-mirror.c.163.com"
  ],
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
EOF
cat /etc/docker/daemon.json

echo "=== 4. start docker ==="
systemctl enable docker >/dev/null 2>&1
systemctl restart docker
sleep 5
systemctl is-active docker
docker --version

echo "=== 5. test pull ==="
timeout 120 docker pull hello-world 2>&1 | tail -5 || echo "pull hello-world FAILED"
docker images | head

echo "=== docker ready ==="
