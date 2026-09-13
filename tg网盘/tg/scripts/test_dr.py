#!/usr/bin/env python3
"""灾备演练：造数据 -> 备份 -> 删库 -> 从 journal 重建 -> 逐项比对"""
import base64
import hashlib
import json
import os
import subprocess
import sys
import urllib.request

BASE = "http://127.0.0.1:8080"
PASS = subprocess.check_output(
    "grep TG_AUTH_PASS /opt/tgpool/tgpool.env | cut -d= -f2", shell=True
).decode().strip()
AUTH = "Basic " + base64.b64encode(("admin:" + PASS).encode()).decode()
PY = "/opt/tgpool/venv/bin/python"
DB = "/opt/tgpool/index.db"

ok_all = True


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                              headers={"Authorization": AUTH,
                                       "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=300) as resp:
        return json.load(resp)


def upload(name, payload, folder_id=None):
    b = "----t" + os.urandom(8).hex()
    buf = b""
    if folder_id is not None:
        buf += ("--%s\r\nContent-Disposition: form-data; name=\"folder_id\"\r\n\r\n%d\r\n"
                % (b, folder_id)).encode()
    buf += ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n" % (b, name)).encode()
    buf += payload + ("\r\n--%s--\r\n" % b).encode()
    r = urllib.request.Request(BASE + "/api/upload", data=buf, method="POST",
                               headers={"Authorization": AUTH,
                                        "Content-Type": "multipart/form-data; boundary=" + b})
    with urllib.request.urlopen(r, timeout=600) as resp:
        return json.load(resp)


def snapshot():
    """递归抓取逻辑结构：目录路径列表 + 文件(id,名,大小,所在路径)列表"""
    folders, files = [], []

    def walk(fid, prefix):
        j = req("GET", "/api/list" + (("?folder_id=%d" % fid) if fid else ""))
        for d in j["folders"]:
            p = prefix + "/" + d["name"]
            folders.append(p)
            walk(d["id"], p)
        for f in j["files"]:
            files.append((f["id"], f["name"], f["size"], prefix))

    walk(None, "")
    return {"folders": sorted(folders), "files": sorted(files)}


def run(args):
    out = subprocess.run([PY] + args, capture_output=True, text=True)
    for line in (out.stdout + out.stderr).strip().splitlines():
        print("   " + line)
    return out.returncode


print("=== 1. 造演练数据 ===")
fa = req("POST", "/api/folders", {"name": "灾备演练", "parent_id": None})["id"]
fs = req("POST", "/api/folders", {"name": "子目录", "parent_id": fa})["id"]
d1, d2 = os.urandom(300 * 1024), os.urandom(120 * 1024)
f1 = upload("dr_a.bin", d1, fa)
f2 = upload("dr_b.bin", d2, fs)
print("   目录: /灾备演练(=%d)  /灾备演练/子目录(=%d)" % (fa, fs))
print("   文件: dr_a.bin(id=%d, 在 灾备演练)  dr_b.bin(id=%d, 在 子目录)" % (f1["id"], f2["id"]))

print("=== 1b. 同级同名文件夹应被拒绝（保证路径唯一）===")
try:
    req("POST", "/api/folders", {"name": "灾备演练", "parent_id": None})
    print("   [x] 竟然允许创建同名文件夹")
    ok_all = False
except urllib.error.HTTPError as e:
    print("   [√] 已拒绝，HTTP %d" % e.code)

print("=== 2. 记录演练前状态 ===")
before = snapshot()
print("   目录 %d 个 / 文件 %d 个" % (len(before["folders"]), len(before["files"])))

print("=== 3. 备份 ===")
run(["/opt/tgpool/tools/backup_index.py", "--no-remote"])

print("=== 4. 模拟索引丢失：删掉 index.db ===")
for suf in ("", "-wal", "-shm"):
    try:
        os.remove(DB + suf)
    except FileNotFoundError:
        pass
print("   已删除 %s{,-wal,-shm}" % DB)
try:
    req("GET", "/api/stats")
    print("   [!] 索引已丢失但接口仍可用？")
except Exception as e:  # noqa: BLE001
    print("   确认接口已失效: %s" % type(e).__name__)

print("=== 5. 从 journal 重建 ===")
rc = run(["/opt/tgpool/tools/rebuild_index.py"])

print("=== 6. 比对重建结果 ===")
after = snapshot()
print("   重建后：目录 %d 个 / 文件 %d 个" % (len(after["folders"]), len(after["files"])))
if before["folders"] != after["folders"]:
    ok_all = False
    print("   [x] 目录不一致")
    print("       before:", before["folders"])
    print("       after :", after["folders"])
elif before["files"] != after["files"]:
    ok_all = False
    print("   [x] 文件清单不一致")
    bs, as_ = set(before["files"]), set(after["files"])
    for x in sorted(bs - as_):
        print("       仅重建前有:", x)
    for x in sorted(as_ - bs):
        print("       仅重建后有:", x)
else:
    print("   [√] 目录、文件 id/名称/大小/所在路径 全部一致")

print("=== 7. 验证下载仍可用（file_id 是否保住）===")
for rec, src in ((f1, d1), (f2, d2)):
    r = urllib.request.Request("%s/api/download/%d" % (BASE, rec["id"]),
                               headers={"Authorization": AUTH})
    with urllib.request.urlopen(r, timeout=600) as resp:
        got = resp.read()
    same = hashlib.sha256(got).hexdigest() == hashlib.sha256(src).hexdigest()
    ok_all = ok_all and same
    print("   [%s] id=%d %-10s %d 字节 sha256 %s"
          % ("√" if same else "x", rec["id"], rec["name"], len(got),
             "一致" if same else "不一致"))

print("=== 8. 清理演练数据 ===")
# 注意：重建后文件夹 id 会被重新分配，必须按名字重新查找
for d in req("GET", "/api/list")["folders"]:
    if d["name"] == "灾备演练":
        req("DELETE", "/api/folders/%d?force=true" % d["id"])
        print("   已删除 /灾备演练（递归含子目录与文件）")
        break
rest = snapshot()
print("   清理后剩余：目录 %d 个 / 文件 %d 个" % (len(rest["folders"]), len(rest["files"])))
if len(before["files"]) - 2 != len(rest["files"]):
    ok_all = False
    print("   [x] 演练数据未清理干净")

print()
print("==> 灾备演练 %s" % ("全部通过" if ok_all else "存在失败"))
sys.exit(0 if ok_all else 1)
