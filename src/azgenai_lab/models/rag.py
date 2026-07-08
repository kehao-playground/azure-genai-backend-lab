from pydantic import BaseModel


class Citation(BaseModel):
    source: str
    title: str | None = None
    url: str | None = None
