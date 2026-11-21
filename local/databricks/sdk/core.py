class Config:
    """Stand-in for databricks.sdk.core.Config (laptop mode)."""
    host = "laptop"

    def authenticate(self):
        return {}
