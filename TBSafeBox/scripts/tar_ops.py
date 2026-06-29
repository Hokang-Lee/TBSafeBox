
# scripts/tar_ops.py
import os
import sys
import subprocess
import shlex
import shutil
import hashlib
import socket
import time
import threading
from datetime import datetime, timedelta
from typing import List, Optional, Callable, Tuple, Dict

# --- Logging callback type ---
TarLogCb = Optional[Callable[[str], None]]

def _log(msg: str, cb: TarLogCb):
    if cb:
        cb(msg)
    else:
        print(msg)

def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")

def which_tar() -> str:
    for cand in ["/usr/bin/tar", "/bin/tar", "tar"]:
        if shutil.which(cand) or os.path.exists(cand):
            return cand
    return "tar"

# --- FreeBSD バージョン検出 & tar オプション生成 ---
_freebsd_version_cache: Optional[int] = None

def _detect_freebsd_version(ssh_cfg: Optional[Dict] = None, log_cb: TarLogCb = None) -> Optional[int]:
    """
    FreeBSD のメジャーバージョンを SSH 経由で検出してキャッシュする。
    戻り値: 12, 13, 14 ... など。検出失敗時は None。
    """
    global _freebsd_version_cache
    if _freebsd_version_cache is not None:
        return _freebsd_version_cache
    try:
        if ssh_cfg:
            # freebsd-version は stderr だけでなく stdout にも usage を出すことがある
            # uname -r で確実に "12.3-RELEASE-p11" 形式を取得する
            rc, out = ssh_exec_subprocess("uname -r", ssh_cfg, log_cb)
            version_str = out.strip() if rc == 0 else ""
        else:
            res = subprocess.run(["freebsd-version", "-u"],
                                 capture_output=True, text=True, timeout=5)
            if res.returncode != 0:
                res = subprocess.run(["uname", "-r"],
                                     capture_output=True, text=True, timeout=5)
            version_str = res.stdout.strip()
        # 複数行が返る場合は最初の行だけ使う ("12.3-RELEASE-p11" → 12)
        first_line = version_str.splitlines()[0].strip() if version_str else ""
        if first_line:
            major = int(first_line.split(".")[0].split("-")[0])
            _freebsd_version_cache = major
            _log(f"[detect] FreeBSD version: {major}", log_cb)
            return major
    except Exception as e:
        _log(f"[detect] FreeBSD version detection failed: {e}", log_cb)
    return None

def _build_zstd_options(compression_level: int, threads: int,
                         ssh_cfg: Optional[Dict] = None,
                         log_cb: TarLogCb = None) -> str:
    """
    FreeBSD 12 以前は zstd:threads を非サポートのため除外する。
    FreeBSD 13 以降、または検出失敗時は threads を含める。
    """
    ver = _detect_freebsd_version(ssh_cfg, log_cb)
    if ver is not None and ver <= 12:
        _log(f"[tar] FreeBSD {ver}: zstd:threads not supported, using compression-level only", log_cb)
        return f"zstd:compression-level={compression_level}"
    if threads > 0:
        return f"zstd:compression-level={compression_level},zstd:threads={threads}"
    return f"zstd:compression-level={compression_level}"

# --- 追加：ヘルパー（tar_ops.py の先頭付近が分かりやすい） ---
def _which_sshpass() -> Optional[str]:
    for cand in ["sshpass", "sshpass.exe"]:
        p = shutil.which(cand)
        if p:
            return p
    return None

def _which_pscp() -> Optional[str]:
    # PuTTYのpscp.exe（Windowsのみ想定）
    return shutil.which("pscp.exe")

def _wrap_with_sshpass_or_pscp(base_cmd: List[str], ssh_cfg: Dict) -> List[str]:
    """
    use_password=True のとき、ssh/scp を "sshpass -p <pw>" でラップ。
    Windowsでsshpassが無ければ pscp.exe に置き換える（scpのみ）。
    """
    use_password = bool(ssh_cfg.get("use_password", False))
    password = ssh_cfg.get("password") or ""
    if not use_password:
        return base_cmd

    # sshpassがあるならそれを使う
    sshpass_bin = _which_sshpass()
    if sshpass_bin:
        # パスワード認証を優先し、プロンプト回数を抑制
        extra = ["-o", "PreferredAuthentications=password",
                 "-o", "NumberOfPasswordPrompts=1"]
        return [sshpass_bin, "-p", password] + base_cmd[:1] + base_cmd[1:] + extra

    # sshpassが無い & コマンドが scp の場合は pscp.exe へ置換（Windows PuTTY）
    if base_cmd and os.path.basename(base_cmd[0]).lower() in ("scp", "scp.exe"):
        pscp = _which_pscp()
        if pscp:
            # scp [args] local remote を pscp.exe 形式に変換
            # scp の共通オプション： -P, -i, -o ... の一部はpscpに渡せないため、最低限 -P/-pw のみ対応
            # base_cmd = [scp, ... , src, dest]
            # ここでは簡易に "-P port" と "-pw password" を適用
            port = int(ssh_cfg.get("port", 22))
            # base_cmd末尾2つが src/dest なので流用
            src = base_cmd[-2]
            dest = base_cmd[-1]
            cmd = [pscp, "-P", str(port)]
            if password:
                cmd += ["-pw", password]
            keyfile = ssh_cfg.get("keyfile")
            if keyfile:
                cmd += ["-i", keyfile]
            cmd += [src, dest]
            return cmd

    # どちらも無ければ、そのまま返す（この場合は対話が必要）
    return base_cmd

def default_remote_dest_dir(source_dir: str) -> str:
    """
    リモート（FreeBSD）のPOSIXパスを返す。
    Windowsで動くクライアント側でも os.path.join を使わない。
    例: /var/spool/epms -> /var/spool/.backups
    """
    s = source_dir.rstrip("/")
    # 親ディレクトリをPOSIX的に取り出す
    parent = s[:s.rfind("/")] if "/" in s else "/"
    if not parent:
        parent = "/"
    return parent + "/.backups"

# --- OpenSSH client helpers (SCP/SSH subprocess, fallback for SFTP mode) ---
def _which_scp() -> Optional[str]:
    for cand in ["scp", "scp.exe"]:
        p = shutil.which(cand)
        if p:
            return p
    return None

def _which_ssh() -> Optional[str]:
    for cand in ["ssh", "ssh.exe"]:
        p = shutil.which(cand)
        if p:
            return p
    return None


def _build_scp_common_args(ssh_cfg: Dict) -> List[str]:
    args = []
    port = int(ssh_cfg.get("port", 22))
    keyfile = ssh_cfg.get("keyfile")
    args.extend(["-P", str(port)])
    if keyfile:
        args.extend(["-i", keyfile, "-o", "IdentitiesOnly=yes"])  # ← この鍵だけを使わせる
    args.extend([
        "-o", "BatchMode=yes",
        "-o", "PreferredAuthentications=publickey",
        "-o", "PubkeyAuthentication=yes",
        "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no",
        "-o", "NumberOfPasswordPrompts=0",
        "-o", "StrictHostKeyChecking=accept-new",
    ])
    return args

def _build_ssh_common_args(ssh_cfg: Dict) -> List[str]:
    args = []
    port = int(ssh_cfg.get("port", 22))
    keyfile = ssh_cfg.get("keyfile")
    args.extend(["-p", str(port)])
    if keyfile:
        args.extend(["-i", keyfile, "-o", "IdentitiesOnly=yes"])  # ← 必須
    args.extend([
        "-o", "BatchMode=yes",
        "-o", "PreferredAuthentications=publickey",
        "-o", "PubkeyAuthentication=yes",
        "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no",
        "-o", "NumberOfPasswordPrompts=0",
        "-o", "StrictHostKeyChecking=accept-new",
    ])
    return args

