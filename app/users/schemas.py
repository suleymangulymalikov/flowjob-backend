from datetime import datetime

from beanie import PydanticObjectId
from pydantic import BaseModel, EmailStr, Field


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str = Field(min_length=1)


class UserRead(BaseModel):
    id: PydanticObjectId
    email: EmailStr
    full_name: str
    created_at: datetime
