class AsyncClient:
    def __init__(self, *a, **k): pass
class HTTPError(Exception): pass
class Timeout:
    def __init__(self, *a, **k): pass
class Response: pass
class HTTPStatusError(Exception): pass
class TimeoutException(Exception): pass
class RequestError(Exception): pass
def __getattr__(name):
    return type(name, (Exception,), {})
