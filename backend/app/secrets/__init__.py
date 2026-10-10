"""Protected local storage for connector credentials."""

from app.secrets.errors import SecretVaultError, SecretVaultOperation
from app.secrets.protector import DataProtectionUnavailable, WindowsDpapiProtector
from app.secrets.reference import SecretReference
from app.secrets.vault import SecretVault, WindowsDpapiSecretVault

__all__ = [
    "DataProtectionUnavailable",
    "SecretReference",
    "SecretVault",
    "SecretVaultError",
    "SecretVaultOperation",
    "WindowsDpapiProtector",
    "WindowsDpapiSecretVault",
]
