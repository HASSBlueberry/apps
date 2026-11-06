"""Compatibility exports and extensible provider registry."""
from .base import Provider,ProviderError,Unsupported,local_url,dns_value,forward_value,dns_settings_value
from .parsing import call_literals,arc_digest
from .drivers.g3100 import G3100Provider
from .drivers.proxy import ProxyProvider
from .drivers.http_json import JsonProvider

def load_eero_provider():
    # The Eero implementation stays in its own provider directory in the source tree
    # and is copied beside the backend in the standalone app build context.
    import importlib.util
    from pathlib import Path
    source = Path(__file__).resolve()
    candidates = [source.parents[2] / 'BlueberryEero' / 'provider.py', source.parents[1] / 'BlueberryEero' / 'provider.py']
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None: raise RuntimeError('The app build context is missing BlueberryEero/provider.py')
    spec = importlib.util.spec_from_file_location('blueberry_quantum.eero_provider', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.EeroProvider


EeroProvider = load_eero_provider()
DRIVERS = {driver.kind: driver for driver in (G3100Provider, ProxyProvider, JsonProvider, EeroProvider)}

def register_provider(driver):
    import re
    if not isinstance(driver,type) or not issubclass(driver,Provider) or not re.fullmatch('[a-z][a-z0-9_]{0,63}',driver.kind):
        raise ValueError('Provider plugins must declare a valid kind and extend Provider')
    if driver.kind in DRIVERS: raise ValueError('Duplicate provider kind '+driver.kind)
    DRIVERS[driver.kind]=driver

from importlib.metadata import entry_points
for entry in entry_points(group='blueberry_quantum.providers'):
    register_provider(entry.load())
