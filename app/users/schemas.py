from datetime import datetime

from beanie import PydanticObjectId
from pydantic import BaseModel, EmailStr, Field


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str = Field(min_length=1)


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1)
    email: EmailStr | None = None
    # Required only when changing the email.
    current_password: str | None = None


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class UserRead(BaseModel):
    id: PydanticObjectId
    email: EmailStr
    full_name: str
    created_at: datetime
