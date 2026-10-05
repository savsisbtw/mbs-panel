import base64
import glob
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
import datetime

from config import DB_PATH, BASE_DIR

ENV_PATH = os.path.join(BASE_DIR, ".env")
MAX_RESTORE_SIZE = 200 * 1024 * 1024
KEEP_SAFETY_COPIES = 5


class RestoreError(Exception):
    pass


def _prune_old_safety_copies(keep: int = KEEP_SAFETY_COPIES):
    for base in (DB_PATH, ENV_PATH):
        copies = sorted(glob.glob(f"{base}.before-restore-*"))
        for path in copies[:-keep] if keep > 0 else copies:
            try:
                os.remove(path)
            except OSError:
                pass


def create_backup() -> bytes:
    buf = io.BytesIO()
    db_tmp = DB_PATH + ".backup_snapshot.tmp"
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(db_tmp)
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()

    try:
        files = ["mbs.db"]
        if os.path.exists(ENV_PATH):
            files.append(".env")
        manifest = {
            "created_at": datetime.datetime.utcnow().isoformat(),
            "files": files,
        }
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(db_tmp, arcname="mbs.db")
            if os.path.exists(ENV_PATH):
                tar.add(ENV_PATH, arcname=".env")
            manifest_bytes = json.dumps(manifest, indent=2).encode()
            info = tarfile.TarInfo(name="manifest.json")
            info.size = len(manifest_bytes)
            tar.addfile(info, io.BytesIO(manifest_bytes))
    finally:
        try:
            os.remove(db_tmp)
        except OSError:
            pass
    return buf.getvalue()


def restore_backup(data: bytes) -> dict:
    if len(data) > MAX_RESTORE_SIZE:
        raise RestoreError("backup file too large")

    try:
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except Exception as e:
        raise RestoreError(f"not a valid backup archive: {e}")

    members = {m.name: m for m in tar.getmembers()}
    if "mbs.db" not in members:
        raise RestoreError("archive has no mbs.db")

    tmp_db_path = DB_PATH + ".restore_candidate.tmp"
    db_member = tar.extractfile(members["mbs.db"])
    with open(tmp_db_path, "wb") as f:
        shutil.copyfileobj(db_member, f)

    try:
        check_conn = sqlite3.connect(tmp_db_path)
        try:
            tables = {r[0] for r in check_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        finally:
            check_conn.close()
    except sqlite3.DatabaseError as e:
        os.remove(tmp_db_path)
        raise RestoreError(f"archive's mbs.db is not a valid sqlite database: {e}")

    required = {"users", "subscriptions", "nodes", "payments"}
    if not required.issubset(tables):
        os.remove(tmp_db_path)
        raise RestoreError("archive's mbs.db is missing expected tables")

    stamp = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    safety_copy = f"{DB_PATH}.before-restore-{stamp}"
    shutil.copy2(DB_PATH, safety_copy)

    restored_env = False
    if ".env" in members and os.path.exists(ENV_PATH):
        env_safety = f"{ENV_PATH}.before-restore-{stamp}"
        shutil.copy2(ENV_PATH, env_safety)
        env_member = tar.extractfile(members[".env"])
        env_tmp = ENV_PATH + ".restore.tmp"
        with open(env_tmp, "wb") as f:
            shutil.copyfileobj(env_member, f)
        os.chmod(env_tmp, 0o600)
        os.replace(env_tmp, ENV_PATH)
        restored_env = True

    for suffix in ("-wal", "-shm"):
        try:
            os.remove(DB_PATH + suffix)
        except OSError:
            pass
    os.chmod(tmp_db_path, 0o600)
    os.replace(tmp_db_path, DB_PATH)

    import db
    db.init_db()

    _prune_old_safety_copies()
    return {"restored_env": restored_env, "safety_copy": safety_copy}


MAGIC = b"MBSENC1"


def _key_from_passphrase(passphrase: str, salt: bytes) -> bytes:
    raw = hashlib.scrypt(passphrase.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return base64.urlsafe_b64encode(raw)


def encrypt_backup(data: bytes, passphrase: str) -> bytes:
    from cryptography.fernet import Fernet

    salt = os.urandom(16)
    token = Fernet(_key_from_passphrase(passphrase, salt)).encrypt(data)
    return MAGIC + salt + token


def decrypt_backup(blob: bytes, passphrase: str) -> bytes:
    from cryptography.fernet import Fernet, InvalidToken

    if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + 17:
        raise RestoreError("это не зашифрованный бэкап панели")
    salt = blob[len(MAGIC):len(MAGIC) + 16]
    token = blob[len(MAGIC) + 16:]
    try:
        return Fernet(_key_from_passphrase(passphrase, salt)).decrypt(token)
    except InvalidToken:
        raise RestoreError("неверная парольная фраза или файл повреждён")
