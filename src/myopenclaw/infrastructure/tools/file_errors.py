from __future__ import annotations


class FileToolError(Exception):
    """Base exception for workspace file operations."""


class PathOutsideWorkspaceError(FileToolError):
    """Raised when a path resolves outside the current workspace."""


class FileNotReadableError(FileToolError):
    """Raised when a file cannot be read as a regular file."""


class DirectoryNotReadableError(FileToolError):
    """Raised when a directory cannot be read as a directory."""


class FileNotWritableError(FileToolError):
    """Raised when a file cannot be written."""


class NoReplacementMatchError(FileToolError):
    """Raised when an exact replacement target cannot be found."""


class MultipleReplacementMatchesError(FileToolError):
    """Raised when an exact replacement target matches multiple locations."""

