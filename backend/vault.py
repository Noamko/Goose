from pathlib import Path
from cryptography.fernet import Fernet

from .database import get_all_secret_blobs, set_secret_blob, delete_secret_entry

VAULT_KEY_PATH = ".vault.key"


class Vault:
    def __init__(self, key: bytes):
        self.fernet = Fernet(key)

    def encrypt(self, value: str) -> bytes:
        return self.fernet.encrypt(value.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        return self.fernet.decrypt(ciphertext).decode("utf-8")

    @classmethod
    def load_or_create(cls, key_path: str = VAULT_KEY_PATH) -> "Vault":
        path = Path(key_path)
        if path.exists():
            key = path.read_bytes()
        else:
            key = Fernet.generate_key()
            path.write_bytes(key)
            path.chmod(0o600)
            print(f"[vault] Created new encryption key at {key_path} — back this up!")
        return cls(key)

    async def get_secrets(self, template_id: str) -> dict[str, str]:
        blobs = await get_all_secret_blobs(template_id)
        return {k: self.decrypt(v) for k, v in blobs.items()}

    async def set_secret(self, template_id: str, key: str, value: str):
        enc = self.encrypt(value)
        await set_secret_blob(template_id, key, enc)

    async def delete_secret(self, template_id: str, key: str):
        await delete_secret_entry(template_id, key)
