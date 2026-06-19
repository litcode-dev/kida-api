from typing import Literal

from pydantic import BaseModel, EmailStr


class AppDownloadRequestBody(BaseModel):
    email: EmailStr
    os: Literal["macos", "windows"]
