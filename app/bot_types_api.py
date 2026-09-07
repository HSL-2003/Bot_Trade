"""Bot Types API endpoints for CRUD operations."""
import os
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from services.auth_dependencies import require_admin


router = APIRouter(prefix="/api/bot-types", tags=["bot-types"])


def get_supabase():
    """Get Supabase client instance."""
    from supabase import create_client
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise HTTPException(status_code=500, detail="Supabase not configured")
    return create_client(url, key)


class BotTypeCreate(BaseModel):
    name: str
    slug: str
    description: Optional[str] = None
    monthly_profit_min: float = 0
    monthly_profit_max: float = 0
    min_capital: float = 0
    max_capital: Optional[float] = None
    risk_level: str = "medium"
    trade_frequency: str = "medium"
    is_active: bool = True


class BotTypeUpdate(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    monthly_profit_min: Optional[float] = None
    monthly_profit_max: Optional[float] = None
    min_capital: Optional[float] = None
    max_capital: Optional[float] = None
    risk_level: Optional[str] = None
    trade_frequency: Optional[str] = None
    is_active: Optional[bool] = None


class BotTypeResponse(BaseModel):
    id: str
    name: str
    slug: str
    description: Optional[str]
    monthly_profit_min: float
    monthly_profit_max: float
    min_capital: float
    max_capital: Optional[float]
    risk_level: str
    trade_frequency: str
    is_active: bool
    created_at: str
    updated_at: str


@router.get("", response_model=list[BotTypeResponse])
async def list_bot_types(
    request: Request,
    supabase=Depends(get_supabase),
):
    """Lấy danh sách tất cả bot types (public)."""
    result = supabase.table("bot_types").select("*").eq("is_active", True).execute()
    return result.data


@router.get("/{bot_type_id}", response_model=BotTypeResponse)
async def get_bot_type(
    bot_type_id: str,
    request: Request,
    supabase=Depends(get_supabase),
):
    """Lấy chi tiết 1 bot type."""
    result = supabase.table("bot_types").select("*").eq("id", bot_type_id).single().execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Bot type not found")
    return result.data


@router.post("", response_model=BotTypeResponse)
async def create_bot_type(
    data: BotTypeCreate,
    request: Request,
    supabase=Depends(get_supabase),
    admin=Depends(require_admin),
):
    """Tạo bot type mới (admin only)."""
    result = supabase.table("bot_types").insert(data.model_dump()).execute()
    return result.data


@router.put("/{bot_type_id}", response_model=BotTypeResponse)
async def update_bot_type(
    bot_type_id: str,
    data: BotTypeUpdate,
    request: Request,
    supabase=Depends(get_supabase),
    admin=Depends(require_admin),
):
    """Cập nhật bot type (admin only)."""
    update_data = {k: v for k, v in data.model_dump().items() if v is not None}
    result = supabase.table("bot_types").update(update_data).eq("id", bot_type_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Bot type not found")
    return result.data


@router.delete("/{bot_type_id}")
async def delete_bot_type(
    bot_type_id: str,
    request: Request,
    supabase=Depends(get_supabase),
    admin=Depends(require_admin),
):
    """Xóa bot type (admin only)."""
    result = supabase.table("bot_types").delete().eq("id", bot_type_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Bot type not found")
    return {"status": "deleted", "id": bot_type_id}
