"""Agent profiles and repositories routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import get_state
from engine.state import StateManager
from models.ticket import (
    CreateAgentProfileRequest,
    CreateLabelMappingRequest,
    CreateRepositoryRequest,
    UpdateAgentProfileRequest,
    UpdateRepositoryRequest,
)

router = APIRouter(tags=["profiles"])


# --- Repositories ---


@router.get("/api/repositories")
async def list_repositories(state: StateManager = Depends(get_state)):
    repos = await state.list_repositories()
    return [r.model_dump() for r in repos]


@router.post("/api/repositories")
async def create_repository(
    req: CreateRepositoryRequest,
    state: StateManager = Depends(get_state),
):
    repo = await state.create_repository(req.name, req.path, req.default_branch, req.jira_label, req.default_profile_id)
    return repo.model_dump()


@router.put("/api/repositories/{repo_id}")
async def update_repository(
    repo_id: int,
    req: UpdateRepositoryRequest,
    state: StateManager = Depends(get_state),
):
    updates = req.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(400, "No fields to update")
    repo = await state.update_repository(repo_id, **updates)
    if not repo:
        raise HTTPException(404, "Repository not found")
    return repo.model_dump()


@router.delete("/api/repositories/{repo_id}")
async def delete_repository(
    repo_id: int,
    state: StateManager = Depends(get_state),
):
    await state.delete_repository(repo_id)
    return {"status": "deleted"}


# --- Label Mappings ---


@router.get("/api/label-mappings")
async def list_label_mappings(state: StateManager = Depends(get_state)):
    mappings = await state.list_label_mappings()
    return [m.model_dump() for m in mappings]


@router.post("/api/label-mappings")
async def create_label_mapping(
    req: CreateLabelMappingRequest,
    state: StateManager = Depends(get_state),
):
    mapping = await state.create_label_mapping(req.jira_label, req.repository_id)
    return mapping.model_dump()


@router.delete("/api/label-mappings/{mapping_id}")
async def delete_label_mapping(
    mapping_id: int,
    state: StateManager = Depends(get_state),
):
    await state.delete_label_mapping(mapping_id)
    return {"status": "deleted"}


# --- Agent Profiles ---


@router.get("/api/profiles")
async def list_agent_profiles(state: StateManager = Depends(get_state)):
    profiles = await state.list_agent_profiles()
    return [p.model_dump() for p in profiles]


@router.post("/api/profiles")
async def create_agent_profile(
    req: CreateAgentProfileRequest,
    state: StateManager = Depends(get_state),
):
    profile = await state.create_agent_profile(
        req.name,
        req.command,
        req.args_template,
        req.log_format,
        phases_config=req.phases_config,
    )
    return profile.model_dump()


@router.put("/api/profiles/{profile_id}")
async def update_agent_profile(
    profile_id: int,
    req: UpdateAgentProfileRequest,
    state: StateManager = Depends(get_state),
):
    updates = {k: v for k, v in req.model_dump(exclude_unset=True).items() if v is not None or k == "phases_config"}
    if not updates:
        raise HTTPException(400, "No fields to update")
    profile = await state.update_agent_profile(profile_id, **updates)
    if not profile:
        raise HTTPException(404, "Profile not found")
    return profile.model_dump()


@router.put("/api/profiles/{profile_id}/default")
async def set_default_profile(
    profile_id: int,
    state: StateManager = Depends(get_state),
):
    await state.set_default_agent_profile(profile_id)
    return {"status": "updated"}


@router.delete("/api/profiles/{profile_id}")
async def delete_agent_profile(
    profile_id: int,
    state: StateManager = Depends(get_state),
):
    await state.delete_agent_profile(profile_id)
    return {"status": "deleted"}
