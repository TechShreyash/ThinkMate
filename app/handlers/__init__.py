from aiogram import Router
from app.handlers.commands import router as commands_router
from app.handlers.messages import router as messages_router

main_router = Router(name="main")
main_router.include_router(commands_router)
main_router.include_router(messages_router)

__all__ = ["main_router"]
