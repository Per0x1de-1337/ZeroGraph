"""
Custom exceptions for ZeroGraph Server
"""


class ZeroGraphError(Exception):
    """Base exception for ZeroGraph"""

    pass


# Alias for backward compatibility
ZeroGraphError = ZeroGraphError


class CPGGenerationError(ZeroGraphError):
    """CPG generation failed"""

    pass


class QueryExecutionError(ZeroGraphError):
    """Query execution failed"""

    pass


class ResourceLimitError(ZeroGraphError):
    """Resource limit exceeded"""

    pass


class ValidationError(ZeroGraphError):
    """Input validation failed"""

    pass


class GitOperationError(ZeroGraphError):
    """Git operation failed"""

    pass
