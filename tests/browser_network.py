"""One-shot faults without toggling Chromium interception during recovery.

Removing the last `times=1` route while aborting can strand the immediately
following fetch before it reaches the server. Retain the dispatcher; subsequent
matches continue normally. Does not retry, fulfill or hide any app request.
"""

def route_once(page,pattern,handler):
    consumed=False
    def dispatch(route):
        nonlocal consumed
        if consumed:return route.continue_()
        consumed=True
        return handler(route)
    page.route(pattern,dispatch)
    return dispatch
