import importlib
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CUSTOM_DIR = os.path.join(BASE_DIR, "custom")

_module = None
_checked = False


def load():
    global _module, _checked
    if _checked:
        return _module
    _checked = True
    if BASE_DIR not in sys.path:
        sys.path.insert(0, BASE_DIR)
    if os.path.isfile(os.path.join(CUSTOM_DIR, "__init__.py")):
        _module = importlib.import_module("custom")
    return _module


def call(name: str, default=None, *args, **kwargs):
    module = load()
    func = getattr(module, name, None) if module else None
    if func is None:
        return default
    return func(*args, **kwargs)


def template(name: str, default: str) -> str:
    path = os.path.join(CUSTOM_DIR, "templates", name)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return default


def site_path(name: str, default_dir: str) -> str:
    custom = os.path.join(CUSTOM_DIR, "site", name)
    if os.path.isfile(custom):
        return custom
    return os.path.join(default_dir, name)
