"""Reject incomplete provider observations before they can remove tracks."""

def validate(repo,key,tracks,metadata,previous=None):
    expected=metadata.get('expected_track_count')
    complete=metadata.get('complete')
    if isinstance(expected,int):
        if len(tracks)<expected:
            raise ValueError(f'Source returned {len(tracks)} of {expected} expected tracks. The existing playlist was left unchanged.')
        complete=True
    prior=previous if isinstance(previous,list) else []
    if not tracks and complete is not True:
        raise ValueError('Source returned no tracks without confirming that the playlist is empty. The existing playlist was left unchanged.')
    if prior and complete is not True and len(tracks)<len(prior):
        raise ValueError(f'Source returned {len(tracks)} tracks, fewer than the previous {len(prior)}, and could not confirm completeness. The existing playlist was left unchanged.')
    return complete is True
