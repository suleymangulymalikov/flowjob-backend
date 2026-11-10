from datetime import datetime

from beanie import PydanticObjectId
from pydantic import BaseModel, EmailStr

from app.auth.inputs import AccountName, ExistingPassword, NewPassword


class UserCreate(BaseModel):
    email: EmailStr
    password: NewPassword
    full_name: AccountName


class UserUpdate(BaseModel):
    full_name: AccountName | None = None
    email: EmailStr | None = None
    # Required only when changing the email.
    current_password: ExistingPassword | None = None


class PasswordChange(BaseModel):
    current_password: ExistingPassword
    new_password: NewPassword


class UserRead(BaseModel):
    id: PydanticObjectId
    email: EmailStr
    full_name: str
    created_at: datetime
