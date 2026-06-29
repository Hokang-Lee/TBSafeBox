import os
import shutil
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from scripts.tar_ops import SSHRunner

try:
    import paramiko
except Exception:
    paramiko = None

class KeyWizard(ttk.Frame):
    def __init__(self, master, on_key_created, ssh_cfg_getter, **kwargs):
        super().__init__(master, **kwargs)
        self.on_key_created = on_key_created
        self.ssh_cfg_getter = ssh_cfg_getter
        self._build_ui()

    def _build_ui(self):
        row = 0
        ttk.Label(self, text="Key Type").grid(row=row, column=0, sticky="w")
        self.key_type = tk.StringVar(value="ed25519")
        ttk.Radiobutton(self, text="Ed25519 (ssh-keygen)", value="ed25519", variable=self.key_type).grid(row=row, column=1, sticky="w")
        ttk.Radiobutton(self, text="RSA (Paramiko)", value="rsa", variable=self.key_type).grid(row=row, column=2, sticky="w")
        row += 1

        ttk.Label(self, text="Output path (private key)").grid(row=row, column=0, sticky="w")
        self.ent_path = ttk.Entry(self, width=48)
        self.ent_path.insert(0, os.path.expanduser("~/id_ed25519"))
        self.ent_path.grid(row=row, column=1, columnspan=2, sticky="ew")
        ttk.Button(self, text="Browse", command=self._browse_path).grid(row=row, column=3, sticky="w")
        row += 1

        ttk.Label(self, text="Comment").grid(row=row, column=0, sticky="w")
        self.ent_comment = ttk.Entry(self, width=48)
        self.ent_comment.insert(0, "backup-key")
        self.ent_comment.grid(row=row, column=1, columnspan=2, sticky="ew")
        row += 1

        ttk.Label(self, text="Passphrase (optional)").grid(row=row, column=0, sticky="w")
        self.ent_pass = ttk.Entry(self, width=48, show="*")
        self.ent_pass.grid(row=row, column=1, columnspan=2, sticky="ew")
        row += 1

        self.var_register = tk.BooleanVar(value=True)
        ttk.Checkbutton(self, text="Register public key to remote (authorized_keys)", variable=self.var_register).grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1

        self.btn_gen = ttk.Button(self, text="Generate", command=self._generate)
        self.btn_gen.grid(row=row, column=0, padx=4)
        self.btn_close = ttk.Button(self, text="Close", command=self._close)
        self.btn_close.grid(row=row, column=1, padx=4)

        for c in range(4):
            self.columnconfigure(c, weight=1)

    def _browse_path(self):
        path = filedialog.asksaveasfilename(title="Choose private key path", initialfile=os.path.basename(self.ent_path.get()))
        if path:
            self.ent_path.delete(0, tk.END)
            self.ent_path.insert(0, path)

    def _generate(self):
        key_type = self.key_type.get()
        path = self.ent_path.get().strip()
        comment = self.ent_comment.get().strip()
        passphrase = self.ent_pass.get()
        if not path:
            messagebox.showerror("Keygen", "Output path is required")
            return
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)

        try:
            if key_type == "ed25519":
                self._generate_ed25519(path, comment, passphrase)
            else:
                self._generate_rsa(path, comment, passphrase)
        except Exception as e:
            messagebox.showerror("Keygen", f"Failed: {e}")
            return

        pub_path = path + ".pub"
        if self.var_register.get():
            try:
                self._register_remote(pub_path)
            except Exception as e:
                messagebox.showerror("Register", f"Remote registration failed: {e}")
        self.on_key_created(path)
        messagebox.showinfo("Keygen", f"Completed\nPrivate: {path}\nPublic:  {pub_path}")

    def _generate_ed25519(self, path, comment, passphrase):
        import subprocess
        if not shutil.which("ssh-keygen"):
            raise RuntimeError("ssh-keygen not found. Install Windows OpenSSH Client or use RSA option.")
        cmd = ["ssh-keygen", "-t", "ed25519", "-C", comment, "-f", path, "-N", passphrase if passphrase else ""]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out, err = p.communicate()
        if p.returncode != 0:
            raise RuntimeError(f"ssh-keygen failed: {err}")

    def _generate_rsa(self, path, comment, passphrase):
        if paramiko is None:
            raise RuntimeError("Paramiko not available. Install with: pip install paramiko")
        key = paramiko.RSAKey.generate(2048)
        key.write_private_key_file(path, password=(passphrase if passphrase else None))
        pub = f"{key.get_name()} {key.get_base64()} {comment}\n"
        with open(path + ".pub", "w", encoding="utf-8") as f:
            f.write(pub)

    def _register_remote(self, pub_path: str):
        if not os.path.isfile(pub_path):
            raise FileNotFoundError(pub_path)
        with open(pub_path, "r", encoding="utf-8") as f:
            pubkey = f.read().strip()
        cfg = self.ssh_cfg_getter()
        runner = SSHRunner(cfg, log_cb=lambda s: None)
        runner.connect()
        try:
            sftp = runner.sftp()
            try:
                sftp.stat(".ssh")
            except FileNotFoundError:
                runner.exec("mkdir -p ~/.ssh && chmod 700 ~/.ssh", use_sudo=False)
            try:
                ak = sftp.open(".ssh/authorized_keys", "a", 0o600)
            except IOError:
                ak = sftp.open(".ssh/authorized_keys", "w", 0o600)
            ak.write(pubkey + "\n")
            ak.close()
            runner.exec("chmod 600 ~/.ssh/authorized_keys", use_sudo=False)
        finally:
            runner.close()

    def _close(self):
        self.master.destroy()
