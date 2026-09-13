#!/usr/bin/env bash
# 文件夹功能端到端测试
set -u
PASS=$(grep TG_AUTH_PASS /opt/tgpool/tgpool.env | cut -d= -f2)
B="http://127.0.0.1:8080"
A=(-s -u "admin:$PASS")
JQ() { python3 -c "import sys,json;d=json.load(sys.stdin);print($1)"; }

echo "=== 0. 初始状态 ==="
curl "${A[@]}" "$B/api/stats"; echo

echo "=== 1. 创建根文件夹 A / B ==="
FA=$(curl "${A[@]}" -H 'Content-Type: application/json' -d '{"name":"备份A","parent_id":null}' "$B/api/folders" | JQ "d['id']")
FB=$(curl "${A[@]}" -H 'Content-Type: application/json' -d '{"name":"备份B","parent_id":null}' "$B/api/folders" | JQ "d['id']")
echo "A=$FA B=$FB"

echo "=== 2. 在 A 下建子文件夹 ==="
FS=$(curl "${A[@]}" -H 'Content-Type: application/json' -d "{\"name\":\"子目录/非法字符\",\"parent_id\":$FA}" "$B/api/folders" | JQ "d['id']")
echo "sub=$FS  (名字里的 / 应被替换为 _)"
curl "${A[@]}" "$B/api/folders/tree"; echo

echo "=== 3. 上传 1MB 到 A ==="
head -c 1048576 /dev/urandom > /tmp/ft.bin
UP=$(curl "${A[@]}" -F "file=@/tmp/ft.bin;filename=folder_test.bin" -F "folder_id=$FA" "$B/api/upload")
echo "$UP"
FID=$(echo "$UP" | JQ "d['id']")

echo "=== 4. 列根目录（应只见文件夹，无文件）==="
curl "${A[@]}" "$B/api/list" | python3 -m json.tool --no-ensure-ascii | head -30

echo "=== 5. 列 A 目录（应有 1 文件 + 1 子目录）==="
curl "${A[@]}" "$B/api/list?folder_id=$FA" | python3 -m json.tool --no-ensure-ascii
