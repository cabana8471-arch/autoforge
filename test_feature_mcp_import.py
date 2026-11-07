"""
Feature MCP Server Import Tests
===============================

Guards against dependency drift that breaks the feature MCP server at import
time (e.g. mcp 2.x removed mcp.server.fastmcp).
Run with: python -m pytest test_feature_mcp_import.py
"""

import importlib


def test_feature_mcp_module_imports_and_registers_tools():
    """The feature MCP server module imports and exposes a FastMCP instance."""
    from mcp.server.fastmcp import FastMCP

    module = importlib.import_module("mcp_server.feature_mcp")

    assert isinstance(module.mcp, FastMCP)
