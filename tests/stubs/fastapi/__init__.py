class HTTPException(Exception):
    def __init__(self, status_code, detail=None):
        super().__init__(detail); self.status_code = status_code; self.detail = detail
class APIRouter:
    def __init__(self, *a, **k): pass
    def _d(self, *a, **k): return lambda f: f
    get = post = put = delete = api_route = _d
class FastAPI(APIRouter):
    def include_router(self, *a, **k): pass
    def middleware(self, *a, **k): return lambda f: f
def Depends(x=None): return None
def Form(x=None, **k): return x
def Header(x=None, **k): return x
class Request: pass
class BackgroundTasks: pass
def File(x=None, **k): return x
class UploadFile:
    def __init__(self, data=b"", filename="", content_type=""):
        self._d, self.filename, self.content_type = data, filename, content_type
    async def read(self, n=-1): return self._d if n < 0 else self._d[:n]
def Query(x=None, **k): return x
