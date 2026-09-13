#!/usr/bin/env python3
"""二进制安全地上传文件到服务器（不改动任何字节，适合 tar.gz / 图片等）。

    python ssh_put.py <本地文件> <远程路径>

与 ssh_upload.py 的区别：后者会把 \\r\\n 替换为 \\n（适合 .sh/.py 文本），
本脚本原样传输，用于二进制文件。
"""
import sys

import paramiko

HOST = "82.158.224.64"
PORT = 22
USER = "root"
PASS = "pyucDKRB1727"


def mkdirs(sftp, path):
    """递归创建远程目录（已存在则忽略）"""
    if not path or path == "/":
        return
    acc = ""
    for seg in path.strip("/").split("/"):
        acc += "/" + seg
        try:
            sftp.stat(acc)
        except IOError:
            try:
                sftp.mkdir(acc)
            except IOError:
                pass


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    local, remote = sys.argv[1], sys.argv[2]
    with open(local, "rb") as f:
        data = f.read()

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, port=PORT, username=USER, password=PASS,
              timeout=20, banner_timeout=20, auth_timeout=20)
    sftp = c.open_sftp()
    if "/" in remote:
        mkdirs(sftp, remote.rsplit("/", 1)[0])
    with sftp.open(remote, "wb") as f:
        f.write(data)
    sftp.close()
    c.close()
    print("uploaded %s -> %s (%d bytes, binary-safe)" % (local, remote, len(data)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