# --- 既存の ssh_exec_subprocess を差し替え ---
def ssh_exec_subprocess(cmd: str, ssh_cfg: Dict, log_cb: TarLogCb = None) -> Tuple[int, str]:
    ssh_bin = _which_ssh()
    if not ssh_bin:
        raise RuntimeError("[ssh] ssh client not found (OpenSSH)")
    user = ssh_cfg.get("user", "")
    host = ssh_cfg.get("host", "")
    if not user or not host:
        raise RuntimeError("[ssh] user/host is required")

    full = [ssh_bin] + _build_ssh_common_args(ssh_cfg) + [f"{user}@{host}", cmd]
    # ★ パスワード運用なら sshpass/pscp ラップ
    full = _wrap_with_sshpass_or_pscp(full, ssh_cfg)

    _log(f"[ssh-subproc] $ {' '.join(shlex.quote(c) for c in full)}", log_cb)
    proc = subprocess.run(full, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if log_cb and proc.stdout:
        for ln in proc.stdout.splitlines():
            log_cb(ln)
    return proc.returncode, proc.stdout or ""

# --- 既存の scp_download を差し替え ---
def scp_download(remote_path: str, local_path: str, ssh_cfg: Dict, log_cb: TarLogCb = None):
    scp_bin = _which_scp()
    if not scp_bin:
        _log("[scp] scp not found; falling back to SFTP", log_cb)
        return sftp_download(remote_path, local_path, ssh_cfg, log_cb)

    user = ssh_cfg.get("user", "")
    host = ssh_cfg.get("host", "")
    if not user or not host:
        raise RuntimeError("[scp] user/host is required")

    local_dir = os.path.dirname(local_path)
    if local_dir:
        os.makedirs(local_dir, exist_ok=True)

    remote_spec = f"{user}@{host}:{remote_path}"
    cmd = [scp_bin] + _build_scp_common_args(ssh_cfg) + [remote_spec, local_path]
    # ★ ラップ適用
    cmd = _wrap_with_sshpass_or_pscp(cmd, ssh_cfg)

    _log(f"[scp] download: {' '.join(shlex.quote(c) for c in cmd)}", log_cb)
    subprocess.check_call(cmd)
    _log("[scp] download done", log_cb)

# --- 既存の scp_upload を差し替え ---
def scp_upload(local_path: str, remote_path: str, ssh_cfg: Dict, log_cb: TarLogCb = None):
    scp_bin = _which_scp()
    if not scp_bin:
        _log("[scp] scp not found; falling back to SFTP", log_cb)
        return sftp_upload(local_path, remote_path, ssh_cfg, log_cb)

    user = ssh_cfg.get("user", "")
    host = ssh_cfg.get("host", "")
    if not user or not host:
        raise RuntimeError("[scp] user/host is required")

    remote_dir = os.path.dirname(remote_path)
    # ★ FreeBSD /bin/sh で確実に動くよう、単純に mkdir -p のみにする
    rc, out = ssh_exec_subprocess(
        f"mkdir -p {shlex.quote(remote_dir)}",
        ssh_cfg, log_cb
    )
    if rc != 0:
        raise RuntimeError("[scp] failed to ensure remote dir\n" + (out or ""))

    remote_spec = f"{user}@{host}:{remote_path}"
    cmd = [scp_bin] + _build_scp_common_args(ssh_cfg) + [local_path, remote_spec]
    # ★ ラップ適用
    cmd = _wrap_with_sshpass_or_pscp(cmd, ssh_cfg)

    _log(f"[scp] upload: {' '.join(shlex.quote(c) for c in cmd)}", log_cb)
    subprocess.check_call(cmd)
    _log("[scp] upload done", log_cb)

# --- SFTP (Paramiko) ※SFTPモード時のみ使用 ---
try:
    import paramiko
except Exception:
    paramiko = None

class SSHRunner:
    """Paramiko wrapper（SFTPモード用。SCPモードでは使わない）。"""
    def __init__(self, cfg: Dict, log_cb: TarLogCb = None):
        if paramiko is None:
            raise RuntimeError("Paramiko not available. Install with: pip install paramiko")
        self.cfg = cfg
        self.log_cb = log_cb
        self.client = None

    def connect(self):
        host = self.cfg.get("host")
        port = int(self.cfg.get("port", 22))
        user = self.cfg.get("user")
        use_password = bool(self.cfg.get("use_password", False))
        password = self.cfg.get("password")
        keyfile = self.cfg.get("keyfile")
        key_pass = self.cfg.get("key_passphrase") or (password if use_password else None)

        allow_agent = True      # agent を使う
        look_for_keys = False   # 既定鍵の探索はしない（明示鍵/agentのみ）

        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # まずは鍵で開く（パス無し鍵 or パスフレーズ提供時）
        pkey, keyfile_password_required = None, False
        if keyfile and os.path.isfile(keyfile):
            for KeyCls in (getattr(paramiko, "Ed25519Key", None), paramiko.RSAKey, paramiko.ECDSAKey):
                if KeyCls is None:
                    continue
                try:
                    pkey = KeyCls.from_private_key_file(keyfile, password=key_pass)
                    _log(f"[ssh] loaded private key: {keyfile} ({KeyCls.__name__})", self.log_cb)
                    break
                except paramiko.PasswordRequiredException:
                    keyfile_password_required = True  # パスフレーズ必須→agentに期待
                    _log("[ssh] key is encrypted; no passphrase provided -> try ssh-agent only", self.log_cb)
                    break
                except Exception:
                    continue

        # Use password が OFF なら、パスワード方式は完全禁止
        connect_password = (password if use_password else None)

        # pkey が作れた時だけ key_filename を渡す（failoverでパスワードに降りない）
        key_filename = keyfile if (pkey is None and not keyfile_password_required and keyfile) else None

        last_exc = None
        for i in range(1, int(self.cfg.get("connect_attempts", 3)) + 1):
            try:
                self.client.connect(
                    hostname=host, port=port, username=user,
                    pkey=pkey,
                    key_filename=key_filename,
                    password=connect_password,   # OFFなら None → パスワード認証しない
                    allow_agent=allow_agent, look_for_keys=look_for_keys,
                    timeout=int(self.cfg.get("timeout", 30)),
                    banner_timeout=int(self.cfg.get("banner_timeout", 60)),
                    auth_timeout=int(self.cfg.get("auth_timeout", 60)),
                )
                _log("[ssh] connected", self.log_cb)
                tr = self.client.get_transport()
                if tr: tr.set_keepalive(15)
                return
            except Exception as e:
                last_exc = e
                _log(f"[ssh] connect failed: {e}", self.log_cb)
                if i < int(self.cfg.get("connect_attempts", 3)):
                    time.sleep(1.5)
                else:
                    raise RuntimeError(f"[ssh] connect error after {self.cfg.get('connect_attempts',3)} attempts: {last_exc}") from last_exc

    def close(self):
        if self.client:
            self.client.close()
            self.client = None

    def exec(self, cmd: str, use_sudo: bool = False) -> Tuple[int, str, str]:
        if use_sudo:
            cmd = f"sudo -n sh -c {shlex.quote(cmd)}"
        _log(f"[ssh] $ {cmd}", self.log_cb)
        stdin, stdout, stderr = self.client.exec_command(cmd)
        out_lines = []
        for line in iter(stdout.readline, ''):
            if line:
                ln = line.rstrip()
                out_lines.append(ln)
                _log(ln, self.log_cb)
        err = stderr.read().decode()
        code = stdout.channel.recv_exit_status()
        return code, "\n".join(out_lines), err

    def sftp(self):
        return self.client.open_sftp()

def to_shell(cmd_list: List[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd_list)

# --- Backup build/run ---
def build_backup_cmd(
    source_dir: str,
    dest_archive_dir: Optional[str] = None,
    compression_level: int = 6,
    threads: int = 0,
    excludes: Optional[List[str]] = None,
    one_file_system: bool = True,
    ssh_cfg: Optional[Dict] = None,
    log_cb: TarLogCb = None,
) -> Tuple[List[str], str, str, str]:
    if dest_archive_dir is None:
        dest_archive_dir = default_remote_dest_dir(source_dir)
    ts = timestamp()
    archive_path = os.path.join(dest_archive_dir, f"epms-{ts}.tar.zst")
    list_path = archive_path + ".list"
    sha_path = archive_path + ".sha256"
    tar_bin = which_tar()
    zstd_opts = _build_zstd_options(compression_level, threads, ssh_cfg, log_cb)
    cmd = [tar_bin, "-c", "--acls", "--xattrs", "--fflags", "--zstd",
           "--options", zstd_opts]
    if one_file_system:
        cmd.append("--one-file-system")
    if excludes:
        for pat in excludes:
            if pat:
                cmd.extend(["--exclude", pat])
    cmd.extend(["-f", archive_path, "-C", os.path.dirname(source_dir), os.path.basename(source_dir)])
    return cmd, archive_path, list_path, sha_path

def run_backup(cmd: List[str], archive_path: str, list_path: str, sha_path: str, log_cb: TarLogCb = None):
    _log("[tar] creating archive...\n " + " ".join(shlex.quote(c) for c in cmd), log_cb)
    subprocess.check_call(cmd)
    lst_cmd = [cmd[0], "-t", "-f", archive_path]
    _log(f"[tar] writing listing: {list_path}", log_cb)
    with open(list_path, "wb") as outf:
        subprocess.check_call(lst_cmd, stdout=outf)
    _log(f"[sha256] calculating (python) -> {sha_path}", log_cb)
    with open(sha_path, "w", encoding="utf-8") as outf:
        outf.write(calc_sha256(archive_path) + "\n")
    _log("[done] backup complete", log_cb)

# --- Backup over SSH ---
def run_backup_ssh(cmd_list: List[str], archive_path: str, list_path: str, sha_path: str, ssh_cfg: Dict, log_cb: TarLogCb = None):
    method = str(ssh_cfg.get("transfer_method", "sftp")).lower()
    if method == "scp":
        dest_dir = os.path.dirname(archive_path)
        rc, out = ssh_exec_subprocess(f"test -d {shlex.quote(dest_dir)} || mkdir -p {shlex.quote(dest_dir)}",
                                      ssh_cfg, log_cb)
        if rc != 0:
            raise RuntimeError("Failed to ensure destination directory on remote host (ssh subprocess)\n" + (out or ""))
        rc, out = ssh_exec_subprocess(to_shell(cmd_list), ssh_cfg, log_cb)
        if rc != 0:
            raise RuntimeError("Remote tar backup failed (ssh subprocess)\n" + (out or ""))
        rc, out = ssh_exec_subprocess(f"tar -t -f {shlex.quote(archive_path)} > {shlex.quote(list_path)}",
                                      ssh_cfg, log_cb)
        if rc != 0:
            raise RuntimeError("Remote listing failed (ssh subprocess)\n" + (out or ""))
        rc, out = ssh_exec_subprocess(f"sha256 {shlex.quote(archive_path)} > {shlex.quote(sha_path)}",
                                      ssh_cfg, log_cb)
        if rc != 0:
            raise RuntimeError("Remote sha256 failed (ssh subprocess)\n" + (out or ""))
        _log("[done] remote backup complete (ssh subprocess)", log_cb)
        return

    runner = SSHRunner(ssh_cfg, log_cb)
    try:
        runner.connect()
        dest_dir = os.path.dirname(archive_path)
        rc, out, err = runner.exec(f"test -d {shlex.quote(dest_dir)} || mkdir -p {shlex.quote(dest_dir)}", use_sudo=False)
        if rc != 0:
            raise RuntimeError("Failed to ensure destination directory on remote host\n" + err)
        rc, out, err = runner.exec(to_shell(cmd_list), use_sudo=bool(ssh_cfg.get("use_sudo", False)))
        if rc != 0:
            raise RuntimeError("Remote tar backup failed\n" + err)
        rc, out, err = runner.exec(f"tar -t -f {shlex.quote(archive_path)} > {shlex.quote(list_path)}", use_sudo=False)
        if rc != 0:
            raise RuntimeError("Remote listing failed\n" + err)
        rc, out, err = runner.exec(f"sha256 {shlex.quote(archive_path)} > {shlex.quote(sha_path)}", use_sudo=False)
        if rc != 0:
            raise RuntimeError("Remote sha256 failed\n" + err)
        _log("[done] remote backup complete", log_cb)
    finally:
        runner.close()

# --- Restore build/run ---
def build_restore_cmd(archive_path: str, restore_parent: str = "/var/spool", keep_old_files: bool = False) -> List[str]:
    tar_bin = which_tar()
    cmd = [tar_bin, "-x", "-p", "--acls", "--xattrs", "--fflags", "--zstd",
           "-f", archive_path, "-C", restore_parent]
    if keep_old_files:
        cmd.insert(1, "--keep-old-files")
    return cmd

def run_restore(cmd: List[str], log_cb: TarLogCb = None):
    _log("[tar] restoring...\n " + " ".join(shlex.quote(c) for c in cmd), log_cb)
    subprocess.check_call(cmd)
    _log("[done] restore complete", log_cb)

# --- Verify SHA256(Local/Remote) ---
def parse_sha256_file(sha256_path: str) -> str:
    with open(sha256_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read().strip()
        if " = " in content:
            return content.split(" = ")[-1].strip()
        parts = content.split()
        return parts[0].strip() if parts else ""

def calc_sha256(file_path: str, chunk_size: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()

def verify_sha256(archive_path: str, sha256_path: str, log_cb: TarLogCb = None) -> bool:
    if not os.path.isfile(archive_path) or not os.path.isfile(sha256_path):
        raise FileNotFoundError("archive or sha256 file missing")
    _log("[sha256] verifying integrity (python-hashlib)", log_cb)
    expected = parse_sha256_file(sha256_path)
    actual = calc_sha256(archive_path)
    ok = (expected.lower() == actual.lower())
    _log(f"[sha256] expected={expected} actual={actual} match={ok}", log_cb)
    return ok

def verify_sha256_ssh(archive_path: str, sha256_path: str, ssh_cfg: Dict, log_cb: TarLogCb = None) -> bool:
    method = str(ssh_cfg.get("transfer_method", "sftp")).lower()
    if method == "scp":
        rc, out1 = ssh_exec_subprocess(f"cat {shlex.quote(sha256_path)}", ssh_cfg, log_cb)
        if rc != 0 or not out1.strip():
            raise RuntimeError("Cannot read .sha256 on remote (ssh subprocess)")
        line = out1.strip()
        if " = " in line:
            expected = line.split(" = ")[-1].strip()
        else:
            parts = line.split()
            expected = parts[-1].strip() if parts else ""
        rc, out2 = ssh_exec_subprocess(f"sha256 {shlex.quote(archive_path)}", ssh_cfg, log_cb)
        if rc != 0:
            raise RuntimeError("sha256 command failed on remote (ssh subprocess)")
        actual_line = out2.strip()
        actual = (actual_line.split(" = ")[-1].strip() if " = " in actual_line else actual_line.split()[-1].strip())
        ok = (expected.lower() == actual.lower())
        _log(f"[sha256] remote match (subproc)={ok}", log_cb)
        return ok

    runner = SSHRunner(ssh_cfg, log_cb)
    try:
        runner.connect()
        rc, out, err = runner.exec(f"cat {shlex.quote(sha256_path)}", use_sudo=False)
        if rc != 0 or not out.strip():
            raise RuntimeError("Cannot read .sha256 on remote")
        line = out.strip()
        if " = " in line:
            expected = line.split(" = ")[-1].strip()
        else:
            parts = line.split()
            expected = parts[-1].strip() if parts else ""
        rc, out, err = runner.exec(f"sha256 {shlex.quote(archive_path)}", use_sudo=False)
        if rc != 0:
            raise RuntimeError("sha256 command failed on remote\n" + err)
        actual_line = out.strip()
        actual = (actual_line.split(" = ")[-1].strip() if " = " in actual_line else actual_line.split()[-1].strip())
        ok = (expected.lower() == actual.lower())
        _log(f"[sha256] remote match={ok}", log_cb)
        return ok
    finally:
        runner.close()

# --- Restore over SSH ---

def run_restore_ssh(archive_path: str, restore_parent: str, ssh_cfg: Dict,
                    log_cb: TarLogCb = None, keep_old_files: bool = False):
    method = str(ssh_cfg.get("transfer_method", "sftp")).lower()
    tar_bin = which_tar()

    if method == "scp":
        # まずは標準の --zstd パスを試す（成功なら終了）
        cmd_std = [tar_bin, "-x", "-p", "--acls", "--xattrs", "--fflags", "--zstd",
                   "-f", shlex.quote(archive_path), "-C", shlex.quote(restore_parent)]
        if keep_old_files:
            cmd_std.insert(1, "--keep-old-files")

        rc, out = ssh_exec_subprocess(" ".join(cmd_std), ssh_cfg, log_cb)
        if rc == 0:
            _log("[done] remote restore complete (ssh subprocess)", log_cb)
            return

        # ★ フォールバック：外部 zstd で伸張 → tar -x -f - にパイプ
        # （libarchive/bsdtar の ZSTD 取り込み問題を回避）
        fb = (
            f"zstd -d --stdout {shlex.quote(archive_path)} | "
            f"{tar_bin} -x -p --acls --xattrs --fflags "
            f"{'--keep-old-files ' if keep_old_files else ''}"
            f"-f - -C {shlex.quote(restore_parent)}"
        )
        rc, out = ssh_exec_subprocess(fb, ssh_cfg, log_cb)
        if rc != 0:
            raise RuntimeError("Remote tar restore failed (fallback)\n" + (out or ""))
        _log("[done] remote restore complete (ssh subprocess, fallback zstd)", log_cb)
        return

    runner = SSHRunner(ssh_cfg, log_cb)
    try:
        runner.connect()
        # 標準パス
        cmd_list = [which_tar(), "-x", "-p", "--acls", "--xattrs", "--fflags", "--zstd",
                    "-f", archive_path, "-C", restore_parent]
        if keep_old_files:
            cmd_list.insert(1, "--keep-old-files")

        rc, out, err = runner.exec(to_shell(cmd_list), use_sudo=bool(ssh_cfg.get("use_sudo", False)))
        if rc == 0:
            _log("[done] remote restore complete", log_cb)
            return

        # ★ フォールバック：外部 zstd で伸張してパイプ
        fb = (
            f"zstd -d --stdout {shlex.quote(archive_path)} | "
            f"{which_tar()} -x -p --acls --xattrs --fflags "
            f"{'--keep-old-files ' if keep_old_files else ''}"
            f"-f - -C {shlex.quote(restore_parent)}"
        )
        rc, out, err = runner.exec(fb, use_sudo=bool(ssh_cfg.get("use_sudo", False)))
        if rc != 0:
            raise RuntimeError("Remote tar restore failed (fallback)\n" + err)
        _log("[done] remote restore complete (fallback zstd)", log_cb)
    finally:
        runner.close()

# --- Health check (remote) ---
def health_check_remote(dest_dir: str, ssh_cfg: Dict, require_mounted: bool = False, min_free_gb: int = 0, log_cb: TarLogCb = None):
    method = str(ssh_cfg.get("transfer_method", "sftp")).lower()
    if method == "scp":
        rc, out = ssh_exec_subprocess(f"test -d {shlex.quote(dest_dir)} || mkdir -p {shlex.quote(dest_dir)}",
                                      ssh_cfg, log_cb)
        if rc != 0:
            raise RuntimeError("[health] cannot create or access dest dir (ssh subprocess): " + dest_dir)
        rc, out = ssh_exec_subprocess(f"test -w {shlex.quote(dest_dir)}", ssh_cfg, log_cb)
        if rc != 0:
            raise RuntimeError("[health] dest not writable: " + dest_dir)
        if require_mounted:
            rc, out = ssh_exec_subprocess("mount", ssh_cfg, log_cb)
            if rc != 0 or dest_dir not in out:
                raise RuntimeError("[health] dest not mounted: " + dest_dir)
        if min_free_gb and min_free_gb > 0:
            rc, out = ssh_exec_subprocess(f"df -k {shlex.quote(dest_dir)}", ssh_cfg, log_cb)
            if rc != 0 or not out:
                raise RuntimeError("[health] df failed for: " + dest_dir)
            lines = [ln for ln in out.splitlines() if ln.strip()]
            last = lines[-1]
            toks = last.split()
            try:
                avail_kb = int(toks[3])
            except Exception:
                raise RuntimeError("[health] cannot parse df output: " + last)
            avail_gb = avail_kb / (1024*1024)
            _log(f"[health] free space: {avail_gb:.2f} GB", log_cb)
            if avail_gb < float(min_free_gb):
                raise RuntimeError(f"[health] insufficient free space: {avail_gb:.2f} GB < {min_free_gb} GB")
        _log("[health] OK (ssh subprocess)", log_cb)
        return

    runner = SSHRunner(ssh_cfg, log_cb)
    try:
        runner.connect()
        rc, out, err = runner.exec(f"test -d {shlex.quote(dest_dir)} || mkdir -p {shlex.quote(dest_dir)}", use_sudo=False)
        if rc != 0:
            raise RuntimeError("[health] cannot create or access dest dir: " + dest_dir)
        rc, out, err = runner.exec(f"test -w {shlex.quote(dest_dir)}", use_sudo=False)
        if rc != 0:
            raise RuntimeError("[health] dest not writable: " + dest_dir)
        if require_mounted:
            rc, out, err = runner.exec("mount", use_sudo=False)
            if dest_dir not in out:
                raise RuntimeError("[health] dest not mounted: " + dest_dir)
        if min_free_gb and min_free_gb > 0:
            rc, out, err = runner.exec(f"df -k {shlex.quote(dest_dir)}", use_sudo=False)
            if rc != 0 or not out:
                raise RuntimeError("[health] df failed for: " + dest_dir)
            lines = [ln for ln in out.splitlines() if ln.strip()]
            last = lines[-1]
            toks = last.split()
            try:
                avail_kb = int(toks[3])
            except Exception:
                raise RuntimeError("[health] cannot parse df output: " + last)
            avail_gb = avail_kb / (1024*1024)
            _log(f"[health] free space: {avail_gb:.2f} GB", log_cb)
            if avail_gb < float(min_free_gb):
                raise RuntimeError(f"[health] insufficient free space: {avail_gb:.2f} GB < {min_free_gb} GB")
        _log("[health] OK", log_cb)
    finally:
        runner.close()

# --- Retention (remote/local) ---
def _is_epms_zst(name: str) -> bool:
    """Retention 用（安全のため epms-* のみ対象）。"""
    return name.startswith('epms-') and name.endswith('.tar.zst')

def _is_zst(name: str) -> bool:
    """一覧表示用：拡張子が .tar.zst のものをすべて対象。"""
    return name.endswith('.tar.zst')

def manage_remote_retention(dest_dir: str, ssh_cfg: Dict, keep_n: int = 0, keep_days: int = 0, log_cb: TarLogCb = None):
    method = str(ssh_cfg.get("transfer_method", "sftp")).lower()
    if method == "scp":
        list_cmd = (
            f'for f in {shlex.quote(dest_dir)}/epms-*.tar.zst; do '
            f' [ -f "$f" ] && stat -f "%N\n%m" "$f"; '
            f'done'
        )
        rc, out = ssh_exec_subprocess(list_cmd, ssh_cfg, log_cb)
        if rc != 0:
            _log("[retention] remote dest not found or list failed; skip", log_cb)
            return
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        if not lines:
            _log("[retention] no archives found, skip", log_cb)
            return
        entries = []
        # name, mtime の2行セット
        for i in range(0, len(lines), 2):
            try:
                name = lines[i]
                mt = float(lines[i+1])
                entries.append((os.path.basename(name), mt))
            except Exception:
                continue
        if not entries:
            _log("[retention] parse failed; skip", log_cb)
            return
        entries.sort(key=lambda x: x[1], reverse=True)
        cutoff_time = None
        if keep_days and keep_days > 0:
            cutoff_time = (datetime.now() - timedelta(days=keep_days)).timestamp()
        to_delete = []
        for idx, (fname, mt) in enumerate(entries):
            del_flag = False
            if keep_n and keep_n > 0 and idx >= keep_n:
                del_flag = True
            if cutoff_time and mt < cutoff_time:
                del_flag = True
            if del_flag:
                to_delete.append(fname)
        for fname in to_delete:
            ap = dest_dir.rstrip('/') + '/' + fname
            sha = ap + '.sha256'
            lst = ap + '.list'
            _log(f"[retention] remote delete {ap}", log_cb)
            ssh_exec_subprocess(f"rm -f {shlex.quote(ap)} {shlex.quote(sha)} {shlex.quote(lst)}", ssh_cfg, log_cb)
        _log(f"[retention] remote kept={len(entries)-len(to_delete)} deleted={len(to_delete)}", log_cb)
        return

    runner = SSHRunner(ssh_cfg, log_cb)
    try:
        runner.connect()
        sftp = runner.sftp()
        try:
            entries = sftp.listdir_attr(dest_dir)
        except FileNotFoundError:
            _log("[retention] remote dest not found, skip", log_cb)
            return
        zsts = [e for e in entries if _is_epms_zst(e.filename)]
        if not zsts:
            _log("[retention] no archives found, skip", log_cb)
            return
        zsts.sort(key=lambda e: e.st_mtime, reverse=True)
        cutoff_time = None
        if keep_days and keep_days > 0:
            cutoff_time = (datetime.now() - timedelta(days=keep_days)).timestamp()
        to_delete = []
        for idx, e in enumerate(zsts):
            del_flag = False
            if keep_n and keep_n > 0 and idx >= keep_n:
                del_flag = True
            if cutoff_time and e.st_mtime < cutoff_time:
                del_flag = True
            if del_flag:
                to_delete.append(e.filename)
        for fname in to_delete:
            ap = dest_dir.rstrip('/') + '/' + fname
            sha = ap + '.sha256'
            lst = ap + '.list'
            _log(f"[retention] remote delete {ap}", log_cb)
            try:
                sftp.remove(ap)
            except FileNotFoundError:
                pass
            for aux in [sha, lst]:
                try:
                    sftp.remove(aux)
                except FileNotFoundError:
                    pass
        _log(f"[retention] remote kept={len(zsts)-len(to_delete)} deleted={len(to_delete)}", log_cb)
    finally:
        runner.close()

def manage_local_retention(local_dir: str, keep_n: int = 0, keep_days: int = 0, log_cb: TarLogCb = None):
    if not os.path.isdir(local_dir):
        _log("[retention] local dir not found, skip", log_cb)
        return
    names = [n for n in os.listdir(local_dir) if _is_epms_zst(n)]
    if not names:
        _log("[retention] no local archives, skip", log_cb)
        return
    zsts = []
    for n in names:
        p = os.path.join(local_dir, n)
        try:
            st = os.stat(p)
            zsts.append((n, st.st_mtime))
        except FileNotFoundError:
            continue
    zsts.sort(key=lambda x: x[1], reverse=True)
    cutoff_time = None
    if keep_days and keep_days > 0:
        cutoff_time = (datetime.now() - timedelta(days=keep_days)).timestamp()
    to_delete = []
    for idx, (n, mt) in enumerate(zsts):
        del_flag = False
        if keep_n and keep_n > 0 and idx >= keep_n:
            del_flag = True
        if cutoff_time and mt < cutoff_time:
            del_flag = True
        if del_flag:
            to_delete.append(n)
    for n in to_delete:
        ap = os.path.join(local_dir, n)
        sha = ap + '.sha256'
        lst = ap + '.list'
        _log(f"[retention] local delete {ap}", log_cb)
        try:
            os.remove(ap)
        except FileNotFoundError:
            pass
        for aux in [sha, lst]:
            try:
                os.remove(aux)
            except FileNotFoundError:
                pass
    _log(f"[retention] local kept={len(zsts)-len(to_delete)} deleted={len(to_delete)}", log_cb)

# --- Remote latest finder & lists ---
def list_remote_archives_for_source(source_dir: str, ssh_cfg: Dict, log_cb: TarLogCb = None) -> List[Dict]:
    dest_dir = default_remote_dest_dir(source_dir)
    method = str(ssh_cfg.get("transfer_method", "sftp")).lower()

    if method == "scp":
        list_cmd = (
            f'for f in {shlex.quote(dest_dir)}/*.tar.zst; do '
            f' [ -f "$f" ] && stat -f "%N\n%m\n%z" "$f"; '
            f'done'
        )
        rc, out = ssh_exec_subprocess(list_cmd, ssh_cfg, log_cb)
        if rc != 0:
            raise FileNotFoundError(f"Remote directory not found or list failed: {dest_dir}")
        lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
        rows = []
        # 3行セット：name, mtime, size
        for i in range(0, len(lines), 3):
            try:
                name = lines[i]
                mt = float(lines[i+1])
                size = int(lines[i+2])
                base = os.path.basename(name)
                if not _is_zst(base):
                    continue
                ap = dest_dir.rstrip('/') + '/' + base
                sha = ap + ".sha256"
                lst = ap + ".list"
                rc_sha, _ = ssh_exec_subprocess(f"test -f {shlex.quote(sha)} && echo Y || echo N", ssh_cfg, None)
                rc_lst, _ = ssh_exec_subprocess(f"test -f {shlex.quote(lst)} && echo Y || echo N", ssh_cfg, None)
                rows.append({
                    "path": ap,
                    "name": base,
                    "size": size,
                    "mtime": mt,
                    "sha_exists": (rc_sha == 0),
                    "list_exists": (rc_lst == 0)
                })
            except Exception:
                continue
        rows.sort(key=lambda r: r["mtime"], reverse=True)
        _log(f"[remote] {len(rows)} archives listed (ssh subprocess)", log_cb)
        return rows

    # SFTP
    runner = SSHRunner(ssh_cfg, log_cb)
    try:
        runner.connect()
        sftp = runner.sftp()
        try:
            entries = sftp.listdir_attr(dest_dir)
        except FileNotFoundError:
            raise FileNotFoundError(f"Remote directory not found: {dest_dir}")
        rows = []
        for e in entries:
            if not _is_zst(e.filename):
                continue
            ap = dest_dir.rstrip('/') + '/' + e.filename
            sha = ap + ".sha256"
            lst = ap + ".list"
            sha_exists = True
            lst_exists = True
            try:
                sftp.stat(sha)
            except FileNotFoundError:
                sha_exists = False
            try:
                sftp.stat(lst)
            except FileNotFoundError:
                lst_exists = False
            rows.append({
                "path": ap,
                "name": e.filename,
                "size": getattr(e, "st_size", 0),
                "mtime": e.st_mtime,
                "sha_exists": sha_exists,
                "list_exists": lst_exists
            })
        rows.sort(key=lambda r: r["mtime"], reverse=True)
        _log(f"[remote] {len(rows)} archives listed", log_cb)
        return rows
    finally:
        runner.close()

def find_latest_remote_archive_auto_by_source(source_dir: str, ssh_cfg: Dict, log_cb: TarLogCb = None) -> Tuple[str, Optional[str], Optional[str]]:
    rows = list_remote_archives_for_source(source_dir, ssh_cfg, log_cb)
    if not rows:
        raise FileNotFoundError("No .tar.zst found in remote backups directory")
    r0 = rows[0]
    ap = r0["path"]
    sha = ap + ".sha256" if r0.get("sha_exists") else None
    lst = ap + ".list" if r0.get("list_exists") else None
    _log(f"[ssh] latest archive: {ap}", log_cb)
    return ap, sha, lst

# --- SFTP download/upload ---
def sftp_download(remote_path: str, local_path: str, ssh_cfg: Dict, log_cb: TarLogCb = None):
    runner = SSHRunner(ssh_cfg, log_cb)
    try:
        runner.connect()
        sftp = runner.sftp()
        local_dir = os.path.dirname(local_path)
        if local_dir and not os.path.isdir(local_dir):
            os.makedirs(local_dir, exist_ok=True)
        _log(f"[download] {remote_path} -> {local_path}", log_cb)
        with sftp.open(remote_path, 'rb') as rf, open(local_path, 'wb') as lf:
            total = 0
            while True:
                # 既存: data = rf.read(1024*64)  # 64KB
                # 修正:
                chunk = int(ssh_cfg.get("sftp_chunk_kb", 512)) * 1024  # 512KB既定
                data = rf.read(chunk)
                if not data:
                    break
                lf.write(data)
                total += len(data)
                if log_cb:
                    log_cb(f"[download] {total} bytes")
        _log("[download] done", log_cb)
    finally:
        runner.close()

def sftp_upload(local_path: str, remote_path: str, ssh_cfg: Dict, log_cb: TarLogCb = None):
    runner = SSHRunner(ssh_cfg, log_cb)
    try:
        runner.connect()
        remote_dir = os.path.dirname(remote_path)
        # ★ mkdir -p だけにする
        rc, out, err = runner.exec(
            f"mkdir -p {shlex.quote(remote_dir)}",
            use_sudo=False)
        if rc != 0:
            raise RuntimeError("Failed to ensure remote directory\n" + err)
        sftp = runner.sftp()
        _log(f"[upload] {local_path} -> {remote_path}", log_cb)
        with open(local_path, 'rb') as lf, sftp.open(remote_path, 'wb') as rf:
            total = 0
            chunk = int(ssh_cfg.get("sftp_chunk_kb", 512)) * 1024  # 既定 512KB
            while True:
                data = lf.read(chunk)             # ← ローカルから読み
                if not data:
                    break
                rf.write(data)                     # ← リモートへ書き
                total += len(data)
            rf.flush()                             # 念のため
        if log_cb:
            log_cb(f"[upload] {total} bytes")
            _log("[upload] done", log_cb)
    finally:
        runner.close()

# --- Local latest & list ---
def find_latest_local_archive(local_dir: str, log_cb: TarLogCb = None) -> Tuple[str, Optional[str], Optional[str]]:
    if not os.path.isdir(local_dir):
        raise FileNotFoundError(f"Local directory not found: {local_dir}")
    names = [n for n in os.listdir(local_dir) if _is_zst(n)]
    if not names:
        raise FileNotFoundError("No .tar.zst found in local directory")
    latest_name = max(names, key=lambda n: os.stat(os.path.join(local_dir, n)).st_mtime)
    arch = os.path.join(local_dir, latest_name)
    sha = arch + ".sha256" if os.path.isfile(arch + ".sha256") else None
    lst = arch + ".list" if os.path.isfile(arch + ".list") else None
    _log(f"[local] latest archive: {arch}", log_cb)
    return arch, sha, lst

def list_local_archives(local_dir: str, log_cb: TarLogCb = None) -> List[Dict]:
    if not os.path.isdir(local_dir):
        raise FileNotFoundError(f"Local directory not found: {local_dir}")
    rows = []
    for n in os.listdir(local_dir):
        if not _is_zst(n):
            continue
        p = os.path.join(local_dir, n)
        try:
            st = os.stat(p)
        except FileNotFoundError:
            continue
        rows.append({
            "path": p,
            "name": n,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "sha_exists": os.path.isfile(p + ".sha256"),
            "list_exists": os.path.isfile(p + ".list")
        })
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    _log(f"[local] {len(rows)} archives listed", log_cb)
    return rows

# --- Windows -> FreeBSD restore (upload to tmp, verify, restore) ---
def copy_specific_paths_to_windows(
    archive_path: str,
    sha_path: Optional[str],
    list_path: Optional[str],
    local_dir: str,
    ssh_cfg: Dict,
    log_cb: TarLogCb = None,
    transfer_method: str = "sftp"  # "sftp" or "scp"
) -> Tuple[str, Optional[str], Optional[str]]:
    base = os.path.basename(archive_path)
    local_arch = os.path.join(local_dir, base)
    if transfer_method.lower() == "scp":
        scp_download(archive_path, local_arch, ssh_cfg, log_cb)
    else:
        sftp_download(archive_path, local_arch, ssh_cfg, log_cb)
    local_sha = None
    local_lst = None
    if sha_path:
        local_sha = os.path.join(local_dir, os.path.basename(sha_path))
        if transfer_method.lower() == "scp":
            scp_download(sha_path, local_sha, ssh_cfg, log_cb)
        else:
            sftp_download(sha_path, local_sha, ssh_cfg, log_cb)
    if list_path:
        local_lst = os.path.join(local_dir, os.path.basename(list_path))
        if transfer_method.lower() == "scp":
            scp_download(list_path, local_lst, ssh_cfg, log_cb)
        else:
            sftp_download(list_path, local_lst, ssh_cfg, log_cb)
    return local_arch, local_sha, local_lst

def restore_latest_from_windows(
    local_dir: str,
    remote_restore_parent: str,
    ssh_cfg: Dict,
    verify_before: bool = True,
    cleanup_remote: bool = True,
    log_cb: TarLogCb = None,
    remote_tmp_dir: Optional[str] = None,
    keep_old_files: bool = False,
    transfer_method: str = "sftp"
) -> str:
    local_arch, local_sha, local_lst = find_latest_local_archive(local_dir, log_cb)
    if verify_before and local_sha:
        ok = verify_sha256(local_arch, local_sha, log_cb)
        if not ok:
            raise RuntimeError("[restore] local sha256 mismatch; aborting")
    if not remote_tmp_dir:
        remote_tmp_dir = remote_restore_parent.rstrip('/') + '/.restore_tmp'
    base = os.path.basename(local_arch)
    remote_arch = remote_tmp_dir.rstrip('/') + '/' + base
    remote_sha = remote_arch + ".sha256" if local_sha else None
    remote_lst = remote_arch + ".list" if local_lst else None
    if transfer_method.lower() == "scp":
        scp_upload(local_arch, remote_arch, ssh_cfg, log_cb)
    else:
        sftp_upload(local_arch, remote_arch, ssh_cfg, log_cb)
    if local_sha:
        if transfer_method.lower() == "scp":
            scp_upload(local_sha, remote_sha, ssh_cfg, log_cb)
        else:
            sftp_upload(local_sha, remote_sha, ssh_cfg, log_cb)
    if local_lst:
        if transfer_method.lower() == "scp":
            scp_upload(local_lst, remote_lst, ssh_cfg, log_cb)
        else:
            sftp_upload(local_lst, remote_lst, ssh_cfg, log_cb)
    if remote_sha:
        ok_remote = verify_sha256_ssh(remote_arch, remote_sha, ssh_cfg, log_cb)
        if not ok_remote:
            raise RuntimeError("[restore] remote sha256 mismatch after upload; aborting")
    run_restore_ssh(remote_arch, remote_restore_parent, ssh_cfg, log_cb=log_cb, keep_old_files=keep_old_files)
    _log(f"[restore] restored {base} to {remote_restore_parent}", log_cb)
    if cleanup_remote:
        method = str(ssh_cfg.get("transfer_method", "sftp")).lower()
        if method == "scp":
            for rp in [remote_arch, remote_sha, remote_lst]:
                if not rp:
                    continue
                _log(f"[cleanup] remove {rp}", log_cb)
                ssh_exec_subprocess(f"rm -f {shlex.quote(rp)}", ssh_cfg, log_cb)
        else:
            runner = SSHRunner(ssh_cfg, log_cb)
            try:
                runner.connect()
                sftp = runner.sftp()
                for rp in [remote_arch, remote_sha, remote_lst]:
                    if not rp:
                        continue
                    _log(f"[cleanup] remove {rp}", log_cb)
                    try:
                        sftp.remove(rp)
                    except FileNotFoundError:
                        pass
            finally:
                runner.close()
    return remote_restore_parent

def restore_specific_local_to_remote(
    local_arch: str,
    remote_restore_parent: str,
    ssh_cfg: Dict,
    verify_before: bool = True,
    cleanup_remote: bool = True,
    keep_old_files: bool = False,
    log_cb: TarLogCb = None,
    remote_tmp_dir: Optional[str] = None,
    transfer_method: str = "sftp"
) -> str:
    if not os.path.isfile(local_arch):
        raise FileNotFoundError(f"Local archive not found: {local_arch}")
    local_sha = local_arch + ".sha256" if os.path.isfile(local_arch + ".sha256") else None
    local_lst = local_arch + ".list" if os.path.isfile(local_arch + ".list") else None
    if verify_before and local_sha:
        ok = verify_sha256(local_arch, local_sha, log_cb)
        if not ok:
            raise RuntimeError("[restore] local sha256 mismatch; aborting")
    if not remote_tmp_dir:
        remote_tmp_dir = remote_restore_parent.rstrip('/') + '/.restore_tmp'
    base = os.path.basename(local_arch)
    remote_arch = remote_tmp_dir.rstrip('/') + '/' + base
    remote_sha = remote_arch + ".sha256" if local_sha else None
    remote_lst = remote_arch + ".list" if local_lst else None
    if transfer_method.lower() == "scp":
        scp_upload(local_arch, remote_arch, ssh_cfg, log_cb)
    else:
        sftp_upload(local_arch, remote_arch, ssh_cfg, log_cb)
    if local_sha:
        if transfer_method.lower() == "scp":
            scp_upload(local_sha, remote_sha, ssh_cfg, log_cb)
        else:
            sftp_upload(local_sha, remote_sha, ssh_cfg, log_cb)
    if local_lst:
        if transfer_method.lower() == "scp":
            scp_upload(local_lst, remote_lst, ssh_cfg, log_cb)
        else:
            sftp_upload(local_lst, remote_lst, ssh_cfg, log_cb)
    if remote_sha:
        ok_remote = verify_sha256_ssh(remote_arch, remote_sha, ssh_cfg, log_cb)
        if not ok_remote:
            raise RuntimeError("[restore] remote sha256 mismatch after upload; aborting")
    run_restore_ssh(remote_arch, remote_restore_parent, ssh_cfg, log_cb=log_cb, keep_old_files=keep_old_files)
    _log(f"[restore] restored {base} to {remote_restore_parent}", log_cb)
    if cleanup_remote:
        method = str(ssh_cfg.get("transfer_method", "sftp")).lower()
        if method == "scp":
            for rp in [remote_arch, remote_sha, remote_lst]:
                if not rp:
                    continue
                _log(f"[cleanup] remove {rp}", log_cb)
                ssh_exec_subprocess(f"rm -f {shlex.quote(rp)}", ssh_cfg, log_cb)
        else:
            runner = SSHRunner(ssh_cfg, log_cb)
            try:
                runner.connect()
                sftp = runner.sftp()
                for rp in [remote_arch, remote_sha, remote_lst]:
                    if not rp:
                        continue
                    _log(f"[cleanup] remove {rp}", log_cb)
                    try:
                        sftp.remove(rp)
                    except FileNotFoundError:
                        pass
            finally:
                runner.close()
    return remote_restore_parent

# --- tar_ops.py に追記（ファイル末尾あたりに置くのが分かりやすいです） ---
def _detect_remote_sha_cmd(ssh_cfg: Dict, log_cb: TarLogCb = None) -> str:
    """
    リモート環境で利用可能な SHA256 コマンドを検出（FreeBSD系: sha256 / Linux系: sha256sum）。
    返り値: 'sha256' または 'sha256sum'
    """
    # sha256sum があればそれを優先（Linux）
    rc, _ = ssh_exec_subprocess("command -v sha256sum >/dev/null 2>&1", ssh_cfg, log_cb)
    if rc == 0:
        return "sha256sum"
    # 無ければ FreeBSD 標準の sha256 を試す
    rc, _ = ssh_exec_subprocess("command -v sha256 >/dev/null 2>&1", ssh_cfg, log_cb)
    if rc == 0:
        return "sha256"
    raise RuntimeError("[sha256] neither 'sha256sum' nor 'sha256' is available on remote")



def run_backup_pipeline_to_windows(
    source_dir: str,
    local_dir: str,
    ssh_cfg: Dict,
    compression_level: int = 4,
    threads: int = 0,
    excludes: Optional[List[str]] = None,
    one_file_system: bool = True,
    create_remote_copy: bool = True,
    make_remote_list_and_sha: bool = True,
    make_remote_sha: bool = False,  # list不要だがshaだけ作りたい場合
    make_local_list: bool = False,
    log_cb: TarLogCb = None,
    remote_dest_dir: Optional[str] = None,
) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
    """
    リモートで `tar -c --zstd -f -` を実行し、その標準出力を SSH 経由で
    Windowsに .part で保存 → 完了後に最終名へ rename。
    create_remote_copy=True の場合は `tee` で同じストリームをリモートにも保存。
    必要に応じて、リモート/ローカルで .list / .sha256 を生成する。
    返り値: (local_arch, local_sha or None, remote_arch or None, remote_sha or None)
    """
    user = ssh_cfg.get("user") or ""
    host = ssh_cfg.get("host") or ""
    if not user or not host:
        raise RuntimeError("[pipeline] ssh user/host is required")

    # 1) 保存先パスを決定
    if remote_dest_dir is None:
        remote_dest_dir = default_remote_dest_dir(source_dir)
    ts = timestamp()
    base = f"epms-{ts}.tar.zst"
    remote_arch = os.path.join(remote_dest_dir.rstrip("/"), base) if create_remote_copy else None
    remote_part = (remote_arch + ".part") if create_remote_copy else None
    remote_sha = (remote_arch + ".sha256") if (create_remote_copy and (make_remote_list_and_sha or make_remote_sha)) else None
    remote_list = (remote_arch + ".list") if (create_remote_copy and make_remote_list_and_sha) else None

    os.makedirs(local_dir, exist_ok=True)
    local_arch = os.path.join(local_dir, base)
    local_part = local_arch + ".part"
    local_sha = local_arch + ".sha256"  # 作成に成功した場合のみ返す
    local_list = local_arch + ".list"   # make_local_list=True の場合のみ作成

    # 2) リモート tar コマンドを構築（stdoutへ出す前提）
    tar_bin = which_tar()
    zstd_opts = _build_zstd_options(compression_level, threads, ssh_cfg, log_cb)
    tar_cmd = [
        tar_bin, "-c", "--acls", "--xattrs", "--fflags", "--zstd",
        "--options", zstd_opts,
    ]
    if one_file_system:
        tar_cmd.append("--one-file-system")
    if excludes:
        for pat in excludes:
            if pat:
                tar_cmd.extend(["--exclude", pat])

    # -f - で stdout へ、-C parent basename の形でソースを与える
    src_parent = os.path.dirname(source_dir.rstrip("/"))
    src_base = os.path.basename(source_dir.rstrip("/"))
    tar_cmd.extend(["-f", "-", "-C", src_parent, src_base])

    # 3) リモートで tee しつつ stdout に流すか、stdout のみにするか
    #    （/bin/sh で pipefail が利かない環境もあるため、完了後の検証で検出）
    if create_remote_copy:
        # ensure remote dest dir
        rc, out = ssh_exec_subprocess(f"mkdir -p {shlex.quote(remote_dest_dir)}", ssh_cfg, log_cb)
        if rc != 0:
            raise RuntimeError("[pipeline] failed to ensure remote dest dir\n" + (out or ""))
        remote_shell = f"{to_shell(tar_cmd)} | tee {shlex.quote(remote_part)}"
    else:
        remote_shell = to_shell(tar_cmd)

    # 4) Windows側で stdout を受け取り、local_part へバイナリ書き込み
    ssh_bin = _which_ssh()
    if not ssh_bin:
        raise RuntimeError("[pipeline] OpenSSH (ssh) client not found")
    full = [ssh_bin] + _build_ssh_common_args(ssh_cfg) + [f"{user}@{host}", remote_shell]
    full = _wrap_with_sshpass_or_pscp(full, ssh_cfg)

    _log(f"[pipeline] $ {' '.join(shlex.quote(c) for c in full)}", log_cb)

    with open(local_part, "wb") as lf:
        # ★ stderr は PIPE に分離（STDOUT へ合流しない）
        proc = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stderr_chunks = []

        def _drain_stderr():
            for chunk in iter(lambda: proc.stderr.read(64 * 1024), b""):
                stderr_chunks.append(chunk)

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()
        total = 0
        while True:
            chunk = proc.stdout.read(64 * 1024)
            if not chunk:
                break
            lf.write(chunk)
            total += len(chunk)
            if log_cb and total % (10 * 1024 * 1024) == 0:
                log_cb(f"[pipeline] received {total} bytes")
        rc = proc.wait()
        stderr_thread.join(timeout=5)
        # ★ 失敗時の stderr をログ出力
        if rc != 0:
            err = b"".join(stderr_chunks).decode("utf-8", errors="ignore")
            _log(f"[pipeline] ssh failed (rc={rc})\n{err}", log_cb)
            try: os.remove(local_part)
            except Exception: pass
            raise RuntimeError("[pipeline] ssh pipeline failed (non-zero exit)")

    # 5) local_part → local_arch へ確定
    os.replace(local_part, local_arch)
    _log(f"[pipeline] local saved: {local_arch}", log_cb)

    # 6) ローカル .sha256 / .list の生成
    try:
        with open(local_sha, "w", encoding="utf-8") as outf:
            outf.write(calc_sha256(local_arch) + "\n")
        _log(f"[pipeline] local sha256: {local_sha}", log_cb)
    except Exception as e:
        _log(f"[pipeline] local sha256 failed: {e}", log_cb)
        local_sha = None

    if make_local_list:
        try:
            lst_cmd = [which_tar(), "-t", "-f", local_arch]
            _log(f"[tar] writing local listing: {local_list}", log_cb)
            with open(local_list, "wb") as outf:
                subprocess.check_call(lst_cmd, stdout=outf)
            _log(f"[pipeline] local list: {local_list}", log_cb)
        except Exception as e:
            _log(f"[pipeline] local list failed: {e}", log_cb)

    # 7) リモート側の確定（create_remote_copy=True のときのみ）
    if create_remote_copy:
        # .part が存在し、サイズ > 0 かを先に確認（サイズ0は致命扱い）
        size_cmd = f"(stat -f %z {shlex.quote(remote_part)} 2>/dev/null || wc -c {shlex.quote(remote_part)})"
        rc, out = ssh_exec_subprocess(size_cmd, ssh_cfg, log_cb)
        if rc != 0 or not out.strip() or out.strip() == "0":
            raise RuntimeError("[pipeline] remote .part size is zero or not found")

        # ★ A適用：.part の tar -t 検査は “待機→1回リトライ→警告化”
        rc, out = ssh_exec_subprocess(
            f"tar -t -f {shlex.quote(remote_part)} >/dev/null",
            ssh_cfg, log_cb
        )
        if rc != 0:
            # 1秒だけ待って再試行
            ssh_exec_subprocess("sleep 1", ssh_cfg, log_cb)
            rc2, out2 = ssh_exec_subprocess(
                f"tar -t -f {shlex.quote(remote_part)} >/dev/null",
                ssh_cfg, log_cb
            )
            if rc2 != 0:
                _log(f"[pipeline] warn: remote 'tar -t' failed on .part (continuing). rc={rc2}\n{out2 or ''}", log_cb)

        # .part → 最終名へリネーム
        rc, out = ssh_exec_subprocess(
            f"mv -f {shlex.quote(remote_part)} {shlex.quote(remote_arch)}",
            ssh_cfg, log_cb
        )
        if rc != 0:
            raise RuntimeError("[pipeline] failed to finalize remote archive" + (out or ""))

        # 必要なら .list / .sha を生成（これは厳格に失敗検出のまま）
        if make_remote_list_and_sha:
            rc, out = ssh_exec_subprocess(
                f"tar -t -f {shlex.quote(remote_arch)} > {shlex.quote(remote_list)}",
                ssh_cfg, log_cb
            )
            if rc != 0:
                raise RuntimeError("[pipeline] remote listing failed after finalize" + (out or ""))
            sha_cmd = _detect_remote_sha_cmd(ssh_cfg, log_cb)
            rc, out = ssh_exec_subprocess(
                f"{sha_cmd} {shlex.quote(remote_arch)} > {shlex.quote(remote_sha)}",
                ssh_cfg, log_cb
            )
            if rc != 0:
                raise RuntimeError("[pipeline] remote sha256 failed after finalize" + (out or ""))
            _log(f"[pipeline] remote saved: {remote_arch}", log_cb)
            _log(f"[pipeline] remote sha256: {remote_sha}", log_cb)
            _log(f"[pipeline] remote list: {remote_list}", log_cb)

        elif make_remote_sha:
            sha_cmd = _detect_remote_sha_cmd(ssh_cfg, log_cb)
            rc, out = ssh_exec_subprocess(
                f"{sha_cmd} {shlex.quote(remote_arch)} > {shlex.quote(remote_sha)}",
                ssh_cfg, log_cb
            )
            if rc != 0:
                raise RuntimeError("[pipeline] remote sha256 failed after finalize" + (out or ""))
            _log(f"[pipeline] remote saved: {remote_arch}", log_cb)
            _log(f"[pipeline] remote sha256: {remote_sha}", log_cb)

        else:
            # ★ A適用：最終ファイルの健全性チェックも “待機→1回リトライ→警告化”
            ssh_exec_subprocess("sleep 1", ssh_cfg, log_cb)
            rc3, out3 = ssh_exec_subprocess(
                f"tar -t -f {shlex.quote(remote_arch)} >/dev/null",
                ssh_cfg, log_cb
            )
            if rc3 != 0:
                _log(f"[pipeline] warn: remote archive check failed after finalize (continuing). rc={rc3}\n{out3 or ''}", log_cb)

    return (
        local_arch,
        (local_sha if local_sha and os.path.isfile(local_sha) else None),
        (remote_arch if create_remote_copy else None),
        (remote_sha if (create_remote_copy and remote_sha) else None),
    )
