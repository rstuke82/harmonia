"""Normalize service-provided names without rewriting user-entered names."""
import html
import re

def clean(value):
    value=html.unescape(str(value or ''))
    for _ in range(2):
        if not any(c in value for c in ('â','Ã','Â')):break
        for encoding in ('latin1','cp1252'):
            try:
                repaired=value.encode(encoding).decode('utf-8')
                if repaired!=value:value=repaired;break
            except (UnicodeError,LookupError):continue
        else:break
    return re.sub(r'\s+(?:on Apple Music|[-–] (?:Playlist [-–] )?Apple Music)$','',value,flags=re.I).strip()
