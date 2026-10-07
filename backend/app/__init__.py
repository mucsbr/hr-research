"""Company intelligence platform."""
import os

# This application does not export user data to third-party tracing services.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
