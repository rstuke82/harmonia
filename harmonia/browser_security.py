"""Explicit transport configuration and browser response hardening."""
import ipaddress
import os
from starlette.responses import JSONResponse


def https_only():
    value=os.environ.get('HARMONIA_HTTPS_ONLY','false').strip().lower()
    if value not in ('true','false','1','0'):
        raise ValueError('HARMONIA_HTTPS_ONLY must be true or false')
    return value in ('true','1')


def trusted_proxies():
    value=os.environ.get('HARMONIA_TRUSTED_PROXIES','127.0.0.1,::1')
    entries=[entry.strip() for entry in value.split(',') if entry.strip()]
    for entry in entries:
        try:
            network=ipaddress.ip_network(entry,strict=False)
            if network.prefixlen==0:raise ValueError()
        except ValueError:
            raise ValueError('HARMONIA_TRUSTED_PROXIES must contain explicit proxy IP addresses or CIDRs; wildcard trust is not allowed') from None
    return ','.join(entries)


def secure_cookie(request):
    return https_only() or request.url.scheme=='https'


CSP="; ".join((
    "default-src 'self'", "script-src 'self'", "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob: https: http:", "font-src 'self' data:",
    "connect-src 'self'", "media-src 'self' blob: https: http:",
    "frame-src https://embed.music.apple.com", "frame-ancestors 'none'",
    "object-src 'none'", "base-uri 'self'", "form-action 'self'",
    "worker-src 'self'", "manifest-src 'self'",
))


class BrowserSecurity:
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        secure=scope.get('scheme')=='https'
        started=False
        async def secured(message):
            nonlocal started
            if message['type']=='http.response.start':
                started=True
                headers=[(k,v) for k,v in message.get('headers',[]) if k.lower() not in {
                    b'content-security-policy',b'x-frame-options',b'x-content-type-options',b'referrer-policy',b'permissions-policy',b'strict-transport-security'}]
                headers.extend([(b'content-security-policy',CSP.encode()),(b'x-frame-options',b'DENY'),
                    (b'x-content-type-options',b'nosniff'),(b'referrer-policy',b'strict-origin-when-cross-origin'),
                    (b'permissions-policy',b'camera=(), microphone=(), geolocation=()')])
                if secure:headers.append((b'strict-transport-security',b'max-age=31536000'))
                path=scope.get('path','')
                if path=='/api' or path.startswith('/api/') or any(k.lower()==b'set-cookie' for k,v in headers):
                    headers=[(k,v) for k,v in headers if k.lower() not in (b'cache-control',b'pragma',b'expires')]
                    headers.extend([(b'cache-control',b'no-store'),(b'pragma',b'no-cache')])
                message={**message,'headers':headers}
            await send(message)
        health=scope.get('path')=='/api/health' and (scope.get('client') or ('',))[0] in ('127.0.0.1','::1')
        if https_only() and not secure and not health:
            return await JSONResponse({'detail':'HTTPS is required. Check TLS termination and trusted proxy forwarding.'},status_code=400)(scope,receive,secured)
        try:
            return await self.app(scope,receive,secured)
        except Exception:
            if not started:
                await JSONResponse({'detail':'Internal server error.'},status_code=500)(scope,receive,secured)
            # Preserve server error logging; Starlette will not send a second response.
            raise
