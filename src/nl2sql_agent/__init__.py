"""NL2SQL Agent: a guarded, tool-calling Text-to-SQL agent for manufacturing ERP."""

from .agent import AgentResult, NL2SQLAgent
from .config import Settings
from .runtime import create_agent

__all__ = ["AgentResult", "NL2SQLAgent", "Settings", "create_agent"]
__version__ = "0.1.0"
