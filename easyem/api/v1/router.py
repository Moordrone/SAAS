from fastapi import APIRouter

from . import auth, credits, me, projects, simulations, waitlist

api_v1 = APIRouter(prefix="/v1")
api_v1.include_router(auth.router)
api_v1.include_router(me.router)
api_v1.include_router(credits.router)
api_v1.include_router(projects.router)
api_v1.include_router(simulations.router)
api_v1.include_router(waitlist.router)
