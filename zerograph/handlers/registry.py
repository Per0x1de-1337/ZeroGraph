"""MCP handler registration."""

import logging

from .index_handlers import register_index_handlers
from .browse_handlers import register_browse_handlers
from .audit_handlers import register_audit_handlers
from .guides import register_prompts

logger = logging.getLogger(__name__)


def register_tools(mcp, services: dict):
    register_index_handlers(mcp, services)
    register_browse_handlers(mcp, services)
    register_audit_handlers(mcp, services)
    register_prompts(mcp)

    try:
        from .extension_handlers import register_extension_handlers
        register_extension_handlers(mcp, services)
        logger.info("Extension handlers loaded")
    except ImportError:
        logger.debug("extension_handlers not present")
    except Exception as e:
        logger.error("Extension handler registration failed: %s", e, exc_info=True)
