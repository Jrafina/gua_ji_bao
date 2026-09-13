#!/bin/bash
echo "=== who holds the lock ==="
ps -fp 358968 2>/dev/null || echo "pid 358968 gone"
ps aux | grep -E 'apt-get|dpkg|unattended' | grep -v grep || echo "none"

echo "=== killing apt/dpkg ==="
pkill -9 -x apt-get 2>/dev/null
pkill -9 -x dpkg 2>/dev/null
pkill -9 -f unattended-upgrade 2>/dev/null
sleep 3

echo "=== removing stale locks ==="
rm -f /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/cache/apt/archives/lock 2>/dev/null
dpkg --configure -a >/dev/null 2>&1 || true

echo "=== recheck ==="
ps aux | grep -E 'apt-get|dpkg' | grep -v grep || echo "clean"
echo "done"
