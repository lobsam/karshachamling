from datetime import datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, EmailStr, Field, field_validator

T = TypeVar("T")
Status = Literal["draft", "published"]
Role = Literal["admin", "editor"]


class Paginated(BaseModel, Generic[T]):
    items: list[T]
    total: int


# ---------- Auth & users ----------


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: int
    email: str
    name: str
    role: Role
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime


class UserCreate(BaseModel):
    email: EmailStr
    name: str = ""
    role: Role = "editor"
    password: str = Field(min_length=10)


class UserUpdate(BaseModel):
    name: str | None = None
    role: Role | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=10)


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=10)


# ---------- Media ----------


class MediaOut(BaseModel):
    id: int
    filename: str
    mime_type: str
    width: int
    height: int
    size_bytes: int
    alt: str
    alt_bo: str
    source_url: str | None
    created_at: datetime
    url: str  # 1200px WebP — the usual one to display
    urls: dict[str, str]  # sm (400px), md (1200px), lg (2000px), original


class MediaUpdate(BaseModel):
    alt: str | None = None
    alt_bo: str | None = None


# ---------- Content ----------


class ContentIn(BaseModel):
    slug: str | None = None
    title: str = Field(min_length=1, max_length=300)
    title_bo: str = ""
    summary: str = ""
    summary_bo: str = ""
    body: str = ""
    body_bo: str = ""
    status: Status = "draft"
    cover_image_id: int | None = None

    @field_validator("slug")
    @classmethod
    def _blank_slug(cls, v: str | None) -> str | None:
        return (v.strip() or None) if v is not None else None


class ContentOut(BaseModel):
    id: int
    slug: str
    title: str
    title_bo: str
    summary: str
    summary_bo: str
    body: str
    body_bo: str
    status: Status
    cover_image_id: int | None
    cover_image: MediaOut | None
    created_at: datetime
    updated_at: datetime


class NewsIn(ContentIn):
    published_at: datetime | None = None


class NewsOut(ContentOut):
    published_at: datetime | None


class EventIn(ContentIn):
    start_at: datetime
    end_at: datetime | None = None
    location: str = ""
    location_bo: str = ""


class EventOut(ContentOut):
    start_at: datetime
    end_at: datetime | None
    location: str
    location_bo: str


class GuideIn(ContentIn):
    position: int | None = None


class GuideOut(ContentOut):
    position: int


class PageIn(ContentIn):
    pass


class PageOut(ContentOut):
    pass


class ReorderIn(BaseModel):
    ids: list[int]


# ---------- Contact & donations ----------


class ContactIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    subject: str = Field(default="", max_length=300)
    message: str = Field(min_length=1, max_length=10_000)
    turnstile_token: str = ""


class ContactOut(BaseModel):
    id: int
    name: str
    email: str
    subject: str
    message: str
    is_read: bool
    created_at: datetime


class ContactUpdate(BaseModel):
    is_read: bool


class DonationOrderIn(BaseModel):
    amount_inr: int = Field(gt=0, le=10_000_000)
    name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    phone: str = Field(default="", max_length=50)
    pan: str = Field(default="", max_length=20)
    address: str = Field(default="", max_length=2000)
    message: str = Field(default="", max_length=2000)


class DonationOrderOut(BaseModel):
    order_id: str
    key_id: str
    amount_paise: int
    currency: str


class DonationVerifyIn(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class DonationOut(BaseModel):
    id: int
    source: str
    razorpay_order_id: str | None
    razorpay_payment_id: str | None
    amount_paise: int
    currency: str
    donor_name: str
    donor_email: str
    donor_phone: str
    donor_pan: str
    donor_address: str
    message: str
    status: str
    created_at: datetime
    paid_at: datetime | None


class DashboardOut(BaseModel):
    news_published: int
    news_drafts: int
    upcoming_events: int
    guide_sections: int
    media_count: int
    unread_messages: int
    donations_paid_count: int | None = None
    donations_paid_total_paise: int | None = None
