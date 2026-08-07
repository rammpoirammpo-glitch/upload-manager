from .base import BaseProvider
from .filehost import FileHostProvider
from .local import LocalProvider
from .s3 import S3Provider
from .sftp import SFTPProvider
from .webdav import WebDAVProvider

REGISTRY = {c.type: c for c in (WebDAVProvider, S3Provider, SFTPProvider, LocalProvider, FileHostProvider)}


def get_provider_class(provider_type):
    return REGISTRY.get(provider_type)


def get_all_provider_classes():
    return [REGISTRY[t] for t in sorted(REGISTRY)]
