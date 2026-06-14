from aiogram import Router
from app.handlers.commands import router as commands_router
from app.handlers.messages import router as messages_router
from app.handlers.middlewares import ProactiveResetMiddleware

# Any command counts as the user engaging, so clear their unanswered-proactive streak
# before the command runs. DB session is already injected by the dispatcher-level
# DbSessionMiddleware, so ``data["db"]`` is available to this inner middleware.
commands_router.message.middleware(ProactiveResetMiddleware())

main_router = Router(name="main")
main_router.include_router(commands_router)
main_router.include_router(messages_router)

__all__ = ["main_router"]
