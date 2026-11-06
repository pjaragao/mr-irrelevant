"""Compatibility shim for PyYAML (`import yaml`) when running inside containers
that provide `ruamel.yaml` (e.g. Hermes Agent Python 3.14 venv) instead of PyYAML.

If a real `PyYAML` package exists further down `sys.path`, we replace `sys.modules["yaml"]`
with it; otherwise we expose `safe_load`, `load`, `safe_dump`, `dump`, and `YAMLError`
backed by `ruamel.yaml.YAML(typ="safe", pure=True)`.
"""
import importlib.machinery
import importlib.util
import io
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_real_pyyaml():
    search_paths = [p for p in sys.path if os.path.abspath(p or ".") != _THIS_DIR]
    spec = importlib.machinery.PathFinder.find_spec("yaml", search_paths)
    if spec and spec.origin and os.path.abspath(os.path.dirname(spec.origin)) != _THIS_DIR:
        mod = importlib.util.module_from_spec(spec)
        sys.modules["yaml"] = mod
        spec.loader.exec_module(mod)
        return mod
    return None


_real = _load_real_pyyaml()

if _real is None:
    from ruamel.yaml import YAML, YAMLError  # type: ignore

    class SafeLoader:
        pass

    def safe_load(stream):
        y = YAML(typ="safe", pure=True)
        if isinstance(stream, (bytes, bytearray)):
            stream = stream.decode("utf-8", errors="replace")
        return y.load(stream)

    def load(stream, Loader=None):
        return safe_load(stream)

    def safe_dump(data, stream=None, **kwargs):
        y = YAML(typ="safe", pure=True)
        y.default_flow_style = kwargs.get("default_flow_style", False)
        buf = io.StringIO()
        y.dump(data, buf)
        out = buf.getvalue()
        if stream is not None:
            stream.write(out)
            return None
        return out

    def dump(data, stream=None, **kwargs):
        return safe_dump(data, stream=stream, **kwargs)
