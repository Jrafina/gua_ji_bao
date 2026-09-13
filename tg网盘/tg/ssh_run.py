#!/usr/bin/env python3
"""通过 SSH 在远程服务器上执行命令。用法: python ssh_run.py "命令" """
import sys
import paramiko

HOST = "82.158.224.64"
PORT = 22
USER = "root"
PASS = "pyucDKRB1727"


def run(cmd, timeout=120):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, port=PORT, username=USER, password=PASS,
                   timeout=20, banner_timeout=20, auth_timeout=20)
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    client.close()
    return out, err, code


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "echo hello"
    try:
        out, err, code = run(cmd)
        if out:
            print(out)
        if err:
            print("[stderr]", err)
        print(f"[exit={code}]")
    except Exception as e:
        print(f"[SSH ERROR] {type(e).__name__}: {e}")
        sys.exit(1)
