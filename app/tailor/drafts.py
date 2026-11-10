"""Owner and source revision checks for persisted tailoring drafts."""

from beanie import PydanticObjectId
from bson.errors import InvalidId
from fastapi import HTTPException

from app.cv.revision import content_revision
from app.integration.models import SavedCv
from app.tailor.models import TailorDraft
from app.users.models import User


async def owned_draft(draft_id: str, user: User) -> TailorDraft:
    try:
        oid = PydanticObjectId(draft_id)
    except (ValueError, TypeError, InvalidId):
        raise HTTPException(404, 'Draft not found.')
    draft = await TailorDraft.find_one(TailorDraft.id == oid, TailorDraft.user_id == user.id)
    if not draft:
        raise HTTPException(404, 'Draft not found.')
    return draft


async def require_current_draft(draft_id: str, user: User) -> TailorDraft:
    draft = await owned_draft(draft_id, user)
    if draft.source_cv_revision:
        source = await SavedCv.find_one(SavedCv.id == draft.source_cv_id, SavedCv.user_id == user.id)
        if source and content_revision(source.content) == draft.source_cv_revision:
            return draft
    raise HTTPException(409, 'The source CV for this draft could not be verified. Start a new tailoring draft.')
