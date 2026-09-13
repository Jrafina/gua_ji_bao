#!/usr/bin/env python3
"""上传本地脚本到服务器并执行。用法: python ssh_push.py <本地脚本> [远程路径]"""
import sys
import paramiko

HOST = "82.158.224.64"
PORT = 22
USER = "root"
PASS = "pyucDKRB1727"


def main():
    local = sys.argv[1]
    remote = sys.argv[2] if len(sys.argv) > 2 else "/tmp/_run.sh"
    with open(local, "rb") as f:
        data = f.read().replace(b"\r\n", b"\n")

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, port=PORT, username=USER, password=PASS, timeout=20,
              banner_timeout=20, auth_timeout=20)
    sftp = c.open_sftp()
    with sftp.open(remote, "wb") as f:
        f.write(data)
    sftp.chmod(remote, 0o755)
    sftp.close()

    stdin, stdout, stderr = c.exec_command(f"bash {remote}", timeout=3500)
    sys.stdout.write(stdout.read().decode("utf-8", "replace"))
    err = stderr.read().decode("utf-8", "replace")
    if err.strip():
        sys.stdout.write("[stderr] " + err)
    print(f"[exit={stdout.channel.recv_exit_status()}]")
    c.close()


if __name__ == "__main__":
    main()
