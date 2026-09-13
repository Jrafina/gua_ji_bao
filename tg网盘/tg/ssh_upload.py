#!/usr/bin/env python3
"""仅上传文件到服务器。用法: python ssh_upload.py <本地文件> <远程路径>"""
import sys
import paramiko

HOST = "82.158.224.64"
PORT = 22
USER = "root"
PASS = "pyucDKRB1727"


def main():
    local, remote = sys.argv[1], sys.argv[2]
    with open(local, "rb") as f:
        data = f.read().replace(b"\r\n", b"\n")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, port=PORT, username=USER, password=PASS, timeout=20,
              banner_timeout=20, auth_timeout=20)
    sftp = c.open_sftp()
    try:
        sftp.mkdir("/opt/tgpool-setup")
    except IOError:
        pass
    with sftp.open(remote, "wb") as f:
        f.write(data)
    sftp.chmod(remote, 0o755)
    sftp.close()
    c.close()
    print(f"uploaded {local} -> {remote} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
