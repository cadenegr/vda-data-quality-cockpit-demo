"""Stand-in for databricks.sdk: the assistant's language model and Genie need Databricks, so offline they fail
with a clear message instead of a stack trace."""


class OfflineError(RuntimeError):
    pass


class WorkspaceClient:
    def __init__(self, *a, **k):
        raise OfflineError("Laptop mode: the assistant and Genie need Databricks and are not available offline.")
