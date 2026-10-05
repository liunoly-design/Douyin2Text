"""Restrict platform requests and pin public DNS results at TCP connection time."""
import asyncio
import ipaddress
import re
import socket
import ssl
from urllib.parse import urlsplit

import httpcore
import httpx
from httpcore._backends.anyio import AnyIOBackend

PLATFORM = ('douyin.com', 'iesdouyin.com')
CDN = PLATFORM + ('douyinvod.com', 'douyinpic.com', 'byteimg.com', 'ibytedtos.com', 'bytedance.com', 'pstatp.com', 'snssdk.com', 'bytecdn.cn')

def validate_url(url, media=False):
    u = urlsplit(str(url))
    domains = CDN if media else PLATFORM
    if (u.scheme != 'https' or not u.hostname or u.username or u.password
            or u.port not in (None, 443) or len(str(url)) > 8192
            or not any(u.hostname == d or u.hostname.endswith('.' + d) for d in domains)):
        raise ValueError('Disallowed platform destination')
    return u

class PublicBackend(AnyIOBackend):
    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if port != 443:
            raise ValueError('Only public HTTPS connections permitted')
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(x[4][0] for x in infos))
        if addresses and all(ipaddress.ip_address(ip) in ipaddress.ip_network('198.18.0.0/15') for ip in addresses):
            # This Mac uses a fake-IP proxy DNS. Resolve against a fixed public DoH
            # endpoint instead of relaxing the ban on private/benchmark addresses.
            async with httpx.AsyncClient(trust_env=False, timeout=15) as dns:
                r = await dns.get('https://1.1.1.1/dns-query', params={'name': host, 'type': 'A'}, headers={'Accept': 'application/dns-json'})
                r.raise_for_status()
                reply = r.json()
                if reply.get('Status') != 0:
                    raise ValueError('Public DNS resolution failed')
                addresses = [x['data'] for x in reply.get('Answer', []) if x.get('type') == 1]
        if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
            raise ValueError('Non-public destination rejected')
        # Connect using the checked numeric address. TLS SNI remains the original host.
        last = None
        for ip in addresses:
            try:
                return await super().connect_tcp(ip, port, timeout, local_address, socket_options)
            except (OSError, httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last = exc
        raise last

class PlatformTransport(httpx.AsyncHTTPTransport):
    def __init__(self, media=False):
        super().__init__(trust_env=False)
        self.media = media
        self._pool = httpcore.AsyncConnectionPool(ssl_context=ssl.create_default_context(), network_backend=PublicBackend())

    async def handle_async_request(self, request):
        validate_url(request.url, self.media)
        return await super().handle_async_request(request)

def client(media=False, **kwargs):
    return httpx.AsyncClient(transport=PlatformTransport(media), trust_env=False, **kwargs)

async def video_id(url, headers):
    validate_url(url)
    async with client(headers=headers, timeout=30, follow_redirects=False) as c:
        for _ in range(8):
            u = validate_url(url)
            found = re.search(r'/(?:video|note)/(\d{10,24})(?:/|$)', u.path)
            if found:
                return found.group(1)
            if u.query:
                from urllib.parse import parse_qs
                values = parse_qs(u.query).get('modal_id', [])
                if values and re.fullmatch(r'\d{10,24}', values[0]):
                    return values[0]
            r = await c.get(url)
            if r.is_redirect:
                url = str(r.url.join(r.headers['location']))
                continue
            r.raise_for_status()
            raise ValueError('No single public video ID in platform URL')
    raise ValueError('Too many platform redirects')
