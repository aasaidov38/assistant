class _R:
    def __init__(self, *a, **k): self.a, self.k = a, k
HTMLResponse = FileResponse = RedirectResponse = JSONResponse = PlainTextResponse = _R
Response = _R
