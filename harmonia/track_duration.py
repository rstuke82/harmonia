"""Read source duration values without persisting malformed provider metadata."""
import math

def source_duration(item):
    for field in ('duration_ms','durationInMillis','durationMillis','duration'):
        value=item.get(field)
        if isinstance(value,dict):
            value=value.get('totalMilliseconds') or value.get('milliseconds') or value.get('seconds')
        if isinstance(value,str) and ':' in value:
            try:
                parts=[float(p) for p in value.split(':')]
                if len(parts) not in (2,3):continue
                value=sum(p*60**i for i,p in enumerate(reversed(parts)))
            except ValueError:continue
        try:
            if isinstance(value,bool):continue
            value=float(value)
            if math.isfinite(value) and value>0:return value
        except (ValueError,TypeError):pass
    return None
