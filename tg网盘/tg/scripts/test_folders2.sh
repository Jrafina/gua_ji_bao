#!/usr/bin/env bash
# 文件夹功能端到端测试（后半）
set -u
PASS=$(grep TG_AUTH_PASS /opt/tgpool/tgpool.env | cut -d= -f2)
B="http://127.0.0.1:8080"
A=(-s -u "admin:$PASS")
JQ() { python3 -c "import sys,json;d=json.load(sys.stdin);print($1)"; }
JS() { python3 -c "import sys,json;d=json.load(sys.stdin);print(json.dumps(d,ensure_ascii=False,indent=1))"; }

FA=1; FB=2; FS=3; FID=7

echo "=== 6. 文件从 A 移到子目录(3) ==="
curl "${A[@]}" -X PATCH -H 'Content-Type: application/json' -d '{"folder_id":3}' "$B/api/files/$FID"; echo
curl "${A[@]}" "$B/api/list?folder_id=3" | JQ "[(f['name'],f['size']) for f in d['files']]"

echo "=== 7. 重命名 备份B -> 归档B ==="
curl "${A[@]}" -X PATCH -H 'Content-Type: application/json' -d '{"name":"归档B"}' "$B/api/folders/$FB"; echo
curl "${A[@]}" "$B/api/folders/tree" | JQ "[f['name'] for f in d['folders']]"

echo "=== 8. 非法移动：A 移入自己的子目录(3) 应 400 ==="
curl "${A[@]}" -o /dev/null -w 'http=%{http_code}\n' -X PATCH -H 'Content-Type: application/json' -d '{"parent_id":3}' "$B/api/folders/$FA"

echo "=== 9. 搜索跨目录 ==="
curl "${A[@]}" "$B/api/list?q=folder_test" | JQ "[(f['name'], [p['name'] for p in f['path']]) for f in d['files']]"

echo "=== 10. 从子目录下载并校验 sha256 ==="
curl "${A[@]}" -o /tmp/ft_dl.bin "$B/api/download/$FID"
echo "源:  $(sha256sum /tmp/ft.bin   | cut -d' ' -f1)"
echo "下载:$(sha256sum /tmp/ft_dl.bin | cut -d' ' -f1)"

echo "=== 11. 非空文件夹不加 force 删除 应 409 ==="
curl "${A[@]}" -o /dev/null -w 'http=%{http_code}\n' -X DELETE "$B/api/folders/$FA"

echo "=== 12. 递归删除 A（含子目录与文件）==="
curl "${A[@]}" -X DELETE "$B/api/folders/$FA?force=true"; echo

echo "=== 13. 清理 B ==="
curl "${A[@]}" -X DELETE "$B/api/folders/$FB?force=true"; echo

echo "=== 14. 最终状态（应回到最初的 3 个根文件、0 文件夹）==="
curl "${A[@]}" "$B/api/stats"; echo
curl "${A[@]}" "$B/api/list" | JQ "([f['name'] for f in d['folders']], [(f['id'],f['name']) for f in d['files']])"
rm -f /tmp/ft.bin /tmp/ft_dl.bin
