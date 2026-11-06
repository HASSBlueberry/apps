"""Inspect the gateway's own Vue/Webpack assets without executing downloaded code."""
from __future__ import annotations
from html.parser import HTMLParser
import hashlib
import json
import re
from urllib.parse import urljoin, urlsplit


class AssetLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('src'):
            self.links.append(attrs['src'])
        if tag == 'link' and attrs.get('href', '').split('?')[0].endswith(('.js', '.json')):
            self.links.append(attrs['href'])


def same_origin(base, value):
    url = urljoin(base, value)
    left, right = urlsplit(base), urlsplit(url)
    if (left.scheme, left.hostname, left.port) != (right.scheme, right.hostname, right.port):
        return None
    if right.username or right.password:
        return None
    return url


def asset_candidates(text):
    candidates = set(re.findall(r'''["']((?:/?(?:static/)?(?:js|assets)/)[^"'<>\s+]+\.js(?:\?[^"'<>\s]*)?)["']''', text))
    # Restrict chunk-map extraction to the JS runtime, excluding CSS hash tables.
    position = text.find('__webpack_require__.u=')
    material = text[position:position+12000].split('}[e]',1)[0] + '}' if position >= 0 else ''
    mappings = re.findall(r'\{((?:\d+:"[a-f0-9]{8}",?)+)\}', material)
    for mapping in mappings:
        for key,digest in re.findall(r'(\d+):"([a-f0-9]{8})"', mapping):
            name = 'about' if key == '2443' and '"about"' in material else key
            candidates.add('js/' + name + '.' + digest + '.js')
    return sorted(candidates)[:128]


def analyze(text):
    endpoints = sorted(set('/' + value.lstrip('/') for value in re.findall(r'''["']((?:/api/|/cgi/)[A-Za-z0-9_./-]+|/?[A-Za-z0-9_./-]+\.cgi)["']''', text)))
    return {'vue': bool(re.search(r'Vue|__VUE__|vue-router', text)),
            'webpack': bool(re.search(r'webpackJsonp|webpackChunk|__webpack_require__|\.webpack', text)),
            'endpoints': endpoints[:300]}


async def inspect_gateway(fetch_text, base, max_assets=128):
    root = await fetch_text('/')
    parser = AssetLinks()
    parser.feed(root)
    pending = [same_origin(base + '/', link) for link in parser.links]
    assets, seen, combined, total = [], set(), [root], len(root.encode())
    while pending and len(assets) < max_assets:
        url = pending.pop(0)
        if not url or url in seen:
            continue
        seen.add(url)
        parsed = urlsplit(url)
        path = parsed.path + ('?' + parsed.query if parsed.query else '')
        try:
            text = await fetch_text(path)
        except Exception:
            assets.append({'path': parsed.path, 'status': 'unavailable'})
            continue
        total += len(text.encode())
        if total > 32 * 1024 * 1024:
            break
        assets.append({'path': parsed.path, 'status': 'read', 'sha256': hashlib.sha256(text.encode()).hexdigest(), 'bytes': len(text.encode())})
        combined.append(text)
        for candidate in asset_candidates(text):
            value = same_origin(base + chr(47), candidate)
            if value and value not in seen:
                pending.append(value)
    analysis = analyze('\n'.join(combined))
    material = '\n'.join(combined)
    analysis['profiles'] = {
        'forwardrule': bool(re.search(r'to:"forwardrule"',material) and re.search(r'tcp:1,udp:2,gre:4,esp:5,ah:6,both:8',material)),
        'dns': all(marker in material for marker in ('dnsmasq_reload','dns_name','dns_ip','apply_abstract.cgi','o[e.i]=e.v')),
        'upnp': all(marker in material for marker in ('ui_upnp','upnp_enable')),
        'lighting': all(marker in material for marker in ('router_led_dim','router_led_timer')),
        'dmz': all(marker in material for marker in ('ui_firewall','firewall_dmz_enable','firewall_dmz_ip')),
        'dhcp': all(marker in material for marker in ('dhcp_start_ip','dhcp_end_ip','dhcp_lease_time','dhcp_enable0')),
        'device_access': all(marker in material for marker in ('ui_block_dev','ui_remove_block_dev','block_mac_')), 
    }
    analysis.update(assets=assets, origin=base, asset_limit=max_assets,
                    fingerprint=hashlib.sha256(json.dumps(assets, sort_keys=True).encode()).hexdigest())
    return analysis
