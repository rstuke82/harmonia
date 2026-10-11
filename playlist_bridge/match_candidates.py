"""Consolidate repeated Plex entries without hiding distinct recordings."""
def consolidate(rows):
    groups=[];by_id={};evidence={}
    def clean(row):return {k:v for k,v in row.items() if not k.startswith('_')}
    for row in rows:
        pid=str(row.get('plex_id') or '')
        if pid in by_id:continue
        signature=tuple(str(row.get(k) or '').casefold().strip() for k in ('_album_id','album','title','artist','track_artist','album_artist','duration','disc_number','track_number','edition'))
        # Titles and percentages alone are never proof of the same recording.
        keys=[('file',str(f),signature) for f in row.get('_files',[]) if f]
        keys += [('recording',str(g),signature) for g in row.get('_guids',[]) if str(g).startswith(('plex://track/','mbid://','musicbrainz://'))]
        group=next((evidence[k] for k in keys if k in evidence),None)
        item=clean(row)
        if group is None:
            group=item;groups.append(group)
        else:
            group.setdefault('other_entries',[]).append(item)
        by_id[pid]=group
        for key in keys:evidence[key]=group
    return groups
