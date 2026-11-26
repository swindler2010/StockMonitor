"""Constant-time presentation of the existing owned feed worker, no polling jobs."""
def activity(client,closing=False,error=None):
    if closing:return 'STOPPED'
    if client is None:return 'UNKNOWN'
    if client.closed:return 'STOPPED'
    process=client.process
    if process is not None and process.poll() is not None:return 'ERROR'
    if client.active:
        return 'ACTIVE' if process is not None and client.active_kind=='feed_update' else 'UNKNOWN'
    if error:return 'ERROR'
    return 'IDLE'

def presentation(state):
    return ('#22c55e' if state=='ACTIVE' else '#ef4444', '● CandlePilot (CP) — '+state)
