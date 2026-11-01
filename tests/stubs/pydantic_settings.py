import os
import pydantic
def SettingsConfigDict(**kw): return kw
class BaseSettings(pydantic.BaseModel):
    model_config = {"extra": "ignore"}
    def __init__(self, **kw):
        for name in type(self).model_fields:
            if name not in kw and name.upper() in os.environ:
                kw[name] = os.environ[name.upper()]
        super().__init__(**kw)
