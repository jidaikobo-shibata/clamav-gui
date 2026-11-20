"""Display ordering, independent of scan conditions and execution queues."""
MEDIA_ID = 'external-media'
PREFERENCE = 'plan-display-order'


def reconcile(saved, keys):
    """Drop stale/duplicate IDs and append newly registered plans."""
    known = set(keys)
    order = []
    seen = set()
    if isinstance(saved, list):
        for key in saved:
            if isinstance(key, str) and key in known and key not in seen:
                order.append(key)
                seen.add(key)
    return order + [key for key in keys if key not in seen]
