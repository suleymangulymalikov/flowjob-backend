import re
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from app.auth.dependencies import get_current_user
from app.cv.http import render_pdf
from app.cv.render import render_cv_pdf
from app.profiles.models import Profile
from app.users.models import User

router = APIRouter(prefix="/cv", tags=["cv"])


@router.get("/pdf", response_class=Response, responses={200: {"content": {"application/pdf": {}}}})
async def download_cv_pdf(user: Annotated[User, Depends(get_current_user)]) -> Response:
    profile = await Profile.find_one(Profile.user_id == user.id) or Profile(user_id=user.id)
    # PDF rendering is CPU-bound, so run it off the event loop.
    pdf = await render_pdf(render_cv_pdf, user.full_name, user.email, profile)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", user.full_name).strip("_")
    filename = f"{safe}_CV.pdf" if safe else "CV.pdf"
    return Response(
        pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
